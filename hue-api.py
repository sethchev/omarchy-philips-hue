#!/usr/bin/env python3
"""Hue bridge API helper — reads credentials from file, never exposes
the username in process arguments."""

import json
import ipaddress
import os
import re
import socket
import ssl
import stat
import sys
import urllib.error
import urllib.request

def state_home():
    return os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local/state")


CREDS_FILE = os.path.join(state_home(), "omarchy/settings/hue.json")
CACERT = os.path.join(
    os.path.expanduser("~"),
    ".config/omarchy/plugins/omarchy-philips-hue/hue_bridge_cacert.pem")

_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        ctx = ssl.create_default_context(cafile=CACERT)
        ctx.check_hostname = True
        _opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx))
    return _opener


class _BridgeResolver:
    def __init__(self, hostname, ip):
        self._hostname = hostname
        self._ip = ip
        self._orig = None

    def __enter__(self):
        self._orig = socket.getaddrinfo
        def _patched(host, port, *args, **kwargs):
            if host == self._hostname:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                         (self._ip, port))]
            return self._orig(host, port, *args, **kwargs)
        socket.getaddrinfo = _patched
        return self

    def __exit__(self, *args):
        socket.getaddrinfo = self._orig


def _load_creds():
    fd = os.open(CREDS_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    status = os.fstat(fd)
    if not stat.S_ISREG(status.st_mode):
        os.close(fd)
        raise ValueError("credentials must be a regular file")
    if stat.S_IMODE(status.st_mode) != 0o600:
        os.fchmod(fd, 0o600)
    with os.fdopen(fd) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Hue API v2 helpers
# ---------------------------------------------------------------------------

import math

_V2_PREFIX = "/clip/v2/resource"
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_BRIDGE_ID_RE = re.compile(r'^[0-9a-f]{16}$')


def _v2_url(creds, path):
    bridge_id = str(creds.get("bridgeId", "")).strip().lower()
    bridge_ip = creds.get("bridgeIp")
    username = creds.get("username")
    if not isinstance(bridge_ip, str):
        raise ValueError("invalid bridge IP")
    try:
        ipaddress.IPv4Address(bridge_ip)
    except ipaddress.AddressValueError:
        raise ValueError("invalid bridge IP")
    if bridge_id and not _BRIDGE_ID_RE.fullmatch(bridge_id):
        raise ValueError("invalid bridge ID")
    if not isinstance(username, str) or not username:
        raise ValueError("invalid Hue application key")
    if bridge_id:
        return ("https://%s%s" % (bridge_id, path),
                bridge_id, bridge_ip, username)
    return ("https://%s%s" % (bridge_ip, path),
            None, bridge_ip, username)


def _v2_get(creds, path, timeout=10):
    url, hostname, ip, app_key = _v2_url(creds, path)
    req = urllib.request.Request(url, headers={
        "hue-application-key": app_key,
        "Accept": "application/json",
    })
    if hostname and ip:
        opener = _get_opener()
        with _BridgeResolver(hostname, ip):
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read())
    else:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())


def _v2_put(creds, path, body, timeout=10):
    url, hostname, ip, app_key = _v2_url(creds, path)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "hue-application-key": app_key,
        }, method="PUT")
    if hostname and ip:
        opener = _get_opener()
        with _BridgeResolver(hostname, ip):
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read())
    else:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())


def v2_get_data(creds, resource_type):
    """Fetch all resources of a type from the v2 API.

    Returns the data list from the v2 envelope, or raises on error.
    """
    return _v2_envelope_data(_v2_get(creds, "%s/%s" % (_V2_PREFIX, resource_type)))


def _v2_envelope_data(resp):
    if not isinstance(resp, dict) or "data" not in resp or "errors" not in resp:
        raise ValueError("invalid v2 response envelope")
    if not isinstance(resp["errors"], list) or not isinstance(resp["data"], list):
        raise ValueError("invalid v2 response envelope")
    if resp["errors"]:
        raise ValueError("Hue v2 API returned %d error(s)" % len(resp["errors"]))
    return resp["data"]


def v2_put_resource(creds, resource_type, resource_id, body):
    """Write a validated Hue v2 resource without exposing the app key."""
    if resource_type not in ("light", "grouped_light"):
        raise ValueError("unsupported v2 resource type")
    if not isinstance(resource_id, str) or not _UUID_RE.match(resource_id):
        raise ValueError("invalid v2 resource id")
    return _v2_envelope_data(_v2_put(creds, "%s/%s/%s" % (
        _V2_PREFIX, resource_type, resource_id), body))


def request_error_exit_code(error):
    """Return a non-secret process status for a bridge request failure."""
    if isinstance(error, urllib.error.HTTPError):
        return 2 if error.code in (401, 403) else 6
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLError):
        return 3
    if isinstance(reason, socket.gaierror):
        return 4
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return 5
    return 1


# ---------------------------------------------------------------------------
# V2 transformations
# ---------------------------------------------------------------------------

def brightness_to_v2(bri):
    return round((max(1, min(254, int(bri))) / 254.0) * 100.0, 2)


def brightness_from_v2(bri):
    return max(1, min(254, round((max(0.0, min(100.0, float(bri))) / 100.0) * 254.0)))


def on_payload_v2(on):
    return {"on": {"on": bool(on)}}


def brightness_payload_v2(bri):
    return {"dimming": {"brightness": brightness_to_v2(bri)}}


def color_payload_v2_xy(x, y):
    return {"color": {"xy": {"x": float(x), "y": float(y)}}}


def color_payload_v2_hs(hue, sat):
    h = (float(hue) / 65535.0) * 360.0
    s = float(sat) / 254.0
    hi = int(math.floor(h / 60.0)) % 6
    f = (h / 60.0) - math.floor(h / 60.0)
    p = 1.0 * (1 - s)
    q = 1.0 * (1 - s * f)
    t = 1.0 * (1 - s * (1 - f))
    if hi == 0:
        r, g, b = 1.0, t, p
    elif hi == 1:
        r, g, b = q, 1.0, p
    elif hi == 2:
        r, g, b = p, 1.0, t
    elif hi == 3:
        r, g, b = p, q, 1.0
    elif hi == 4:
        r, g, b = t, p, 1.0
    else:
        r, g, b = 1.0, p, q
    X = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    Y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    Z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    d = X + Y + Z
    if d == 0:
        return color_payload_v2_xy(0.0, 0.0)
    return color_payload_v2_xy(X / d, Y / d)


def ct_payload_v2(ct):
    return {"color_temperature": {"mirek": max(153, min(500, int(ct)))}}


def _convert_control_payload_to_v2(body):
    """Convert the panel's compact light control payload to v2 format.

    Accepts the panel keys: on, bri, ct, hue, sat
    Returns a v2-compatible dict for PUT /clip/v2/resource/light/<uuid>

    Detects already-v2 payloads and passes them through unchanged.
    """
    if not isinstance(body, dict):
        return body
    is_v2 = ("on" in body and isinstance(body["on"], dict)) or "dimming" in body or "color_temperature" in body or "color" in body
    if is_v2:
        return body
    parts = []
    if "on" in body:
        parts.append(on_payload_v2(body["on"]))
    if "bri" in body:
        parts.append(brightness_payload_v2(body["bri"]))
    if "ct" in body:
        parts.append(ct_payload_v2(body["ct"]))
    if "hue" in body and "sat" in body:
        parts.append(color_payload_v2_hs(body["hue"], body["sat"]))
    elif "hue" in body:
        parts.append(color_payload_v2_hs(body["hue"], 254))
    elif "sat" in body:
        parts.append(color_payload_v2_hs(0, body["sat"]))
    if not parts:
        return body
    result = {}
    for p in parts:
        result.update(p)
    return result


def extract_grouped_light_uuid(room):
    if not isinstance(room, dict):
        return None
    services = room.get("services")
    if not isinstance(services, list):
        return None
    for svc in services:
        if (isinstance(svc, dict)
                and svc.get("rtype") == "grouped_light"
                and isinstance(svc.get("rid"), str)
                and _UUID_RE.match(svc["rid"])):
            return svc["rid"]
    return None


def parse_v2_envelope(text):
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty response")
    obj = json.loads(raw)
    return _v2_envelope_data(obj)


def parse_light_v2(resource):
    if not isinstance(resource, dict):
        return None
    on_state = resource.get("on") or {}
    if not isinstance(on_state, dict):
        on_state = {}
    dimming = resource.get("dimming") or {}
    if not isinstance(dimming, dict):
        dimming = {}
    ct = resource.get("color_temperature") or {}
    if not isinstance(ct, dict):
        ct = {}
    color = resource.get("color") or {}
    if not isinstance(color, dict):
        color = {}
    xy = color.get("xy") or {}
    if not isinstance(xy, dict):
        xy = {}
    brightness = dimming.get("brightness", 0)
    mirek = ct.get("mirek")
    mirek_valid = ct.get("mirek_valid", False)
    x = xy.get("x")
    y = xy.get("y")
    has_ct = mirek_valid and isinstance(mirek, (int, float))
    has_xy = isinstance(x, (int, float)) and isinstance(y, (int, float))
    bri = brightness_from_v2(brightness) if brightness > 0 else 0
    return {
        "id": resource.get("id", ""),
        "name": resource.get("metadata", {}).get("name", "Light"),
        "on": bool(on_state.get("on")),
        "bri": bri,
        "hasBri": True,
        "ct": mirek if has_ct else 0,
        "hasCt": has_ct,
        "hue": 0,
        "sat": 0,
        "hasColor": has_xy,
        "colormode": "ct" if has_ct and not has_xy else "hs",
        "xy": [x, y] if has_xy else [],
        "pickerOpen": False,
    }


def parse_room_v2(resource):
    if not isinstance(resource, dict):
        return None
    metadata = resource.get("metadata") or {}
    children = resource.get("children") or []
    device_ids = []
    for child in children:
        if isinstance(child, dict) and child.get("rtype") == "device":
            rid = child.get("rid")
            if isinstance(rid, str) and _UUID_RE.match(rid):
                device_ids.append(rid)
    return {
        "id": resource.get("id", ""),
        "name": metadata.get("name", "Room"),
        "type": "Room",
        "on": False,
        "allOn": False,
        "lightIds": device_ids,
        "groupedLightId": extract_grouped_light_uuid(resource),
    }


# ---------------------------------------------------------------------------
# Main CLI dispatcher
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        return
    op = sys.argv[1]

    if op == "write-theme-config" and len(sys.argv) >= 3:
        _write_config_map("themeSync", sys.argv[2])
        return

    if op == "write-scene-config" and len(sys.argv) >= 3:
        _write_config_map("sceneRooms", sys.argv[2])
        return

    creds = _load_creds()

    # V2 operations
    if op == "get-lights-v2":
        data = v2_get_data(creds, "light")
        print(json.dumps(data))
    elif op == "get-rooms-v2":
        data = v2_get_data(creds, "room")
        print(json.dumps(data))
    elif op == "get-zones-v2":
        data = v2_get_data(creds, "zone")
        print(json.dumps(data))
    elif op == "get-scenes-v2":
        data = v2_get_data(creds, "scene")
        print(json.dumps(data))
    elif op == "get-grouped-lights-v2":
        data = v2_get_data(creds, "grouped_light")
        print(json.dumps(data))
    elif op == "get-devices-v2":
        data = v2_get_data(creds, "device")
        print(json.dumps(data))
    elif op == "put-light-v2" and len(sys.argv) >= 4:
        light_uuid = sys.argv[2]
        if not _UUID_RE.match(light_uuid):
            return
        body = json.loads(sys.argv[3])
        v2_body = _convert_control_payload_to_v2(body)
        v2_put_resource(creds, "light", light_uuid, v2_body)
    elif op == "put-grouped-light-v2" and len(sys.argv) >= 4:
        gl_uuid = sys.argv[2]
        if not _UUID_RE.match(gl_uuid):
            return
        body = json.loads(sys.argv[3])
        v2_body = _convert_control_payload_to_v2(body)
        v2_put_resource(creds, "grouped_light", gl_uuid, v2_body)
    elif op == "verify-v2":
        data = v2_get_data(creds, "light")
        print(len(data))


def _write_config_map(key, map_json):
    settings = json.loads(map_json)
    if not isinstance(settings, dict):
        return
    for rid in settings:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', str(rid)):
            return
        if not isinstance(settings[rid], bool):
            return
    config_path = os.path.join(
        os.path.expanduser("~"), ".config/omarchy/settings/hue-theme.json")
    cfg = {}
    try:
        fd = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    except (OSError, ValueError):
        pass
    cfg[key] = settings
    try:
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            fd = os.open(config_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            os.fchmod(fd, 0o600)
        except (OSError, ValueError):
            try:
                os.remove(config_path)
            except OSError:
                pass
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(request_error_exit_code(error))
