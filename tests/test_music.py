"""Synthetic beats, room-scoped streaming, and capture/stream teardown."""
import copy
import array
import json
import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hue_client as hue
import hue_music as music
import test_hue_client as fixtures

AREA = '11111111-2222-4333-8444-555555555555'
ENTERTAINMENT = '11111111-3333-4333-8444-555555555555'


def resources():
    items = copy.deepcopy(fixtures.V2_RESOURCES)
    items.extend([
        {'id': ENTERTAINMENT, 'type': 'entertainment',
         'owner': {'rid': fixtures.V2_RESOURCES[0]['owner']['rid'], 'rtype': 'device'}},
        {'id': AREA, 'type': 'entertainment_configuration', 'status': 'inactive',
         'channels': [{'channel_id': 2, 'members': [{'service': {'rid': ENTERTAINMENT}}]}]},
    ])
    return items


class FixedTracker:
    def __init__(self, period=0.6):
        self.period = period
        self.locked = True
        self.beats = 0
        self.bpm = 60 / period
        self.confidence = 1.0

    def nearest(self, at):
        return round(at / self.period) * self.period if self.locked else None

    def feed(self, samples, now, sensitivity=1):
        pass

    def reset(self):
        self.locked = False

    def close(self):
        pass


class DefaultSinkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bin = Path(directory.name)
        (self.bin / 'pw-cat').symlink_to(sys.executable)
        env = mock.patch.dict(os.environ, {'PATH': str(self.bin)})
        env.start()
        self.addCleanup(env.stop)

    def pactl(self, code):
        script = self.bin / 'pactl'
        script.write_text(f'#!{sys.executable}\nimport os, time\n{code}')
        script.chmod(0o700)

    def test_accepts_exact_byte_limits_on_both_pipes(self):
        self.pactl("os.write(2, b'x' * 4096)\nos.write(1, b'fixture-sink\\n'.ljust(4096))\n")
        self.assertEqual(music.default_sink(), 'fixture-sink')

    def test_rejects_stdout_and_stderr_overflow_before_child_exit(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                self.pactl(f"os.write({fd}, b'x' * 4097)\ntime.sleep(30)\n")
                with self.assertRaisesRegex(hue.HueError, 'exceeded the size limit'):
                    music.default_sink()


class AnalyzerTests(unittest.TestCase):
    def test_unlocked_baseline_and_locked_10_to_75_percent_profile(self):
        tracker = FixedTracker(period=0.5)
        tracker.locked = False
        analyzer = music.Analyzer(tracker)
        self.assertEqual(analyzer.position_and_brightness(1), (0, 0.1))
        tracker.locked = True
        values = [analyzer.position_and_brightness(1 + tick * 0.04)[1] for tick in range(100)]
        self.assertAlmostEqual(min(values), 0.1)
        self.assertAlmostEqual(max(values), 0.75)

    def test_configurable_levels_are_absolute_and_reach_both_endpoints(self):
        for idle, peak in ((0.1, 0.75), (0.2, 0.9), (0.3, 0.3)):
            with self.subTest(idle=idle, peak=peak):
                analyzer = music.Analyzer(FixedTracker(period=0.6))
                values = [analyzer.position_and_brightness(tick * 0.04, idle=idle, peak=peak)[1]
                          for tick in range(100)]
                self.assertAlmostEqual(min(values[20:]), idle)
                self.assertAlmostEqual(max(values[20:]), peak)
                self.assertLessEqual(max(values), peak)

    def test_pace_changes_width_not_the_predicted_peak_time(self):
        widths = []
        for pace in (0.25, 1, 2):
            analyzer = music.Analyzer(FixedTracker(period=1))
            values = [analyzer.position_and_brightness(tick * 0.02, pace=pace)[1] for tick in range(100, 200)]
            widths.append(sum(value > 0.4 for value in values))
        self.assertGreater(widths[0], widths[1])
        self.assertGreater(widths[1], widths[2])

    def test_lost_lock_returns_to_low_level_without_a_bright_flash(self):
        tracker = FixedTracker()
        analyzer = music.Analyzer(tracker)
        values = []
        for tick in range(100):
            if tick == 40:
                tracker.locked = False
            position, brightness = analyzer.position_and_brightness(tick * 0.04)
            self.assertEqual(position, 0)
            self.assertTrue(0.1 <= brightness <= 0.75)
            values.append(brightness)
        self.assertAlmostEqual(values[-1], 0.1)
        self.assertTrue(all(b <= a for a, b in zip(values[40:], values[41:])))


class SmoothOutputTests(unittest.TestCase):
    def test_absolute_output_reaches_10_and_75_regardless_of_snapshot_brightness(self):
        lights = {'1': {'state': {'on': True, 'bri': 25}}}
        analyzer = music.Analyzer(FixedTracker(period=0.5))
        values = []
        for tick in range(1, 301):
            now = tick * 0.04
            position, brightness = analyzer.position_and_brightness(now)
            color = music.music_colors(['1'], [(0, ['1'])], lights, ['ff0000'], position, brightness, absolute=True)[0]
            self.assertEqual(color[2:], (0, 0))
            values.append((now, color[1]))
        self.assertAlmostEqual(min(value for _, value in values), 0.1)
        self.assertAlmostEqual(max(value for _, value in values), 0.75)
        errors = []
        for beat in range(5, 23):
            at = beat * 0.5
            window = [item for item in values if abs(item[0] - at) < 0.2]
            errors.append(abs(max(window, key=lambda item: item[1])[0] - at))
        self.assertLessEqual(max(errors), 0.04)

    def test_timing_offset_shifts_bright_peak_without_changing_tempo(self):
        peaks = []
        for offset in (-0.1, 0.1):
            analyzer = music.Analyzer(FixedTracker(period=1))
            values = []
            for tick in range(1, 181):
                now = tick * 0.02
                _, brightness = analyzer.position_and_brightness(now, timing=offset)
                if 2.65 < now < 3.35:
                    values.append((now, brightness))
            maximum = max(value for _, value in values)
            plateau = [now for now, value in values if abs(value - maximum) < 1e-6]
            peaks.append(sum(plateau) / len(plateau))
        self.assertAlmostEqual(peaks[1] - peaks[0], 0.2, delta=0.04)

    def test_phase_corrections_stay_within_explicit_absolute_levels(self):
        tracker = FixedTracker()
        analyzer = music.Analyzer(tracker)
        previous = 0.1
        for tick in range(1, 250):
            tracker.period = 0.5 if tick % 2 else 0.6
            _, value = analyzer.position_and_brightness(tick * 0.04)
            self.assertTrue(0.1 <= value <= 0.75)
            self.assertLess(abs(value - previous), 0.3)
            previous = value

    def test_theme_change_and_capture_stall_remain_smooth(self):
        smoother = music.ColorSmoother([(0, 0.5, 0, 0)], clock=lambda: 0)
        first = smoother.render([(0, 0, 0, 0.5)], now=0.04)[0]
        second = smoother.render([(0, 0, 0.5, 0)], now=100)[0]
        self.assertLessEqual(max(abs(a - b) for a, b in zip(first[1:], second[1:])), 0.025)


class MusicOptionsTests(unittest.TestCase):
    def test_pace_saves_with_other_options_and_rejects_invalid_values(self):
        from test_party import cli
        import hue_party as party
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with mock.patch.object(hue, 'CONFIG_FILE', path), \
                 mock.patch.object(cli, 'CONFIG_FILE', path), \
                 mock.patch.object(party, 'DIRECTORY', Path(directory)):
                hue.write_json_secure(path, {'scene': True, 'musicOptions': {}})
                options = {'pace': 1.5, 'intensity': 0.7, 'sensitivity': 1.2, 'timing': -0.12}
                cli.main(['hue-api.py', 'write-music-options', 'room', json.dumps(options)])
                expected = {'scene': True, 'musicOptions': {'room': options}}
                self.assertEqual(hue.read_theme_config(), expected)
                for invalid in ({'idle': 0.8, 'peak': 0.5}, {'idle': -0.1}, {'peak': 1.1}):
                    with self.subTest(invalid=invalid), self.assertRaises(hue.HueError):
                        cli.main(['hue-api.py', 'write-music-options', 'room', json.dumps(invalid)])
                    self.assertEqual(hue.read_theme_config(), expected)
                for value in (0, 2.1, True, float('nan')):
                    with self.subTest(value=value), self.assertRaises(hue.HueError):
                        cli.main(['hue-api.py', 'write-music-options', 'room', json.dumps({'pace': value})])
                    self.assertEqual(hue.read_theme_config(), expected)
                hue.write_json_secure(path, {'musicOptions': {'room': {'pace': 3}}})
                with self.assertRaises(hue.HueError):
                    hue.read_theme_config()


class CaptureContinuityTests(unittest.TestCase):
    def capture(self):
        capture = music.AudioCapture.__new__(music.AudioCapture)
        capture.process = mock.Mock()
        capture.process.poll.return_value = None
        capture.pending = bytearray()
        capture.discontinuous = False
        capture.last_data = music.time.monotonic()
        return capture

    def test_normal_buffered_audio_is_not_truncated_to_100_ms(self):
        capture = self.capture()
        raw = array.array('f', [0.1] * 3072).tobytes()
        with mock.patch.object(music.os, 'read', side_effect=[raw, BlockingIOError]):
            samples = capture.read()
        self.assertEqual(len(samples), 3072)
        self.assertFalse(capture.discontinuous)

    def test_large_backlog_explicitly_invalidates_the_beat_clock(self):
        capture = self.capture()
        raw = array.array('f', [0.1] * 7000).tobytes()
        with mock.patch.object(music.os, 'read', side_effect=[raw, BlockingIOError]):
            samples = capture.read()
        self.assertEqual(len(samples), 3000)
        self.assertTrue(capture.discontinuous)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.resources = resources()
        self.state = hue.HueClient(fixtures.HueClientTests().creds())._normalize_v2(self.resources)

    def test_area_is_selected_by_exact_room_membership(self):
        area, channels, lights = music.choose_area(self.resources, self.state, '4')
        self.assertEqual(area['id'], AREA)
        self.assertEqual(channels, [(2, ['7'])])
        self.assertEqual(set(lights), {'7'})

    def test_area_containing_lights_from_other_rooms_cannot_be_claimed(self):
        self.resources[-1]['channels'].append({'channel_id': 3, 'members': [{'service': {'rid': 'outside'}}]})
        with self.assertRaisesRegex(hue.HueError, 'exactly this room'):
            music.choose_area(self.resources, self.state, '4')

    def test_existing_active_stream_is_not_taken_over(self):
        self.resources[-1]['status'] = 'active'
        with self.assertRaisesRegex(hue.HueError, 'another app'):
            music.choose_area(self.resources, self.state, '4')

    def test_gradient_channels_preserve_mapping_to_one_light(self):
        self.resources[-1]['channels'].append({'channel_id': 3, 'members': [{'service': {'rid': ENTERTAINMENT}}]})
        _, channels, _ = music.choose_area(self.resources, self.state, '4')
        self.assertEqual(channels, [(2, ['7']), (3, ['7'])])

    def test_music_colors_follow_saved_order_preserve_brightness_and_leave_off_bulbs_dark(self):
        lights = {key: {'state': {'on': True, 'bri': 127}} for key in ('1', '2')}
        channels = [(0, ['1']), (1, ['2'])]
        colors = music.music_colors(['1', '2'], channels, lights,
                                    ['ff0000', '00ff00', '0000ff'], 1, 0.8)
        self.assertEqual(colors, [(0, 0, 0, 0.4), (1, 0.4, 0, 0)])
        lights['1']['state']['on'] = False
        self.assertEqual(music.music_colors(['1', '2'], channels, lights,
                            ['ff0000'], 0, 1)[0], (0, 0, 0, 0))

    def test_halfway_between_beats_blends_colors(self):
        lights = {'1': {'state': {'on': True, 'bri': 254}}}
        self.assertEqual(music.music_colors(['1'], [(0, ['1'])], lights,
                            ['ff0000', '00ff00'], 0.5, 1), [(0, 1, 1, 0)])


class MusicLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.bridge_ip = '192.0.2.10'
        self.client._v2_data.return_value = resources()
        normalized = hue.HueClient(fixtures.HueClientTests().creds())._normalize_v2(resources())
        self.client._normalize_v2.return_value = normalized
        self.capture = mock.Mock(sink='fixture-output')
        self.capture.read.return_value = []
        self.transport = mock.Mock()
        patches = [mock.patch.object(hue, 'read_json', return_value={}),
                   mock.patch.object(hue, 'HueClient', return_value=self.client),
                   mock.patch.object(hue, 'read_theme_config', return_value={}),
                   mock.patch.object(hue, 'accent_color', return_value='ff0000'),
                   mock.patch.object(hue, 'build_scene_palette', return_value=['ff0000', '00ff00']),
                   mock.patch.object(music, 'entertainment_credentials',
                       return_value=(fixtures.HueClientTests().creds(), '0123456789abcdef' * 2)),
                   mock.patch.object(music, 'AudioCapture', return_value=self.capture),
                   mock.patch.object(music, 'Dtls', return_value=self.transport),
                   mock.patch.object(music, 'default_sink', return_value='fixture-output'),
                   mock.patch.object(music, 'BeatTracker', side_effect=lambda *_args: FixedTracker())]
        self.started = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)

    def test_start_frame_stop_does_not_create_audio_files_or_poll_bridge_per_frame(self):
        session = music.MusicSession('4')
        self.client._request.assert_called_once_with('PUT', session.path, {'action': 'start'})
        self.client.reset_mock()
        session.frame()
        self.transport.send.assert_called_once()
        self.assertEqual(self.transport.send.call_args.args[0][:9], b'HueStream')
        self.client._request.assert_not_called()
        self.client._v2_data.assert_not_called()
        session.close()
        session.close()
        self.capture.close.assert_called_once()
        self.transport.close.assert_called_once()
        self.client._request.assert_called_once_with('PUT', session.path, {'action': 'stop'})

    def test_streamed_rgb_uses_absolute_concert_levels_after_entry_fade(self):
        session = music.MusicSession('4')
        base = music.time.monotonic()
        session.started = base - 1
        session.last_sink_check = base
        values = []
        for tick in range(1, 301):
            with mock.patch.object(music.time, 'monotonic', return_value=base + tick * 0.04):
                session.frame()
            packet = self.transport.send.call_args.args[0]
            _channel, red, green, blue = struct.unpack('!BHHH', packet[52:59])
            if tick > 150:
                values.append(max(red, green, blue) / 65535)
        self.assertAlmostEqual(min(values), 0.1, delta=0.002)
        self.assertAlmostEqual(max(values), 0.75, delta=0.002)
        session.close()

    def test_handshake_failure_stops_area_and_capture(self):
        self.started[7].side_effect = hue.HueError('DTLS failed')
        with self.assertRaises(hue.HueError):
            music.MusicSession('4')
        self.started[6].assert_not_called()
        self.assertEqual([call.args[2]['action'] for call in self.client._request.call_args_list], ['start', 'stop'])

    def test_lost_start_response_still_releases_the_entertainment_area(self):
        # The bridge may have processed the start before the response was lost;
        # cleanup must still send the matching stop.
        self.client._request.side_effect = [hue.HueError('Bridge request timed out', 'timeout'), None]
        with self.assertRaises(hue.HueError):
            music.MusicSession('4')
        self.assertEqual([call.args[2]['action'] for call in self.client._request.call_args_list],
                         ['start', 'stop'])

    def test_failure_before_start_does_not_send_a_stop(self):
        with mock.patch.object(music, 'default_sink',
                               side_effect=hue.HueError('No PipeWire playback output is available')):
            with self.assertRaises(hue.HueError):
                music.MusicSession('4')
        self.client._request.assert_not_called()

    def test_capture_failure_does_not_prevent_releasing_stream(self):
        session = music.MusicSession('4')
        self.capture.close.side_effect = OSError('capture failed')
        with self.assertRaises(OSError):
            session.close()
        self.transport.close.assert_called_once()
        self.assertEqual(self.client._request.call_args_list[-1].args[2], {'action': 'stop'})

    def test_playback_output_change_reopens_capture(self):
        session = music.MusicSession('4')
        self.started[8].return_value = 'new-output'
        session.frame()
        self.capture.close.assert_called_once()
        self.started[6].assert_called_with('new-output')
        session.close()

    def test_option_write_is_loaded_on_next_frame_without_waiting_one_second(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text('{}')
            with mock.patch.object(hue, 'CONFIG_FILE', path):
                session = music.MusicSession('4')
                session.last_settings = music.time.monotonic()
                session.last_sink_check = session.last_settings
                self.started[2].return_value = {'musicOptions': {'4': {'sensitivity': 2, 'intensity': 1}}}
                path.write_text('{"changed":true}')
                session.frame()
                self.assertEqual(session.settings['musicOptions']['4']['intensity'], 1)
                session.close()
