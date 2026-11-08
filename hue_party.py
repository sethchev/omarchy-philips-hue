"""Party animation and local IPC; no bridge credentials cross process arguments."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import select
import signal
import socket
import sys
from pathlib import Path
import time
from contextlib import contextmanager

import hue_client as hue
from hue_transport import DeadlineExceeded, operation_deadline

INTERVAL = 1.5
DIRECTORY = hue.STATE_HOME / "omarchy/hue-party"
STATUS = DIRECTORY / "status.json"
# Short socket paths also support long custom XDG state directories.
SOCKET = str(Path(os.environ.get("XDG_RUNTIME_DIR") or DIRECTORY) /
             ("hue-party-" + hashlib.sha256(str(DIRECTORY).encode()).hexdigest()[:12] + ".sock"))


def lock_file(name):
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.fdopen(os.open(DIRECTORY / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+")


@contextmanager
def write_guard():
    with lock_file("writes.lock") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def active_light_ids():
    """Called under write_guard; a dead worker cannot leave ownership behind."""
    with lock_file("worker.lock") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return set()
        except BlockingIOError:
            status = hue.read_json(STATUS, {})
            return set(status.get("lights", []))


def request(operation, room_id="", optional=False):
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(20)
            connection.connect(SOCKET)
            connection.sendall(json.dumps({"operation": operation, "room": room_id}).encode() + b"\n")
            raw = bytearray()
            while not raw.endswith(b"\n") and len(raw) <= 65536:
                block = connection.recv(4096)
                if not block:
                    break
                raw.extend(block)
            response = json.loads(raw)
            if response.get("error"):
                raise hue.HueError(response["error"], response.get("code", "request"))
            return response
    except (OSError, ValueError) as error:
        if optional and isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
            return {}
        raise hue.HueError("Party worker unavailable; reopen the Hue plugin", "connection") from error


def ordered_ids(group, settings):
    members = list(dict.fromkeys(group.get("lights", [])))
    saved = settings.get("partyOrder", {}).get(group["id"], [])
    return [item for item in saved if item in members] + [item for item in members if item not in saved]


def frame_colors(ids, palette, step):
    # For one light walk forward; for many, each old color moves to the next bulb.
    return [(light_id, palette[(index + (step if len(ids) == 1 else -step)) % len(palette)])
            for index, light_id in enumerate(ids)] if palette else []


def original_color(light):
    state = light["state"]
    if state.get("colormode") == "ct" and light.get("has_ct"):
        return {"ct": state["ct"], "transitiontime": 5}
    if state.get("xy") and state.get("colormode") == "xy":
        return {"xy": state["xy"], "transitiontime": 5}
    return {"hue": state.get("hue", 0), "sat": state.get("sat", 0), "transitiontime": 5}


class Party:
    def __init__(self, client=None, clock=time.monotonic):
        self.client = client or hue.HueClient()
        self.clock = clock
        self.sessions = {}
        self.error = ""

    def publish(self):
        beats = {}
        tracking = {}
        for room_id, session in self.sessions.items():
            if session.get("music"):
                count = getattr(getattr(session["music"], "analyzer", None), "beats", 0)
                beats[room_id] = count if isinstance(count, int) else 0
                tracker = getattr(getattr(session["music"], "analyzer", None), "tracker", None)
                bpm, confidence = getattr(tracker, "bpm", 0), getattr(tracker, "confidence", 0)
                tracking[room_id] = {
                    "bpm": round(bpm, 1) if isinstance(bpm, (int, float)) and math.isfinite(bpm) else 0,
                    "confidence": round(confidence, 2) if isinstance(confidence, (int, float)) and math.isfinite(confidence) else 0,
                    "locked": getattr(tracker, "locked", False) is True,
                }
        status = {"rooms": {key: True for key, session in self.sessions.items() if not session.get("music")},
            "lights": sorted({item for session in self.sessions.values() for item in session["members"]}),
            "musicRooms": {key: True for key, session in self.sessions.items() if session.get("music")},
            "musicBeats": beats,
            "musicTracking": tracking,
            "error": self.error}
        if status != getattr(self, "last_status", None):
            hue.write_json_secure(STATUS, status)
            self.last_status = status

    def start(self, room_id, publish=True):
        hue.read_theme_config()
        state = self.client.get_state()
        group = state["groups"].get(room_id)
        if not group or group.get("type") not in ("Room", "Zone"):
            raise hue.HueError("Room is no longer available")
        if not hue.accent_color():
            raise hue.HueError("No theme palette is available")
        members = set(group.get("lights", []))
        if not any(light.get("has_color") and light.get("state", {}).get("on") and
                   light.get("state", {}).get("reachable", True)
                   for key, light in state["lights"].items() if key in members):
            raise hue.HueError("No switched-on, reachable color lights in this room")
        if room_id in self.sessions:
            session = self.sessions[room_id]
            if session.get("music"):
                self.stop(room_id, restore=False)
                del session["music"]
                session["started"] = self.clock()
                self.sessions[room_id] = session
                if publish:
                    self.publish()
            return
        if any(members.intersection(item["members"]) for item in self.sessions.values()):
            raise hue.HueError("An overlapping room or zone already has Party Mode on")
        self.sessions[room_id] = {"members": members, "started": self.clock(),
            "original": {key: light for key, light in state["lights"].items()
                         if key in members and light.get("has_color")}}
        self.error = ""
        if publish:
            self.publish()

    def stop(self, room_id, restore=True):
        session = self.sessions.get(room_id)
        was_music = bool(session and session.get("music"))
        cleanup_error = None
        try:
            if session and session.get("music"):
                try:
                    session["music"].close()
                except BaseException as error:
                    # Cleanup must not skip restoration; re-raised below.
                    cleanup_error = error
        finally:
            self.sessions.pop(room_id, None)
            self.publish()
        failures = []
        if was_music:
            state = self.client.get_state()
            for key, original in session["original"].items():
                light = state["lights"].get(key, {})
                if original.get("has_bri") and light.get("state", {}).get("on") and light.get("state", {}).get("reachable", True):
                    try:
                        self.client.put_light(light["api_id"], {"bri": original["state"]["bri"]}, light)
                    except hue.HueError as error:
                        failures.append(error)
        if session and restore:
            settings = hue.read_theme_config()
            if settings.get("enabled", True) and settings.get("themeSync", {}).get(room_id, True):
                hue.theme_sync(room_id=room_id)
            else:
                state = self.client.get_state()
                for key, original in session["original"].items():
                    light = state["lights"].get(key, {})
                    if not light.get("state", {}).get("on") or not light.get("state", {}).get("reachable", True):
                        continue
                    try:
                        body = original_color(original)
                        self.client.put_light(light["api_id"], body, light)
                    except hue.HueError as error:
                        failures.append(error)
        if cleanup_error is not None:
            raise cleanup_error
        if failures:
            raise failures[0]

    def start_music(self, room_id):
        from hue_music import MusicSession
        if self.sessions.get(room_id, {}).get("music"):
            return
        if any(session.get("music") for session in self.sessions.values()):
            raise hue.HueError("Music Sync is already active; stop it before switching rooms")
        existed = room_id in self.sessions
        self.start(room_id, publish=False)
        try:
            self.sessions[room_id]["music"] = MusicSession(room_id)
        except BaseException:
            if not existed:
                self.sessions.pop(room_id, None)
            self.publish()
            raise
        self.publish()

    def stop_music(self, room_id):
        session = self.sessions.get(room_id)
        if session and session.get("music"):
            self.stop(room_id)

    def stop_music_target(self, target_id):
        if not any(session.get("music") for session in self.sessions.values()):
            return
        state = self.client.get_state()
        # The panel's master toggle uses the bridge-wide control ID (v1 "0" or
        # the bridge-home grouped-light UUID on v2), so treat it as "all".
        bridge_wide = target_id == "all" or target_id == str(state.get("allControlId") or "")
        members = set()
        if not bridge_wide:
            for key, group in state["groups"].items():
                if target_id in (key, str(group.get("control_id") or key)):
                    members.update(group.get("lights", []))
            members.update(key for key, light in state["lights"].items()
                           if target_id in (key, str(light.get("api_id") or key)))
        for room_id, session in list(self.sessions.items()):
            if session.get("music") and (bridge_wide or members.intersection(session["members"])):
                self.stop(room_id, restore=False)

    def has_music(self):
        return any(session.get("music") for session in self.sessions.values())

    def interval(self):
        return 0.04 if self.has_music() else INTERVAL

    def stop_overlapping(self, room_id):
        state = self.client.get_state() if self.sessions else {"groups": {}}
        members = set()
        for key, group in state["groups"].items():
            if room_id in (key, str(group.get("control_id") or key)):
                members.update(group.get("lights", []))
        for key, session in list(self.sessions.items()):
            if key == room_id or members.intersection(session["members"]):
                self.stop(key, restore=False)

    def frame(self):
        for room_id, session in list(self.sessions.items()):
            if session.get("music"):
                try:
                    session["music"].frame()
                except (hue.HueError, OSError, DeadlineExceeded):
                    self.stop(room_id, restore=False)
                    raise
        # Ordinary parties keep their 1.5 s cadence alongside a 25 Hz music room.
        now = self.clock()
        if self.has_music() and now < getattr(self, "next_regular_frame", 0):
            return
        self.next_regular_frame = now + INTERVAL
        if self.sessions and all(session.get("music") for session in self.sessions.values()):
            return
        settings = hue.read_theme_config()
        anchor = hue.accent_color()
        if not anchor:
            raise hue.HueError("No theme palette is available")
        slug = hue._read_text_limited(hue.STATE_HOME / "omarchy/current/theme.name").strip()
        anchor = str(settings.get("themes", {}).get(slug) or anchor).lstrip("#").lower()
        if not hue.re.fullmatch(r"[0-9a-f]{6}", anchor):
            raise hue.HueError("Theme color is invalid")
        palette = list(dict.fromkeys([anchor] + hue.build_scene_palette(anchor)))
        state = self.client.get_state()
        failures = []
        claimed = set()
        for room_id, session in list(self.sessions.items()):
            if session.get("music"):
                continue
            group = state["groups"].get(room_id)
            if not group:
                self.sessions.pop(room_id)
                continue
            session["members"] = set(group.get("lights", []))
            # Keep positions stable when a bulb is off/unreachable.
            ids = [key for key in ordered_ids(dict(group, id=room_id), settings)
                   if state["lights"].get(key, {}).get("has_color") and key not in claimed]
            claimed.update(ids)
            step = math.floor((self.clock() - session["started"]) / INTERVAL)
            for key, color in frame_colors(ids, palette, step):
                light = state["lights"][key]
                if not light["state"].get("on") or not light["state"].get("reachable", True):
                    continue
                session["original"].setdefault(key, light)
                hue_value, saturation = hue._hex_to_hs(color)
                try:
                    self.client.put_light(light["api_id"],
                        {"hue": hue_value, "sat": saturation, "transitiontime": 5}, light)
                except hue.HueError as error:
                    failures.append(error)
                    if error.code in ("auth", "tls", "connection", "timeout"):
                        raise
        self.publish()
        if failures:
            raise failures[0]


def serve():
    # stdin is held open by Quickshell; EOF stops us even if the shell crashes.
    with lock_file("worker.lock") as lease:
        while True:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # Bars on multiple monitors share the active worker and status.
                # A standby stays alive so its panel can control that worker.
                ready, _, _ = select.select([sys.stdin], [], [], 1)
                if ready and not os.read(sys.stdin.fileno(), 4096):
                    return
        party = Party()
        credentials = hue._read_text_limited(hue.CREDS_FILE)
        server = socket.socket(socket.AF_UNIX)
        stopping = False

        def stop_signal(*_args):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop_signal)
        signal.signal(signal.SIGINT, stop_signal)
        try:
            try:
                os.unlink(SOCKET)
            except FileNotFoundError:
                pass
            server.bind(SOCKET)
            os.chmod(SOCKET, 0o600)
            server.listen(4)
            party.publish()
            next_frame, failures = time.monotonic(), 0
            while not stopping:
                current_credentials = hue._read_text_limited(hue.CREDS_FILE)
                if credentials != current_credentials:
                    for room_id in list(party.sessions):
                        party.stop(room_id, restore=False)
                    party.client = hue.HueClient()
                    credentials = current_credentials
                ready, _, _ = select.select([server, sys.stdin], [], [],
                    max(0, next_frame - time.monotonic()) if party.sessions else 1)
                if sys.stdin in ready and not os.read(sys.stdin.fileno(), 4096):
                    break
                if server in ready:
                    with server.accept()[0] as connection:
                        connection.settimeout(1)
                        try:
                            raw = bytearray()
                            while not raw.endswith(b"\n") and len(raw) <= 8192:
                                block = connection.recv(8192)
                                if not block:
                                    break
                                raw.extend(block)
                            command = json.loads(raw)
                            operation, room = command.get("operation"), command.get("room", "")
                            if not isinstance(room, str) or not hue._SAFE_ID.fullmatch(room):
                                raise hue.HueError("Invalid party room")
                            with operation_deadline(15):
                                if operation == "start":
                                    with write_guard():
                                        party.start(room)
                                elif operation == "stop":
                                    # Publish release before theme_sync acquires the guard.
                                    party.stop(room)
                                elif operation == "music-start":
                                    with write_guard():
                                        party.start_music(room)
                                elif operation == "music-stop":
                                    party.stop_music(room)
                                elif operation == "stop-music-target":
                                    with write_guard():
                                        party.stop_music_target(room)
                                elif operation == "stop-all":
                                    with write_guard():
                                        for room_id in list(party.sessions):
                                            party.stop(room_id, restore=False)
                                elif operation == "stop-overlap":
                                    with write_guard():
                                        party.stop_overlapping(room)
                                else:
                                    raise hue.HueError("Unknown party command")
                            party.error = ""
                            party.publish()
                            response = {"ok": True}
                            next_frame, failures = time.monotonic(), 0
                        except (hue.HueError, DeadlineExceeded, ValueError, OSError, AttributeError) as error:
                            response = {"error": str(error), "code": getattr(error, "code", "request")}
                            party.error = str(error)
                            party.publish()
                        try:
                            connection.sendall(json.dumps(response).encode() + b"\n")
                        except OSError:
                            pass
                if party.sessions and time.monotonic() >= next_frame:
                    started = time.monotonic()
                    try:
                        with operation_deadline(5), write_guard():
                            party.frame()
                        failures = 0
                        party.error = ""
                    except (hue.HueError, DeadlineExceeded, OSError) as error:
                        failures += 1
                        party.error = "Party Mode paused: " + str(error)
                    party.publish()
                    # Drop missed frames, preserving the wall-clock palette position.
                    next_frame = max(started + party.interval(), time.monotonic() + 0.001)
                    if failures:
                        next_frame = time.monotonic() + min(60, INTERVAL * 2 ** min(failures, 6))
        finally:
            for room_id in list(party.sessions):
                try:
                    with operation_deadline(8):
                        party.stop(room_id, restore=False)
                except (hue.HueError, OSError, DeadlineExceeded):
                    pass
            party.sessions.clear()
            party.publish()
            server.close()
            try:
                os.unlink(SOCKET)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    try:
        serve()
    except (hue.HueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
