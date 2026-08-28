var API = Qt.resolvedUrl("hue-api.py").toString().replace("file://", "")

function apiCmd(args) {
  var cmd = ["python3", API]
  for (var i = 0; i < args.length; i++) cmd.push(String(args[i]))
  return cmd
}

function isValidIp(ip) {
  return /^(\d{1,3}\.){3}\d{1,3}$/.test(ip)
}

function isValidId(id) {
  return /^[a-zA-Z0-9_-]{1,40}$/.test(String(id))
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
    if (bridgeId && !isValidId(bridgeId)) bridgeId = ""
    return { bridgeIp: bridgeIp, username: username, bridgeId: bridgeId }
  } catch (e) {
    return null
  }
}

function parseJsonObject(text) {
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
    var hasBri = typeof state.bri === "number"
    var hasCt = typeof state.ct === "number"
    var hasColor = typeof state.hue === "number" && typeof state.sat === "number"
    var hasXy = Array.isArray(state.xy) && state.xy.length >= 2
    lights.push({
      id: String(id),
      name: String(light.name || "Light " + id),
      on: !!state.on,
      bri: hasBri ? Math.max(1, Math.min(254, state.bri)) : 0,
      hasBri: hasBri,
      ct: hasCt ? Math.max(153, Math.min(500, state.ct)) : 0,
      hasCt: hasCt,
      hue: hasColor ? state.hue : 0,
      sat: hasColor ? state.sat : 0,
      hasColor: hasColor,
      colormode: String(state.colormode || ""),
      xy: hasXy ? [Number(state.xy[0]), Number(state.xy[1])] : [],
      pickerOpen: false
    })
  }
  lights.sort(function(a, b) { return a.name.localeCompare(b.name) })
  return lights
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

function roomLights(room, byId) {
  var result = []
  for (var i = 0; i < room.lightIds.length; i++) {
    var light = byId[room.lightIds[i]]
    if (light) result.push(light)
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
