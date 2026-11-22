# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""Bluetooth client for the Amazfit Helio Strap (Zepp OS).

One instance owns one BLE connection. It authenticates with the auth key,
discovers the Zepp OS service endpoints, and exposes the requests Helios
needs. Everything here is read-only towards the strap's stored data: fetched
history is acknowledged with "keep on device" so the Zepp app still syncs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import crypto
from .protocol import Decoder, Encoder, Message, decode_time, encode_time
from .radio import Radio

log = logging.getLogger(__name__)

# bleak waits on BlueZ's D-Bus replies without a time limit. One that never
# comes (seen when a firmware update rebooted the strap mid-sync) would freeze
# the daemon on a dead link, so every call to BlueZ is bounded by this.
BLUEZ_TIMEOUT = 15.0

# One history transfer may bring this much and take this long. The largest
# real one is the per-second heart rate, about 440 KB a day of which the strap
# keeps two or three days; even on a poor link that is a few megabytes over a
# few minutes. A transfer past either limit is not history, and since the
# batch can't be checked until it ends, the connection is given up instead.
MAX_TRANSFER_BYTES = 16 * 1024 * 1024
MAX_TRANSFER_SECONDS = 30 * 60


def huami_uuid(short: int) -> str:
    return f"{short:08x}-0000-3512-2118-0009af100700"


def sig_uuid(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


CHUNKED_WRITE = huami_uuid(0x0016)
CHUNKED_READ = huami_uuid(0x0017)
ACTIVITY_CONTROL = huami_uuid(0x0004)
ACTIVITY_DATA = huami_uuid(0x0005)
HEART_RATE_MEASUREMENT = sig_uuid(0x2A37)
BATTERY_LEVEL = sig_uuid(0x2A19)

DEVICE_INFO = {
    "serial": sig_uuid(0x2A25),
    "hardware": sig_uuid(0x2A27),
    "firmware": sig_uuid(0x2A28),
    "manufacturer": sig_uuid(0x2A29),
}

# Zepp OS service endpoints (Gadgetbridge ZeppOs*Service.ENDPOINT).
EP_SERVICES = 0x0000
EP_AUTH = 0x0082
EP_BATTERY = 0x0029
EP_HEART_RATE = 0x001D
EP_STEPS = 0x0016
EP_ACTIVITY_FETCH = 0x004B
EP_DEVICE_INFO = 0x0043

RESPONSE = 0x10
SUCCESS = 0x01

# Fetchable history (Gadgetbridge HuamiFetchDataType).
FETCH_ACTIVITY = 0x01
FETCH_MANUAL_HR = 0x02
FETCH_WORKOUT = 0x05
FETCH_PAI = 0x0D
FETCH_STRESS_MANUAL = 0x12
FETCH_STRESS_AUTO = 0x13
FETCH_SPO2 = 0x25
FETCH_SPO2_SLEEP = 0x26
FETCH_TEMPERATURE = 0x2E
FETCH_SLEEP_RESPIRATORY = 0x38
FETCH_RESTING_HR = 0x3A
FETCH_MAX_HR = 0x3D
FETCH_SLEEP_SESSION = 0x48
FETCH_HRV = 0x49
# Not in Gadgetbridge; identified on the Helio Strap (docs/unknown-history.md).
FETCH_HEART_RATE_SECONDS = 0x55
FETCH_ACTIVITY_EXTRA = 0x58

ACK_KEEP_ON_DEVICE = 0x09


class StrapError(Exception):
    pass


class AuthError(StrapError):
    pass


class LinkError(StrapError):
    """The connection stopped working: BlueZ or the strap no longer answers.
    Only a new connection helps."""


class TransferStalled(StrapError):
    """A history transfer began and then went quiet. Once is a hiccup; type
    after type, it is the strap's history service having stopped."""


async def bluez[T](call: Awaitable[T], what: str) -> T:
    """Await one BlueZ call, giving up after BLUEZ_TIMEOUT seconds."""
    try:
        async with asyncio.timeout(BLUEZ_TIMEOUT):
            return await call
    except TimeoutError:
        raise LinkError(f"Bluetooth stopped answering ({what})") from None


@dataclass
class Fetched:
    kind: int
    start: datetime
    data: bytes


@dataclass
class Strap:
    address: str
    auth_key: bytes
    on_heart_rate: Callable[[int], None] | None = None
    on_battery: Callable[[dict], None] | None = None
    on_disconnect: Callable[[], None] | None = None
    on_unsolicited: Callable[[Message], None] | None = None

    client: BleakClient | None = None
    services: dict[int, bool] = field(default_factory=dict)  # endpoint -> encrypted
    _encoder: Encoder = field(default_factory=Encoder)
    _decoder: Decoder = field(default_factory=Decoder)
    _waiters: list[tuple[int, Callable[[bytes], bool], asyncio.Future[bytes]]] = field(default_factory=list)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _activity_chunks: bytearray = field(default_factory=bytearray)
    _activity_counter: int = -1
    _activity_broken: bool = False
    _activity_at: float = 0.0
    _transfer_done: asyncio.Future[bytes] | None = None  # set while a transfer runs
    _tasks: set[asyncio.Future] = field(default_factory=set)

    # ---- connection ----------------------------------------------------------

    async def connect(self, timeout: float = 30.0, radio: Radio | None = None) -> None:
        """Find and connect to the strap. In the daemon, `radio` is the
        adapter's one scanner, which also notes the strap's signal strength."""
        device: BLEDevice | str | None
        if radio is not None:
            device = await radio.find_address(self.address, timeout)
        else:
            device = await BleakScanner.find_device_by_address(self.address, timeout=timeout)
        if device is None:
            raise StrapError(f"strap {self.address} is not advertising nearby")
        self.client = BleakClient(device, timeout=timeout, disconnected_callback=self._disconnected)
        async with radio or contextlib.nullcontext():
            # bleak gives up connecting after `timeout`, then waits without
            # limit for BlueZ to confirm it has let go. This is that limit.
            try:
                async with asyncio.timeout(timeout + BLUEZ_TIMEOUT):
                    await self.gatt.connect()
            except TimeoutError:
                self.client = None
                raise LinkError("Bluetooth stopped answering (connect)") from None
        try:
            await self._negotiate_mtu()
            await bluez(self.gatt.start_notify(CHUNKED_READ, self._on_chunked_read), "start_notify")
            await bluez(self.gatt.start_notify(CHUNKED_WRITE, self._on_chunked_write), "start_notify")
            # History arrives here. Subscribed once for the connection: doing it
            # around every fetch cost two writes over the air per history type.
            await bluez(self.gatt.start_notify(ACTIVITY_DATA, self._on_activity_data), "start_notify")
            await self.authenticate()
            await self._discover_services()
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await bluez(self.client.disconnect(), "disconnect")
        self.client = None

    @property
    def gatt(self) -> BleakClient:
        if self.client is None:
            raise StrapError("not connected")
        return self.client

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def _disconnected(self, _client: BleakClient) -> None:
        for _, _, future in self._waiters:
            if not future.done():
                future.set_exception(StrapError("strap disconnected"))
        self._waiters.clear()
        if self.on_disconnect:
            self.on_disconnect()

    async def _negotiate_mtu(self) -> None:
        backend = getattr(self.client, "_backend", None)
        acquire = getattr(backend, "_acquire_mtu", None)
        if acquire is not None:
            with contextlib.suppress(Exception):
                await bluez(acquire(), "acquire MTU")
        mtu = self.gatt.mtu_size
        # Gadgetbridge caps Zepp OS at 247; larger writes are not reliably accepted.
        self._encoder.mtu = min(mtu, 247)
        log.debug("MTU %d", self._encoder.mtu)

    # ---- chunked transport ---------------------------------------------------

    def _on_chunked_read(self, _char, data: bytearray) -> None:
        try:
            message, needs_ack = self._decoder.feed(bytes(data))
        except Exception as error:  # a frame the decoder couldn't take must not end the callback chain
            log.debug("dropped an unreadable chunk: %r", error)
            self._decoder.reset()
            return
        if needs_ack:
            task = asyncio.ensure_future(self._write_raw(CHUNKED_READ, self._decoder.ack()))
            self._tasks.add(task)
            task.add_done_callback(self._ack_done)
        if message is not None:
            self._dispatch(message)

    def _ack_done(self, task: asyncio.Future) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.debug("chunk ack failed: %r", task.exception())

    def _on_chunked_write(self, _char, data: bytearray) -> None:
        pass  # chunk acks from the strap; nothing to do with them

    def _dispatch(self, message: Message) -> None:
        log.debug("<- %04x %s", message.endpoint, message.payload.hex())
        for waiter in list(self._waiters):
            endpoint, matches, future = waiter
            if endpoint == message.endpoint and not future.done() and matches(message.payload):
                self._waiters.remove(waiter)
                future.set_result(message.payload)
                return
        if message.endpoint == EP_BATTERY and message.payload[:1] == b"\x04":
            self._handle_battery(message.payload[1:])
        elif self.on_unsolicited:
            self.on_unsolicited(message)

    async def _write_raw(self, uuid: str, data: bytes) -> None:
        if not self.connected:
            return
        async with self._write_lock:
            await bluez(self.gatt.write_gatt_char(uuid, data, response=False), "write")

    async def send(self, endpoint: int, payload: bytes, encrypt: bool | None = None) -> None:
        if encrypt is None:
            encrypt = self.services.get(endpoint, False)
        log.debug("-> %04x %s", endpoint, payload.hex())
        async with self._write_lock:
            for chunk in self._encoder.encode(endpoint, payload, encrypt):
                await bluez(self.gatt.write_gatt_char(CHUNKED_WRITE, chunk, response=False), "write")

    def _expect(self, endpoint: int, matches: Callable[[bytes], bool]) -> asyncio.Future[bytes]:
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        waiter = (endpoint, matches, future)
        self._waiters.append(waiter)
        future.add_done_callback(lambda _f: waiter in self._waiters and self._waiters.remove(waiter))
        return future

    async def request(
        self,
        endpoint: int,
        payload: bytes,
        matches: Callable[[bytes], bool],
        timeout: float = 10.0,
        encrypt: bool | None = None,
    ) -> bytes:
        future = self._expect(endpoint, matches)
        try:
            await self.send(endpoint, payload, encrypt)
            return await asyncio.wait_for(future, timeout)
        finally:
            future.cancel()

    # ---- handshake -----------------------------------------------------------

    async def authenticate(self) -> None:
        private = crypto.generate_private_key()
        hello = bytes([0x04, 0x02, 0x00, 0x02]) + crypto.public_key(private)
        reply = await self.request(EP_AUTH, hello, lambda p: p[:2] == bytes([RESPONSE, 0x04]), encrypt=False)
        if len(reply) < 67 or reply[2] != SUCCESS:
            raise AuthError(f"strap refused the key exchange ({reply[:3].hex()})")
        remote_random, remote_public = reply[3:19], reply[19:67]
        shared = crypto.shared_secret(private, remote_public)
        sequence = struct.unpack_from("<I", shared)[0]
        session_key = bytes(a ^ b for a, b in zip(shared[8:24], self.auth_key, strict=True))
        self._encoder.set_encryption(sequence, session_key)
        self._decoder.session_key = session_key

        proof = (
            bytes([0x05])
            + crypto.aes_ecb_encrypt(remote_random, self.auth_key)
            + crypto.aes_ecb_encrypt(remote_random, session_key)
        )
        reply = await self.request(EP_AUTH, proof, lambda p: p[:2] == bytes([RESPONSE, 0x05]), encrypt=False)
        if len(reply) < 3:
            raise AuthError("the strap's answer to the key was cut short")
        if reply[2] == 0x25:
            raise AuthError("the strap rejected the auth key; it changes when the strap is re-paired in Zepp")
        if reply[2] != SUCCESS:
            raise AuthError(f"authentication failed ({reply[:3].hex()})")
        log.info("authenticated")

    async def _discover_services(self) -> None:
        reply = await self.request(EP_SERVICES, b"\x03", lambda p: len(p) >= 3 and p[:1] == b"\x04", encrypt=False)
        (count,) = struct.unpack_from("<H", reply, 1)
        self.services = {}
        # No more entries than the reply has room for, whatever it claims.
        for i in range(min(count, (len(reply) - 3) // 3)):
            endpoint, encrypted = struct.unpack_from("<HB", reply, 3 + 3 * i)
            self.services[endpoint] = encrypted == 1
        log.debug("services %s", {f"{k:04x}": v for k, v in self.services.items()})

    # ---- plain GATT reads ----------------------------------------------------

    async def read_device_info(self) -> dict[str, str]:
        info = {}
        for key, uuid in DEVICE_INFO.items():
            with contextlib.suppress(Exception):
                value = await bluez(self.gatt.read_gatt_char(uuid), "read")
                info[key] = bytes(value).decode(errors="replace").strip("\x00")
        return info

    # ---- battery -------------------------------------------------------------

    async def request_battery(self) -> dict:
        reply = await self.request(EP_BATTERY, b"\x03", lambda p: p[:1] == b"\x04")
        return self._handle_battery(reply[1:])

    async def probe(self, attempts: int = 2) -> None:
        """Check that the strap still answers, using the battery request:
        small, and answered on every working link. A link BlueZ still calls
        connected can be dead (the strap rebooted for a firmware update, say);
        this raises LinkError then, so the caller connects again."""
        for attempt in range(attempts):
            try:
                await self.request_battery()
                return
            except (TimeoutError, StrapError) as error:
                log.debug("probe %d failed: %r", attempt + 1, error)
                if not self.connected:
                    break
        raise LinkError("the strap stopped answering")

    def _handle_battery(self, info: bytes) -> dict:
        # 0x0f, level, charging, last charge time (7 bytes + UTC offset), the
        # same again, then the level that charge reached.
        battery: dict = {"level": info[1] if len(info) > 1 else -1, "charging": len(info) > 2 and info[2] == 1}
        if len(info) >= 11:
            with contextlib.suppress(ValueError):
                battery["lastCharge"] = int(decode_time(info[3:11]).timestamp())
        if len(info) >= 20:
            battery["lastChargeLevel"] = info[19]
        if self.on_battery:
            self.on_battery(battery)
        return battery

    def _on_battery_level(self, _char, data: bytearray) -> None:
        if data and self.on_battery:
            self.on_battery({"level": data[0]})

    # ---- live readings -------------------------------------------------------

    async def watch(self) -> None:
        """Subscribe to the standard heart rate and battery characteristics.

        While a client listens, the strap pushes its continuous heart rate about
        once a second, without the sensor-intensive realtime mode Zepp uses for
        workouts."""
        await bluez(self.gatt.start_notify(HEART_RATE_MEASUREMENT, self._on_heart_rate), "start_notify")
        with contextlib.suppress(Exception):
            await bluez(self.gatt.start_notify(BATTERY_LEVEL, self._on_battery_level), "start_notify")

    def _on_heart_rate(self, _char, data: bytearray) -> None:
        # Flags, then the rate in one byte or (flag bit 0) two.
        wide = bool(data) and bool(data[0] & 0x01)
        if len(data) < (3 if wide else 2):
            return
        bpm = struct.unpack_from("<H", data, 1)[0] if wide else data[1]
        # The range stored history is held to: anything else is not a pulse.
        if 25 <= bpm <= 230 and self.on_heart_rate:
            self.on_heart_rate(bpm)

    # ---- stored history ------------------------------------------------------

    async def fetch(self, kind: int, since: datetime, idle_timeout: float = 20.0) -> Fetched | None:
        """Download one batch of stored history starting at `since`.

        Returns None when the strap has nothing newer. The strap may stop short
        of the present; callers repeat from the last returned timestamp. Every
        outcome, failures included, is acknowledged with "keep on device", so
        the Zepp app still receives the data.
        """
        control = self._activity_control
        keep = bytes([0x03, ACK_KEEP_ON_DEVICE])
        start = bytes([0x01, kind]) + encode_time(since)
        reply = await control(start, 0x01)
        if reply[2] != SUCCESS or len(reply) < 15:
            log.debug("fetch %02x refused: %s", kind, reply.hex())
            return None
        expected = struct.unpack_from("<I", reply, 3)[0]
        if expected == 0:
            await control(keep, 0x03)
            return None
        try:
            first = decode_time(reply[7:15])
        except ValueError:
            await control(keep, 0x03)
            raise StrapError(f"fetch {kind:02x} began at an impossible time ({reply[7:15].hex()})") from None

        self._activity_chunks = bytearray()
        self._activity_counter = -1
        self._activity_broken = False
        self._activity_at = time.monotonic()
        try:
            done = await self._transfer(idle_timeout)
        except BaseException:
            self._activity_chunks = bytearray()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(control(keep, 0x03), 5)
            raise
        data = bytes(self._activity_chunks)
        self._activity_chunks = bytearray()
        await control(keep, 0x03)

        if done[2] != SUCCESS or self._activity_broken:
            raise StrapError(f"fetch {kind:02x} was interrupted")
        if len(done) >= 7:
            crc = struct.unpack_from("<I", done, 3)[0]
            if crc != zlib.crc32(data):
                raise StrapError(f"fetch {kind:02x} failed its checksum")
        log.debug("fetch %02x: %d bytes from %s", kind, len(data), first.isoformat())
        return Fetched(kind, first, data)

    async def _transfer(self, idle_timeout: float) -> bytes:
        """Ask for the batch and wait for the strap's end-of-data reply, for as
        long as packets keep arriving, within the limits on one transfer."""
        done = self._expect(EP_ACTIVITY_FETCH, lambda p: len(p) >= 3 and p[0] == RESPONSE and p[1] == 0x02)
        self._transfer_done = done
        deadline = time.monotonic() + MAX_TRANSFER_SECONDS
        try:
            await self.send(EP_ACTIVITY_FETCH, b"\x02")
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LinkError("a history transfer went on too long to be one")
                try:
                    return await asyncio.wait_for(asyncio.shield(done), min(idle_timeout, left))
                except TimeoutError:
                    if time.monotonic() - self._activity_at >= idle_timeout:
                        raise TransferStalled("history transfer stalled") from None
        finally:
            self._transfer_done = None
            done.cancel()

    async def _activity_control(self, payload: bytes, command: int, timeout: float = 15.0) -> bytes:
        return await self.request(
            EP_ACTIVITY_FETCH,
            payload,
            lambda p: len(p) >= 3 and p[0] == RESPONSE and p[1] == command,
            timeout=timeout,
        )

    def _on_activity_data(self, _char, data: bytearray) -> None:
        waiting = self._transfer_done
        if not data or waiting is None:
            return  # nothing asked for this
        self._activity_at = time.monotonic()
        counter = data[0]
        if counter != (self._activity_counter + 1) & 0xFF:
            self._activity_broken = True
            return
        if len(self._activity_chunks) + len(data) - 1 > MAX_TRANSFER_BYTES:
            self._activity_broken = True
            self._activity_chunks = bytearray()
            if not waiting.done():
                waiting.set_exception(LinkError("a history transfer grew too large to be one"))
            return
        self._activity_counter = counter
        self._activity_chunks += data[1:]
