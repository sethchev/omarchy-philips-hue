#!/usr/bin/env python3
"""CLI boundary for the Hue client.

Credentials are read from hue.json so the application key never appears in
process arguments.
"""

import json
import re
import sys

from hue_client import (
    CONFIG_FILE,
    HueClient,
    HueError,
    migrate_api,
    read_json,
    secure_log,
    theme_sync,
    write_json_secure,
)


def _config_map(key: str, raw: str):
    settings = json.loads(raw)
    if not isinstance(settings, dict):
        raise HueError("Settings must be an object")
    for resource_id, enabled in settings.items():
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", str(resource_id)) or not isinstance(enabled, bool):
            raise HueError("Invalid room setting")
    config = read_json(CONFIG_FILE, {}) or {}
    config[key] = settings
    write_json_secure(CONFIG_FILE, config)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    operation = argv[1]

    if operation == "write-theme-config" and len(argv) >= 3:
        _config_map("themeSync", argv[2])
        return 0
    if operation == "write-scene-config" and len(argv) >= 3:
        _config_map("sceneRooms", argv[2])
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
    if operation == "get-state":
        print(json.dumps(client.get_state()))
    elif operation == "get-lights":
        print(json.dumps(client.get_lights()))
    elif operation == "get-groups":
        print(json.dumps(client.get_groups()))
    elif operation == "put-light" and len(argv) >= 4:
        client.put_light(argv[2], json.loads(argv[3]))
    elif operation == "put-group" and len(argv) >= 4:
        client.put_group(argv[2], json.loads(argv[3]))
    elif operation == "verify":
        print(client.verify())
    else:
        raise HueError("Unknown or incomplete operation")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (HueError, json.JSONDecodeError, ValueError) as error:
        if len(sys.argv) >= 2 and sys.argv[1] in ("theme-sync", "sync-room"):
            secure_log("theme sync failed: %s" % error)
        print("Hue error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
