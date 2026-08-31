#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$HERE/45-hue.sh"
CONFIG_SRC="$HERE/hue-theme.json"
CONFIG_DIR="$HOME/.config/omarchy/settings"
HOOK_DEST="$HOME/.config/omarchy/hooks/theme-set.d/45-hue.sh"
CONFIG_DEST="$CONFIG_DIR/hue-theme.json"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings"
OWNER_FILE="$STATE_DIR/omarchy-philips-hue-theme-hook.sha256"

[[ -f "$HOOK_SRC" ]] || { echo "error: missing $HOOK_SRC" >&2; exit 1; }

file_hash() {
  local hash ignored
  read -r hash ignored < <(sha256sum -- "$1")
  printf '%s\n' "$hash"
}

owner_hash() {
  [[ -f "$OWNER_FILE" && ! -L "$OWNER_FILE" ]] || return 1
  local hash
  read -r hash < "$OWNER_FILE"
  [[ $hash =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$hash"
}

hook_is_owned() {
  [[ -f "$HOOK_DEST" && ! -L "$HOOK_DEST" ]] || return 1
  [[ $(file_hash "$HOOK_DEST") == "$(owner_hash)" ]]
}

record_hook_ownership() {
  if [[ -e "$OWNER_FILE" || -L "$OWNER_FILE" ]] && ! owner_hash >/dev/null; then
    echo "error: refusing to replace invalid ownership record $OWNER_FILE" >&2
    return 1
  fi
  mkdir -p "$STATE_DIR"
  local temporary
  temporary=$(mktemp "$STATE_DIR/.omarchy-philips-hue-theme-hook.XXXXXX")
  (umask 077; printf '%s\n' "$(file_hash "$HOOK_DEST")" > "$temporary")
  chmod 600 "$temporary"
  mv -f -- "$temporary" "$OWNER_FILE"
}

if [[ -e "$OWNER_FILE" || -L "$OWNER_FILE" ]] && ! owner_hash >/dev/null; then
  echo "error: refusing to replace invalid ownership record $OWNER_FILE" >&2
  exit 1
fi

if [[ -L "$HOOK_DEST" ]]; then
  echo "error: refusing symlink hook $HOOK_DEST" >&2
  exit 1
elif [[ -f "$HOOK_DEST" ]]; then
  if cmp -s "$HOOK_SRC" "$HOOK_DEST"; then
    record_hook_ownership
    echo "Theme Sync hook already installed."
  elif hook_is_owned; then
    omarchy hook install theme-set "$HOOK_SRC"
    record_hook_ownership
  else
    echo "error: refusing to replace unverified hook $HOOK_DEST" >&2
    exit 1
  fi
elif [[ -e "$HOOK_DEST" ]]; then
  echo "error: refusing non-regular hook $HOOK_DEST" >&2
  exit 1
else
  omarchy hook install theme-set "$HOOK_SRC"
  record_hook_ownership
fi

if [[ ! -e "$CONFIG_DEST" && ! -L "$CONFIG_DEST" ]]; then
  mkdir -p "$CONFIG_DIR"
  install -m 600 "$CONFIG_SRC" "$CONFIG_DEST"
fi
