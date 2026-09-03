#!/usr/bin/env python3
"""Hue bridge API helper — reads credentials from file, never exposes
the username in process arguments."""

import json
import os
import re
import socket
import ssl
import sys
import urllib.request

CREDS_FILE = os.path.join(
    os.path.expanduser("~"), ".local/state/omarchy/settings/hue.json")
CONFIG_FILE = os.path.join(
    os.path.expanduser("~"), ".config/omarchy/settings/hue-theme.json")
COLORS_FILE = os.path.join(
    os.path.expanduser("~"), ".local/state/omarchy/current/theme/colors.toml")
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
    with open(CREDS_FILE) as f:
        return json.load(f)


def _bridge_url(creds, path):
    bridge_id = creds.get("bridgeId", "").strip().lower()
    bridge_ip = creds["bridgeIp"]
    username = creds["username"]
    if bridge_id:
        return ("https://%s/api/%s%s" % (bridge_id, username, path),
                bridge_id, bridge_ip)
    return ("https://%s/api/%s%s" % (bridge_ip, username, path),
            None, bridge_ip)


def _request(req_or_url, creds):
    if isinstance(req_or_url, str):
        url, hostname, ip = _bridge_url(creds, req_or_url)
        req = urllib.request.Request(url)
    else:
        url = req_or_url.full_url
        hostname, ip = None, None
        for c in creds, None:
            if c:
                _, hostname, ip = _bridge_url(c, "")
                break
    if hostname and ip:
        opener = _get_opener()
        with _BridgeResolver(hostname, ip):
            with opener.open(req, timeout=5) as r:
                return json.load(r)
    else:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)


def _put(creds, path, body):
    url, hostname, ip = _bridge_url(creds, path)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    if hostname and ip:
        opener = _get_opener()
        with _BridgeResolver(hostname, ip):
            with opener.open(req, timeout=5) as r:
                r.read()
    else:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()


def _hex_to_hsv(hexval):
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


def _accent_color():
    try:
        with open(COLORS_FILE) as f:
            for line in f:
                m = re.match(
                    r'^\s*accent\s*=\s*["\']*#([0-9a-fA-F]{6})["\']*\s*$', line)
                if m:
                    return m.group(1).lower()
    except Exception:
        pass
    return None


def _build_scene_palette(color):
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
        with open(COLORS_FILE) as f:
            for line in f:
                m = re.match(
                    r'^\s*([A-Za-z0-9_]+)\s*=\s*"#([0-9a-fA-F]{6})"\s*$', line)
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
    if palette:
        palette[0] = color  # per-theme override re-colors the accent anchor
    return palette


def _room_or_zone(g):
    return g.get("type") in ("Room", "Zone")


def _try_get(url, hostname, ip):
    try:
        if hostname and ip:
            opener = _get_opener()
            with _BridgeResolver(hostname, ip):
                with opener.open(url, timeout=5) as r:
                    return json.load(r)
        else:
            with urllib.request.urlopen(url, timeout=5) as r:
                return json.load(r)
    except Exception:
        return None


def _try_put(url, body, hostname, ip):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    try:
        if hostname and ip:
            opener = _get_opener()
            with _BridgeResolver(hostname, ip):
                with opener.open(req, timeout=5) as r:
                    r.read()
        else:
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
        return True
    except Exception:
        return False


def _sync_room(room_id):
    """Force-apply the current theme's color to a single room."""
    if not re.fullmatch(r'[0-9A-Za-z_-]{1,40}', room_id):
        return
    cfg = {}
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        return
    if not cfg.get("enabled", True):
        return
    theme_sync = cfg.get("themeSync") or {}
    if theme_sync.get(room_id, True) is False:
        return
    accent = _accent_color()
    if not accent:
        return
    overrides = cfg.get("themes") or {}
    theme_slug = os.environ.get("THEME_SLUG", "")
    color = str(overrides.get(theme_slug) or accent).lstrip("#").lower()
    if not re.fullmatch(r'[0-9a-fA-F]{6}', color):
        return

    creds = _load_creds()
    bridge_id = str(creds.get("bridgeId", "")).strip().lower()
    bridge_ip = creds["bridgeIp"]
    username = creds["username"]
    if bridge_id:
        base = "https://%s/api/%s" % (bridge_id, username)
        hostname = bridge_id
    else:
        base = "https://%s/api/%s" % (bridge_ip, username)
        hostname = None
    ip = bridge_ip if hostname else None

    groups = _try_get(base + "/groups", hostname, ip)
    if not groups or room_id not in groups:
        return
    group = groups[room_id]
    if not _room_or_zone(group):
        return

    hue, sat = _hex_to_hsv(color)
    transition = int(cfg.get("transition", 20) or 20)
    body = {"hue": hue, "sat": sat, "transitiontime": transition}
    if cfg.get("bri") is not None:
        body["bri"] = int(max(1, min(254, cfg["bri"])))
    if cfg.get("turnOn"):
        body["on"] = True

    scene_default = bool(cfg.get("scene", False))
    scene_rooms = cfg.get("sceneRooms") or {}
    scene_ids = []
    if scene_rooms.get(room_id, scene_default):
        lights = _try_get(base + "/lights", hostname, ip) or {}
        def has_color(lid):
            st = lights.get(lid, {}).get("state") or {}
            return (isinstance(st.get("hue"), (int, float))
                    and isinstance(st.get("sat"), (int, float)))
        scene_ids = [lid for lid in group.get("lights", []) if has_color(lid)]
        if len(scene_ids) < 2:
            scene_ids = []

    if scene_ids:
        palette_hs = [_hex_to_hsv(h) for h in _build_scene_palette(color)]
        for i, lid in enumerate(scene_ids):
            hs = palette_hs[i % len(palette_hs)]
            sc = {"hue": hs[0], "sat": hs[1],
                  "transitiontime": body["transitiontime"]}
            if "bri" in body:
                sc["bri"] = body["bri"]
            if "on" in body:
                sc["on"] = body["on"]
            _try_put(base + "/lights/%s/state" % lid, sc, hostname, ip)
        return

    _try_put(base + "/groups/%s/action" % room_id, body, hostname, ip)


def main():
    if len(sys.argv) < 2:
        return
    op = sys.argv[1]

    if op == "sync-room" and len(sys.argv) >= 3:
        _sync_room(sys.argv[2])
        return

    if op == "write-theme-config" and len(sys.argv) >= 3:
        _write_config_map("themeSync", sys.argv[2])
        return

    if op == "write-scene-config" and len(sys.argv) >= 3:
        _write_config_map("sceneRooms", sys.argv[2])
        return

    creds = _load_creds()

    if op == "get-lights":
        print(json.dumps(_request("/lights", creds)))
    elif op == "get-groups":
        print(json.dumps(_request("/groups", creds)))
    elif op == "put-light" and len(sys.argv) >= 4:
        light_id = sys.argv[2]
        if not re.fullmatch(r'[0-9]+', light_id):
            return
        state = json.loads(sys.argv[3])
        _put(creds, "/lights/%s/state" % light_id, state)
    elif op == "put-group" and len(sys.argv) >= 4:
        group_id = sys.argv[2]
        if not re.fullmatch(r'[0-9]+', group_id):
            return
        action = json.loads(sys.argv[3])
        _put(creds, "/groups/%s/action" % group_id, action)
    elif op == "verify":
        lights = _request("/lights", creds)
        print(len(lights))


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
    except Exception:
        pass
