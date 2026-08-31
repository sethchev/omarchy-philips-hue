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

function xyToHueSat(x, y) {
  if (typeof x !== "number" || typeof y !== "number" || y <= 0) return [0, 0]
  var z = 1 - x - y
  if (z <= 0) return [0, 0]
  var X = x / y
  var Z = z / y
  // The picker is an sRGB HSV wheel. Hue gamuts can exceed sRGB, so its marker
  // is the nearest displayable sRGB color rather than an exact inverse.
  var gamma = function(value) {
    value = clamp01(value)
    return value <= 0.0031308 ? 12.92 * value : 1.055 * Math.pow(value, 1 / 2.4) - 0.055
  }
  var r = gamma(X * 3.2404542 - 1.5371385 - Z * 0.4985314)
  var g = gamma(X * -0.9692660 + 1.8760108 + Z * 0.0415560)
  var b = gamma(X * 0.0556434 - 0.2040259 + Z * 1.0572252)
  var max = Math.max(r, g, b)
  var min = Math.min(r, g, b)
  var d = max - min
  var hue = 0
  if (d !== 0) {
    if (max === r) hue = ((g - b) / d) % 6
    else if (max === g) hue = (b - r) / d + 2
    else hue = (r - g) / d + 4
    hue = (hue / 6 + 1) % 1
  }
  return [Math.round(hue * 65535), Math.round((max === 0 ? 0 : d / max) * 254)]
}

function parseLightsV2(text) {
  var arr = parseJsonObject(text)
  if (!arr || !Array.isArray(arr)) return []
  var lights = []
  for (var i = 0; i < arr.length; i++) {
    var r = arr[i]
    if (!r || typeof r !== "object") continue
    var type = String(r.type || "")
    if (type !== "light") continue
    var onState = r.on || {}
    var dimming = r.dimming || {}
    var ct = r.color_temperature || {}
    var color = r.color || {}
    var xy = color.xy || {}
    var brightness = typeof dimming.brightness === "number" ? dimming.brightness : 0
    var mirek = ct.mirek
    var mirekValid = !!ct.mirek_valid
    var x = typeof xy.x === "number" ? xy.x : null
    var y = typeof xy.y === "number" ? xy.y : null
    var hasCt = mirekValid && typeof mirek === "number"
    var hasXy = x !== null && y !== null
    var bri = brightness > 0 ? Math.max(1, Math.min(254, Math.round((brightness / 100) * 254))) : 0
    var hueSat = hasXy ? xyToHueSat(x, y) : [0, 0]
    lights.push({
      id: String(r.id || ""),
      name: String((r.metadata || {}).name || "Light"),
      on: !!onState.on,
      bri: bri,
      hasBri: true,
      ct: hasCt ? Math.max(153, Math.min(500, Math.round(mirek))) : 0,
      hasCt: hasCt,
      hue: hueSat[0],
      sat: hueSat[1],
      hasColor: hasXy,
      colormode: hasCt && !hasXy ? "ct" : "hs",
      xy: hasXy ? [Number(x), Number(y)] : [],
      pickerOpen: false
    })
  }
  lights.sort(function(a, b) { return a.name.localeCompare(b.name) })
  return lights
}

function parseGroupsV2(roomsText, zonesText, devicesText, groupedLightsText) {
  var roomsArr = parseJsonObject(roomsText)
  var zonesArr = parseJsonObject(zonesText)
  var devicesArr = parseJsonObject(devicesText)
  var glArr = parseJsonObject(groupedLightsText)
  if (!roomsArr || !Array.isArray(roomsArr) || !zonesArr || !Array.isArray(zonesArr)) return []

  var deviceToLight = {}
  if (devicesArr && Array.isArray(devicesArr)) {
    for (var d = 0; d < devicesArr.length; d++) {
      var dev = devicesArr[d]
      if (!dev || typeof dev !== "object") continue
      var devId = String(dev.id || "")
      var services = dev.services || []
      if (!Array.isArray(services)) continue
      for (var s = 0; s < services.length; s++) {
        var svc = services[s]
        if (svc && svc.rtype === "light" && typeof svc.rid === "string") {
          deviceToLight[devId] = svc.rid
          break
        }
      }
    }
  }

  var glState = {}
  if (glArr && Array.isArray(glArr)) {
    for (var g = 0; g < glArr.length; g++) {
      var gl = glArr[g]
      if (!gl || typeof gl !== "object") continue
      var glId = String(gl.id || "")
      var onState = gl.on || {}
      var dim = gl.dimming || {}
      glState[glId] = {
        on: !!onState.on,
        brightness: typeof dim.brightness === "number" ? dim.brightness : 0
      }
    }
  }

  var groups = []
  var resources = roomsArr.concat(zonesArr)
  for (var r = 0; r < resources.length; r++) {
    var resource = resources[r]
    if (!resource || typeof resource !== "object") continue
    var resourceType = String(resource.type || "")
    if (resourceType !== "room" && resourceType !== "zone") continue
    var resourceId = String(resource.id || "")
    var meta = resource.metadata || {}
    var children = resource.children || []
    var services = resource.services || []
    var glUuid = ""
    for (var si = 0; si < services.length; si++) {
      var svc = services[si]
      if (svc && svc.rtype === "grouped_light" && typeof svc.rid === "string") {
        glUuid = svc.rid
        break
      }
    }
    var lightIds = []
    if (Array.isArray(children)) {
      for (var c = 0; c < children.length; c++) {
        var child = children[c]
        if (child && child.rtype === "device" && typeof child.rid === "string") {
          var lightUuid = deviceToLight[child.rid]
          if (lightUuid && lightIds.indexOf(lightUuid) === -1) lightIds.push(lightUuid)
        } else if (child && child.rtype === "light" && typeof child.rid === "string") {
          if (lightIds.indexOf(child.rid) === -1) lightIds.push(child.rid)
        }
      }
    }
    var state = glState[glUuid] || { on: false, brightness: 0 }
    groups.push({
      id: resourceId,
      name: String(meta.name || (resourceType === "zone" ? "Zone" : "Room")),
      type: resourceType === "zone" ? "Zone" : "Room",
      on: state.on,
      allOn: false,
      lightIds: lightIds,
      groupedLightId: glUuid
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
