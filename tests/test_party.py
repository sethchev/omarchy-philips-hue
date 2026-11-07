"""Party behavior is exercised with fixture lights; never use real credentials."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hue_client as hue
import hue_party as party

spec = importlib.util.spec_from_file_location('party_cli', ROOT / 'hue-api.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class PartyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        ipc = mock.patch.object(party, 'request', return_value={})
        ipc.start()
        self.addCleanup(ipc.stop)
        for module, key, value in ((party, 'DIRECTORY', self.directory),
                                   (party, 'STATUS', self.directory / 'status.json'),
                                   (hue, 'CONFIG_FILE', self.directory / 'config.json'),
                                   (cli, 'CONFIG_FILE', self.directory / 'config.json')):
            patch = mock.patch.object(module, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        hue.write_json_secure(hue.CONFIG_FILE, {'themeSync': {'room': False}})
        self.state = {'lights': {
            key: {'api_id': key, 'has_color': True, 'state': {'on': True, 'reachable': True,
                'hue': 1234, 'sat': 100, 'colormode': 'hs'}} for key in ('1', '2', '3')},
            'groups': {'room': {'type': 'Room', 'lights': ['1', '2', '3'], 'control_id': 'control'},
                       'zone': {'type': 'Zone', 'lights': ['1'], 'control_id': 'zone-control'}}}
        self.client = mock.Mock()
        self.client.get_state.side_effect = lambda: copy.deepcopy(self.state)
        self.now = 100
        self.engine = party.Party(self.client, clock=lambda: self.now)
        for key, value in (('accent_color', 'ff0000'),
                           ('build_scene_palette', ['ff0000', '00ff00', '0000ff'])):
            patch = mock.patch.object(hue, key, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def colors(self):
        return [(call.args[0], call.args[1]['hue']) for call in self.client.put_light.call_args_list]

    def test_music_is_exclusive_and_stopping_does_not_start_timed_party(self):
        with mock.patch('hue_music.MusicSession') as factory:
            self.engine.start_music('room')
            self.assertEqual(self.engine.interval(), 0.04)
            self.assertEqual(hue.read_json(party.STATUS)['musicRooms'], {'room': True})
            self.assertEqual(hue.read_json(party.STATUS)['rooms'], {})
            factory.return_value.analyzer.beats = 7
            self.engine.publish()
            self.assertEqual(hue.read_json(party.STATUS)['musicBeats'], {'room': 7})
            self.client.reset_mock()
            self.engine.frame()
            factory.return_value.frame.assert_called_once()
            self.client.put_light.assert_not_called()
            self.client.get_state.assert_not_called()
            self.engine.stop_music('room')
            factory.return_value.close.assert_called_once()
            self.assertEqual(self.engine.interval(), 1.5)
            self.assertNotIn('room', self.engine.sessions)
            self.assertEqual(hue.read_json(party.STATUS)['musicRooms'], {})

    def test_switching_between_party_and_music_never_runs_both_for_the_room(self):
        with mock.patch('hue_music.MusicSession') as factory:
            self.engine.start('room')
            self.assertEqual(hue.read_json(party.STATUS)['rooms'], {'room': True})
            self.engine.start_music('room')
            self.assertEqual(hue.read_json(party.STATUS)['rooms'], {})
            self.client.reset_mock()
            for _ in range(50):
                self.now += 0.04
                self.engine.frame()
            self.client.put_light.assert_not_called()
            self.client.put_group.assert_not_called()
            self.engine.start('room')
            factory.return_value.close.assert_called_once()
            self.assertEqual(hue.read_json(party.STATUS)['musicRooms'], {})
            self.assertEqual(hue.read_json(party.STATUS)['rooms'], {'room': True})
            self.engine.frame()
            self.assertGreater(self.client.put_light.call_count, 0)

    def test_music_failure_releases_session_even_if_bridge_cleanup_fails(self):
        with mock.patch('hue_music.MusicSession') as factory:
            self.engine.start_music('room')
            factory.return_value.frame.side_effect = hue.HueError('stream failed')
            factory.return_value.close.side_effect = hue.HueError('bridge offline')
            with self.assertRaises(hue.HueError):
                self.engine.frame()
            self.assertFalse(self.engine.sessions)
            self.assertEqual(hue.read_json(party.STATUS)['rooms'], {})

    def test_leaving_music_restores_brightness_even_before_a_scene_or_manual_action(self):
        for light in self.state['lights'].values():
            light['has_bri'] = True
            light['state']['bri'] = 127
        with mock.patch('hue_music.MusicSession'):
            self.engine.start_music('room')
            for light in self.state['lights'].values():
                light['state']['bri'] = 190
            self.engine.stop('room', restore=False)
        self.assertEqual([call.args[1] for call in self.client.put_light.call_args_list],
                         [{'bri': 127}] * 3)

    def test_manual_light_control_stops_only_the_music_room(self):
        with mock.patch('hue_music.MusicSession') as factory:
            self.engine.start_music('room')
            self.engine.stop_music_target('1')
            self.assertFalse(self.engine.sessions)
            factory.return_value.close.assert_called_once()

    def test_single_light_advances_at_each_one_point_five_seconds(self):
        self.state['groups']['room']['lights'] = ['1']
        self.engine.start('room')
        for elapsed, color in ((0, 'ff0000'), (1.49, 'ff0000'), (1.5, '00ff00'),
                               (3, '0000ff'), (4.5, 'ff0000')):
            self.client.reset_mock()
            self.now = 100 + elapsed
            self.engine.frame()
            self.assertEqual(self.colors(), [('1', hue._hex_to_hs(color)[0])])
            self.assertEqual(self.client.put_light.call_args.args[1]['transitiontime'], 5)
            self.assertNotIn('bri', self.client.put_light.call_args.args[1])
            self.assertNotIn('on', self.client.put_light.call_args.args[1])

    def test_three_lights_shift_each_old_color_to_the_next_bulb(self):
        self.engine.start('room')
        self.engine.frame()
        initial = self.colors()
        self.client.reset_mock()
        self.now += 1.5
        self.engine.frame()
        self.assertEqual([color for _, color in self.colors()], [initial[2][1], initial[0][1], initial[1][1]])
        self.client.reset_mock()
        self.now += 30  # Slow frames skip straight to the current position.
        self.engine.frame()
        self.assertEqual([color for _, color in self.colors()], [initial[0][1], initial[1][1], initial[2][1]])

    def test_all_palette_colors_flow_even_when_there_are_fewer_lights(self):
        ids = ['1', '2']
        palette = ['a', 'b', 'c', 'd', 'e']
        frames = [dict(party.frame_colors(ids, palette, step)) for step in range(5)]
        self.assertEqual({frame['1'] for frame in frames}, set(palette))
        for previous, current in zip(frames, frames[1:]):
            self.assertEqual(previous['1'], current['2'])
        self.assertEqual(party.frame_colors(['1'] * 7, palette, 0)[5][1], 'a')

    def test_off_unreachable_and_white_bulbs_do_not_consume_positions(self):
        self.state['lights']['2']['has_color'] = False
        self.state['lights']['3']['state']['reachable'] = False
        self.engine.start('room')
        self.engine.frame()
        self.assertEqual(self.colors(), [('1', 0)])
        self.state['lights']['3']['state']['reachable'] = True
        self.now += 1.5
        self.client.reset_mock()
        self.engine.frame()
        self.assertEqual(self.colors(), [('1', hue._hex_to_hs('0000ff')[0]), ('3', 0)])
        self.state['lights']['1']['state']['on'] = False
        self.client.reset_mock()
        self.engine.frame()
        self.assertEqual(self.colors(), [('3', 0)])

    def test_saved_order_ignores_removed_lights_and_appends_new_ones(self):
        hue.write_json_secure(hue.CONFIG_FILE, {'partyOrder': {'room': ['3', 'gone', '1']}})
        self.engine.start('room')
        self.engine.frame()
        self.assertEqual([key for key, _ in self.colors()], ['3', '1', '2'])

    def test_overlapping_party_is_rejected_and_scene_control_id_stops_it(self):
        self.engine.start('room')
        with self.assertRaisesRegex(hue.HueError, 'overlapping'):
            self.engine.start('zone')
        self.engine.stop_overlapping('zone-control')
        self.assertFalse(self.engine.sessions)
        self.client.put_light.assert_not_called()
        self.assertEqual(hue.read_json(party.STATUS)['rooms'], {})

    def test_stopping_unsynced_party_restores_colors_without_power_or_brightness(self):
        self.engine.start('room')
        self.engine.stop('room')
        self.assertFalse(self.engine.sessions)
        for call in self.client.put_light.call_args_list:
            self.assertEqual(call.args[1], {'hue': 1234, 'sat': 100, 'transitiontime': 5})

    def test_stopping_synced_party_reapplies_theme_after_releasing_ownership(self):
        hue.write_json_secure(hue.CONFIG_FILE, {})
        self.engine.start('room')
        def sync(**kwargs):
            self.assertEqual(kwargs, {'room_id': 'room'})
            self.assertEqual(hue.read_json(party.STATUS)['lights'], [])
        with mock.patch.object(hue, 'theme_sync', side_effect=sync):
            self.engine.stop('room')
        self.client.put_light.assert_not_called()

    def test_theme_changes_are_loaded_without_restarting(self):
        self.engine.start('room')
        self.engine.frame()
        self.client.reset_mock()
        with mock.patch.object(hue, 'accent_color', return_value='ffffff'), \
             mock.patch.object(hue, 'build_scene_palette', return_value=['ffffff', 'ffff00']):
            self.engine.frame()
        self.assertEqual(self.colors(), [('1', 0), ('2', hue._hex_to_hs('ffff00')[0]), ('3', 0)])
        self.assertEqual(self.client.put_light.call_args_list[0].args[1]['sat'], 0)

    def test_one_rejected_bulb_does_not_block_the_remaining_lights(self):
        self.engine.start('room')
        self.client.put_light.side_effect = [hue.HueError('rejected'), None, None]
        with self.assertRaises(hue.HueError):
            self.engine.frame()
        self.assertEqual(self.client.put_light.call_count, 3)

    def test_disconnected_bridge_stops_frame_early_for_backoff(self):
        self.engine.start('room')
        self.client.put_light.side_effect = hue.HueError('unavailable', 'connection')
        with self.assertRaises(hue.HueError):
            self.engine.frame()
        self.assertEqual(self.client.put_light.call_count, 1)

    def test_theme_hook_skips_party_lights_including_through_overlapping_zones(self):
        self.engine.start('room')
        with party.lock_file('worker.lock') as lease:
            party.fcntl.flock(lease, party.fcntl.LOCK_EX)
            with mock.patch.object(hue, 'HueClient', return_value=self.client):
                self.assertEqual(hue.theme_sync(room_id='zone'), (0, 0))
        self.client.put_light.assert_not_called()
        self.client.put_group.assert_not_called()
        # Once the worker lease ends, stale status cannot block normal sync.
        self.assertEqual(party.active_light_ids(), set())

    def test_color_restore_supports_ct_and_exact_xy_for_v2(self):
        light = {'has_ct': True, 'state': {'colormode': 'ct', 'ct': 310}}
        self.assertEqual(party.original_color(light), {'ct': 310, 'transitiontime': 5})
        body = party.original_color({'state': {'colormode': 'xy', 'xy': [0.31, 0.42]}})
        self.assertEqual(hue.HueClient._v2_payload(None, body),
                         {'color': {'xy': {'x': 0.31, 'y': 0.42}}, 'dynamics': {'duration': 500}})

    def test_invalid_xy_restore_values_are_classified_errors(self):
        for xy in ([], [0.1], [True, 0.2], [float('nan'), 0.2], [-1, 0.2], 'ab'):
            with self.subTest(xy=xy), self.assertRaises(hue.HueError):
                hue.HueClient._v2_payload(None, {'xy': xy})

    def test_v2_temperature_mode_is_retained_for_restoration(self):
        from test_hue_client import V2_RESOURCES, HueClientTests
        resources = copy.deepcopy(V2_RESOURCES)
        resources[0]['color_temperature']['mirek_valid'] = True
        client = hue.HueClient(HueClientTests().creds())
        light = client._normalize_v2(resources)['lights']['7']
        self.assertEqual(party.original_color(light), {'ct': 250, 'transitiontime': 5})

    def test_party_order_validation_keeps_existing_settings(self):
        for bad in ([], {'room': ['1', '1']}, {'room': [True]}, {'room': '123'}):
            hue.write_json_secure(hue.CONFIG_FILE, {'partyOrder': bad})
            with self.assertRaises(hue.HueError):
                hue.read_theme_config()
        hue.write_json_secure(hue.CONFIG_FILE, {'scene': True, 'partyOrder': {}})
        cli.main(['hue-api.py', 'write-party-order', 'room', '["2", "1"]'])
        self.assertEqual(hue.read_theme_config(), {'scene': True, 'partyOrder': {'room': ['2', '1']}})

    def test_scene_handoff_occurs_before_scene_write_without_extra_reads(self):
        self.client.get_state.reset_mock()
        with mock.patch.object(cli, 'HueClient', return_value=self.client), \
             mock.patch.object(party, 'request') as request:
            self.client.recall_scene.side_effect = lambda *_args: request.assert_called_once_with(
                'stop-overlap', 'control', optional=True)
            cli.main(['hue-api.py', 'recall-scene', 'scene-id', 'control'])
        self.client.get_state.assert_not_called()


class WorkerLifecycleTests(unittest.TestCase):
    def test_socket_commands_animation_and_stdin_cleanup_with_fixture_bridge(self):
        with tempfile.TemporaryDirectory(prefix='hue-party-') as directory:
            fixture = Path(directory)
            env = {**os.environ, 'XDG_STATE_HOME': directory, 'XDG_CONFIG_HOME': directory,
                   'XDG_RUNTIME_DIR': directory, 'PYTHONDONTWRITEBYTECODE': '1'}
            script = '''import hue_party as p
import hue_client as h
from unittest.mock import Mock
h.accent_color = lambda: 'ff0000'
h.build_scene_palette = lambda _: ['ff0000', '00ff00']
client = Mock()
client.get_state.return_value = {'lights': {'1': {'api_id':'1', 'has_color':True,
    'state':{'on':True, 'reachable':True, 'hue':42, 'sat':99}}},
    'groups': {'room': {'type':'Room', 'lights':['1']}}}
def put(*args):
    with (h.STATE_HOME / 'writes.jsonl').open('a') as f:
        f.write(h.json.dumps(args) + '\\n')
client.put_light.side_effect = put
h.HueClient = lambda: client
p.serve()
'''
            worker = subprocess.Popen([sys.executable, '-B', '-c', script], cwd=ROOT,
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                status = fixture / 'omarchy/hue-party/status.json'
                deadline = time.monotonic() + 5
                while not status.exists() and time.monotonic() < deadline:
                    if worker.poll() is not None:
                        self.fail(worker.communicate()[1].decode())
                    time.sleep(0.02)
                self.assertTrue(status.exists())
                def send(operation):
                    return subprocess.run([sys.executable, '-B', '-c',
                        'import hue_party as p; p.request("' + operation + '", "room")'],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=6)
                self.assertEqual(send('start').returncode, 0)
                time.sleep(1.7)
                writes = [json.loads(line) for line in (fixture / 'writes.jsonl').read_text().splitlines()]
                self.assertGreaterEqual(len(writes), 2)
                self.assertEqual(writes[0][1]['hue'], 0)
                self.assertEqual(writes[1][1]['hue'], hue._hex_to_hs('00ff00')[0])
                self.assertEqual(send('stop-overlap').returncode, 0)
                self.assertEqual(json.loads(status.read_text())['rooms'], {})
                self.assertEqual(send('start').returncode, 0)
                worker.stdin.close()
                worker.wait(timeout=6)
                self.assertEqual(worker.returncode, 0, worker.stderr.read().decode())
                self.assertEqual(json.loads(status.read_text())['rooms'], {})
                self.assertFalse(list(fixture.glob('*.sock')))
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
                for stream in (worker.stdin, worker.stdout, worker.stderr):
                    if stream and not stream.closed:
                        stream.close()
