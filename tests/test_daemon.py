import asyncio
import json
import os
import signal
import struct
from datetime import UTC, datetime
from typing import ClassVar

import pytest

import helios.daemon as d
from helios import config, summary
from helios import parsers as p
from helios.config import ConfigError, Settings, StrapAuth
from helios.radio import BluetoothStuck
from helios.strap import Fetched, LinkError, StrapError, TransferStalled


class FakeStrap:
    """Stands in for the BLE client. `fetches` maps a history type to what
    fetch() returns, or an exception to raise; anything else has no data."""

    fetches: ClassVar[dict] = {}
    block_fetch: ClassVar[bool] = False
    # Probes answered before the link goes dead; None keeps it working.
    probes_answered: ClassVar[int | None] = None

    def __init__(self, address, key, **callbacks):
        self.address = address
        self.connected = True
        self.callbacks = callbacks
        self.attempted: list[int] = []
        self.probes = 0

    async def connect(self, timeout=20.0, radio=None):
        pass

    async def disconnect(self):
        await asyncio.sleep(0)
        self.connected = False

    async def read_device_info(self):
        return {"firmware": "test"}

    async def watch(self):
        pass

    async def request_battery(self):
        return {}

    async def probe(self):
        self.probes += 1
        if self.probes_answered is not None and self.probes > self.probes_answered:
            raise LinkError("the strap stopped answering")

    async def fetch(self, kind, since, idle_timeout=20.0):
        self.attempted.append(kind)
        if self.block_fetch:
            try:
                await asyncio.sleep(30)
            finally:
                await asyncio.sleep(0.05)  # like the stop_notify round trip
        outcome = self.fetches.pop(kind, None)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setattr(d, "DATABASE", tmp_path / "helios.db")
    monkeypatch.setattr(d, "LOCK_FILE", tmp_path / "daemon.lock")
    monkeypatch.setattr(d, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(d, "CONTROL_SOCKET", runtime / "control.sock")
    monkeypatch.setattr(d, "Strap", FakeStrap)
    monkeypatch.setattr(d, "load_auth", lambda: StrapAuth("AA:BB:CC:DD:EE:FF", bytes(16)))
    monkeypatch.setattr(d, "load_settings", lambda: Settings(live=True))
    FakeStrap.fetches = {}
    FakeStrap.block_fetch = False
    FakeStrap.probes_answered = None
    return tmp_path


def test_one_failing_history_type_does_not_stop_the_rest(isolated):
    async def main():
        daemon = d.Daemon(Settings())
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        FakeStrap.fetches = {d.FETCH_ACTIVITY: StrapError("history transfer stalled")}
        await daemon.sync(strap)
        return daemon, strap

    daemon, strap = asyncio.run(main())
    assert strap.attempted == d.SYNC_ORDER
    assert daemon.last_sync is not None
    assert not daemon.syncing


def test_cursor_moves_past_records_the_decoder_drops(isolated):
    daemon = d.Daemon(Settings())
    t0 = int(datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp())
    # One good HRV reading, then three minutes the strap marked with 0.
    data = b"".join(struct.pack("<IbB", t0 + 60 * i, 0, 45 if i == 0 else 0) for i in range(4))
    end = daemon._store_batch(Fetched(d.FETCH_HRV, datetime.fromtimestamp(t0, UTC), data))
    assert end == t0 + 3 * 60 + 1  # just past the last record, valid or not
    assert daemon.store.latest(p.HRV) == (t0, 45)


class SecondsStrap:
    """Per-second heart rate as the strap serves it: every record from the
    requested whole minute on."""

    def __init__(self, rates: dict[int, int]):
        self.rates = rates
        self.requests: list[int] = []

    async def fetch(self, kind, since, idle_timeout=20.0):
        start = int(since.timestamp())
        start -= start % 60
        self.requests.append(start)
        times = sorted(t for t in self.rates if t >= start)
        if not times:
            return None
        data = b"".join(struct.pack("<IB", t, self.rates[t]) for t in times)
        return Fetched(kind, datetime.fromtimestamp(times[0], UTC), data)


def test_per_second_replay_keeps_extremes_the_strap_has_since_dropped(isolated, monkeypatch):
    t0 = int(datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp())
    # Half a minute: a dip to 50 and a rise to 100 among readings of 70.
    rates = {t0 + s: 50 if 3 <= s < 8 else 100 if 10 <= s < 15 else 70 for s in range(30)}
    strap = SecondsStrap(rates)
    daemon = d.Daemon(Settings())
    clock = [t0 + 30]
    monkeypatch.setattr(d.time, "time", lambda: clock[0])

    def sync():
        asyncio.run(daemon._sync_kind(strap, d.FETCH_HEART_RATE_SECONDS, t0 - 86400, clock[0]))

    sync()
    assert daemon.store.cursor(d.FETCH_HEART_RATE_SECONDS, 0) == t0 + 30
    # The strap drops the minute's first 20 seconds and records another minute.
    for s in range(20):
        del rates[t0 + s]
    rates.update({t0 + s: 80 for s in range(30, 90)})
    clock[0] = t0 + 90
    sync()
    assert strap.requests[-1] == t0  # the half minute came again
    assert daemon.store.stats(p.HEART_RATE_LOW, t0, t0 + 60)["min"] == 50
    assert daemon.store.stats(p.HEART_RATE_HIGH, t0, t0 + 60)["max"] == 100
    assert daemon.store.latest(p.HEART_RATE_HIGH) == (t0 + 60, 80)
    # Nothing new: one small fetch, and the cursor stays where it was.
    fetches = len(strap.requests)
    sync()
    assert len(strap.requests) == fetches + 1
    assert daemon.store.cursor(d.FETCH_HEART_RATE_SECONDS, 0) == t0 + 90


def test_per_second_records_from_the_future_are_dropped(isolated, monkeypatch):
    t0 = int(datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp())
    monkeypatch.setattr(d.time, "time", lambda: t0 + 60)
    data = b"".join(struct.pack("<IB", t, 70) for t in [*range(t0, t0 + 60), *range(t0 + 3600, t0 + 3610)])
    daemon = d.Daemon(Settings())
    end = daemon._store_batch(Fetched(d.FETCH_HEART_RATE_SECONDS, datetime.fromtimestamp(t0, UTC), data))
    assert end == t0 + 60
    assert daemon.store.latest(p.HEART_RATE_LOW) == (t0, 70)


def test_minute_series_cursor_counts_records(isolated):
    daemon = d.Daemon(Settings())
    start = datetime(2026, 10, 31, 12, tzinfo=UTC)
    end = daemon._store_batch(Fetched(d.FETCH_STRESS_AUTO, start, bytes([0xFF, 40, 0xFF])))
    assert end == int(start.timestamp()) + 180


def test_retry_delay_checks_often_soon_after_leaving():
    assert d.retry_delay(30, live=True, sync_minutes=10) == 10
    assert d.retry_delay(20 * 60, live=True, sync_minutes=10) == 60
    assert d.retry_delay(3 * 3600, live=True, sync_minutes=10) == 180
    assert d.retry_delay(30, live=False, sync_minutes=10) == 600


def test_heart_rate_events_skip_small_wobbles(isolated, monkeypatch):
    daemon = d.Daemon(Settings())
    sent = []
    monkeypatch.setattr(daemon, "broadcast", sent.append)
    clock = iter([100.0, 101.0, 102.0, 103.0, 107.0, 120.0])
    monkeypatch.setattr(d.time, "time", lambda: next(clock))
    # 71 and 70 are wobbles; 74 jumps; 75 at +4 s waits; 75 at +17 s is a keepalive.
    for bpm in [70, 71, 70, 74, 75, 75]:
        daemon.on_heart_rate(bpm)
    assert [json.loads(m)["bpm"] for m in sent] == [70, 74, 75]


def test_second_daemon_is_refused(isolated):
    fd = d._lock_instance()
    try:
        with pytest.raises(SystemExit):
            d._lock_instance()
    finally:
        os.close(fd)


def test_sigterm_during_a_fetch_exits_cleanly(isolated):
    FakeStrap.block_fetch = True

    async def main():
        asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(d.run_daemon(), 5)

    asyncio.run(main())
    assert not (isolated / "run" / "control.sock").exists()


def test_socket_serves_state_and_accepts_sync(isolated):
    async def main():
        daemon = d.Daemon(Settings())
        d.RUNTIME_DIR.mkdir(exist_ok=True)
        server = await daemon.serve()
        reader, writer = await asyncio.open_unix_connection(str(d.CONTROL_SOCKET))
        first = json.loads(await reader.readline())
        writer.write(b"not json\n" + b'{"cmd": "sync"}\n')
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()
        server.close()
        return first, daemon.kick.is_set()

    state, kicked = asyncio.run(main())
    assert state["type"] == "state"
    assert "version" in state and "timezone" in state
    assert kicked


def test_a_sync_where_everything_failed_is_not_reported_as_synced(isolated):
    async def main():
        daemon = d.Daemon(Settings())
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        FakeStrap.fetches = {kind: StrapError("stalled") for kind in d.SYNC_ORDER}
        await daemon.sync(strap)
        return daemon

    assert asyncio.run(main()).last_sync is None


def test_cursor_ignores_timestamps_from_the_future(isolated):
    daemon = d.Daemon(Settings())
    t0 = int(datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp())
    data = struct.pack("<IbB", t0, 0, 45) + struct.pack("<IbB", 0xFFFFFFF0, 0, 50)
    assert daemon._store_batch(Fetched(d.FETCH_HRV, datetime.fromtimestamp(t0, UTC), data)) == t0 + 1
    # A first-record time from the future doesn't move the cursor either.
    future = datetime.fromtimestamp(0xFFFFFF00, UTC)
    assert daemon._store_batch(Fetched(d.FETCH_HRV, future, struct.pack("<IbB", 0xFFFFFF00, 0, 45))) <= t0


def test_a_dead_link_ends_the_sync_instead_of_failing_every_type(isolated):
    async def main():
        daemon = d.Daemon(Settings())
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        FakeStrap.fetches = {d.SYNC_ORDER[1]: LinkError("Bluetooth stopped answering (start_notify)")}
        with pytest.raises(LinkError):
            await daemon.sync(strap)
        return daemon, strap

    daemon, strap = asyncio.run(main())
    assert strap.attempted == d.SYNC_ORDER[:2]
    assert not daemon.syncing
    assert daemon.last_sync is None


def test_a_sync_starts_by_checking_the_link(isolated):
    FakeStrap.probes_answered = 0

    async def main():
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        with pytest.raises(LinkError):
            await d.Daemon(Settings()).sync(strap)
        return strap

    assert asyncio.run(main()).attempted == []


def test_quiet_heart_rate_checks_the_link_between_syncs(isolated, monkeypatch):
    monkeypatch.setattr(d, "QUIET_PROBE_SECONDS", 0.05)
    # The sync's own probe answers; the next one, between syncs, does not.
    FakeStrap.probes_answered = 1

    async def main():
        daemon = d.Daemon(Settings(live=True))
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        with pytest.raises(LinkError):
            await asyncio.wait_for(daemon._session(strap), 5)
        return strap

    assert asyncio.run(main()).probes == 2


def test_a_dead_link_is_replaced_with_a_new_connection(isolated, monkeypatch):
    FakeStrap.probes_answered = 0
    connects = []
    original = FakeStrap.connect

    async def connect(self, timeout=20.0, radio=None):
        connects.append(self)
        await original(self, timeout, radio)

    monkeypatch.setattr(FakeStrap, "connect", connect)

    async def main():
        daemon = d.Daemon(Settings(live=True))
        waits = []

        async def no_wait(seconds):
            waits.append(seconds)
            if len(waits) >= 2:
                raise asyncio.CancelledError

        daemon._wait = no_wait
        with pytest.raises(asyncio.CancelledError):
            await daemon.run()
        return waits

    waits = asyncio.run(main())
    assert len(connects) >= 2
    assert all(not strap.connected for strap in connects)
    assert waits[0] <= 10


def test_each_workout_moves_the_cursor_past_its_end(isolated):
    from test_parsers import workout_summary

    daemon = d.Daemon(Settings())
    start = datetime(2026, 11, 17, 22, 40, 25, tzinfo=UTC)
    t0 = int(start.timestamp())
    end = daemon._store_batch(Fetched(d.FETCH_WORKOUT, start, workout_summary()))
    assert end == t0 + 2460 + 60
    (stored,) = daemon.store.workouts(0)
    assert (stored["start"], stored["avgHr"], stored["load"]) == (t0, 127, 45)
    # One the decoder can't read is stepped over, not retried forever.
    assert daemon._store_batch(Fetched(d.FETCH_WORKOUT, start, b"\x00")) == t0 + 60
    assert len(daemon.store.workouts(0)) == 1


def test_active_energy_is_a_minute_series(isolated):
    daemon = d.Daemon(Settings())
    start = datetime(2026, 10, 31, 12, tzinfo=UTC)
    minute = bytes([1, 0, 0, 0, 0, 0, 0, 0])
    data = minute + bytes([7, 1, 17, 2]) + minute + bytes(4) + minute + bytes([20, 2, 0xE8, 5])
    end = daemon._store_batch(Fetched(d.FETCH_ACTIVITY_EXTRA, start, data))
    assert end == int(start.timestamp()) + 180
    assert daemon.store.stats(p.CALORIES, 0, 1 << 40)["sum"] == 3


def test_unanswered_fetches_in_a_row_end_the_connection(isolated):
    async def main(fetches):
        daemon = d.Daemon(Settings())
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        FakeStrap.fetches = fetches
        try:
            await daemon.sync(strap)
        except LinkError as error:
            return strap, error
        return strap, None

    # The history service has stopped: no point timing out on every type.
    strap, error = asyncio.run(main({d.SYNC_ORDER[0]: TimeoutError(), d.SYNC_ORDER[1]: TimeoutError()}))
    assert error is not None
    assert strap.attempted == d.SYNC_ORDER[:2]

    # One slow answer among working ones is only that type's failure.
    strap, error = asyncio.run(main({d.SYNC_ORDER[0]: TimeoutError(), d.SYNC_ORDER[2]: TimeoutError()}))
    assert error is None
    assert strap.attempted == d.SYNC_ORDER


def test_history_that_stops_arriving_gets_one_reconnect(isolated):
    async def main():
        daemon = d.Daemon(Settings())
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        now = int(datetime.now(UTC).timestamp())
        # The strap files activity every minute, yet says it has none for two hours.
        daemon.store.set_cursor(d.FETCH_ACTIVITY, now - 2 * 3600)
        with pytest.raises(LinkError):
            await daemon.sync(strap)
        first = list(strap.attempted)
        # If the new connection has nothing either, carry on with the rest.
        await daemon.sync(strap)
        return first, strap.attempted[len(first) :], daemon

    first, second, daemon = asyncio.run(main())
    assert first == [d.FETCH_ACTIVITY]
    assert second == d.SYNC_ORDER
    assert daemon.last_sync is not None


def test_history_a_little_behind_is_left_alone(isolated):
    async def main():
        daemon = d.Daemon(Settings())
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        daemon.store.set_cursor(d.FETCH_ACTIVITY, int(datetime.now(UTC).timestamp()) - 20 * 60)
        await daemon.sync(strap)
        return strap

    assert asyncio.run(main()).attempted == d.SYNC_ORDER


def test_a_workout_ending_in_the_future_does_not_take_the_cursor_there(isolated):
    from test_parsers import _field, _message, _varint

    daemon = d.Daemon(Settings())
    start = datetime.now(UTC).replace(microsecond=0)
    summary = struct.pack("<H", p.WORKOUT_VERSION) + _message(7, _field(1, 0, _varint(2**30)))
    end = daemon._store_batch(Fetched(d.FETCH_WORKOUT, start, summary))
    assert end == int(start.timestamp()) + 60
    assert daemon.store.workouts(0) == []


def test_a_drop_on_a_faint_signal_says_so(isolated, monkeypatch):
    daemon = d.Daemon(Settings())
    daemon._address = "AA:BB:CC:DD:EE:FF"
    daemon.radio.track(daemon._address)
    assert daemon._signal() is None and daemon._weak_signal() is None
    now = datetime.now(UTC).timestamp()
    daemon.radio._signals[daemon._address] = (-88, now - 20)
    assert daemon._signal() == {"rssi": -88, "at": int(now - 20)}
    assert daemon._weak_signal() == -88
    assert json.loads(daemon.state_message())["signal"]["rssi"] == -88
    # A strong signal, or one heard too long ago to describe the link, isn't blamed.
    daemon.radio._signals[daemon._address] = (-70, now - 20)
    assert daemon._weak_signal() is None
    daemon.radio._signals[daemon._address] = (-88, now - 600)
    assert daemon._weak_signal() is None


NOON = int(datetime(2026, 11, 22, 12, tzinfo=UTC).timestamp())


class BatchStrap:
    """Hands out the given batches, one a fetch, then says it has no more."""

    def __init__(self, *batches: Fetched):
        self.batches = list(batches)
        self.asked: list[int] = []

    async def fetch(self, kind, since, idle_timeout=20.0):
        self.asked.append(int(since.timestamp()))
        return self.batches.pop(0) if self.batches else None


def at(monkeypatch, now: int) -> None:
    monkeypatch.setattr(d.time, "time", lambda: now)


def test_a_minute_batch_from_the_future_is_neither_stored_nor_followed(isolated, monkeypatch):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    minute = bytes([1, 1, 5, 60, 0, 0, 0, 0])  # 5 steps at 60 bpm
    # Stamped 2106, as an all-ones time decodes.
    strap = BatchStrap(Fetched(d.FETCH_ACTIVITY, datetime.fromtimestamp(4294967280, UTC), minute))
    asyncio.run(daemon._sync_kind(strap, d.FETCH_ACTIVITY, NOON - 86400, NOON))
    assert daemon.store.cursor(d.FETCH_ACTIVITY, -1) == -1  # never set
    assert daemon.store.latest(p.STEPS) is None and daemon.store.latest(p.HEART_RATE) is None
    # The next sync asks again from where it was, and takes a good batch.
    strap = BatchStrap(Fetched(d.FETCH_ACTIVITY, datetime.fromtimestamp(NOON - 120, UTC), minute * 2))
    asyncio.run(daemon._sync_kind(strap, d.FETCH_ACTIVITY, NOON - 86400, NOON))
    assert strap.asked[0] == NOON - 86400
    assert daemon.store.cursor(d.FETCH_ACTIVITY, -1) == NOON
    assert daemon.store.latest(p.STEPS) == (NOON - 60, 5)


@pytest.mark.parametrize("kind", [d.FETCH_ACTIVITY, d.FETCH_ACTIVITY_EXTRA, d.FETCH_STRESS_AUTO, d.FETCH_TEMPERATURE])
def test_no_minute_series_follows_a_future_start(isolated, monkeypatch, kind):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    size = d.MINUTE_SERIES[kind][0]
    assert daemon._store_batch(Fetched(kind, datetime.fromtimestamp(NOON + 3600, UTC), bytes([40]) * size * 3)) == 0
    (rows,) = daemon.store.db.execute("SELECT COUNT(*) FROM samples").fetchone()
    assert rows == 0


def test_a_minute_batch_longer_than_the_time_since_it_began_stops_at_now(isolated, monkeypatch):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    # Began ten minutes ago, yet claims three hours of stress readings.
    start = datetime.fromtimestamp(NOON - 600, UTC)
    end = daemon._store_batch(Fetched(d.FETCH_STRESS_AUTO, start, bytes([40]) * 180))
    assert end == NOON
    newest = daemon.store.latest(p.STRESS)
    assert newest is not None and newest[0] <= NOON + d.CLOCK_SLACK
    # A batch that runs to the present minute, as real ones do, is all kept.
    daemon.store.db.execute("DELETE FROM samples")
    end = daemon._store_batch(Fetched(d.FETCH_STRESS_AUTO, start, bytes([40]) * 11))
    assert end == NOON + 60
    assert daemon.store.stats(p.STRESS, 0, 1 << 40)["count"] == 11


def test_a_record_from_the_future_is_not_stored(isolated, monkeypatch):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    data = struct.pack("<IbB", NOON - 120, 0, 45) + struct.pack("<IbB", 0xFFFFFFF0, 0, 199)
    end = daemon._store_batch(Fetched(d.FETCH_HRV, datetime.fromtimestamp(NOON - 120, UTC), data))
    assert end == NOON - 120 + 1
    assert daemon.store.latest(p.HRV) == (NOON - 120, 45)
    # A later good reading is the latest, as it should be.
    daemon._store_batch(
        Fetched(d.FETCH_HRV, datetime.fromtimestamp(NOON - 60, UTC), struct.pack("<IbB", NOON - 60, 0, 46))
    )
    daemon.refresh_snapshot()
    assert json.loads(daemon.state_message())["latest"]["hrv"] == {"value": 46.0, "at": NOON - 60}


def test_clocks_a_little_apart_lose_nothing(isolated, monkeypatch):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    # The strap's clock runs two minutes ahead of this computer's.
    data = struct.pack("<IbB", NOON + 120, 0, 45)
    end = daemon._store_batch(Fetched(d.FETCH_HRV, datetime.fromtimestamp(NOON + 120, UTC), data))
    assert end == NOON + 121
    assert daemon.store.latest(p.HRV) == (NOON + 120, 45)


def test_an_invalid_reading_at_a_real_time_still_moves_the_cursor(isolated, monkeypatch):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    data = struct.pack("<IbB", NOON - 300, 0, 0) + struct.pack("<IbB", NOON - 240, 0, 255)
    assert daemon._store_batch(Fetched(d.FETCH_HRV, datetime.fromtimestamp(NOON - 300, UTC), data)) == NOON - 239
    assert daemon.store.latest(p.HRV) is None


def test_sleep_and_pai_reports_from_the_future_are_not_stored(isolated, monkeypatch):
    from test_parsers import pai_record, sleep_record

    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    midnight = NOON - 12 * 3600
    good = sleep_record(NOON - 3600, midnight, 23 * 60, 30 * 60, [(23 * 60, 30 * 60, 4)])
    ahead = sleep_record(NOON + 40 * 86400, midnight + 40 * 86400, 23 * 60, 30 * 60, [(23 * 60, 30 * 60, 4)])
    daemon._store_batch(Fetched(d.FETCH_SLEEP_SESSION, datetime.fromtimestamp(NOON - 3600, UTC), good + ahead))
    assert len(daemon.store.sleep_sessions(0)) == 1
    daemon._store_batch(
        Fetched(
            d.FETCH_PAI,
            datetime.fromtimestamp(NOON - 60, UTC),
            pai_record(NOON - 60, 2.0, 30.0) + pai_record(NOON + 86400, 9.0, 90.0),
        )
    )
    assert [day["total"] for day in daemon.store.pai_days(0)] == [30.0]


def test_a_cursor_left_in_the_future_goes_back_for_what_was_missed(isolated, monkeypatch):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    # As 0.6 left it after a batch stamped 2106.
    daemon.store.set_cursor(d.FETCH_ACTIVITY, 4294967340)
    minute = bytes([1, 1, 5, 60, 0, 0, 0, 0])
    since = NOON - 14 * 86400
    strap = BatchStrap(Fetched(d.FETCH_ACTIVITY, datetime.fromtimestamp(NOON - 180, UTC), minute * 3))
    asyncio.run(daemon._sync_kind(strap, d.FETCH_ACTIVITY, since, NOON))
    assert strap.asked[0] == since
    assert daemon.store.cursor(d.FETCH_ACTIVITY, 0) == NOON
    assert not daemon._history_stalled(NOON)
    # Once is enough: the next sync carries on from the repaired cursor.
    strap = BatchStrap()
    asyncio.run(daemon._sync_kind(strap, d.FETCH_ACTIVITY, since, NOON))
    assert strap.asked == [NOON]


def test_an_infinite_pai_total_never_reaches_a_reader(isolated, monkeypatch):
    from test_parsers import pai_record

    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    daemon.store.add_samples([(p.SPO2, NOON - 300, 97)])
    bad = pai_record(NOON - 60, 0.0, float("inf"))
    end = daemon._store_batch(Fetched(d.FETCH_PAI, datetime.fromtimestamp(NOON - 60, UTC), bad))
    assert end == NOON  # stepped over, not retried for ever
    daemon.refresh_snapshot()
    state = json.loads(daemon.state_message(), parse_constant=pytest.fail)
    assert state["today"]["pai"] is None and state["latest"]["spo2"]["value"] == 97
    # A good record later takes its place as usual.
    daemon._store_batch(Fetched(d.FETCH_PAI, datetime.fromtimestamp(NOON - 30, UTC), pai_record(NOON - 30, 2.0, 30.0)))
    daemon.refresh_snapshot()
    assert json.loads(daemon.state_message())["today"]["pai"]["total"] == 30.0


def test_messages_are_standard_json_whatever_they_are_given(isolated):
    line = d.encode({"type": "state", "a": float("inf"), "b": [1.5, float("nan")], "c": {"d": float("-inf"), "e": "x"}})
    assert json.loads(line, parse_constant=pytest.fail) == {
        "type": "state",
        "a": None,
        "b": [1.5, None],
        "c": {"d": None, "e": "x"},
    }
    # Even a value that slipped into the daemon's own state can't break a snapshot.
    daemon = d.Daemon(Settings())
    daemon.snapshot = {"today": {"pai": {"total": float("inf")}}}
    assert json.loads(daemon.state_message(), parse_constant=pytest.fail)["today"]["pai"]["total"] is None


def test_transfers_that_stall_in_a_row_end_the_connection(isolated):
    async def main(fetches):
        daemon = d.Daemon(Settings())
        strap = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
        FakeStrap.fetches = fetches
        try:
            await daemon.sync(strap)
        except LinkError as error:
            return strap, error
        return strap, None

    first, second, third = d.SYNC_ORDER[:3]
    # The strap answers the request and then sends nothing, twice running.
    strap, error = asyncio.run(
        main({first: TransferStalled("history transfer stalled"), second: TransferStalled("again")})
    )
    assert error is not None and strap.attempted == [first, second]
    # A stall and an unanswered request count together.
    strap, error = asyncio.run(main({first: TransferStalled("stalled"), second: TimeoutError()}))
    assert error is not None and strap.attempted == [first, second]
    # A transfer that worked in between starts the count again.
    strap, error = asyncio.run(main({first: TransferStalled("stalled"), third: TransferStalled("stalled")}))
    assert error is None and strap.attempted == d.SYNC_ORDER
    # A batch that arrived damaged says nothing about the link.
    strap, error = asyncio.run(main({kind: StrapError("fetch failed its checksum") for kind in d.SYNC_ORDER}))
    assert error is None and strap.attempted == d.SYNC_ORDER


def test_an_auth_file_of_the_wrong_shape_asks_for_setup(isolated, monkeypatch, tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text('strap = "not a table"\n')
    monkeypatch.setattr(
        d,
        "load_auth",
        lambda: config.load_auth(path),
    )

    async def main():
        daemon = d.Daemon(Settings())

        async def stop(_seconds):
            raise asyncio.CancelledError

        daemon._wait = stop
        with pytest.raises(asyncio.CancelledError):
            await daemon.run()
        return daemon

    daemon = asyncio.run(main())
    assert daemon.status == "needs-auth" and "[strap]" in daemon.message


def test_a_worker_that_dies_takes_the_service_down_with_a_failure(isolated, monkeypatch):
    def broken():
        raise AttributeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(d, "load_auth", broken)

    async def main():
        with pytest.raises(SystemExit) as stopped:
            await asyncio.wait_for(d.run_daemon(), 5)
        return stopped.value.code

    assert asyncio.run(main()) == 1
    # Cleaned up on the way out: no socket, and the lock is free for the restart.
    assert not (isolated / "run" / "control.sock").exists()
    os.close(d._lock_instance())


def test_a_dead_ticker_is_noticed_too(isolated, monkeypatch):
    async def broken(_daemon):
        raise RuntimeError("ticker bug")

    monkeypatch.setattr(d, "_ticker", broken)

    async def main():
        with pytest.raises(SystemExit) as stopped:
            await asyncio.wait_for(d.run_daemon(), 5)
        return stopped.value.code

    assert asyncio.run(main()) == 1
    os.close(d._lock_instance())


def test_bluetooth_that_never_answers_restarts_the_service(isolated, monkeypatch):
    async def stuck(self, timeout=20.0, radio=None):
        raise BluetoothStuck("Bluetooth has not answered for ten minutes")

    monkeypatch.setattr(FakeStrap, "connect", stuck)

    async def main():
        with pytest.raises(SystemExit) as stopped:
            await asyncio.wait_for(d.run_daemon(), 5)
        return stopped.value.code

    assert asyncio.run(main()) == 1


def test_a_config_error_is_not_a_crash(isolated, monkeypatch):
    def missing():
        raise ConfigError("auth.toml does not exist; run setup to fetch the strap's key")

    monkeypatch.setattr(d, "load_auth", missing)

    async def main():
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(d.run_daemon(), 5)  # ends on the signal, without SystemExit

    asyncio.run(main())


def test_an_offline_snapshot_follows_the_clock(isolated, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(d.time, "time", lambda: clock[0])
    daemon = d.Daemon(Settings())
    midnight = summary.local_midnight(datetime.now().date())
    # Late yesterday: steps taken, and a snapshot built just before midnight.
    daemon.store.add_samples([(p.STEPS, midnight - 120, 123), (p.HEART_RATE, midnight - 120, 70)])
    real_build = d.summary.build
    monkeypatch.setattr(d.summary, "build", lambda store: real_build(store, datetime.fromtimestamp(clock[0])))
    clock[0] = midnight - 60
    daemon.refresh_snapshot()
    assert daemon.snapshot["today"]["steps"] == 123
    sent = []
    monkeypatch.setattr(daemon, "broadcast", sent.append)
    # Nothing rebuilds a snapshot that is still fresh and of the same day...
    clock[0] = midnight - 30
    daemon.refresh_stale_snapshot()
    assert sent == []
    # ...but the date changing does, however recent it is: yesterday's steps aren't today's.
    clock[0] = midnight + 5
    daemon.refresh_stale_snapshot()
    assert daemon.snapshot["today"]["steps"] is None and len(sent) == 1
    # And age does, so "the last 24 hours" keeps ending now while the strap is away.
    clock[0] = midnight + 5 + d.SNAPSHOT_MAX_AGE
    daemon.refresh_stale_snapshot()
    assert daemon.snapshot["day"]["end"] == int(clock[0]) and len(sent) == 2
    # A sync under way rebuilds it itself when it ends.
    daemon.syncing = True
    clock[0] += 10 * d.SNAPSHOT_MAX_AGE
    daemon.refresh_stale_snapshot()
    assert len(sent) == 2


def test_no_bluetooth_on_this_computer_is_not_blamed_on_the_strap(isolated, monkeypatch):
    class NoService(Exception):
        dbus_error = "org.freedesktop.DBus.Error.NameHasNoOwner"

    class BleakBluetoothNotAvailableError(Exception):
        pass

    async def offline_message(error):
        async def fail(self, timeout=20.0, radio=None):
            raise error

        monkeypatch.setattr(FakeStrap, "connect", fail)
        daemon = d.Daemon(Settings())

        async def stop(_seconds):
            raise asyncio.CancelledError

        daemon._wait = stop
        with pytest.raises(asyncio.CancelledError):
            await daemon.run()
        return daemon.status, daemon.message

    for error in (NoService("Could not activate remote peer 'org.bluez'"), BleakBluetoothNotAvailableError("off")):
        assert asyncio.run(offline_message(error)) == ("offline", "Bluetooth is off or missing on this computer.")
    assert asyncio.run(offline_message(StrapError("strap is not advertising nearby"))) == (
        "offline",
        "The strap is out of range or switched off.",
    )


class MinuteStrap:
    """Keeps timestamped records and answers a fetch as the strap does: from
    the start of the requested minute on."""

    def __init__(self):
        self.records: dict[int, int] = {}
        self.fetches = 0

    async def fetch(self, kind, since, idle_timeout=20.0):
        self.fetches += 1
        start = int(since.timestamp())
        start -= start % 60
        times = sorted(t for t in self.records if t >= start)
        if not times:
            return None
        data = b"".join(struct.pack("<IbB", t, 0, self.records[t]) for t in times)
        return Fetched(kind, datetime.fromtimestamp(times[0], UTC), data)


def test_a_reading_later_in_the_last_readings_minute_is_not_passed_over(isolated, monkeypatch):
    at(monkeypatch, NOON + 120)
    daemon = d.Daemon(Settings())
    strap = MinuteStrap()
    strap.records[NOON + 30] = 71

    def sync():
        asyncio.run(daemon._sync_kind(strap, d.FETCH_MANUAL_HR, NOON - 86400, NOON + 120))

    sync()
    # A second manual reading, twenty seconds after the first.
    strap.records[NOON + 50] = 74
    sync()
    assert daemon.store.stats(p.MANUAL_HR, 0, 1 << 40)["count"] == 2
    assert daemon.store.latest(p.MANUAL_HR) == (NOON + 50, 74)
    # With nothing new, a sync is one small fetch and the cursor stays put.
    before = strap.fetches
    sync()
    assert strap.fetches == before + 1
    assert daemon.store.cursor(d.FETCH_MANUAL_HR, 0) == NOON + 51


def test_a_batch_with_nothing_readable_is_stepped_past(isolated, monkeypatch):
    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    # Too short to hold one record.
    end = daemon._store_batch(Fetched(d.FETCH_HRV, datetime.fromtimestamp(NOON - 600, UTC), b"\x01\x02"))
    assert end == NOON - 540


def test_a_workout_the_database_cannot_hold_does_not_block_the_rest(isolated, monkeypatch):
    from test_parsers import _field, _message, _varint

    at(monkeypatch, NOON)
    daemon = d.Daemon(Settings())
    start = datetime.fromtimestamp(NOON - 7200, UTC)
    # Calories of -1, as protobuf sends a negative int32: a ten-byte number.
    summary = struct.pack("<H", p.WORKOUT_VERSION) + _message(7, _field(1, 0, _varint(600)))
    summary += _message(16, _field(1, 0, _varint((1 << 64) - 1)))
    end = daemon._store_batch(Fetched(d.FETCH_WORKOUT, start, summary))
    assert end == NOON - 7200 + 600 + 60
    (stored,) = daemon.store.workouts(0)
    assert stored["calories"] is None and stored["seconds"] == 600
    # And should storing still fail, the workout is stepped over, not retried for ever.
    monkeypatch.setattr(daemon.store, "add_workout", lambda *a: (_ for _ in ()).throw(OverflowError("too large")))
    later = datetime.fromtimestamp(NOON - 3600, UTC)
    assert daemon._store_batch(Fetched(d.FETCH_WORKOUT, later, summary)) == NOON - 3600 + 60


def test_an_old_connections_late_disconnect_does_not_end_the_new_one(isolated):
    daemon = d.Daemon(Settings())
    old, new = FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16)), FakeStrap("AA:BB:CC:DD:EE:FF", bytes(16))
    daemon.strap = new
    daemon.on_disconnect(old)  # BlueZ finally reports the old link down
    assert not daemon.disconnected.is_set()
    daemon.on_disconnect(new)
    assert daemon.disconnected.is_set()


def test_each_connection_reports_its_own_disconnect(isolated, monkeypatch):
    made = []
    original = FakeStrap.connect

    async def connect(self, timeout=20.0, radio=None):
        made.append(self)
        await original(self, timeout, radio)

    monkeypatch.setattr(FakeStrap, "connect", connect)
    FakeStrap.probes_answered = 0

    async def main():
        daemon = d.Daemon(Settings(live=True))

        async def stop(_seconds):
            raise asyncio.CancelledError

        daemon._wait = stop
        with pytest.raises(asyncio.CancelledError):
            await daemon.run()
        return daemon

    daemon = asyncio.run(main())
    (strap,) = made
    assert strap.on_disconnect.func == daemon.on_disconnect and strap.on_disconnect.args == (strap,)


def test_the_time_zone_is_followed_when_it_changes(isolated, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    d.time.tzset()
    try:
        daemon = d.Daemon(Settings())
        daemon.refresh_snapshot()
        assert daemon.timezone == "Europe/London"
        sent = []
        monkeypatch.setattr(daemon, "broadcast", sent.append)
        daemon.follow_timezone()
        assert sent == []  # nothing changed, nothing rebuilt
        london_midnight = daemon.snapshot["week"]["start"]
        monkeypatch.setenv("TZ", "Asia/Tokyo")  # the owner has flown east
        daemon.follow_timezone()
        assert daemon.timezone == "Asia/Tokyo"
        assert json.loads(sent[-1])["timezone"] == "Asia/Tokyo"
        # The days of the trends now start at Tokyo's midnights.
        assert (daemon.snapshot["week"]["start"] - london_midnight) % 86400 != 0
    finally:
        monkeypatch.delenv("TZ")
        d.time.tzset()
