import json
import time
from datetime import UTC, datetime

from helios import parsers as p
from helios import summary
from helios.parsers import Pai, SleepSession
from helios.store import Store

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
T = int(NOW.timestamp())


def session(start: int, end: int, reported: int, score: int) -> SleepSession:
    return SleepSession(
        reported, start, end, score, 60, {"deep": 60, "light": 200, "rem": 90, "awake": 10}, [(start, end, "light")]
    )


def test_later_sleep_report_replaces_an_overlapping_one(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_sleep([session(T - 40000, T - 30000, T - 29000, 65)])
    store.add_sleep([session(T - 40000, T - 20000, T - 19000, 36)])
    (only,) = store.sleep_sessions(0)
    assert only["score"] == 36
    # A stale report arriving late does not undo the revision.
    store.add_sleep([session(T - 40000, T - 30000, T - 29000, 65)])
    assert [s["score"] for s in store.sleep_sessions(0)] == [36]


def test_pai_keeps_one_row_per_day(tmp_path):
    store = Store(tmp_path / "h.db")
    morning = Pai(T - 3600 * 4, 0, 0, 0, 0, 0, 0, 0, 0.1, 5.0)
    evening = Pai(T + 3600 * 6, 0, 0, 0, 0, 0, 0, 0, 0.4, 5.3)
    store.add_pai([morning, evening])
    assert [d["total"] for d in store.pai_days(0)] == [5.3]


def test_cursor_and_meta(tmp_path):
    store = Store(tmp_path / "h.db")
    assert store.cursor(1, 99) == 99
    store.set_cursor(1, 123)
    assert store.cursor(1, 99) == 123
    store.set_meta("battery", {"level": 79})
    assert store.meta("battery") == {"level": 79}


def test_snapshot_is_json_and_summarises(tmp_path):
    store = Store(tmp_path / "h.db")
    samples = [(p.HEART_RATE, T - 600 * i, 60 + i % 30) for i in range(144)]
    samples += [(p.STRESS, T - 60 * i, 30 + i % 50) for i in range(120)]
    samples += [(p.SPO2, T - 300, 97), (p.HRV, T - 900, 42), (p.TEMPERATURE, T - 60, 33.1)]
    samples += [(p.RESTING_HR, T - 3600, 58), (p.STEPS, T - 120, 40), (p.STEPS, T - 60, 2)]
    # Per-second lows: two during the sleep, one after it.
    samples += [(p.HEART_RATE_LOW, T - 30000, 52), (p.HEART_RATE_LOW, T - 20000, 49), (p.HEART_RATE_LOW, T - 600, 45)]
    store.add_samples(samples)
    store.add_sleep([session(T - 40000, T - 12000, T - 11000, 82)])

    snap = summary.build(store, NOW)
    json.dumps(snap)
    assert snap["latest"]["spo2"]["value"] == 97
    assert snap["today"]["restingHr"]["value"] == 58
    assert snap["today"]["steps"] == 42
    assert sum(snap["today"]["stressZones"]) > 0
    assert snap["sleep"]["score"] == 82
    assert snap["sleep"]["asleepMinutes"] == 350
    assert snap["sleep"]["hrMin"] == 49
    assert len(snap["day"]["heartRate"]) >= 140
    assert all(len(row) == 4 for row in snap["day"]["heartRate"])


def test_sleep_low_falls_back_to_minute_rates(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_sleep([session(T - 40000, T - 12000, T - 11000, 82)])
    assert summary.build(store, NOW)["sleep"]["hrMin"] is None
    # A night the per-second history no longer covers uses the minute rates.
    store.add_samples([(p.HEART_RATE, T - 30000, 58), (p.HEART_RATE, T - 20000, 55), (p.HEART_RATE, T - 600, 50)])
    assert summary.build(store, NOW)["sleep"]["hrMin"] == 55


def test_sleep_low_skips_awake_stages(tmp_path):
    store = Store(tmp_path / "h.db")
    start, end = T - 40000, T - 12000
    night = session(start, end, T - 11000, 82)
    night.stages = [(start, start + 3600, "awake"), (start + 3600, end, "deep")]
    store.add_sleep([night])
    store.add_samples(
        [(p.HEART_RATE_LOW, start + 600, 42), (p.HEART_RATE_LOW, start + 7200, 51), (p.HEART_RATE, start + 7200, 56)]
    )
    assert summary.build(store, NOW)["sleep"]["hrMin"] == 51


def test_failed_batch_rolls_back(tmp_path):
    store = Store(tmp_path / "h.db")
    try:
        with store.transaction():
            store.db.execute("INSERT INTO samples VALUES (1, 1, 1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert store.latest(1) is None


def test_pai_rows_arriving_newest_first_keep_one_per_day(tmp_path):
    store = Store(tmp_path / "h.db")
    evening = Pai(T + 3600 * 6, 0, 0, 0, 0, 0, 0, 0, 0.4, 5.3)
    morning = Pai(T - 3600 * 4, 0, 0, 0, 0, 0, 0, 0, 0.1, 5.0)
    store.add_pai([evening, morning])
    assert [d["total"] for d in store.pai_days(0)] == [5.3]


def test_main_sleep_prefers_the_night_over_a_later_nap():
    night = {"start": T - 40000, "end": T - 12000, "minutes": {"deep": 60, "light": 300, "rem": 90, "awake": 10}}
    nap = {"start": T - 9000, "end": T - 7200, "minutes": {"deep": 0, "light": 25, "rem": 0, "awake": 5}}
    assert summary.main_sleep([night, nap], T) is night


def test_stress_minutes_follow_the_sampling_gap():
    rows = [(T, 30), (T + 300, 50), (T + 600, 90)]
    # Each reading stands for five minutes; the last runs to now, capped at five.
    assert summary.stress_zones(rows, T + 660) == [5, 5, 0, 1]


def test_local_midnight_spans_a_dst_change(monkeypatch):
    import time
    from datetime import date

    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    try:
        # Clocks went forward on 29 March 2026: that day has 23 hours.
        length = summary.local_midnight(date(2026, 3, 30)) - summary.local_midnight(date(2026, 3, 29))
        assert length == 23 * 3600
    finally:
        monkeypatch.undo()
        time.tzset()


def test_main_sleep_is_the_latest_night_not_the_longest():
    older = {
        "start": T - 2 * 86400,
        "end": T - 2 * 86400 + 8 * 3600,
        "minutes": {"deep": 60, "light": 360, "rem": 60, "awake": 10},
    }
    latest = {
        "start": T - 86400,
        "end": T - 86400 + 6 * 3600,
        "minutes": {"deep": 50, "light": 250, "rem": 60, "awake": 5},
    }
    assert summary.main_sleep([older, latest], T) is latest


def nap(start: int, end: int, reported: int, day: int | None = None) -> SleepSession:
    return SleepSession(
        reported,
        start,
        end,
        0,
        0,
        {"deep": 0, "light": (end - start) // 60, "rem": 0, "awake": 0},
        [],
        nap=True,
        day=day,
    )


def test_a_days_later_report_drops_a_nap_the_strap_withdrew(tmp_path):
    store = Store(tmp_path / "h.db")
    day = T - T % 86400
    night = session(T - 40000, T - 12000, T - 11000, 82)
    night.day = day
    store.add_sleep([night, nap(T - 9000, T - 7200, T - 11000, day)])
    assert [s["nap"] for s in store.sleep_sessions(0)] == [False, True]
    # The revised report has the night alone.
    revised = session(T - 40000, T - 12000, T - 5000, 80)
    revised.day = day
    store.add_sleep([revised])
    assert [(s["nap"], s["score"]) for s in store.sleep_sessions(0)] == [(False, 80)]


def test_an_older_database_gains_the_new_sleep_columns(tmp_path):
    import sqlite3

    path = tmp_path / "h.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE sleep (start INTEGER PRIMARY KEY, end INTEGER NOT NULL, reported INTEGER NOT NULL, "
        "score INTEGER NOT NULL, avg_hr INTEGER NOT NULL, minutes TEXT NOT NULL, stages TEXT NOT NULL);"
        "CREATE TABLE cursor (kind INTEGER PRIMARY KEY, since INTEGER NOT NULL);"
        "INSERT INTO cursor VALUES (72, 1795000000), (1, 1795000000);"
        "INSERT INTO sleep VALUES (1794964140, 1794994800, 1795000000, 82, 71, "
        """'{"deep": 117, "light": 276, "rem": 106, "awake": 11}', '[]');"""
    )
    old.commit()
    old.close()

    store = Store(path)
    (kept,) = store.sleep_sessions(0)
    assert kept["score"] == 82 and kept["nap"] is False and kept["bedStart"] is None
    # Sleep is read again from well back; other history carries on.
    assert store.cursor(0x48, 0) < 1795000000 - 30 * 86400
    assert store.cursor(1, 0) == 1795000000
    # Opening it again changes nothing more.
    store.set_cursor(0x48, 1795000000)
    store.close()
    assert Store(path).cursor(0x48, 0) == 1795000000


def test_a_new_database_has_no_sleep_cursor_to_rewind(tmp_path):
    store = Store(tmp_path / "h.db")
    assert store.cursor(0x48, 7) == 7


def nights(store: Store, count: int, hrv: float, temperature: float) -> None:
    """`count` nights before the one test_snapshot uses, each with readings."""
    for n in range(1, count + 1):
        start, end = T - 40000 - n * 86400, T - 12000 - n * 86400
        store.add_sleep([session(start, end, end + 1000, 70)])
        store.add_samples(
            [
                (p.HRV, start + 600, hrv + n),
                (p.TEMPERATURE, start + 600, temperature),
                (p.RESPIRATION, start + 600, 14),
                (p.RESTING_HR, end - 60, 60 + n),
            ]
        )


def test_recovery_compares_the_night_with_the_ones_before(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_sleep([session(T - 40000, T - 12000, T - 11000, 82)])
    store.add_samples(
        [
            (p.HRV, T - 30000, 50),
            (p.HRV, T - 20000, 54),
            (p.TEMPERATURE, T - 30000, 34.9),
            (p.RESTING_HR, T - 11900, 58),
        ]
    )
    nights(store, 6, 60, 34.5)
    recovery = summary.build(store, NOW)["recovery"]
    assert recovery["nights"] == 6
    # Six nights are not yet a usual.
    assert recovery["hrv"] == {"value": 52.0, "baseline": None}

    nights(store, 8, 60, 34.5)
    recovery = summary.build(store, NOW)["recovery"]
    assert recovery["nights"] == 8
    assert recovery["hrv"] == {"value": 52.0, "baseline": 64.5}
    assert recovery["temperature"] == {"value": 34.9, "baseline": 34.5}
    # The resting rate filed as the night ended, against earlier nights'.
    assert recovery["restingHr"] == {"value": 58, "baseline": 64.5}
    assert recovery["respiration"] == {"value": None, "baseline": 14.0}


def test_snapshot_lists_naps_after_the_night_and_keeps_them_out_of_it(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_sleep([session(T - 40000, T - 12000, T - 11000, 82)])
    # A long "nap" must not displace the night, however long it ran.
    store.add_sleep([nap(T - 11000, T - 100, T - 50)])
    snap = summary.build(store, NOW)
    assert snap["sleep"]["score"] == 82
    assert snap["naps"] == [{"start": T - 11000, "end": T - 100, "asleepMinutes": 181}]
    assert snap["week"]["sleep"][-1][1] == 82


def test_snapshot_carries_workouts_calories_and_a_month(tmp_path):
    from helios.parsers import Workout

    store = Store(tmp_path / "h.db")
    assert summary.build(store, NOW)["today"]["calories"] is None
    store.add_samples([(p.CALORIES, T - 120, 4), (p.CALORIES, T - 60, 3), (p.CALORIES, T - 3 * 86400, 9)])
    store.add_samples([(p.STEPS, T - 10 * 86400, 900)])
    run = Workout(T - 86400, T - 86400 + 1800, 1, False, 1800, calories=300, avg_hr=150, max_hr=171, load=40)
    odd = Workout(T - 2 * 86400, T - 2 * 86400 + 600, 223, True, 600, load=None, zone_seconds=[0, 60, 540])
    store.add_workout(run, b"raw")
    store.add_workout(odd, b"raw")
    store.add_workout(Workout(T - 9 * 86400, T - 9 * 86400 + 600, 1, False, 600, load=99), b"raw")

    snap = summary.build(store, NOW)
    json.dumps(snap)
    assert snap["today"]["calories"] == 7
    assert snap["workouts"]["load"] == 40
    assert [(w["name"], w["kind"]) for w in snap["workouts"]["recent"]] == [("Outdoor running", 1), (None, 223)]
    assert snap["workouts"]["recent"][1]["zones"] == [0, 60, 540]
    assert snap["month"]["days"] == 30 and snap["week"]["days"] == 7
    assert [value for _, value in snap["month"]["steps"]] == [900]
    assert snap["week"]["steps"] == []


def test_a_nap_is_never_shown_as_the_night(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_sleep([nap(T - 9000, T - 3000, T - 2000)])
    snap = summary.build(store, NOW)
    assert snap["sleep"] is None and snap["recovery"] is None
    assert [n["asleepMinutes"] for n in snap["naps"]] == [100]
    # A short sleep the strap filed as a night still is one.
    short = session(T - 40000, T - 36000, T - 35000, 40)
    short.minutes = {"deep": 10, "light": 50, "rem": 0, "awake": 6}
    store.add_sleep([short])
    assert summary.build(store, NOW)["sleep"]["score"] == 40


def test_a_reading_from_the_future_is_never_the_latest(tmp_path):
    store = Store(tmp_path / "h.db")
    # As stored by a version that didn't check: an HRV reading stamped 2106.
    store.add_samples([(p.HRV, 0xFFFFFFF0, 199), (p.HRV, T - 60, 46), (p.HEART_RATE, T + 86400, 180)])
    store.add_samples([(p.HEART_RATE, T - 30, 71)])
    snap = summary.build(store, NOW)
    assert snap["latest"]["hrv"] == {"value": 46.0, "at": T - 60}
    assert snap["latest"]["heartRate"] == {"value": 71.0, "at": T - 30}
    # A small difference between the two clocks is not the future.
    store.add_samples([(p.HEART_RATE, T + 120, 75)])
    assert summary.build(store, NOW)["latest"]["heartRate"]["value"] == 75.0


def test_sleep_workouts_and_pai_from_the_future_stay_out_of_the_snapshot(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_sleep([session(T - 40000, T - 12000, T - 11000, 82), session(T + 90000, T + 110000, T + 111000, 99)])
    store.add_pai([Pai(T - 3600, 0, 0, 0, 0, 0, 0, 0, 2.0, 30.0), Pai(T + 5 * 86400, 0, 0, 0, 0, 0, 0, 0, 9.0, 99.0)])
    store.add_workout(p.Workout(T + 86400, T + 90000, 1, False, 3600, load=50), b"")
    snap = summary.build(store, NOW)
    assert snap["sleep"]["score"] == 82
    assert snap["today"]["pai"]["total"] == 30.0
    assert snap["workouts"]["recent"] == []


def test_a_stored_infinity_does_not_reach_the_snapshot(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_pai([Pai(T - 86400, 0, 0, 0, 0, 0, 0, 0, 3.0, 28.0)])
    # What 0.6 would have kept from a malformed record.
    store.db.execute("INSERT INTO pai VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0, ?)", (T - 3600, float("inf")))
    snap = summary.build(store, NOW)
    assert snap["today"]["pai"]["total"] == 28.0
    json.dumps(snap, allow_nan=False)


def test_pai_from_an_earlier_day_is_not_todays(tmp_path):
    store = Store(tmp_path / "h.db")
    store.add_pai([Pai(T - 2 * 86400, 0, 0, 0, 0, 0, 0, 0, 5.0, 40.0)])
    assert summary.build(store, NOW)["today"]["pai"] == {
        "ts": T - 2 * 86400,
        "offset": 0,
        "today": 5.0,
        "total": 40.0,
        "minutes": [0, 0, 0],
        "current": False,
    }
    store.add_pai([Pai(T - 3600, 0, 0, 0, 0, 0, 0, 0, 1.5, 38.0)])
    assert summary.build(store, NOW)["today"]["pai"]["current"] is True


def test_pai_days_follow_the_offset_the_record_was_filed_with(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Auckland")  # UTC+13 in November
    time.tzset()
    try:
        now = datetime(2026, 11, 1, 11, 30, tzinfo=UTC).astimezone()  # 00:30 on the 2nd in Auckland
        store = Store(tmp_path / "h.db")
        # Filed an hour earlier: 23:30 on the 1st there, yesterday.
        store.add_pai([Pai(int(now.timestamp()) - 3600, 13 * 3600, 0, 0, 0, 0, 0, 0, 5.0, 40.0)])
        assert summary.build(store, now)["today"]["pai"]["current"] is False
        store.add_pai([Pai(int(now.timestamp()) - 600, 13 * 3600, 0, 0, 0, 0, 0, 0, 0.0, 35.0)])
        assert summary.build(store, now)["today"]["pai"]["current"] is True
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_a_day_with_nothing_from_the_strap_has_unknown_totals(tmp_path):
    store = Store(tmp_path / "h.db")
    midnight = summary.local_midnight(NOW.astimezone().date())
    # Yesterday evening's steps and energy, and nothing since midnight.
    store.add_samples(
        [(p.STEPS, midnight - 600, 123), (p.CALORIES, midnight - 600, 4), (p.HEART_RATE, midnight - 600, 70)]
    )
    today = summary.build(store, NOW)["today"]
    assert (today["steps"], today["calories"]) == (None, None)
    # Worn but still: zero is then a count.
    store.add_samples([(p.HEART_RATE, midnight + 600, 58)])
    today = summary.build(store, NOW)["today"]
    assert (today["steps"], today["calories"]) == (0, 0)
    store.add_samples([(p.STEPS, midnight + 900, 40)])
    assert summary.build(store, NOW)["today"]["steps"] == 40
