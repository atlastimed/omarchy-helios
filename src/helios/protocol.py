# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""Zepp OS "chunked 2021" framing.

Every request and reply travels over two GATT characteristics as chunks of a
message addressed to a service endpoint. Messages to encrypted endpoints are
AES-128-ECB encrypted with a per-message key: the session key XORed with the
message handle. Layout follows Gadgetbridge's Huami2021ChunkedEncoder/Decoder.

Chunk header: 0x03, flags, 0x00 (extended flags), handle, count.
The first chunk then carries the plaintext length (u32 LE) and endpoint (u16 LE).
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .crypto import aes_ecb_decrypt, aes_ecb_encrypt

CHUNK = 0x03
CHUNK_ACK = 0x04

FLAG_FIRST = 0x01
FLAG_LAST = 0x02
FLAG_NEEDS_ACK = 0x04
FLAG_ENCRYPTED = 0x08

# The largest message taken from the strap. Everything Helios asks for is
# answered in well under a kilobyte (history travels over its own
# characteristic, not this one); a longer message is acknowledged chunk by
# chunk, as the strap expects, and dropped.
MAX_MESSAGE = 256 * 1024


def _padded(length: int) -> int:
    return (length + 15) // 16 * 16


def _message_key(session_key: bytes, handle: int) -> bytes:
    return bytes(b ^ handle for b in session_key)


@dataclass
class Encoder:
    mtu: int = 23
    session_key: bytes | None = None
    sequence: int = 0
    handle: int = 0

    def set_encryption(self, sequence: int, session_key: bytes) -> None:
        self.sequence = sequence
        self.session_key = session_key

    def encode(self, endpoint: int, data: bytes, encrypt: bool) -> list[bytes]:
        self.handle = (self.handle + 1) & 0xFF
        length = len(data)
        if encrypt:
            if self.session_key is None:
                raise RuntimeError("cannot encrypt before the session key is established")
            body = bytearray(data)
            body += struct.pack("<I", self.sequence)
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
            body += struct.pack("<I", zlib.crc32(body))
            body += bytes(_padded(len(body)) - len(body))
            payload = aes_ecb_encrypt(bytes(body), _message_key(self.session_key, self.handle))
        else:
            payload = data

        max_write = min(512, max(23, self.mtu) - 3)
        chunks = []
        offset = 0
        count = 0
        while offset < len(payload) or count == 0:
            first = count == 0
            header_size = 11 if first else 5
            piece = payload[offset : offset + max_write - header_size]
            offset += len(piece)
            flags = FLAG_ENCRYPTED if encrypt else 0
            if first:
                flags |= FLAG_FIRST
            if offset >= len(payload):
                flags |= FLAG_LAST | FLAG_NEEDS_ACK
            header = bytes([CHUNK, flags, 0x00, self.handle, count & 0xFF])
            if first:
                header += struct.pack("<IH", length, endpoint)
            chunks.append(header + piece)
            count += 1
        return chunks


@dataclass
class Message:
    endpoint: int
    payload: bytes


@dataclass
class Decoder:
    session_key: bytes | None = None
    _handle: int | None = None
    _endpoint: int = 0
    _length: int = 0
    _buffer: bytearray = field(default_factory=bytearray)
    _dropping: bool = False  # the message under way is too long to keep
    last_handle: int = 0
    last_count: int = 0

    def reset(self) -> None:
        self._handle = None
        self._endpoint = 0
        self._length = 0
        self._buffer = bytearray()
        self._dropping = False

    def feed(self, chunk: bytes) -> tuple[Message | None, bool]:
        """Consume one notification. Returns a completed message, if any, and
        whether the device asked for an ack of this chunk."""
        if len(chunk) < 5 or chunk[0] != CHUNK:
            return None, False
        flags = chunk[1]
        handle = chunk[3]
        if flags & FLAG_FIRST and len(chunk) < 11:
            # Too short to hold the length and endpoint a first chunk carries.
            self.reset()
            return None, False
        if not flags & FLAG_FIRST and (self._handle is None or handle != self._handle):
            # A continuation of a message we never saw the start of.
            return None, False
        self.last_handle = handle
        self.last_count = chunk[4]
        pos = 5
        if flags & FLAG_FIRST:
            self._length, self._endpoint = struct.unpack_from("<IH", chunk, pos)
            pos += 6
            self._handle = handle
            self._buffer = bytearray()
            self._dropping = self._length > MAX_MESSAGE
        needs_ack = bool(flags & FLAG_NEEDS_ACK)
        if not self._dropping:
            self._buffer += chunk[pos:]
            # The length is the plaintext's; ciphertext adds a sequence, a
            # CRC and padding.
            if len(self._buffer) > _padded(MAX_MESSAGE + 8):
                self._dropping = True
                self._buffer = bytearray()
        if not flags & FLAG_LAST:
            return None, needs_ack
        if self._dropping:
            self.reset()
            return None, needs_ack

        data = bytes(self._buffer)
        if flags & FLAG_ENCRYPTED:
            if self.session_key is None:
                self.reset()
                return None, needs_ack
            # The ciphertext is the plaintext plus sequence and CRC, padded to 16.
            data = aes_ecb_decrypt(data[: len(data) // 16 * 16], _message_key(self.session_key, handle))
            data = data[: self._length]
        message = Message(self._endpoint, data)
        self.reset()
        return message, needs_ack

    def ack(self) -> bytes:
        return bytes([CHUNK_ACK, 0x00, self.last_handle, 0x01, self.last_count])


# ---- time encoding -----------------------------------------------------------


def encode_time(when: datetime) -> bytes:
    """Wall-clock time in the strap's format: year u16, month, day, hour,
    minute, second, then the UTC offset in quarter hours (DST included)."""
    local = when.astimezone()
    offset = local.utcoffset() or timedelta()
    quarters = int(offset.total_seconds() // 900)
    return struct.pack("<HBBBBBb", local.year, local.month, local.day, local.hour, local.minute, 0, quarters)


def decode_time(data: bytes) -> datetime:
    year, month, day, hour, minute, second = struct.unpack_from("<HBBBBB", data)
    quarters = struct.unpack_from("<b", data, 7)[0] if len(data) > 7 else 0
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone(timedelta(minutes=15 * quarters)))


def iter_struct(fmt: str, data: bytes) -> Iterator[tuple]:
    size = struct.calcsize(fmt)
    for offset in range(0, len(data) - size + 1, size):
        yield struct.unpack_from(fmt, data, offset)
