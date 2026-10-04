import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USERNAME = "0123456789abcdef" * 2
SUCCESS = [{"success": {"username": USERNAME}}]


class PairTests(unittest.TestCase):
    def run_pair(self, responses):
        # Load the real functions without running discovery or writing credentials.
        definitions = (ROOT / "pair.sh").read_text().split('mkdir -p "$STATE_DIR"', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / "responses.json").write_text(json.dumps(responses))
            (fixture / "count").write_text("0")
            (fixture / "bridge.py").write_text("""
import json, os
from pathlib import Path
fixture = Path(os.environ['PAIR_FIXTURE'])
count = int((fixture / 'count').read_text())
responses = json.loads((fixture / 'responses.json').read_text())
(fixture / 'count').write_text(str(count + 1))
print(responses[min(count, len(responses) - 1)])
""")
            harness = definitions + r'''
LOG_FILE="$PAIR_FIXTURE/pair.log"
python3() {
  if [[ "$1" == "-" ]]; then
    # Mock the HTTPS request only; run both response parsers with real Python.
    cat >/dev/null
    command python3 "$PAIR_FIXTURE/bridge.py"
  else
    command python3 "$@"
  fi
}
date() {
  if [[ "$1" == "+%s" ]]; then
    printf '%s\n' "$(( $(cat "$PAIR_FIXTURE/count") * 30 ))"
  else
    printf '2026-10-03 12:00:00\n'
  fi
}
sleep() { :; }
pair 192.0.2.10 001788fffe123456 || exit "$?"
'''
            script = fixture / "pair-test.sh"
            script.write_text(harness)
            (fixture / "pair.log").touch()
            result = subprocess.run(
                ["bash", str(script)],
                env={**os.environ, "PAIR_FIXTURE": directory},
                capture_output=True, text=True, timeout=10,
            )
            return result, (fixture / "pair.log").read_text(), int((fixture / "count").read_text())

    def test_success_returns_username_and_redacts_log(self):
        result, log, attempts = self.run_pair([json.dumps(SUCCESS)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), USERNAME)
        self.assertEqual(result.stderr, "")
        self.assertEqual(attempts, 1)
        self.assertIn('"username": "0123****"', log)
        self.assertNotIn(USERNAME, log)

    def test_link_button_error_retries_then_succeeds(self):
        error = [{"error": {"type": 101, "description": "link button not pressed"}}]
        result, log, attempts = self.run_pair([json.dumps(error), json.dumps(SUCCESS)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), USERNAME)
        self.assertEqual(attempts, 2)
        self.assertIn('"type": 101', log)
        self.assertNotIn(USERNAME, log)

    def test_malformed_response_retries_then_succeeds(self):
        result, log, attempts = self.run_pair(["not JSON", json.dumps(SUCCESS)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), USERNAME)
        self.assertEqual(attempts, 2)
        self.assertIn("<parse-error>", log)

    def test_link_button_error_stops_at_deadline(self):
        error = [{"error": {"type": 101, "description": "link button not pressed"}}]
        result, log, attempts = self.run_pair([json.dumps(error)])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(attempts, 3)
        self.assertEqual(log.count('"type": 101'), 3)

    def test_oversized_valid_response_is_rejected(self):
        # Valid JSON even when truncated: rejection must come from the size cap.
        response = json.dumps(SUCCESS) + " " * 1048577
        result, log, attempts = self.run_pair([response])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(attempts, 3)
        self.assertEqual(log.count("<parse-error>"), 3)
        self.assertNotIn(USERNAME, log)


if __name__ == "__main__":
    unittest.main()
