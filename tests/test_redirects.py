"""Exercise urllib's real error-handler chain without sockets or bridge keys."""
import contextlib
import io
import json
import os
import re
import socket
import sys
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hue_client
import hue_transport


class TrackedBody(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("Unbounded redirect response read")
        return super().read(size)


class FixtureHTTPSHandler(urllib.request.HTTPSHandler):
    status = 302
    content = b'not a JSON response'
    requests = []
    bodies = []

    def https_open(self, request):
        self.requests.append(request)
        headers = Message()
        headers['Location'] = 'https://redirect.invalid/collect-key'
        body = TrackedBody(self.content)
        self.bodies.append(body)
        response = urllib.response.addinfourl(body, headers, request.full_url, self.status)
        response.msg = 'Fixture response'
        return response


class RedirectTests(unittest.TestCase):
    def setUp(self):
        FixtureHTTPSHandler.requests = []
        FixtureHTTPSHandler.bodies = []
        self.factory = mock.patch.object(urllib.request, 'HTTPSHandler', FixtureHTTPSHandler)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def configure(self, status, content=b'not a JSON response'):
        FixtureHTTPSHandler.status = status
        FixtureHTTPSHandler.content = content
        FixtureHTTPSHandler.requests = []
        FixtureHTTPSHandler.bodies = []

    def assert_rejected_without_reading_or_following(self):
        self.assertEqual(len(FixtureHTTPSHandler.requests), 1)
        self.assertEqual(FixtureHTTPSHandler.bodies[0].read_sizes, [])
        self.assertTrue(FixtureHTTPSHandler.bodies[0].closed)

    def test_opener_rejects_every_redirect_before_reading_body(self):
        for code in (301, 302, 303, 307, 308):
            for method in ('GET', 'POST'):
                with self.subTest(code=code, method=method):
                    self.configure(code)
                    request = urllib.request.Request('https://bridge.invalid/api',
                        headers={'hue-application-key': 'fixture-key'}, method=method)
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        hue_transport.no_redirect_opener().open(request, timeout=5)
                    self.assertEqual(raised.exception.code, code)
                    self.assert_rejected_without_reading_or_following()

    def test_bridge_requests_reject_redirects_for_both_api_versions(self):
        for version in ('v1', 'v2'):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(version=version, code=code):
                    self.configure(code)
                    client = hue_client.HueClient({
                        'bridgeIp': '192.0.2.10', 'bridgeId': '001788fffe123456',
                        'username': 'fixture-key', 'apiVersion': version})
                    with self.assertRaises(hue_client.HueError):
                        client.get_snapshot()
                    self.assert_rejected_without_reading_or_following()

    def test_migration_bridge_id_lookup_rejects_redirects(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.configure(code)
                with self.assertRaises(hue_client.HueError):
                    hue_client.fetch_bridge_id('192.0.2.10')
                self.assert_rejected_without_reading_or_following()

    def test_pairing_discovery_bridge_id_probe_and_post_reject_redirects(self):
        scripts = re.findall(r"python3 - <<'PY'[^\n]*\n(.*?)\nPY", (ROOT / 'pair.sh').read_text(), re.S)
        self.assertEqual(len(scripts), 4)
        env = {'HUE_PLUGIN_DIR': str(ROOT), 'CACERT': str(hue_client.CACERT),
               'TARGET_IP': '192.0.2.10', 'BRIDGE_ID': '001788fffe123456',
               'HUE_APPLICATION_KEY': 'fixture-key', 'DEVICETYPE': 'philips#fixture'}
        for index, script in enumerate(scripts):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(path=index, code=code):
                    self.configure(code)
                    with mock.patch.dict(os.environ, env), mock.patch.object(socket, 'getaddrinfo', socket.getaddrinfo), contextlib.redirect_stdout(io.StringIO()):
                        if index == 1:  # Shell bridge-ID lookup deliberately returns empty on failure.
                            exec(compile(script, 'pair.sh heredoc', 'exec'), {})
                        else:
                            with self.assertRaises(urllib.error.HTTPError):
                                exec(compile(script, 'pair.sh heredoc', 'exec'), {})
                    self.assert_rejected_without_reading_or_following()

    def test_normal_bridge_response_still_uses_the_body_size_cap(self):
        self.configure(200, json.dumps({'errors': [], 'data': []}).encode())
        client = hue_client.HueClient({'bridgeIp': '192.0.2.10', 'username': 'fixture-key', 'apiVersion': 'v2'})
        self.assertEqual(client.get_snapshot()['lights'], {})
        self.assertEqual(FixtureHTTPSHandler.bodies[0].read_sizes, [hue_client._MAX_RESPONSE_BYTES + 1])

    def test_normal_pairing_and_discovery_responses_keep_their_size_cap(self):
        scripts = re.findall(r"python3 - <<'PY'[^\n]*\n(.*?)\nPY", (ROOT / 'pair.sh').read_text(), re.S)
        replies = [
            [{'internalipaddress': '192.0.2.10'}], {'bridgeid': '001788fffe123456'},
            {'errors': [], 'data': []}, [{'success': {'username': 'fixture-key'}}],
        ]
        env = {'HUE_PLUGIN_DIR': str(ROOT), 'CACERT': str(hue_client.CACERT),
               'TARGET_IP': '192.0.2.10', 'BRIDGE_ID': '001788fffe123456',
               'HUE_APPLICATION_KEY': 'fixture-key', 'DEVICETYPE': 'philips#fixture'}
        for script, reply in zip(scripts, replies):
            with self.subTest(reply=reply):
                self.configure(200, json.dumps(reply).encode())
                with mock.patch.dict(os.environ, env), mock.patch.object(socket, 'getaddrinfo', socket.getaddrinfo), contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(script, 'pair.sh heredoc', 'exec'), {})
                self.assertEqual(FixtureHTTPSHandler.bodies[0].read_sizes, [1048577])

    def test_oversized_normal_response_is_still_rejected(self):
        self.configure(200, b' ' * (hue_client._MAX_RESPONSE_BYTES + 1))
        client = hue_client.HueClient({'bridgeIp': '192.0.2.10', 'username': 'fixture-key', 'apiVersion': 'v2'})
        with self.assertRaisesRegex(hue_client.HueError, 'maximum allowed size'):
            client.get_snapshot()
        self.assertEqual(FixtureHTTPSHandler.bodies[0].read_sizes, [hue_client._MAX_RESPONSE_BYTES + 1])


if __name__ == '__main__':
    unittest.main()
