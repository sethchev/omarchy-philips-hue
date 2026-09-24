import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hue_client


V2_RESOURCES = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "id_v1": "/lights/7",
        "type": "light",
        "owner": {"rid": "22222222-2222-4222-8222-222222222222", "rtype": "device"},
        "metadata": {"name": "Desk"},
        "on": {"on": True},
        "dimming": {"brightness": 50.0},
        "color_temperature": {
            "mirek": 250,
            "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 454},
        },
        "color": {
            "xy": {"x": 0.3, "y": 0.4},
            "gamut": {
                "red": {"x": 0.7, "y": 0.3},
                "green": {"x": 0.17, "y": 0.7},
                "blue": {"x": 0.15, "y": 0.06},
            },
        },
    },
    {
        "id": "33333333-3333-4333-8333-333333333333",
        "id_v1": "/groups/4",
        "type": "room",
        "metadata": {"name": "Office"},
        "children": [{"rid": "22222222-2222-4222-8222-222222222222", "rtype": "device"}],
        "services": [{"rid": "44444444-4444-4444-8444-444444444444", "rtype": "grouped_light"}],
    },
    {
        "id": "44444444-4444-4444-8444-444444444444",
        "type": "grouped_light",
        "on": {"on": True},
    },
]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.request = None

    def open(self, request, timeout=0):
        self.request = request
        return FakeResponse(json.dumps(self.result).encode())


class HueClientTests(unittest.TestCase):
    def creds(self, version="v2"):
        return {
            "bridgeIp": "192.0.2.10",
            "bridgeId": "001788fffe123456",
            "username": "a" * 40,
            "apiVersion": version,
        }

    def test_missing_api_version_remains_v1(self):
        self.assertEqual(hue_client.configured_api_version({}), "v1")

    def test_v1_normalization_exposes_capabilities_for_shared_model(self):
        client = hue_client.HueClient(self.creds("v1"))
        lights = client._normalize_v1_lights({
            "7": {
                "state": {"on": True, "bri": 127, "hue": 100, "sat": 200, "ct": 250},
                "capabilities": {"control": {"ct": {"min": 160, "max": 450}}},
            }
        })
        self.assertTrue(lights["7"]["has_color"])
        self.assertTrue(lights["7"]["has_ct"])
        self.assertEqual(lights["7"]["ct_min"], 160)
        self.assertEqual(lights["7"]["api_id"], "7")

    def test_v2_normalization_preserves_v1_ids_and_v2_control_ids(self):
        client = hue_client.HueClient(self.creds())
        state = client._normalize_v2(V2_RESOURCES)
        light = state["lights"]["7"]
        room = state["groups"]["4"]
        self.assertEqual(light["api_id"], V2_RESOURCES[0]["id"])
        self.assertEqual(light["state"]["bri"], 127)
        self.assertTrue(light["state"]["reachable"])
        self.assertEqual(light["ct_max"], 454)
        self.assertEqual(room["lights"], ["7"])
        self.assertEqual(room["control_id"], V2_RESOURCES[2]["id"])
        self.assertTrue(room["state"]["all_on"])

    def test_v2_semantic_payload_conversion(self):
        client = hue_client.HueClient(self.creds())
        payload = client._v2_payload({"on": True, "bri": 127, "ct": 250, "transitiontime": 20})
        self.assertEqual(payload["on"], {"on": True})
        self.assertAlmostEqual(payload["dimming"]["brightness"], 50.0, delta=0.1)
        self.assertEqual(payload["color_temperature"], {"mirek": 250})
        self.assertEqual(payload["dynamics"], {"duration": 2000})

    def test_v2_color_is_xy_inside_gamut(self):
        client = hue_client.HueClient(self.creds())
        gamut = V2_RESOURCES[0]["color"]["gamut"]
        payload = client._v2_payload({"hue": 50000, "sat": 254}, gamut)
        xy = payload["color"]["xy"]
        self.assertGreaterEqual(xy["x"], 0)
        self.assertLessEqual(xy["x"], 1)
        self.assertGreaterEqual(xy["y"], 0)
        self.assertLessEqual(xy["y"], 1)

    def test_v2_request_uses_application_key_header_not_url(self):
        client = hue_client.HueClient(self.creds())
        opener = FakeOpener({"errors": [], "data": []})
        client._get_opener = lambda _verify: opener
        with mock.patch.object(hue_client, "_BridgeResolver", mock.MagicMock()):
            client._request("GET", "/bridge")
        self.assertIn("/clip/v2/resource/bridge", opener.request.full_url)
        self.assertNotIn(client.username, opener.request.full_url)
        self.assertEqual(opener.request.get_header("Hue-application-key"), client.username)

    def test_v2_write_uses_uuid_resource_routes(self):
        client = hue_client.HueClient(self.creds())
        calls = []
        client._request = lambda method, path, body=None: calls.append((method, path, body)) or {}
        client.put_light(V2_RESOURCES[0]["id"], {"on": True})
        client.put_group(V2_RESOURCES[2]["id"], {"bri": 127})
        self.assertEqual(calls[0][1], "/light/" + V2_RESOURCES[0]["id"])
        self.assertEqual(calls[1][1], "/grouped_light/" + V2_RESOURCES[2]["id"])
        self.assertAlmostEqual(calls[1][2]["dimming"]["brightness"], 50.0, delta=0.1)

    def test_v2_response_errors_are_not_ignored(self):
        client = hue_client.HueClient(self.creds())
        with self.assertRaises(hue_client.HueError):
            client._check_response({"errors": [{"description": "unauthorized"}], "data": []})

    def test_migration_reuses_existing_application_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hue.json"
            original = self.creds("v1")
            hue_client.write_json_secure(path, original)
            verified = []

            class Verifier:
                def __init__(self, creds, version):
                    self.creds = creds
                    self.version = version

                def verify(self):
                    verified.append((self.creds["username"], self.version))
                    return 1

            with mock.patch.object(hue_client, "CREDS_FILE", path), mock.patch.object(hue_client, "HueClient", Verifier):
                self.assertEqual(hue_client.migrate_api("v2"), "v2")
            migrated = json.loads(path.read_text())
            self.assertEqual(migrated["username"], original["username"])
            self.assertEqual(migrated["apiVersion"], "v2")
            self.assertEqual(verified, [(original["username"], "v2")])

    def test_v2_scene_listing_normalizes_room_group(self):
        client = hue_client.HueClient(self.creds())
        scene_id = "55555555-5555-4555-8555-555555555555"
        room_id = V2_RESOURCES[1]["id"]
        client._v2_data = lambda path="": [{
            "id": scene_id,
            "type": "scene",
            "metadata": {"name": "Relax"},
            "group": {"rid": room_id, "rtype": "room"},
            "status": {"active": "static"},
        }]
        scenes = client.get_scenes()
        self.assertEqual(scenes[scene_id]["name"], "Relax")
        self.assertEqual(scenes[scene_id]["group_api_id"], room_id)
        self.assertTrue(scenes[scene_id]["active"])

    def test_prebuilt_scene_recipe_uses_capabilities(self):
        client = hue_client.HueClient(self.creds())
        light = {
            "has_color": False,
            "has_ct": True,
            "ct_min": 153,
            "ct_max": 454,
        }
        body = client._scene_body_for_light(hue_client.PREBUILT_SCENES["natural light"], light, 0)
        self.assertEqual(body["on"], True)
        self.assertEqual(body["bri"], 254)
        self.assertEqual(body["ct"], 200)

    def test_apply_prebuilt_scene_reuses_existing_scene(self):
        client = hue_client.HueClient(self.creds())
        client.get_state = lambda: client._normalize_v2(V2_RESOURCES)
        scene_id = "55555555-5555-4555-8555-555555555555"
        client.get_scenes = lambda: {
            scene_id: {
                "api_id": scene_id,
                "name": "Relax",
                "group": V2_RESOURCES[1]["id"],
                "group_api_id": V2_RESOURCES[1]["id"],
            }
        }
        recalled = []
        client.recall_scene = lambda sid, group_id=None: recalled.append((sid, group_id))
        client._create_scene = mock.Mock()
        self.assertEqual(client.apply_prebuilt_scene("Relax"), (1, 1))
        self.assertEqual(recalled, [(scene_id, V2_RESOURCES[2]["id"])])
        client._create_scene.assert_not_called()

    def test_apply_prebuilt_scene_creates_missing_scene(self):
        client = hue_client.HueClient(self.creds())
        client.get_state = lambda: client._normalize_v2(V2_RESOURCES)
        client.get_scenes = lambda: {}
        created_id = "55555555-5555-4555-8555-555555555555"
        client._create_scene = mock.Mock(return_value=created_id)
        recalled = []
        client.recall_scene = lambda sid, group_id=None: recalled.append((sid, group_id))
        self.assertEqual(client.apply_prebuilt_scene("Natural Light"), (1, 1))
        client._create_scene.assert_called_once()
        self.assertEqual(client._create_scene.call_args.args[0], "Natural Light")
        self.assertEqual(recalled, [(created_id, V2_RESOURCES[2]["id"])])

    def test_apply_prebuilt_scene_targets_selected_room_only(self):
        client = hue_client.HueClient(self.creds())
        state = client._normalize_v2(V2_RESOURCES)
        other_room = dict(state["groups"]["4"], api_id="66666666-6666-4666-8666-666666666666")
        state["groups"]["5"] = other_room
        client.get_state = lambda: state
        client.get_scenes = lambda: {}
        client._create_scene = mock.Mock(return_value="55555555-5555-4555-8555-555555555555")
        client.recall_scene = mock.Mock()
        self.assertEqual(client.apply_prebuilt_scene("Read", "4"), (1, 1))
        self.assertEqual(client._create_scene.call_args.args[1], "4")
        client.recall_scene.assert_called_once()

    def test_secure_json_write_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hue.json"
            hue_client.write_json_secure(path, {"apiVersion": "v2"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["apiVersion"], "v2")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
