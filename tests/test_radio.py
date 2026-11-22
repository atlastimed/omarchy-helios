import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest

from helios import radio as r


class FakeScanner:
    """Records what the radio asks BlueZ for; `advertise` plays an advert."""

    running: ClassVar[list["FakeScanner"]] = []
    started: ClassVar[list[dict]] = []
    refuse = False

    def __init__(self, detection_callback, **kwargs):
        self.callback = detection_callback
        self.kwargs = kwargs

    async def start(self):
        if FakeScanner.refuse:
            raise RuntimeError("org.bluez.Error.InProgress")
        FakeScanner.running.append(self)
        FakeScanner.started.append(self.kwargs)

    async def stop(self):
        FakeScanner.running.remove(self)

    @classmethod
    def advertise(cls, address, uuids=()):
        for scanner in list(cls.running):
            scanner.callback(SimpleNamespace(address=address, name=""), SimpleNamespace(service_uuids=list(uuids)))


@pytest.fixture(autouse=True)
def fake_scanner(monkeypatch):
    FakeScanner.running, FakeScanner.started, FakeScanner.refuse = [], [], False
    monkeypatch.setattr(r, "BleakScanner", FakeScanner)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_finds_and_stops_scanning():
    async def main():
        radio = r.Radio()
        search = asyncio.create_task(radio.find_address("aa:bb", 5))
        await settle()
        FakeScanner.advertise("CC:DD")
        FakeScanner.advertise("AA:BB")
        device = await search
        assert device.address == "AA:BB"
        assert FakeScanner.running == []

    asyncio.run(main())


def test_timeout_returns_none():
    async def main():
        assert await r.Radio().find_address("AA:BB", 0.05) is None
        assert FakeScanner.running == []

    asyncio.run(main())


def test_concurrent_searches_share_one_unfiltered_scanner():
    async def main():
        radio = r.Radio()
        other = asyncio.create_task(radio.find_address("M", None))
        strap = asyncio.create_task(radio.find_address("S", 5))
        await settle()
        # BlueZ 5.87 crashes on UUID discovery filters: none is ever set.
        assert FakeScanner.started == [{"scanning_mode": "active"}] and len(FakeScanner.running) == 1
        FakeScanner.advertise("S")
        await strap
        await settle()
        assert len(FakeScanner.running) == 1 and len(FakeScanner.started) == 1
        FakeScanner.advertise("M")
        assert (await other).address == "M"
        assert FakeScanner.running == []

    asyncio.run(main())


def test_scan_left_alone_during_a_connect(monkeypatch):
    monkeypatch.setattr(r, "REFRESH", 0.01)

    async def main():
        radio = r.Radio()
        other = asyncio.create_task(radio.find_address("M", None))
        await settle()
        async with radio:
            # A refresh restarts the scan, which BlueZ refuses mid-connect.
            await asyncio.sleep(0.05)
            assert len(FakeScanner.started) == 1
        await asyncio.sleep(0.05)
        assert len(FakeScanner.started) > 1 and len(FakeScanner.running) == 1
        other.cancel()
        with pytest.raises(asyncio.CancelledError):
            await other
        await asyncio.sleep(0.01)  # the scan is stopped behind the cancelled search
        assert FakeScanner.running == []

    asyncio.run(main())


def test_refused_start_during_a_connect_is_retried(monkeypatch):
    monkeypatch.setattr(r, "RETRY", 0.01)

    async def main():
        radio = r.Radio()
        FakeScanner.refuse = True
        async with radio:
            search = asyncio.create_task(radio.find_address("AA", 2))
            await asyncio.sleep(0.03)
            assert FakeScanner.running == []
        FakeScanner.refuse = False
        await asyncio.sleep(0.03)
        FakeScanner.advertise("AA")
        assert (await search).address == "AA"

    asyncio.run(main())


def test_refused_start_outside_a_connect_raises():
    async def main():
        FakeScanner.refuse = True
        with pytest.raises(RuntimeError):
            await r.Radio().find_address("AA", 1)

    asyncio.run(main())


def test_long_searches_refresh_the_session(monkeypatch):
    monkeypatch.setattr(r, "REFRESH", 0.02)

    async def main():
        search = asyncio.create_task(r.Radio().find_address("AA", 0.1))
        assert await search is None
        assert len(FakeScanner.started) >= 3

    asyncio.run(main())


class Busy(Exception):
    dbus_error = "org.bluez.Error.InProgress"


def test_busy_refusals_are_retried_and_do_not_count_as_silence(monkeypatch):
    monkeypatch.setattr(r, "RETRY", 0.01)

    class Refusing(FakeScanner):
        refusals = 3

        async def start(self):
            if Refusing.refusals:
                Refusing.refusals -= 1
                raise Busy()
            await super().start()

    monkeypatch.setattr(r, "BleakScanner", Refusing)

    async def main():
        began = asyncio.get_running_loop().time()
        assert await r.Radio().find_address("AA", 0.05) is None
        # The 50 ms only started once a scan was running.
        assert asyncio.get_running_loop().time() - began >= 0.07
        assert len(FakeScanner.started) == 1 and FakeScanner.running == []

    asyncio.run(main())


def test_a_cancelled_search_never_leaks_a_scan(monkeypatch):
    class Slow(FakeScanner):
        async def start(self):
            await asyncio.sleep(0.02)  # BlueZ answers StartDiscovery later
            await super().start()

    monkeypatch.setattr(r, "BleakScanner", Slow)

    async def main():
        search = asyncio.create_task(r.Radio().find_address("AA", 5))
        await asyncio.sleep(0.005)  # cancelled while the start is in flight
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search
        await asyncio.sleep(0.05)
        assert len(FakeScanner.started) == 1 and FakeScanner.running == []

    asyncio.run(main())


def test_a_tracked_device_keeps_its_latest_signal_strength():
    radio = r.Radio()
    radio.track("aa:bb:cc:dd:ee:ff")
    assert radio.signal("AA:BB:CC:DD:EE:FF") is None
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="")
    radio._seen(device, SimpleNamespace(service_uuids=[], rssi=-72))
    radio._seen(SimpleNamespace(address="11:22:33:44:55:66", name=""), SimpleNamespace(service_uuids=[], rssi=-40))
    radio._seen(device, SimpleNamespace(service_uuids=[]))  # an advertisement with no strength keeps the last
    rssi, at = radio.signal("aa:bb:cc:dd:ee:ff")
    assert rssi == -72 and at > 0
    assert radio.signal("11:22:33:44:55:66") is None


class Unanswered(FakeScanner):
    """A scanner whose start (or stop) BlueZ doesn't answer until released."""

    hold_start: ClassVar[asyncio.Event | None] = None
    hold_stop: ClassVar[asyncio.Event | None] = None

    async def start(self):
        if Unanswered.hold_start is not None:
            await Unanswered.hold_start.wait()
        await super().start()

    async def stop(self):
        if Unanswered.hold_stop is not None:
            await Unanswered.hold_stop.wait()
        await super().stop()


@pytest.fixture
def unanswered(monkeypatch):
    Unanswered.hold_start = Unanswered.hold_stop = None
    monkeypatch.setattr(r, "BleakScanner", Unanswered)
    monkeypatch.setattr(r, "CALL_TIMEOUT", 0.03)
    monkeypatch.setattr(r, "GRACE", 0.03)
    monkeypatch.setattr(r, "RETRY", 0.01)
    return Unanswered


def test_a_start_bluez_never_answers_does_not_outlast_the_search(unanswered):
    async def main():
        unanswered.hold_start = asyncio.Event()
        radio = r.Radio()
        began = asyncio.get_running_loop().time()
        assert await asyncio.wait_for(radio.find_address("AA", 0.02), 1) is None
        took = asyncio.get_running_loop().time() - began
        # The search's own time, its grace for a refused scan, and one unanswered call.
        assert took < 0.02 + r.GRACE + 2 * r.CALL_TIMEOUT + 0.1
        assert radio.waiting and not radio._changing.locked()
        # While the request is open no second one is made; searches just find nothing.
        assert await asyncio.wait_for(radio.find_address("AA", 0.02), 1) is None
        assert FakeScanner.started == []
        return radio

    asyncio.run(main())


def test_a_scan_that_starts_late_is_taken_over_and_stopped(unanswered):
    async def main():
        unanswered.hold_start = asyncio.Event()
        radio = r.Radio()
        assert await radio.find_address("AA", 0.02) is None
        # BlueZ gets round to it after the search has gone.
        unanswered.hold_start.set()
        await asyncio.sleep(0.05)
        assert len(FakeScanner.started) == 1 and FakeScanner.running == []
        assert not radio.waiting
        # And the next search scans as usual.
        unanswered.hold_start = None
        search = asyncio.create_task(radio.find_address("AA", 1))
        await asyncio.sleep(0.02)
        FakeScanner.advertise("AA")
        assert (await search).address == "AA"
        await asyncio.sleep(0.02)
        assert FakeScanner.running == []

    asyncio.run(main())


def test_a_scan_that_starts_late_serves_a_search_still_waiting(unanswered):
    async def main():
        unanswered.hold_start = asyncio.Event()
        radio = r.Radio()
        search = asyncio.create_task(radio.find_address("AA", 1))
        await asyncio.sleep(0.06)  # past the wait for an answer
        assert radio.waiting and not search.done()
        unanswered.hold_start.set()
        await asyncio.sleep(0.03)
        FakeScanner.advertise("AA")
        assert (await asyncio.wait_for(search, 1)).address == "AA"
        await asyncio.sleep(0.02)
        assert len(FakeScanner.started) == 1 and FakeScanner.running == []

    asyncio.run(main())


def test_cancelling_a_search_does_not_wait_on_bluez(unanswered, monkeypatch):
    # As long as in the daemon: a cancelled search that waited for BlueZ's
    # answer, or for the time allowed it, would fail this.
    monkeypatch.setattr(r, "CALL_TIMEOUT", 15.0)

    async def main():
        unanswered.hold_start = asyncio.Event()
        radio = r.Radio()
        search = asyncio.create_task(radio.find_address("AA", None))
        await asyncio.sleep(0.01)  # the start is in flight
        began = asyncio.get_running_loop().time()
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(search, 0.5)
        assert asyncio.get_running_loop().time() - began < 0.1
        # The scan it asked for starts in the end, and is stopped, not left running.
        unanswered.hold_start.set()
        await asyncio.sleep(0.08)
        assert len(FakeScanner.started) == 1 and FakeScanner.running == []

    asyncio.run(main())


def test_a_stop_bluez_never_answers_is_not_waited_on_for_ever(unanswered):
    async def main():
        radio = r.Radio()
        search = asyncio.create_task(radio.find_address("AA", 1))
        await asyncio.sleep(0.01)
        unanswered.hold_stop = asyncio.Event()
        FakeScanner.advertise("AA")
        began = asyncio.get_running_loop().time()
        assert (await asyncio.wait_for(search, 1)).address == "AA"
        assert asyncio.get_running_loop().time() - began < 2 * r.CALL_TIMEOUT + 0.1
        assert radio.waiting and not radio._changing.locked()
        # Once BlueZ answers, scanning works again.
        unanswered.hold_stop.set()
        unanswered.hold_stop = None
        await asyncio.sleep(0.02)
        assert FakeScanner.running == [] and not radio.waiting
        search = asyncio.create_task(radio.find_address("BB", 1))
        await asyncio.sleep(0.02)
        FakeScanner.advertise("BB")
        assert (await search).address == "BB"

    asyncio.run(main())


def test_bluetooth_silent_for_too_long_is_reported_as_stuck(unanswered, monkeypatch):
    monkeypatch.setattr(r, "STUCK_AFTER", 0.08)

    async def main():
        unanswered.hold_start = asyncio.Event()
        radio = r.Radio()
        assert await radio.find_address("AA", 0.02) is None
        await asyncio.sleep(0.1)
        with pytest.raises(r.BluetoothStuck):
            await radio.find_address("AA", 0.02)

    asyncio.run(main())
