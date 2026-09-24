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
  property var scenes: []
  property var roomsWithLights: []
  property var orphanLights: []
  property int pendingFetches: 0
  property bool fetchScheduled: false
  property bool refreshRequested: false
  property bool loading: false
  property bool lastFetchFailed: false
  property int consecutiveFetchFailures: 0
  property string lastSuccessfulFetch: ""
  property var favoriteRooms: ({})
  property var actionQueue: []
  property var expandedRooms: ({})
  property var expandedSceneMenus: ({})
  property string sceneStatus: ""
  property string sceneStatusRoomId: ""
  readonly property var prebuiltScenes: ["Relax", "Read", "Natural Light", "Tv Mode"]

  readonly property int roomCount: root.roomsWithLights.length
  readonly property int lightTotal: root.lightsTotal()
  readonly property int lightedRoomCount: root.lightedRooms().length
  readonly property int emptyRoomCount: root.emptyRooms().length
  readonly property bool allLightsOn: root.computeAllLightsOn()
  readonly property bool insecureMode: root.config !== null && !root.config.bridgeId

  readonly property string statusText: {
    if (root.config === null) return "Not paired"
    var refreshed = root.lastSuccessfulFetch ? " · Updated " + root.lastSuccessfulFetch : ""
    if (root.lastFetchFailed) return "Bridge unreachable" + refreshed
    if (root.loading) return "Loading…"
    var roomLabel = root.lightedRoomCount + " room" + (root.lightedRoomCount === 1 ? "" : "s")
    var unreachable = root.unreachableLightCount()
    var lightLabel = root.lightTotal + " light" + (root.lightTotal === 1 ? "" : "s")
    if (unreachable > 0) lightLabel += " · " + unreachable + " unreachable"
    var apiLabel = root.config ? " · Hue " + root.config.apiVersion : ""
    return roomLabel + " · " + lightLabel + apiLabel + refreshed
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
    result.sort(function(a, b) {
      var af = root.favoriteRooms[a.id] === true
      var bf = root.favoriteRooms[b.id] === true
      return af === bf ? a.name.localeCompare(b.name) : (af ? -1 : 1)
    })
    return result
  }

  function toggleFavorite(roomId) {
    var favorites = JSON.parse(JSON.stringify(root.favoriteRooms))
    favorites[roomId] = favorites[roomId] !== true
    root.favoriteRooms = favorites
    root.runAction(HueApi.apiCmd(["write-favorite-config", JSON.stringify(favorites)]))
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

  function unreachableLightCount() {
    var count = 0
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      for (var j = 0; j < root.roomsWithLights[i].lights.length; j++) {
        if (root.roomsWithLights[i].lights[j].reachable === false) count++
      }
    }
    for (var k = 0; k < root.orphanLights.length; k++) {
      if (root.orphanLights[k].reachable === false) count++
    }
    return count
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
    if (root.pendingFetches > 0 || root.fetchScheduled) {
      root.refreshRequested = true
      return
    }
    if (root.roomsWithLights.length === 0 && root.orphanLights.length === 0) root.loading = true
    root.fetchScheduled = true
    Qt.callLater(startFetches)
  }

  function startFetches() {
    root.fetchScheduled = false
    if (!root.config) return
    if (root.pendingFetches > 0) {
      root.refreshRequested = true
      return
    }
    root.lastFetchFailed = false
    root.pendingFetches = 3
    prepareFetch(lightsProc, ["get-lights"])
    prepareFetch(groupsProc, ["get-groups"])
    prepareFetch(scenesProc, ["get-scenes"])
  }

  function prepareFetch(proc, args) {
    proc.fetchOutput = ""
    proc.fetchOutputFinished = false
    proc.fetchExitCode = -1
    proc.fetchExited = false
    proc.fetchReported = false
    proc.command = HueApi.apiCmd(args)
    proc.running = true
  }

  function tryCompleteFetch(proc) {
    if (proc.fetchReported || !proc.fetchOutputFinished || !proc.fetchExited) return
    proc.fetchReported = true
    var success = proc.fetchExitCode === 0
    if (success) {
      try {
        var parsed = JSON.parse(proc.fetchOutput)
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) success = false
        else if (proc.fetchKind === "lights") {
          var lights = HueApi.parseLights(proc.fetchOutput)
          var byId = {}
          for (var i = 0; i < lights.length; i++) byId[lights[i].id] = lights[i]
          root.lightsById = byId
        } else if (proc.fetchKind === "groups") {
          root.rooms = HueApi.parseGroups(proc.fetchOutput)
        } else if (proc.fetchKind === "scenes") {
          root.scenes = HueApi.parseScenes(proc.fetchOutput)
        }
      } catch (error) {
        success = false
      }
    }
    root.finishFetch(success)
  }

  function finishFetch(success) {
    if (root.pendingFetches <= 0) return
    root.pendingFetches--
    if (success === false) root.lastFetchFailed = true
    if (root.pendingFetches === 0) {
      root.loading = false
      if (root.lastFetchFailed) root.consecutiveFetchFailures++
      else {
        root.consecutiveFetchFailures = 0
        root.lastSuccessfulFetch = new Date().toLocaleTimeString()
      }
      root.assembleRooms()
      if (root.refreshRequested) {
        root.refreshRequested = false
        root.refresh()
      } else if (root.opened) {
        pollTimer.restart()
      }
    }
  }

  function assembleRooms() {
    var used = {}
    var result = []
    for (var i = 0; i < root.rooms.length; i++) {
      var room = root.rooms[i]
      var lights = HueApi.roomLights(room, root.lightsById)
      for (var j = 0; j < room.lightIds.length; j++) used[room.lightIds[j]] = true
      result.push({
        id: room.id,
        apiId: room.apiId,
        controlId: room.controlId,
        name: room.name,
        on: room.on,
        lightCount: lights.length,
        lights: lights
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
      apiId: light.apiId,
      name: light.name,
      on: changes.on !== undefined ? changes.on : light.on,
      bri: changes.bri !== undefined ? changes.bri : light.bri,
      hasBri: light.hasBri,
      ct: changes.ct !== undefined ? changes.ct : light.ct,
      hasCt: light.hasCt,
      ctMin: light.ctMin,
      ctMax: light.ctMax,
      hue: changes.hue !== undefined ? changes.hue : light.hue,
      sat: changes.sat !== undefined ? changes.sat : light.sat,
      hasColor: light.hasColor,
      colormode: light.colormode,
      xy: light.xy ? light.xy.slice() : [],
      gamut: light.gamut || {},
      pickerOpen: changes.pickerOpen !== undefined ? changes.pickerOpen : light.pickerOpen
    }
  }

  function lightCopy(light, on) {
    return root.lightClone(light, { on: on })
  }

  function setRoomOn(roomId, on) {
    var newRooms = []
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      var room = root.roomsWithLights[i]
      newRooms.push({
        id: room.id,
        apiId: room.apiId,
        controlId: room.controlId,
        name: room.name,
        on: room.id === roomId ? on : room.on,
        lightCount: room.lightCount,
        lights: room.id === roomId
          ? room.lights.map(function(light) { return root.lightCopy(light, on) })
          : room.lights
      })
    }
    root.roomsWithLights = newRooms
  }

  function roomHasBrightness(room) {
    for (var i = 0; i < room.lights.length; i++) {
      if (room.lights[i].hasBri) return true
    }
    return false
  }

  function roomBrightness(room) {
    var total = 0
    var count = 0
    for (var i = 0; i < room.lights.length; i++) {
      if (!room.lights[i].hasBri) continue
      total += room.lights[i].bri
      count++
    }
    return count > 0 ? Math.round(total / count) : 1
  }

  function setRoomBrightness(roomId, bri) {
    if (!root.config) return
    var target = root.roomById(roomId)
    if (!target || !target.controlId) return
    var clamped = Math.max(1, Math.min(254, Math.round(bri)))
    root.roomsWithLights = root.roomsWithLights.map(function(room) {
      if (room.id !== roomId) return room
      return {
        id: room.id,
        apiId: room.apiId,
        controlId: room.controlId,
        name: room.name,
        on: room.on,
        lightCount: room.lightCount,
        lights: room.lights.map(function(light) {
          return light.hasBri ? root.lightClone(light, { bri: clamped }) : light
        })
      }
    })
    root.runAction(HueApi.apiCmd(["put-group", target.controlId, JSON.stringify({ bri: clamped })]))
    root.scheduleRefresh()
  }

  function setLightOn(lightId, on) {
    var newRooms = []
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      var room = root.roomsWithLights[i]
      newRooms.push({
        id: room.id,
        apiId: room.apiId,
        controlId: room.controlId,
        name: room.name,
        on: room.on,
        lightCount: room.lightCount,
        lights: room.lights.map(function(light) {
          return light.id === lightId ? root.lightCopy(light, on) : light
        })
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
        apiId: room.apiId,
        controlId: room.controlId,
        name: room.name,
        on: room.on,
        lightCount: room.lightCount,
        lights: room.lights.map(function(light) {
          return light.id === lightId ? root.lightClone(light, changes) : light
        })
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

  function roomById(roomId) {
    for (var i = 0; i < root.roomsWithLights.length; i++) {
      if (root.roomsWithLights[i].id === roomId) return root.roomsWithLights[i]
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

  function roomScenesExpanded(roomId) {
    return root.expandedSceneMenus[roomId] === true
  }

  function roomSceneActive(room, sceneName) {
    var wanted = String(sceneName).trim().toLowerCase()
    for (var i = 0; i < root.scenes.length; i++) {
      var scene = root.scenes[i]
      if (scene.active && scene.groupApiId === room.apiId && scene.name.trim().toLowerCase() === wanted) return true
    }
    return false
  }

  function toggleRoomScenes(roomId) {
    var map = JSON.parse(JSON.stringify(root.expandedSceneMenus))
    map[roomId] = map[roomId] !== true
    root.expandedSceneMenus = map
  }

  function applyScene(roomId, sceneName) {
    if (!root.config) return
    root.sceneStatusRoomId = roomId
    root.sceneStatus = "Applying " + sceneName + "…"
    root.runAction(HueApi.apiCmd(["apply-scene", sceneName, roomId]))
    root.scheduleRefresh()
    sceneStatusTimer.restart()
  }

  function toggleColorPicker(lightId) {
    var light = root.lightById(lightId)
    if (light) root.patchLights(lightId, { pickerOpen: !light.pickerOpen })
  }

  function toggleRoom(roomId, on) {
    if (!root.config) return
    var room = root.roomById(roomId)
    if (!room || !room.controlId) return
    root.setRoomOn(roomId, on)
    root.runAction(HueApi.apiCmd(["put-group", room.controlId, JSON.stringify({ on: on })]))
    root.scheduleRefresh()
  }

  function toggleLight(lightId, on) {
    if (!root.config) return
    var light = root.lightById(lightId)
    if (!light) return
    root.setLightOn(lightId, on)
    root.runAction(HueApi.apiCmd(["put-light", light.apiId, JSON.stringify({ on: on })]))
    root.scheduleRefresh()
  }

  function setBrightness(lightId, bri) {
    if (!root.config) return
    var light = root.lightById(lightId)
    if (!light) return
    var clamped = Math.max(1, Math.min(254, Math.round(bri)))
    root.setLightBri(lightId, clamped)
    root.runAction(HueApi.apiCmd(["put-light", light.apiId, JSON.stringify({ bri: clamped })]))
    root.scheduleRefresh()
  }

  function setColorTemperature(lightId, ct) {
    if (!root.config) return
    var light = root.lightById(lightId)
    if (!light) return
    var clamped = Math.max(light.ctMin, Math.min(light.ctMax, Math.round(ct)))
    root.setLightCt(lightId, clamped)
    root.runAction(HueApi.apiCmd(["put-light", light.apiId, JSON.stringify({ ct: clamped })]))
    root.scheduleRefresh()
  }

  function setLightColor(lightId, hue, sat) {
    if (!root.config) return
    var light = root.lightById(lightId)
    if (!light) return
    root.patchLightColor(lightId, hue, sat)
    root.runAction(HueApi.apiCmd(["put-light", light.apiId, JSON.stringify({ hue: hue, sat: sat })]))
    root.scheduleRefresh()
  }

  function toggleAll(on) {
    if (!root.config || root.lightedRoomCount === 0) return
    var rooms = root.lightedRooms()
    var body = JSON.stringify({ on: on })
    for (var i = 0; i < rooms.length; i++) {
      if (rooms[i].controlId) root.runAction(HueApi.apiCmd(["put-group", rooms[i].controlId, body]))
    }
    root.setAllOn(on)
    root.scheduleRefresh()
  }

  function setAllOn(on) {
    root.roomsWithLights = root.roomsWithLights.map(function(room) {
      if (room.lightCount === 0) return room
      return {
        id: room.id,
        apiId: room.apiId,
        controlId: room.controlId,
        name: room.name,
        on: on,
        lightCount: room.lightCount,
        lights: room.lights.map(function(light) { return root.lightCopy(light, on) })
      }
    })
  }

  function runAction(command) {
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
    path: Quickshell.env("HOME") + "/.local/state/omarchy/settings/hue.json"
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
    path: Quickshell.env("HOME") + "/.local/state/omarchy/current/theme.name"
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
        root.favoriteRooms = parsed.favoriteRooms || {}
        root.sceneRooms = parsed.sceneRooms || {}
        root.sceneDefault = parsed.scene === true
      } catch (e) {
        root.themeSync = {}
        root.favoriteRooms = {}
        root.sceneRooms = {}
        root.sceneDefault = false
      }
    }
    onLoadFailed: {
      root.themeSync = {}
      root.favoriteRooms = {}
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
    id: sceneStatusTimer
    interval: 2500
    onTriggered: {
      root.sceneStatus = ""
      root.sceneStatusRoomId = ""
    }
  }

  Timer {
    id: pollTimer
    interval: root.consecutiveFetchFailures > 0
      ? Math.min(120000, 15000 * Math.pow(2, root.consecutiveFetchFailures)) : 15000
    repeat: true
    running: root.config !== null && root.opened
    onTriggered: root.refresh()
  }

  Process {
    id: lightsProc
    property string fetchKind: "lights"
    property string fetchOutput: ""
    property bool fetchOutputFinished: false
    property int fetchExitCode: -1
    property bool fetchExited: false
    property bool fetchReported: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        lightsProc.fetchOutput = text
        lightsProc.fetchOutputFinished = true
        root.tryCompleteFetch(lightsProc)
      }
    }
    onExited: function(exitCode) {
      fetchExitCode = exitCode
      fetchExited = true
      root.tryCompleteFetch(lightsProc)
    }
  }

  Process {
    id: groupsProc
    property string fetchKind: "groups"
    property string fetchOutput: ""
    property bool fetchOutputFinished: false
    property int fetchExitCode: -1
    property bool fetchExited: false
    property bool fetchReported: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        groupsProc.fetchOutput = text
        groupsProc.fetchOutputFinished = true
        root.tryCompleteFetch(groupsProc)
      }
    }
    onExited: function(exitCode) {
      fetchExitCode = exitCode
      fetchExited = true
      root.tryCompleteFetch(groupsProc)
    }
  }

  Process {
    id: scenesProc
    property string fetchKind: "scenes"
    property string fetchOutput: ""
    property bool fetchOutputFinished: false
    property int fetchExitCode: -1
    property bool fetchExited: false
    property bool fetchReported: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        scenesProc.fetchOutput = text
        scenesProc.fetchOutputFinished = true
        root.tryCompleteFetch(scenesProc)
      }
    }
    onExited: function(exitCode) {
      fetchExitCode = exitCode
      fetchExited = true
      root.tryCompleteFetch(scenesProc)
    }
  }

  Process {
    id: actionProc
    onExited: function(exitCode) {
      root.scheduleRefresh()
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
            text: "Couldn't reach the bridge. Check that it's on and the IP is valid. Retrying automatically with increasing delays."
            color: Color.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          BorderSurface {
            visible: root.config !== null && root.lastFetchFailed
            width: parent.width
            height: Style.space(40)
            radius: Style.cornerRadius
            color: Style.controlFill(activeFocus, retryMouse.containsMouse, root.bar.foreground, Color.accent)
            borderSpec: Border.controlSpec(activeFocus ? "focus" : (retryMouse.containsMouse ? "hover-cursor" : "normal"), root.bar.foreground, Color.accent)
            activeFocusOnTab: true
            Accessible.role: Accessible.Button
            Accessible.name: "Retry Hue bridge connection now"
            Keys.onReturnPressed: root.refresh()
            Keys.onEnterPressed: root.refresh()
            Keys.onSpacePressed: root.refresh()
            Text {
              anchors.centerIn: parent
              text: "Retry now"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body
            }
            MouseArea {
              id: retryMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.refresh()
            }
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
                  Accessible.name: roomColumn.modelData.name + " lights toggle"
                  Accessible.description: roomColumn.modelData.lightCount + " lights, currently " + (roomColumn.modelData.on ? "on" : "off")
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
                    id: favoriteButton
                    anchors.right: discButton.left
                    anchors.rightMargin: Style.spacing.rowPaddingX
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(22)
                    height: Style.space(22)
                    color: "transparent"
                    Accessible.role: Accessible.Button
                    Accessible.name: (root.favoriteRooms[roomColumn.modelData.id] === true ? "Remove " : "Add ") + roomColumn.modelData.name + " favorite"
                    activeFocusOnTab: true
                    Keys.onReturnPressed: root.toggleFavorite(roomColumn.modelData.id)
                    Keys.onSpacePressed: root.toggleFavorite(roomColumn.modelData.id)
                    Text {
                      anchors.centerIn: parent
                      text: root.favoriteRooms[roomColumn.modelData.id] === true ? "★" : "☆"
                      color: root.favoriteRooms[roomColumn.modelData.id] === true ? Color.accent : root.bar.foreground
                      font.pixelSize: Style.font.body
                    }
                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.toggleFavorite(roomColumn.modelData.id)
                    }
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
                    anchors.right: favoriteButton.left
                    anchors.rightMargin: Style.spacing.rowPaddingX
                    anchors.verticalCenter: parent.verticalCenter
                    text: roomColumn.modelData.name + " (" + roomColumn.modelData.lightCount + ")"
                    Accessible.name: roomColumn.modelData.name + ", " + roomColumn.modelData.lightCount + " lights, " + (roomColumn.modelData.on ? "on" : "off")
                    textFormat: Text.PlainText
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.subtitle
                    font.bold: true
                    elide: Text.ElideRight
                  }
                }

                Row {
                  id: roomBrightnessRow
                  visible: roomColumn.lightsOpen && root.roomHasBrightness(roomColumn.modelData)
                  width: parent.width - Style.space(24)
                  anchors.horizontalCenter: parent.horizontalCenter
                  spacing: Style.space(10)

                  Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Brightness"
                    color: Qt.darker(root.bar.foreground, 1.35)
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }

                  PanelSlider {
                    width: parent.width - x
                    anchors.verticalCenter: parent.verticalCenter
                    bar: root.bar
                    minimum: 1
                    maximum: 254
                    integer: true
                    step: 10
                    value: root.roomBrightness(roomColumn.modelData)
                    onReleased: function(v) { root.setRoomBrightness(roomColumn.modelData.id, v) }
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
                      var turningOn = ts[modelData.id] === false
                      ts[modelData.id] = turningOn
                      root.themeSync = ts
                      actionProc.command = HueApi.apiCmd(["write-theme-config", JSON.stringify(ts)])
                      actionProc.running = true
                      if (turningOn) {
                        root.runAction(HueApi.apiCmd(["sync-room", modelData.id]))
                      }
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
                      actionProc.command = HueApi.apiCmd(["write-scene-config", JSON.stringify(ss)])
                      actionProc.running = true
                    }
                  }
                }

                BorderSurface {
                  visible: roomColumn.lightsOpen && root.roomSyncOn(roomColumn.modelData.id) && root.currentThemeName !== ""
                  width: parent.width
                  height: Style.space(42)
                  radius: Style.cornerRadius
                  color: Style.controlFill(activeFocus, previewMouse.containsMouse, root.bar.foreground, Color.accent)
                  borderSpec: Border.controlSpec(activeFocus ? "focus" : (previewMouse.containsMouse ? "hover-cursor" : "normal"), root.bar.foreground, Color.accent)
                  activeFocusOnTab: true
                  Accessible.role: Accessible.Button
                  Accessible.name: "Preview current theme " + root.currentThemeName + " in " + roomColumn.modelData.name
                  Keys.onReturnPressed: root.runAction(HueApi.apiCmd(["sync-room", roomColumn.modelData.id]))
                  Keys.onEnterPressed: root.runAction(HueApi.apiCmd(["sync-room", roomColumn.modelData.id]))
                  Keys.onSpacePressed: root.runAction(HueApi.apiCmd(["sync-room", roomColumn.modelData.id]))
                  Text {
                    anchors.centerIn: parent
                    text: "Preview theme: " + root.currentThemeName
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    id: previewMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.runAction(HueApi.apiCmd(["sync-room", roomColumn.modelData.id]))
                  }
                }

                Column {
                  visible: roomColumn.lightsOpen
                  width: parent.width
                  spacing: Style.space(2)

                  BorderSurface {
                    id: roomScenesHeader
                    width: parent.width
                    radius: Style.cornerRadius
                    implicitHeight: Math.max(42, Style.font.body + Style.spacing.huge)
                    readonly property bool menuOpen: root.roomScenesExpanded(roomColumn.modelData.id)
                    readonly property bool hot: roomScenesMouse.containsMouse || activeFocus || menuOpen
                    color: Style.controlFill(activeFocus, hot, root.bar.foreground, Color.accent)
                    borderSpec: Border.controlSpec(activeFocus ? "focus" : (hot ? "hover-cursor" : "normal"), root.bar.foreground, Color.accent)

                    activeFocusOnTab: true
                    Keys.onReturnPressed: root.toggleRoomScenes(roomColumn.modelData.id)
                    Keys.onEnterPressed: root.toggleRoomScenes(roomColumn.modelData.id)
                    Keys.onSpacePressed: root.toggleRoomScenes(roomColumn.modelData.id)

                    MouseArea {
                      id: roomScenesMouse
                      anchors.fill: parent
                      hoverEnabled: true
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.toggleRoomScenes(roomColumn.modelData.id)
                    }

                    Text {
                      anchors.left: parent.left
                      anchors.leftMargin: parent.borderLeft + Style.spacing.rowPaddingX
                      anchors.right: sceneArrow.left
                      anchors.rightMargin: Style.spacing.rowPaddingX
                      anchors.verticalCenter: parent.verticalCenter
                      text: root.sceneStatus !== "" && root.sceneStatusRoomId === roomColumn.modelData.id
                        ? root.sceneStatus : "Scenes"
                      textFormat: Text.PlainText
                      elide: Text.ElideRight
                      color: root.bar.foreground
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                    }

                    Text {
                      id: sceneArrow
                      anchors.right: parent.right
                      anchors.rightMargin: parent.borderRight + Style.spacing.rowPaddingX
                      anchors.verticalCenter: parent.verticalCenter
                      text: "\u25b8"
                      color: roomScenesHeader.menuOpen ? Color.accent : Qt.darker(root.bar.foreground, 1.4)
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.body
                      rotation: roomScenesHeader.menuOpen ? 90 : 0
                      transformOrigin: Item.Center
                      Behavior on rotation { NumberAnimation { duration: 120 } }
                    }
                  }

                  Grid {
                    id: roomSceneGrid
                    visible: roomScenesHeader.menuOpen
                    width: parent.width
                    columns: 2
                    spacing: Style.space(2)

                    Repeater {
                      model: root.prebuiltScenes

                      BorderSurface {
                        id: roomSceneTile
                        required property string modelData
                        width: (roomSceneGrid.width - roomSceneGrid.spacing) / 2
                        height: Math.max(46, Style.font.body + Style.spacing.huge)
                        radius: Style.cornerRadius
                        readonly property bool sceneActive: root.roomSceneActive(roomColumn.modelData, modelData)
                        readonly property bool hot: roomSceneMouse.containsMouse || activeFocus
                        color: sceneActive
                          ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, hot ? 0.34 : 0.24)
                          : Style.controlFill(activeFocus, hot, Qt.darker(root.bar.foreground, 1.2), Color.accent)
                        borderSpec: Border.controlSpec(sceneActive ? "focus" : (activeFocus ? "focus" : (hot ? "hover-cursor" : "normal")), Qt.darker(root.bar.foreground, 1.2), Color.accent)

                        activeFocusOnTab: true
                        Accessible.role: Accessible.Button
                        Accessible.name: (sceneActive ? "Active scene " : "Apply scene ") + modelData + " for " + roomColumn.modelData.name
                        Keys.onReturnPressed: root.applyScene(roomColumn.modelData.id, modelData)
                        Keys.onEnterPressed: root.applyScene(roomColumn.modelData.id, modelData)
                        Keys.onSpacePressed: root.applyScene(roomColumn.modelData.id, modelData)

                        MouseArea {
                          id: roomSceneMouse
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: Qt.PointingHandCursor
                          onClicked: root.applyScene(roomColumn.modelData.id, roomSceneTile.modelData)
                        }

                        Text {
                          anchors.centerIn: parent
                          width: parent.width - Style.space(12)
                          text: (roomSceneTile.sceneActive ? "✓ " : "") + roomSceneTile.modelData
                          textFormat: Text.PlainText
                          horizontalAlignment: Text.AlignHCenter
                          wrapMode: Text.WordWrap
                          color: roomSceneTile.sceneActive ? Color.accent : Qt.darker(root.bar.foreground, 1.2)
                          font.family: root.bar.fontFamily
                          font.pixelSize: Style.font.body
                          font.bold: true
                        }
                      }
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
                      label: modelData.name + (modelData.reachable === false ? " · unreachable" : "")
                      Accessible.name: modelData.name + " light toggle, " + (modelData.on ? "on" : "off") + (modelData.reachable === false ? ", unreachable" : "")
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
                      minimum: modelData.ctMin
                      maximum: modelData.ctMax
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
                        var turningOn = ts[modelData.id] === false
                        ts[modelData.id] = turningOn
                        root.themeSync = ts
                        actionProc.command = HueApi.apiCmd(["write-theme-config", JSON.stringify(ts)])
                        actionProc.running = true
                        if (turningOn) {
                          root.runAction(HueApi.apiCmd(["sync-room", modelData.id]))
                        }
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
                  minimum: modelData.ctMin
                  maximum: modelData.ctMax
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
