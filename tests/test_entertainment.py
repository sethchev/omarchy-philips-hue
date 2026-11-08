"""Entertainment pairing never prints keys or replaces regular control pairing."""
import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hue_client as hue
import hue_entertainment as entertainment

NORMAL = {'bridgeIp': '192.0.2.10', 'bridgeId': '001788fffe123456',
          'username': 'normal-key-01234567890123456789', 'apiVersion': 'v2', 'custom': 'preserved'}
USERNAME = 'entertainment-key-0123456789012345'
KEY = '0123456789abcdef' * 2
SUCCESS = [{'success': {'username': USERNAME, 'clientkey': KEY}}]


class EntertainmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'hue.json'
        patch = mock.patch.object(hue, 'CREDS_FILE', self.path)
        patch.start()
        self.addCleanup(patch.stop)
        hue.write_json_secure(self.path, NORMAL)
        self.now = 100

    def sleep(self, seconds):
        self.now += seconds

    def test_pair_saves_private_streaming_credentials_preserving_control_key(self):
        client = mock.Mock()
        with mock.patch.object(hue, 'HueClient', return_value=client), \
             mock.patch.object(entertainment.RegistrationClient, '_request', return_value=SUCCESS) as request:
            entertainment.pair()
        request.assert_called_once_with('POST', '', {
            'devicetype': 'omarchy-hue#entertainment', 'generateclientkey': True})
        saved = hue.read_json(self.path)
        self.assertEqual({key: value for key, value in saved.items() if key != 'entertainment'}, NORMAL)
        self.assertEqual(saved['entertainment'], {'username': USERNAME, 'clientKey': KEY,
                                               'bridgeId': NORMAL['bridgeId']})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        separate, key = entertainment.entertainment_credentials(saved)
        self.assertEqual(separate['username'], USERNAME)
        self.assertEqual(key, KEY)
        self.assertEqual([call.args[0] for call in client._v2_data.call_args_list],
                         ['/entertainment_configuration', '/bridge'])

    def test_link_button_retries_and_timeout_keeps_existing_credentials(self):
        with mock.patch.object(hue, 'HueClient'), \
             mock.patch.object(entertainment.RegistrationClient, '_request',
                 side_effect=entertainment.LinkButtonNeeded('press button')) as request:
            with self.assertRaisesRegex(hue.HueError, 'expired'):
                entertainment.pair(3, clock=lambda: self.now, sleep=self.sleep)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(hue.read_json(self.path), NORMAL)

    def test_press_after_first_attempt_completes_pairing(self):
        with mock.patch.object(hue, 'HueClient'), \
             mock.patch.object(entertainment.RegistrationClient, '_request',
                 side_effect=[entertainment.LinkButtonNeeded('press'), SUCCESS]):
            entertainment.pair(3, clock=lambda: self.now, sleep=self.sleep)
        self.assertEqual(self.now, 102)
        self.assertEqual(hue.read_json(self.path)['username'], NORMAL['username'])

    def test_registration_checks_identity_and_rejects_invalid_responses(self):
        client = entertainment.RegistrationClient(NORMAL)
        self.assertEqual(client._url(''), ('https://001788fffe123456/api', '001788fffe123456'))
        client._check_response(SUCCESS)
        for response in ({}, [], [None], [{'success': {}}],
                         [{'success': {'username': USERNAME, 'clientkey': 'invalid'}}],
                         [{'success': {'username': '../bad', 'clientkey': KEY}}]):
            with self.subTest(response=response), self.assertRaises(hue.HueError):
                client._check_response(response)
        with self.assertRaises(entertainment.LinkButtonNeeded):
            client._check_response([{'error': {'type': 101}}])
        with self.assertRaises(hue.HueError) as raised:
            client._check_response([{'error': {'type': 7, 'description': KEY}}])
        self.assertNotIn(KEY, str(raised.exception))
        with self.assertRaises(hue.HueError):
            entertainment.RegistrationClient(dict(NORMAL, bridgeId=''))

    def test_failed_new_key_verification_does_not_overwrite_existing_credentials(self):
        client = mock.Mock()
        client._v2_data.side_effect = [[], hue.HueError('verification failed')]
        with mock.patch.object(hue, 'HueClient', return_value=client), \
             mock.patch.object(entertainment.RegistrationClient, '_request', return_value=SUCCESS):
            with self.assertRaises(hue.HueError):
                entertainment.pair()
        self.assertEqual(hue.read_json(self.path), NORMAL)

    def test_changed_bridge_or_pairing_cannot_be_overwritten(self):
        def register(*_args):
            hue.write_json_secure(self.path, dict(NORMAL, username='changed'))
            return SUCCESS
        with mock.patch.object(hue, 'HueClient'), \
             mock.patch.object(entertainment.RegistrationClient, '_request', side_effect=register):
            with self.assertRaisesRegex(hue.HueError, 'settings changed'):
                entertainment.pair()
        self.assertEqual(hue.read_json(self.path)['username'], 'changed')
        self.assertNotIn('entertainment', hue.read_json(self.path))

    def test_final_compare_and_write_share_the_credential_lock(self):
        events = []

        @contextlib.contextmanager
        def guard():
            events.append('locked')
            try:
                yield
            finally:
                events.append('released')

        with mock.patch.object(hue, 'HueClient'), \
             mock.patch.object(entertainment.RegistrationClient, '_request', return_value=SUCCESS), \
             mock.patch.object(hue, 'credentials_guard', guard):
            entertainment.pair()
        self.assertEqual(events, ['locked', 'released'])
        self.assertIn('entertainment', hue.read_json(self.path))

    def test_saved_streaming_key_is_tied_to_its_bridge(self):
        saved = dict(NORMAL, entertainment={'username': USERNAME, 'clientKey': KEY, 'bridgeId': 'different'})
        with self.assertRaises(hue.HueError):
            entertainment.entertainment_credentials(saved)

    def test_cli_never_prints_credentials(self):
        output = io.StringIO()
        with mock.patch.object(hue, 'HueClient'), \
             mock.patch.object(entertainment.RegistrationClient, '_request', return_value=SUCCESS), \
             contextlib.redirect_stdout(output):
            self.assertEqual(entertainment.main(['pair']), 0)
        for secret in (KEY, USERNAME, NORMAL['username']):
            self.assertNotIn(secret, output.getvalue())
        self.assertIn('preserved', output.getvalue())
