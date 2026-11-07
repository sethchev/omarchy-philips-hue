#!/usr/bin/env python3
"""Validate the rhythm tracker without connecting to Hue or playing test audio."""
import argparse
import array
import json
import math
import statistics
import time

from hue_beat import BeatTracker
from hue_client import HueError

RATE = 12000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', choices=('metronome', 'playback'))
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--bpm', type=float, default=120)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 120 or not 50 <= args.bpm <= 200:
        parser.error('Use 5–120 seconds and 50–200 BPM')
    tracker = BeatTracker(RATE)
    capture = None
    errors, bpms, confidences = [], [], []
    locks = frames = 0
    previous_count = 0
    try:
        if args.source == 'playback':
            from hue_music import AudioCapture
            capture = AudioCapture()
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                now = time.monotonic()
                tracker.feed(capture.read(), now)
                if tracker.beats != previous_count:
                    print(json.dumps({'bpm': round(tracker.bpm, 1),
                        'confidence': round(tracker.confidence, 3), 'locked': tracker.locked}), flush=True)
                    previous_count = tracker.beats
                if tracker.locked:
                    bpms.append(tracker.bpm)
                    confidences.append(tracker.confidence)
                locks += tracker.locked
                frames += 1
                time.sleep(0.02)
        else:
            period = 60 / args.bpm
            for start in range(0, RATE * args.seconds, 480):
                samples = array.array('f')
                for index in range(start, start + 480):
                    age = (index / RATE) % period
                    samples.append(0.7 * math.exp(-age / 0.008) * math.cos(2 * math.pi * 1000 * age)
                                   if age < 0.04 else 0)
                now = (start + len(samples)) / RATE
                tracker.feed(samples, now)
                if tracker.beats != previous_count and tracker.last_observed is not None:
                    if now >= 12:
                        bpms.append(tracker.bpm)
                        confidences.append(tracker.confidence)
                        errors.append(abs(tracker.last_observed - round(tracker.last_observed / period) * period))
                    previous_count = tracker.beats
                if now >= 12:
                    locks += tracker.locked
                    frames += 1
        print(json.dumps({'source': args.source, 'tracked_events': tracker.beats,
            'locked_fraction': round(locks / max(1, frames), 3),
            'median_bpm': round(statistics.median(bpms), 2) if bpms else None,
            'mean_error_ms': round(statistics.mean(errors) * 1000, 2) if errors else None,
            'max_error_ms': round(max(errors) * 1000, 2) if errors else None,
            'median_raw_confidence': round(statistics.median(confidences), 3) if confidences else None}), flush=True)
        return 0 if tracker.beats else 1
    finally:
        if capture:
            capture.close()
        tracker.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except HueError as error:
        print(str(error))
        raise SystemExit(1)
