"""Theme writes stay deterministic, isolated, and tolerant of bulb failures."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hue_client


class ThemeSyncTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            'lights': {key: {'api_id': key, 'has_color': True} for key in ('1', '2', '3', '4')},
            'groups': {
                'zone': {'type': 'Zone', 'lights': ['1', '2', '3'], 'control_id': 'zone'},
                'room': {'type': 'Room', 'lights': ['1', '2'], 'control_id': 'room'},
                'other': {'type': 'Room', 'lights': ['4'], 'control_id': 'other'},
            },
        }
        self.client = mock.Mock(api_version='v1')
        self.client.get_state.return_value = self.state
        for name, value in [('HueClient', self.client), ('read_theme_config', {}),
                            ('accent_color', 'ff0000'), ('build_scene_palette', ['ff0000', '00ff00'])]:
            patcher = mock.patch.object(hue_client, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(hue_client, 'secure_log')
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_overlap_uses_room_first_and_only_writes_remaining_zone_lights(self):
        self.assertEqual(hue_client.theme_sync(), (3, 3))
        self.assertEqual([c.args[0] for c in self.client.put_group.call_args_list], ['other', 'room'])
        self.assertEqual([c.args[0] for c in self.client.put_light.call_args_list], ['3'])

    def test_failure_continues_other_rooms_and_returns_partial_failure(self):
        self.client.put_group.side_effect = [hue_client.HueError('offline'), None]
        with self.assertRaisesRegex(hue_client.HueError, '2/3 group'):
            hue_client.theme_sync()
        self.assertEqual(self.client.put_group.call_count, 2)
        self.assertEqual(self.client.put_light.call_count, 1)

    def test_scene_failure_does_not_skip_remaining_bulbs(self):
        self.read_theme_config.return_value = {'scene': True}
        self.client.put_light.side_effect = [hue_client.HueError('offline'), None, None]
        with self.assertRaisesRegex(hue_client.HueError, '2/3 group'):
            hue_client.theme_sync()
        self.assertEqual([c.args[0] for c in self.client.put_light.call_args_list], ['1', '2', '3'])

    def test_disabled_room_cannot_be_overwritten_by_zone(self):
        self.read_theme_config.return_value = {'themeSync': {'room': False}}
        self.assertEqual(hue_client.theme_sync(), (2, 2))
        self.assertEqual([c.args[0] for c in self.client.put_group.call_args_list], ['other'])
        self.assertEqual([c.args[0] for c in self.client.put_light.call_args_list], ['3'])

    def test_explicit_room_sync_is_scoped_and_zero_transition_is_preserved(self):
        self.read_theme_config.return_value = {'transition': 0}
        self.assertEqual(hue_client.theme_sync(room_id='zone'), (1, 1))
        self.assertEqual(self.client.put_group.call_args.args[0], 'zone')
        self.assertEqual(self.client.put_group.call_args.args[1]['transitiontime'], 0)

    def test_bridge_order_does_not_change_palette_assignment(self):
        self.read_theme_config.return_value = {'scene': True}
        hue_client.theme_sync()
        first = [copy.deepcopy(call.args) for call in self.client.put_light.call_args_list]
        self.client.reset_mock()
        self.state['groups'] = dict(reversed(list(self.state['groups'].items())))
        hue_client.theme_sync()
        self.assertEqual([call.args for call in self.client.put_light.call_args_list], first)


class ConfigurationTests(unittest.TestCase):
    def test_invalid_configuration_is_rejected_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with mock.patch.object(hue_client, 'CONFIG_FILE', path):
                self.assertEqual(hue_client.read_theme_config(), {})
                for value in ('{', '[]', '{"themeSync": []}', '{"scene": "false"}', '{"transition": -1}'):
                    path.write_text(value)
                    with self.assertRaises(hue_client.HueError):
                        hue_client.read_theme_config()
                    self.assertEqual(path.read_text(), value)

    def test_python_paths_honor_custom_and_empty_xdg_variables(self):
        for config, state in (('/tmp/hue-config-fixture', '/tmp/hue-state-fixture'), ('', '')):
            result = subprocess.run([sys.executable, '-B', '-c',
                'import hue_client as h, json; print(json.dumps([str(h.CREDS_FILE), str(h.CONFIG_FILE), str(h.COLORS_FILE)]))'],
                cwd=ROOT, env={**os.environ, 'XDG_CONFIG_HOME': config, 'XDG_STATE_HOME': state},
                capture_output=True, text=True, check=True)
            paths = json.loads(result.stdout)
            self.assertEqual(paths, [str(Path(state or Path.home() / '.local/state') / 'omarchy/settings/hue.json'),
                                     str(Path(config or Path.home() / '.config') / 'omarchy/settings/hue-theme.json'),
                                     str(Path(state or Path.home() / '.local/state') / 'omarchy/current/theme/colors.toml')])

    def test_installer_uses_custom_xdg_path_with_quotes_and_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            binary = fixture / 'bin'
            binary.mkdir()
            omarchy = binary / 'omarchy'
            omarchy.write_text('#!/bin/sh\nexit 0\n')
            omarchy.chmod(0o755)
            config_home = fixture / "config ' quoted"
            env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'], 'XDG_CONFIG_HOME': str(config_home)}
            script = ROOT / 'theme-sync/enable-theme-sync.sh'
            subprocess.run(['bash', str(script)], env=env, check=True, capture_output=True)
            config = config_home / 'omarchy/settings/hue-theme.json'
            self.assertEqual(json.loads(config.read_text())['enabled'], True)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            config.write_text('{"enabled": false}')
            subprocess.run(['bash', str(script)], env=env, check=True, capture_output=True)
            self.assertEqual(json.loads(config.read_text()), {'enabled': False})
