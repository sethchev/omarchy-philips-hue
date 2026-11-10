var API = Qt.resolvedUrl("hue-api.py").toString().replace("file://", "")

// Match the Python producer. Its stdout is ASCII JSON followed by a newline.
var maxSnapshotSize = 524288
var maxLights = 192
var maxGroups = 64
var maxScenes = 512
var maxGroupMembers = 256
var maxTotalMembers = 512
var maxNameLength = 128

function apiCmd(args) {
  var cmd = ["python3", API]
  for (var i = 0; i < args.length; i++) cmd.push(String(args[i]))
  cmd.push("--json-errors")
  return cmd
}

function isValidIp(ip) {
  return /^(\d{1,3}\.){3}\d{1,3}$/.test(ip) && ip.split(".").every(function(part) {
    return Number(part) <= 255 && (part === "0" || part.charAt(0) !== "0")
  })
}

function isValidId(id) {
  return /^[a-zA-Z0-9_-]{1,64}$/.test(String(id))
}

function parseConfig(text) {
  var raw = String(text || "").trim()
  if (!raw) return null
  try {
    var parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== "object") return null
    var bridgeIp = String(parsed.bridgeIp || "").trim()
    var username = String(parsed.username || "").trim()
    var bridgeId = String(parsed.bridgeId || "").trim().toLowerCase()
    if (!bridgeIp || !isValidIp(bridgeIp)) return null
    if (!username || !isValidId(username)) return null
    if (bridgeId && !/^[0-9a-f]{16}$/.test(bridgeId)) return null
    var apiVersion = String(parsed.apiVersion || "v1").toLowerCase()
    if (apiVersion !== "v1" && apiVersion !== "v2") apiVersion = "v1"
    return { bridgeIp: bridgeIp, username: username, bridgeId: bridgeId, apiVersion: apiVersion, entertainmentPaired: !!(parsed.entertainment && parsed.entertainment.username && parsed.entertainment.clientKey) }
  } catch (e) {
    return null
  }
}

function parseJsonObject(text) {
  if (isObject(text)) return text
  var raw = String(text || "").trim()
  if (!raw) return null
  try {
    var parsed = JSON.parse(raw)
    return parsed && typeof parsed === "object" ? parsed : null
  } catch (e) {
    return null
  }
}

function parseLights(text) {
  var obj = parseJsonObject(text)
  if (!obj) return []
  var lights = []
  for (var id in obj) {
    if (!Object.prototype.hasOwnProperty.call(obj, id)) continue
    var light = obj[id]
    var state = light.state || {}
    var hasBri = light.has_bri !== undefined ? !!light.has_bri : typeof state.bri === "number"
    var hasCt = light.has_ct !== undefined ? !!light.has_ct : typeof state.ct === "number"
    var hasColor = light.has_color !== undefined
      ? !!light.has_color
      : typeof state.hue === "number" && typeof state.sat === "number"
    var hasXy = Array.isArray(state.xy) && state.xy.length >= 2
    lights.push({
      id: String(id),
      apiId: String(light.api_id || id),
      name: String(light.name || "Light " + id),
      reachable: state.reachable !== false,
      on: !!state.on,
      bri: hasBri ? Math.max(1, Math.min(254, state.bri)) : 0,
      hasBri: hasBri,
      ct: hasCt ? Math.max(153, Math.min(500, state.ct)) : 0,
      hasCt: hasCt,
      ctMin: Number(light.ct_min || 153),
      ctMax: Number(light.ct_max || 500),
      hue: hasColor ? Number(state.hue || 0) : 0,
      sat: hasColor ? Number(state.sat || 0) : 0,
      hasColor: hasColor,
      colormode: String(state.colormode || ""),
      xy: hasXy ? [Number(state.xy[0]), Number(state.xy[1])] : [],
      gamut: light.gamut || {}
    })
  }
  lights.sort(function(a, b) { return a.name.localeCompare(b.name) })
  return lights
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function isValidResourceName(name) {
  return name === undefined || name === null || (typeof name === "string" && name.length <= maxNameLength)
}

function isValidSnapshotId(id) {
  return (typeof id === "string" || Number.isSafeInteger(id)) && isValidId(id) &&
    !Object.prototype.hasOwnProperty.call(Object.prototype, id)
}

function parseSnapshot(text) {
  if (typeof text !== "string" || text.length > maxSnapshotSize) return null
  try {
    var raw = JSON.parse(text)
    if (!isObject(raw) || !isObject(raw.lights) || !isObject(raw.groups) || !isObject(raw.scenes)) return null
    if (Object.keys(raw.lights).length > maxLights || Object.keys(raw.groups).length > maxGroups ||
        Object.keys(raw.scenes).length > maxScenes) return null
    if (raw.allControlId !== undefined && raw.allControlId !== "" && !isValidSnapshotId(raw.allControlId)) return null
    var numericKeys = ["bri", "ct", "hue", "sat"]
    for (var id in raw.lights) {
      var light = raw.lights[id]
      if (!isValidSnapshotId(id) || !isObject(light) || !isObject(light.state) || typeof light.state.on !== "boolean") return null
      if (!isValidResourceName(light.name)) return null
      var state = light.state
      for (var i = 0; i < numericKeys.length; i++) {
        var key = numericKeys[i]
        if (state[key] !== undefined && (typeof state[key] !== "number" || !isFinite(state[key]))) return null
      }
    }
    // A light can be represented by more than one room or zone delegate.
    var totalMembers = 0
    for (var groupId in raw.groups) {
      var group = raw.groups[groupId]
      if (!isValidSnapshotId(groupId) || !isObject(group) || !Array.isArray(group.lights) || !isObject(group.state)) return null
      if (!isValidResourceName(group.name) || group.lights.length > maxGroupMembers) return null
      totalMembers += group.lights.length
      if (totalMembers > maxTotalMembers) return null
      if (!group.lights.every(isValidSnapshotId)) return null
    }
    for (var sceneId in raw.scenes) {
      var scene = raw.scenes[sceneId]
      if (!isValidSnapshotId(sceneId) || !isObject(scene) || !isValidResourceName(scene.name)) return null
    }
    var lights = parseLights(raw.lights)
    var byId = {}
    for (var index = 0; index < lights.length; index++) byId[lights[index].id] = lights[index]
    return { lightsById: byId, rooms: parseGroups(raw.groups), scenes: parseScenes(raw.scenes), allControlId: String(raw.allControlId || "") }
  } catch (error) {
    return null
  }
}

function parseGroups(text) {
  var obj = parseJsonObject(text)
  if (!obj) return []
  var groups = []
  for (var id in obj) {
    if (!Object.prototype.hasOwnProperty.call(obj, id)) continue
    var group = obj[id]
    var type = String(group.type || "")
    if (type !== "Room" && type !== "Zone") continue
    groups.push({
      id: String(id),
      apiId: String(group.api_id || id),
      controlId: String(group.control_id !== undefined ? group.control_id : id),
      name: String(group.name || "Group " + id),
      type: type,
      on: !!(group.state && group.state.any_on),
      allOn: !!(group.state && group.state.all_on),
      lightIds: Array.isArray(group.lights) ? group.lights.map(String) : []
    })
  }
  groups.sort(function(a, b) { return a.name.localeCompare(b.name) })
  return groups
}

function parseScenes(text) {
  var obj = parseJsonObject(text)
  if (!obj) return []
  var scenes = []
  for (var id in obj) {
    if (!Object.prototype.hasOwnProperty.call(obj, id)) continue
    var scene = obj[id]
    scenes.push({
      id: String(scene.api_id || scene.id || id),
      name: String(scene.name || "Scene"),
      groupApiId: String(scene.group_api_id || scene.group || ""),
      active: scene.active === true
    })
  }
  return scenes
}

function roomLights(room, byId) {
  var result = []
  for (var i = 0; i < room.lightIds.length; i++) {
    var id = room.lightIds[i]
    if (Object.prototype.hasOwnProperty.call(byId, id)) result.push(byId[id])
  }
  return result
}

function clamp01(n) {
  return n < 0 ? 0 : (n > 1 ? 1 : n)
}

function componentToHex(c) {
  var s = Math.round(clamp01(c) * 255).toString(16)
  return s.length === 1 ? "0" + s : s
}

function hsvToHex(hueDeg, saturation, val) {
  var hue = (((hueDeg % 360) + 360) % 360) / 60
  var sat = clamp01(saturation)
  var value = clamp01(val)
  var sector = Math.floor(hue)
  var frac = hue - sector
  var p = value * (1 - sat)
  var q = value * (1 - sat * frac)
  var t = value * (1 - sat * (1 - frac))
  var r, g, b
  if (sector === 0) { r = value; g = t; b = p }
  else if (sector === 1) { r = q; g = value; b = p }
  else if (sector === 2) { r = p; g = value; b = t }
  else if (sector === 3) { r = p; g = q; b = value }
  else if (sector === 4) { r = t; g = p; b = value }
  else { r = value; g = p; b = q }
  return "#" + componentToHex(r) + componentToHex(g) + componentToHex(b)
}

function colorTempToRgb(kelvin, val) {
  var k = Math.max(1000, Math.min(40000, kelvin)) / 100
  var r, g, b
  if (k <= 66) {
    r = 255
    g = 99.4708025861 * Math.log(k) - 161.1195681661
    b = k <= 19 ? 0 : 138.5177312231 * Math.log(k - 10) - 305.0447927307
  } else {
    r = 329.698727446 * Math.pow(k - 60, -0.1332047592)
    g = 288.1221695283 * Math.pow(k - 60, -0.0755148492)
    b = 255
  }
  return "#" + componentToHex(r / 255 * (val === undefined ? 1 : val)) +
         componentToHex(g / 255 * (val === undefined ? 1 : val)) +
         componentToHex(b / 255 * (val === undefined ? 1 : val))
}

function xyToRgb(x, y, Y) {
  if (y <= 0) return "#000000"
  var z = 1 - x - y
  if (z <= 0) return "#000000"
  Y = Y === undefined ? 1 : Y
  // Reconstruct the full XYZ tristimulus at the bulb's relative luminance Y
  // (not chromacity-normalized) so the result shows the color as the light
  // actually appears, dimmed by brightness — instead of a max-luminance pastel.
  var X = (Y / y) * x
  var Z = (Y / y) * z
  var rl = X * 3.2404542 + Y * -1.5371385 + Z * -0.4985314
  var gl = X * -0.9692660 + Y * 1.8760108 + Z * 0.0415560
  var bl = X * 0.0556434 + Y * -0.2040259 + Z * 1.0572252
  function srgb(c) {
    var v = clamp01(c)
    return v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055
  }
  return "#" + componentToHex(srgb(rl)) +
               componentToHex(srgb(gl)) +
               componentToHex(srgb(bl))
}

function lightColor(light) {
  // A switched-off light gives a solid black row.
  if (!light.on) return "#000000"
  // Fixed reference luminance: the row shows only the bulb's chromaticity so
  // the accent stays put while the brightness slider changes. Brightness is
  // communicated by the slider position instead of the tint.
  var value = 0.5
  // The bridge always reports the bulb's CIE xy alongside hue/sat/ct, and it
  // reflects (and is updated by) every set command, so it is the ground truth
  // for the color the bulb is emitting.
  if (light.xy && light.xy.length >= 2) {
    return xyToRgb(Number(light.xy[0]), Number(light.xy[1]), value)
  }
  if (light.hasColor) return hsvToHex((Number(light.hue) / 65535) * 360, Number(light.sat) / 254, value)
  if (light.hasCt) return colorTempToRgb(1000000 / light.ct, value)
  var neutral = value
  return "#" + componentToHex(neutral) + componentToHex(neutral) + componentToHex(neutral)
}

// Representative color for a room header: the first light that is on, or the
// first light of the room when every light is off.
function roomColor(room) {
  var lights = room && Array.isArray(room.lights) ? room.lights : []
  if (lights.length === 0) return "#000000"
  var sample = lights[0]
  for (var i = 0; i < lights.length; i++) {
    if (lights[i].on) { sample = lights[i]; break }
  }
  return lightColor(sample)
}

// Number of color-capable lights in a room. Theme scenes are only offered for
// rooms with at least two of these.
function roomColorLightCount(room) {
  var lights = room && room.lights ? room.lights : []
  var n = 0
  for (var i = 0; i < lights.length; i++) {
    if (lights[i].hasColor) n++
  }
  return n
}


// Only adjacent pending scalar updates may replace each other. Toggles,
// scenes, unrelated config writes, and other properties act as barriers.
function actionReplaceKey(command) {
  if (command[2] === "write-music-options") return "music-options:" + command[3]
  if (command[2] !== "put-light" && command[2] !== "put-group") return ""
  var body = parseJsonObject(command[4])
  if (!isObject(body)) return ""
  var keys = Object.keys(body).sort().join(",")
  if (keys !== "bri" && keys !== "ct" && keys !== "hue,sat") return ""
  return command[2] + ":" + command[3] + ":" + keys
}


function connectionDiagnostic(stderr, exitCode, invalidSnapshot) {
  var code = "connection"
  if (exitCode === 124 || exitCode === 137) code = "timeout"
  else if (exitCode === 126 || exitCode === 127) code = "helper"
  else if (invalidSnapshot) code = "response"
  else {
    var payload = parseJsonObject(stderr)
    if (payload && isObject(payload.error)) code = String(payload.error.code || "request")
    else code = "request"
  }
  var labels = {
    auth: ["Authentication failed", "Re-pair the bridge to renew access."],
    tls: ["TLS connection failed", "Check bridge identity and the system clock."],
    timeout: ["Bridge timed out", "Check the bridge and network, then retry."],
    helper: ["Hue helper could not start", "Check that timeout and Python 3 are installed, then retry."],
    response: ["Invalid bridge response", "Retry; if it persists, check bridge firmware."],
    connection: ["Bridge unreachable", "Check bridge power, network, and configured IP."],
    request: ["Hue command failed", "Refresh and try again."]
  }
  if (!Object.prototype.hasOwnProperty.call(labels, code)) code = "request"
  return { code: code, label: labels[code][0], hint: labels[code][1] }
}

function roomSceneChoices(room, scenes, presets, query) {
  var choices = []
  var names = {}
  for (var i = 0; i < scenes.length; i++) {
    var scene = scenes[i]
    if (scene.groupApiId !== room.apiId) continue
    choices.push({ id: scene.id, name: scene.name, active: scene.active, preset: false })
    names[scene.name.trim().toLowerCase()] = true
  }
  choices.sort(function(a, b) {
    if (a.active !== b.active) return a.active ? -1 : 1
    return a.name.localeCompare(b.name) || a.id.localeCompare(b.id)
  })
  for (var j = 0; j < presets.length; j++) {
    var name = presets[j]
    if (!Object.prototype.hasOwnProperty.call(names, name.trim().toLowerCase())) {
      choices.push({ id: "preset:" + name, name: name, active: false, preset: true })
    }
  }
  var needle = String(query || "").trim().toLowerCase()
  return needle ? choices.filter(function(scene) { return scene.name.toLowerCase().indexOf(needle) !== -1 }) : choices
}
