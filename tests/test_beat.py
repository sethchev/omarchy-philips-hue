"""Beat-clock tests and real aubio timing checks, with no lights or audio output."""
import array
import ctypes.util
import math
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hue_beat import AubioTempo, BeatEstimate, BeatTracker


class ClockTests(unittest.TestCase):
    def tracker(self):
        engine = mock.Mock()
        engine.feed.return_value = []
        tracker = BeatTracker(engine_factory=lambda **_kwargs: engine)
        tracker.origin = 100
        tracker.last_audio = 105
        return tracker

    def test_requires_consistent_tempo_and_phase_before_predicting(self):
        tracker = self.tracker()
        for seconds in (1, 1.5, 2):
            tracker.observe(BeatEstimate(seconds, 120, 1))
        self.assertFalse(tracker.locked)
        tracker.observe(BeatEstimate(2.5, 120, 1))
        self.assertTrue(tracker.locked)
        self.assertEqual(tracker.upcoming(102.6), 103)
        self.assertEqual(tracker.upcoming(103.1), 103.5)

    def test_off_grid_attack_breaks_lock_instead_of_resetting_beat_clock_blindly(self):
        tracker = self.tracker()
        for seconds in (1, 1.5, 2, 2.5):
            tracker.observe(BeatEstimate(seconds, 120, 1))
        tracker.observe(BeatEstimate(2.7, 120, 1))
        self.assertFalse(tracker.locked)
        self.assertIsNone(tracker.upcoming(103))

    def test_silence_and_stale_estimates_disable_predictions(self):
        tracker = self.tracker()
        for seconds in (1, 1.5, 2, 2.5):
            tracker.observe(BeatEstimate(seconds, 120, 1))
        tracker.last_audio = 102.5
        tracker.refresh(103.1)
        self.assertFalse(tracker.locked)

    def test_audio_gap_resets_the_sample_clock(self):
        tracker = self.tracker()
        tracker.feed([0.1] * 128, 100)
        tracker.locked = True
        tracker.feed([0.1] * 128, 102)
        self.assertFalse(tracker.locked)
        self.assertAlmostEqual(tracker.origin, 102 - 128 / 12000)


@unittest.skipUnless(ctypes.util.find_library('aubio'), 'Requires the aubio system library')
class AubioTimingTests(unittest.TestCase):
    def test_real_tracker_and_rendered_pulses_align_with_reference_clicks(self):
        from hue_music import Analyzer
        analyzer = Analyzer(BeatTracker())
        output = []
        try:
            for start in range(0, 12000 * 30, 480):
                samples = []
                for index in range(start, start + 480):
                    age = (index / 12000) % 0.5
                    samples.append(0.7 * math.exp(-age / 0.008) * math.cos(2 * math.pi * 1000 * age)
                                   if age < 0.04 else 0)
                now = (start + 480) / 12000
                analyzer.feed(samples, now)
                _, brightness = analyzer.position_and_brightness(now)
                value = brightness
                output.append((now, value, analyzer.tracker.locked))
            errors = []
            for beat in range(30, 59):
                at = beat * 0.5
                window = [item for item in output if abs(item[0] - at) < 0.2 and item[2]]
                self.assertTrue(window)
                errors.append(abs(max(window, key=lambda item: item[1])[0] - at))
            self.assertLess(sum(errors) / len(errors), 0.025)
            self.assertLess(max(errors), 0.06)
        finally:
            analyzer.close()

    def test_known_tempos_and_guitar_distractors_keep_the_click_grid(self):
        for bpm in (90, 120, 150):
            with self.subTest(bpm=bpm):
                tracker = BeatTracker()
                periods, errors, locked = [], [], []
                try:
                    for start in range(0, 12000 * 30, 480):
                        samples = []
                        period = 60 / bpm
                        for index in range(start, start + 480):
                            at = index / 12000
                            age = at % period
                            click = 0.7 * math.exp(-age / 0.008) * math.cos(2 * math.pi * 1000 * age) if age < 0.04 else 0
                            guitar = 0.07 * math.tanh(3 * math.sin(2 * math.pi * 330 * at)) * math.exp(-(at % 0.13) / 0.04)
                            samples.append(click + guitar)
                        now = (start + 480) / 12000
                        tracker.feed(samples, now)
                        if now >= 15:
                            locked.append(tracker.locked)
                            if tracker.locked:
                                periods.append(tracker.bpm)
                                beat = tracker.nearest(now)
                                errors.append(abs(beat - round(beat / period) * period))
                    self.assertGreater(sum(locked) / len(locked), 0.65)
                    self.assertGreater(len(periods), 100)
                    self.assertLess(abs(sum(periods) / len(periods) - bpm), 3)
                    self.assertLess(sum(errors) / len(errors), 0.04)
                finally:
                    tracker.close()

    def test_silence_stationary_noise_and_sustained_guitar_tone_do_not_lock(self):
        for kind in ('silence', 'noise', 'tone'):
            with self.subTest(kind=kind):
                tracker = BeatTracker()
                rng = random.Random(9)
                try:
                    for start in range(0, 12000 * 20, 480):
                        samples = [0 if kind == 'silence' else (rng.uniform(-0.2, 0.2) if kind == 'noise'
                                   else 0.3 * math.sin(2 * math.pi * 330 * index / 12000))
                                   for index in range(start, start + 480)]
                        now = (start + 480) / 12000
                        tracker.feed(samples, now)
                        if now > 10:
                            self.assertFalse(tracker.locked)
                finally:
                    tracker.close()

    def test_continuous_click_track(self):
        rate, bpm, seconds = 12000, 120, 30
        engine = AubioTempo(sample_rate=rate)
        events = []
        try:
            for start in range(0, rate * seconds, 480):
                samples = array.array('f')
                for index in range(start, start + 480):
                    age = index % (rate // 2)
                    value = 0.7 * math.exp(-age / (rate * 0.008)) * math.cos(2 * math.pi * 1000 * age / rate) if age < rate * 0.04 else 0
                    samples.append(value)
                events.extend(engine.feed(samples))
            usable = [event for event in events if event.seconds > 12]
            self.assertGreater(len(usable), 20)
            self.assertLess(max(abs(event.bpm - bpm) for event in usable), 4)
            errors = [abs(event.seconds - round(event.seconds * 2) / 2) for event in usable]
            self.assertLess(sum(errors) / len(errors), 0.06)
        finally:
            engine.close()
