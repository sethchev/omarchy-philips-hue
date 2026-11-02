"""Offscreen smoke test against installed Omarchy UI, with fixture-only state."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path('/usr/share/omarchy/shell')


@unittest.skipUnless(shutil.which('quickshell') and (SHELL / 'Commons').is_dir(),
                     'Requires Quickshell and installed Omarchy UI modules')
class SceneBrowserQmlTests(unittest.TestCase):
    def test_compact_pages_search_and_empty_state_have_no_runtime_errors(self):
        with tempfile.TemporaryDirectory(prefix='hue-qml-') as directory:
            fixture = Path(directory)
            for module in ('Commons', 'Ui'):
                (fixture / module).symlink_to(SHELL / module)
            for name in ('SceneBrowser.qml', 'HueApi.js'):
                shutil.copyfile(ROOT / name, fixture / name)
            (fixture / 'runtime').mkdir(mode=0o700)
            functions = '\n'.join(match[0] for match in re.finditer(
                r'^  function (\w+)\([\s\S]*?^  }', (ROOT / 'Panel.qml').read_text(), re.M)
                if match[1] in {'roomSceneChoices', 'roomSceneCount', 'roomScenePageIndex',
                                'roomScenePage', 'setSceneQuery', 'changeScenePage', 'lightClone'})
            (fixture / 'shell.qml').write_text('''import QtQuick
import QtQuick.Window
import Quickshell
import qs.Commons as Commons
import "HueApi.js" as HueApi
Window {
  id: window
  width: 380; height: 320; visible: true
  Rectangle { anchors.fill: parent; color: "#18202a" }
  QtObject {
    id: root
    property var sceneQueries: ({})
    property var scenePages: ({})
    property int scenePageSize: 6
    property var prebuiltScenes: ["Relax", "Read", "Natural Light", "Tv Mode"]
    property var scenes: [
      {id:"one", name:"Evening after the last meeting", active:true, groupApiId:"office"},
      {id:"two", name:"Long scene name that should be clipped", active:false, groupApiId:"office"},
      {id:"three", name:"Concentrate", active:false, groupApiId:"office"},
      {id:"four", name:"Blue lagoon", active:false, groupApiId:"office"},
      {id:"five", name:"Golden hour", active:false, groupApiId:"office"}]
    function applySceneChoice(roomId, choice) {}
''' + functions + '''
  }
  QtObject { id: bar; property color foreground: "#eeeeee"; property string fontFamily: "DejaVu Sans" }
  SceneBrowser { id: browser; x:12; y:12; width:356; room: ({id:"4", apiId:"office", name:"Office"}); controller:root; bar:bar }
  Timer {
    property int phase: 0
    interval: 100; repeat: true; running: true
    onTriggered: {
      if (browser.height > 250 || browser.width > window.width) { Qt.exit(2); return }
      if (phase === 0) {
        if (browser.choiceCount !== 9) { Qt.exit(3); return }
        root.changeScenePage(browser.room, 1)
      } else if (phase === 1) {
        if (browser.pageIndex !== 1) { Qt.exit(4); return }
        root.setSceneQuery("4", "no matches")
      } else if (phase === 2) {
        if (browser.choiceCount !== 0 || browser.pageIndex !== 0) { Qt.exit(5); return }
        root.setSceneQuery("4", "Golden")
      } else if (phase === 3) {
        if (browser.choiceCount !== 1) { Qt.exit(6); return }
        root.setSceneQuery("4", "")
        root.scenes = []
      } else {
        if (browser.choiceCount !== 4 || browser.pageIndex !== 0) { Qt.exit(7); return }
        Qt.quit()
      }
      phase++
    }
  }
}
''')
            env = {**os.environ, 'HOME': str(fixture), 'XDG_CONFIG_HOME': str(fixture / 'config'),
                   'XDG_STATE_HOME': str(fixture / 'state'), 'XDG_RUNTIME_DIR': str(fixture / 'runtime'),
                   'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software', 'GSETTINGS_BACKEND': 'memory'}
            env.pop('WAYLAND_DISPLAY', None)
            env.pop('HYPRLAND_INSTANCE_SIGNATURE', None)
            result = subprocess.run(['quickshell', '-p', str(fixture / 'shell.qml')], env=env,
                                    capture_output=True, text=True, timeout=10)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            for failure in ('TypeError', 'ReferenceError', 'Unable to assign', 'Binding loop', 'Failed to load configuration'):
                self.assertNotIn(failure, output)
