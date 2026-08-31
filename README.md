# omarchy-philips-hue

Omarchy / Quickshell bar widget for controlling Philips Hue lights over the bridge's local Hue API v2.

<p align="center">
  <img src="preview.png" alt="omarchy-philips-hue panel screenshot" width="360">
</p>

## Features

- Bar icon (lightbulb) that opens a control panel
- Toggle all groups, individual Rooms/Zones, or single lights
- Every group and light row is tinted with the bulb's current color
  (hue/sat, color temperature, or XY as reported by the bridge)
- Per-light brightness slider
- Per-light color temperature slider (warm ⇄ cool white)
- Per-light color wheel picker (hue + saturation) and color temperature slider;
  both hidden for lights in groups with theme sync enabled
- Reads credentials from `~/.local/state/omarchy/settings/hue.json`
- Retries / re-fetches state automatically after every change

## Requirements

- Arch Linux + Omarchy (Quickshell-based shell)
- `curl`, `python3` (for the pairing script), `omarchy-shell`

## Install

```sh
omarchy plugin add https://github.com/sethchev/omarchy-philips-hue.git --enable
```

## Pairing with the bridge

On first run the bar shows a lightbulb icon. Click it to open the panel, then
click **Pair with bridge**. This opens a terminal — press the link button on
your Hue bridge when prompted. The script discovers the bridge, requests a
username, and writes `~/.local/state/omarchy/settings/hue.json`. The panel
picks up the new credentials automatically within seconds.

You can also pair manually:

```sh
~/.config/omarchy/plugins/omarchy-philips-hue/pair.sh
```

Pass an IP directly to skip auto-discovery: `pair.sh 192.168.1.14`.

## Syncing lights with the omarchy theme

A theme-set hook (`45-hue.sh`, vendored in `theme-sync/`) recolors every
room/zone from the active theme whenever you run `omarchy theme set`: either
the accent color, or — when scenes are enabled — a per-light scene built from
the theme's palette. The bar widget picks the change up within its 15 s poll.

### Per-group opt-out

Each room or zone that is switched on gets a **Theme Sync** toggle in the panel,
right below its own toggle. Every group starts out synced; toggling a group
off excludes it from theme changes until you re-enable it.

Groups with at least two color-capable lights also get a **Scene Mode** toggle
next to it. With Scene Mode on, the group's lights are colored from the theme's
palette instead of one uniform accent (see [Theme scenes](#theme-scenes)).

While a Room or Zone is synced, its lights' color wheel and color temperature
slider are hidden in the panel — the hook owns their color, so manual
picking would be overwritten anyway. Groups with sync off keep full manual
control.

The toggle states live under the `themeSync` key of `hue-theme.json`
(missing group = enabled), and are picked up by the hook immediately — no
restart needed.

Pairing registers the hook with Omarchy automatically. Existing installations
can register it once without overwriting their Hue settings:

```sh
bash ~/.config/omarchy/plugins/omarchy-philips-hue/theme-sync/install.sh
```

It uses `omarchy hook install theme-set` and leaves an existing non-plugin
hook or symlink at the same path untouched.

Behavior is configured in `~/.config/omarchy/settings/hue-theme.json`:

```json
{
  "enabled": true,
  "transition": 20,
  "groups": ["all"],
  "bri": null,
  "turnOn": false,
  "themes": {},
  "scene": false,
  "sceneRooms": {},
  "themeSync": {}
}
```

- `transition` — fade length in tenths of a second (20 = 2 s)
- `groups` — `["all"]`, or exact room/zone names or resource UUIDs to sync; duplicate names must use UUIDs
- `bri` — optional forced brightness (1–254); leave `null` to keep each light's current brightness
- `turnOn` — `true` to turn lights on when syncing; `false` leaves on/off state untouched
- `themes` — per-theme hex overrides, e.g. `{ "spacehaven": "#0c8184" }`; themes without an override use their own `accent`
- `scene` — `true` to enable theme scenes globally; off by default (`false`). Groups missing from `sceneRooms` follow this value
- `sceneRooms` — per-group scene override map keyed by room/zone resource UUID, written by the panel's Scene Mode toggles
- `themeSync` — per-group opt-out map keyed by room/zone resource UUID, written by the panel's Theme Sync toggles; groups missing from the map are synced

Existing numeric v1 keys in `themeSync` and `sceneRooms` are translated only
when their v2 resource exposes a unique matching `id_v1`; otherwise sync is
skipped rather than targeting the wrong group.

### Theme scenes

When a synced Room or Zone has two or more color-capable lights and Scene Mode
is on for it, the hook stops painting the whole group one color and instead maps
the theme's palette onto the group's lights, one hue per light. Scene Mode
requires at least two color-capable lights; otherwise the hook skips that group
rather than issuing a uniform grouped-light write. Groups with Scene Mode off
use the uniform accent.

The scene palette is built from `colors.toml`:

1. `accent` first (a `themes` override re-colors just this anchor)
2. the named palette colors in file order (`red`, `yellow`, `green`, `cyan`,
   `blue`, `magenta`, then `bright_*`)
3. any other plain `#rrggbb` keys in file order

Keys that describe surfaces rather than lights (backgrounds, foregrounds,
`selection`, `muted`, borders, tabs) are skipped, and duplicate hexes are
collapsed. Colors are assigned in group light order; light #1 always gets the
accent. If a group has more lights than the palette, the palette cycles.

`transition`, `bri`, and `turnOn` behave the same as in uniform mode, applied
per light, so scenes fade in together. The bridge applies the writes
immediately; the panel's rows and the group swatch pick the scene up within the
normal 15 s poll.

Test the hook without changing your theme:

```sh
bash ~/.config/omarchy/hooks/theme-set.d/45-hue.sh <theme-slug>
```

## Remove

```sh
~/.config/omarchy/plugins/omarchy-philips-hue/cleanup.sh
omarchy plugin remove omarchy-philips-hue
```

The cleanup script removes your bridge credentials from
`~/.local/state/omarchy/settings/hue.json`. Run it before removing the
plugin so no auth token is left behind.

## Notes

- Runtime control uses the Hue API v2 over **HTTPS** on your LAN
  (`/clip/v2/resource`) — no cloud, no SDK.
- TLS is verified with the bundled `hue_bridge_cacert.pem`, the official
  Philips Hue root CA from Signify. During pairing, the bridge's unique ID
  is read from `/api/config` and saved as `bridgeId` in `hue.json`; requests
  are then addressed to that ID so the bridge certificate's hostname is
  matched, while the connection itself goes straight to the bridge's IP.
- Automatic discovery tries **mDNS** first (`avahi-browse -t -r _hue._tcp`,
  the same on-LAN mechanism the official Hue app uses), then falls back to
  Philips' hosted lookup `discovery.meethue.com`. Pass an IP directly to
  `pair.sh` to skip auto-discovery entirely.
- If `bridgeId` is missing (e.g. from an older config), the panel warns
  "TLS verification disabled" — re-run `pair.sh` to restore full
  certificate verification.
- Pairing uses Hue's legacy local enrollment endpoints only to read the bridge
  ID and create the application key. Normal panel and theme-sync control use
  v2 exclusively.
- Credentials are stored per-user in `~/.local/state/omarchy/settings/hue.json`;
  keep that file out of version control.
