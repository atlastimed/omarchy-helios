import struct
from datetime import UTC, datetime

import pytest

from helios import parsers as p

START = datetime(2026, 10, 30, 7, 39, tzinfo=UTC)
T0 = int(START.timestamp())


def test_activity_skips_missing_heart_rate_and_idle_minutes():
    data = bytes([0x76, 0, 0, 0xFF, 5, 0, 0, 0]) + bytes([0x50, 7, 12, 91, 5, 0, 0, 0])
    assert p.activity(START, data) == [
        (p.HEART_RATE, T0 + 60, 91),
        (p.STEPS, T0 + 60, 12),
        (p.INTENSITY, T0 + 60, 7),
    ]


def test_stress_skips_unmeasured_minutes():
    assert p.stress_auto(START, bytes([0xFF, 52, 0xFF, 64])) == [(p.STRESS, T0 + 60, 52), (p.STRESS, T0 + 180, 64)]


def test_temperature_is_centi_celsius_per_minute():
    data = struct.pack("<hhhh", 32767, 3243, 23130, 23130) + struct.pack("<hhhh", 32767, 0, 23130, 23130)
    assert p.temperature(START, data) == [(p.TEMPERATURE, T0, 32.43)]


def test_hrv_records():
    data = struct.pack("<IbB", T0, 0, 45) + struct.pack("<IbB", T0 + 60, 0, 0)
    assert p.hrv(data) == [(p.HRV, T0, 45)]


def test_spo2_masks_the_automatic_flag():
    record = struct.pack("<IB", T0, 0x80 | 97) + bytes(60)
    assert p.spo2(b"\x02" + record) == [(p.SPO2, T0, 97)]
    assert p.spo2(b"\x01" + record) == []


def test_sleep_spo2():
    record = struct.pack("<IBB", T0, 99, 64) + bytes(24)
    assert p.spo2_sleep(b"\x02" + record) == [(p.SPO2_SLEEP, T0, 99)]


def test_respiration_and_resting_hr():
    assert p.respiration(struct.pack("<IbBBB", T0, 0, 13, 0, 1)) == [(p.RESPIRATION, T0, 13)]
    assert p.daily_heart_rate(p.RESTING_HR, struct.pack("<IbB", T0, 0, 74)) == [(p.RESTING_HR, T0, 74)]


def test_heart_rate_seconds_ignore_a_one_second_glitch():
    # 70 bpm over seconds 50-69, with a one-second drop to 40 and five seconds
    # at 90; 0 is a gap.
    rates = [70] * 20
    rates[5] = 40
    rates[10:15] = [90] * 5
    rates[17] = 0
    data = b"".join(struct.pack("<IB", T0 + 50 + i, bpm) for i, bpm in enumerate(rates))
    lows, highs = p.heart_rate_seconds(data)
    assert lows == [(p.HEART_RATE_LOW, T0, 70), (p.HEART_RATE_LOW, T0 + 60, 70)]
    assert highs == [(p.HEART_RATE_HIGH, T0, 70), (p.HEART_RATE_HIGH, T0 + 60, 90)]


def test_pai_ignores_pre_reset_rows():
    def row(kind: int, total: float) -> bytes:
        body = bytearray(102)
        struct.pack_into("<BIb", body, 0, kind, T0, 4)
        struct.pack_into("<fff", body, 37, 0.1, 0.2, 0.0)
        struct.pack_into("<hhh", body, 49, 11, 3, 0)
        struct.pack_into("<ff", body, 55, 0.3, total)
        return bytes(body)

    (only,) = p.pai(row(0, 9.0) + row(5, 12.5))
    assert only.total == 12.5
    assert only.utc_offset == 3600
    assert (only.minutes_low, only.minutes_moderate) == (11, 3)


def sleep_record(reported: int, midnight: int, start: int, end: int, stages: list[tuple[int, int, int]]) -> bytes:
    body = bytearray(594)
    struct.pack_into("<II", body, 0, reported, midnight)
    struct.pack_into("<HH", body, 0x0A, start, end)
    body[0x15], body[0x16] = 71, 80
    body[0x54] = len(stages)
    for i, stage in enumerate(stages):
        struct.pack_into("<HHB", body, 0x56 + 5 * i, *stage)
    struct.pack_into("<HHHH", body, 0x24A, 30, 300, 60, 10)
    return bytes(body)


def test_sleep_session_times_count_from_the_previous_midnight():
    midnight = T0 - T0 % 86400 + 86400
    data = sleep_record(midnight + 8 * 3600, midnight, 1380, 1859, [(1380, 1409, 4), (1410, 1859, 5)])
    (session,) = p.sleep_sessions(data)
    base = midnight - 86400
    assert session.start == base + 1380 * 60
    assert session.end == base + 1860 * 60
    assert session.stages[1] == (base + 1410 * 60, base + 1860 * 60, "deep")
    assert session.minutes == {"deep": 60, "light": 300, "rem": 30, "awake": 10}
    assert session.score == 80


def test_sleep_placeholder_without_stages_is_dropped():
    assert p.sleep_sessions(sleep_record(T0, T0, 0, 0, [])) == []


def test_sleep_report_carries_bed_times_and_naps():
    midnight = T0 - T0 % 86400 + 86400
    base = midnight - 86400
    body = bytearray(sleep_record(midnight + 18 * 3600, midnight, 1509, 2019, [(1509, 1600, 4), (1601, 2019, 5)]))
    struct.pack_into("<HH", body, 0x0E, 1490, 2020)  # in bed 19 minutes before sleep, up a minute after
    body[0x17] = 1
    struct.pack_into("<HHH", body, 0x18, 2319, 2465, 147)
    # The nap's stages sit in the second table, after the gap since the night.
    body[0x55] = 3
    for i, stage in enumerate([(2020, 2318, 128), (2319, 2400, 4), (2401, 2465, 5)]):
        struct.pack_into("<HHB", body, 0x56 + 5 * (50 + i), *stage)

    night, nap = p.sleep_sessions(bytes(body))
    assert (night.bed_start, night.bed_end) == (base + 1490 * 60, base + 2021 * 60)
    assert not night.nap and night.day == midnight
    assert nap.nap and nap.score == 0
    assert (nap.start, nap.end) == (base + 2319 * 60, base + 2466 * 60)
    assert nap.minutes == {"deep": 65, "light": 82, "rem": 0, "awake": 0}
    assert [kind for _, _, kind in nap.stages] == ["light", "deep"]


def test_sleep_report_without_bed_times_leaves_them_unset():
    midnight = T0 - T0 % 86400 + 86400
    (night,) = p.sleep_sessions(sleep_record(midnight, midnight, 1380, 1859, [(1380, 1859, 4)]))
    assert night.bed_start is None and night.bed_end is None


def test_a_full_stage_table_marks_its_folded_tail():
    midnight = T0 - T0 % 86400 + 86400
    # 49 ten-minute stages, then everything else folded into the last slot.
    stages = [(1000 + 10 * i, 1009 + 10 * i, 4 if i % 2 else 5) for i in range(49)] + [(1490, 1999, 4)]
    body = bytearray(sleep_record(midnight, midnight, 1000, 1999, stages))
    # The record's own totals say that tail wasn't all light sleep.
    struct.pack_into("<HHHH", body, 0x24A, 200, 400, 350, 50)
    (night,) = p.sleep_sessions(bytes(body))
    assert len(night.stages) == 50
    assert night.stages[-1][2] == p.UNSTAGED
    assert night.stages[-2][2] == "deep"
    assert night.minutes["rem"] == 200

    # When the totals agree with the stages, nothing was folded.
    struct.pack_into("<HHHH", body, 0x24A, 0, 750, 250, 0)
    (night,) = p.sleep_sessions(bytes(body))
    assert night.stages[-1][2] == "light"


def test_activity_extra_reads_active_energy():
    minute = bytes([1, 0, 0, 0, 0, 0, 0, 0])
    data = minute + bytes([0, 0, 0, 0]) + minute + bytes([152, 10, 0x2F, 0x2D])
    assert p.activity_extra(START, data) == [(p.CALORIES, T0 + 60, 10)]


def _field(number: int, wire: int, payload: bytes) -> bytes:
    return (
        bytes([number << 3 | wire]) + payload
        if number < 16
        else bytes([(number << 3 | wire) & 0x7F | 0x80, number >> 4]) + payload
    )


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        out.append(value & 0x7F | (0x80 if value > 0x7F else 0))
        value >>= 7
        if not value:
            return bytes(out)


def _message(number: int, body: bytes) -> bytes:
    return _field(number, 2, _varint(len(body)) + body)


def _packed(*values: int) -> bytes:
    body = b"".join(_varint(value) for value in values)
    return _varint(len(body)) + body


def workout_summary() -> bytes:
    # Invented figures, in the shape the strap files a workout.
    kind = _field(1, 0, _varint(223)) + _field(2, 0, _varint(1))
    time = _field(1, 0, _varint(2460)) + _field(2, 0, _varint(2400))
    heart = _field(1, 0, _varint(127)) + _field(2, 0, _varint(149)) + _field(3, 0, _varint(94))
    effect = _field(4, 5, struct.pack("<f", 2.4)) + _field(5, 5, struct.pack("<f", 0.3)) + _field(6, 0, _varint(45))
    zones = (
        _field(1, 0, _varint(1)) + _field(2, 2, _packed(100, 120, 140, 160)) + _field(3, 2, _packed(0, 240, 1680, 480))
    )
    body = (
        _message(1, b"2.1")
        + _message(3, kind)
        + _message(7, time)
        + _message(16, _field(1, 0, _varint(298)))
        + _message(19, heart)
        + _message(21, effect)
        + _message(22, zones)
    )
    return struct.pack("<H", p.WORKOUT_VERSION) + body


def test_workout_summary():
    w = p.workout(START, workout_summary())
    assert w is not None
    assert (w.start, w.end, w.seconds) == (T0, T0 + 2460, 2400)
    assert (w.kind, w.automatic) == (223, True)
    assert (w.avg_hr, w.max_hr, w.min_hr, w.calories) == (127, 149, 94, 298)
    assert (w.aerobic, w.anaerobic, w.load) == (2.4, 0.3, 45)
    assert w.zone_max == [100, 120, 140, 160]
    assert w.zone_seconds == [0, 240, 1680, 480]
    assert w.distance is None


def test_unreadable_workout_is_none():
    assert p.workout(START, b"") is None
    assert p.workout(START, struct.pack("<H", p.WORKOUT_VERSION) + b"\x0a\xff") is None
    # A summary with no duration isn't a workout.
    assert p.workout(START, struct.pack("<H", p.WORKOUT_VERSION) + _message(1, b"2.1")) is None


def test_a_workout_with_a_broken_zone_list_is_none():
    zones = _field(2, 2, _varint(2) + bytes([99, 0x8A]))  # ends inside a varint
    time = _field(1, 0, _varint(600))
    body = _message(7, time) + _message(22, zones)
    assert p.workout(START, struct.pack("<H", p.WORKOUT_VERSION) + body) is None


def test_a_stage_kind_the_table_lacks_still_counts_as_sleep():
    midnight = T0 - T0 % 86400 + 86400
    (night,) = p.sleep_sessions(sleep_record(midnight, midnight, 1380, 1859, [(1380, 1409, 4), (1410, 1859, 9)]))
    assert [kind for _, _, kind in night.stages] == ["light", "light"]


def pai_record(ts: int, today: float, total: float, parts=(1.0, 2.0, 3.0), minutes=(10, 5, 2)) -> bytes:
    record = bytearray(102)
    struct.pack_into("<BIb", record, 0, 5, ts, 4)
    struct.pack_into("<fff", record, 37, *parts)
    struct.pack_into("<hhh", record, 49, *minutes)
    struct.pack_into("<ff", record, 55, today, total)
    return bytes(record)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), -1.0, 5000.0])
def test_pai_that_is_not_a_plausible_number_is_dropped(bad):
    good = pai_record(1000, 4.5, 31.0)
    # Either figure being off loses the record; the good one beside it stays.
    assert [d.total for d in p.pai(pai_record(900, 3.0, bad) + good)] == [31.0]
    assert [d.total for d in p.pai(pai_record(900, bad, 30.0) + good)] == [31.0]


def test_pai_parts_that_are_not_numbers_are_left_out_of_a_good_record():
    (day,) = p.pai(pai_record(1000, 4.5, 31.0, parts=(float("nan"), 2.0, float("inf")), minutes=(-3, 5, 2)))
    assert (day.low, day.moderate, day.high) == (None, 2.0, None)
    assert (day.minutes_low, day.minutes_moderate) == (0, 5)
    assert (day.today, day.total, day.utc_offset) == (4.5, 31.0, 3600)


def test_a_workout_distance_that_is_not_a_number_is_left_out():
    summary = struct.pack("<H", p.WORKOUT_VERSION) + _message(7, _field(1, 0, _varint(600)))
    far = summary + _message(4, _field(1, 5, struct.pack("<f", float("inf"))))
    assert p.workout(datetime(2026, 11, 1, tzinfo=UTC), far).distance is None
    near = summary + _message(4, _field(1, 5, struct.pack("<f", 5012.34)))
    assert p.workout(datetime(2026, 11, 1, tzinfo=UTC), near).distance == 5012.3


def test_workout_counts_outside_what_a_count_can_be_are_left_out():
    minus_one = _varint((1 << 64) - 1)  # how protobuf sends an int32 of -1
    summary = struct.pack("<H", p.WORKOUT_VERSION) + _message(7, _field(1, 0, _varint(600)))
    summary += _message(16, _field(1, 0, minus_one)) + _message(21, _field(6, 0, minus_one))
    summary += _message(3, _field(1, 0, minus_one))
    workout = p.workout(datetime(2026, 11, 1, tzinfo=UTC), summary)
    assert (workout.calories, workout.load, workout.kind, workout.seconds) == (None, None, 0, 600)
    # A duration that isn't one means there is no workout to file.
    assert (
        p.workout(datetime(2026, 11, 1, tzinfo=UTC), struct.pack("<H", 0x8000) + _message(7, _field(1, 0, minus_one)))
        is None
    )
