import struct
from datetime import UTC, datetime, timedelta, timezone

import pytest

from helios import protocol
from helios.protocol import Decoder, Encoder, decode_time, encode_time

KEY = bytes(range(16))


def roundtrip(payload: bytes, encrypt: bool, mtu: int) -> tuple[list[bytes], bytes, int]:
    encoder = Encoder(mtu=mtu)
    encoder.set_encryption(0x01020304, KEY)
    decoder = Decoder(session_key=KEY)
    chunks = encoder.encode(0x004B, payload, encrypt)
    message = None
    for chunk in chunks:
        message, _ack = decoder.feed(chunk)
    assert message is not None
    return chunks, message.payload, message.endpoint


@pytest.mark.parametrize("encrypt", [False, True])
@pytest.mark.parametrize("size", [1, 12, 200, 1000])
def test_roundtrip(encrypt, size):
    payload = bytes((i * 7) & 0xFF for i in range(size))
    chunks, decoded, endpoint = roundtrip(payload, encrypt, mtu=23)
    assert decoded == payload
    assert endpoint == 0x004B
    assert all(len(c) <= 20 for c in chunks)


def test_first_chunk_header():
    encoder = Encoder(mtu=247)
    (chunk,) = encoder.encode(0x0029, b"\x03", encrypt=False)
    # 0x03, first|last|needs-ack, extended flags, handle 1, count 0, length, endpoint
    assert chunk[:5] == bytes([0x03, 0x07, 0x00, 0x01, 0x00])
    assert struct.unpack_from("<IH", chunk, 5) == (1, 0x0029)
    assert chunk[11:] == b"\x03"


def test_encrypted_payload_carries_sequence_and_crc():
    encoder = Encoder(mtu=247)
    encoder.set_encryption(41, KEY)
    (chunk,) = encoder.encode(0x0029, b"\x03", encrypt=True)
    assert chunk[1] & 0x08
    assert len(chunk) - 11 == 16  # 1 byte + sequence + CRC, padded to a block
    assert encoder.sequence == 42


def test_ack_echoes_handle_and_count():
    decoder = Decoder()
    decoder.feed(bytes([0x03, 0x07, 0x00, 0x09, 0x02]) + struct.pack("<IH", 1, 0x10) + b"\x01")
    assert decoder.ack() == bytes([0x04, 0x00, 0x09, 0x01, 0x02])


def test_time_roundtrip_keeps_the_instant():
    when = datetime(2026, 10, 30, 7, 39, tzinfo=UTC)
    encoded = encode_time(when)
    assert len(encoded) == 8
    assert decode_time(encoded) == when


def test_decode_time_applies_quarter_hour_offset():
    data = struct.pack("<HBBBBBb", 2026, 10, 30, 8, 39, 0, 4)
    assert decode_time(data) == datetime(2026, 10, 30, 8, 39, tzinfo=timezone(timedelta(hours=1)))


def test_continuation_without_a_start_is_ignored():
    decoder = Decoder()
    orphan = bytes([0x03, 0x02, 0x00, 0x05, 0x01]) + b"tail"
    assert decoder.feed(orphan) == (None, False)


def first_chunk(length: int, data: bytes, flags: int = protocol.FLAG_FIRST, handle: int = 7) -> bytes:
    return bytes([protocol.CHUNK, flags, 0, handle, 0]) + struct.pack("<IH", length, 0x0029) + data


def test_a_first_chunk_too_short_for_its_header_is_dropped():
    decoder = Decoder()
    for size in range(5, 11):
        assert decoder.feed(bytes([protocol.CHUNK, protocol.FLAG_FIRST, 0, 7, 0]) + bytes(size - 5)) == (None, False)
    # The decoder still takes the next whole message.
    message, _ack = decoder.feed(first_chunk(2, b"ok", protocol.FLAG_FIRST | protocol.FLAG_LAST))
    assert message.payload == b"ok"


def test_a_message_that_never_ends_stops_being_kept(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_MESSAGE", 1000)
    decoder = Decoder()
    decoder.feed(first_chunk(1, b"x"))
    more = bytes([protocol.CHUNK, protocol.FLAG_NEEDS_ACK, 0, 7, 1]) + bytes(240)
    for _ in range(4096):
        message, needs_ack = decoder.feed(more)
        # Still acknowledged, so the strap isn't left waiting, but not kept.
        assert message is None and needs_ack
        assert len(decoder._buffer) <= 1024
    # Its end is dropped whole, and the decoder is ready for the next message.
    assert decoder.feed(bytes([protocol.CHUNK, protocol.FLAG_LAST, 0, 7, 2]) + b"end") == (None, False)
    message, _ack = decoder.feed(first_chunk(2, b"ok", protocol.FLAG_FIRST | protocol.FLAG_LAST, handle=8))
    assert message.payload == b"ok"


def test_a_message_announcing_too_much_is_not_kept(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_MESSAGE", 1000)
    decoder = Decoder()
    assert decoder.feed(first_chunk(1001, bytes(200))) == (None, False)
    assert len(decoder._buffer) == 0
    assert decoder.feed(bytes([protocol.CHUNK, protocol.FLAG_LAST, 0, 7, 1]) + bytes(200)) == (None, False)


@pytest.mark.parametrize("encrypt", [False, True])
def test_the_largest_message_still_arrives(monkeypatch, encrypt):
    monkeypatch.setattr(protocol, "MAX_MESSAGE", 1000)
    payload = bytes(i & 0xFF for i in range(1000))
    assert roundtrip(payload, encrypt, mtu=247)[1] == payload
