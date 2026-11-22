# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""The health snapshot the bar widget renders, built from the store."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import parsers as p
from .store import CLOCK_SLACK, Store
from .workouts import workout_name

DAY = 86400
# The strap measures stress every five minutes; a reading stands for at most that long.
STRESS_SAMPLE_SECONDS = 300
MAIN_SLEEP_MINUTES = 180
MONTH_DAYS = 30
# A night is compared with the average of up to this many nights before it,
# once there are enough of them to call it usual. Zepp starts at seven too.
BASELINE_NIGHTS = 28
BASELINE_MIN_NIGHTS = 7
# The strap files the night's resting heart rate around waking.
RESTING_HR_SLACK = 600
WORKOUT_DAYS = 7
WORKOUTS_SHOWN = 6


def local_midnight(day: date) -> int:
    """Unix time of local midnight starting `day`, honouring DST."""
    return int(datetime(day.year, day.month, day.day).astimezone().timestamp())


def _reading(row: tuple[int, float] | None) -> dict | None:
    return {"value": row[1], "at": row[0]} if row else None


def _asleep(session: dict) -> int:
    return sum(v for k, v in session["minutes"].items() if k != "awake")


def night_readings(store: Store, session: dict) -> dict:
    """What the body did over one sleep: the averages Zepp's readiness is
    built from. Its "sleep HRV" and skin temperature are these same means
    (checked night by night against the app), and its "sleep RHR" is the
    resting heart rate the strap files as the sleep ends."""
    start, end = session["start"], session["end"]
    hrv = store.stats(p.HRV, start, end)
    temperature = store.stats(p.TEMPERATURE, start, end)
    respiration = store.stats(p.RESPIRATION, start, end)
    resting = store.latest(p.RESTING_HR, start, end + RESTING_HR_SLACK)
    return {
        "hrv": hrv["avg"] if hrv else None,
        "temperature": round(temperature["avg"], 2) if temperature else None,
        "respiration": respiration["avg"] if respiration else None,
        "restingHr": resting[1] if resting else None,
    }


def recovery(store: Store, current: dict, earlier: list[dict]) -> dict:
    """A night's readings (`current`, from night_readings) beside the
    wearer's usual: the mean of the nights before it. Each reading is
    {value, baseline}; a baseline is None until enough earlier nights have
    that reading."""
    usual = [night_readings(store, s) for s in earlier[-BASELINE_NIGHTS:]]
    out: dict = {"nights": len(usual)}
    for key, digits in (("hrv", 1), ("restingHr", 1), ("temperature", 2), ("respiration", 1)):
        values = [n[key] for n in usual if n[key] is not None]
        baseline = round(sum(values) / len(values), digits) if len(values) >= BASELINE_MIN_NIGHTS else None
        out[key] = {"value": current[key], "baseline": baseline}
    return out


def _sleep_extras(store: Store, session: dict, readings: dict) -> dict:
    start, end = session["start"], session["end"]
    # The five-minute readings resolve dips; the sleep record mostly reads 99 to 100.
    spo2 = store.stats(p.SPO2, start, end) or store.stats(p.SPO2_SLEEP, start, end)
    bed = session.get("bedStart")
    return {
        **session,
        "respiration": readings["respiration"],
        "spo2": spo2["avg"] if spo2 else None,
        "spo2Min": spo2["min"] if spo2 else None,
        "hrv": readings["hrv"],
        "hrMin": sleeping_low(store, session),
        "asleepMinutes": _asleep(session),
        "fellAsleepMinutes": (start - bed) // 60 if bed else None,
    }


def sleeping_low(store: Store, session: dict) -> float | None:
    """The lowest heart rate outside the session's awake stages. Per-second
    lows catch what minute rates smooth over; minutes the strap no longer
    had seconds for, when Helios fetched them, count by their minute rate."""
    asleep = [(s, e) for s, e, kind in session["stages"] if kind != "awake"]
    rows = store.db.execute(
        "SELECT ts, value FROM samples WHERE metric IN (?, ?) AND ts >= ? AND ts < ?",
        (p.HEART_RATE, p.HEART_RATE_LOW, session["start"], session["end"]),
    ).fetchall()
    return min((v for ts, v in rows if any(s <= ts < e for s, e in asleep)), default=None)


def main_sleeps(sessions: list[dict]) -> list[dict]:
    """The nights among the sessions: not what the strap filed as a nap, and
    three hours or more asleep."""
    return [s for s in sessions if not s.get("nap") and _asleep(s) >= MAIN_SLEEP_MINUTES]


def main_sleep(sessions: list[dict], now: int) -> dict | None:
    """The night to show: the most recent sleep of three hours or more, so
    neither a nap after it nor a longer night before it takes its place."""
    main = main_sleeps(sessions)
    if main:
        return max(main, key=lambda s: s["end"])
    # Nothing that long: the latest sleep the strap didn't file as a nap.
    short = [s for s in sessions if not s.get("nap")]
    return short[-1] if short else None


def naps_since(sessions: list[dict], night: dict | None, now: int) -> list[dict]:
    """Naps since the night shown, within the last day: when, and how long
    asleep. The strap's own nap list; a doze it never filed isn't here."""
    after = night["end"] if night else 0
    return [
        {"start": s["start"], "end": s["end"], "asleepMinutes": _asleep(s)}
        for s in sessions
        if s.get("nap") and s["start"] >= after and s["end"] > now - DAY
    ]


def stress_zones(rows: list[tuple[int, float]], now: int) -> list[int]:
    """Minutes in each band (relaxed, normal, medium, high), each reading
    counting until the next one, up to the sampling interval."""
    seconds = [0, 0, 0, 0]
    for i, (ts, value) in enumerate(rows):
        following = rows[i + 1][0] if i + 1 < len(rows) else now
        span = max(0, min(STRESS_SAMPLE_SECONDS, following - ts))
        seconds[0 if value < 40 else 1 if value < 60 else 2 if value < 80 else 3] += span
    return [round(s / 60) for s in seconds]


def _days(today: date, count: int) -> list[tuple[int, int]]:
    """(start, end) of the `count` local days ending today, oldest first."""
    return [
        (local_midnight(today - timedelta(days=n)), local_midnight(today - timedelta(days=n - 1)))
        for n in range(count - 1, -1, -1)
    ]


def _trends(store: Store, sessions: list[dict], days: list[tuple[int, int]]) -> dict:
    nights = main_sleeps(sessions)
    sleep = []
    for start, end in days:
        ending = [s for s in nights if start <= s["end"] < end] or [
            s for s in sessions if not s.get("nap") and start <= s["end"] < end
        ]
        if ending:
            longest = max(ending, key=_asleep)
            sleep.append([start, longest["score"], _asleep(longest)])
    return {
        "start": days[0][0],
        "days": len(days),
        "restingHr": store.daily(p.RESTING_HR, days, "MIN"),
        "hrv": store.daily(p.HRV, days),
        "sleep": sleep,
        "steps": store.daily(p.STEPS, days, "SUM"),
    }


def _workouts(store: Store, since: int, until: int) -> list[dict]:
    return [{**w, "name": workout_name(w["kind"])} for w in store.workouts(since, until)]


def _latest_pai(days: list[dict], now: datetime) -> dict | None:
    """The newest PAI record, marked with whether it is today's: its score
    for "today" is that record's day, which after a day away is not this one.
    Days are local calendar days, the record's by the offset it was filed with."""
    if not days:
        return None
    latest = days[-1]
    offset = now.utcoffset()
    today = (int(now.timestamp()) + int(offset.total_seconds() if offset else 0)) // DAY
    return {**latest, "current": (latest["ts"] + latest["offset"]) // DAY == today}


def build(store: Store, now: datetime | None = None) -> dict:
    now = (now or datetime.now()).astimezone()
    ts = int(now.timestamp())
    today = now.date()
    midnight = local_midnight(today)
    day_ago = ts - DAY
    # Reads stop here, so a time from the future (stored by a version that
    # didn't check, or while this computer's clock was behind) can't pass
    # for the latest reading.
    ahead = ts + CLOCK_SLACK

    # With nothing from the strap since midnight, today's totals are unknown,
    # not zero: it has been away, and may be counting steps all the same.
    seen_today = store.latest(p.HEART_RATE, midnight, ahead) is not None
    steps_today = store.stats(p.STEPS, midnight, ts + 60)
    calories_today = store.stats(p.CALORIES, midnight, ts + 60)
    stress_today = store.stats(p.STRESS, midnight, ts + 60)
    stress_rows = store.db.execute(
        "SELECT ts, value FROM samples WHERE metric = ? AND ts >= ? AND ts <= ? ORDER BY ts", (p.STRESS, midnight, ts)
    ).fetchall()

    # Far enough back for a month of trends and a night's baseline.
    sessions = store.sleep_sessions(ts - (max(MONTH_DAYS, BASELINE_NIGHTS) + 10) * DAY, ahead)
    recent = [s for s in sessions if s["end"] >= ts - 8 * DAY]
    night = main_sleep(recent, ts)
    earlier = [s for s in main_sleeps(sessions) if night and s["end"] <= night["start"]]
    readings = night_readings(store, night) if night else {}

    pai_days = store.pai_days(ts - 8 * DAY, ahead)
    workouts = _workouts(store, ts - WORKOUT_DAYS * DAY, ahead)

    return {
        "generatedAt": ts,
        "today": {
            "steps": int(steps_today["sum"]) if steps_today else (0 if seen_today else None),
            # None, too, until the strap has served its energy history at all.
            "calories": int(calories_today["sum"])
            if calories_today
            else (0 if seen_today and store.latest(p.CALORIES, until=ahead) else None),
            "stress": stress_today["avg"] if stress_today else None,
            "stressZones": stress_zones(stress_rows, ts),
            "restingHr": _reading(store.latest(p.RESTING_HR, midnight - DAY, ahead)),
            "pai": _latest_pai(pai_days, now),
        },
        "latest": {
            "heartRate": _reading(store.latest(p.HEART_RATE, ts - 15 * 60, ahead)),
            "hrv": _reading(store.latest(p.HRV, day_ago, ahead)),
            "stress": _reading(store.latest(p.STRESS, ts - 3 * 3600, ahead)),
            "spo2": _reading(store.latest(p.SPO2, day_ago, ahead)),
            "temperature": _reading(store.latest(p.TEMPERATURE, ts - 3 * 3600, ahead)),
        },
        "day": {
            "start": day_ago,
            "end": ts,
            "heartRate": store.series(p.HEART_RATE, day_ago, ts + 60, 600),
            "heartRateStats": store.stats(p.HEART_RATE, day_ago, ts + 60),
        },
        "sleep": _sleep_extras(store, night, readings) if night else None,
        "naps": naps_since(recent, night, ts),
        "recovery": recovery(store, readings, earlier) if night else None,
        "workouts": {
            "days": WORKOUT_DAYS,
            "load": sum(w["load"] or 0 for w in workouts),
            "recent": workouts[:WORKOUTS_SHOWN],
        },
        "week": _trends(store, recent, _days(today, 7)),
        "month": _trends(store, sessions, _days(today, MONTH_DAYS)),
    }
