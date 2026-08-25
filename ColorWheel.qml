import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

Item {
  id: root
  visible: lightOn && hasColor && pickerOpen && !themeSynced
  width: Style.space(180)
  height: Style.space(180)
  anchors.horizontalCenter: parent.horizontalCenter

  property bool lightOn: true
  property bool hasColor: false
  property bool pickerOpen: false
  property bool themeSynced: false
  property real initialHue: 0
  property real initialSat: 0
  signal colorSelected(real hue, real sat)

  property real dragHue: initialHue
  property real dragSat: initialSat
  property bool picking: false

  Image {
    anchors.fill: parent
    source: Qt.resolvedUrl("hsv_wheel.png")
    fillMode: Image.Stretch
    smooth: true
  }

  Rectangle {
    width: 12
    height: 12
    radius: 6
    border.color: "#ffffff"
    border.width: 2
    color: "transparent"
    x: root.width / 2
       + Math.cos(-Math.PI / 2 + (root.dragHue / 65535) * 2 * Math.PI)
         * (root.dragSat / 254) * (root.width / 2) - width / 2
    y: root.height / 2
       + Math.sin(-Math.PI / 2 + (root.dragHue / 65535) * 2 * Math.PI)
         * (root.dragSat / 254) * (root.height / 2) - height / 2
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor

    function apply(x, y) {
      var c = root.width / 2
      var dx = x - c
      var dy = y - c
      var dist = Math.sqrt(dx * dx + dy * dy)
      if (dist > c) return
      var hue01 = (((Math.atan2(dy, dx) + Math.PI / 2) / (2 * Math.PI)) % 1 + 1) % 1
      root.dragHue = Math.round(hue01 * 65535)
      root.dragSat = Math.round((dist / c) * 254)
    }

    onPressed: function(mouse) {
      root.picking = true
      apply(mouse.x, mouse.y)
    }
    onPositionChanged: function(mouse) {
      if (root.picking) apply(mouse.x, mouse.y)
    }
    onReleased: function(mouse) {
      root.picking = false
      root.colorSelected(root.dragHue, root.dragSat)
    }
  }
}
