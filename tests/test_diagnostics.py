"""Classify transport/API failures without leaking bridge responses or keys."""
import contextlib
import http.client
import importlib.util
import io
import json
import ssl
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hue_client
from hue_transport import DeadlineExceeded
import test_hue_client as fixtures

spec = importlib.util.spec_from_file_location('hue_cli', ROOT / 'hue-api.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class DiagnosticTests(unittest.TestCase):
    def test_transport_failures_have_stable_codes_and_no_raw_details(self):
        for error, expected in (
            (urllib.error.HTTPError('https://secret-key.invalid', 401, 'secret-key', {}, None), 'auth'),
            (urllib.error.HTTPError('https://secret-key.invalid', 403, 'secret-key', {}, None), 'auth'),
            (ssl.SSLCertVerificationError('secret-key'), 'tls'),
            (urllib.error.URLError(ssl.SSLError('secret-key')), 'tls'),
            (TimeoutError('secret-key'), 'timeout'),
            (urllib.error.URLError(TimeoutError('secret-key')), 'timeout'),
            (ConnectionRefusedError('secret-key'), 'connection'),
            (urllib.error.URLError('secret-key'), 'connection'),
            (http.client.IncompleteRead(b'secret-key'), 'response'),
        ):
            with self.subTest(error=type(error).__name__, code=expected):
                client = hue_client.HueClient(fixtures.HueClientTests().creds())
                opener = mock.Mock()
                opener.open.side_effect = error
                client._get_opener = mock.Mock(return_value=opener)
                with self.assertRaises(hue_client.HueError) as raised:
                    client.get_snapshot()
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn('secret-key', str(raised.exception))

    def test_invalid_json_utf8_and_oversized_responses_are_classified(self):
        for raw, limit in ((b'not json secret-key', 100), (b'"\xff"', 100), (b'{}   ', 2)):
            with self.subTest(raw=raw):
                with self.assertRaises(hue_client.HueError) as raised:
                    hue_client._json_load_response(fixtures.FakeResponse(raw), limit)
                self.assertEqual(raised.exception.code, 'response')
                self.assertNotIn('secret-key', str(raised.exception))

    def test_v1_and_v2_api_authentication_errors_are_classified(self):
        for version, payload in (
            ('v1', [{'error': {'type': 1, 'description': 'secret-key'}}]),
            ('v2', {'errors': [{'description': 'unauthorized'}]}),
        ):
            with self.subTest(version=version):
                client = hue_client.HueClient(fixtures.HueClientTests().creds(version))
                with self.assertRaises(hue_client.HueError) as raised:
                    client._check_response(payload)
                self.assertEqual(raised.exception.code, 'auth')
                self.assertNotIn('secret-key', str(raised.exception))

    def test_malformed_resource_shapes_are_response_errors(self):
        for version, payload in (
            ('v1', {'7': None}),
            ('v2', [{'type': 'light', 'id': '7', 'owner': 'invalid'}]),
        ):
            with self.subTest(version=version):
                client = hue_client.HueClient(fixtures.HueClientTests().creds(version))
                client._request = mock.Mock(return_value=payload if version == 'v1' else {'errors': [], 'data': payload})
                with self.assertRaises(hue_client.HueError) as raised:
                    client.get_snapshot()
                self.assertEqual(raised.exception.code, 'response')

    def test_cli_structured_errors_and_legacy_text_mode(self):
        for error, code in ((hue_client.HueError('Bridge authentication failed', 'auth'), 'auth'),
                            (DeadlineExceeded('Hue operation timed out'), 'timeout')):
            for structured in (True, False):
                with self.subTest(code=code, structured=structured):
                    args = ['hue-api.py', 'get-state'] + (['--json-errors'] if structured else [])
                    output = io.StringIO()
                    with mock.patch.object(cli, 'HueClient', side_effect=error), contextlib.redirect_stderr(output):
                        self.assertEqual(cli.run(args), 1)
                    if structured:
                        self.assertEqual(json.loads(output.getvalue())['error']['code'], code)
                    else:
                        self.assertTrue(output.getvalue().startswith('Hue error: '))
                    self.assertNotIn('Traceback', output.getvalue())

    def test_cli_recalls_bridge_scene_by_id_with_group_for_v1_and_v2(self):
        for version in ('v1', 'v2'):
            with self.subTest(version=version):
                client = hue_client.HueClient(fixtures.HueClientTests().creds(version))
                client._request = mock.Mock(return_value={})
                scene = 'abc' if version == 'v1' else '55555555-5555-4555-8555-555555555555'
                with mock.patch.object(cli, 'HueClient', return_value=client):
                    self.assertEqual(cli.run(['hue-api.py', 'recall-scene', scene, '4', '--json-errors']), 0)
                expected = ('/groups/4/action', {'scene': scene}) if version == 'v1' else (
                    '/scene/' + scene, {'recall': {'action': 'active'}})
                client._request.assert_called_once_with('PUT', *expected)
