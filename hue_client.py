#!/usr/bin/env python3
"""Shared Philips Hue local API v1/v2 client and normalization layer."""

from __future__ import annotations

import colorsys
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
CACERT = PLUGIN_DIR / "hue_bridge_cacert.pem"
CREDS_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy/settings/hue.json"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy/settings/hue-theme.json"
COLORS_FILE = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
LOG_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy/hue-theme-hook.log"

_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,36}$")


class HueError(RuntimeError):
    pass


class _BridgeResolver:
    """Temporarily resolve the certificate hostname directly to the bridge IP."""

    def __init__(self, hostname: str, ip: str):
        self.hostname = hostname
        self.ip = ip
        self.original = None

    def __enter__(self):
        self.original = socket.getaddrinfo

        def patched(host, port, *args, **kwargs):
            if host == self.hostname:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.ip, port))]
            return self.original(host, port, *args, **kwargs)

        socket.getaddrinfo = patched
        return self

    def __exit__(self, *_args):
        socket.getaddrinfo = self.original


def read_json(path: Path, default=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def write_json_secure(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2) + "\n").encode()
    temporary = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def configured_api_version(creds: dict) -> str:
    version = str(creds.get("apiVersion") or "v1").lower()
    return version if version in ("v1", "v2") else "v1"


def _logical_id(resource: dict, prefix: str) -> str:
    id_v1 = str(resource.get("id_v1") or "")
    marker = "/%s/" % prefix
    if id_v1.startswith(marker):
        suffix = id_v1[len(marker):]
        if _SAFE_ID.fullmatch(suffix):
            return suffix
    return str(resource.get("id") or "")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _xy_to_rgb(x: float, y: float) -> tuple[float, float, float]:
    if y <= 0:
        return 0.0, 0.0, 0.0
    z = 1.0 - x - y
    X = x / y
    Y = 1.0
    Z = max(0.0, z / y)
    red = X * 3.2404542 + Y * -1.5371385 + Z * -0.4985314
    green = X * -0.9692660 + Y * 1.8760108 + Z * 0.0415560
    blue = X * 0.0556434 + Y * -0.2040259 + Z * 1.0572252

    def srgb(component):
        component = _clamp(component, 0.0, 1.0)
        return 12.92 * component if component <= 0.0031308 else 1.055 * component ** (1 / 2.4) - 0.055

    return srgb(red), srgb(green), srgb(blue)


def _hsv_to_xy(hue: float, saturation: float, gamut=None) -> tuple[float, float]:
    red, green, blue = colorsys.hsv_to_rgb((hue % 65536) / 65535.0, _clamp(saturation / 254.0, 0, 1), 1.0)

    def linear(component):
        return component / 12.92 if component <= 0.04045 else ((component + 0.055) / 1.055) ** 2.4

    red, green, blue = linear(red), linear(green), linear(blue)
    X = red * 0.664511 + green * 0.154324 + blue * 0.162028
    Y = red * 0.283881 + green * 0.668433 + blue * 0.047685
    Z = red * 0.000088 + green * 0.072310 + blue * 0.986039
    total = X + Y + Z
    point = (0.3127, 0.3290) if total <= 0 else (X / total, Y / total)
    return _clip_to_gamut(point, gamut)


def _gamut_points(gamut):
    if not isinstance(gamut, dict):
        return None
    try:
        return tuple((float(gamut[name]["x"]), float(gamut[name]["y"])) for name in ("red", "green", "blue"))
    except (KeyError, TypeError, ValueError):
        return None


def _clip_to_gamut(point, gamut):
    triangle = _gamut_points(gamut)
    if not triangle:
        return point
    red, green, blue = triangle

    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    inside = not ((sign(point, red, green) < 0) or (sign(point, green, blue) < 0) or (sign(point, blue, red) < 0)) or not ((sign(point, red, green) > 0) or (sign(point, green, blue) > 0) or (sign(point, blue, red) > 0))
    if inside:
        return point

    def closest(p, a, b):
        ab = (b[0] - a[0], b[1] - a[1])
        length = ab[0] ** 2 + ab[1] ** 2
        if length == 0:
            return a
        scale = _clamp(((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / length, 0, 1)
        return a[0] + scale * ab[0], a[1] + scale * ab[1]

    candidates = (closest(point, red, green), closest(point, green, blue), closest(point, blue, red))
    return min(candidates, key=lambda p: (p[0] - point[0]) ** 2 + (p[1] - point[1]) ** 2)


class HueClient:
    def __init__(self, creds: dict | None = None, api_version: str | None = None):
        self.creds = creds or read_json(CREDS_FILE)
        if not isinstance(self.creds, dict) or not self.creds.get("bridgeIp") or not self.creds.get("username"):
            raise HueError("Hue bridge is not paired")
        self.api_version = api_version or configured_api_version(self.creds)
        if self.api_version not in ("v1", "v2"):
            raise HueError("Unsupported Hue API version")
        self.bridge_ip = str(self.creds["bridgeIp"])
        self.bridge_id = str(self.creds.get("bridgeId") or "").strip().lower()
        self.username = str(self.creds["username"])
        self._opener = None

    def _url(self, path: str) -> tuple[str, str | None]:
        hostname = self.bridge_id or self.bridge_ip
        if self.api_version == "v1":
            return "https://%s/api/%s%s" % (hostname, self.username, path), self.bridge_id or None
        return "https://%s/clip/v2/resource%s" % (hostname, path), self.bridge_id or None

    def _get_opener(self, verify_hostname: bool):
        key = verify_hostname
        if not self._opener or self._opener[0] != key:
            context = ssl.create_default_context(cafile=str(CACERT))
            context.check_hostname = verify_hostname
            self._opener = (key, urllib.request.build_opener(urllib.request.HTTPSHandler(context=context)))
        return self._opener[1]

    def _request(self, method: str, path: str, body=None):
        url, certificate_hostname = self._url(path)
        headers = {"Accept": "application/json"}
        if self.api_version == "v2":
            headers["hue-application-key"] = self.username
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            opener = self._get_opener(bool(certificate_hostname))
            if certificate_hostname:
                with _BridgeResolver(certificate_hostname, self.bridge_ip):
                    with opener.open(request, timeout=5) as response:
                        result = json.load(response)
            else:
                with opener.open(request, timeout=5) as response:
                    result = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise HueError("Bridge request failed: %s" % type(error).__name__) from error
        self._check_response(result)
        return result

    def _check_response(self, result):
        if self.api_version == "v2":
            errors = result.get("errors", []) if isinstance(result, dict) else []
            if errors:
                description = errors[0].get("description", "Hue v2 request failed") if isinstance(errors[0], dict) else "Hue v2 request failed"
                raise HueError(description)
        elif isinstance(result, list):
            errors = [item.get("error") for item in result if isinstance(item, dict) and item.get("error")]
            if errors:
                raise HueError(str(errors[0].get("description") or "Hue v1 request failed"))

    def _v2_data(self, path="") -> list[dict]:
        result = self._request("GET", path)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise HueError("Invalid Hue v2 response")
        return data

    def get_state(self) -> dict:
        if self.api_version == "v1":
            return self._get_v1_state()
        return self._normalize_v2(self._v2_data())

    def get_lights(self) -> dict:
        if self.api_version == "v1":
            return self._normalize_v1_lights(self._request("GET", "/lights"))
        return self._normalize_v2(self._v2_data("/light"))["lights"]

    def get_groups(self) -> dict:
        if self.api_version == "v1":
            return self._normalize_v1_groups(self._request("GET", "/groups"))
        # Room membership is expressed through device owners, so group
        # normalization needs the related light resources as well.
        return self._normalize_v2(self._v2_data())["groups"]

    def _normalize_v1_lights(self, lights: dict) -> dict:
        if not isinstance(lights, dict):
            raise HueError("Invalid Hue v1 lights response")
        normalized = {}
        for light_id, light in lights.items():
            item = dict(light)
            item["api_id"] = str(light_id)
            capabilities = item.get("capabilities") or {}
            control = capabilities.get("control") or {}
            ct_schema = control.get("ct") or {}
            state = item.get("state") or {}
            item["has_bri"] = isinstance(state.get("bri"), (int, float))
            item["has_ct"] = isinstance(state.get("ct"), (int, float))
            item["has_color"] = isinstance(state.get("hue"), (int, float)) and isinstance(state.get("sat"), (int, float))
            item["ct_min"] = int(ct_schema.get("min") or 153)
            item["ct_max"] = int(ct_schema.get("max") or 500)
            item["gamut"] = control.get("colorgamut") or {}
            normalized[str(light_id)] = item
        return normalized

    def _normalize_v1_groups(self, groups: dict) -> dict:
        if not isinstance(groups, dict):
            raise HueError("Invalid Hue v1 groups response")
        normalized = {}
        for group_id, group in groups.items():
            item = dict(group)
            item["api_id"] = str(group_id)
            item["control_id"] = str(group_id)
            normalized[str(group_id)] = item
        return normalized

    def _get_v1_state(self) -> dict:
        lights = self._normalize_v1_lights(self._request("GET", "/lights"))
        groups = self._normalize_v1_groups(self._request("GET", "/groups"))
        return {"apiVersion": "v1", "lights": lights, "groups": groups}

    def _normalize_v2(self, resources: list[dict]) -> dict:
        by_type: dict[str, list[dict]] = {}
        for resource in resources:
            if isinstance(resource, dict):
                by_type.setdefault(str(resource.get("type") or ""), []).append(resource)

        lights = {}
        device_lights: dict[str, list[str]] = {}
        for resource in by_type.get("light", []):
            logical_id = _logical_id(resource, "lights")
            if not logical_id:
                continue
            on = bool((resource.get("on") or {}).get("on"))
            dimming = resource.get("dimming") or {}
            has_bri = isinstance(dimming.get("brightness"), (int, float))
            bri = round(_clamp(float(dimming.get("brightness") or 0), 0, 100) * 254 / 100) if has_bri else 0
            temperature = resource.get("color_temperature") or {}
            mirek = temperature.get("mirek")
            has_ct = "mirek" in temperature or "mirek_schema" in temperature
            schema = temperature.get("mirek_schema") or {}
            ct_min = int(schema.get("mirek_minimum") or 153)
            ct_max = int(schema.get("mirek_maximum") or 500)
            ct = int(_clamp(float(mirek if isinstance(mirek, (int, float)) else ct_min), ct_min, ct_max)) if has_ct else 0
            color = resource.get("color") or {}
            xy_value = color.get("xy") or {}
            has_color = isinstance(xy_value.get("x"), (int, float)) and isinstance(xy_value.get("y"), (int, float))
            xy = [float(xy_value["x"]), float(xy_value["y"])] if has_color else []
            hue, sat = 0, 0
            if has_color:
                red, green, blue = _xy_to_rgb(xy[0], xy[1])
                hue01, saturation, _value = colorsys.rgb_to_hsv(red, green, blue)
                hue, sat = round(hue01 * 65535), round(saturation * 254)
            metadata = resource.get("metadata") or {}
            owner = resource.get("owner") or {}
            device_id = str(owner.get("rid") or "") if owner.get("rtype") == "device" else ""
            lights[logical_id] = {
                "name": str(metadata.get("name") or "Light %s" % logical_id),
                "api_id": str(resource.get("id") or logical_id),
                "device_id": device_id,
                "state": {
                    "on": on, "bri": max(1, bri) if has_bri else 0,
                    "ct": ct, "hue": hue, "sat": sat,
                    "xy": xy, "colormode": "xy" if has_color else ("ct" if has_ct else "")
                },
                "has_bri": has_bri,
                "has_ct": has_ct,
                "has_color": has_color,
                "ct_min": ct_min,
                "ct_max": ct_max,
                "gamut": color.get("gamut") or {},
            }
            if device_id:
                device_lights.setdefault(device_id, []).append(logical_id)

        grouped = {str(item.get("id")): item for item in by_type.get("grouped_light", []) if item.get("id")}
        groups = {}
        for resource_type, label in (("room", "Room"), ("zone", "Zone")):
            for resource in by_type.get(resource_type, []):
                logical_id = _logical_id(resource, "groups")
                if not logical_id:
                    continue
                children = resource.get("children") or []
                light_ids = []
                for child in children:
                    if isinstance(child, dict) and child.get("rtype") == "device":
                        light_ids.extend(device_lights.get(str(child.get("rid") or ""), []))
                light_ids = list(dict.fromkeys(light_ids))
                services = resource.get("services") or []
                control_id = next((str(service.get("rid")) for service in services if isinstance(service, dict) and service.get("rtype") == "grouped_light"), "")
                states = [bool(lights[item]["state"]["on"]) for item in light_ids if item in lights]
                aggregate = grouped.get(control_id, {})
                any_on = any(states) if states else bool((aggregate.get("on") or {}).get("on"))
                all_on = all(states) if states else any_on
                groups[logical_id] = {
                    "name": str((resource.get("metadata") or {}).get("name") or "%s %s" % (label, logical_id)),
                    "type": label,
                    "api_id": str(resource.get("id") or logical_id),
                    "control_id": control_id,
                    "lights": light_ids,
                    "state": {"any_on": any_on, "all_on": all_on},
                }
        return {"apiVersion": "v2", "lights": lights, "groups": groups}

    @staticmethod
    def _valid_resource_id(resource_id: str, v2=False) -> str:
        resource_id = str(resource_id)
        pattern = _UUID if v2 else re.compile(r"^[0-9]+$")
        if not pattern.fullmatch(resource_id):
            raise HueError("Invalid Hue resource ID")
        return resource_id

    def _v2_payload(self, body: dict, gamut=None) -> dict:
        payload = {}
        if "on" in body:
            payload["on"] = {"on": bool(body["on"])}
        if "bri" in body:
            payload["dimming"] = {"brightness": round(_clamp(float(body["bri"]), 1, 254) * 100 / 254, 2)}
        if "ct" in body:
            payload["color_temperature"] = {"mirek": int(body["ct"])}
        if "hue" in body or "sat" in body:
            x, y = _hsv_to_xy(float(body.get("hue", 0)), float(body.get("sat", 0)), gamut)
            payload["color"] = {"xy": {"x": round(x, 5), "y": round(y, 5)}}
        if "transitiontime" in body:
            payload["dynamics"] = {"duration": max(0, int(body["transitiontime"])) * 100}
        if not payload:
            raise HueError("No supported Hue state values")
        return payload

    def put_light(self, light_id: str, body: dict, resource: dict | None = None):
        light_id = self._valid_resource_id(light_id, self.api_version == "v2")
        if self.api_version == "v1":
            return self._request("PUT", "/lights/%s/state" % light_id, body)
        gamut = (resource or {}).get("gamut")
        if ("hue" in body or "sat" in body) and not gamut:
            data = self._v2_data("/light/%s" % light_id)
            gamut = ((data[0].get("color") or {}).get("gamut") if data else None)
        return self._request("PUT", "/light/%s" % light_id, self._v2_payload(body, gamut))

    def put_group(self, control_id: str, body: dict):
        control_id = self._valid_resource_id(control_id, self.api_version == "v2")
        if self.api_version == "v1":
            return self._request("PUT", "/groups/%s/action" % control_id, body)
        return self._request("PUT", "/grouped_light/%s" % control_id, self._v2_payload(body))

    def verify(self) -> int:
        return len(self.get_lights())


def fetch_bridge_id(ip: str) -> str:
    context = ssl.create_default_context(cafile=str(CACERT))
    context.check_hostname = False
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open("https://%s/api/config" % ip, timeout=5) as response:
            value = str(json.load(response).get("bridgeid") or "").lower()
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise HueError("Could not read bridge ID") from error
    if not re.fullmatch(r"[0-9a-f]{16}", value):
        raise HueError("Bridge returned an invalid ID")
    return value


def migrate_api(version: str) -> str:
    version = str(version).lower()
    if version not in ("v1", "v2"):
        raise HueError("API version must be v1 or v2")
    creds = read_json(CREDS_FILE)
    if not isinstance(creds, dict):
        raise HueError("Hue bridge is not paired")
    if not creds.get("bridgeId"):
        creds["bridgeId"] = fetch_bridge_id(str(creds.get("bridgeIp") or ""))
    HueClient(creds, version).verify()
    creds["apiVersion"] = version
    write_json_secure(CREDS_FILE, creds)
    return version


def _hex_to_hs(value: str) -> tuple[int, int]:
    value = value.lstrip("#")
    red, green, blue = (int(value[index:index + 2], 16) / 255 for index in (0, 2, 4))
    hue, saturation, _brightness = colorsys.rgb_to_hsv(red, green, blue)
    return round(hue * 65535) % 65536, round(saturation * 254)


def accent_color() -> str | None:
    try:
        for line in COLORS_FILE.read_text().splitlines():
            match = re.match(r'^\s*accent\s*=\s*["\']?#([0-9a-fA-F]{6})["\']?\s*$', line)
            if match:
                return match.group(1).lower()
    except OSError:
        pass
    return None


def build_scene_palette(anchor: str) -> list[str]:
    named = {"red", "green", "yellow", "blue", "magenta", "cyan", "bright_red", "bright_green", "bright_yellow", "bright_blue", "bright_magenta", "bright_cyan"}
    order, values = [], {}
    try:
        for line in COLORS_FILE.read_text().splitlines():
            match = re.match(r'^\s*([A-Za-z0-9_]+)\s*=\s*"#([0-9a-fA-F]{6})"\s*$', line)
            if match:
                order.append(match.group(1))
                values[match.group(1)] = match.group(2).lower()
    except OSError:
        return []

    def skipped(key):
        return key in ("selection", "muted") or any(part in key for part in ("background", "foreground", "border", "tab"))

    keys = [key for key in order if key == "accent"] + [key for key in order if key in named] + [key for key in order if key not in named and not skipped(key)]
    palette = []
    for key in keys:
        value = values.get(key)
        if value and value not in palette:
            palette.append(value)
    if palette:
        palette[0] = anchor
    return palette


def secure_log(message: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write("[%s] %s\n" % (time.strftime("%F %T"), message))
    except OSError:
        pass


def _theme_targets(state: dict, cfg: dict, room_id: str | None = None) -> list[tuple[str, dict]]:
    groups = state["groups"]
    if room_id is not None:
        group = groups.get(room_id)
        return [(room_id, group)] if group and group.get("type") in ("Room", "Zone") else []
    configured = cfg.get("groups")
    items = [(group_id, group) for group_id, group in groups.items() if group.get("type") in ("Room", "Zone")]
    if configured and "all" not in configured:
        names = [str(name).strip().lower() for name in configured if str(name).strip() and str(name).lower() != "all"]
        if names:
            items = [(group_id, group) for group_id, group in items if any(name in str(group.get("name", "")).lower() for name in names)]
    theme_sync = cfg.get("themeSync") or {}
    return [(group_id, group) for group_id, group in items if theme_sync.get(group_id, True)]


def theme_sync(theme_slug: str = "", room_id: str | None = None) -> tuple[int, int]:
    cfg = read_json(CONFIG_FILE, {}) or {}
    if not cfg.get("enabled", True):
        return 0, 0
    anchor = accent_color()
    if not anchor:
        raise HueError("No theme accent color is available")
    color = str((cfg.get("themes") or {}).get(theme_slug) or anchor).lstrip("#").lower()
    if not re.fullmatch(r"[0-9a-f]{6}", color):
        raise HueError("Theme color is invalid")
    client = HueClient()
    state = client.get_state()
    targets = _theme_targets(state, cfg, room_id)
    hue, saturation = _hex_to_hs(color)
    transition = int(cfg.get("transition", 20) or 20)
    base_body = {"hue": hue, "sat": saturation, "transitiontime": transition}
    if cfg.get("bri") is not None:
        base_body["bri"] = int(_clamp(float(cfg["bri"]), 1, 254))
    if cfg.get("turnOn"):
        base_body["on"] = True
    scene_default = bool(cfg.get("scene", False))
    scene_rooms = cfg.get("sceneRooms") or {}
    palette = build_scene_palette(color)
    sent = 0
    for group_id, group in targets:
        light_resources = [state["lights"][light_id] for light_id in group.get("lights", []) if light_id in state["lights"] and state["lights"][light_id].get("has_color")]
        use_scene = scene_rooms.get(group_id, scene_default) and len(light_resources) >= 2 and palette
        if use_scene:
            for index, light in enumerate(light_resources):
                light_hue, light_sat = _hex_to_hs(palette[index % len(palette)])
                body = dict(base_body, hue=light_hue, sat=light_sat)
                client.put_light(light["api_id"], body, light)
            secure_log("theme scene: room %s -> %d light(s), palette #%s" % (group_id, len(light_resources), color))
        else:
            control_id = group.get("control_id")
            if not control_id:
                continue
            client.put_group(control_id, base_body)
        sent += 1
    secure_log("theme sync: %d/%d group(s) via %s -> #%s" % (sent, len(targets), client.api_version, color))
    return sent, len(targets)
