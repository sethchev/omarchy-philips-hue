import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Column {
  id: root
  required property var room
  required property var controller
  required property var bar
  readonly property int choiceCount: controller.roomSceneChoices(room).length
  readonly property int pageIndex: controller.roomScenePageIndex(room)
  spacing: Style.space(2)

  BorderSurface {
    visible: root.controller.roomSceneCount(root.room) > root.controller.scenePageSize
      || (root.controller.sceneQueries[root.room.id] || "") !== ""
    width: parent.width
    height: Style.space(34)
    radius: Style.cornerRadius
    color: "transparent"
    borderSpec: Border.controlSpec(search.activeFocus ? "focus" : "normal", root.bar.foreground, Commons.Color.accent)

    TextInput {
      id: search
      objectName: "sceneSearch"
      anchors.fill: parent
      anchors.margins: Style.space(8)
      text: root.controller.sceneQueries[root.room.id] || ""
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
      verticalAlignment: TextInput.AlignVCenter
      clip: true
      selectByMouse: true
      activeFocusOnTab: true
      Accessible.name: "Search scenes for " + root.room.name
      onTextEdited: root.controller.setSceneQuery(root.room.id, text)

      Text {
        visible: search.text === ""
        anchors.verticalCenter: parent.verticalCenter
        text: "Search scenes…"
        color: Qt.darker(root.bar.foreground, 1.4)
        font: search.font
      }
    }
  }

  Grid {
    id: grid
    width: parent.width
    columns: 2
    spacing: Style.space(2)

    Repeater {
      model: root.controller.roomScenePage(root.room)

      BorderSurface {
        id: tile
        required property var modelData
        width: (grid.width - grid.spacing) / 2
        height: Math.max(46, Style.font.body + Style.spacing.huge)
        radius: Style.cornerRadius
        readonly property bool hot: mouse.containsMouse || activeFocus
        color: modelData.active
          ? Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, hot ? 0.34 : 0.24)
          : Style.controlFill(activeFocus, hot, Qt.darker(root.bar.foreground, 1.2), Commons.Color.accent)
        borderSpec: Border.controlSpec(modelData.active || activeFocus ? "focus" : (hot ? "hover-cursor" : "normal"), root.bar.foreground, Commons.Color.accent)
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: (modelData.active ? "Active scene " : "Apply scene ") + modelData.name + " for " + root.room.name
        Keys.onReturnPressed: root.controller.applySceneChoice(root.room.id, modelData)
        Keys.onEnterPressed: root.controller.applySceneChoice(root.room.id, modelData)
        Keys.onSpacePressed: root.controller.applySceneChoice(root.room.id, modelData)

        MouseArea {
          id: mouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.controller.applySceneChoice(root.room.id, tile.modelData)
        }

        Text {
          anchors.centerIn: parent
          width: parent.width - Style.space(12)
          text: (tile.modelData.active ? "✓ " : "") + tile.modelData.name
          textFormat: Text.PlainText
          horizontalAlignment: Text.AlignHCenter
          maximumLineCount: 1
          elide: Text.ElideRight
          color: tile.modelData.active ? Commons.Color.accent : root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        PanelToolTip {
          visible: mouse.containsMouse || tile.activeFocus
          text: tile.modelData.name + (tile.modelData.preset ? " · Built-in preset" : " · Bridge scene")
        }
      }
    }
  }

  Text {
    visible: root.choiceCount === 0
    width: parent.width
    text: "No matching scenes"
    color: Qt.darker(root.bar.foreground, 1.4)
    font.family: root.bar.fontFamily
    font.pixelSize: Style.font.caption
  }

  Row {
    visible: root.choiceCount > root.controller.scenePageSize
    width: parent.width
    spacing: Style.space(4)

    PanelActionButton {
      id: previous
      size: Style.space(28)
      iconText: "‹"
      tooltipText: "Previous scenes"
      foreground: root.bar.foreground
      focusable: true
      enabled: root.pageIndex > 0
      Accessible.role: Accessible.Button
      Accessible.name: "Previous scenes for " + root.room.name
      onClicked: root.controller.changeScenePage(root.room, -1)
    }

    Text {
      width: parent.width - previous.width - next.width - parent.spacing * 2
      height: previous.height
      verticalAlignment: Text.AlignVCenter
      horizontalAlignment: Text.AlignHCenter
      text: (root.pageIndex * root.controller.scenePageSize + 1) + "–"
        + Math.min((root.pageIndex + 1) * root.controller.scenePageSize, root.choiceCount) + " / " + root.choiceCount
      color: Qt.darker(root.bar.foreground, 1.4)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
    }

    PanelActionButton {
      id: next
      size: Style.space(28)
      iconText: "›"
      tooltipText: "Next scenes"
      foreground: root.bar.foreground
      focusable: true
      enabled: (root.pageIndex + 1) * root.controller.scenePageSize < root.choiceCount
      Accessible.role: Accessible.Button
      Accessible.name: "Next scenes for " + root.room.name
      onClicked: root.controller.changeScenePage(root.room, 1)
    }
  }
}
