"""HueStream v2 transport using system OpenSSL's authenticated DTLS 1.2 PSK."""
from __future__ import annotations

import ctypes as C
import ctypes.util
import ipaddress
import select
import socket
import struct
import time
import uuid

from hue_client import HueError


class OpenSSL:
    """Small typed ABI boundary; cryptography and peer verification stay in OpenSSL."""
    def __init__(self):
        name = ctypes.util.find_library("ssl")
        if not name:
            raise HueError("OpenSSL is required for Hue Entertainment")
        self.lib = C.CDLL(name)
        signatures = {
            "DTLS_client_method": (C.c_void_p, []),
            "SSL_CTX_new": (C.c_void_p, [C.c_void_p]),
            "SSL_CTX_free": (None, [C.c_void_p]),
            "SSL_CTX_ctrl": (C.c_long, [C.c_void_p, C.c_int, C.c_long, C.c_void_p]),
            "SSL_CTX_set_cipher_list": (C.c_int, [C.c_void_p, C.c_char_p]),
            "SSL_CTX_set_psk_client_callback": (None, [C.c_void_p, C.c_void_p]),
            "SSL_new": (C.c_void_p, [C.c_void_p]),
            "SSL_free": (None, [C.c_void_p]),
            "SSL_set_bio": (None, [C.c_void_p, C.c_void_p, C.c_void_p]),
            "SSL_connect": (C.c_int, [C.c_void_p]),
            "SSL_get_error": (C.c_int, [C.c_void_p, C.c_int]),
            "SSL_write": (C.c_int, [C.c_void_p, C.c_void_p, C.c_int]),
            "SSL_ctrl": (C.c_long, [C.c_void_p, C.c_int, C.c_long, C.c_void_p]),
            "BIO_new_dgram": (C.c_void_p, [C.c_int, C.c_int]),
            "BIO_ctrl": (C.c_long, [C.c_void_p, C.c_int, C.c_long, C.c_void_p]),
            "BIO_free": (C.c_int, [C.c_void_p]),
        }
        for function, (result, arguments) in signatures.items():
            method = getattr(self.lib, function)
            method.restype, method.argtypes = result, arguments


PSKCallback = C.CFUNCTYPE(C.c_uint, C.c_void_p, C.c_char_p, C.c_void_p,
                         C.c_uint, C.c_void_p, C.c_uint)


class Dtls:
    def __init__(self, host, identity, key, port=2100, timeout=6):
        self.context = self.ssl = self.sock = None
        self.api = OpenSSL().lib
        ipaddress.IPv4Address(host)
        if not isinstance(identity, str) or not 1 <= len(identity) <= 64 or not identity.isascii():
            raise HueError("Invalid DTLS application identity")
        try:
            secret = bytes.fromhex(key)
        except (ValueError, TypeError):
            raise HueError("Invalid DTLS streaming key") from None
        if len(secret) != 16:
            raise HueError("Invalid DTLS streaming key")
        username = identity.encode("ascii") + b"\0"

        def supply_psk(_ssl, _hint, output_identity, identity_size, output_key, key_size):
            if len(username) > identity_size or len(secret) > key_size:
                return 0
            C.memmove(output_identity, username, len(username))
            C.memmove(output_key, secret, len(secret))
            return len(secret)

        self.callback = PSKCallback(supply_psk)  # Keep callback alive until SSL_CTX_free.
        try:
            self.context = self.api.SSL_CTX_new(self.api.DTLS_client_method())
            if not self.context:
                raise HueError("Could not initialize DTLS")
            # SSL_CTRL_SET_MIN/MAX_PROTO_VERSION; DTLS1_2_VERSION from OpenSSL headers.
            if self.api.SSL_CTX_ctrl(self.context, 123, 0xFEFD, None) != 1 or \
               self.api.SSL_CTX_ctrl(self.context, 124, 0xFEFD, None) != 1 or \
               self.api.SSL_CTX_set_cipher_list(self.context, b"PSK-AES128-GCM-SHA256") != 1:
                raise HueError("OpenSSL lacks Hue's DTLS cipher")
            self.api.SSL_CTX_set_psk_client_callback(self.context, self.callback)
            self.ssl = self.api.SSL_new(self.context)
            if not self.ssl:
                raise HueError("Could not initialize DTLS session")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.connect((host, port))
            self.sock.setblocking(False)
            bio = self.api.BIO_new_dgram(self.sock.fileno(), 0)
            if not bio:
                raise HueError("Could not initialize DTLS datagram transport")
            # Linux sockaddr_in: native family, network-order port/address, padding.
            address = C.create_string_buffer(struct.pack("=H", socket.AF_INET) +
                struct.pack("!H", port) + socket.inet_aton(host) + b"\0" * 8)
            if self.api.BIO_ctrl(bio, 32, 0, address) != 1:  # BIO_CTRL_DGRAM_SET_CONNECTED
                self.api.BIO_free(bio)
                raise HueError("Could not connect DTLS datagram transport")
            self.api.SSL_set_bio(self.ssl, bio, bio)  # SSL now owns the BIO.
            self._perform(lambda: self.api.SSL_connect(self.ssl), timeout)
        except BaseException:
            self.close()
            raise

    def _perform(self, operation, timeout):
        deadline = time.monotonic() + timeout
        while True:
            result = operation()
            if result > 0:
                return result
            error = self.api.SSL_get_error(self.ssl, result)
            if error not in (2, 3):  # SSL_ERROR_WANT_READ / SSL_ERROR_WANT_WRITE
                raise HueError("Hue DTLS authentication or connection failed", "connection")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HueError("Hue DTLS connection timed out", "timeout")
            select.select([self.sock] if error == 2 else [],
                          [self.sock] if error == 3 else [], [], min(remaining, 0.2))
            self.api.SSL_ctrl(self.ssl, 74, 0, None)  # DTLS_CTRL_HANDLE_TIMEOUT

    def send(self, packet):
        if not self.ssl or not 1 <= len(packet) <= 1200:
            raise HueError("Invalid Hue streaming frame")
        buffer = C.create_string_buffer(packet)
        if self._perform(lambda: self.api.SSL_write(self.ssl, buffer, len(packet)), 1) != len(packet):
            raise HueError("Incomplete Hue streaming frame", "connection")

    def close(self):
        if self.ssl:
            self.api.SSL_free(self.ssl)
            self.ssl = None
        if self.context:
            self.api.SSL_CTX_free(self.context)
            self.context = None
        if self.sock:
            self.sock.close()
            self.sock = None


def encode_frame(area_id, sequence, colors):
    """RGB components are normalized 0–1; channel IDs come from the bridge."""
    try:
        area = str(uuid.UUID(area_id)).encode("ascii")
    except (ValueError, TypeError, AttributeError):
        raise HueError("Invalid Entertainment Area ID") from None
    if not 1 <= len(colors) <= 20:
        raise HueError("Invalid Entertainment channel count")
    packet = bytearray(b"HueStream" + bytes([2, 0, sequence % 256, 0, 0, 0, 0]) + area)
    for channel, red, green, blue in colors:
        if not isinstance(channel, int) or not 0 <= channel <= 255:
            raise HueError("Invalid Entertainment channel")
        packet.extend(struct.pack("!BHHH", channel,
            *(round(max(0.0, min(1.0, value)) * 65535) for value in (red, green, blue))))
    return bytes(packet)
