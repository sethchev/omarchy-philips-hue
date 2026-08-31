#!/usr/bin/env python3
"""Hue API v2 transformation tests.

Tests the production v2 helpers in hue-api.py. Also includes test-only
v1 helpers for v1↔v2 comparison tests.

Run: python3 tests/test_v2_transforms.py -v
"""

import importlib.util
import json
import os
import re
import sys
import unittest
import socket
import ssl
import tempfile
import subprocess
import urllib.error
from unittest.mock import patch

# Import hue-api.py (hyphenated name requires importlib)
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("hue_api", os.path.join(_root, "hue-api.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

brightness_to_v2 = _mod.brightness_to_v2
brightness_from_v2 = _mod.brightness_from_v2
on_payload_v2 = _mod.on_payload_v2
brightness_payload_v2 = _mod.brightness_payload_v2
color_payload_v2_xy = _mod.color_payload_v2_xy
color_payload_v2_hs = _mod.color_payload_v2_hs
ct_payload_v2 = _mod.ct_payload_v2
extract_grouped_light_uuid = _mod.extract_grouped_light_uuid
parse_v2_envelope = _mod.parse_v2_envelope
parse_light_v2 = _mod.parse_light_v2
parse_room_v2 = _mod.parse_room_v2
_convert_control_payload_to_v2 = _mod._convert_control_payload_to_v2
v2_put_resource = _mod.v2_put_resource
v2_get_data = _mod.v2_get_data
request_error_exit_code = _mod.request_error_exit_code
_v2_url = _mod._v2_url
_panel_source = open(os.path.join(_root, "Panel.qml")).read()
_hue_js = os.path.join(_root, "HueApi.js")
_pair_source = open(os.path.join(_root, "pair.sh")).read()


def parse_lights_via_production_js(resources):
    script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/^var API =.*\n/, "");
const context = { Math: Math, JSON: JSON };
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify(context.parseLightsV2(process.argv[2])));
'''
    result = subprocess.run(
        ["node", "-e", script, _hue_js, json.dumps(resources)],
        text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def parse_groups_via_production_js(rooms, zones, devices, grouped_lights):
    script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/^var API =.*\n/, "");
const context = { Math: Math, JSON: JSON };
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify(context.parseGroupsV2(process.argv[2], process.argv[3], process.argv[4], process.argv[5])));
'''
    result = subprocess.run(
        ["node", "-e", script, _hue_js, json.dumps(rooms), json.dumps(zones),
         json.dumps(devices), json.dumps(grouped_lights)],
        text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Test-only v1 helpers (NOT in production — used for comparison tests)
# ---------------------------------------------------------------------------

UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def is_uuid(s):
    return bool(UUID_RE.match(str(s))) if s else False


def on_payload_v1(on):
    return {"on": bool(on)}


def brightness_payload_v1(bri):
    return {"bri": max(1, min(254, int(bri)))}


def color_payload_v1(hue, sat):
    return {"hue": int(hue), "sat": int(sat)}


def ct_payload_v1(ct):
    return {"ct": max(153, min(500, int(ct)))}


def scene_recall_payload(action="active"):
    valid_actions = ("active", "dynamic_palette", "static")
    if action not in valid_actions:
        raise ValueError(f"invalid action: {action!r}, must be one of {valid_actions}")
    return {"recall": {"action": action}}


def build_room_light_map(rooms):
    result = {}
    if not isinstance(rooms, list):
        return result
    for room in rooms:
        room_id = room.get("id") if isinstance(room, dict) else None
        gl_uuid = extract_grouped_light_uuid(room)
        if room_id and gl_uuid:
            result[room_id] = gl_uuid
    return result


# ===========================================================================
# Bridge Error Classification
# ===========================================================================

class TestBridgeErrorClassification(unittest.TestCase):
    def test_http_auth_errors_are_not_transport_errors(self):
        for code in (401, 403):
            error = urllib.error.HTTPError("https://bridge", code, "", {}, None)
            self.assertEqual(request_error_exit_code(error), 2)
        error = urllib.error.HTTPError("https://bridge", 500, "", {}, None)
        self.assertEqual(request_error_exit_code(error), 6)

    def test_transport_error_classes(self):
        self.assertEqual(request_error_exit_code(ssl.SSLError()), 3)
        self.assertEqual(request_error_exit_code(socket.gaierror()), 4)
        self.assertEqual(request_error_exit_code(socket.timeout()), 5)


class TestCredentialPathSafety(unittest.TestCase):
    def test_state_home_honors_xdg_state_home(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/hue-state"}, clear=False):
            self.assertEqual(_mod.state_home(), "/tmp/hue-state")

    def test_credentials_reject_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.join(directory, "real.json")
            link = os.path.join(directory, "hue.json")
            with open(real, "w") as handle:
                json.dump({"username": "secret"}, handle)
            os.symlink(real, link)
            with patch.object(_mod, "CREDS_FILE", link):
                with self.assertRaises(OSError):
                    _mod._load_creds()

    def test_credentials_repair_broad_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "hue.json")
            with open(path, "w") as handle:
                json.dump({"bridgeIp": "192.0.2.1", "username": "secret"}, handle)
            os.chmod(path, 0o644)
            with patch.object(_mod, "CREDS_FILE", path):
                self.assertEqual(_mod._load_creds()["username"], "secret")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_credentials_reject_non_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(_mod, "CREDS_FILE", directory):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    _mod._load_creds()

    def test_bridge_target_validation(self):
        creds = {
            "bridgeIp": "192.0.2.1",
            "bridgeId": "001788fffeabcdef",
            "username": "secret",
        }
        url, host, ip, key = _v2_url(creds, "/clip/v2/resource/light")
        self.assertEqual(url, "https://001788fffeabcdef/clip/v2/resource/light")
        self.assertEqual((host, ip, key), ("001788fffeabcdef", "192.0.2.1", "secret"))
        for bridge_ip in ("https://bridge", "192.0.2.1/path", "999.0.0.1", "host"):
            with self.assertRaisesRegex(ValueError, "bridge IP"):
                _v2_url({**creds, "bridgeIp": bridge_ip}, "/")
        for bridge_id in ("bridge/path", "001788fffeabcdef?x", "not-a-bridge"):
            with self.assertRaisesRegex(ValueError, "bridge ID"):
                _v2_url({**creds, "bridgeId": bridge_id}, "/")

    def test_pairing_does_not_log_the_application_key(self):
        self.assertNotIn("Got username", _pair_source)
        self.assertNotIn("username: ${", _pair_source)


class TestPanelV2Polling(unittest.TestCase):
    class Lifecycle:
        def __init__(self):
            self.active = None
            self.next_generation = 0
            self.pending = 0
            self.queued = False

        def refresh(self):
            if self.active is not None:
                self.queued = True
                return None
            self.next_generation += 1
            self.active = self.next_generation
            self.pending = 5
            return self.active

        def finished(self, generation):
            if generation != self.active:
                return False
            self.pending -= 1
            if self.pending == 0:
                self.active = None
                if self.queued:
                    self.queued = False
                    self.refresh()
            return True

    def test_stale_callback_cannot_mutate_the_queued_generation(self):
        poll = self.Lifecycle()
        first = poll.refresh()
        poll.refresh()
        for _ in range(5):
            self.assertTrue(poll.finished(first))
        self.assertEqual(poll.active, 2)
        self.assertEqual(poll.pending, 5)
        self.assertFalse(poll.finished(first))
        self.assertEqual(poll.pending, 5)

    def test_production_polling_coalesces_while_five_resources_are_active(self):
        self.assertIn("root.pendingFetches > 0 || root.fetchStarting", _panel_source)
        self.assertNotIn("lightsProc.running = false", _panel_source)
        self.assertEqual(_panel_source.count("root.finishFetch("), 5)

    def test_write_failures_remain_visible(self):
        self.assertIn("property string actionErrorText", _panel_source)
        self.assertIn("Update failed. Refresh and try again.", _panel_source)


class TestProductionHueApiJs(unittest.TestCase):
    def test_xy_marker_uses_the_production_light_parser(self):
        lights = parse_lights_via_production_js([{
            "id": "red", "type": "light", "metadata": {"name": "Red"},
            "color": {"xy": {"x": 0.64, "y": 0.33}},
        }, {
            "id": "green", "type": "light", "metadata": {"name": "Green"},
            "color": {"xy": {"x": 0.30, "y": 0.60}},
        }])
        by_id = {light["id"]: light for light in lights}
        self.assertLess(by_id["red"]["hue"], 1000)
        self.assertGreater(by_id["red"]["sat"], 250)
        self.assertGreater(by_id["green"]["hue"], 18000)
        self.assertLess(by_id["green"]["hue"], 26000)
        self.assertGreater(by_id["green"]["sat"], 250)

    def test_xy_marker_clamps_to_the_picker_display_gamut(self):
        light = parse_lights_via_production_js([{
            "id": "wide-gamut", "type": "light",
            "color": {"xy": {"x": 0.70, "y": 0.29}},
        }])[0]
        self.assertGreaterEqual(light["hue"], 0)
        self.assertLessEqual(light["hue"], 65535)
        self.assertGreaterEqual(light["sat"], 0)
        self.assertLessEqual(light["sat"], 254)

    def test_room_zone_overlap_retains_both_native_groups(self):
        groups = parse_groups_via_production_js(
            [{"id": "room", "type": "room", "metadata": {"name": "Room"},
              "children": [{"rtype": "device", "rid": "device"}],
              "services": [{"rtype": "grouped_light", "rid": "room-gl"}]}],
            [{"id": "zone", "type": "zone", "metadata": {"name": "Zone"},
              "children": [{"rtype": "light", "rid": "light"}],
              "services": [{"rtype": "grouped_light", "rid": "zone-gl"}]}],
            [{"id": "device", "services": [{"rtype": "light", "rid": "light"}]}],
            [{"id": "room-gl", "on": {"on": True}}, {"id": "zone-gl", "on": {"on": True}}])
        self.assertEqual([group["type"] for group in groups], ["Room", "Zone"])
        self.assertTrue(all(group["lightIds"] == ["light"] for group in groups))

    def test_room_toggle_updates_every_displayed_copy_of_an_overlapping_light(self):
        groups = [
            {"id": "room", "on": False, "lights": [{"id": "light", "on": False}]},
            {"id": "zone", "on": False, "lights": [{"id": "light", "on": False}]},
        ]
        affected = {light["id"] for group in groups if group["id"] == "room"
                    for light in group["lights"]}
        updated = []
        for group in groups:
            lights = [{**light, "on": True} if light["id"] in affected else light
                      for light in group["lights"]]
            updated.append({**group, "on": any(light["on"] for light in lights), "lights": lights})
        self.assertTrue(all(group["lights"][0]["on"] for group in updated))
        self.assertTrue(all(group["on"] for group in updated))
        self.assertIn("affected[light.id] ? root.lightCopy(light, on) : light", _panel_source)
        self.assertIn("var roomOn = room.id === roomId ? on : room.on", _panel_source)


# ===========================================================================
# V2 Envelope Parsing
# ===========================================================================

class TestV2EnvelopeParsing(unittest.TestCase):
    def test_valid_envelope(self):
        resp = '{"errors":[],"data":[{"id":"abc","type":"light"}]}'
        data = parse_v2_envelope(resp)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "abc")

    def test_empty_data(self):
        resp = '{"errors":[],"data":[]}'
        data = parse_v2_envelope(resp)
        self.assertEqual(data, [])

    def test_multiple_resources(self):
        resp = '{"errors":[],"data":[{"id":"1"},{"id":"2"},{"id":"3"}]}'
        data = parse_v2_envelope(resp)
        self.assertEqual(len(data), 3)

    def test_errors_present(self):
        resp = '{"errors":[{"description":"not found"}],"data":[]}'
        with self.assertRaisesRegex(ValueError, "returned 1 error"):
            parse_v2_envelope(resp)

    def test_transport_rejects_v2_error_envelopes_for_reads_and_writes(self):
        with patch.object(_mod, "_v2_get", return_value={"errors": [], "data": ["ok"]}):
            self.assertEqual(v2_get_data({}, "light"), ["ok"])
        with patch.object(_mod, "_v2_get", return_value={"errors": [{}], "data": []}):
            with self.assertRaises(ValueError):
                v2_get_data({}, "light")
        with patch.object(_mod, "_v2_put", return_value={"errors": [{}], "data": []}):
            with self.assertRaises(ValueError):
                v2_put_resource({}, "light", "3e40dfd6-abcd-1234-5678-abcdef012345", {})

    def test_empty_string(self):
        with self.assertRaises(ValueError):
            parse_v2_envelope("")

    def test_none(self):
        with self.assertRaises(ValueError):
            parse_v2_envelope(None)

    def test_not_json(self):
        with self.assertRaises(ValueError):
            parse_v2_envelope("not json")

    def test_not_object(self):
        with self.assertRaises(ValueError):
            parse_v2_envelope('"string"')

    def test_missing_data_key(self):
        with self.assertRaises(ValueError):
            parse_v2_envelope('{"errors":[]}')

    def test_data_not_array(self):
        with self.assertRaises(ValueError):
            parse_v2_envelope('{"errors":[],"data":"not array"}')


# ===========================================================================
# Brightness Conversion
# ===========================================================================

class TestBrightnessConversion(unittest.TestCase):
    def test_v1_min(self):
        self.assertAlmostEqual(brightness_to_v2(1), 0.39, places=1)

    def test_v1_max(self):
        self.assertAlmostEqual(brightness_to_v2(254), 100.0, places=1)

    def test_v1_mid(self):
        self.assertAlmostEqual(brightness_to_v2(127), 50.0, delta=1.0)

    def test_v2_min(self):
        self.assertEqual(brightness_from_v2(0.0), 1)

    def test_v2_max(self):
        self.assertEqual(brightness_from_v2(100.0), 254)

    def test_v2_mid(self):
        self.assertAlmostEqual(brightness_from_v2(50.0), 127, delta=1)

    def test_v1_below_min_clamped(self):
        self.assertAlmostEqual(brightness_to_v2(0), 0.39, places=1)

    def test_v1_above_max_clamped(self):
        self.assertAlmostEqual(brightness_to_v2(300), 100.0, places=1)

    def test_v2_below_min_clamped(self):
        self.assertEqual(brightness_from_v2(-10.0), 1)

    def test_v2_above_max_clamped(self):
        self.assertEqual(brightness_from_v2(150.0), 254)

    def test_roundtrip_approximate(self):
        for bri in [1, 50, 127, 200, 254]:
            v2 = brightness_to_v2(bri)
            back = brightness_from_v2(v2)
            self.assertAlmostEqual(back, bri, delta=2,
                msg=f"roundtrip failed for {bri}: got {back}")

    def test_known_bridge_value(self):
        bri = brightness_from_v2(72.73)
        self.assertGreaterEqual(bri, 180)
        self.assertLessEqual(bri, 190)


# ===========================================================================
# On/Off Payload Construction
# ===========================================================================

class TestOnOffPayload(unittest.TestCase):
    def test_v1_on(self):
        self.assertEqual(on_payload_v1(True), {"on": True})

    def test_v1_off(self):
        self.assertEqual(on_payload_v1(False), {"on": False})

    def test_v2_on(self):
        self.assertEqual(on_payload_v2(True), {"on": {"on": True}})

    def test_v2_off(self):
        self.assertEqual(on_payload_v2(False), {"on": {"on": False}})

    def test_v2_toggle_from_v1_state(self):
        v1_state = {"on": True}
        new_state = not v1_state["on"]
        self.assertEqual(on_payload_v2(new_state), {"on": {"on": False}})


# ===========================================================================
# Brightness Payload Construction
# ===========================================================================

class TestBrightnessPayload(unittest.TestCase):
    def test_v1_payload(self):
        self.assertEqual(brightness_payload_v1(200), {"bri": 200})

    def test_v1_clamped(self):
        self.assertEqual(brightness_payload_v1(0), {"bri": 1})
        self.assertEqual(brightness_payload_v1(300), {"bri": 254})

    def test_v2_payload(self):
        result = brightness_payload_v2(200)
        self.assertIn("dimming", result)
        self.assertAlmostEqual(result["dimming"]["brightness"], 78.74, delta=1.0)

    def test_v2_payload_min(self):
        result = brightness_payload_v2(1)
        self.assertAlmostEqual(result["dimming"]["brightness"], 0.39, delta=1.0)

    def test_v2_payload_max(self):
        result = brightness_payload_v2(254)
        self.assertAlmostEqual(result["dimming"]["brightness"], 100.0, delta=1.0)


# ===========================================================================
# Color Payload Construction
# ===========================================================================

class TestColorPayload(unittest.TestCase):
    def test_v1_red(self):
        self.assertEqual(color_payload_v1(0, 254), {"hue": 0, "sat": 254})

    def test_v2_xy_passthrough(self):
        result = color_payload_v2_xy(0.5013, 0.4152)
        self.assertEqual(result, {"color": {"xy": {"x": 0.5013, "y": 0.4152}}})

    def test_v2_from_hs_red(self):
        result = color_payload_v2_hs(0, 254)
        xy = result["color"]["xy"]
        self.assertAlmostEqual(xy["x"], 0.675, delta=0.05)
        self.assertAlmostEqual(xy["y"], 0.322, delta=0.05)

    def test_v2_from_hs_green(self):
        result = color_payload_v2_hs(21845, 254)
        xy = result["color"]["xy"]
        self.assertGreater(xy["x"], 0.1)
        self.assertLess(xy["x"], 0.4)
        self.assertGreater(xy["y"], 0.5)

    def test_v2_from_hs_blue(self):
        result = color_payload_v2_hs(43690, 254)
        xy = result["color"]["xy"]
        self.assertGreater(xy["x"], 0.05)
        self.assertLess(xy["x"], 0.3)
        self.assertGreater(xy["y"], 0.0)
        self.assertLess(xy["y"], 0.3)

    def test_zero_saturation(self):
        result = color_payload_v2_hs(0, 0)
        xy = result["color"]["xy"]
        self.assertAlmostEqual(xy["x"], 0.33, delta=0.1)
        self.assertAlmostEqual(xy["y"], 0.33, delta=0.1)


# ===========================================================================
# Color Temperature Payload
# ===========================================================================

class TestColorTemperaturePayload(unittest.TestCase):
    def test_v1_warm(self):
        self.assertEqual(ct_payload_v1(153), {"ct": 153})

    def test_v1_cool(self):
        self.assertEqual(ct_payload_v1(500), {"ct": 500})

    def test_v2_warm(self):
        self.assertEqual(ct_payload_v2(153),
                         {"color_temperature": {"mirek": 153}})

    def test_v2_cool(self):
        self.assertEqual(ct_payload_v2(500),
                         {"color_temperature": {"mirek": 500}})

    def test_v2_clamped(self):
        self.assertEqual(ct_payload_v2(100),
                         {"color_temperature": {"mirek": 153}})
        self.assertEqual(ct_payload_v2(600),
                         {"color_temperature": {"mirek": 500}})

    def test_mirek_matches_v1_ct(self):
        for ct in [153, 250, 350, 446, 500]:
            self.assertEqual(ct_payload_v2(ct)["color_temperature"]["mirek"], ct)


# ===========================================================================
# UUID Handling
# ===========================================================================

class TestUUIDHandling(unittest.TestCase):
    def test_valid_uuid(self):
        self.assertTrue(is_uuid("0a601c1f-1234-5678-9abc-def012345678"))

    def test_invalid_uuid(self):
        self.assertFalse(is_uuid("0a601c1f"))
        self.assertFalse(is_uuid("/lights/17"))
        self.assertFalse(is_uuid(""))
        self.assertFalse(is_uuid(None))


# ===========================================================================
# Room → Grouped Light Resolution
# ===========================================================================

class TestRoomGroupedLightResolution(unittest.TestCase):
    VALID_ROOM = {
        "id": "3d70dd57-1234-5678-9abc-def012345678",
        "id_v1": "/groups/5",
        "services": [
            {"rid": "3e40dfd6-abcd-1234-5678-abcdef012345", "rtype": "grouped_light"}
        ],
        "metadata": {"name": "Bureau"},
        "type": "room",
    }

    def test_valid_room(self):
        uuid = extract_grouped_light_uuid(self.VALID_ROOM)
        self.assertEqual(uuid, "3e40dfd6-abcd-1234-5678-abcdef012345")

    def test_services_in_arbitrary_order(self):
        room = {
            "id": "room-uuid-1234-5678-9abc-def01234567",
            "services": [
                {"rid": "aaaa1111-2222-3333-4444-555566667777", "rtype": "other_service"},
                {"rid": "bbbb1111-2222-3333-4444-555566667777", "rtype": "light"},
                {"rid": "cccc1111-2222-3333-4444-555566667777", "rtype": "grouped_light"},
            ],
        }
        uuid = extract_grouped_light_uuid(room)
        self.assertEqual(uuid, "cccc1111-2222-3333-4444-555566667777")

    def test_no_grouped_light_service(self):
        room = {
            "id": "room-uuid",
            "services": [
                {"rid": "other-uuid-1234-5678-9abc-def01234567", "rtype": "other_service"},
            ],
        }
        self.assertIsNone(extract_grouped_light_uuid(room))

    def test_empty_services(self):
        room = {"id": "room-uuid", "services": []}
        self.assertIsNone(extract_grouped_light_uuid(room))

    def test_no_services_key(self):
        room = {"id": "room-uuid"}
        self.assertIsNone(extract_grouped_light_uuid(room))

    def test_services_not_list(self):
        room = {"id": "room-uuid", "services": "not a list"}
        self.assertIsNone(extract_grouped_light_uuid(room))

    def test_malformed_service_entry(self):
        room = {
            "id": "room-uuid",
            "services": [
                "not a dict",
                {"rid": 12345, "rtype": "grouped_light"},
                {"rtype": "grouped_light"},
                {"rid": "valid-uuid-1234-5678-9abc-def012345678"},
            ],
        }
        self.assertIsNone(extract_grouped_light_uuid(room))

    def test_unexpected_resource_types(self):
        room = {
            "id": "room-uuid",
            "services": [
                {"rid": "uuid-1234-5678-9abc-def012345678", "rtype": "zigbee_connectivity"},
                {"rid": "uuid-1234-5678-9abc-def012345678", "rtype": "device_software_update"},
            ],
        }
        self.assertIsNone(extract_grouped_light_uuid(room))

    def test_not_a_dict(self):
        self.assertIsNone(extract_grouped_light_uuid("not a dict"))
        self.assertIsNone(extract_grouped_light_uuid(None))
        self.assertIsNone(extract_grouped_light_uuid([]))

    def test_build_room_light_map(self):
        rooms = [
            self.VALID_ROOM,
            {
                "id": "aaaa2222-3333-4444-5555-666677778888",
                "services": [
                    {"rid": "bbbb2222-3333-4444-5555-666677778888", "rtype": "grouped_light"}
                ],
            },
            {
                "id": "cccc2222-3333-4444-5555-666677778888",
                "services": [
                    {"rid": "dddd2222-3333-4444-5555-666677778888", "rtype": "other"}
                ],
            },
        ]
        m = build_room_light_map(rooms)
        self.assertEqual(len(m), 2)
        self.assertIn("3d70dd57-1234-5678-9abc-def012345678", m)
        self.assertIn("aaaa2222-3333-4444-5555-666677778888", m)

    def test_build_room_light_map_empty(self):
        self.assertEqual(build_room_light_map([]), {})
        self.assertEqual(build_room_light_map(None), {})

    def test_real_bridge_data_structure(self):
        real_room = {
            "id": "3d70dd57-1111-2222-3333-444455556666",
            "id_v1": "/groups/5",
            "children": [
                {"rid": "1f84d41d-aaaa-bbbb-cccc-dddddddddddd", "rtype": "device"},
                {"rid": "a55796c0-aaaa-bbbb-cccc-dddddddddddd", "rtype": "device"},
            ],
            "services": [
                {"rid": "3e40dfd6-aaaa-bbbb-cccc-dddddddddddd", "rtype": "grouped_light"}
            ],
            "metadata": {"name": "Bureau", "archetype": "living_room"},
            "type": "room",
        }
        uuid = extract_grouped_light_uuid(real_room)
        self.assertEqual(uuid, "3e40dfd6-aaaa-bbbb-cccc-dddddddddddd")
        self.assertTrue(is_uuid(uuid))


# ===========================================================================
# Scene Recall Payload
# ===========================================================================

class TestSceneRecallPayload(unittest.TestCase):
    def test_active(self):
        self.assertEqual(scene_recall_payload("active"),
                         {"recall": {"action": "active"}})

    def test_dynamic_palette(self):
        self.assertEqual(scene_recall_payload("dynamic_palette"),
                         {"recall": {"action": "dynamic_palette"}})

    def test_static(self):
        self.assertEqual(scene_recall_payload("static"),
                         {"recall": {"action": "static"}})

    def test_invalid_action(self):
        with self.assertRaises(ValueError):
            scene_recall_payload("invalid")

    def test_default_action(self):
        self.assertEqual(scene_recall_payload(), {"recall": {"action": "active"}})


# ===========================================================================
# V2 Light Resource Parsing
# ===========================================================================

class TestParseLightV2(unittest.TestCase):
    REAL_LIGHT = {
        "id": "0a601c1f-1234-5678-9abc-def012345678",
        "id_v1": "/lights/17",
        "on": {"on": False},
        "dimming": {"brightness": 72.73, "min_dim_level": 0.2},
        "color_temperature": {
            "mirek": 446,
            "mirek_valid": True,
            "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500},
        },
        "color": {
            "xy": {"x": 0.5013, "y": 0.4152},
            "gamut": {"red": {"x": 0.6915, "y": 0.3083},
                      "green": {"x": 0.17, "y": 0.7},
                      "blue": {"x": 0.1532, "y": 0.0475}},
            "gamut_type": "C",
        },
        "metadata": {"name": "Salon", "archetype": "wall_spot", "function": "mixed"},
        "type": "light",
    }

    def test_basic_fields(self):
        light = parse_light_v2(self.REAL_LIGHT)
        self.assertIsNotNone(light)
        self.assertEqual(light["name"], "Salon")
        self.assertFalse(light["on"])

    def test_brightness_converted(self):
        light = parse_light_v2(self.REAL_LIGHT)
        self.assertGreaterEqual(light["bri"], 180)
        self.assertLessEqual(light["bri"], 190)

    def test_color_temperature(self):
        light = parse_light_v2(self.REAL_LIGHT)
        self.assertTrue(light["hasCt"])
        self.assertEqual(light["ct"], 446)

    def test_xy_color(self):
        light = parse_light_v2(self.REAL_LIGHT)
        self.assertTrue(light["hasColor"])
        self.assertEqual(light["xy"], [0.5013, 0.4152])

    def test_id_preserved(self):
        light = parse_light_v2(self.REAL_LIGHT)
        self.assertEqual(light["id"], "0a601c1f-1234-5678-9abc-def012345678")

    def test_not_dict(self):
        self.assertIsNone(parse_light_v2(None))
        self.assertIsNone(parse_light_v2("string"))

    def test_minimal_light(self):
        minimal = {"id": "min-uuid-1234-5678-9abc-def012345678", "type": "light"}
        light = parse_light_v2(minimal)
        self.assertIsNotNone(light)
        self.assertEqual(light["name"], "Light")
        self.assertFalse(light["on"])
        self.assertEqual(light["bri"], 0)


# ===========================================================================
# V2 Room Resource Parsing
# ===========================================================================

class TestParseRoomV2(unittest.TestCase):
    REAL_ROOM = {
        "id": "3d70dd57-1234-5678-9abc-def012345678",
        "id_v1": "/groups/5",
        "children": [
            {"rid": "1f84d41d-aaaa-bbbb-cccc-dddddddddddd", "rtype": "device"},
            {"rid": "a55796c0-aaaa-bbbb-cccc-dddddddddddd", "rtype": "device"},
        ],
        "services": [
            {"rid": "3e40dfd6-aaaa-bbbb-cccc-dddddddddddd", "rtype": "grouped_light"}
        ],
        "metadata": {"name": "Bureau", "archetype": "living_room"},
        "type": "room",
    }

    def test_basic_fields(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertIsNotNone(room)
        self.assertEqual(room["name"], "Bureau")
        self.assertEqual(room["type"], "Room")

    def test_grouped_light_extracted(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertEqual(room["groupedLightId"],
                         "3e40dfd6-aaaa-bbbb-cccc-dddddddddddd")

    def test_children_are_device_uuids(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertEqual(len(room["lightIds"]), 2)

    def test_not_dict(self):
        self.assertIsNone(parse_room_v2(None))


# ===========================================================================
# V1 → V2 Payload Conversion
# ===========================================================================

class TestConvertV1PayloadToV2(unittest.TestCase):
    def test_on_only(self):
        result = _convert_control_payload_to_v2({"on": True})
        self.assertEqual(result, {"on": {"on": True}})

    def test_on_false(self):
        result = _convert_control_payload_to_v2({"on": False})
        self.assertEqual(result, {"on": {"on": False}})

    def test_bri_only(self):
        result = _convert_control_payload_to_v2({"bri": 200})
        self.assertIn("dimming", result)
        self.assertAlmostEqual(result["dimming"]["brightness"], 78.74, delta=1.0)

    def test_ct_only(self):
        result = _convert_control_payload_to_v2({"ct": 300})
        self.assertEqual(result, {"color_temperature": {"mirek": 300}})

    def test_hue_and_sat(self):
        result = _convert_control_payload_to_v2({"hue": 0, "sat": 254})
        self.assertIn("color", result)
        xy = result["color"]["xy"]
        self.assertAlmostEqual(xy["x"], 0.675, delta=0.05)

    def test_on_and_bri(self):
        result = _convert_control_payload_to_v2({"on": True, "bri": 128})
        self.assertEqual(result["on"], {"on": True})
        self.assertIn("dimming", result)

    def test_on_bri_ct(self):
        result = _convert_control_payload_to_v2({"on": True, "bri": 128, "ct": 300})
        self.assertEqual(result["on"], {"on": True})
        self.assertIn("dimming", result)
        self.assertEqual(result["color_temperature"], {"mirek": 300})

    def test_empty_payload(self):
        result = _convert_control_payload_to_v2({})
        self.assertEqual(result, {})

    def test_passthrough_v2_payload(self):
        v2 = {"on": {"on": True}, "dimming": {"brightness": 50.0}}
        result = _convert_control_payload_to_v2(v2)
        self.assertEqual(result, v2)


# ===========================================================================
# V2 Light Model Contract (UI-facing)
# ===========================================================================

class TestV2LightModelContract(unittest.TestCase):
    """Verify the light model produced by parse_light_v2 meets the UI contract."""

    FULL_LIGHT = {
        "id": "0a601c1f-1234-5678-9abc-def012345678",
        "id_v1": "/lights/17",
        "on": {"on": True},
        "dimming": {"brightness": 72.73, "min_dim_level": 0.2},
        "color_temperature": {
            "mirek": 446,
            "mirek_valid": True,
            "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500},
        },
        "color": {
            "xy": {"x": 0.5013, "y": 0.4152},
            "gamut": {"red": {"x": 0.6915, "y": 0.3083},
                      "green": {"x": 0.17, "y": 0.7},
                      "blue": {"x": 0.1532, "y": 0.0475}},
            "gamut_type": "C",
        },
        "metadata": {"name": "Salon", "archetype": "wall_spot", "function": "mixed"},
        "type": "light",
    }

    CT_ONLY_LIGHT = {
        "id": "ct-only-uuid-1234-5678-9abc-def012345678",
        "on": {"on": True},
        "dimming": {"brightness": 50.0},
        "color_temperature": {
            "mirek": 300,
            "mirek_valid": True,
            "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500},
        },
        "metadata": {"name": "CT Light"},
        "type": "light",
    }

    DIM_ONLY_LIGHT = {
        "id": "dim-only-uuid-1234-5678-9abc-def012345678",
        "on": {"on": False},
        "dimming": {"brightness": 0.0},
        "metadata": {"name": "Dim Light"},
        "type": "light",
    }

    def test_has_required_fields(self):
        light = parse_light_v2(self.FULL_LIGHT)
        for key in ["id", "name", "on", "bri", "hasBri", "ct", "hasCt",
                     "hasColor", "colormode", "xy", "pickerOpen"]:
            self.assertIn(key, light)

    def test_uuid_is_string(self):
        light = parse_light_v2(self.FULL_LIGHT)
        self.assertIsInstance(light["id"], str)
        self.assertTrue(UUID_RE.match(light["id"]))

    def test_name_preserved(self):
        light = parse_light_v2(self.FULL_LIGHT)
        self.assertEqual(light["name"], "Salon")

    def test_on_state(self):
        light = parse_light_v2(self.FULL_LIGHT)
        self.assertTrue(light["on"])
        light_off = parse_light_v2({**self.FULL_LIGHT, "on": {"on": False}})
        self.assertFalse(light_off["on"])

    def test_brightness_converted(self):
        light = parse_light_v2(self.FULL_LIGHT)
        self.assertGreaterEqual(light["bri"], 180)
        self.assertLessEqual(light["bri"], 190)

    def test_ct_only_no_color(self):
        light = parse_light_v2(self.CT_ONLY_LIGHT)
        self.assertTrue(light["hasCt"])
        self.assertFalse(light["hasColor"])
        self.assertEqual(light["ct"], 300)

    def test_dim_only_no_ct_no_color(self):
        light = parse_light_v2(self.DIM_ONLY_LIGHT)
        self.assertFalse(light["hasCt"])
        self.assertFalse(light["hasColor"])

    def test_xy_color_present(self):
        light = parse_light_v2(self.FULL_LIGHT)
        self.assertTrue(light["hasColor"])
        self.assertEqual(light["xy"], [0.5013, 0.4152])

    def test_id_v1_not_required(self):
        no_v1 = {k: v for k, v in self.FULL_LIGHT.items() if k != "id_v1"}
        light = parse_light_v2(no_v1)
        self.assertIsNotNone(light)
        self.assertEqual(light["id"], "0a601c1f-1234-5678-9abc-def012345678")

    def test_malformed_optional_fields(self):
        bad = {
            "id": "bad-uuid-1234-5678-9abc-def012345678",
            "type": "light",
            "on": "not a dict",
            "dimming": "not a dict",
            "color_temperature": "not a dict",
            "color": "not a dict",
        }
        light = parse_light_v2(bad)
        self.assertIsNotNone(light)
        self.assertFalse(light["on"])
        self.assertEqual(light["bri"], 0)
        self.assertFalse(light["hasCt"])
        self.assertFalse(light["hasColor"])


# ===========================================================================
# Grouped Light Control (Room ON/OFF)
# ===========================================================================

class TestGroupedLightControl(unittest.TestCase):
    def test_on_payload(self):
        result = on_payload_v2(True)
        self.assertEqual(result, {"on": {"on": True}})

    def test_off_payload(self):
        result = on_payload_v2(False)
        self.assertEqual(result, {"on": {"on": False}})

    def test_brightness_payload(self):
        result = brightness_payload_v2(128)
        self.assertIn("dimming", result)
        self.assertAlmostEqual(result["dimming"]["brightness"], 50.0, delta=1.0)

    def test_room_uuid_format(self):
        uuid = "3e40dfd6-abcd-1234-5678-abcdef012345"
        self.assertTrue(UUID_RE.match(uuid))

    def test_room_not_same_as_grouped_light(self):
        room_uuid = "3d70dd57-1234-5678-9abc-def012345678"
        gl_uuid = "3e40dfd6-abcd-1234-5678-abcdef012345"
        self.assertNotEqual(room_uuid, gl_uuid)

    def test_grouped_light_write_uses_native_v2_target(self):
        gl_uuid = "3e40dfd6-abcd-1234-5678-abcdef012345"
        body = {"on": {"on": True}}
        with patch.object(_mod, "_v2_put", return_value={"errors": [], "data": []}) as put:
            v2_put_resource({}, "grouped_light", gl_uuid, body)
        put.assert_called_once_with(
            {}, "/clip/v2/resource/grouped_light/" + gl_uuid, body)


class TestPanelGroupedLightContract(unittest.TestCase):
    """Ensure optimistic room updates retain the v2 grouped-light target."""

    def _function_body(self, name, next_name):
        start = _panel_source.index("function %s(" % name)
        end = _panel_source.index("function %s(" % next_name, start)
        return _panel_source[start:end]

    def test_room_updates_preserve_grouped_light_id(self):
        functions = [
            ("setRoomOn", "setLightOn"),
            ("setLightOn", "patchLights"),
            ("patchLights", "setLightBri"),
            ("setAllOn", "runAction"),
        ]
        for name, next_name in functions:
            self.assertIn(
                'groupedLightId: room.groupedLightId || ""',
                self._function_body(name, next_name),
                name,
            )

    def test_room_command_uses_grouped_light_id(self):
        body = self._function_body("toggleRoom", "findRoom")
        self.assertIn('"put-grouped-light-v2", room.groupedLightId', body)
        self.assertNotIn('"put-grouped-light-v2", roomId', body)

    def test_fetch_completion_uses_process_exit_status(self):
        self.assertEqual(
            _panel_source.count("exitCode === 0, exitCode)"), 5)
        self.assertIn("function finishFetch(generation, success, exitCode)", _panel_source)


# ===========================================================================
# Room V2 Model Contract
# ===========================================================================

class TestV2RoomModelContract(unittest.TestCase):
    """Verify the room model produced by parse_room_v2 meets the UI contract."""

    REAL_ROOM = {
        "id": "3d70dd57-1234-5678-9abc-def012345678",
        "id_v1": "/groups/5",
        "children": [
            {"rid": "1f84d41d-aaaa-bbbb-cccc-dddddddddddd", "rtype": "device"},
            {"rid": "a55796c0-aaaa-bbbb-cccc-dddddddddddd", "rtype": "device"},
        ],
        "services": [
            {"rid": "3e40dfd6-aaaa-bbbb-cccc-dddddddddddd", "rtype": "grouped_light"}
        ],
        "metadata": {"name": "Bureau", "archetype": "living_room"},
        "type": "room",
    }

    def test_has_required_fields(self):
        room = parse_room_v2(self.REAL_ROOM)
        for key in ["id", "name", "type", "on", "allOn", "lightIds", "groupedLightId"]:
            self.assertIn(key, room)

    def test_uuid_is_string(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertIsInstance(room["id"], str)
        self.assertTrue(UUID_RE.match(room["id"]))

    def test_name_preserved(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertEqual(room["name"], "Bureau")

    def test_type_is_room(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertEqual(room["type"], "Room")

    def test_grouped_light_extracted(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertEqual(room["groupedLightId"],
                         "3e40dfd6-aaaa-bbbb-cccc-dddddddddddd")

    def test_children_are_device_uuids(self):
        room = parse_room_v2(self.REAL_ROOM)
        self.assertEqual(len(room["lightIds"]), 2)
        for child_id in room["lightIds"]:
            self.assertTrue(UUID_RE.match(child_id))

    def test_no_grouped_light(self):
        no_gl = {
            "id": "room-uuid-1234-5678-9abc-def012345678",
            "children": [],
            "services": [{"rid": "other-uuid-1234-5678-9abc-def012345678", "rtype": "other"}],
            "metadata": {"name": "No GL Room"},
            "type": "room",
        }
        room = parse_room_v2(no_gl)
        self.assertIsNone(room["groupedLightId"])

    def test_malformed_children(self):
        bad = {
            "id": "room-uuid-1234-5678-9abc-def012345678",
            "children": "not an array",
            "services": [],
            "metadata": {"name": "Bad Room"},
            "type": "room",
        }
        room = parse_room_v2(bad)
        self.assertIsNotNone(room)
        self.assertEqual(room["lightIds"], [])


if __name__ == "__main__":
    unittest.main()
