#!/usr/bin/env python3
"""Count scene-application HTTP calls using fixtures, without bridge access."""
import json
from unittest import mock

from test_hue_client import HueClientTests, V2_RESOURCES
import hue_client


def scene_requests(version):
    client = hue_client.HueClient(HueClientTests().creds(version))
    scene_id = '55555555-5555-4555-8555-555555555555'
    scene = {'id': scene_id, 'type': 'scene', 'metadata': {'name': 'Relax'},
             'group': {'rid': V2_RESOURCES[1]['id'], 'rtype': 'room'}}
    replies = {
        '/lights': {'7': {'state': {'on': True, 'bri': 127}}},
        '/groups': {'4': {'type': 'Room', 'lights': ['7'], 'state': {'any_on': True}}},
        '/scenes': {scene_id: {'name': 'Relax', 'group': '4'}},
        '': {'errors': [], 'data': V2_RESOURCES + [scene]},
        '/scene': {'errors': [], 'data': [scene]},
    }
    client._request = mock.Mock(side_effect=lambda method, path, body=None: replies[path] if method == 'GET' else {})
    client.apply_prebuilt_scene('Relax', '4')
    reads = sum(call.args[0] == 'GET' for call in client._request.call_args_list)
    return {'reads': reads, 'writes': client._request.call_count - reads}


if __name__ == '__main__':
    print(json.dumps({version: scene_requests(version) for version in ('v1', 'v2')}, indent=2))
