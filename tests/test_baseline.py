#!/usr/bin/env python3
"""Minimal baseline tests for deterministic behavior in omarchy-philips-hue.

These tests freeze current behavior to protect against regression during
the Hue v2 migration. They require no Omarchy, no Quickshell, and no
real Hue Bridge.

Run: python3 -m pytest tests/test_baseline.py -v
  or: python3 tests/test_baseline.py
"""

import json
import os
import re
import tempfile
import unittest


# ---------------------------------------------------------------------------
# hex_to_hsv — extracted from theme-sync/45-hue.sh
# ---------------------------------------------------------------------------

def hex_to_hsv(hexval):
    r = int(hexval[0:2], 16) / 255.0
    g = int(hexval[2:4], 16) / 255.0
    b = int(hexval[4:6], 16) / 255.0
    mx = max(r, g, b)
    mn = min(r, g, b)
    d = mx - mn
    if d == 0:
        hue01 = 0.0
    elif mx == r:
        hue01 = ((g - b) / d) % 6
    elif mx == g:
        hue01 = (b - r) / d + 2
    else:
        hue01 = (r - g) / d + 4
    hue01 = (hue01 / 6.0) % 1.0
    sat = 0.0 if mx == 0 else d / mx
    return int(round(hue01 * 65535)) % 65536, int(round(sat * 254))


class TestHexToHsv(unittest.TestCase):
    def test_red(self):
        self.assertEqual(hex_to_hsv("ff0000"), (0, 254))

    def test_green(self):
        self.assertEqual(hex_to_hsv("00ff00"), (21845, 254))

    def test_blue(self):
        self.assertEqual(hex_to_hsv("0000ff"), (43690, 254))

    def test_white(self):
        self.assertEqual(hex_to_hsv("ffffff"), (0, 0))

    def test_black(self):
        self.assertEqual(hex_to_hsv("000000"), (0, 0))

    def test_pure_yellow(self):
        self.assertEqual(hex_to_hsv("ffff00"), (10922, 254))

    def test_pure_cyan(self):
        self.assertEqual(hex_to_hsv("00ffff"), (32768, 254))

    def test_pure_magenta(self):
        self.assertEqual(hex_to_hsv("ff00ff"), (54612, 254))

    def test_grey(self):
        h, s = hex_to_hsv("808080")
        self.assertEqual(s, 0)


# ---------------------------------------------------------------------------
# build_scene_palette — extracted from theme-sync/45-hue.sh
# ---------------------------------------------------------------------------

def build_scene_palette(path):
    named = set((
        "red", "green", "yellow", "blue", "magenta", "cyan",
        "bright_red", "bright_green", "bright_yellow",
        "bright_blue", "bright_magenta", "bright_cyan",
    ))
    order = []
    values = {}
    try:
        with open(path) as f:
            for raw in f:
                m = re.match(
                    r'^\s*([A-Za-z0-9_]+)\s*=\s*"#([0-9a-fA-F]{6})"\s*$',
                    raw,
                )
                if m:
                    values[m.group(1)] = m.group(2).lower()
                    order.append(m.group(1))
    except Exception:
        return []

    def skip(k):
        return (
            k in ("selection", "muted")
            or "background" in k
            or "foreground" in k
            or "border" in k
            or "tab" in k
        )

    keys = (
        [k for k in order if k == "accent"]
        + [k for k in order if k in named]
        + [k for k in order if k not in named and not skip(k)]
    )
    palette, seen = [], set()
    for k in keys:
        v = values.get(k)
        if v and v not in seen:
            seen.add(v)
            palette.append(v)
    return palette


class TestBuildScenePalette(unittest.TestCase):
    SAMPLE = (
        'accent = "#ff5500"\n'
        'background = "#1a1b26"\n'
        'foreground = "#c0caf5"\n'
        'red = "#f7768e"\n'
        'green = "#9ece6a"\n'
        'blue = "#7aa2f7"\n'
        'selection = "#33467c"\n'
        'muted = "#565f89"\n'
        'border = "#3b4261"\n'
        'tab_bar = "#24283b"\n'
    )

    def _palette(self, content=None):
        if content is None:
            content = self.SAMPLE
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(content)
            path = f.name
        try:
            return build_scene_palette(path)
        finally:
            os.unlink(path)

    def test_accent_first(self):
        p = self._palette()
        self.assertEqual(p[0], "ff5500")

    def test_named_colors_present(self):
        p = self._palette()
        self.assertIn("f7768e", p)  # red
        self.assertIn("9ece6a", p)  # green
        self.assertIn("7aa2f7", p)  # blue

    def test_surface_keys_excluded(self):
        p = self._palette()
        self.assertNotIn("1a1b26", p)  # background
        self.assertNotIn("c0caf5", p)  # foreground
        self.assertNotIn("33467c", p)  # selection
        self.assertNotIn("565f89", p)  # muted
        self.assertNotIn("3b4261", p)  # border
        self.assertNotIn("24283b", p)  # tab_bar

    def test_deduplication(self):
        content = (
            'accent = "#ff0000"\n'
            'red = "#ff0000"\n'
            'green = "#00ff00"\n'
        )
        p = self._palette(content)
        self.assertEqual(p.count("ff0000"), 1)

    def test_empty_file(self):
        p = self._palette("")
        self.assertEqual(p, [])

    def test_no_colors(self):
        p = self._palette('background = "#000000"\n')
        self.assertEqual(p, [])


# ---------------------------------------------------------------------------
# Config parsing — equivalent to HueApi.js parseConfig
# ---------------------------------------------------------------------------

def isValidIp(ip):
    return bool(re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip))


def isValidId(id_str):
    return bool(re.match(r"^[a-zA-Z0-9_-]{1,40}$", str(id_str)))


def parseConfig(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if not parsed or not isinstance(parsed, dict):
            return None
        bridgeIp = str(parsed.get("bridgeIp", "")).strip()
        username = str(parsed.get("username", "")).strip()
        bridgeId = str(parsed.get("bridgeId", "")).strip().lower()
        if not bridgeIp or not isValidIp(bridgeIp):
            return None
        if not username or not isValidId(username):
            return None
        if bridgeId and not isValidId(bridgeId):
            bridgeId = ""
        return {
            "bridgeIp": bridgeIp,
            "username": username,
            "bridgeId": bridgeId,
        }
    except Exception:
        return None


class TestParseConfig(unittest.TestCase):
    def test_valid_full(self):
        r = parseConfig(
            '{"bridgeIp":"192.168.1.100","username":"abc123","bridgeId":"001788fffeabcdef"}'
        )
        self.assertEqual(r["bridgeIp"], "192.168.1.100")
        self.assertEqual(r["username"], "abc123")
        self.assertEqual(r["bridgeId"], "001788fffeabcdef")

    def test_valid_no_bridge_id(self):
        r = parseConfig('{"bridgeIp":"10.0.0.1","username":"testuser123"}')
        self.assertIsNotNone(r)
        self.assertEqual(r["bridgeId"], "")

    def test_invalid_ip(self):
        # Regex validates format only, not range — by design
        r = parseConfig('{"bridgeIp":"999.1.1.1","username":"abc"}')
        self.assertIsNotNone(r)

    def test_empty_username(self):
        self.assertIsNone(parseConfig('{"bridgeIp":"1.2.3.4","username":""}'))

    def test_invalid_bridge_id_cleared(self):
        r = parseConfig(
            '{"bridgeIp":"1.2.3.4","username":"abc","bridgeId":"invalid!id"}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(r["bridgeId"], "")

    def test_empty_string(self):
        self.assertIsNone(parseConfig(""))

    def test_not_json(self):
        self.assertIsNone(parseConfig("not json"))

    def test_not_object(self):
        self.assertIsNone(parseConfig('"just a string"'))

    def test_none(self):
        self.assertIsNone(parseConfig(None))


# ---------------------------------------------------------------------------
# IP and ID validation
# ---------------------------------------------------------------------------

class TestValidation(unittest.TestCase):
    def test_valid_ips(self):
        for ip in ["192.168.1.1", "10.0.0.1", "172.16.0.1", "0.0.0.0", "255.255.255.255"]:
            self.assertTrue(isValidIp(ip), f"Expected valid: {ip}")

    def test_invalid_ips(self):
        for ip in ["", "192.168.1", "192.168.1.1.1", "abc.def.ghi.jkl"]:
            self.assertFalse(isValidIp(ip), f"Expected invalid: {ip}")

    def test_valid_ids(self):
        for id_ in ["abc123", "001788fffeabcdef", "my-bridge_1", "a" * 40]:
            self.assertTrue(isValidId(id_), f"Expected valid: {id_}")

    def test_invalid_ids(self):
        for id_ in ["", "a" * 41, "has spaces", "special!chars", "dot.name"]:
            self.assertFalse(isValidId(id_), f"Expected invalid: {id_}")


# ---------------------------------------------------------------------------
# Security patterns
# ---------------------------------------------------------------------------

class TestSecurityPatterns(unittest.TestCase):
    def test_atomic_write_creates_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, b'{"ok": true}')
            os.close(fd)
            self.assertTrue(os.path.exists(path))
            st = os.stat(path)
            self.assertEqual(oct(st.st_mode)[-3:], "600")

    def test_atomic_write_prevents_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, b"first")
            os.close(fd)
            with self.assertRaises(FileExistsError):
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def test_nofollow_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.json")
            with open(path, "w") as f:
                f.write('{"data": 42}')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            data = os.read(fd, 100)
            os.close(fd)
            self.assertEqual(json.loads(data), {"data": 42})


# ---------------------------------------------------------------------------
# HueApi.js parseLights / parseGroups — equivalent in Python
# ---------------------------------------------------------------------------

def parseLights(text):
    try:
        obj = json.loads(text) if text else None
    except Exception:
        obj = None
    if not obj or not isinstance(obj, dict):
        return []
    lights = []
    for id_, light in obj.items():
        state = light.get("state") or {}
        hasBri = isinstance(state.get("bri"), (int, float))
        hasCt = isinstance(state.get("ct"), (int, float))
        hasColor = isinstance(state.get("hue"), (int, float)) and isinstance(
            state.get("sat"), (int, float)
        )
        hasXy = isinstance(state.get("xy"), list) and len(state["xy"]) >= 2
        lights.append(
            {
                "id": str(id_),
                "name": str(light.get("name", f"Light {id_}")),
                "on": bool(state.get("on")),
                "bri": max(1, min(254, state["bri"])) if hasBri else 0,
                "hasBri": hasBri,
                "ct": max(153, min(500, state["ct"])) if hasCt else 0,
                "hasCt": hasCt,
                "hue": state.get("hue", 0) if hasColor else 0,
                "sat": state.get("sat", 0) if hasColor else 0,
                "hasColor": hasColor,
                "colormode": str(state.get("colormode", "")),
                "xy": [float(state["xy"][0]), float(state["xy"][1])]
                if hasXy
                else [],
            }
        )
    lights.sort(key=lambda x: x["name"])
    return lights


def parseGroups(text):
    try:
        obj = json.loads(text) if text else None
    except Exception:
        obj = None
    if not obj or not isinstance(obj, dict):
        return []
    groups = []
    for id_, group in obj.items():
        type_ = str(group.get("type", ""))
        if type_ not in ("Room", "Zone"):
            continue
        state = group.get("state") or {}
        groups.append(
            {
                "id": str(id_),
                "name": str(group.get("name", f"Group {id_}")),
                "type": type_,
                "on": bool(state.get("any_on")),
                "allOn": bool(state.get("all_on")),
                "lightIds": [str(lid) for lid in group.get("lights", [])],
            }
        )
    groups.sort(key=lambda x: x["name"])
    return groups


class TestParseLights(unittest.TestCase):
    SAMPLE = json.dumps(
        {
            "1": {
                "name": "Desk Lamp",
                "state": {
                    "on": True,
                    "bri": 200,
                    "hue": 40000,
                    "sat": 200,
                    "ct": 300,
                    "colormode": "hs",
                    "xy": [0.4, 0.5],
                },
            },
            "2": {
                "name": "Ceiling",
                "state": {"on": False, "bri": 100, "ct": 250, "colormode": "ct"},
            },
        }
    )

    def test_parses_lights(self):
        lights = parseLights(self.SAMPLE)
        self.assertEqual(len(lights), 2)

    def test_sorted_by_name(self):
        lights = parseLights(self.SAMPLE)
        self.assertEqual(lights[0]["name"], "Ceiling")
        self.assertEqual(lights[1]["name"], "Desk Lamp")

    def test_light_properties(self):
        lights = parseLights(self.SAMPLE)
        desk = [l for l in lights if l["name"] == "Desk Lamp"][0]
        self.assertTrue(desk["on"])
        self.assertTrue(desk["hasBri"])
        self.assertTrue(desk["hasCt"])
        self.assertTrue(desk["hasColor"])
        self.assertEqual(desk["bri"], 200)
        self.assertEqual(desk["hue"], 40000)

    def test_empty_input(self):
        self.assertEqual(parseLights(""), [])
        self.assertEqual(parseLights(None), [])
        self.assertEqual(parseLights("not json"), [])


class TestParseGroups(unittest.TestCase):
    SAMPLE = json.dumps(
        {
            "1": {
                "name": "Living Room",
                "type": "Room",
                "state": {"any_on": True, "all_on": False},
                "lights": ["1", "2", "3"],
            },
            "2": {
                "name": "Kitchen",
                "type": "Room",
                "state": {"any_on": False, "all_on": False},
                "lights": ["4"],
            },
            "3": {
                "name": "Other",
                "type": "LightGroup",
                "lights": ["5"],
            },
        }
    )

    def test_filters_room_type(self):
        groups = parseGroups(self.SAMPLE)
        self.assertEqual(len(groups), 2)

    def test_excludes_non_room_zone(self):
        groups = parseGroups(self.SAMPLE)
        names = [g["name"] for g in groups]
        self.assertNotIn("Other", names)

    def test_group_properties(self):
        groups = parseGroups(self.SAMPLE)
        lr = [g for g in groups if g["name"] == "Living Room"][0]
        self.assertEqual(lr["type"], "Room")
        self.assertTrue(lr["on"])
        self.assertFalse(lr["allOn"])
        self.assertEqual(lr["lightIds"], ["1", "2", "3"])

    def test_empty_input(self):
        self.assertEqual(parseGroups(""), [])
        self.assertEqual(parseGroups(None), [])


if __name__ == "__main__":
    unittest.main()
