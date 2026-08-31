#!/usr/bin/env bash
if [[ -f "${THPM_THEME_ENV:-$HOME/.local/share/thpm/lib/theme-env.sh}" ]]; then
  source "${THPM_THEME_ENV:-$HOME/.local/share/thpm/lib/theme-env.sh}"
fi
set -u

THEME_SLUG="${1:-}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
CONFIG_FILE="$HOME/.config/omarchy/settings/hue-theme.json"
CREDS_FILE="$STATE_HOME/omarchy/settings/hue.json"
LOG_DIR="$STATE_HOME/omarchy"
LOG_FILE="$LOG_DIR/hue-theme-hook.log"
PLUGIN_DIR="$HOME/.config/omarchy/plugins/omarchy-philips-hue"
HUE_API_PATH="$PLUGIN_DIR/hue-api.py"

if ! command -v python3 >/dev/null 2>&1; then
  printf '[%s] %s\n' "$(date '+%F %T')" "python3 not found; skipping hue theme sync" >> "$LOG_FILE" 2>/dev/null
  exit 0
fi

log() {
  python3 -c "
import os, sys, time
try:
    fd = os.open('''$LOG_FILE''', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write('[%s] %s\n' % (time.strftime('%F %T'), sys.argv[1]))
except Exception:
    pass
" "$*" 2>/dev/null
}

# Ensure log file has restricted permissions before first write
python3 -c "
import os
try:
    fd = os.open('''$LOG_FILE''', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
except FileExistsError:
    try:
        fd = os.open('''$LOG_FILE''', os.O_WRONLY | os.O_NOFOLLOW)
        os.fchmod(fd, 0o600)
        os.close(fd)
    except (OSError, ValueError):
        try:
            os.remove('''$LOG_FILE''')
        except OSError:
            pass
        fd = os.open('''$LOG_FILE''', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
" 2>/dev/null

if [[ ! -f "$CREDS_FILE" ]]; then
  exit 0
fi

if [[ ! -r "$HUE_API_PATH" ]]; then
  log "hue-api.py unavailable; skipping hue theme sync"
  exit 0
fi

COLORS_FILE="$STATE_HOME/omarchy/current/theme/colors.toml"
accent_color=""
if [[ -f "$COLORS_FILE" ]]; then
  accent_color="$(grep -E '^accent\s*=' "$COLORS_FILE" | sed 's/^accent\s*=\s*["'\'']\?\([#]*[0-9a-fA-F]\{6\}\)["'\'']\?/\1/' | tr -d '#')"
fi
if [[ -z "$accent_color" ]]; then
  log "no accent color available; skipping hue theme sync"
  exit 0
fi

CONFIG_FILE="$CONFIG_FILE" \
  THEME_SLUG="$THEME_SLUG" \
  ACCENT_COLOR="$accent_color" \
  HUE_API_PATH="$HUE_API_PATH" \
  STATE_HOME="$STATE_HOME" \
  python3 - "$LOG_FILE" <<'PY'
import json
import importlib.util
import math
import os
import re
import socket
import ssl
import sys
import time
from urllib.error import HTTPError, URLError

log_file = sys.argv[1]
theme_slug = os.environ.get("THEME_SLUG", "")
accent = os.environ.get("ACCENT_COLOR", "").lstrip("#")
config_file = os.environ.get("CONFIG_FILE", "")
state_home = os.environ.get("STATE_HOME", os.path.expanduser("~/.local/state"))
creds_file = os.path.join(state_home, "omarchy/settings/hue.json")
hue_api_path = os.environ.get("HUE_API_PATH", "")

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def log(msg):
    try:
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write("[%s] %s\n" % (time.strftime("%F %T"), msg))
    except Exception:
        pass


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


try:
    spec = importlib.util.spec_from_file_location("hue_api", hue_api_path)
    hue_api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hue_api)
    creds = hue_api._load_creds()
except Exception as e:
    log("unable to load Hue v2 transport; skipping hue theme sync")
    sys.exit(0)


def bridge_error(error):
    if isinstance(error, HTTPError):
        return "Hue API HTTP %d" % error.code
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLError):
        return "Hue bridge TLS error"
    if isinstance(reason, socket.gaierror):
        return "Hue bridge DNS error"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "Hue bridge timeout"
    return "Hue bridge transport error"


def get_resource(resource_type):
    try:
        return hue_api.v2_get_data(creds, resource_type)
    except Exception as e:
        log("%s while reading %s" % (bridge_error(e), resource_type))
        return None


def put_resource(resource_type, resource_id, body):
    try:
        hue_api.v2_put_resource(creds, resource_type, resource_id, body)
        return True
    except Exception as e:
        log("%s while writing %s/%s" % (
            bridge_error(e), resource_type, resource_id))
        return False


# ---------------------------------------------------------------------------
# Color conversion: hex -> CIE 1931 XY (same math as hue-api.py)
# ---------------------------------------------------------------------------

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
# Credential loading and permission repair
# ---------------------------------------------------------------------------

try:
    fd = os.open(creds_file, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        perms = oct(st.st_mode)[-3:]
        if perms != "600":
            os.fchmod(fd, 0o600)
            log("repaired hue.json permissions from %s to 600" % perms)
    finally:
        os.close(fd)
except (OSError, ValueError):
    pass

cfg = read_json(config_file) or {}
if not cfg.get("enabled", True):
    sys.exit(0)

overrides = cfg.get("themes") or {}
color = str(overrides.get(theme_slug) or accent).lstrip("#")
if not color or len(color) != 6:
    log("invalid color '%s'; skipping hue theme sync" % color)
    sys.exit(0)

xy_x, xy_y = hex_to_xy(color)
transition_ms = transition_to_ms(int(cfg.get("transition", 20) or 20))

# ---------------------------------------------------------------------------
# Discover rooms/zones and their grouped_light UUIDs (v2)
# ---------------------------------------------------------------------------
rooms = get_resource("room")
zones = get_resource("zone")
if rooms is None or zones is None:
    log("room or zone discovery failed; skipping hue theme sync")
    sys.exit(0)


def group_info(resource):
    if not isinstance(resource, dict):
        return None
    kind = resource.get("type")
    rid = resource.get("id", "")
    if kind not in ("room", "zone") or not _UUID_RE.match(str(rid)):
        return None
    metadata = resource.get("metadata") or {}
    name = metadata.get("name", "") if isinstance(metadata, dict) else ""
    services = resource.get("services")
    children = resource.get("children")
    if not isinstance(services, list) or not isinstance(children, list):
        return None
    grouped_light_id = None
    for service in services:
        if (isinstance(service, dict) and service.get("rtype") == "grouped_light"
                and _UUID_RE.match(str(service.get("rid", "")))):
            grouped_light_id = service["rid"]
            break
    device_ids = []
    light_ids = []
    children_valid = True
    for child in children:
        if not isinstance(child, dict):
            children_valid = False
            continue
        if child.get("rtype") == "device":
            device_id = child.get("rid", "")
            if _UUID_RE.match(str(device_id)):
                device_ids.append(device_id)
            else:
                children_valid = False
        elif child.get("rtype") == "light":
            light_id = child.get("rid", "")
            if _UUID_RE.match(str(light_id)):
                light_ids.append(light_id)
            else:
                children_valid = False
    return {
        "id": rid,
        "legacy_id": str(resource.get("id_v1", "")).rsplit("/", 1)[-1],
        "name": str(name),
        "kind": kind,
        "grouped_light_id": grouped_light_id,
        "device_ids": device_ids,
        "light_ids": light_ids,
        "children_valid": children_valid,
    }


groups = [info for resource in rooms + zones for info in [group_info(resource)]
          if info is not None]


theme_sync = cfg.get("themeSync") or {}
scene_rooms = cfg.get("sceneRooms") or {}
if not isinstance(theme_sync, dict) or not isinstance(scene_rooms, dict):
    log("invalid per-group theme config; skipping hue theme sync")
    sys.exit(0)
if any(type(value) is not bool for value in theme_sync.values()) or any(
        type(value) is not bool for value in scene_rooms.values()):
    log("invalid per-group theme config value; skipping hue theme sync")
    sys.exit(0)


def migrate_legacy_keys(settings):
    """Translate persisted v1 numeric group keys to v2 resource UUIDs only."""
    migrated = {}
    native = {}
    for key, value in settings.items():
        key = str(key)
        if key.isdigit():
            matches = [group for group in groups if group["legacy_id"] == key]
            if len(matches) != 1:
                return None
            key = matches[0]["id"]
            migrated[key] = value
        else:
            native[key] = value
    # Explicit v2 resource settings win regardless of JSON key order.
    migrated.update(native)
    return migrated


theme_sync = migrate_legacy_keys(theme_sync)
scene_rooms = migrate_legacy_keys(scene_rooms)
if theme_sync is None or scene_rooms is None:
    log("legacy numeric theme config cannot be resolved; skipping hue theme sync")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Build target list from exact config names or native UUIDs
# ---------------------------------------------------------------------------
configured = cfg.get("groups")
if configured is not None and not isinstance(configured, list):
    log("invalid group list; skipping hue theme sync")
    sys.exit(0)
if configured and not any(str(value).strip().lower() == "all" for value in configured):
    targets, seen = [], set()
    for value in configured:
        selector = str(value).strip()
        if not selector:
            continue
        if _UUID_RE.match(selector):
            matches = [group for group in groups if group["id"] == selector]
        else:
            matches = [group for group in groups
                       if group["name"].lower() == selector.lower()]
        if len(matches) != 1:
            log("group selector '%s' is missing or ambiguous; skipping" % selector)
            continue
        target = matches[0]
        if target["id"] not in seen:
            targets.append(target)
            seen.add(target["id"])
else:
    targets = groups

# themeSync and sceneRooms are keyed by v2 room/zone resource UUIDs.
targets = [target for target in targets if theme_sync.get(target["id"], True)]
scene = bool(cfg.get("scene", False))


def uses_scene(target):
    return scene_rooms.get(target["id"], scene)


any_scene = any(uses_scene(target) for target in targets)
palette_xy = []
if any_scene:
    palette = build_scene_palette(os.path.join(
        state_home, "omarchy/current/theme/colors.toml"))
    if not palette:
        log("scene palette unavailable; skipping requested multi-color sync")
    else:
        palette[0] = color  # per-theme override re-colors the accent anchor
        palette_xy = [hex_to_xy(h) for h in palette]

# Device/light discovery is required only for requested multi-color targets.
devices_by_id = {}
lights_by_id = {}
scene_discovery_failed = False
if any_scene:
    v2_devices = get_resource("device")
    v2_lights = get_resource("light")
    if v2_devices is None or v2_lights is None:
        scene_discovery_failed = True
        log("scene device or light discovery failed; skipping requested multi-color sync")
    else:
        for device in v2_devices:
            if isinstance(device, dict) and _UUID_RE.match(str(device.get("id", ""))):
                devices_by_id[device["id"]] = device
        for light in v2_lights:
            if isinstance(light, dict) and _UUID_RE.match(str(light.get("id", ""))):
                lights_by_id[light["id"]] = light

# ---------------------------------------------------------------------------
# Build v2 bodies
# ---------------------------------------------------------------------------
# Uniform body for grouped_light
uniform_body = {"color": {"xy": {"x": xy_x, "y": xy_y}}}
if cfg.get("bri") is not None:
    bri_v2 = brightness_to_v2(cfg["bri"])
    uniform_body["dimming"] = {"brightness": bri_v2}
if cfg.get("turnOn"):
    uniform_body["on"] = {"on": True}
if transition_ms > 0:
    uniform_body["dynamics"] = {"duration": transition_ms}

# ---------------------------------------------------------------------------
# Send commands
# ---------------------------------------------------------------------------
sent = 0
scenes = 0
for target in targets:
    gl_uuid = target["grouped_light_id"]
    group_name = target["name"] or target["id"]

    if not gl_uuid:
        log("%s %s has no grouped_light; skipping" % (target["kind"], group_name))
        continue

    if uses_scene(target):
        if scene_discovery_failed or not palette_xy or not target["children_valid"]:
            log("scene discovery incomplete for %s; skipping" % group_name)
            continue

        # Scene mode: resolve direct child lights and child device light services.
        color_lids = []
        mapping_valid = True
        for lid in target["light_ids"]:
            light = lights_by_id.get(lid)
            if not isinstance(light, dict):
                mapping_valid = False
                break
            xy = (light.get("color") or {}).get("xy") or {}
            if isinstance(xy.get("x"), (int, float)) and isinstance(xy.get("y"), (int, float)):
                color_lids.append(lid)
        if not mapping_valid:
            log("scene device-to-light mapping incomplete for %s; skipping" % group_name)
            continue
        for did in target["device_ids"]:
            device = devices_by_id.get(did)
            services = device.get("services") if isinstance(device, dict) else None
            if not isinstance(services, list):
                mapping_valid = False
                break
            owns_light = False
            for service in services:
                if not isinstance(service, dict):
                    mapping_valid = False
                    break
                if service.get("rtype") != "light":
                    continue
                owns_light = True
                lid = service.get("rid", "")
                light = lights_by_id.get(lid)
                if not _UUID_RE.match(str(lid)) or not isinstance(light, dict):
                    mapping_valid = False
                    break
                xy = (light.get("color") or {}).get("xy") or {}
                if (isinstance(xy.get("x"), (int, float))
                        and isinstance(xy.get("y"), (int, float))
                        and lid not in color_lids):
                    color_lids.append(lid)
            if not mapping_valid or not owns_light:
                mapping_valid = False
                break
        if not mapping_valid:
            log("scene device-to-light mapping incomplete for %s; skipping" % group_name)
            continue
        if len(color_lids) < 2:
            log("scene requires at least two color lights for %s; skipping" % group_name)
            continue

        done = 0
        for i, lid in enumerate(color_lids):
            px, py = palette_xy[i % len(palette_xy)]
            light_body = {"color": {"xy": {"x": px, "y": py}}}
            if "dimming" in uniform_body:
                light_body["dimming"] = uniform_body["dimming"]
            if "on" in uniform_body:
                light_body["on"] = uniform_body["on"]
            if "dynamics" in uniform_body:
                light_body["dynamics"] = uniform_body["dynamics"]
            if put_resource("light", lid, light_body):
                done += 1
        if done == len(color_lids):
            sent += 1
            scenes += 1
            log("theme scene: %s -> %d light(s), palette #%s" % (group_name, done, color))
        else:
            log("theme scene incomplete: %s -> %d/%d light(s)" % (
                group_name, done, len(color_lids)))
        continue

    # Uniform mode: set the verified room or zone grouped_light.
    if put_resource("grouped_light", gl_uuid, uniform_body):
        sent += 1

log("theme sync: %d/%d group(s) -> #%s" % (sent, len(targets), color))
PY
exit 0
