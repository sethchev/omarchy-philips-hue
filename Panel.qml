import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "HueApi.js" as HueApi

Panel {
  id: root
  moduleName: "omarchy-philips-hue"
  ipcTarget: "omarchy-philips-hue"

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  property var config: null
  property string currentThemeName: ""
  property var lightsById: ({})
  property var rooms: []
  property var roomsWithLights: []
  property var orphanLights: []
  property int pendingFetches: 0
  property bool fetchStarting: false
  property bool refreshQueued: false
  property bool loading: false
  property bool lastFetchFailed: false
  property int fetchErrorCode: 0
  property string actionErrorText: ""
  property var actionQueue: []
  property var expandedRooms: ({})
  property var rawDevicesText: ""
  property var rawGroupedLightsText: ""
  property var rawLightsText: ""
  property var rawRoomsText: ""
  property var rawZonesText: ""
  property int pollGeneration: 0
  property int fetchSuccessCount: 0

  readonly property int roomCount: root.roomsWithLights.length
  readonly property int lightTotal: root.lightsTotal()
  readonly property int lightedRoomCount: root.lightedRooms().length
  readonly property int emptyRoomCount: root.emptyRooms().length
  readonly property bool allLightsOn: root.computeAllLightsOn()
  readonly property bool insecureMode: root.config !== null && !root.config.bridgeId
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state"
  readonly property string fetchErrorText: {
    if (!root.lastFetchFailed) return ""
    if (root.fetchErrorCode === 2) return "Bridge authorization failed. Re-run pair.sh."
    if (root.fetchErrorCode === 3) return "Bridge TLS verification failed. Re-run pair.sh."
    if (root.fetchErrorCode === 4) return "Bridge DNS resolution failed. Check the bridge configuration."
    if (root.fetchErrorCode === 5) return "Bridge request timed out. Check the bridge is on and reachable."
    if (root.fetchErrorCode === 6) return "Bridge API request failed. Try again later."
    return "Couldn't reach the bridge. Check the bridge is on and the IP is still valid, then re-run pair.sh."
  }

  readonly property string statusText: {
    if (root.config === null) return "Not paired"
    if (root.actionErrorText) return root.actionErrorText
    if (root.lastFetchFailed) return root.fetchErrorText
    if (root.loading) return "Loading…"
    var roomLabel = root.lightedRoomCount + " room" + (root.lightedRoomCount === 1 ? "" : "s")
    return roomLabel + " · " + root.lightTotal + " light" + (root.lightTotal === 1 ? "" : "s")
  }

  function computeAllLightsOn() {
    if (root.lightedRoomCount === 0) return false
    var rooms = root.lightedRooms()
    for (var i = 0; i < rooms.length; i++) {
      if (!rooms[i].on) return false
    }
    return true
  }

  function lightedRooms() {
    var result = []
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      if (root.roomsWithLights[i].lightCount > 0) result.push(root.roomsWithLights[i])
    }
    return result
  }

  function emptyRooms() {
    var result = []
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      if (root.roomsWithLights[i].lightCount === 0) result.push(root.roomsWithLights[i])
    }
    return result
  }

  function lightsTotal() {
    var total = 0
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      total += root.roomsWithLights[i].lightCount
    }
    return total + root.orphanLights.length
  }

  function open() {
    root.controller.show()
    root.refresh()
  }

  function openFromHotkey() {
    root.controller.show()
    root.refresh()
  }

  function close() {
    root.controller.hide()
  }

  function refresh() {
    if (!root.config) {
      configFile.reload()
      return
    }
    if (root.pendingFetches > 0 || root.fetchStarting) {
      root.refreshQueued = true
      return
    }
    root.lastFetchFailed = false
    root.fetchErrorCode = 0
    if (root.roomsWithLights.length === 0 && root.orphanLights.length === 0) root.loading = true
    root.fetchStarting = true
    Qt.callLater(startFetches)
  }

  function startFetches() {
    if (!root.config || root.pendingFetches > 0) {
      root.fetchStarting = false
      return
    }
    root.fetchStarting = false
    var gen = ++root.pollGeneration
    root.pendingFetches = 5
    root.fetchSuccessCount = 0
    lightsProc.generation = gen
    roomsProc.generation = gen
    zonesProc.generation = gen
    devicesProc.generation = gen
    groupedLightsProc.generation = gen
    lightsProc.command = HueApi.apiCmd(["get-lights-v2"])
    roomsProc.command = HueApi.apiCmd(["get-rooms-v2"])
    zonesProc.command = HueApi.apiCmd(["get-zones-v2"])
    devicesProc.command = HueApi.apiCmd(["get-devices-v2"])
    groupedLightsProc.command = HueApi.apiCmd(["get-grouped-lights-v2"])
    lightsProc.running = true
    roomsProc.running = true
    zonesProc.running = true
    devicesProc.running = true
    groupedLightsProc.running = true
  }

  function finishFetch(generation, success, exitCode) {
    if (generation !== root.pollGeneration) return
    root.pendingFetches--
    if (success) root.fetchSuccessCount++
    else if (root.fetchErrorCode === 0 || exitCode === 2) root.fetchErrorCode = exitCode || 1
    if (root.pendingFetches <= 0) {
      root.loading = false
      root.lastFetchFailed = root.fetchSuccessCount !== 5
      if (!root.lastFetchFailed) {
        var lights = HueApi.parseLightsV2(root.rawLightsText)
        var byId = {}
        for (var i = 0; i < lights.length; i++) byId[lights[i].id] = lights[i]
        root.lightsById = byId
        root.rooms = HueApi.parseGroupsV2(root.rawRoomsText, root.rawZonesText,
          root.rawDevicesText, root.rawGroupedLightsText)
        root.assembleRooms()
      }
      if (root.refreshQueued) {
        root.refreshQueued = false
        Qt.callLater(root.refresh)
      }
    }
  }

  function assembleRooms() {
    var used = {}
    var result = []
    for (var i = 0; i < root.rooms.length; i++) {
      var room = root.rooms[i]
      var lights = []
      for (var j = 0; j < room.lightIds.length; j++) {
        var light = root.lightsById[room.lightIds[j]]
        if (light) {
          lights.push(light)
          used[light.id] = true
        }
      }
      result.push({
        id: room.id,
        name: room.name,
        on: room.on,
        lightCount: lights.length,
        lights: lights,
        groupedLightId: room.groupedLightId || ""
      })
    }
    var orphans = []
    for (var id in root.lightsById) {
      if (!used[id]) orphans.push(root.lightsById[id])
    }
    root.roomsWithLights = result
    root.orphanLights = orphans
  }

  function lightClone(light, changes) {
    return {
      id: light.id,
      name: light.name,
      on: changes.on !== undefined ? changes.on : light.on,
      bri: changes.bri !== undefined ? changes.bri : light.bri,
      hasBri: light.hasBri,
      ct: changes.ct !== undefined ? changes.ct : light.ct,
      hasCt: light.hasCt,
      hue: changes.hue !== undefined ? changes.hue : light.hue,
      sat: changes.sat !== undefined ? changes.sat : light.sat,
      hasColor: light.hasColor,
      colormode: light.colormode,
      xy: light.xy ? light.xy.slice() : [],
      pickerOpen: changes.pickerOpen !== undefined ? changes.pickerOpen : light.pickerOpen
    }
  }

  function lightCopy(light, on) {
    return root.lightClone(light, { on: on })
  }

  function setRoomOn(roomId, on) {
    var affected = {}
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      if (root.roomsWithLights[i].id !== roomId) continue
      for (var j = 0; j < root.roomsWithLights[i].lights.length; j++) {
        affected[root.roomsWithLights[i].lights[j].id] = true
      }
      break
    }
    var newRooms = []
    for (var k = 0; k < root.roomsWithLights.length; k++) {
      var room = root.roomsWithLights[k]
      var updatedLights = room.lights.map(function(light) {
        return affected[light.id] ? root.lightCopy(light, on) : light
      })
      var roomOn = room.id === roomId ? on : room.on
      if (updatedLights.length > 0) {
        roomOn = false
        for (var l = 0; l < updatedLights.length; l++) {
          if (updatedLights[l].on) {
            roomOn = true
            break
          }
        }
      }
      newRooms.push({
        id: room.id,
        name: room.name,
        on: roomOn,
        lightCount: room.lightCount,
        lights: updatedLights,
        groupedLightId: room.groupedLightId || ""
      })
    }
    root.roomsWithLights = newRooms
  }

  function setLightOn(lightId, on) {
    var newRooms = []
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      var room = root.roomsWithLights[i]
      newRooms.push({
        id: room.id,
        name: room.name,
        on: room.on,
        lightCount: room.lightCount,
        lights: room.lights.map(function(light) {
          return light.id === lightId ? root.lightCopy(light, on) : light
        }),
        groupedLightId: room.groupedLightId || ""
      })
    }
    root.roomsWithLights = newRooms
    root.orphanLights = root.orphanLights.map(function(light) {
      return light.id === lightId ? root.lightCopy(light, on) : light
    })
  }

  function patchLights(lightId, changes) {
    var newRooms = []
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      var room = root.roomsWithLights[i]
      newRooms.push({
        id: room.id,
        name: room.name,
        on: room.on,
        lightCount: room.lightCount,
        lights: room.lights.map(function(light) {
          return light.id === lightId ? root.lightClone(light, changes) : light
        }),
        groupedLightId: room.groupedLightId || ""
      })
    }
    root.roomsWithLights = newRooms
    root.orphanLights = root.orphanLights.map(function(light) {
      return light.id === lightId ? root.lightClone(light, changes) : light
    })
  }

  function setLightBri(lightId, bri) {
    root.patchLights(lightId, { bri: bri })
  }

  function setLightCt(lightId, ct) {
    root.patchLights(lightId, { ct: ct })
  }

  function patchLightColor(lightId, hue, sat) {
    root.patchLights(lightId, { hue: hue, sat: sat })
  }

  function lightById(lightId) {
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      var room = root.roomsWithLights[i]
      for (var j = 0; j < room.lights.length; j++) {
        if (room.lights[j].id === lightId) return room.lights[j]
      }
    }
    for (var k = 0; k < root.orphanLights.length; k++) {
      if (root.orphanLights[k].id === lightId) return root.orphanLights[k]
    }
    return null
  }

  function roomSyncOn(roomId) {
    return root.themeSync[roomId] !== false
  }

  // Effective scene-mode state for a room: per-room override, else the global
  // "scene" default from hue-theme.json.
  function roomSceneDefault(roomId) {
    return root.sceneRooms[roomId] !== undefined ? root.sceneRooms[roomId] : root.sceneDefault
  }

  // Scene Mode is only offered when the room is synced and has at least two
  // color-capable lights (the hook's own threshold for building a scene).
  function roomSceneApplies(room) {
    return root.roomSyncOn(room.id) && HueApi.roomColorLightCount(room) >= 2
  }

  function roomExpanded(roomId) {
    return root.expandedRooms[roomId] === true
  }

  function toggleRoomExpanded(roomId) {
    var map = JSON.parse(JSON.stringify(root.expandedRooms))
    map[roomId] = map[roomId] !== true
    root.expandedRooms = map
  }

  function toggleColorPicker(lightId) {
    var light = root.lightById(lightId)
    if (light) root.patchLights(lightId, { pickerOpen: !light.pickerOpen })
  }

  function toggleRoom(roomId, on) {
    if (!root.config) return
    root.setRoomOn(roomId, on)
    var room = root.findRoom(roomId)
    if (room && room.groupedLightId) {
      root.runAction(HueApi.apiCmd(["put-grouped-light-v2", room.groupedLightId, JSON.stringify({ on: on })]))
    }
    root.scheduleRefresh()
  }

  function findRoom(roomId) {
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      if (root.roomsWithLights[i].id === roomId) return root.roomsWithLights[i]
    }
    return null
  }

  function toggleLight(lightId, on) {
    if (!root.config) return
    root.setLightOn(lightId, on)
    root.runAction(HueApi.apiCmd(["put-light-v2", lightId, JSON.stringify({ on: on })]))
    root.scheduleRefresh()
  }

  function setBrightness(lightId, bri) {
    if (!root.config) return
    var clamped = Math.max(1, Math.min(254, Math.round(bri)))
    root.setLightBri(lightId, clamped)
    root.runAction(HueApi.apiCmd(["put-light-v2", lightId, JSON.stringify({ bri: clamped })]))
    root.scheduleRefresh()
  }

  function setColorTemperature(lightId, ct) {
    if (!root.config) return
    var clamped = Math.max(153, Math.min(500, Math.round(ct)))
    root.setLightCt(lightId, clamped)
    root.runAction(HueApi.apiCmd(["put-light-v2", lightId, JSON.stringify({ ct: clamped })]))
    root.scheduleRefresh()
  }

  function setLightColor(lightId, hue, sat) {
    if (!root.config) return
    root.patchLightColor(lightId, hue, sat)
    root.runAction(HueApi.apiCmd(["put-light-v2", lightId, JSON.stringify({ hue: hue, sat: sat })]))
    root.scheduleRefresh()
  }

  function toggleAll(on) {
    if (!root.config || root.lightedRoomCount === 0) return
    var rooms = root.lightedRooms()
    var body = JSON.stringify({ on: on })
    for (var i = 0; i < rooms.length; i++) {
      if (rooms[i].groupedLightId) {
        root.runAction(HueApi.apiCmd(["put-grouped-light-v2", rooms[i].groupedLightId, body]))
      }
    }
    root.setAllOn(on)
    root.scheduleRefresh()
  }

  function setAllOn(on) {
    root.roomsWithLights = root.roomsWithLights.map(function(room) {
      if (room.lightCount === 0) return room
      return {
        id: room.id,
        name: room.name,
        on: on,
        lightCount: room.lightCount,
        lights: room.lights.map(function(light) { return root.lightCopy(light, on) }),
        groupedLightId: room.groupedLightId || ""
      }
    })
  }

  function runAction(command) {
    root.actionErrorText = ""
    root.actionQueue.push(command)
    drainActionQueue()
  }

  function drainActionQueue() {
    if (actionProc.running) return
    if (root.actionQueue.length === 0) return
    var next = root.actionQueue.shift()
    actionProc.command = next
    actionProc.running = true
  }

  function scheduleRefresh() {
    resyncTimer.restart()
  }

  property FileView configFile: FileView {
    path: root.stateHome + "/omarchy/settings/hue.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      root.config = HueApi.parseConfig(text())
      if (root.config) {
        root.config.username = ""
        root.refresh()
      }
    }
    onLoadFailed: root.config = null
  }

  property FileView themeNameFile: FileView {
    path: root.stateHome + "/omarchy/current/theme.name"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.currentThemeName = String(text()).trim()
  }

  property var themeSync: ({})
  property var sceneRooms: ({})
  property bool sceneDefault: false
  property FileView themeConfigFile: FileView {
    path: Quickshell.env("HOME") + "/.config/omarchy/settings/hue-theme.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var parsed = JSON.parse(text())
        root.themeSync = parsed.themeSync || {}
        root.sceneRooms = parsed.sceneRooms || {}
        root.sceneDefault = parsed.scene === true
      } catch (e) {
        root.themeSync = {}
        root.sceneRooms = {}
        root.sceneDefault = false
      }
    }
    onLoadFailed: {
      root.themeSync = {}
      root.sceneRooms = {}
      root.sceneDefault = false
    }
  }

  Timer {
    interval: 1500
    running: true
    onTriggered: configFile.reload()
  }

  Timer {
    interval: 5000
    repeat: true
    running: root.config === null
    onTriggered: configFile.reload()
  }

  Timer {
    id: resyncTimer
    interval: 700
    onTriggered: root.refresh()
  }

  Timer {
    id: pollTimer
    interval: 15000
    repeat: true
    running: root.config !== null
    onTriggered: root.refresh()
  }

  Process {
    id: lightsProc
    property int generation: 0
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (lightsProc.generation !== root.pollGeneration) return
        root.rawLightsText = text
      }
    }
    onExited: function(exitCode) {
      if (lightsProc.generation !== root.pollGeneration) return
      root.finishFetch(lightsProc.generation, exitCode === 0, exitCode)
    }
  }

  Process {
    id: roomsProc
    property int generation: 0
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (roomsProc.generation !== root.pollGeneration) return
        root.rawRoomsText = text
      }
    }
    onExited: function(exitCode) {
      if (roomsProc.generation !== root.pollGeneration) return
      root.finishFetch(roomsProc.generation, exitCode === 0, exitCode)
    }
  }

  Process {
    id: zonesProc
    property int generation: 0
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (zonesProc.generation !== root.pollGeneration) return
        root.rawZonesText = text
      }
    }
    onExited: function(exitCode) {
      if (zonesProc.generation !== root.pollGeneration) return
      root.finishFetch(zonesProc.generation, exitCode === 0, exitCode)
    }
  }

  Process {
    id: devicesProc
    property int generation: 0
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (devicesProc.generation !== root.pollGeneration) return
        root.rawDevicesText = text
      }
    }
    onExited: function(exitCode) {
      if (devicesProc.generation !== root.pollGeneration) return
      root.finishFetch(devicesProc.generation, exitCode === 0, exitCode)
    }
  }

  Process {
    id: groupedLightsProc
    property int generation: 0
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (groupedLightsProc.generation !== root.pollGeneration) return
        root.rawGroupedLightsText = text
      }
    }
    onExited: function(exitCode) {
      if (groupedLightsProc.generation !== root.pollGeneration) return
      root.finishFetch(groupedLightsProc.generation, exitCode === 0, exitCode)
    }
  }

  Process {
    id: actionProc
    onExited: function(exitCode) {
      if (exitCode !== 0) root.actionErrorText = "Update failed. Refresh and try again."
      root.drainActionQueue()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroll.width
          spacing: Style.space(6)

          Row {
            width: parent.width
            spacing: Style.space(10)

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: "󰌵"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
            }

            Column {
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "Hue Lights" + (root.currentThemeName ? " (" + root.currentThemeName + ")" : "")
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }

              Text {
                text: root.statusText
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

          }

          Rectangle {
            width: parent.width
            height: Style.spacing.hairline
            color: root.bar.foreground
            opacity: 0.12
          }

          Column {
            visible: root.config === null
            width: parent.width
            spacing: Style.space(4)

            Text {
              width: parent.width
              text: "No bridge configured yet."
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              text: "Press the link button on your Hue bridge, then click below."
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Rectangle {
              width: parent.width
              height: pairButton.implicitHeight + Style.space(16)
              radius: Style.space(8)
              color: pairButtonMouse.containsMouse ? Qt.lighter(Color.accent, 1.2) : Color.accent

              Text {
                id: pairButton
                anchors.centerIn: parent
                text: "Pair with bridge"
                color: "#ffffff"
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }

              MouseArea {
                id: pairButtonMouse
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: {
                  var pairPath = Qt.resolvedUrl("pair.sh").toString().replace("file://", "")
                  Quickshell.execDetached(["omarchy-launch-terminal", "bash", pairPath])
                }
              }
            }
          }

          Row {
            visible: root.config !== null && root.loading && root.roomCount === 0 && root.orphanLights.length === 0
            spacing: Style.space(4)

            Text {
              text: "󰦖"
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body

              RotationAnimator on rotation {
                running: root.loading
                from: 0
                to: 360
                duration: 800
                loops: Animation.Infinite
              }
            }

            Text {
              text: "Loading…"
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body
            }
          }

          Text {
            visible: root.config !== null && root.lastFetchFailed && !root.loading
            width: parent.width
            text: root.fetchErrorText
            color: Color.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            visible: root.insecureMode
            width: parent.width
            text: "TLS verification disabled. Re-run pair.sh to secure the connection."
            color: Color.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          InlineToggle {
            visible: root.config !== null && root.lightedRoomCount > 0
            width: parent.width
            label: "All lights"
            checked: root.allLightsOn
            foreground: root.bar.foreground
            accent: Color.accent
            fontFamily: root.bar.fontFamily
            onClicked: root.toggleAll(!root.allLightsOn)
          }

          Column {
            visible: root.config !== null && root.roomCount > 0
            width: parent.width
            spacing: Style.space(4)

            Repeater {
              model: root.lightedRooms()

              Column {
                id: roomColumn
                required property var modelData
                readonly property bool lightsOpen: root.roomExpanded(modelData.id)
                width: parent.width
                spacing: Style.space(2)

                BorderSurface {
                  id: roomHeader
                  width: parent.width
                  radius: Style.cornerRadius
                  implicitHeight: Math.max(54, Style.font.subtitle + Style.spacing.huge)
                  readonly property bool hot: headerMouse.containsMouse || powerTrack.hot
                  readonly property color roomTint: HueApi.roomColor(roomColumn.modelData)
                  color: roomHeader.roomTint.a > 0
                    ? (roomHeader.roomTint.r + roomHeader.roomTint.g + roomHeader.roomTint.b === 0
                       ? "#000000"
                       : Qt.rgba(roomHeader.roomTint.r, roomHeader.roomTint.g, roomHeader.roomTint.b,
                                 roomHeader.hot ? 0.45 : 0.30))
                    : Style.controlFill(false, roomHeader.hot, root.bar.foreground, Color.accent)
                  borderSpec: Border.controlSpec(
                    roomHeader.hot ? "hover-cursor" : "normal",
                    root.bar.foreground, Color.accent)
                  Behavior on color { ColorAnimation { duration: 100 } }

                  activeFocusOnTab: true
                  Keys.onReturnPressed: root.toggleRoom(roomColumn.modelData.id, !roomColumn.modelData.on)
                  Keys.onEnterPressed: root.toggleRoom(roomColumn.modelData.id, !roomColumn.modelData.on)
                  Keys.onSpacePressed: root.toggleRoom(roomColumn.modelData.id, !roomColumn.modelData.on)

                  MouseArea {
                    id: headerMouse
                    anchors.fill: parent
                    hoverEnabled: true
                  }

                  ToggleSwitch {
                    id: powerTrack
                    anchors.right: parent.right
                    anchors.rightMargin: parent.borderRight + Style.spacing.rowPaddingX
                    anchors.verticalCenter: parent.verticalCenter
                    checked: roomColumn.modelData.on
                    foreground: root.bar.foreground
                    accent: Color.accent
                    interactive: true
                    onToggled: root.toggleRoom(roomColumn.modelData.id, !roomColumn.modelData.on)
                  }

                  Rectangle {
                    id: discButton
                    anchors.right: powerTrack.left
                    anchors.rightMargin: Style.spacing.rowPaddingX
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(22)
                    height: Style.space(22)
                    radius: Style.space(5)
                    color: discMouse.containsMouse || roomColumn.lightsOpen
                      ? Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.14)
                      : Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.06)
                    border.width: 1
                    border.color: roomColumn.lightsOpen
                      ? Color.accent
                      : Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.28)

                    Text {
                      anchors.centerIn: parent
                      text: "\u25b8"
                      color: roomColumn.lightsOpen ? Color.accent : Qt.darker(root.bar.foreground, 1.4)
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.caption
                      rotation: roomColumn.lightsOpen ? 90 : 0
                      Behavior on rotation { NumberAnimation { duration: 120 } }
                    }

                    MouseArea {
                      id: discMouse
                      anchors.fill: parent
                      hoverEnabled: true
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.toggleRoomExpanded(roomColumn.modelData.id)
                    }
                  }

                  Text {
                    anchors.left: parent.left
                    anchors.leftMargin: parent.borderLeft + Style.spacing.rowPaddingX
                    anchors.right: discButton.left
                    anchors.rightMargin: Style.spacing.rowPaddingX
                    anchors.verticalCenter: parent.verticalCenter
                    text: roomColumn.modelData.name + " (" + roomColumn.modelData.lightCount + ")"
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.subtitle
                    font.bold: true
                    elide: Text.ElideRight
                  }
                }

                Row {
                  id: syncRow
                  visible: modelData.on && roomColumn.lightsOpen
                  width: parent.width
                  spacing: Style.space(1)
                  readonly property bool sceneUsable: root.roomSceneApplies(modelData)

                  InlineToggle {
                    width: syncRow.sceneUsable ? (parent.width - parent.spacing) / 2 : parent.width
                    label: "Theme Sync"
                    checked: root.themeSync[modelData.id] !== false
                    foreground: root.bar.foreground
                    accent: Color.accent
                    fontFamily: root.bar.fontFamily
                    onClicked: {
                      var ts = JSON.parse(JSON.stringify(root.themeSync))
                      ts[modelData.id] = ts[modelData.id] === false
                      root.themeSync = ts
                      root.runAction(HueApi.apiCmd(["write-theme-config", JSON.stringify(ts)]))
                    }
                  }

                  InlineToggle {
                    visible: syncRow.sceneUsable
                    width: (parent.width - parent.spacing) / 2
                    label: "Scene Mode"
                    checked: root.roomSceneDefault(modelData.id)
                    foreground: root.bar.foreground
                    accent: Color.accent
                    fontFamily: root.bar.fontFamily
                    onClicked: {
                      var ss = JSON.parse(JSON.stringify(root.sceneRooms))
                      ss[modelData.id] = !root.roomSceneDefault(modelData.id)
                      root.sceneRooms = ss
                      root.runAction(HueApi.apiCmd(["write-scene-config", JSON.stringify(ss)]))
                    }
                  }
                }

                Repeater {
                  model: modelData.lights

                  Column {
                    id: roomLightRow
                    required property var modelData
                    readonly property bool themeSynced: root.roomSyncOn(roomColumn.modelData.id)
                    visible: roomColumn.lightsOpen
                    width: parent.width
                    spacing: Style.space(1)

                    InlineToggle {
                      width: parent.width
                      label: modelData.name
                      titleSize: Style.font.body
                      rowColor: HueApi.lightColor(modelData)
                      checked: modelData.on
                      foreground: Qt.darker(root.bar.foreground, 1.2)
                      accent: Color.accent
                      fontFamily: root.bar.fontFamily
                      onClicked: root.toggleLight(modelData.id, !modelData.on)
                    }

                    Row {
                      visible: modelData.on && modelData.hasColor
                      width: parent.width - Style.space(24)
                      anchors.horizontalCenter: parent.horizontalCenter
                      spacing: Style.space(8)

                      PanelSlider {
                        width: roomLightRow.themeSynced ? parent.width : parent.width - Style.space(30)
                        bar: root.bar
                        minimum: 1
                        maximum: 254
                        integer: true
                        step: 10
                        value: modelData.bri
                        onReleased: function(v) { root.setBrightness(modelData.id, v) }
                      }

                      Item {
                        visible: !roomLightRow.themeSynced
                        width: Style.space(22)
                        height: Style.space(22)

                        Image {
                          anchors.fill: parent
                          source: Qt.resolvedUrl("hsv_wheel.png")
                          fillMode: Image.Stretch
                          smooth: true
                        }

                        Rectangle {
                          anchors.fill: parent
                          radius: Style.space(11)
                          border.width: modelData.pickerOpen ? 2 : 1
                          border.color: modelData.pickerOpen ? Color.accent : Qt.darker(root.bar.foreground, 1.6)
                          color: "transparent"
                        }

                        MouseArea {
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          onClicked: root.toggleColorPicker(modelData.id)
                        }
                      }
                    }

                    ColorWheel {
                      lightOn: modelData.on
                      hasColor: modelData.hasColor
                      pickerOpen: modelData.pickerOpen
                      themeSynced: roomLightRow.themeSynced
                      initialHue: modelData.hue
                      initialSat: modelData.sat
                      onColorSelected: function(hue, sat) { root.setLightColor(modelData.id, hue, sat) }
                    }

                    PanelSlider {
                      visible: modelData.on && modelData.hasCt && !roomLightRow.themeSynced
                      width: parent.width - Style.space(24)
                      anchors.horizontalCenter: parent.horizontalCenter
                      bar: root.bar
                      minimum: 153
                      maximum: 500
                      integer: true
                      step: 10
                      value: modelData.ct
                      onReleased: function(v) { root.setColorTemperature(modelData.id, v) }
                    }
                  }
                }
              }
            }
          }

          Item {
            id: emptyRoomsSection
            visible: root.config !== null && root.emptyRoomCount > 0
            width: parent.width
            height: emptyRoomsInner.height + Style.space(16)

            property bool expanded: false

            Rectangle {
              anchors.fill: parent
              radius: Style.space(8)
              color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b,
                headerMouse.containsMouse || emptyRoomsSection.expanded ? 0.10 : 0.05)
              border.width: 1
              border.color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b,
                headerMouse.containsMouse || emptyRoomsSection.expanded ? 0.32 : 0.16)
            }

            MouseArea {
              id: headerMouse
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(8)
              height: headerRow.height
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: emptyRoomsSection.expanded = !emptyRoomsSection.expanded
            }

            Text {
              anchors.right: parent.right
              anchors.rightMargin: Style.space(10)
              y: Style.space(8) + (headerRow.height - height) / 2
              text: "\u25b8"
              color: headerMouse.containsMouse ? Color.accent : Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body
              rotation: emptyRoomsSection.expanded ? 90 : 0
              transformOrigin: Item.Center
              Behavior on rotation { NumberAnimation { duration: 120 } }
            }

            Column {
              id: emptyRoomsInner
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(8)
              spacing: Style.space(6)

              Row {
                id: headerRow
                width: parent.width
                spacing: Style.space(6)

                Text {
                  text: "Empty rooms"
                  color: headerMouse.containsMouse ? Color.accent : root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  text: "(" + root.emptyRoomCount + ")"
                  color: Qt.darker(root.bar.foreground, 1.4)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              Column {
                visible: emptyRoomsSection.expanded
                width: parent.width
                spacing: Style.space(4)

                Rectangle {
                  width: parent.width
                  height: Style.spacing.hairline
                  color: root.bar.foreground
                  opacity: 0.12
                }

                Repeater {
                  model: root.emptyRooms()

                  Column {
                    id: emptyRoomRow
                    required property var modelData
                    width: parent.width
                    spacing: Style.space(2)

                    InlineToggle {
                      width: parent.width
                      label: modelData.name
                      checked: modelData.on
                      foreground: root.bar.foreground
                      accent: Color.accent
                      fontFamily: root.bar.fontFamily
                      onClicked: root.toggleRoom(modelData.id, !modelData.on)
                    }

                    InlineToggle {
                      visible: modelData.on
                      width: parent.width
                      label: "Theme Sync"
                      checked: root.themeSync[modelData.id] !== false
                      foreground: root.bar.foreground
                      accent: Color.accent
                      fontFamily: root.bar.fontFamily
                      onClicked: {
                        var ts = JSON.parse(JSON.stringify(root.themeSync))
                        ts[modelData.id] = ts[modelData.id] === false
                        root.themeSync = ts
                        root.runAction(HueApi.apiCmd(["write-theme-config", JSON.stringify(ts)]))
                      }
                    }
                  }
                }
              }
            }
          }

          Column {
            visible: root.config !== null && root.orphanLights.length > 0
            width: parent.width
            spacing: Style.space(2)

            Text {
              text: "Other lights"
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.letterSpacing: 1
              font.bold: true
            }

            Repeater {
              model: root.orphanLights

              Column {
                id: orphanLightRow
                required property var modelData
                width: parent.width
                spacing: Style.space(2)

                InlineToggle {
                  width: parent.width
                  label: modelData.name
                  titleSize: Style.font.body
                  rowColor: HueApi.lightColor(modelData)
                  checked: modelData.on
                  foreground: Qt.darker(root.bar.foreground, 1.2)
                  accent: Color.accent
                  fontFamily: root.bar.fontFamily
                  onClicked: root.toggleLight(modelData.id, !modelData.on)
                }

                PanelSlider {
                  visible: modelData.on && modelData.hasBri
                  width: parent.width - Style.space(24)
                  anchors.horizontalCenter: parent.horizontalCenter
                  bar: root.bar
                  minimum: 1
                  maximum: 254
                  integer: true
                  step: 10
                  value: modelData.bri
                  onReleased: function(v) { root.setBrightness(modelData.id, v) }
                }

                Row {
                  visible: modelData.on && modelData.hasColor
                  width: parent.width - Style.space(24)
                  anchors.horizontalCenter: parent.horizontalCenter
                  spacing: Style.space(8)

                  PanelSlider {
                    width: parent.width - Style.space(30)
                    bar: root.bar
                    minimum: 1
                    maximum: 254
                    integer: true
                    step: 10
                    value: modelData.bri
                    onReleased: function(v) { root.setBrightness(modelData.id, v) }
                  }

                  Item {
                    width: Style.space(22)
                    height: Style.space(22)

                    Image {
                      anchors.fill: parent
                      source: Qt.resolvedUrl("hsv_wheel.png")
                      fillMode: Image.Stretch
                      smooth: true
                    }

                    Rectangle {
                      anchors.fill: parent
                      radius: Style.space(11)
                      border.width: modelData.pickerOpen ? 2 : 1
                      border.color: modelData.pickerOpen ? Color.accent : Qt.darker(root.bar.foreground, 1.6)
                      color: "transparent"
                    }

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.toggleColorPicker(modelData.id)
                    }
                  }
                }

                ColorWheel {
                  lightOn: modelData.on
                  hasColor: modelData.hasColor
                  pickerOpen: modelData.pickerOpen
                  initialHue: modelData.hue
                  initialSat: modelData.sat
                  onColorSelected: function(hue, sat) { root.setLightColor(modelData.id, hue, sat) }
                }

                PanelSlider {
                  visible: modelData.on && modelData.hasCt
                  width: parent.width - Style.space(24)
                  anchors.horizontalCenter: parent.horizontalCenter
                  bar: root.bar
                  minimum: 153
                  maximum: 500
                  integer: true
                  step: 10
                  value: modelData.ct
                  onReleased: function(v) { root.setColorTemperature(modelData.id, v) }
                }
              }
            }
          }

          Text {
            visible: root.config !== null && !root.loading && !root.lastFetchFailed && root.roomCount === 0 && root.orphanLights.length === 0
            text: "No lights found on this bridge."
            color: Qt.darker(root.bar.foreground, 1.4)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
