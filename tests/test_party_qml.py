"""Load the plugin panel with a platform-window stub in an isolated QML session."""
import os
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path('/usr/share/omarchy/shell')


@unittest.skipUnless(shutil.which('quickshell') and (SHELL / 'Commons').is_dir(),
                     'Requires Quickshell and installed Omarchy UI modules')
class PartyQmlTests(unittest.TestCase):
    def test_full_panel_and_party_order_controls_load(self):
        with tempfile.TemporaryDirectory(prefix='hue-panel-') as directory:
            fixture = Path(directory)
            for module in ('Commons', 'Ui'):
                (fixture / module).symlink_to(SHELL / module)
            plugin = fixture / 'plugin'
            plugin.mkdir()
            for source in ROOT.iterdir():
                if source.suffix in ('.qml', '.js', '.png'):
                    shutil.copyfile(source, plugin / source.name)
            # Offscreen Qt cannot create a Wayland layer-shell window. Keep the
            # real panel and controls, replacing only that platform host.
            (plugin / 'PartyTestWindow.qml').write_text('''import QtQuick
Item {
  property Item anchorItem
  property QtObject bar
  property var owner
  property bool open: false
  property bool centerOnBar: false
  property Item focusTarget
  property int contentWidth: 380
  property int contentHeight: 600
  width: contentWidth
  height: contentHeight
  function fittedContentWidth(value) { return value }
  function fittedContentHeight(value) { return value }
}
''')
            panel_path = plugin / 'Panel.qml'
            panel_path.write_text(panel_path.read_text().replace('  KeyboardPanel {', '  PartyTestWindow {'))
            (fixture / 'runtime').mkdir(mode=0o700)
            (fixture / 'shell.qml').write_text('''import QtQuick
import Quickshell
import "plugin" as Hue
ShellRoot {
  function findObject(item, name) {
    if (item.objectName === name) return item
    if (!item.children) return null
    for (var i = 0; i < item.children.length; i++) {
      var found = findObject(item.children[i], name)
      if (found) return found
    }
    return null
  }
  function findButton(item, text) {
    if (item.text !== undefined && item.text === text && item.clicked !== undefined) return item
    if (!item.children) return null
    for (var i = 0; i < item.children.length; i++) {
      var found = findButton(item.children[i], text)
      if (found) return found
    }
    return null
  }
  Item { id: anchor }
  QtObject {
    id: bar
    property color foreground: "#eeeeee"
    property color background: "#18202a"
    property color barForeground: foreground
    property string fontFamily: "DejaVu Sans"
    property string position: "top"
  }
  Hue.Panel { id: panel; bar: bar; anchorItem: anchor; manageIpc: false }
  Timer {
    interval: 100; running: true
    onTriggered: {
      panel.lightsById = {
        "1": {id:"1", apiId:"1", name:"Desk", on:true, hasColor:true, reachable:true, hasBri:true, bri:127,
              hasCt:true, ct:250, ctMin:153, ctMax:500, hue:0, sat:254, xy:[], colormode:"hs"},
        "2": {id:"2", apiId:"2", name:"Shelf", on:true, hasColor:true, reachable:true, hasBri:true, bri:127,
              hasCt:true, ct:250, ctMin:153, ctMax:500, hue:0, sat:254, xy:[], colormode:"hs"}}
      panel.rooms = [{id:"room", name:"Office", lightIds:["1","2"]}]
      panel.assembleRooms()
      panel.expandedRooms = ({room:true})
      panel.musicRooms = ({room:true})
      panel.musicBeats = ({room:4})
      panel.musicTracking = ({room:{locked:true, bpm:120, confidence:1.2}})
    }
  }
  Timer {
    interval: 300; running: true
    onTriggered: {
      var arrange = findButton(panel, "Arrange party light order")
      if (!arrange || !arrange.focusable) { Qt.exit(2); return }
      arrange.clicked()
      var intensity = findObject(panel, "musicOption-idle")
      var sensitivity = findObject(panel, "musicOption-sensitivity")
      var pace = findObject(panel, "musicOption-pace")
      var count = findObject(panel, "musicBeatCount-room")
      if (!count || count.text !== "Tracked beats: 4") { Qt.exit(7); return }
      var tracking = findObject(panel, "musicTracking-room")
      if (!tracking || tracking.text.indexOf("120 BPM") < 0) { Qt.exit(9); return }
      var timing = findObject(panel, "musicOption-timing")
      if (!timing) { Qt.exit(10); return }
      if (!intensity || !sensitivity || !pace || intensity.width < 80 || sensitivity.width < 80 || pace.width < 80) { Qt.exit(5); return }
      // Actual QML slider signals must update while dragging, before release.
      intensity.moved(0.15)
      var peak = findObject(panel, "musicOption-peak")
      if (!peak) { Qt.exit(11); return }
      peak.moved(0.8)
      sensitivity.moved(0.25)
      pace.moved(1.5)
      timing.moved(-0.12)
      if (panel.musicOption("room", "idle") !== 0.15 || panel.musicOption("room", "sensitivity") !== 0.25) { Qt.exit(6); return }
    }
  }
  Timer {
    interval: 450; running: true
    onTriggered: {
      var depth = findObject(panel, "musicOptionValue-idle")
      if (!depth || depth.text !== "15%") { Qt.exit(8); return }
      var later = findButton(panel, "↓")
      if (!later || !later.focusable) { Qt.exit(3); return }
      later.clicked()
      if (panel.partyOrder.room.join(",") !== "2,1") { Qt.exit(4); return }
    }
  }
  Timer {
    property int phase: -6
    interval: 100; repeat: true; running: true
    onTriggered: {
      if (phase < 0) { phase++; return }
      var loader = findObject(panel, "sceneBrowser-room")
      if (!loader) { Qt.exit(12); return }
      if (phase === 0) {
        if (loader.item) { Qt.exit(13); return }
        panel.expandedSceneMenus = ({room:true})
        panel.sceneQueries = ({room:"Golden"})
      } else if (phase === 1) {
        // A remembered open menu must remain unloaded while the panel is closed.
        if (loader.item) { Qt.exit(14); return }
        panel.open()
      } else if (phase === 2) {
        if (!loader.item) { Qt.exit(15); return }
        panel.close()
      } else if (phase === 3) {
        if (loader.item) { Qt.exit(16); return }
        panel.open()
      } else if (phase === 4) {
        if (!loader.item || panel.sceneQueries.room !== "Golden") { Qt.exit(17); return }
        panel.expandedRooms = ({})
      } else if (phase === 5) {
        if (loader.item) { Qt.exit(18); return }
        panel.expandedRooms = ({room:true})
      } else if (phase === 6) {
        if (!loader.item) { Qt.exit(19); return }
        panel.expandedSceneMenus = ({})
      } else {
        if (loader.item) { Qt.exit(20); return }
        Qt.quit()
      }
      phase++
    }
  }
}
''')
            (plugin / 'hue-api.py').write_text(
                'import json, sys\nfrom pathlib import Path\n'
                'with Path(__file__).with_name("calls.jsonl").open("a") as output:\n'
                '    output.write(json.dumps(sys.argv[1:]) + "\\n")\n')
            env = {**os.environ, 'HOME': directory, 'XDG_CONFIG_HOME': str(fixture / 'config'),
                   'XDG_STATE_HOME': str(fixture / 'state'), 'XDG_RUNTIME_DIR': str(fixture / 'runtime'),
                   'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software', 'GSETTINGS_BACKEND': 'memory'}
            env.pop('WAYLAND_DISPLAY', None)
            env.pop('HYPRLAND_INSTANCE_SIGNATURE', None)
            result = subprocess.run(['quickshell', '-p', str(fixture / 'shell.qml')], env=env,
                capture_output=True, text=True, timeout=10)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            calls = [json.loads(line) for line in (plugin / 'calls.jsonl').read_text().splitlines()]
            options = [json.loads(call[2]) for call in calls if call[0] == 'write-music-options']
            self.assertEqual(options, [{'idle': 0.15, 'peak': 0.8, 'sensitivity': 0.25, 'pace': 1.5, 'timing': -0.12}])
            for failure in ('TypeError', 'ReferenceError', 'Unable to assign', 'Binding loop',
                            'Failed to load configuration', 'Cannot assign to non-existent property'):
                self.assertNotIn(failure, output)
