#!/usr/bin/env python3
"""CLI boundary for the Hue client.

Credentials are read from hue.json so the application key never appears in
process arguments.
"""

import json
import re
import sys

from hue_transport import DeadlineExceeded, operation_deadline

from hue_client import (
    CONFIG_FILE,
    HueClient,
    HueError,
    migrate_api,
    read_theme_config,
    secure_log,
    theme_sync,
    write_json_secure,
)


def _update_config(update):
    """Run a read-modify-write of the theme settings under the shared guard.

    Two helpers (for example panels on multiple monitors) writing different
    settings concurrently would otherwise atomically replace the file and
    silently drop each other's changes.
    """
    from hue_party import write_guard
    with write_guard():
        config = read_theme_config()
        update(config)
        write_json_secure(CONFIG_FILE, config)


def _config_map(key: str, raw: str):
    settings = json.loads(raw)
    if not isinstance(settings, dict):
        raise HueError("Settings must be an object")
    for resource_id, enabled in settings.items():
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", str(resource_id)) or not isinstance(enabled, bool):
            raise HueError("Invalid room setting")

    def apply(config):
        config[key] = settings
    _update_config(apply)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    operation = argv[1]

    if operation == "party" and len(argv) == 4:
        from hue_party import request
        request(argv[2], argv[3])
        return 0
    if operation == "write-music-options" and len(argv) == 4:
        room, options = argv[2], json.loads(argv[3])
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", room) or not isinstance(options, dict):
            raise HueError("Invalid Music Sync options")
        for key, value in options.items():
            low, high = ((-0.3, 0.3) if key == "timing" else ((0.0, 1.0) if key in ("intensity", "idle", "peak") else (0.25, 2.0)))
            if key not in ("intensity", "sensitivity", "pace", "timing", "idle", "peak") or isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise HueError("Invalid Music Sync option")
        if options.get("idle", 0.10) > options.get("peak", 0.75):
            raise HueError("Between-beat brightness must not exceed on-beat brightness")

        def apply(config):
            config.setdefault("musicOptions", {})[room] = options
        _update_config(apply)
        return 0
    if operation == "write-party-order" and len(argv) == 4:
        room, ids = argv[2], json.loads(argv[3])
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", room) or not isinstance(ids, list) or len(ids) > 256 or any(
            not isinstance(item, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", item) for item in ids
        ) or len(set(ids)) != len(ids):
            raise HueError("Invalid party light order")

        def apply(config):
            config.setdefault("partyOrder", {})[room] = ids
        _update_config(apply)
        return 0
    if operation == "write-theme-config" and len(argv) >= 3:
        _config_map("themeSync", argv[2])
        return 0
    if operation == "write-scene-config" and len(argv) >= 3:
        _config_map("sceneRooms", argv[2])
        return 0
    if operation == "write-favorite-config" and len(argv) >= 3:
        _config_map("favoriteRooms", argv[2])
        return 0
    if operation == "migrate-api" and len(argv) >= 3:
        version = migrate_api(argv[2])
        print("Hue API set to %s; existing application key verified." % version)
        return 0
    if operation == "theme-sync":
        sent, total = theme_sync(argv[2] if len(argv) >= 3 else "")
        print("Synced %d/%d room(s)." % (sent, total))
        return 0
    if operation == "sync-room" and len(argv) >= 3:
        theme_sync(room_id=argv[2])
        return 0

    client = HueClient()
    if operation == "get-scenes":
        print(json.dumps(client.get_scenes()))
    elif operation == "recall-scene" and len(argv) >= 4:
        from hue_party import request
        request("stop-overlap", argv[3], optional=True)
        client.recall_scene(argv[2], argv[3])
    elif operation == "apply-scene" and len(argv) >= 3:
        room_id = argv[3] if len(argv) >= 4 else None
        from hue_party import request
        if room_id:
            request("stop-overlap", room_id, optional=True)
        else:
            request("stop-all", "all", optional=True)
        applied, total = client.apply_prebuilt_scene(argv[2], room_id)
        print("Applied scene to %d/%d room(s)." % (applied, total))
    elif operation == "get-state":
        print(json.dumps(client.get_snapshot()))
    elif operation == "get-lights":
        print(json.dumps(client.get_lights()))
    elif operation == "get-groups":
        print(json.dumps(client.get_groups()))
    elif operation == "put-light" and len(argv) >= 4:
        resource = None
        if len(argv) >= 5:
            gamut = json.loads(argv[4])
            if not isinstance(gamut, dict):
                raise HueError("Light gamut must be an object")
            resource = {"gamut": gamut}
        from hue_party import request
        request("stop-music-target", argv[2], optional=True)
        client.put_light(argv[2], json.loads(argv[3]), resource)
    elif operation == "put-group" and len(argv) >= 4:
        from hue_party import request
        request("stop-music-target", argv[2], optional=True)
        client.put_group(argv[2], json.loads(argv[3]))
    elif operation == "put-all" and len(argv) >= 4:
        from hue_party import request
        request("stop-music-target", "all", optional=True)
        client.put_all(json.loads(argv[2]), json.loads(argv[3]))
    elif operation == "verify":
        print(client.verify())
    else:
        raise HueError("Unknown or incomplete operation")
    return 0


def run(argv: list[str]) -> int:
    structured = len(argv) > 1 and argv[-1] == "--json-errors"
    if structured:
        argv = argv[:-1]
    try:
        with operation_deadline():
            return main(argv)
    except (DeadlineExceeded, HueError, json.JSONDecodeError, ValueError) as error:
        code = "timeout" if isinstance(error, DeadlineExceeded) else getattr(error, "code", "request")
        if len(argv) >= 2 and argv[1] in ("theme-sync", "sync-room"):
            secure_log("theme sync failed: %s" % error)
        if structured:
            print(json.dumps({"error": {"code": code, "message": str(error)}}), file=sys.stderr)
        else:
            print("Hue error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
