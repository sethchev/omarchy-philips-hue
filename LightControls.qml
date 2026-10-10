import QtQuick
import qs.Commons
import qs.Ui
import "HueApi.js" as HueApi

Column {
  id: root
  required property var light
  required property var controller
  required property var bar
  property bool themeSynced: false
  property bool pickerOpen: false
  spacing: Style.space(1)

  InlineToggle {
    width: parent.width
    label: root.light.name + (root.light.reachable === false ? " · unreachable" : "")
    Accessible.name: root.light.name + " light toggle, " + (root.light.on ? "on" : "off") + (root.light.reachable === false ? ", unreachable" : "")
    titleSize: Style.font.body
    rowColor: HueApi.lightColor(root.light)
    checked: root.light.on
    foreground: Qt.darker(root.bar.foreground, 1.2)
    accent: Color.accent
    fontFamily: root.bar.fontFamily
    onClicked: root.controller.toggleLight(root.light.id, !root.light.on)
  }

  Row {
    visible: root.light.on && (root.light.hasBri || (root.light.hasColor && !root.themeSynced))
    width: parent.width - Style.space(24)
    anchors.horizontalCenter: parent.horizontalCenter
    spacing: Style.space(8)

    PanelSlider {
      visible: root.light.hasBri
      width: parent.width - (pickerButton.visible ? pickerButton.width + parent.spacing : 0)
      bar: root.bar
      minimum: 1
      maximum: 254
      integer: true
      step: 10
      value: root.light.bri
      onReleased: function(v) { root.controller.setBrightness(root.light.id, v) }
    }

    Item {
      id: pickerButton
      visible: root.light.hasColor && !root.themeSynced
      width: Style.space(22)
      height: Style.space(22)
      activeFocusOnTab: true
      Accessible.role: Accessible.Button
      Accessible.name: "Toggle color picker for " + root.light.name
      Keys.onReturnPressed: root.controller.toggleColorPicker(root.light.id)
      Keys.onEnterPressed: root.controller.toggleColorPicker(root.light.id)
      Keys.onSpacePressed: root.controller.toggleColorPicker(root.light.id)

      Image {
        anchors.fill: parent
        source: Qt.resolvedUrl("hsv_wheel.png")
        fillMode: Image.Stretch
        smooth: true
      }
      Rectangle {
        anchors.fill: parent
        radius: Style.space(11)
        border.width: root.pickerOpen || pickerButton.activeFocus ? 2 : 1
        border.color: root.pickerOpen || pickerButton.activeFocus ? Color.accent : Qt.darker(root.bar.foreground, 1.6)
        color: "transparent"
      }
      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.controller.toggleColorPicker(root.light.id)
      }
    }
  }

  ColorWheel {
    lightOn: root.light.on
    hasColor: root.light.hasColor
    pickerOpen: root.pickerOpen
    themeSynced: root.themeSynced
    initialHue: root.light.hue
    initialSat: root.light.sat
    onColorSelected: function(hue, sat) { root.controller.setLightColor(root.light.id, hue, sat) }
  }

  PanelSlider {
    visible: root.light.on && root.light.hasCt && !root.themeSynced
    width: parent.width - Style.space(24)
    anchors.horizontalCenter: parent.horizontalCenter
    bar: root.bar
    minimum: root.light.ctMin
    maximum: root.light.ctMax
    integer: true
    step: 10
    value: root.light.ct
    onReleased: function(v) { root.controller.setColorTemperature(root.light.id, v) }
  }
}
