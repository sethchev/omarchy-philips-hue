"""Local PipeWire analysis and theme-colored Hue Entertainment rendering."""
from __future__ import annotations

import array
import colorsys
import math
import os
import shutil
import subprocess
import sys
import time

import hue_client as hue
from hue_entertainment import entertainment_credentials
from hue_beat import BeatTracker
from hue_stream import Dtls, encode_frame
from hue_transport import DeadlineExceeded, operation_deadline

FRAME_INTERVAL = 0.04
COLOR_SLEW = 0.24
SAMPLE_RATE = 12000


def choose_area(resources, state, room_id):
    group = state['groups'].get(room_id)
    if not group:
        raise hue.HueError('Room is no longer available')
    lights = {key: light for key, light in state['lights'].items()
              if key in group.get('lights', []) and light.get('has_color')}
    devices = {light.get('device_id') for light in lights.values()}
    owners = {item['id']: item.get('owner', {}).get('rid') for item in resources
              if item.get('type') == 'entertainment'}
    for area in resources:
        if area.get('type') != 'entertainment_configuration':
            continue
        channels = area.get('channels', [])
        members = {owners.get(member.get('service', {}).get('rid'))
                   for channel in channels for member in channel.get('members', [])}
        # Streaming takes control of the entire area. Never claim other rooms.
        if members == devices and devices and None not in devices and 1 <= len(channels) <= 20:
            if area.get('status') != 'inactive':
                raise hue.HueError('This Entertainment Area is already being used by another app')
            mapping = []
            for channel in channels:
                ids = [key for key, light in lights.items() if light.get('device_id') in
                       {owners.get(member.get('service', {}).get('rid')) for member in channel.get('members', [])}]
                channel_id = channel.get('channel_id')
                if not ids or not isinstance(channel_id, int) or not 0 <= channel_id <= 255:
                    raise hue.HueError('Invalid Entertainment channel layout', 'response')
                mapping.append((channel_id, ids))
            return area, mapping, lights
    raise hue.HueError('Create an Entertainment Area containing exactly this room’s color lights in the Hue app')


def default_sink():
    if not shutil.which('pactl') or not shutil.which('pw-cat'):
        raise hue.HueError('Music Sync requires pw-cat and pactl (PipeWire/PulseAudio tools)')
    try:
        result = subprocess.run(['pactl', 'get-default-sink'], capture_output=True,
                                text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        raise hue.HueError('Could not find the PipeWire playback output') from None
    sink = result.stdout.strip()
    if not sink or len(sink) > 1024 or '\n' in sink:
        raise hue.HueError('No PipeWire playback output is available')
    return sink


class AudioCapture:
    def __init__(self, sink=None):
        self.sink = sink or default_sink()
        self.pending = bytearray()
        self.discontinuous = False
        self.last_data = time.monotonic()
        self.process = subprocess.Popen(['pw-cat', '--record', '--raw', '--format=f32',
            '--rate=' + str(SAMPLE_RATE), '--channels=1', '--latency=40ms',
            '--target=' + self.sink, '--properties={"stream.capture.sink":true,"node.name":"omarchy-hue-music","node.description":"Hue Music Sync"}', '-'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(self.process.stdout.fileno(), False)

    def read(self):
        if self.process.poll() is not None:
            raise hue.HueError('PipeWire capture stopped; check the playback output')
        for _ in range(8):
            try:
                block = os.read(self.process.stdout.fileno(), 16384)
            except BlockingIOError:
                break
            if not block:
                break
            self.pending.extend(block)
            self.last_data = time.monotonic()
        if time.monotonic() - self.last_data > 5:
            raise hue.HueError('PipeWire capture is not receiving playback samples')
        # Tempo tracking requires continuous samples. Only discard on a real
        # backlog and explicitly invalidate the beat clock when that happens.
        usable = len(self.pending) // 4 * 4
        start = 0
        if usable > SAMPLE_RATE // 2 * 4:
            start = usable - SAMPLE_RATE // 4 * 4
            self.discontinuous = True
        raw = bytes(self.pending[start:usable])
        del self.pending[:usable]
        samples = array.array('f')
        samples.frombytes(raw)
        return samples

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.process.stdout.close()


class Analyzer:
    def __init__(self, tracker=None):
        self.tracker = tracker if tracker is not None else BeatTracker(SAMPLE_RATE)
        self.render_time = None
        self.brightness = 0.10

    @property
    def beats(self):
        return self.tracker.beats

    def feed(self, samples, now, sensitivity=1.0):
        self.tracker.feed(samples, now, sensitivity)

    def reset(self):
        self.tracker.reset()

    def close(self):
        self.tracker.close()

    def position_and_brightness(self, now, idle=0.10, peak=0.75, pace=1.0, timing=0.0):
        """Absolute concert levels: ease up before a predicted beat, down after."""
        elapsed = 0 if self.render_time is None else max(0, min(0.1, now - self.render_time))
        self.render_time = now
        target = idle
        max_slope = 1.0
        if self.tracker.locked and self.tracker.period:
            period = self.tracker.period
            duration = min(1.2, period * min(0.9, 0.8 / math.sqrt(pace)))
            plateau = min(0.06, duration * 0.2)
            at = now - timing
            beat = self.tracker.nearest(at)
            if beat is not None:
                distance = abs(at - beat)
                if distance <= plateau / 2:
                    target = peak
                elif distance < duration / 2:
                    progress = (distance - plateau / 2) / ((duration - plateau) / 2)
                    target = idle + (peak - idle) * (0.5 + 0.5 * math.cos(math.pi * progress))
            # Match the slope to the requested levels and rise time. A fixed
            # low slew limit would prevent the peak arriving on the beat.
            max_slope = max(1.0, (peak - idle) * math.pi / max(0.01, duration - plateau))
        change = max_slope * elapsed
        self.brightness += max(-change, min(change, target - self.brightness))
        self.brightness = max(0.0, min(1.0, self.brightness))
        return 0.0, self.brightness


class ColorSmoother:
    """Bound actual streamed RGB changes, including theme and option changes."""
    def __init__(self, colors, clock=time.monotonic, rate_limit=COLOR_SLEW):
        self.colors = {channel: (red, green, blue) for channel, red, green, blue in colors}
        self.clock = clock
        self.updated = clock()
        self.rate_limit = rate_limit

    def render(self, targets, now=None):
        now = self.clock() if now is None else now
        elapsed = max(0, min(0.1, now - self.updated))
        self.updated = now
        # The beat-clock envelope already eases each pulse. Keep this short;
        # its known delay is included in pulse scheduling above.
        blend = 1 - math.exp(-elapsed / 0.08)
        # Bound every RGB frame. Pulse rendering uses a coordinated limit so
        # increasing depth also increases the visible dip instead of clipping it.
        limit = self.rate_limit * elapsed
        output = []
        for channel, red, green, blue in targets:
            previous = self.colors.get(channel, (0.0, 0.0, 0.0))
            current = tuple(old + max(-limit, min(limit, (target - old) * blend))
                            for old, target in zip(previous, (red, green, blue)))
            self.colors[channel] = current
            output.append((channel, *current))
        return output


def starting_colors(channels, lights):
    colors = {}
    for key, light in lights.items():
        state = light['state']
        power = state.get('on') and state.get('reachable', True)
        value = state.get('bri', 254) / 254 if power else 0
        colors[key] = colorsys.hsv_to_rgb(state.get('hue', 0) / 65535,
                                        state.get('sat', 0) / 254, value)
    return [(channel, *(sum(colors[key][index] for key in members) / len(members)
                         for index in range(3))) for channel, members in channels]


def music_colors(ids, channels, lights, palette, position, brightness, absolute=False):
    palette_rgb = [tuple(int(color[index:index + 2], 16) / 255 for index in (0, 2, 4)) for color in palette]
    colors = {}
    for index, key in enumerate(ids):
        offset = index + (position if len(ids) == 1 else -position)
        base = math.floor(offset)
        fraction = offset - base
        first, second = palette_rgb[base % len(palette_rgb)], palette_rgb[(base + 1) % len(palette_rgb)]
        light = lights[key]
        power = light['state'].get('on') and light['state'].get('reachable', True)
        scale = brightness * (1 if absolute else light['state'].get('bri', 254) / 254) if power else 0
        mixed = tuple(a + (b - a) * fraction for a, b in zip(first, second))
        # Keep the color's value steady while blending, rather than dimming
        # halfway between complementary palette colors and brightening again.
        peak = max(mixed)
        colors[key] = tuple(value / peak * scale if peak > 0 else 0 for value in mixed)
    result = []
    for channel_id, members in channels:
        rgb = [colors[key] for key in members if key in colors]
        result.append((channel_id, *(sum(value[index] for value in rgb) / len(rgb) if rgb else 0 for index in range(3))))
    return result


class MusicSession:
    def __init__(self, room_id):
        credentials, key = entertainment_credentials(hue.read_json(hue.CREDS_FILE, {}))
        self.client = hue.HueClient(credentials, api_version='v2')
        self.connection = self.capture = None
        self.activated = False
        # The start request may reach the bridge and still fail with a timeout,
        # so cleanup must attempt the matching stop even without a confirmation.
        self.start_attempted = False
        self.sequence = 0
        self.room_id = room_id
        self.analyzer = None
        self.settings = {}
        self.palette = []
        self.last_settings = self.last_sink_check = 0.0
        resources = self.client._v2_data()
        state = self.client._normalize_v2(resources)
        self.area, self.channels, self.lights = choose_area(resources, state, room_id)
        self.original_colors = starting_colors(self.channels, self.lights)
        chroma = [(channel, *(value / max(red, green, blue) if max(red, green, blue) > 0 else 0
                              for value in (red, green, blue)))
                  for channel, red, green, blue in self.original_colors]
        self.smoother = ColorSmoother(chroma)
        self.started = None
        self.group = dict(state['groups'][room_id], id=room_id)
        if any(item.get('type') == 'entertainment_configuration' and item.get('status') == 'active'
               for item in resources):
            raise hue.HueError('Another Hue Entertainment stream is active; stop it first')
        try:
            self.analyzer = Analyzer()
            sink = default_sink()
            self.start_attempted = True
            self.client._request('PUT', self.path, {'action': 'start'})
            self.activated = True
            self.connection = Dtls(self.client.bridge_ip, credentials['username'], key)
            self.capture = AudioCapture(sink)
            self.started = time.monotonic()
            self.reload_settings()
        except BaseException:
            self.close()
            raise

    @property
    def path(self):
        return '/entertainment_configuration/' + self.area['id']

    def reload_settings(self):
        self.settings = hue.read_theme_config()
        anchor = hue.accent_color()
        slug = hue._read_text_limited(hue.STATE_HOME / 'omarchy/current/theme.name').strip()
        anchor = str(self.settings.get('themes', {}).get(slug) or anchor or '').lstrip('#').lower()
        if not hue.re.fullmatch(r'[0-9a-f]{6}', anchor):
            raise hue.HueError('No valid theme palette is available')
        self.palette = list(dict.fromkeys([anchor] + hue.build_scene_palette(anchor)))
        self.settings_stamp = self.config_stamp()

    @staticmethod
    def config_stamp():
        try:
            return hue.CONFIG_FILE.stat().st_mtime_ns
        except OSError:
            return None

    def frame(self):
        from hue_party import ordered_ids
        now = time.monotonic()
        if now - self.last_settings >= 1 or self.config_stamp() != self.settings_stamp:
            self.reload_settings()
            self.last_settings = now
        if now - self.last_sink_check >= 5:
            sink = default_sink()
            if sink != self.capture.sink:
                self.capture.close()
                self.capture = AudioCapture(sink)
                self.analyzer.reset()
            self.last_sink_check = now
        options = self.settings.get('musicOptions', {}).get(self.room_id, {})
        samples = self.capture.read()
        if getattr(self.capture, 'discontinuous', False) is True:
            self.analyzer.reset()
            self.capture.discontinuous = False
        self.analyzer.feed(samples, now, options.get('sensitivity', 1.0))
        position, brightness = self.analyzer.position_and_brightness(now, idle=options.get('idle', 0.10),
            peak=options.get('peak', 0.75), pace=options.get('pace', 1.0), timing=options.get('timing', 0.0))
        ids = [key for key in ordered_ids(self.group, self.settings) if key in self.lights]
        # Smooth chroma separately so it cannot flatten or delay the beat's
        # absolute brightness envelope. This removes the old snapshot ceiling.
        colors = music_colors(ids, self.channels, self.lights, self.palette, position, 1, absolute=True)
        colors = [(channel, red * brightness, green * brightness, blue * brightness)
                  for channel, red, green, blue in self.smoother.render(colors, now)]
        entry = max(0, min(1, (now - self.started) / 0.4))
        entry = entry * entry * (3 - 2 * entry)
        original = {channel: (red, green, blue) for channel, red, green, blue in self.original_colors}
        colors = [(channel, *(old + (new - old) * entry for old, new in
                              zip(original[channel], (red, green, blue))))
                  for channel, red, green, blue in colors]
        self.connection.send(encode_frame(self.area['id'], self.sequence, colors))
        self.sequence = (self.sequence + 1) % 256

    def close(self):
        try:
            if self.capture:
                self.capture.close()
        finally:
            self.capture = None
            try:
                if self.connection:
                    self.connection.close()
            finally:
                self.connection = None
                try:
                    if self.activated or self.start_attempted:
                        try:
                            with operation_deadline(5):
                                self.client._request('PUT', self.path, {'action': 'stop'})
                        except (hue.HueError, OSError, DeadlineExceeded):
                            pass  # The bridge may be gone; the area times out on its own.
                        finally:
                            self.activated = False
                            self.start_attempted = False
                finally:
                    if self.analyzer:
                        self.analyzer.close()
                        self.analyzer = None
