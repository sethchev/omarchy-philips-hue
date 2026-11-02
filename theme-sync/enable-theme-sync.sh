#!/usr/bin/env bash
# Installs the omarchy theme-set hook that syncs Hue lights to the active
# theme, plus a default hue-theme.json config.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$HERE/45-hue.sh"
CONFIG_SRC="$HERE/hue-theme.json"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
HOOK_DIR="$CONFIG_HOME/omarchy/hooks/theme-set.d"
CONFIG_DIR="$CONFIG_HOME/omarchy/settings"
HOOK_DEST="$HOOK_DIR/45-hue.sh"
CONFIG_DEST="$CONFIG_DIR/hue-theme.json"

[[ -f "$HOOK_SRC" ]] || { echo "error: missing $HOOK_SRC" >&2; exit 1; }

mkdir -p "$HOOK_DIR" "$CONFIG_DIR"

if [[ -f "$HOOK_DEST" ]] && [[ ! -L "$HOOK_DEST" ]]; then
  backup="$HOOK_DEST.bak.$(date +%s)"
  [[ ! -L "$backup" ]] || rm "$backup"
  cp -f "$HOOK_DEST" "$backup"
  echo "Backed up existing hook to $backup"
fi

if [[ -L "$HOOK_DEST" ]]; then
  rm "$HOOK_DEST"
fi
omarchy hook install theme-set "$HOOK_SRC"

if [[ -L "$CONFIG_DEST" ]]; then
  rm "$CONFIG_DEST"
fi
if [[ ! -f "$CONFIG_DEST" ]]; then
  python3 - "$CONFIG_SRC" "$CONFIG_DEST" <<'PYCODE'
import os, sys
with open(sys.argv[1], "rb") as source:
    data = source.read()
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "wb") as target:
    target.write(data)
PYCODE
  echo "Installed $CONFIG_DEST"
else
  chmod 600 "$CONFIG_DEST"
  echo "Kept existing $CONFIG_DEST"
fi

echo
echo "Done. The hook runs automatically on every 'omarchy theme set'."
echo "Test it now with:  bash $HOOK_DEST <theme-slug>"
