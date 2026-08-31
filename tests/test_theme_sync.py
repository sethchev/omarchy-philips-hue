"""Tests for theme-sync color transformations and selection logic.

These tests validate the v2 migration of 45-hue.sh without touching real
light state.  Run with:  python3 -m pytest tests/test_theme_sync.py -v
"""
import json
import math
import re
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Functions extracted from 45-hue.sh (v2 version) for testing
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_ROOT = Path(__file__).resolve().parents[1]
_HOOK = _ROOT / "theme-sync" / "45-hue.sh"

_ROOM_ID = "3d70dd57-f9c1-4621-bafd-0a8754926fee"
_GROUPED_LIGHT_ID = "3e40dfd6-e5ce-4419-9828-75f0808f76cd"
_DEVICE_1 = "1f84d41d-5ab7-49a8-9b76-d198d6aa9384"
_DEVICE_2 = "a55796c0-c097-425d-a6fe-67cad52e2a07"
_DEVICE_3 = "c61a4cbd-9f6e-4f9d-8fc0-7151b7d5ed31"
_LIGHT_1 = "4aa2a1a7-7fce-47ef-8b8c-8e718e6f19a1"
_LIGHT_2 = "b5bb53d3-79fa-46e6-a963-4bb9416a0a76"
_LIGHT_3 = "a2dd2cd6-2e5b-4d5c-bb85-3d8e9a3e9f51"
_ZONE_ID = "e1c2a3b4-d5e6-47f8-9012-3456789abcde"
_ZONE_GROUPED_LIGHT_ID = "f1e2d3c4-b5a6-4789-9012-3456789abcde"


def _run_hook(tmp_path, config, fixtures, extra_env=None):
    home = tmp_path / "home"
    plugin = home / ".config/omarchy/plugins/omarchy-philips-hue"
    state_home = Path((extra_env or {}).get("XDG_STATE_HOME", home / ".local/state"))
    state = state_home / "omarchy"
    settings = home / ".config/omarchy/settings"
    current_theme = state / "current/theme"
    plugin.mkdir(parents=True)
    settings.mkdir(parents=True)
    current_theme.mkdir(parents=True)
    (state / "settings").mkdir(parents=True)
    (state / "settings/hue.json").write_text("{}")
    (settings / "hue-theme.json").write_text(json.dumps(config))
    (current_theme / "colors.toml").write_text(textwrap.dedent("""\
        accent = "#ff5733"
        red = "#ff0000"
        blue = "#0000ff"
    """))

    capture = home / "capture.json"
    fake_api = plugin / "hue-api.py"
    fake_api.write_text(textwrap.dedent("""\
        import json
        import os

        FIXTURES = %r
        CAPTURE = os.environ["HUE_CAPTURE"]

        def _record(value):
            records = []
            if os.path.exists(CAPTURE):
                with open(CAPTURE) as f:
                    records = json.load(f)
            records.append(value)
            with open(CAPTURE, "w") as f:
                json.dump(records, f)

        def _load_creds():
            return {"bridgeIp": "192.0.2.1", "username": "not-in-argv"}

        def v2_get_data(creds, resource_type):
            _record({"op": "get", "resource": resource_type})
            if os.environ.get("HUE_HTTP_ERROR_RESOURCE") == resource_type:
                from urllib.error import HTTPError
                raise HTTPError("https://bridge", int(os.environ["HUE_HTTP_STATUS"]), "", {}, None)
            if os.environ.get("HUE_FAIL_RESOURCE") == resource_type:
                raise RuntimeError("simulated transport failure")
            return FIXTURES.get(resource_type, [])

        def v2_put_resource(creds, resource_type, resource_id, body):
            _record({"op": "put", "resource": resource_type,
                     "id": resource_id, "body": body})
            if os.environ.get("HUE_FAIL_WRITE") == resource_id:
                raise RuntimeError("simulated write failure")
    """ % fixtures))

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "HUE_CAPTURE": str(capture),
    })
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", str(_HOOK), "test-theme"], env=env, text=True,
        capture_output=True, check=False)
    records = json.loads(capture.read_text()) if capture.exists() else []
    log_file = state / "hue-theme-hook.log"
    log = log_file.read_text() if log_file.exists() else ""
    return result, records, log


def _bureau_fixtures(include_non_color=False):
    children = [
        {"rtype": "device", "rid": _DEVICE_1},
        {"rtype": "device", "rid": _DEVICE_2},
    ]
    devices = [
        {"id": _DEVICE_1, "services": [{"rtype": "light", "rid": _LIGHT_1}]},
        {"id": _DEVICE_2, "services": [{"rtype": "light", "rid": _LIGHT_2}]},
    ]
    lights = [
        {"id": _LIGHT_1, "color": {"xy": {"x": 0.1, "y": 0.2}}},
        {"id": _LIGHT_2, "color": {"xy": {"x": 0.3, "y": 0.4}}},
    ]
    if include_non_color:
        children.append({"rtype": "device", "rid": _DEVICE_3})
        devices.append(
            {"id": _DEVICE_3, "services": [{"rtype": "light", "rid": _LIGHT_3}]})
        lights.append({"id": _LIGHT_3})
    return {
        "room": [{
            "id": _ROOM_ID,
            "type": "room",
            "metadata": {"name": "Bureau"},
            "services": [{"rtype": "grouped_light", "rid": _GROUPED_LIGHT_ID}],
            "children": children,
        }],
        "device": devices,
        "light": lights,
    }


def _zone_fixtures():
    fixtures = _bureau_fixtures()
    fixtures["zone"] = [{
        "id": _ZONE_ID,
        "type": "zone",
        "metadata": {"name": "Whole Home"},
        "services": [{"rtype": "grouped_light", "rid": _ZONE_GROUPED_LIGHT_ID}],
        "children": [],
    }]
    return fixtures


def _direct_light_zone_fixtures():
    fixtures = _bureau_fixtures()
    fixtures["zone"] = [{
        "id": _ZONE_ID,
        "type": "zone",
        "metadata": {"name": "Whole Home"},
        "services": [{"rtype": "grouped_light", "rid": _ZONE_GROUPED_LIGHT_ID}],
        "children": [
            {"rtype": "light", "rid": _LIGHT_1},
            {"rtype": "light", "rid": _LIGHT_2},
        ],
    }]
    return fixtures


def hex_to_xy(hexval):
    """Convert 6-digit hex color to CIE 1931 (x, y) coordinates."""
    r = int(hexval[0:2], 16) / 255.0
    g = int(hexval[2:4], 16) / 255.0
    b = int(hexval[4:6], 16) / 255.0
    X = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    Y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    Z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    d = X + Y + Z
    if d == 0:
        return 0.0, 0.0
    return round(X / d, 4), round(Y / d, 4)


def brightness_to_v2(bri):
    """Convert the panel brightness scale (1-254) to v2 (0.0-100.0)."""
    return round((max(1, min(254, int(bri))) / 254.0) * 100.0, 2)


def transition_to_ms(transitiontime):
    """Convert the config transition (tenths of a second) to milliseconds."""
    return max(0, int(transitiontime)) * 100


def build_scene_palette(path):
    """Ordered, de-duplicated scene palette from colors.toml: accent first,
    then the named palette colors and any other plain hex keys in file order.
    Surface / neutral keys (background, foreground, selection, borders,
    tabs) are skipped so scenes stay colorful."""
    named = set(("red", "green", "yellow", "blue", "magenta", "cyan",
                 "bright_red", "bright_green", "bright_yellow",
                 "bright_blue", "bright_magenta", "bright_cyan"))
    order = []
    values = {}
    try:
        with open(path) as f:
            for raw in f:
                m = re.match(
                    r'^\s*([A-Za-z0-9_]+)\s*=\s*"#([0-9a-fA-F]{6})"\s*$', raw)
                if m:
                    values[m.group(1)] = m.group(2).lower()
                    order.append(m.group(1))
    except Exception:
        return []
    def skip(k):
        return (k in ("selection", "muted")
                or "background" in k or "foreground" in k
                or "border" in k or "tab" in k)
    keys = ([k for k in order if k == "accent"]
            + [k for k in order if k in named]
            + [k for k in order if k not in named and not skip(k)])
    palette, seen = [], set()
    for k in keys:
        v = values.get(k)
        if v and v not in seen:
            seen.add(v)
            palette.append(v)
    return palette


# ---------------------------------------------------------------------------
# hex_to_xy tests
# ---------------------------------------------------------------------------

class TestHexToXY:
    def test_pure_red(self):
        x, y = hex_to_xy("FF0000")
        # CIE 1931 red ≈ (0.6484, 0.3309)
        assert abs(x - 0.6484) < 0.01
        assert abs(y - 0.3309) < 0.01

    def test_pure_green(self):
        x, y = hex_to_xy("00FF00")
        assert abs(x - 0.2973) < 0.02
        assert abs(y - 0.6) < 0.02

    def test_pure_blue(self):
        x, y = hex_to_xy("0000FF")
        assert abs(x - 0.1523) < 0.02
        assert abs(y - 0.06) < 0.02

    def test_white(self):
        x, y = hex_to_xy("FFFFFF")
        assert abs(x - 0.3127) < 0.01
        assert abs(y - 0.3290) < 0.01

    def test_black_returns_zero(self):
        x, y = hex_to_xy("000000")
        assert x == 0.0
        assert y == 0.0

    def test_midgray(self):
        x, y = hex_to_xy("808080")
        assert abs(x - 0.3127) < 0.01
        assert abs(y - 0.3290) < 0.01

    def test_round_trip_accuracy(self):
        """XY values should be within valid Hue range (0-1)."""
        for hexval in ["FF5733", "33FF57", "3357FF", "FFFF00", "FF00FF"]:
            x, y = hex_to_xy(hexval)
            assert 0.0 <= x <= 1.0, f"x={x} out of range for {hexval}"
            assert 0.0 <= y <= 1.0, f"y={y} out of range for {hexval}"


# ---------------------------------------------------------------------------
# brightness_v1_to_v2 tests
# ---------------------------------------------------------------------------

class TestBrightnessConversion:
    def test_min(self):
        assert brightness_to_v2(1) == round(1 / 254.0 * 100.0, 2)

    def test_max(self):
        assert brightness_to_v2(254) == 100.0

    def test_mid(self):
        result = brightness_to_v2(127)
        assert 49.0 < result < 51.0

    def test_clamp_low(self):
        assert brightness_to_v2(0) == brightness_to_v2(1)

    def test_clamp_high(self):
        assert brightness_to_v2(300) == 100.0


# ---------------------------------------------------------------------------
# transition_v1_to_ms tests
# ---------------------------------------------------------------------------

class TestTransitionConversion:
    def test_default(self):
        assert transition_to_ms(20) == 2000

    def test_instant(self):
        assert transition_to_ms(0) == 0

    def test_one_second(self):
        assert transition_to_ms(10) == 1000

    def test_half_second(self):
        assert transition_to_ms(5) == 500


# ---------------------------------------------------------------------------
# build_scene_palette tests
# ---------------------------------------------------------------------------

class TestBuildScenePalette:
    def test_empty_file(self, tmp_path):
        p = tmp_path / "colors.toml"
        p.write_text("")
        assert build_scene_palette(str(p)) == []

    def test_accent_first(self, tmp_path):
        p = tmp_path / "colors.toml"
        p.write_text(textwrap.dedent("""\
            foreground = "#ffffff"
            accent = "#ff5733"
            background = "#000000"
            red = "#ff0000"
        """))
        palette = build_scene_palette(str(p))
        assert palette[0] == "ff5733"
        assert "ff0000" in palette

    def test_skips_surface_keys(self, tmp_path):
        p = tmp_path / "colors.toml"
        p.write_text(textwrap.dedent("""\
            background = "#000000"
            foreground = "#ffffff"
            selection = "#333333"
            border = "#444444"
            tab = "#555555"
            accent = "#aabbcc"
            red = "#ff0000"
        """))
        palette = build_scene_palette(str(p))
        assert "000000" not in palette  # background
        assert "ffffff" not in palette  # foreground
        assert "333333" not in palette  # selection
        assert "aabbcc" in palette
        assert "ff0000" in palette

    def test_deduplicates(self, tmp_path):
        p = tmp_path / "colors.toml"
        p.write_text(textwrap.dedent("""\
            accent = "#aabbcc"
            red = "#aabbcc"
        """))
        palette = build_scene_palette(str(p))
        assert palette.count("aabbcc") == 1

    def test_missing_file(self):
        assert build_scene_palette("/nonexistent/path") == []


# ---------------------------------------------------------------------------
# V2 payload structure tests
# ---------------------------------------------------------------------------

class TestV2Payloads:
    def test_uniform_body_structure(self):
        """Verify the uniform body matches v2 grouped_light PUT schema."""
        xy_x, xy_y = hex_to_xy("ff5733")
        body = {"color": {"xy": {"x": xy_x, "y": xy_y}}}
        bri = brightness_to_v2(128)
        body["dimming"] = {"brightness": bri}
        body["on"] = {"on": True}
        body["dynamics"] = {"duration": 2000}

        assert "color" in body
        assert "xy" in body["color"]
        assert 0.0 <= body["color"]["xy"]["x"] <= 1.0
        assert 0.0 <= body["color"]["xy"]["y"] <= 1.0
        assert body["dimming"]["brightness"] > 0
        assert body["on"]["on"] is True
        assert body["dynamics"]["duration"] > 0

    def test_scene_light_body(self):
        """Verify scene light body structure."""
        px, py = hex_to_xy("00ff00")
        body = {"color": {"xy": {"x": px, "y": py}}}
        body["dimming"] = {"brightness": 75.0}
        body["on"] = {"on": True}
        body["dynamics"] = {"duration": 1500}

        assert body["color"]["xy"]["x"] == px
        assert body["color"]["xy"]["y"] == py
        assert body["dimming"]["brightness"] == 75.0

    def test_v2_envelope_parsing(self):
        """Verify we handle the v2 response envelope correctly."""
        envelope = json.dumps({"data": [{"id": "test-uuid"}], "errors": []})
        data = json.loads(envelope)
        assert "data" in data
        assert len(data["data"]) == 1
        assert data["data"][0]["id"] == "test-uuid"


# ---------------------------------------------------------------------------
# UUID validation tests
# ---------------------------------------------------------------------------

class TestUUIDValidation:
    def test_valid_uuid(self):
        assert _UUID_RE.match("3e40dfd6-e5ce-4419-9828-75f0808f76cd")

    def test_invalid_uuid_short(self):
        assert not _UUID_RE.match("abc-123")

    def test_invalid_uuid_uppercase(self):
        # Hue UUIDs are lowercase hex
        assert not _UUID_RE.match("3E40DFD6-E5CE-4419-9828-75F0808F76CD")


# ---------------------------------------------------------------------------
# Room mapping tests
# ---------------------------------------------------------------------------

class TestRoomMapping:
    def test_room_services_parsing(self):
        """Verify room-to-grouped_light mapping logic."""
        room = {
            "id": "room-uuid",
            "metadata": {"name": "Test Room"},
            "services": [
                {"rtype": "grouped_light", "rid": "3e40dfd6-e5ce-4419-9828-75f0808f76cd"},
                {"rtype": "device", "rid": "dev-uuid-1234-5678-9abc-def0"},
            ],
        }
        # Extract grouped_light UUID
        gl_uuid = None
        for svc in room.get("services", []):
            if (isinstance(svc, dict)
                    and svc.get("rtype") == "grouped_light"
                    and _UUID_RE.match(str(svc.get("rid", "")))):
                gl_uuid = svc["rid"]
        assert gl_uuid is not None

    def test_device_to_light_mapping(self):
        """Verify device-to-light ID mapping."""
        v2_lights = [
            {"id": "light-uuid-1", "owner": {"rtype": "device", "rid": "dev-1"}},
            {"id": "light-uuid-2", "owner": {"rtype": "device", "rid": "dev-2"}},
        ]
        device_to_light = {}
        for l in v2_lights:
            owner = l.get("owner") or {}
            if isinstance(owner, dict) and owner.get("rtype") == "device":
                device_to_light[owner.get("rid", "")] = l["id"]
        assert device_to_light["dev-1"] == "light-uuid-1"
        assert device_to_light["dev-2"] == "light-uuid-2"


class TestProductionThemeHook:
    def test_uses_validated_hue_api_transport(self):
        source = _HOOK.read_text()
        assert "hue_api.v2_get_data(creds, resource_type)" in source
        assert "hue_api.v2_put_resource(creds, resource_type, resource_id, body)" in source
        assert "urllib.request" not in source
        assert "hue-application-key" not in source

    def test_uniform_mode_targets_bureau_grouped_light(self, tmp_path):
        result, records, _ = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "transition": 20,
             "bri": 127, "turnOn": True},
            _bureau_fixtures())
        assert result.returncode == 0
        writes = [r for r in records if r["op"] == "put"]
        assert writes == [{
            "op": "put",
            "resource": "grouped_light",
            "id": _GROUPED_LIGHT_ID,
            "body": {
                "color": {"xy": {"x": 0.4417, "y": 0.3647}},
                "dimming": {"brightness": 50.0},
                "on": {"on": True},
                "dynamics": {"duration": 2000},
            },
        }]

    def test_scene_mode_resolves_devices_and_skips_non_color_lights(self, tmp_path):
        result, records, _ = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": True},
            _bureau_fixtures(include_non_color=True))
        assert result.returncode == 0
        writes = [r for r in records if r["op"] == "put"]
        assert [r["resource"] for r in writes] == ["light", "light"]
        assert [r["id"] for r in writes] == [_LIGHT_1, _LIGHT_2]
        assert all(r["id"] != _LIGHT_3 for r in writes)

    def test_failed_scene_write_is_not_counted_as_a_completed_scene(self, tmp_path):
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": True},
            _bureau_fixtures(), {"HUE_FAIL_WRITE": _LIGHT_2})
        assert result.returncode == 0
        assert [record["id"] for record in records if record["op"] == "put"] == [_LIGHT_1, _LIGHT_2]
        assert "theme scene incomplete: Bureau -> 1/2 light(s)" in log
        assert "theme sync: 0/1 group(s)" in log

    def test_scene_discovery_failure_never_falls_back_to_grouped_light(self, tmp_path):
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": True},
            _bureau_fixtures(),
            {"HUE_FAIL_RESOURCE": "device"})
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "scene device or light discovery failed" in log

    def test_incomplete_scene_mapping_never_falls_back_to_grouped_light(self, tmp_path):
        fixtures = _bureau_fixtures()
        fixtures["device"] = []
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": True}, fixtures)
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "scene device-to-light mapping incomplete" in log

    def test_empty_device_services_never_falls_back_to_grouped_light(self, tmp_path):
        fixtures = _bureau_fixtures()
        fixtures["device"][1]["services"] = []
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": True}, fixtures)
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "scene device-to-light mapping incomplete" in log
        assert "theme sync: 0/1 group(s)" in log

    def test_device_without_owned_light_never_falls_back_to_grouped_light(self, tmp_path):
        fixtures = _bureau_fixtures()
        fixtures["device"][1]["services"] = [{"rtype": "zigbee_connectivity", "rid": _LIGHT_2}]
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": True}, fixtures)
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "scene device-to-light mapping incomplete" in log

    def test_single_color_light_never_falls_back_to_grouped_light(self, tmp_path):
        fixtures = _bureau_fixtures()
        fixtures["room"][0]["children"] = fixtures["room"][0]["children"][:1]
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": True}, fixtures)
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "scene requires at least two color lights" in log

    def test_zone_uuid_targets_its_grouped_light(self, tmp_path):
        result, records, _ = _run_hook(
            tmp_path,
            {"enabled": True, "groups": [_ZONE_ID]}, _zone_fixtures())
        assert result.returncode == 0
        writes = [record for record in records if record["op"] == "put"]
        assert [record["id"] for record in writes] == [_ZONE_GROUPED_LIGHT_ID]

    def test_direct_light_zone_scene_targets_its_child_lights(self, tmp_path):
        result, records, _ = _run_hook(
            tmp_path,
            {"enabled": True, "groups": [_ZONE_ID], "scene": True},
            _direct_light_zone_fixtures())
        assert result.returncode == 0
        writes = [record for record in records if record["op"] == "put"]
        assert [record["resource"] for record in writes] == ["light", "light"]
        assert [record["id"] for record in writes] == [_LIGHT_1, _LIGHT_2]

    def test_ambiguous_group_name_is_not_selected(self, tmp_path):
        fixtures = _zone_fixtures()
        fixtures["zone"][0]["metadata"]["name"] = "Bureau"
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"]}, fixtures)
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "missing or ambiguous" in log

    def test_group_selector_does_not_use_substring_matching(self, tmp_path):
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bure"]}, _bureau_fixtures())
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "group selector 'Bure' is missing or ambiguous" in log

    def test_legacy_numeric_group_config_is_not_applied(self, tmp_path):
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "themeSync": {"5": False}},
            _bureau_fixtures())
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "legacy numeric theme config" in log

    def test_legacy_numeric_group_config_migrates_from_id_v1(self, tmp_path):
        fixtures = _bureau_fixtures()
        fixtures["room"][0]["id_v1"] = "/groups/5"
        result, records, _ = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "themeSync": {"5": False}},
            fixtures)
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]

    @pytest.mark.parametrize("key", ["themeSync", "sceneRooms"])
    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, {}])
    def test_non_boolean_per_group_config_is_rejected(self, tmp_path, key, value):
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], key: {_ROOM_ID: value}},
            _bureau_fixtures())
        assert result.returncode == 0
        assert not [record for record in records if record["op"] == "put"]
        assert "invalid per-group theme config value" in log

    @pytest.mark.parametrize("settings", [
        {"5": False, _ROOM_ID: True},
        {_ROOM_ID: True, "5": False},
    ])
    def test_native_theme_sync_setting_wins_over_legacy_key_order(self, tmp_path, settings):
        fixtures = _bureau_fixtures()
        fixtures["room"][0]["id_v1"] = "/groups/5"
        result, records, _ = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "themeSync": settings}, fixtures)
        assert result.returncode == 0
        assert [record["id"] for record in records if record["op"] == "put"] == [_GROUPED_LIGHT_ID]

    @pytest.mark.parametrize("settings", [
        {"5": True, _ROOM_ID: False},
        {_ROOM_ID: False, "5": True},
    ])
    def test_native_scene_setting_wins_over_legacy_key_order(self, tmp_path, settings):
        fixtures = _bureau_fixtures()
        fixtures["room"][0]["id_v1"] = "/groups/5"
        result, records, _ = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"], "scene": False,
             "sceneRooms": settings}, fixtures)
        assert result.returncode == 0
        assert [record["resource"] for record in records if record["op"] == "put"] == ["grouped_light"]

    def test_uses_xdg_state_home_for_theme_files_and_log(self, tmp_path):
        state_home = tmp_path / "state"
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"]}, _bureau_fixtures(),
            {"XDG_STATE_HOME": str(state_home)})
        assert result.returncode == 0
        assert [record for record in records if record["op"] == "put"]
        assert "theme sync" in log

    def test_transport_failure_is_logged_and_sends_nothing(self, tmp_path):
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"]},
            _bureau_fixtures(),
            {"HUE_FAIL_RESOURCE": "room"})
        assert result.returncode == 0
        assert not [r for r in records if r["op"] == "put"]
        assert "Hue bridge transport error while reading room" in log
        assert "room or zone discovery failed; skipping hue theme sync" in log

    def test_http_auth_failure_is_not_reported_as_bridge_unreachable(self, tmp_path):
        result, records, log = _run_hook(
            tmp_path,
            {"enabled": True, "groups": ["Bureau"]},
            _bureau_fixtures(),
            {"HUE_HTTP_ERROR_RESOURCE": "room", "HUE_HTTP_STATUS": "403"})
        assert result.returncode == 0
        assert not [r for r in records if r["op"] == "put"]
        assert "Hue API HTTP 403 while reading room" in log
        assert "bridge unreachable" not in log
