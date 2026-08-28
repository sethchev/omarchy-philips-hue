#!/usr/bin/env bash
if [[ -f "${THPM_THEME_ENV:-$HOME/.local/share/thpm/lib/theme-env.sh}" ]]; then
  source "${THPM_THEME_ENV:-$HOME/.local/share/thpm/lib/theme-env.sh}"
fi
set -u

THEME_SLUG="${1:-}"
CONFIG_FILE="$HOME/.config/omarchy/settings/hue-theme.json"
CREDS_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings/hue.json"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy"
LOG_FILE="$LOG_DIR/hue-theme-hook.log"

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

COLORS_FILE="$HOME/.local/state/omarchy/current/theme/colors.toml"
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
  python3 - "$LOG_FILE" <<'PY'
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.request

log_file = sys.argv[1]
theme_slug = os.environ.get("THEME_SLUG", "")
accent = os.environ.get("ACCENT_COLOR", "").lstrip("#")
config_file = os.environ.get("CONFIG_FILE", "")
creds_file = os.path.expanduser("~/.local/state/omarchy/settings/hue.json")

cacert_file = ""
_plugin_dir = os.path.join(os.path.expanduser("~"),
                           ".config/omarchy/plugins/omarchy-philips-hue")
_candidate = os.path.join(_plugin_dir, "hue_bridge_cacert.pem")
if os.path.isfile(_candidate):
    cacert_file = os.path.abspath(_candidate)


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


class _BridgeResolver:
    """Context manager that makes hostname resolve to a specific IP."""
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


_opener = None


def _get_opener(hostname, cafile):
    global _opener
    if _opener is None:
        ctx = ssl.create_default_context(cafile=cafile)
        ctx.check_hostname = True
        _opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx))
    return _opener


def get_json(url, hostname=None):
    try:
        if hostname and cacert_file:
            opener = _get_opener(hostname, cacert_file)
            with _BridgeResolver(hostname, bridge_ip):
                with opener.open(url, timeout=5) as r:
                    return json.load(r)
        else:
            with urllib.request.urlopen(url, timeout=5) as r:
                return json.load(r)
    except Exception as e:
        safe_url = re.sub(r'/api/[^/]+/', '/api/***/', url)
        safe_e = re.sub(r'/api/[^/]+/', '/api/***/', str(e))
        log("bridge request failed %s: %s" % (safe_url, safe_e))
        return None


def put_url(url, data, hostname=None):
    req = urllib.request.Request(
        url, data=data.encode(), headers={"Content-Type": "application/json"},
        method="PUT")
    if hostname and cacert_file:
        opener = _get_opener(hostname, cacert_file)
        with _BridgeResolver(hostname, bridge_ip):
            with opener.open(req, timeout=5) as r:
                r.read()
    else:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()


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

creds = read_json(creds_file)
if not creds or not creds.get("bridgeIp") or not creds.get("username"):
    sys.exit(0)

bridge_id = str(creds.get("bridgeId") or "").strip().lower()
bridge_ip = creds["bridgeIp"]

cfg = read_json(config_file) or {}
if not cfg.get("enabled", True):
    sys.exit(0)

overrides = cfg.get("themes") or {}
color = str(overrides.get(theme_slug) or accent).lstrip("#")
if not color or len(color) != 6:
    log("invalid color '%s'; skipping hue theme sync" % color)
    sys.exit(0)

hue, sat = hex_to_hsv(color)
transition = int(cfg.get("transition", 20) or 20)

if bridge_id and cacert_file:
    base = "https://%s/api/%s" % (bridge_id, creds["username"])
    hostname = bridge_id
else:
    base = "https://%s/api/%s" % (bridge_ip, creds["username"])
    hostname = None

groups = get_json(base + "/groups", hostname=hostname)
if groups is None:
    log("bridge unreachable; skipping hue theme sync")
    sys.exit(0)


def room_or_zone(g):
    return g.get("type") in ("Room", "Zone")


configured = cfg.get("groups")
if configured and "all" not in configured:
    names = [str(n).strip().lower() for n in configured if str(n) and str(n).strip() and str(n).lower() != "all"]
    if names:
        targets = [gid for gid, g in groups.items()
                   if room_or_zone(g)
                   and any(n in str(g.get("name", "")).lower() for n in names)]
    else:
        targets = [gid for gid, g in groups.items() if room_or_zone(g)]
else:
    targets = [gid for gid, g in groups.items() if room_or_zone(g)]

# Per-room theme sync toggle: filter targets based on themeSync dict
theme_sync = cfg.get("themeSync") or {}
targets = [gid for gid in targets if theme_sync.get(gid, True)]

body = {"hue": hue, "sat": sat, "transitiontime": transition}
if cfg.get("bri") is not None:
    body["bri"] = int(max(1, min(254, cfg["bri"])))
if cfg.get("turnOn"):
    body["on"] = True

sent = 0
for gid in targets:
    try:
        put_url(base + "/groups/%s/action" % gid, json.dumps(body),
                hostname=hostname)
        sent += 1
    except Exception as e:
        safe_e = re.sub(r'/api/[^/]+/', '/api/***/', str(e))
        log("group %s failed: %s" % (gid, safe_e))

log("theme sync: %d/%d group(s) -> #%s" % (sent, len(targets), color))
PY
exit 0
