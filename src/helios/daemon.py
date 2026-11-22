# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""The Helios daemon: one BLE connection, many local readers.

It keeps the strap connected (or connects on a schedule when `live` is off),
copies new history into SQLite every few minutes, and serves newline-delimited
JSON on a Unix socket. Each client gets the full state when it connects and
whenever it changes, plus small heart-rate events in between.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import functools
import json
import logging
import math
import os
import signal
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime

from . import __version__, summary
from . import parsers as p
from .config import (
    CONTROL_SOCKET,
    DATABASE,
    LOCK_FILE,
    RUNTIME_DIR,
    ConfigError,
    Settings,
    load_auth,
    load_settings,
)
from .radio import BluetoothStuck, Radio
from .store import CLOCK_SLACK, Store
from .strap import (
    FETCH_ACTIVITY,
    FETCH_ACTIVITY_EXTRA,
    FETCH_HEART_RATE_SECONDS,
    FETCH_HRV,
    FETCH_MANUAL_HR,
    FETCH_MAX_HR,
    FETCH_PAI,
    FETCH_RESTING_HR,
    FETCH_SLEEP_RESPIRATORY,
    FETCH_SLEEP_SESSION,
    FETCH_SPO2,
    FETCH_SPO2_SLEEP,
    FETCH_STRESS_AUTO,
    FETCH_STRESS_MANUAL,
    FETCH_TEMPERATURE,
    FETCH_WORKOUT,
    AuthError,
    Fetched,
    LinkError,
    Strap,
    TransferStalled,
)

log = logging.getLogger(__name__)

# How each history type is decoded and how far it advances the fetch cursor.
# Minute series carry no timestamps: record n is n minutes after the batch start.
MINUTE_SERIES: dict[int, tuple[int, Callable]] = {
    FETCH_ACTIVITY: (8, p.activity),
    FETCH_ACTIVITY_EXTRA: (12, p.activity_extra),
    FETCH_STRESS_AUTO: (1, p.stress_auto),
    FETCH_TEMPERATURE: (8, p.temperature),
}
# Timestamped records: decoder, record size, header bytes, timestamp offset.
RECORDS: dict[int, tuple[Callable[[bytes], list], int, int, int]] = {
    FETCH_HRV: (p.hrv, 6, 0, 0),
    FETCH_SPO2: (p.spo2, 65, 1, 0),
    FETCH_SPO2_SLEEP: (p.spo2_sleep, 30, 1, 0),
    FETCH_SLEEP_RESPIRATORY: (p.respiration, 8, 0, 0),
    FETCH_RESTING_HR: (lambda d: p.daily_heart_rate(p.RESTING_HR, d), 6, 0, 0),
    FETCH_MAX_HR: (lambda d: p.daily_heart_rate(p.MAX_HR, d), 6, 0, 0),
    FETCH_MANUAL_HR: (lambda d: p.daily_heart_rate(p.MANUAL_HR, d), 6, 0, 0),
    FETCH_STRESS_MANUAL: (p.stress_manual, 5, 0, 0),
}
SYNC_ORDER = [
    FETCH_ACTIVITY,
    FETCH_ACTIVITY_EXTRA,
    FETCH_STRESS_AUTO,
    FETCH_TEMPERATURE,
    FETCH_HRV,
    FETCH_SPO2,
    FETCH_SPO2_SLEEP,
    FETCH_SLEEP_RESPIRATORY,
    FETCH_RESTING_HR,
    FETCH_MAX_HR,
    FETCH_MANUAL_HR,
    FETCH_STRESS_MANUAL,
    FETCH_PAI,
    FETCH_SLEEP_SESSION,
    FETCH_WORKOUT,
    # The largest transfer (about 440 KB a day) goes last.
    FETCH_HEART_RATE_SECONDS,
]

HEART_RATE_STALE_SECONDS = 30
# The snapshot is rebuilt after every sync. Between syncs (the strap is away,
# say) it is rebuilt once it is this old, and when the date changes, so "today"
# and the last 24 hours keep meaning that.
SNAPSHOT_MAX_AGE = 300
# Heart-rate events wake every bar. Small wobbles wait; a steady value is
# repeated now and then so readers know it is still live.
HEART_RATE_JUMP = 2
HEART_RATE_MIN_INTERVAL = 5
HEART_RATE_REPEAT_SECONDS = 15
SCAN_SECONDS = 20
# Live heart rate arrives every second while the strap is worn. When it has
# been quiet this long, check that the link still works rather than waiting
# for the next sync to find out.
QUIET_PROBE_SECONDS = 120
# The strap can keep answering the battery request while its history service
# has stopped: every fetch then times out, sync after sync, until the next
# connection. This many unanswered fetches in a row end the connection.
FETCH_TIMEOUTS_BEFORE_RECONNECT = 2
# It can also keep answering fetches with "nothing new" while it goes on
# recording (seen for an hour on firmware 3.27; a new connection got the lot).
# The strap files a row of activity every minute, worn or not, so an activity
# history this far behind after a sync that found nothing is that state.
HISTORY_STALL_SECONDS = 30 * 60
# A reconnect for it is tried this often at most, in case the strap really
# has nothing (it was switched off, say).
HISTORY_STALL_RETRY_SECONDS = 60 * 60
# At this strength (dBm) or below, a link drops on a turn of the wrist. The
# strap is heard this faintly a room away, or close by when the computer's
# Bluetooth aerial is a small dongle behind the case.
WEAK_SIGNAL = -85
# How old a signal reading can be and still describe the link.
SIGNAL_FRESH_SECONDS = 120
# The longest wait before trying again after a connection that dropped at once.
QUICK_DROP_WAIT = 60


def _finite(value: object) -> object:
    """The value with any infinity or NaN inside it replaced by None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(item) for item in value]
    return value


def encode(message: dict) -> str:
    """One line of standard JSON. Python would write infinity and NaN as bare
    words, which no JSON parser accepts: a reader would drop the whole
    message, every other reading with it."""
    try:
        return json.dumps(message, separators=(",", ":"), allow_nan=False)
    except ValueError:
        log.warning("a reading that is not a number was left out of a message")
        return json.dumps(_finite(message), separators=(",", ":"), allow_nan=False)


def _timezone() -> str:
    if os.environ.get("TZ"):
        return os.environ["TZ"].lstrip(":")
    with contextlib.suppress(OSError):
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    return ""


def bluetooth_unavailable(error: Exception) -> bool:
    """Whether a failed search says this computer has no working Bluetooth
    (no adapter, the adapter off, or no Bluetooth service) rather than that
    the strap wasn't found."""
    if type(error).__name__ == "BleakBluetoothNotAvailableError":
        return True
    return getattr(error, "dbus_error", None) in (
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.ServiceUnknown",
    )


def retry_delay(away_seconds: float, live: bool, sync_minutes: int) -> float:
    """How long to wait before scanning for the strap again. Soon after it
    leaves, check often so walking back in reconnects quickly; after a while
    (charging, another room), back off."""
    if not live:
        return sync_minutes * 60
    if away_seconds < 10 * 60:
        return 10
    if away_seconds < 60 * 60:
        return 60
    return 180


class Daemon:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(DATABASE)
        self.clients: set[asyncio.StreamWriter] = set()
        self.strap: Strap | None = None
        self.kick = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.version = __version__
        self.timezone = _timezone()

        self.status = "starting"
        self.message = ""
        self.syncing = False
        self.last_sync: int | None = self.store.meta("last_sync")  # type: ignore[assignment]
        self.device: dict = self.store.meta("device", {})  # type: ignore[assignment]
        self.battery: dict = self.store.meta("battery", {})  # type: ignore[assignment]
        self.heart_rate: int | None = None
        self.heart_rate_at = 0.0
        self._sent_heart_rate: tuple[int | None, float] = (None, 0.0)
        self.snapshot: dict = {}
        self._snapshot_json = ""
        self._stall_reconnect_at = float("-inf")
        self._address = ""
        self.radio = Radio()

    # ---- client socket -------------------------------------------------------

    async def serve(self) -> asyncio.AbstractServer:
        # The caller holds the instance lock, so a socket left here is stale.
        with contextlib.suppress(FileNotFoundError):
            CONTROL_SOCKET.unlink()
        old_umask = os.umask(0o177)
        try:
            return await asyncio.start_unix_server(self._client, path=str(CONTROL_SOCKET))
        finally:
            os.umask(old_umask)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        self._send(writer, self.state_message())
        try:
            while line := await reader.readline():
                with contextlib.suppress(ValueError, AttributeError, RecursionError):
                    self._command(json.loads(line), writer)
        except (ConnectionError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # disconnected, or sent a line too long to be a command
        finally:
            self.clients.discard(writer)
            writer.close()

    def _command(self, request: dict, writer: asyncio.StreamWriter) -> None:
        cmd = request.get("cmd")
        if cmd == "sync":
            self.kick.set()
        elif cmd == "state":
            self._send(writer, self.state_message())

    def _send(self, writer: asyncio.StreamWriter, line: str) -> None:
        # A reader that stops reading is dropped instead of buffering forever.
        if writer.is_closing() or writer.transport.get_write_buffer_size() > 1 << 20:
            writer.close()
            self.clients.discard(writer)
            return
        writer.write(line.encode() + b"\n")

    def broadcast(self, line: str) -> None:
        for writer in list(self.clients):
            self._send(writer, line)

    # ---- state ---------------------------------------------------------------

    def state_message(self) -> str:
        return encode(
            {
                "type": "state",
                "version": self.version,
                "timezone": self.timezone,
                "status": self.status,
                "message": self.message,
                "syncing": self.syncing,
                "live": self.settings.live,
                "lastSync": self.last_sync,
                "syncMinutes": self.settings.sync_minutes,
                "device": self.device,
                "battery": self.battery,
                "signal": self._signal(),
                "heartRate": self._live_heart_rate(),
                **self.snapshot,
            }
        )

    def publish(self) -> None:
        message = self.state_message()
        if message != self._snapshot_json:
            self._snapshot_json = message
            self.broadcast(message)

    def set_status(self, status: str, message: str = "") -> None:
        if (status, message) != (self.status, self.message):
            log.info("status %s %s", status, message)
            self.status, self.message = status, message
            self.publish()

    def refresh_snapshot(self) -> None:
        self.snapshot = summary.build(self.store)
        self.publish()

    def follow_timezone(self) -> None:
        """Notice the computer's time zone changing (its owner has travelled).
        Python reads the zone once, at start, unless asked to look again;
        "today" and each day of the trends would keep the old one."""
        time.tzset()
        zone = _timezone()
        if zone != self.timezone:
            log.info("time zone is now %s", zone or "unset")
            self.timezone = zone
            if not self.syncing:
                self.refresh_snapshot()

    def refresh_stale_snapshot(self) -> None:
        """Rebuild a snapshot that has aged, or was built on another day.
        A sync does that itself when it ends."""
        built = self.snapshot.get("generatedAt", 0)
        now = time.time()
        if self.syncing or (
            now - built < SNAPSHOT_MAX_AGE
            and datetime.fromtimestamp(built).date() == datetime.fromtimestamp(now).date()
        ):
            return
        self.refresh_snapshot()

    def _signal(self) -> dict | None:
        """How strongly the strap was last heard, from whichever scan saw it:
        {rssi, at}. Scans run only while looking for the strap, so this can
        be as old as the connection; the panel says so."""
        heard = self.radio.signal(self._address) if self._address else None
        return {"rssi": heard[0], "at": int(heard[1])} if heard else None

    def _weak_signal(self) -> int | None:
        """The strap's signal strength, if it was heard recently and faintly."""
        heard = self._signal()
        if heard and heard["rssi"] <= WEAK_SIGNAL and time.time() - heard["at"] <= SIGNAL_FRESH_SECONDS:
            return heard["rssi"]
        return None

    def _live_heart_rate(self) -> dict | None:
        if self.heart_rate is None or time.time() - self.heart_rate_at > HEART_RATE_STALE_SECONDS:
            return None
        return {"bpm": self.heart_rate, "at": int(self.heart_rate_at)}

    # ---- strap events --------------------------------------------------------

    def on_heart_rate(self, bpm: int) -> None:
        now = time.time()
        self.heart_rate, self.heart_rate_at = bpm, now
        last_bpm, last_at = self._sent_heart_rate
        since = now - last_at
        if (
            last_bpm is None
            or abs(bpm - last_bpm) >= HEART_RATE_JUMP
            or (bpm != last_bpm and since >= HEART_RATE_MIN_INTERVAL)
            or since >= HEART_RATE_REPEAT_SECONDS
        ):
            self._sent_heart_rate = (bpm, now)
            self.broadcast(encode({"type": "hr", "bpm": bpm, "at": int(now)}))

    def on_battery(self, battery: dict) -> None:
        if 0 <= battery.get("level", -1) <= 100:
            self.battery = {**self.battery, **battery, "at": int(time.time())}
            self.store.set_meta("battery", self.battery)
            self.publish()

    def on_disconnect(self, strap: Strap) -> None:
        # BlueZ can report an old connection down long after its disconnect
        # was given up on; only the session in hand is ended by this.
        if strap is self.strap:
            self.disconnected.set()

    # ---- connection loop -----------------------------------------------------

    async def run(self) -> None:
        self.refresh_snapshot()
        offline_since: float | None = None
        quick_drops = 0
        while True:
            try:
                auth = load_auth()
            except ConfigError as error:
                self.set_status("needs-auth", str(error))
                await self._wait(120)
                continue
            if self.store.meta("address") != auth.address:
                # Another strap: its history doesn't continue the old cursors.
                self.store.reset_cursors()
                self.store.set_meta("address", auth.address)
            self._address = auth.address
            self.radio.track(auth.address)

            self.set_status("connecting")
            self.disconnected.clear()
            strap = Strap(
                auth.address,
                auth.key,
                on_heart_rate=self.on_heart_rate,
                on_battery=self.on_battery,
            )
            strap.on_disconnect = functools.partial(self.on_disconnect, strap)
            try:
                await strap.connect(timeout=SCAN_SECONDS, radio=self.radio)
            except AuthError as error:
                await strap.disconnect()
                self.set_status("auth-failed", str(error))
                await self._wait(600)
                continue
            except BluetoothStuck:
                # Not something waiting cures: end the task, and with it the
                # process, so systemd starts one with a new line to BlueZ.
                await strap.disconnect()
                raise
            except Exception as error:
                await strap.disconnect()
                log.debug("connect failed: %r", error)
                offline_since = offline_since or time.monotonic()
                if self.radio.waiting:
                    self.set_status("offline", "Bluetooth on this computer has stopped answering.")
                elif bluetooth_unavailable(error):
                    self.set_status("offline", "Bluetooth is off or missing on this computer.")
                else:
                    self.set_status("offline", "The strap is out of range or switched off.")
                away = time.monotonic() - offline_since
                await self._wait(retry_delay(away, self.settings.live, self.settings.sync_minutes))
                continue

            offline_since = None
            self.strap = strap
            began = time.monotonic()
            try:
                await self._session(strap)
            except Exception as error:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                log.warning("connection ended: %r", error)
            finally:
                self.strap = None
                self.heart_rate = None
                self.syncing = False
                await strap.disconnect()

            if not self.settings.live:
                self.set_status("idle")
                await self._wait(self.settings.sync_minutes * 60)
                continue
            # A link that keeps dropping right after connecting is retried
            # more and more slowly instead of every few seconds.
            lasted = time.monotonic() - began
            quick_drops = quick_drops + 1 if lasted < 60 else 0
            weak = self._weak_signal()
            heard = self._signal()
            log.info(
                "connection lost after %s; last heard at %s",
                f"{lasted / 60:.0f} min" if lasted >= 90 else f"{lasted:.0f} s",
                f"{heard['rssi']} dBm, {time.time() - heard['at']:.0f} s before" if heard else "no known strength",
            )
            if weak is None:
                self.set_status("offline", "Connection lost; reconnecting…")
            else:
                self.set_status("offline", f"Connection lost on a weak signal ({weak} dBm); reconnecting…")
            await self._wait(min(5 * 2**quick_drops, QUICK_DROP_WAIT))

    async def _session(self, strap: Strap) -> None:
        info = await strap.read_device_info()
        self.device = {"name": "Amazfit Helio Strap", "address": strap.address, **info}
        self.store.set_meta("device", self.device)
        heard = self._signal()
        if heard:
            log.info("connected; heard at %d dBm", heard["rssi"])
        self.set_status("connected")
        if self.settings.live:
            await strap.watch()

        while strap.connected:
            await self.sync(strap)
            if not self.settings.live:
                return
            self.kick.clear()
            next_sync = time.monotonic() + self.settings.sync_minutes * 60
            while (left := next_sync - time.monotonic()) > 0:
                waiter = asyncio.ensure_future(self.disconnected.wait())
                kicked = asyncio.ensure_future(self.kick.wait())
                try:
                    done, _ = await asyncio.wait(
                        {waiter, kicked},
                        timeout=min(left, QUIET_PROBE_SECONDS),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    waiter.cancel()
                    kicked.cancel()
                if waiter in done:
                    return
                if kicked in done:
                    break
                if time.time() - self.heart_rate_at >= QUIET_PROBE_SECONDS:
                    # Off the wrist, or a dead link: only the second needs action.
                    await strap.probe()

    async def _wait(self, seconds: float) -> None:
        self.kick.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.kick.wait(), seconds)

    # ---- history sync --------------------------------------------------------

    async def sync(self, strap: Strap) -> None:
        self.syncing = True
        self.publish()
        failed = []
        try:
            # Also the liveness check: a dead link ends the session here.
            await strap.probe()
            now = int(time.time())
            default_since = now - self.settings.backfill_days * 86400
            unanswered = 0
            for kind in SYNC_ORDER:
                if not strap.connected:
                    return
                try:
                    caught_up = await self._sync_kind(strap, kind, default_since, now)
                    unanswered = 0
                except LinkError:
                    raise
                except Exception as error:
                    # One type failing (a stalled transfer, an odd record)
                    # shouldn't hold back the others.
                    failed.append(kind)
                    log.warning("sync of history type %02x failed: %r", kind, error)
                    # No answer to the request, or to the transfer it started.
                    quiet = isinstance(error, TimeoutError | TransferStalled)
                    unanswered = unanswered + 1 if quiet else 0
                    if unanswered >= FETCH_TIMEOUTS_BEFORE_RECONNECT:
                        raise LinkError("the strap stopped answering history requests") from None
                    continue
                if kind == FETCH_ACTIVITY and caught_up and self._history_stalled(now):
                    raise LinkError("the strap reports no new history while it goes on recording")
            self.store.prune(self.settings.keep_days)
            if len(failed) < len(SYNC_ORDER):
                self.last_sync = int(time.time())
                self.store.set_meta("last_sync", self.last_sync)
            if failed:
                log.info("sync finished; %d type(s) will be retried next time", len(failed))
        finally:
            self.syncing = False
            self.refresh_snapshot()

    async def _sync_kind(self, strap: Strap, kind: int, default_since: int, now: int) -> bool:
        """Copy what the strap has of one history type since its cursor.
        True when the strap ended by saying it has nothing newer."""
        since = self.store.cursor(kind, default_since)
        if since > now + CLOCK_SLACK:
            # A batch stamped in the future took the cursor there (versions
            # before 0.7 let it) and nothing has been fetched since. Go back
            # for what the strap still holds; storing it again changes nothing.
            log.warning(
                "history type %02x was waiting for %s; fetching from %s instead",
                kind,
                datetime.fromtimestamp(since).strftime("%Y-%m-%d"),
                datetime.fromtimestamp(default_since).strftime("%Y-%m-%d"),
            )
            since = default_since
            self.store.set_cursor(kind, since)
        for _round in range(10):
            fetched = await strap.fetch(kind, datetime.fromtimestamp(since).astimezone())
            if fetched is None:
                return True
            end = self._store_batch(fetched)
            log.debug("kind %02x: %d bytes, cursor %d -> %d", kind, len(fetched.data), since, end)
            if end <= since:
                return False
            self.store.set_cursor(kind, end)
            since = end
            if end >= now - 60:
                return False
        return False

    def _history_stalled(self, now: int) -> bool:
        """Whether the activity history, which the strap just called
        complete, is too far behind to be. True at most once in a while: the
        caller reconnects on it."""
        cursor = self.store.cursor(FETCH_ACTIVITY, now)
        behind = now - cursor
        if behind <= max(HISTORY_STALL_SECONDS, 3 * self.settings.sync_minutes * 60):
            return False
        if time.monotonic() - self._stall_reconnect_at < HISTORY_STALL_RETRY_SECONDS:
            return False
        self._stall_reconnect_at = time.monotonic()
        log.warning("no new history since %s; reconnecting", datetime.fromtimestamp(cursor).strftime("%H:%M"))
        return True

    def _store_batch(self, fetched: Fetched) -> int:
        """Save a batch and return where the next fetch should start: just
        past its last record, whether or not that record held a valid reading.

        Times are checked before anything is kept. A reading stamped further
        ahead than CLOCK_SLACK is not stored, where it would pass for the
        latest one for good, and never moves the cursor, which would wait
        there for history that can't exist. A batch with no believable time
        returns 0: the cursor stays put and the type is tried again next sync."""
        now = int(time.time())
        horizon = now + CLOCK_SLACK
        start = int(fetched.start.timestamp())
        data = fetched.data
        kind = fetched.kind
        if kind == FETCH_HEART_RATE_SECONDS:
            return self._store_heart_rate_seconds(data, horizon)
        # A workout and a minute series take every time from the batch's start.
        if (kind == FETCH_WORKOUT or kind in MINUTE_SERIES) and start > horizon:
            log.warning("history type %02x batch is stamped %s; skipped", kind, fetched.start.isoformat())
            return 0
        if kind == FETCH_WORKOUT:
            # One workout a fetch; the next starts after this one ends.
            workout = p.workout(fetched.start, data)
            if workout is None:
                log.warning("unreadable workout summary from %s (%d bytes)", fetched.start.isoformat(), len(data))
                return start + 60
            if workout.end > horizon:
                # A summary is filed when its workout ends, so this one's
                # duration is wrong.
                log.warning("workout from %s claims %d seconds; skipped", fetched.start.isoformat(), workout.seconds)
                return start + 60
            try:
                self.store.add_workout(workout, data)
            except (OverflowError, ValueError, sqlite3.Error) as error:
                # Stepped over like one that can't be read: retried, it would
                # hold back every workout after it.
                log.warning("workout from %s could not be stored (%r); skipped", fetched.start.isoformat(), error)
                return start + 60
            return workout.end + 60
        if kind in MINUTE_SERIES:
            size, decode = MINUTE_SERIES[kind]
            self.store.add_samples([s for s in decode(fetched.start, data) if s[1] <= horizon])
            end = start + (len(data) // size) * 60
            if end > horizon:
                # More minutes than have passed since the start: the tail is
                # not history. Carry on from the present.
                log.warning("history type %02x batch from %s runs past now", kind, fetched.start.isoformat())
                return now
            return end
        if kind in RECORDS:
            decode, stride, header, offset = RECORDS[kind]
            self.store.add_samples([s for s in decode(data) if s[1] <= horizon])
            times = [t for t in p.record_times(data, stride, header, offset) if t <= horizon]
            # One second on, not a minute: a fetch starts on a whole minute,
            # so this asks for the last reading's minute again and a reading
            # filed later in that minute is not passed over. Storing the
            # repeat changes nothing.
            if times:
                return max(times) + 1
            # Nothing readable in it: step past, as before, so it isn't fetched for ever.
            return start + 60 if start <= horizon else 0
        elif kind == FETCH_PAI:
            self.store.add_pai([day for day in p.pai(data) if day.timestamp <= horizon])
            times = p.record_times(data, 102, 0, 1)
        elif kind == FETCH_SLEEP_SESSION:
            # A report is filed after the sleep it describes.
            self.store.add_sleep([s for s in p.sleep_sessions(data) if max(s.reported, s.end) <= horizon])
            times = p.record_times(data, p.SLEEP_RECORD)
        else:
            times = []
        return max((t for t in [start, *times] if t <= horizon), default=0) + 60

    def _store_heart_rate_seconds(self, data: bytes, horizon: int) -> int:
        """Save per-second heart rate as minute extremes and return the next
        fetch's start: just past the last record. Fetches start on a whole
        minute, so the next one repeats that minute, and its extremes merge
        with the stored ones rather than replace them: by then the strap may
        have dropped the minute's first seconds.

        These records are seconds old at most, so one stamped later than
        `horizon` is a bad timestamp. It is neither stored nor allowed to
        move the cursor past seconds still to come."""
        lows, highs = p.heart_rate_seconds(data)
        self.store.merge_samples([s for s in lows if s[1] <= horizon], "MIN")
        self.store.merge_samples([s for s in highs if s[1] <= horizon], "MAX")
        times = [t for t in p.record_times(data, 5) if t <= horizon]
        return max(times, default=0) + 1


# Exit status for "another daemon holds the lock"; the unit doesn't restart on it.
EXIT_ALREADY_RUNNING = 75


def _lock_instance() -> int:
    # The lock sits beside the database it protects, outside the runtime
    # directory that systemd removes when a unit stops.
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        log.error("another Helios daemon is already running (systemctl --user status helios)")
        raise SystemExit(EXIT_ALREADY_RUNNING) from None
    return fd


async def run_daemon() -> None:
    # Health history: readable by this user only.
    os.umask(0o077)
    lock = _lock_instance()
    daemon: Daemon | None = None
    server: asyncio.AbstractServer | None = None
    workers: list[asyncio.Task] = []
    failed = False
    try:
        try:
            settings = load_settings()
        except ConfigError as error:
            log.warning("%s; using the defaults", error)
            settings = Settings()
        daemon = Daemon(settings)
        server = await daemon.serve()
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        workers = [
            asyncio.create_task(_ticker(daemon), name="ticker"),
            asyncio.create_task(daemon.run(), name="strap"),
        ]
        stopping = asyncio.create_task(stop.wait())
        # Neither worker is meant to end. One that has, on an error nothing
        # caught, would leave a service that answers its socket and collects
        # nothing: stop instead, with a failure systemd restarts.
        done, _ = await asyncio.wait({stopping, *workers}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        for worker in workers:
            if worker in done:
                failed = True
                error = None if worker.cancelled() else worker.exception()
                log.error("the %s task stopped (%r); shutting down", worker.get_name(), error, exc_info=error)
    finally:
        # Let the workers unwind (disconnect, finish their writes) before the store closes.
        for worker in workers:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        if daemon is not None and daemon.strap is not None:
            await daemon.strap.disconnect()
        if server is not None:
            server.close()
        if daemon is not None:
            for writer in list(daemon.clients):
                writer.close()
            with contextlib.suppress(FileNotFoundError):
                CONTROL_SOCKET.unlink()
            daemon.store.close()
        os.close(lock)
    if failed:
        raise SystemExit(1)


async def _ticker(daemon: Daemon) -> None:
    """The daemon's slow clock: what must change with time alone."""
    while True:
        await asyncio.sleep(HEART_RATE_STALE_SECONDS)
        # Live heart rate goes stale when the strap is taken off; tell readers.
        if daemon.heart_rate is not None and time.time() - daemon.heart_rate_at > HEART_RATE_STALE_SECONDS:
            daemon.heart_rate = None
            daemon.broadcast(encode({"type": "hr", "bpm": None, "at": int(time.time())}))
        daemon.follow_timezone()
        daemon.refresh_stale_snapshot()
