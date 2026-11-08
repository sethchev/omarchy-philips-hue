#!/usr/bin/env python3
"""Secure Entertainment registration using the plugin's existing bridge identity."""
from __future__ import annotations

import argparse
import re
import time

import hue_client as hue
from hue_transport import DeadlineExceeded, operation_deadline


class LinkButtonNeeded(hue.HueError):
    """The bridge has not opened its physical registration window."""


class RegistrationClient(hue.HueClient):
    def __init__(self, credentials):
        super().__init__(credentials, api_version="v1")
        if not self.bridge_id:
            raise hue.HueError("Bridge identity is missing; pair normally before enabling Entertainment", "tls")

    def _url(self, path):
        if path:
            raise hue.HueError("Invalid registration endpoint")
        return "https://%s/api" % self.bridge_id, self.bridge_id

    def _check_response(self, response):
        if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
            raise hue.HueError("Invalid Entertainment registration response", "response")
        error = response[0].get("error")
        if isinstance(error, dict):
            if error.get("type") == 101:
                raise LinkButtonNeeded("Press the physical link button on the Hue bridge")
            raise hue.HueError("Bridge rejected Entertainment registration", "request")
        success = response[0].get("success")
        if not isinstance(success, dict):
            raise hue.HueError("Invalid Entertainment registration response", "response")
        username, client_key = success.get("username"), success.get("clientkey")
        if not isinstance(username, str) or not re.fullmatch(r"[0-9A-Za-z_-]{20,64}", username):
            raise hue.HueError("Bridge returned an invalid Entertainment application key", "response")
        if not isinstance(client_key, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", client_key):
            raise hue.HueError("Bridge returned an invalid Entertainment streaming key", "response")


def entertainment_credentials(credentials):
    """Build a separate REST client identity without changing normal controls."""
    saved = credentials.get("entertainment")
    if not isinstance(saved, dict) or saved.get("bridgeId") != credentials.get("bridgeId"):
        raise hue.HueError("Entertainment has not been paired with this bridge")
    username, client_key = saved.get("username"), saved.get("clientKey")
    if not isinstance(username, str) or not re.fullmatch(r"[0-9A-Za-z_-]{20,64}", username):
        raise hue.HueError("Entertainment application key is missing or invalid")
    if not isinstance(client_key, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", client_key):
        raise hue.HueError("Entertainment streaming key is missing or invalid")
    return dict(credentials, username=username, apiVersion="v2"), client_key


def pair(wait_seconds=90, clock=time.monotonic, sleep=time.sleep):
    original = hue.read_json(hue.CREDS_FILE)
    if not isinstance(original, dict):
        raise hue.HueError("Pair the Hue plugin normally before enabling Entertainment")
    registration = RegistrationClient(original)
    # Verify support before requesting another application registration.
    with operation_deadline(8):
        hue.HueClient(original, api_version="v2")._v2_data("/entertainment_configuration")
    deadline = clock() + wait_seconds
    while True:
        try:
            with operation_deadline(min(8, max(0.01, deadline - clock()))):
                response = registration._request("POST", "", {
                    "devicetype": "omarchy-hue#entertainment", "generateclientkey": True})
            success = response[0]["success"]
            break
        except LinkButtonNeeded:
            remaining = deadline - clock()
            if remaining <= 0:
                raise hue.HueError("Pairing window expired; press the bridge button and try again") from None
            sleep(min(2, remaining))
    saved = {"username": success["username"], "clientKey": success["clientkey"].lower(),
             "bridgeId": original["bridgeId"]}
    candidate = dict(original, entertainment=saved)
    separate, _key = entertainment_credentials(candidate)
    # Validate the returned application key before touching existing credentials.
    with operation_deadline(8):
        hue.HueClient(separate, api_version="v2")._v2_data("/bridge")
    # Hold the shared credential lock across the final compare-and-write so a
    # concurrent pairing or migration cannot be overwritten by this snapshot.
    with hue.credentials_guard():
        current = hue.read_json(hue.CREDS_FILE)
        if not isinstance(current, dict) or any(current.get(key) != original.get(key)
                for key in ("bridgeIp", "bridgeId", "username", "apiVersion", "entertainment")):
            raise hue.HueError("Pairing settings changed; Entertainment keys were not saved. Try again")
        current["entertainment"] = saved
        hue.write_json_secure(hue.CREDS_FILE, current)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pair Hue Entertainment without replacing the normal control key")
    parser.add_argument("operation", choices=["pair"])
    parser.add_argument("--wait", type=int, default=90, help="Seconds to wait for the physical bridge button (1–180)")
    args = parser.parse_args(argv)
    if not 1 <= args.wait <= 180:
        parser.error("--wait must be between 1 and 180 seconds")
    print("Press the link button on your Hue bridge. Waiting up to %d seconds…" % args.wait, flush=True)
    try:
        pair(args.wait)
    except (hue.HueError, DeadlineExceeded, OSError) as error:
        # Bridge responses and credential values are deliberately never logged.
        print("Entertainment pairing failed: %s" % error, flush=True)
        return 1
    print("Entertainment keys verified and saved privately. Normal control pairing preserved.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
