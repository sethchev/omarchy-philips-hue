"""Real signal deadlines against a peer that keeps making slow progress."""
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hue_transport import DeadlineExceeded, operation_deadline


class DeadlineTests(unittest.TestCase):
    def test_deadline_interrupts_repeated_short_waits(self):
        handler = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with self.assertRaises(DeadlineExceeded):
            with operation_deadline(0.05):
                while True:
                    time.sleep(0.005)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_cli_reports_deadline_without_traceback(self):
        script = '''
import runpy, time
import hue_transport
from contextlib import contextmanager
original = hue_transport.operation_deadline
@contextmanager
def short_deadline():
    with original(0.05):
        yield
hue_transport.operation_deadline = short_deadline
import hue_client
hue_client.theme_sync = lambda *args, **kwargs: time.sleep(2)
hue_client.secure_log = lambda message: None
runpy.run_path('hue-api.py', run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, 'theme-sync'],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Hue operation timed out', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
