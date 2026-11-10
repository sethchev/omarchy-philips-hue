import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USERNAME = "0123456789abcdef" * 2
SUCCESS = [{"success": {"username": USERNAME}}]


class PairTests(unittest.TestCase):
    def run_discovery(self, output, after_output="", timeout=5):
        source = (ROOT / "pair.sh").read_text()
        definitions, main = source.split('mkdir -p "$STATE_DIR"', 1)
        selection = 'local_ip=""' + main.split('local_ip=""', 1)[1]
        selection = selection.split('\nif [[ -z "$local_ip" ]]; then', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / "avahi-output").write_bytes(output)
            avahi = fixture / "avahi-browse"
            avahi.write_text(f"#!{sys.executable}\n" + """
from pathlib import Path
import sys, time
sys.stdout.buffer.write(Path(__file__).with_name("avahi-output").read_bytes())
sys.stdout.buffer.flush()
""" + after_output)
            avahi.chmod(0o700)
            harness = fixture / "discovery-test.sh"
            harness.write_text(definitions + '''
PLUGIN_DIR="$HUE_TEST_PLUGIN_DIR"
discover_bridge_cloud() { printf '%s\\n' 192.0.2.20; }
''' + selection + '''
printf '%s\\t%s\\n' "$local_ip" "$discovered_bridge_id"
''')
            return subprocess.run(
                ["bash", str(harness)], capture_output=True, text=True, check=True,
                env={**os.environ, "PATH": f"{fixture}:{os.environ['PATH']}",
                     "HUE_TEST_PLUGIN_DIR": str(ROOT)}, timeout=timeout,
            ).stdout.splitlines()[-1]

    def test_local_discovery_accepts_byte_and_row_limits(self):
        prefix = b'address = [192.0.2.10]\ntxt = ["bridgeid=001788FFFE123456"]\n'
        for output in (prefix.ljust(65536), prefix + b"\n" * 509 + b"last row"):
            with self.subTest(size=len(output)):
                self.assertEqual(self.run_discovery(output), "192.0.2.10\t001788fffe123456")

    def test_local_discovery_rejects_overflow_before_parsing_and_falls_back(self):
        prefix = b'address = [192.0.2.10]\ntxt = ["bridgeid=001788FFFE123456"]\n'
        for output in (prefix.ljust(65537), prefix + b"\n" * 510 + b"extra row"):
            with self.subTest(size=len(output)):
                self.assertEqual(self.run_discovery(output, "time.sleep(3)\n", timeout=2), "192.0.2.20\t")

    def test_local_discovery_keeps_bounded_results_on_nonzero_exit_or_timeout(self):
        output = b'address = [192.0.2.10]\ntxt = ["bridgeid=001788FFFE123456"]\n'
        for after_output in ("sys.exit(1)\n", "time.sleep(8)\n"):
            with self.subTest(after_output=after_output):
                self.assertEqual(self.run_discovery(output, after_output, timeout=7),
                                 "192.0.2.10\t001788fffe123456")

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

    def test_pair_log_rejects_symlinks_and_fifos(self):
        definitions = (ROOT / "pair.sh").read_text().split('mkdir -p "$STATE_DIR"', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            target = fixture / "target"
            target.write_text("unchanged")
            log = fixture / "pair.log"
            script = fixture / "log-test.sh"
            script.write_text(definitions + '\nLOG_FILE="$PAIR_FIXTURE/pair.log"\nprintf "entry\\n" | append_pair_log\n')
            env = {**os.environ, "PAIR_FIXTURE": directory}
            log.symlink_to(target)
            subprocess.run(["bash", str(script)], env=env, check=True, timeout=3)
            self.assertEqual(target.read_text(), "unchanged")
            log.unlink()
            os.mkfifo(log)
            subprocess.run(["bash", str(script)], env=env, check=True, timeout=3)
            log.unlink()
            subprocess.run(["bash", str(script)], env=env, check=True, timeout=3)
            self.assertEqual(log.read_text(), "entry\n")
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

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
