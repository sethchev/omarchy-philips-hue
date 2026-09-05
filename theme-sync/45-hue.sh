#!/usr/bin/env bash
# Keep this hook intentionally thin: all v1/v2 behavior lives in the plugin's
# shared Python client so the panel and theme sync cannot drift apart.
set -u

THEME_SLUG="${1:-}"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/omarchy-philips-hue"
HELPER="$PLUGIN_DIR/hue-api.py"

[[ -f "$HELPER" ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
[[ -f "${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings/hue.json" ]] || exit 0

python3 "$HELPER" theme-sync "$THEME_SLUG" >/dev/null 2>&1 || true
exit 0
