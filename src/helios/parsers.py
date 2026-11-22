# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""Decoders for the strap's stored history.

Formats follow Gadgetbridge's Huami fetch operations and were checked against
a Helio Strap on firmware 3.25.0.3 and 3.27.0.1. Each decoder returns plain records with
Unix timestamps (seconds); invalid sentinels are dropped here so storage and
display never see them.
"""

from __future__ import annotations

import math
import statistics
import struct
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from .protocol import iter_struct

# Per-minute and point metrics share one table, keyed by these ids.
HEART_RATE = 1
STEPS = 2
INTENSITY = 3
STRESS = 5
SPO2 = 6
TEMPERATURE = 7
RESPIRATION = 8
HRV = 9
RESTING_HR = 10
MAX_HR = 11
MANUAL_HR = 12
SPO2_SLEEP = 13
# Per-second extremes. The strap drops seconds after two or three days, so the
# highs are kept now for a peak display, though nothing shows them yet.
HEART_RATE_LOW = 14
HEART_RATE_HIGH = 15
# Active energy in kcal, per minute.
CALORIES = 16

Sample = tuple[int, int, float]  # metric, timestamp, value


def _minutes(start: datetime) -> Iterator[int]:
    ts = int(start.timestamp())
    while True:
        yield ts
        ts += 60


def activity(start: datetime, data: bytes) -> list[Sample]:
    """8 bytes per minute: kind, intensity, steps, heart rate, then four bytes
    of sleep-related flags that sleep sessions describe better."""
    out: list[Sample] = []
    for ts, (_kind, intensity, steps, hr, *_rest) in zip(_minutes(start), iter_struct("8B", data), strict=False):
        if 25 <= hr <= 230:
            out.append((HEART_RATE, ts, hr))
        if steps:
            out.append((STEPS, ts, steps))
        if intensity:
            out.append((INTENSITY, ts, intensity))
    return out


def activity_extra(start: datetime, data: bytes) -> list[Sample]:
    """12 bytes per minute (not in Gadgetbridge; see docs/unknown-history.md):
    byte 8 repeats the minute's steps and byte 9 is the active energy burned
    in kcal. The distance in the last two bytes is steps times a fixed
    stride, which is not the figure Zepp shows, so it is left alone."""
    rows = zip(_minutes(start), iter_struct("12B", data), strict=False)
    return [(CALORIES, ts, row[9]) for ts, row in rows if row[9]]


def stress_auto(start: datetime, data: bytes) -> list[Sample]:
    """One byte per minute, 0xff where nothing was measured. 0-39 relaxed,
    40-59 normal, 60-79 medium, 80-100 high."""
    return [(STRESS, ts, b) for ts, b in zip(_minutes(start), data, strict=False) if b <= 100]


def temperature(start: datetime, data: bytes) -> list[Sample]:
    """8 bytes per minute; the second int16 is skin temperature in 0.01 °C."""
    out: list[Sample] = []
    for ts, (_a, centi, _b, _c) in zip(_minutes(start), iter_struct("<hhhh", data), strict=False):
        celsius = centi / 100
        if 20.0 <= celsius <= 45.0:
            out.append((TEMPERATURE, ts, round(celsius, 2)))
    return out


def hrv(data: bytes) -> list[Sample]:
    """6 bytes: timestamp, unknown byte, HRV in ms."""
    return [(HRV, ts, value) for ts, _u, value in iter_struct("<IbB", data) if 0 < value < 255]


def spo2(data: bytes) -> list[Sample]:
    """Version byte 2, then 65-byte records: timestamp, SpO2 (high bit marks an
    automatic reading), 60 bytes of detail."""
    if not data or data[0] != 2:
        return []
    out: list[Sample] = []
    for offset in range(1, len(data) - 64, 65):
        ts, raw = struct.unpack_from("<IB", data, offset)
        value = raw & 0x7F
        if 70 <= value <= 100:
            out.append((SPO2, ts, value))
    return out


def spo2_sleep(data: bytes) -> list[Sample]:
    """Version byte 2, then 30-byte records: timestamp, SpO2, duration, ranges."""
    if not data or data[0] != 2:
        return []
    out: list[Sample] = []
    for offset in range(1, len(data) - 29, 30):
        ts, value = struct.unpack_from("<IB", data, offset)
        if 70 <= value <= 100:
            out.append((SPO2_SLEEP, ts, value))
    return out


def respiration(data: bytes) -> list[Sample]:
    """8 bytes during sleep: timestamp, UTC offset, breaths per minute, 2 unknown."""
    return [(RESPIRATION, ts, rate) for ts, _tz, rate, _a, _b in iter_struct("<IbBBB", data) if 4 <= rate <= 60]


def daily_heart_rate(metric: int, data: bytes) -> list[Sample]:
    """Resting, maximum and manual readings: timestamp, UTC offset, bpm."""
    return [(metric, ts, bpm) for ts, _tz, bpm in iter_struct("<IbB", data) if 25 <= bpm <= 230]


def heart_rate_seconds(data: bytes) -> tuple[list[Sample], list[Sample]]:
    """5 bytes a second: timestamp, bpm (not in Gadgetbridge; see
    docs/unknown-history.md). Returns each minute's lowest and highest rate,
    taken over medians of five consecutive readings so that a glitch of a
    second or two can't set them. The activity history already has a rate
    for every minute."""
    low: dict[int, float] = {}
    high: dict[int, float] = {}
    window: deque[tuple[int, int]] = deque(maxlen=5)
    for ts, bpm in iter_struct("<IB", data):
        if not 25 <= bpm <= 230:
            continue
        if window and not 0 <= ts - window[-1][0] <= 5:
            window.clear()  # off the wrist in between, or a stray timestamp
        window.append((ts, bpm))
        if len(window) == 5:
            rate = statistics.median(b for _, b in window)
            minute = window[2][0] - window[2][0] % 60
            low[minute] = min(rate, low[minute]) if minute in low else rate
            high[minute] = max(rate, high[minute]) if minute in high else rate
    return [(HEART_RATE_LOW, m, v) for m, v in low.items()], [(HEART_RATE_HIGH, m, v) for m, v in high.items()]


def stress_manual(data: bytes) -> list[Sample]:
    return [(STRESS, ts, value) for ts, value in iter_struct("<IB", data) if value <= 100]


@dataclass
class Pai:
    timestamp: int
    utc_offset: int  # seconds
    low: float | None
    moderate: float | None
    high: float | None
    minutes_low: int
    minutes_moderate: int
    minutes_high: int
    today: float
    total: float


# A day earns at most 75 PAI and the total covers seven days; anything far
# outside that is not a score.
PAI_LIMIT = 1000.0


def _score(value: float) -> float | None:
    """A PAI figure, or None when the bytes weren't one: the floats come
    straight off the strap, and infinity or NaN among them would be stored
    and then break every snapshot's JSON."""
    return value if math.isfinite(value) and 0.0 <= value <= PAI_LIMIT else None


def pai(data: bytes) -> list[Pai]:
    """102-byte daily records; type 0 rows predate a factory reset. A record
    whose day's score or total isn't a plausible number is dropped."""
    out: list[Pai] = []
    for offset in range(0, len(data) - 101, 102):
        kind, ts, tz = struct.unpack_from("<BIb", data, offset)
        if kind != 5:
            continue
        low, moderate, high = (_score(v) for v in struct.unpack_from("<fff", data, offset + 37))
        m_low, m_mod, m_high = (max(0, m) for m in struct.unpack_from("<hhh", data, offset + 49))
        today, total = (_score(v) for v in struct.unpack_from("<ff", data, offset + 55))
        if today is None or total is None:
            continue
        out.append(Pai(ts, tz * 900, low, moderate, high, m_low, m_mod, m_high, today, total))
    return out


STAGE_NAMES = {4: "light", 5: "deep", 7: "awake", 8: "rem"}
STAGE_GAP = 128  # in the nap table: the time between one sleep and the next
# A record has room for this many stages of the main sleep. The strap folds
# everything after the last slot into it, whatever the stages really were.
STAGE_CAP = 50
UNSTAGED = "unstaged"
# How far a capped record's stages may differ from its own totals (minutes)
# before its last stage counts as folded.
STAGE_TOLERANCE = 10

SLEEP_RECORD = 594
_STAGES_AT = 0x56
_NAP_STAGES_AT = _STAGES_AT + 5 * STAGE_CAP
_TOTALS_AT = 0x24A
_NAPS_AT = 0x18
_MAX_NAPS = 10


@dataclass
class SleepSession:
    reported: int
    start: int
    end: int
    score: int
    avg_hr: int
    minutes: dict[str, int]
    stages: list[tuple[int, int, str]] = field(default_factory=list)
    # In bed before falling asleep and after waking, when the strap says.
    bed_start: int | None = None
    bed_end: int | None = None
    nap: bool = False
    day: int | None = None  # the midnight the report belongs to


def _stage_minutes(stages: list[tuple[int, int, str]]) -> dict[str, int]:
    minutes = {"deep": 0, "light": 0, "rem": 0, "awake": 0}
    for start, end, kind in stages:
        if kind in minutes:
            minutes[kind] += (end - start) // 60
    return minutes


def _stages(record: bytes, at: int, count: int, base: int) -> list[tuple[int, int, str]]:
    stages = []
    for i in range(min(count, STAGE_CAP)):
        s, e, kind = struct.unpack_from("<HHB", record, at + 5 * i)
        if kind == STAGE_GAP:
            continue
        # A kind this table doesn't know still was sleep: light, as before.
        stages.append((base + s * 60, base + (e + 1) * 60, STAGE_NAMES.get(kind, "light")))
    return stages


def sleep_sessions(data: bytes) -> list[SleepSession]:
    """594-byte reports, one per day. Times are minutes from the midnight
    before the report's day, and every span includes both endpoint minutes.
    The strap sends revised reports as a night progresses, so later reports
    supersede earlier ones that overlap them.

    A report holds the main sleep (when the wearer went to bed and got up,
    fell asleep and woke, the score, up to 50 stages and each stage's total)
    and the day's naps, whose stages follow in a second table. The layout
    past the stages is not in Gadgetbridge; the bed times were checked against
    the time to fall asleep that Zepp shows, the naps against its nap list."""
    out: list[SleepSession] = []
    for offset in range(0, len(data) - SLEEP_RECORD + 1, SLEEP_RECORD):
        record = data[offset : offset + SLEEP_RECORD]
        reported, midnight = struct.unpack_from("<II", record, 0)
        base = midnight - 24 * 3600
        start, end, bed_start, bed_end = struct.unpack_from("<HHHH", record, 0x0A)
        count, nap_count = record[0x54], record[0x55]
        if count:
            stages = _stages(record, _STAGES_AT, count, base)
            rem, light, deep, wake = struct.unpack_from("<HHHH", record, _TOTALS_AT)
            minutes = {"deep": deep, "light": light, "rem": rem, "awake": wake}
            staged = _stage_minutes(stages)
            if (
                count >= STAGE_CAP
                and stages
                and sum(abs(staged[kind] - minutes[kind]) for kind in minutes) > STAGE_TOLERANCE
            ):
                stages[-1] = (stages[-1][0], stages[-1][1], UNSTAGED)
            in_bed = 0 < bed_start <= start and end <= bed_end < 0xFFFF
            out.append(
                SleepSession(
                    day=midnight,
                    reported=reported,
                    start=base + start * 60,
                    end=base + (end + 1) * 60,
                    score=record[0x16],
                    avg_hr=record[0x15],
                    minutes=minutes,
                    stages=stages,
                    bed_start=base + bed_start * 60 if in_bed else None,
                    bed_end=base + (bed_end + 1) * 60 if in_bed else None,
                )
            )
        nap_stages = _stages(record, _NAP_STAGES_AT, nap_count, base)
        for i in range(min(record[0x17], _MAX_NAPS)):
            nap_start, nap_end, _length = struct.unpack_from("<HHH", record, _NAPS_AT + 6 * i)
            if nap_end < nap_start or nap_end == 0xFFFF:
                continue
            first, last = base + nap_start * 60, base + (nap_end + 1) * 60
            stages = [stage for stage in nap_stages if first <= stage[0] and stage[1] <= last]
            minutes = _stage_minutes(stages)
            if not stages:
                # No stages for it: count the whole span as light sleep.
                minutes["light"] = (last - first) // 60
            out.append(
                SleepSession(
                    day=midnight,
                    reported=reported,
                    start=first,
                    end=last,
                    score=0,
                    avg_hr=0,
                    minutes=minutes,
                    stages=stages,
                    nap=True,
                )
            )
    return out


# ---- workouts ------------------------------------------------------------------


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def protobuf_fields(data: bytes) -> dict[int, list[int | bytes]]:
    """A protobuf message's fields by number: ints for varints, bytes for
    everything else (strings, nested messages, packed lists, fixed widths).
    Enough to read the strap's messages without their schema compiled in."""
    fields: dict[int, list[int | bytes]] = {}
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        value: int | bytes
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, pos = data[pos : pos + size], pos + size
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos : pos + size], pos + size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if pos > len(data):
            raise ValueError("protobuf field runs past the message")
        fields.setdefault(number, []).append(value)
    return fields


def _message(fields: dict[int, list[int | bytes]], number: int) -> dict[int, list[int | bytes]]:
    value = fields.get(number, [b""])[-1]
    return protobuf_fields(value) if isinstance(value, bytes) else {}


def _int(fields: dict[int, list[int | bytes]], number: int) -> int | None:
    """A field's value as a count, or None if it isn't one. Protobuf sends a
    negative int32 as a ten-byte number near 2^64, which no count is and no
    database column holds."""
    value = fields.get(number, [None])[-1]
    return value if isinstance(value, int) and 0 <= value < 1 << 31 else None


def _float(fields: dict[int, list[int | bytes]], number: int) -> float | None:
    value = fields.get(number, [None])[-1]
    return struct.unpack("<f", value)[0] if isinstance(value, bytes) and len(value) == 4 else None


def _packed(fields: dict[int, list[int | bytes]], number: int) -> list[int]:
    out: list[int] = []
    for value in fields.get(number, []):
        if isinstance(value, int):
            out.append(value)
            continue
        pos = 0
        while pos < len(value):
            item, pos = _varint(value, pos)
            out.append(item)
    return out


WORKOUT_VERSION = 0x8000


@dataclass
class Workout:
    start: int
    end: int
    kind: int  # Zepp OS activity type
    automatic: bool  # the strap noticed it; nobody started it
    seconds: int  # exercising, pauses excluded
    calories: int | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    min_hr: int | None = None
    distance: float | None = None  # metres
    aerobic: float | None = None  # training effect, 0 to 5
    anaerobic: float | None = None
    load: int | None = None
    zone_max: list[int] = field(default_factory=list)  # bpm, top of each zone
    zone_seconds: list[int] = field(default_factory=list)


def workout(start: datetime, data: bytes) -> Workout | None:
    """One workout summary: a version word, then a protobuf message
    (Gadgetbridge's WorkoutSummary in huami.proto). The fetch's start time is
    the workout's. None if the message can't be read."""
    if len(data) < 2:
        return None
    try:
        summary = protobuf_fields(data[2:])
        kind = _message(summary, 3)
        time = _message(summary, 7)
        heart = _message(summary, 19)
        effect = _message(summary, 21)
        zones = _message(summary, 22)
        distance = _float(_message(summary, 4), 1)
        calories = _int(_message(summary, 16), 1)
        zone_max, zone_seconds = _packed(zones, 2), _packed(zones, 3)
    except (ValueError, IndexError, struct.error):
        return None
    began = int(start.timestamp())
    total = _int(time, 1) or 0
    if total <= 0:
        return None

    def bpm(number: int) -> int | None:
        value = _int(heart, number)
        return value if value is not None and 25 <= value <= 230 else None

    def effect_value(number: int) -> float | None:
        value = _float(effect, number)
        return round(value, 1) if value is not None and 0 <= value <= 5 else None

    return Workout(
        start=began,
        end=began + total,
        kind=_int(kind, 1) or 0,
        automatic=_int(kind, 2) == 1,
        seconds=_int(time, 2) or total,
        calories=calories,
        avg_hr=bpm(1),
        max_hr=bpm(2),
        min_hr=bpm(3),
        distance=round(distance, 1) if distance and math.isfinite(distance) and 0 < distance < 1e7 else None,
        aerobic=effect_value(4),
        anaerobic=effect_value(5),
        load=_int(effect, 6),
        zone_max=zone_max,
        zone_seconds=zone_seconds,
    )


def record_times(data: bytes, stride: int, header: int = 0, offset: int = 0) -> list[int]:
    """Timestamps of every record in a batch, valid or not, so the fetch
    cursor moves past readings the decoders drop."""
    return [struct.unpack_from("<I", data, i + offset)[0] for i in range(header, len(data) - stride + 1, stride)]
