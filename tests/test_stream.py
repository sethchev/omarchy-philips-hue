"""HueStream encoding and a real authenticated DTLS exchange against local OpenSSL."""
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hue_client import HueError
from hue_stream import Dtls, encode_frame

AREA = '11111111-1111-4111-8111-111111111111'


class StreamTests(unittest.TestCase):
    def test_frame_has_v2_header_area_sequence_and_per_channel_rgb(self):
        frame = encode_frame(AREA, 257, [(4, 1, 0.5, 0), (0, -1, 2, 0.25)])
        self.assertEqual(frame[:16], b'HueStream\x02\x00\x01\x00\x00\x00\x00')
        self.assertEqual(frame[16:52], AREA.encode())
        self.assertEqual(struct.unpack('!BHHH', frame[52:59]), (4, 65535, 32768, 0))
        self.assertEqual(struct.unpack('!BHHH', frame[59:66]), (0, 0, 65535, 16384))

    def test_invalid_area_and_channels_are_rejected(self):
        for area, colors in (('invalid', [(0, 0, 0, 0)]), (AREA, []),
                             (AREA, [(256, 0, 0, 0)]), (AREA, [(0, 0, 0, 0)] * 21)):
            with self.subTest(area=area, count=len(colors)), self.assertRaises(HueError):
                encode_frame(area, 0, colors)

    @unittest.skipUnless(shutil.which('openssl'), 'Requires system OpenSSL')
    def test_real_dtls_handshake_and_encrypted_datagram(self):
        # All keys below are public fixtures, never user credentials.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        key = '0123456789abcdef' * 2
        server = subprocess.Popen(['openssl', 's_server', '-dtls1_2', '-nocert',
            '-accept', '127.0.0.1:' + str(port), '-cipher', 'PSK-AES128-GCM-SHA256',
            '-psk', key, '-psk_identity', 'fixture-app', '-quiet'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        connection = None
        try:
            time.sleep(0.15)
            self.assertIsNone(server.poll())
            connection = Dtls('127.0.0.1', 'fixture-app', key, port=port, timeout=3)
            packet = encode_frame(AREA, 7, [(0, 0.4, 0.5, 0.6)])
            connection.send(packet)
            self.assertTrue(select.select([server.stdout], [], [], 3)[0])
            self.assertEqual(os.read(server.stdout.fileno(), 1200), packet)
            connection.close()
            connection.close()  # Cleanup is idempotent.
        finally:
            if connection:
                connection.close()
            server.terminate()
            server.communicate(timeout=3)
