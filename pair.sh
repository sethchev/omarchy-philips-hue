#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings"
STATE_FILE="$STATE_DIR/hue.json"
CACERT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/hue_bridge_cacert.pem"
DEVICETYPE="${PHILIPS_HUE_DEVICETYPE:-philips#omarchy-hue}"
DEVICETYPE="${DEVICETYPE//[^a-zA-Z0-9#_-]/}"

BRIDGE_IP="${1:-}"

info() { printf '\033[1;34m::\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m::\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m::\033[0m %s\n' "$*" >&2; }

valid_ip() {
  local IFS='.'
  local parts=($1)
  [[ ${#parts[@]} -eq 4 ]] || return 1
  for part in "${parts[@]}"; do
    [[ "$part" =~ ^[0-9]{1,3}$ ]] || return 1
    (( 10#$part >= 0 && 10#$part <= 255 )) || return 1
  done
}

discover_bridge_local() {
  local out ip bridgeid
  out=$(timeout 5 avahi-browse -t -r _hue._tcp 2>/dev/null || true)
  [[ -z "$out" ]] && return 1
  read -r ip bridgeid <<<"$(python3 -c "
import re, sys
text = sys.stdin.read()
ips = re.findall(r'address = \[([^]]+)\]', text)
ipv4 = [addr for addr in ips if ':' not in addr]
ids = re.findall(r'bridgeid=([0-9a-fA-F]{16})', text)
print((ipv4[0] if ipv4 else (ips[0] if ips else '')), (ids[0].lower() if ids else ''))
" <<<"$out")"
  [[ -n "$ip" ]] || return 1
  printf '%s\t%s\n' "$ip" "$bridgeid"
}

discover_bridge_cloud() {
  local response ip
  response=$(curl -fsS --max-time 5 https://discovery.meethue.com/ 2>/dev/null || true)
  [[ -z "$response" ]] && return 1
  ip=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
ips = [x.get('internalipaddress', '') for x in d if x.get('internalipaddress')]
print(ips[0] if ips else '')
" <<<"$response")
  [[ -n "$ip" ]] || return 1
  printf '%s\n' "$ip"
}

fetch_bridge_id() {
  local ip="$1" bridge_id
  bridge_id=$(CACERT="$CACERT" TARGET_IP="$ip" python3 - <<'PY' 2>/dev/null || true
import json, os, ssl, sys, urllib.request
cacert = os.environ["CACERT"]
target = os.environ["TARGET_IP"]
ctx = ssl.create_default_context(cafile=cacert)
ctx.check_hostname = False
try:
    with urllib.request.urlopen("https://%s/api/config" % target, timeout=5, context=ctx) as r:
        d = json.load(r)
        bid = d.get("bridgeid", "")
        bid = bid.lower() if bid else ""
        if all(c in "0123456789abcdef" for c in bid) and len(bid) == 16:
            print(bid)
except Exception:
    pass
PY
  )
  printf '%s\n' "$bridge_id"
}

pair() {
  local ip="$1" bridge_id="$2" response username
  local now deadline=$(( $(date +%s) + 90 ))
  while :; do
    response=$(curl -fsS --max-time 5 --cacert "$CACERT" \
      --resolve "${bridge_id}:443:${ip}" \
      -X POST -H "Content-Type: application/json" \
      -d "{\"devicetype\":\"$DEVICETYPE\"}" "https://${bridge_id}/api" 2>/dev/null || true)
    username=$(python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for item in d:
        if isinstance(item, dict) and 'success' in item and 'username' in item['success']:
            u = item['success']['username']
            if 20 <= len(u) <= 64 and all(c.isalnum() or c == '-' for c in u):
                print(u)
            break
except Exception:
    pass
" <<<"$response")
    [[ -n "$username" ]] && break
    now=$(date +%s)
    (( now >= deadline )) && break
    sleep 2
  done
  [[ -n "$username" ]] || return 1
  printf '%s\n' "$username"
}

mkdir -p "$STATE_DIR" || {
  echo "Could not create state dir: $STATE_DIR" >&2
  exit 1
}

if [[ -f "$STATE_FILE" ]] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('bridgeIp') else 1)" "$STATE_FILE" 2>/dev/null; then
  info "Existing config found at $STATE_FILE."
  info "Re-pairing will replace the current username. The old username will stop working."
fi

local_ip=""
discovered_bridge_id=""
if [[ -n "$BRIDGE_IP" ]]; then
  local_ip="$BRIDGE_IP"
else
  info "Discovering bridge..."
  discovery=""
  discovery=$(discover_bridge_local) || true
  if [[ -n "$discovery" ]]; then
    local_ip="${discovery%%$'\t'*}"
    discovered_bridge_id="${discovery##*$'\t'}"
  fi
  if [[ -z "$local_ip" ]]; then
    local_ip=$(discover_bridge_cloud) || true
  fi
  if [[ -z "$local_ip" ]]; then
    read -r -p "Couldn't discover the bridge automatically. Enter its IP address: " local_ip </dev/tty
  fi
fi

if [[ -z "$local_ip" ]]; then
  err "No bridge IP. Aborting."
  exit 1
fi

if ! valid_ip "$local_ip"; then
  err "Invalid IP address: $local_ip"
  exit 1
fi

info "Using bridge at $local_ip"

if [[ -n "$discovered_bridge_id" ]]; then
  bridge_id="$discovered_bridge_id"
else
  info "Fetching bridge ID..."
  bridge_id=$(fetch_bridge_id "$local_ip")
  if [[ -z "$bridge_id" ]]; then
    err "Could not fetch bridge ID. Aborting."
    exit 1
  fi
fi
ok "Bridge ID: ${bridge_id:0:8}***"

info "Press the link button on the Hue bridge now, if you haven't. The pairing window stays open ~90 seconds."
info "Requesting access from the bridge..."
username=$(pair "$local_ip" "$bridge_id") || true
if [[ -z "$username" ]]; then
  err "Pairing failed after 90 seconds. Press the link button and try again."
  exit 1
fi
printf '%s\n%s\n%s\n' "$local_ip" "$bridge_id" "$username" | python3 -c "
import json, os, stat, sys
bridge_ip, bridge_id, username = sys.stdin.read().splitlines()[:3]
data = json.dumps({'bridgeIp': bridge_ip, 'bridgeId': bridge_id, 'username': username}, indent=2) + '\n'
path = '''$STATE_FILE'''
try:
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise OSError('refusing credential symlink')
except FileNotFoundError:
    pass
tmp = path + '.tmp.' + str(os.getpid())
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
except:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    raise
" 2>/dev/null || {
  err "Could not write $STATE_FILE."
  exit 1
}
ok "Saved config to $STATE_FILE"

if [[ ! -f "$CACERT" ]]; then
  err "CA cert not found at $CACERT. Cannot verify bridge connection."
  exit 1
fi

info "Verifying access..."
light_count=$(python3 "$(dirname -- "${BASH_SOURCE[0]}")/hue-api.py" verify-v2 2>/dev/null || true)
if [[ -n "$light_count" ]]; then
  ok "Connected. Found $light_count light(s)."
else
  err "Wrote config but couldn't list lights yet. The panel will retry automatically."
fi
