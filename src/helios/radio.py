# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""The computer's Bluetooth adapter, as the daemon uses it.

BlueZ keeps one discovery session and one filter per D-Bus client, and bleak
reaches BlueZ through a single client per process, so two BleakScanners in the
daemon would stop or re-filter each other. Every search goes through the one
scanner here instead; each search picks out its device itself, and each scan
records how strongly the strap was heard.

The scan sets no discovery filter. BlueZ 5.87 crashes on the first
advertisement that matches a UUID filter (it calls the UUID string as a
function; fixed upstream in 82af2beafc), and a Pattern filter hides every
other device from every other program while it is set.

Connections take turns: holding the radio (`async with radio:`) keeps a
second connect from starting while one is in progress. BlueZ refuses to start
a scan while a connection is being set up, so the scanner is left alone then.

BlueZ answers a request to start or stop scanning in milliseconds, or, when
it is in trouble, not at all, and bleak waits without limit. Each request
therefore gets CALL_TIMEOUT seconds. One still unanswered is left to finish
on its own rather than cancelled, because BlueZ may act on it yet: a scan
that starts late is then one this module knows about, and stops. No second
request is made meanwhile (BlueZ refuses one), so searches find nothing until
the answer comes; if it never does, BluetoothStuck tells the daemon to start
over with a new connection to BlueZ.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

log = logging.getLogger(__name__)

# Some controllers report each device once per scan session, so a scan that
# runs for hours could miss a device that went quiet and came back. Restarting
# the session this often bounds that delay; it costs a few D-Bus calls.
REFRESH = 30.0
RETRY = 2.0  # after BlueZ refused to start a scan
GRACE = 60.0  # how long past its timeout a search waits for a scan BlueZ keeps refusing
CALL_TIMEOUT = 15.0  # how long BlueZ gets to answer a request to start or stop scanning
STUCK_AFTER = 600.0  # how long a request may stay unanswered before the daemon starts over

# Refusals that pass once BlueZ finishes something else, such as a connect.
_BUSY = ("org.bluez.Error.InProgress", "org.bluez.Error.Busy")

Match = Callable[[BLEDevice, AdvertisementData], bool]


class BluetoothStuck(Exception):
    """BlueZ has left a request unanswered for so long that only a new
    connection to it (a new process) is worth trying."""


@dataclass
class _Search:
    match: Match
    found: asyncio.Future[BLEDevice]


class Radio:
    def __init__(self) -> None:
        self._connect = asyncio.Lock()
        self._changing = asyncio.Lock()
        self._searches: dict[object, _Search] = {}
        self._scanner: BleakScanner | None = None
        self._started = 0.0
        # A start or stop BlueZ hasn't answered yet, when it was made, and the
        # scanner it would start.
        self._unanswered: asyncio.Task | None = None
        self._unanswered_since = 0.0
        self._starting: BleakScanner | None = None
        self._tracked: set[str] = set()
        self._signals: dict[str, tuple[int, float]] = {}

    async def __aenter__(self) -> Radio:
        await self._connect.acquire()
        return self

    async def __aexit__(self, *_exc) -> None:
        self._connect.release()

    async def find(self, match: Match, timeout: float | None) -> BLEDevice | None:
        """The first advertising device `match` accepts within `timeout`
        seconds of scanning (None waits indefinitely), or None.

        Only time spent scanning counts: None means the device was really
        not seen, not that BlueZ refused to scan meanwhile."""
        token = object()
        search = _Search(match, asyncio.get_running_loop().create_future())
        self._searches[token] = search
        remaining = timeout
        give_up = None if timeout is None else time.monotonic() + timeout + GRACE
        cancelled = False
        try:
            while True:
                # Shielded: a scan BlueZ has started must be tracked even if
                # this search is cancelled meanwhile, or nothing would stop it.
                await asyncio.shield(self._update())
                scanning = self._scanner is not None
                wait = REFRESH if scanning else RETRY
                if remaining is not None and give_up is not None:
                    if remaining <= 0 or time.monotonic() >= give_up:
                        return None
                    # Never past the search's own end, scanning or not.
                    wait = min(wait, remaining if scanning else give_up - time.monotonic())
                began = time.monotonic()
                with contextlib.suppress(TimeoutError):
                    return await asyncio.wait_for(asyncio.shield(search.found), wait)
                if scanning and remaining is not None:
                    remaining -= time.monotonic() - began
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            del self._searches[token]
            tidy = asyncio.ensure_future(self._update())
            if cancelled:
                # Whoever cancelled is waiting (a shutdown, say): the scan is
                # stopped behind their back rather than at BlueZ's pace.
                tidy.add_done_callback(lambda t: t.cancelled() or t.exception())
            else:
                with contextlib.suppress(Exception):
                    await asyncio.shield(tidy)

    @property
    def waiting(self) -> bool:
        """Whether BlueZ still owes an answer, so nothing can be scanned for."""
        return self._unanswered is not None and not self._unanswered.done()

    async def find_address(self, address: str, timeout: float | None) -> BLEDevice | None:
        address = address.upper()
        return await self.find(lambda device, _adv: device.address.upper() == address, timeout)

    def track(self, address: str) -> None:
        """Remember how strongly this device is heard whenever a scan sees it."""
        self._tracked.add(address.upper())

    def signal(self, address: str) -> tuple[int, float] | None:
        """(RSSI in dBm, Unix time) of a tracked device's latest advertisement."""
        return self._signals.get(address.upper())

    def _seen(self, device: BLEDevice, adv: AdvertisementData) -> None:
        address = device.address.upper()
        rssi = getattr(adv, "rssi", None)
        if address in self._tracked and isinstance(rssi, int):
            self._signals[address] = (rssi, time.time())
        for search in list(self._searches.values()):
            if not search.found.done() and search.match(device, adv):
                search.found.set_result(device)

    async def _update(self) -> None:
        """Start, stop or refresh the scanner to suit the searches. Returns
        within two CALL_TIMEOUTs, whatever BlueZ does."""
        async with self._changing:
            if self._unanswered is not None:
                if not self._unanswered.done():
                    if time.monotonic() - self._unanswered_since >= STUCK_AFTER:
                        raise BluetoothStuck("Bluetooth has not answered for ten minutes")
                    return
                # It has finished after all. A scan it started is this
                # module's to refresh and stop, like any other.
                finished, started = self._unanswered, self._starting
                self._unanswered = self._starting = None
                if started is not None and not finished.cancelled() and finished.exception() is None:
                    self._scanner, self._started = started, time.monotonic()
            run = bool(self._searches)
            if self._scanner is not None and run:
                fresh = time.monotonic() - self._started < REFRESH
                if fresh or self._connect.locked():
                    return
            if self._scanner is not None:
                scanner, self._scanner = self._scanner, None
                with contextlib.suppress(Exception):
                    if not await self._answered(scanner.stop(), "stop"):
                        return
            if not run:
                return
            scanner = BleakScanner(detection_callback=self._seen, scanning_mode="active")
            try:
                if not await self._answered(scanner.start(), "start", scanner):
                    return
            except Exception as error:
                if not (self._connect.locked() or getattr(error, "dbus_error", None) in _BUSY):
                    raise
                log.debug("scan refused (%r); retrying", error)
                return
            self._scanner, self._started = scanner, time.monotonic()

    async def _answered(self, call, what: str, starting: BleakScanner | None = None) -> bool:
        """Make one request of BlueZ and wait up to CALL_TIMEOUT for it.
        False if it is still unanswered; it is then left running, and
        _update deals with its outcome (a scan by `starting`) when it ends."""
        task = asyncio.ensure_future(call)
        done, _ = await asyncio.wait({task}, timeout=CALL_TIMEOUT)
        if done:
            task.result()  # raises what the request raised
            return True
        log.warning("Bluetooth has not answered a request to %s scanning", what)
        self._unanswered, self._unanswered_since, self._starting = task, time.monotonic(), starting
        task.add_done_callback(self._late)
        return False

    def _late(self, _task: asyncio.Task) -> None:
        """An unanswered request finished. Whoever is searching will notice;
        if nobody is, a scan it started would otherwise run on unseen."""
        tidy = asyncio.ensure_future(self._update())
        tidy.add_done_callback(lambda t: t.cancelled() or t.exception())
