#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings"
STATE_FILE="$STATE_DIR/hue.json"
CONFIG_FILE="$HOME/.config/omarchy/settings/hue-theme.json"
HOOK_FILE="$HOME/.config/omarchy/hooks/theme-set.d/45-hue.sh"
HOOK_SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/theme-sync/45-hue.sh"
OWNER_FILE="$STATE_DIR/omarchy-philips-hue-theme-hook.sha256"

removed=0

secure_remove() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  if [[ -L "$f" ]]; then
    rm "$f"
    return 0
  fi
  local owner
  owner=$(stat -c '%U' "$f" 2>/dev/null || true)
  if [[ "$owner" == "$USER" ]]; then
    python3 -c "
import os, stat
try:
    st = os.lstat('''$f''')
    if stat.S_ISREG(st.st_mode) and st.st_size > 0:
        fd = os.open('''$f''', os.O_WRONLY | os.O_NOFOLLOW)
        os.write(fd, b'\x00' * st.st_size)
        os.close(fd)
except Exception:
    pass
" 2>/dev/null || true
  fi
  rm "$f"
}

file_hash() {
  local hash ignored
  read -r hash ignored < <(sha256sum -- "$1")
  printf '%s\n' "$hash"
}

hook_is_owned() {
  [[ -f "$HOOK_FILE" && ! -L "$HOOK_FILE" && -f "$OWNER_FILE" && ! -L "$OWNER_FILE" ]] || return 1
  local recorded
  read -r recorded < "$OWNER_FILE"
  [[ $recorded =~ ^[0-9a-f]{64}$ && $recorded == "$(file_hash "$HOOK_FILE")" ]]
}

if [[ -f "$STATE_FILE" ]]; then
  secure_remove "$STATE_FILE"
  echo "Removed $STATE_FILE"
  removed=$((removed + 1))
fi

if [[ -f "$CONFIG_FILE" ]]; then
  rm -f "$CONFIG_FILE"
  echo "Removed $CONFIG_FILE"
  removed=$((removed + 1))
fi

if [[ -f "$HOOK_FILE" && ! -L "$HOOK_FILE" ]] && { cmp -s "$HOOK_SOURCE" "$HOOK_FILE" || hook_is_owned; }; then
  rm "$HOOK_FILE"
  echo "Removed $HOOK_FILE"
  removed=$((removed + 1))
elif [[ -e "$HOOK_FILE" || -L "$HOOK_FILE" ]]; then
  echo "Left unverified hook $HOOK_FILE untouched."
fi

if [[ -f "$OWNER_FILE" && ! -L "$OWNER_FILE" && ! -e "$HOOK_FILE" && ! -L "$HOOK_FILE" ]]; then
  rm "$OWNER_FILE"
fi

if [[ $removed -eq 0 ]]; then
  echo "No Hue files found to remove."
fi
