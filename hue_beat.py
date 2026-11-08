"""Continuous-sample aubio tempo tracking and a confidence-gated beat clock."""
from __future__ import annotations

import array
import ctypes as C
import ctypes.util
import math
import statistics
from collections import deque
from dataclasses import dataclass

from hue_client import HueError


@dataclass(frozen=True)
class BeatEstimate:
    seconds: float
    bpm: float
    confidence: float


class AubioTempo:
    """Typed bindings to the system library; no heuristic fallback detector."""
    def __init__(self, sample_rate=12000, hop_size=64, buffer_size=1024):
        name = ctypes.util.find_library('aubio')
        if not name:
            raise HueError('Music Sync needs aubio; install it with omarchy pkg add aubio')
        self.api = C.CDLL(name)
        signatures = {
            'new_aubio_tempo': (C.c_void_p, [C.c_char_p, C.c_uint, C.c_uint, C.c_uint]),
            'del_aubio_tempo': (None, [C.c_void_p]),
            'new_fvec': (C.c_void_p, [C.c_uint]),
            'del_fvec': (None, [C.c_void_p]),
            'fvec_get_data': (C.POINTER(C.c_float), [C.c_void_p]),
            'aubio_tempo_do': (None, [C.c_void_p, C.c_void_p, C.c_void_p]),
            'aubio_tempo_get_last_s': (C.c_float, [C.c_void_p]),
            'aubio_tempo_get_last': (C.c_uint, [C.c_void_p]),
            'aubio_tempo_get_bpm': (C.c_float, [C.c_void_p]),
            'aubio_tempo_get_confidence': (C.c_float, [C.c_void_p]),
            'aubio_tempo_set_threshold': (C.c_uint, [C.c_void_p, C.c_float]),
            'aubio_tempo_set_silence': (C.c_uint, [C.c_void_p, C.c_float]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, arguments
        self.sample_rate, self.hop_size, self.buffer_size = sample_rate, hop_size, buffer_size
        self.tempo = self.input = self.output = None
        self.pending = array.array('f')
        self.samples = 0
        self.last_sensitivity = None
        self.last_beat_sample = 0
        try:
            self.tempo = self.api.new_aubio_tempo(b'default', buffer_size, hop_size, sample_rate)
            self.input, self.output = self.api.new_fvec(hop_size), self.api.new_fvec(2)
            if not all((self.tempo, self.input, self.output)):
                raise HueError('Could not initialize aubio beat tracking')
            self.input_data = self.api.fvec_get_data(self.input)
            self.output_data = self.api.fvec_get_data(self.output)
            self.api.aubio_tempo_set_silence(self.tempo, -55)
        except BaseException:
            self.close()
            raise

    def feed(self, samples, sensitivity=1.0):
        if sensitivity != self.last_sensitivity:
            self.api.aubio_tempo_set_threshold(self.tempo, max(0.05, min(0.9, 0.3 / sensitivity)))
            self.last_sensitivity = sensitivity
        self.pending.extend(max(-1.0, min(1.0, value)) if math.isfinite(value) else 0 for value in samples)
        events = []
        consumed = 0
        while len(self.pending) - consumed >= self.hop_size:
            block = self.pending[consumed:consumed + self.hop_size]
            pointer = (C.c_float * self.hop_size).from_buffer(block)
            C.memmove(self.input_data, pointer, self.hop_size * C.sizeof(C.c_float))
            self.api.aubio_tempo_do(self.tempo, self.input, self.output)
            self.samples += self.hop_size
            consumed += self.hop_size
            beat_sample = self.api.aubio_tempo_get_last(self.tempo)
            # Aubio's single-frame output is silence-gated. Its tracked beat
            # position still advances when a sparse click falls in an adjacent
            # audio block; use that rhythmic grid and gate silence separately.
            if beat_sample > self.last_beat_sample:
                self.last_beat_sample = beat_sample
                events.append(BeatEstimate(beat_sample / self.sample_rate,
                    float(self.api.aubio_tempo_get_bpm(self.tempo)),
                    float(self.api.aubio_tempo_get_confidence(self.tempo))))
        del self.pending[:consumed]
        return events

    def close(self):
        for attribute, function in (('tempo', 'del_aubio_tempo'), ('input', 'del_fvec'), ('output', 'del_fvec')):
            handle = getattr(self, attribute, None)
            if handle:
                getattr(self.api, function)(handle)
                setattr(self, attribute, None)


class BeatTracker:
    """Map sample positions onto a monotonic clock and predict upcoming beats."""
    def __init__(self, sample_rate=12000, engine_factory=AubioTempo):
        self.sample_rate = sample_rate
        self.engine_factory = engine_factory
        self.engine = engine_factory(sample_rate=sample_rate)
        self.received = 0
        self.origin = None
        self.recent = deque(maxlen=8)
        self.last_observed = None
        self.period = None
        self.confidence = self.bpm = 0.0
        self.locked = False
        self.last_audio = -1e6
        self.beats = 0
        self.energy_pending = array.array('f')
        self.energy_history = deque(maxlen=100)
        self.rhythmic = False

    def reset(self):
        self.engine.close()
        self.engine = self.engine_factory(sample_rate=self.sample_rate)
        self.received = 0
        self.origin = None
        self.recent.clear()
        self.last_observed = self.period = None
        self.confidence = self.bpm = 0.0
        self.locked = False
        self.last_audio = -1e6
        self.energy_pending = array.array('f')
        self.energy_history.clear()
        self.rhythmic = False

    def feed(self, samples, now, sensitivity=1.0):
        if not samples:
            self.refresh(now)
            return
        samples = array.array('f', (max(-1.0, min(1.0, value)) if math.isfinite(value) else 0 for value in samples))
        if self.origin is None:
            self.origin = now - len(samples) / self.sample_rate
        expected_end = self.origin + (self.received + len(samples)) / self.sample_rate
        if abs(expected_end - now) > 0.5:
            self.reset()
            self.origin = now - len(samples) / self.sample_rate
        self.received += len(samples)
        self.energy_pending.extend(value if math.isfinite(value) else 0 for value in samples)
        window = self.sample_rate // 25
        while len(self.energy_pending) >= window:
            block = self.energy_pending[:window]
            del self.energy_pending[:window]
            self.energy_history.append(math.sqrt(sum(value * value for value in block) / window))
        if len(self.energy_history) >= 25:
            mean = statistics.mean(self.energy_history)
            self.rhythmic = mean > 0.001 and statistics.pstdev(self.energy_history) / mean > 0.1
        if sum(value * value for value in samples if math.isfinite(value)) / len(samples) > 1e-6:
            self.last_audio = now
        for event in self.engine.feed(samples, sensitivity):
            self.observe(event)
        self.refresh(now)

    def observe(self, event):
        if not all(math.isfinite(value) for value in (event.seconds, event.bpm, event.confidence)):
            return
        if not 40 <= event.bpm <= 220 or event.confidence < 0.08:
            self.locked = False
            self.recent.clear()
            return
        observed = self.origin + event.seconds
        period = 60 / event.bpm
        if self.last_observed is not None:
            interval = observed - self.last_observed
            if interval <= 0:
                return
            if abs(interval - period) > period * 0.15:
                self.recent.clear()
                self.locked = False
        self.last_observed = observed
        self.period, self.bpm, self.confidence = period, event.bpm, event.confidence
        self.recent.append((observed, period))
        self.beats += 1
        if len(self.recent) >= 4:
            periods = [item[1] for item in self.recent]
            median = statistics.median(periods)
            self.locked = max(abs(value - median) for value in periods) < median * 0.08

    def refresh(self, now):
        if not self.rhythmic or now - self.last_audio > 0.5 or self.last_observed is None or \
                now - self.last_observed > max(1.5, (self.period or 0.5) * 2.5):
            self.locked = False

    def upcoming(self, after):
        if not self.locked or not self.period or self.last_observed is None:
            return None
        steps = max(0, math.floor((after - self.last_observed) / self.period) + 1)
        return self.last_observed + steps * self.period

    def nearest(self, at):
        if not self.locked or not self.period or self.last_observed is None:
            return None
        return self.last_observed + round((at - self.last_observed) / self.period) * self.period

    def close(self):
        self.engine.close()
