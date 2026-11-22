import asyncio
import struct
import zlib
from datetime import UTC, datetime

import pytest

import helios.strap as s
from helios.protocol import Message, encode_time


class HangingClient:
    """A BleakClient whose BlueZ calls are never answered, as when the strap
    rebooted for a firmware update while connected."""

    is_connected = True
    mtu_size = 247

    def __init__(self):
        self.calls: list[str] = []

    async def _hang(self, name):
        self.calls.append(name)
        await asyncio.Event().wait()

    async def start_notify(self, *_args):
        await self._hang("start_notify")

    async def stop_notify(self, *_args):
        await self._hang("stop_notify")

    async def write_gatt_char(self, *_args, **_kwargs):
        await self._hang("write")

    async def read_gatt_char(self, *_args):
        await self._hang("read")

    async def disconnect(self):
        await self._hang("disconnect")


@pytest.fixture
def strap(monkeypatch):
    monkeypatch.setattr(s, "BLUEZ_TIMEOUT", 0.05)
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    strap.client = HangingClient()  # type: ignore[assignment]
    return strap


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 5))


def test_an_unanswered_request_fails_the_fetch(strap):
    with pytest.raises(s.LinkError):
        run(strap.fetch(s.FETCH_STRESS_AUTO, s.datetime.now().astimezone()))
    # The history characteristic is subscribed once per connection, not here.
    assert strap.client.calls == ["write"]


def test_an_unanswered_write_fails_a_request(strap):
    with pytest.raises(s.LinkError):
        run(strap.request(s.EP_BATTERY, b"\x03", lambda _p: True))


def test_probe_reports_a_dead_link(strap):
    with pytest.raises(s.LinkError):
        run(strap.probe())
    assert strap.client.calls == ["write", "write"]


def test_probe_passes_on_a_working_link(strap, monkeypatch):
    async def battery():
        return {"level": 50}

    monkeypatch.setattr(strap, "request_battery", battery)
    run(strap.probe())


def test_disconnect_gives_up_on_an_unanswered_call(strap):
    run(strap.disconnect())
    assert strap.client is None


def test_device_info_skips_unanswered_reads(strap):
    assert run(strap.read_device_info()) == {}


BEGAN = datetime(2026, 11, 22, 9, 30, tzinfo=UTC)
DONE = bytes([s.RESPONSE, 0x02, s.SUCCESS])


class Fetch:
    """Plays the strap's side of a history fetch: answers the control
    requests, then sends `packets` and, if `end` is given, the end-of-data
    reply. `gap` paces the packets; `forever` repeats them without end."""

    def __init__(self, strap, packets=(), end=None, gap=0.0, forever=False, start=None):
        self.strap, self.packets, self.end, self.gap, self.forever = strap, list(packets), end, gap, forever
        self.start = encode_time(BEGAN) if start is None else start
        self.control: list[bytes] = []
        self.player: asyncio.Task | None = None
        strap._activity_control = self.answer
        strap.send = self.send

    async def answer(self, payload, command, timeout=15.0):
        self.control.append(bytes(payload))
        if command == 0x01:
            return bytes([s.RESPONSE, 0x01, s.SUCCESS]) + struct.pack("<I", 1) + self.start
        return bytes([s.RESPONSE, 0x03, s.SUCCESS])

    async def send(self, endpoint, payload, encrypt=None):
        self.player = asyncio.ensure_future(self.play())

    async def play(self):
        counter = 0
        while True:
            for packet in self.packets:
                self.strap._on_activity_data(None, bytearray([counter & 0xFF]) + packet)
                counter += 1
                if self.gap or counter % 64 == 0:
                    await asyncio.sleep(self.gap)
            if not self.forever:
                break
        if self.end is not None:
            self.strap._dispatch(Message(s.EP_ACTIVITY_FETCH, self.end))

    def stop(self):
        if self.player is not None:
            self.player.cancel()


def fetch(strap, link, idle_timeout=0.05):
    async def main():
        try:
            return await asyncio.wait_for(strap.fetch(s.FETCH_HEART_RATE_SECONDS, BEGAN, idle_timeout), 20)
        finally:
            link.stop()

    return asyncio.run(main())


KEEP = bytes([0x03, s.ACK_KEEP_ON_DEVICE])


def test_a_transfer_arrives_whole_and_is_kept_on_the_strap():
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    data = bytes(range(200)) * 3
    packets = [data[i : i + 100] for i in range(0, len(data), 100)]
    link = Fetch(strap, packets, DONE + struct.pack("<I", zlib.crc32(data)))
    fetched = fetch(strap, link)
    assert (fetched.kind, fetched.start, fetched.data) == (s.FETCH_HEART_RATE_SECONDS, BEGAN, data)
    assert link.control[-1] == KEEP
    assert len(strap._activity_chunks) == 0  # not held once handed over


def test_three_days_of_per_second_heart_rate_fit_one_transfer():
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    packet = bytes(240)
    count = 3 * 86400 * 5 // 240 + 1  # five bytes a second
    link = Fetch(strap, [packet] * count, DONE + struct.pack("<I", zlib.crc32(packet * count)))
    assert len(fetch(strap, link, idle_timeout=5).data) == 240 * count > 1_290_000


def test_a_transfer_that_goes_quiet_is_a_stall_not_a_timeout():
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    link = Fetch(strap, [bytes(100)])  # one packet, then nothing: no end-of-data reply
    with pytest.raises(s.TransferStalled) as stalled:
        fetch(strap, link)
    assert isinstance(stalled.value, s.StrapError) and not isinstance(stalled.value, s.LinkError)
    # Still acknowledged with "keep on device", so Zepp gets the data.
    assert link.control[-1] == KEEP


def test_a_damaged_batch_is_not_a_stall():
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    link = Fetch(strap, [bytes(100)], DONE + struct.pack("<I", 12345))
    with pytest.raises(s.StrapError) as failed:
        fetch(strap, link)
    assert not isinstance(failed.value, s.TransferStalled | s.LinkError)
    assert link.control[-1] == KEEP


def test_a_transfer_that_never_stops_growing_ends_the_connection(monkeypatch):
    monkeypatch.setattr(s, "MAX_TRANSFER_BYTES", 50_000)
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    link = Fetch(strap, [bytes(240)], forever=True)
    with pytest.raises(s.LinkError):
        fetch(strap, link)
    assert len(strap._activity_chunks) <= 50_000
    assert link.control[-1] == KEEP


def test_a_transfer_that_trickles_for_ever_ends_the_connection(monkeypatch):
    monkeypatch.setattr(s, "MAX_TRANSFER_SECONDS", 0.15)
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    # A packet every 10 ms: never idle for the 50 ms that would call it stalled.
    link = Fetch(strap, [b"x"], gap=0.01, forever=True)
    with pytest.raises(s.LinkError):
        fetch(strap, link)
    assert link.control[-1] == KEEP


def test_history_packets_nobody_asked_for_are_not_kept():
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    for counter in range(1000):
        strap._on_activity_data(None, bytearray([counter & 0xFF]) + bytes(240))
    assert len(strap._activity_chunks) == 0


def test_a_fetch_that_begins_at_an_impossible_time_is_refused_and_acknowledged():
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    link = Fetch(strap, start=struct.pack("<HBBBBBb", 2026, 13, 40, 25, 61, 61, 0))
    with pytest.raises(s.StrapError):
        fetch(strap, link)
    assert link.control[-1] == KEEP and link.player is None


def test_an_unreadable_chunk_does_not_break_the_notification_handler(strap, monkeypatch):
    # Too short to be a first chunk; then one the decoder trips over outright.
    strap._on_chunked_read(None, bytearray([0x03, 0x01, 0x00, 0x07, 0x00]))

    def explode(_chunk):
        raise struct.error("unpack requires a buffer of 6 bytes")

    monkeypatch.setattr(strap._decoder, "feed", explode)
    strap._on_chunked_read(None, bytearray(20))


def test_a_service_list_claiming_more_than_it_holds_is_read_as_far_as_it_goes(strap, monkeypatch):
    async def reply(endpoint, payload, matches, timeout=10.0, encrypt=None):
        # Says 500 services; carries two.
        return b"\x04" + struct.pack("<H", 500) + struct.pack("<HB", 0x0029, 1) + struct.pack("<HB", 0x004B, 0)

    monkeypatch.setattr(strap, "request", reply)
    run(strap._discover_services())
    assert strap.services == {0x0029: True, 0x004B: False}


def test_a_connect_bluez_never_finishes_is_given_up_on(monkeypatch):
    """bleak gives up connecting after its timeout, then waits without limit
    for BlueZ to confirm the disconnect: the strap's own limit ends that."""
    monkeypatch.setattr(s, "BLUEZ_TIMEOUT", 0.05)

    class Client:
        is_connected = False

        def __init__(self, device, timeout, disconnected_callback):
            pass

        async def connect(self):
            await asyncio.Event().wait()

        async def disconnect(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(s, "BleakClient", Client)

    class Radio:
        locked = False

        async def find_address(self, address, timeout):
            return "device"

        async def __aenter__(self):
            Radio.locked = True

        async def __aexit__(self, *_exc):
            Radio.locked = False

    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16))
    with pytest.raises(s.LinkError):
        run(strap.connect(timeout=0.05, radio=Radio()))
    # The radio is free for the next attempt, and nothing is left half-connected.
    assert not Radio.locked and strap.client is None


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (bytes([0x00, 72]), [72]),
        (bytes([0x01, 72, 0]), [72]),
        (bytes([0x01, 72]), []),  # says two bytes of rate, carries one
        (bytes([0x01, 0xFF, 0xFF]), []),  # 65535 is not a pulse
        (bytes([0x00, 0]), []),
        (bytes([0x00, 250]), []),
        (bytes([0x00]), []),
        (b"", []),
    ],
)
def test_live_heart_rate_frames_are_checked(frame, expected):
    heard = []
    strap = s.Strap("AA:BB:CC:DD:EE:FF", bytes(16), on_heart_rate=heard.append)
    strap._on_heart_rate(None, bytearray(frame))
    assert heard == expected
