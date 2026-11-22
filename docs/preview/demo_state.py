"""A demo Helios state for screenshots: invented readings pushed through the
real store and summary, so the widget shows what it would for a real wearer
without anyone's health data.

    PYTHONPATH=src uv run python docs/preview/demo_state.py <state.json> <scratch folder>

demo_daemon.py serves the result on Helios's socket in place of the service.
"""

import json
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from helios import parsers as p
from helios import summary
from helios.store import Store

out, tmp = Path(sys.argv[1]), Path(sys.argv[2])
random.seed(11)
db = tmp / "demo.db"
for f in tmp.glob("demo.db*"):
    f.unlink()
store = Store(db)
now = datetime.now().astimezone().replace(second=0, microsecond=0)
ts = int(now.timestamp())
today = now.date()
mid = summary.local_midnight
S = []


def night(day, bed_h=23.4, hours=7.6, score=None, last=False):
    """A night ending on `day` morning; returns the session."""
    start = mid(day) - int((24 - bed_h) * 3600) + random.randint(-1500, 1500)
    end = start + int(hours * 3600) + random.randint(-1800, 1800)
    stages, t, minutes = [], start, {"light": 0, "deep": 0, "rem": 0, "awake": 0}
    cycle = ["light", "deep", "light", "rem"]
    i = 0
    while t < end:
        kind = cycle[i % 4]
        span = {
            "light": random.randint(18, 34),
            "deep": random.randint(20, 38) if i < 9 else random.randint(8, 14),
            "rem": random.randint(12, 28),
        }[kind] * 60
        if random.random() < 0.13 and i > 2:
            kind, span = "awake", random.randint(2, 6) * 60
        span = min(span, end - t)
        stages.append((t, t + span, kind))
        minutes[kind] += span // 60
        t += span
        i += 1
    asleep = sum(v for k, v in minutes.items() if k != "awake")
    sc = score or max(61, min(93, int(34 + asleep / 60 * 6.2 + random.randint(-7, 7))))
    hrv = 58 if last else 52 + random.gauss(0, 5)
    for m in range(start, end, 300):
        S.append((p.HRV, m, max(22, round(hrv + random.gauss(0, 4 if last else 9)))))
        S.append((p.RESPIRATION, m, round(14.2 + random.gauss(0, 0.6), 1)))
        S.append((p.TEMPERATURE, m, round(34.6 + random.gauss(0, 0.12) + (0.22 if last else 0), 2)))
        S.append((p.SPO2, m, min(100, round(96.6 + random.gauss(0, 0.9)))))
    rhr = 52 if last else round(54.5 + random.gauss(0, 1.6))
    S.append((p.RESTING_HR, end + 120, rhr))
    return p.SleepSession(
        reported=end + 600,
        start=start,
        end=end,
        score=sc,
        avg_hr=rhr + 3,
        minutes=minutes,
        stages=stages,
        bed_start=start - random.randint(4, 16) * 60,
        bed_end=end + 300,
        nap=False,
        day=mid(day),
    ), rhr


sessions = []
last_night = None
for back in range(34, -1, -1):
    day = today - timedelta(days=back)
    s, rhr = night(
        day,
        hours=7.7 if back == 0 else random.choice([6.4, 6.9, 7.3, 7.6, 7.9, 8.2]),
        score=88 if back == 0 else None,
        last=back == 0,
    )
    # the day's steps, filed hourly
    total = random.randint(4200, 11800)
    hours = [h for h in range(7, 22) if mid(day) + h * 3600 < ts]
    if back == 0:
        total = 6412
    for h in hours:
        S.append((p.STEPS, mid(day) + h * 3600, round(total / len(hours))))
    # Run early in the morning, tonight's sleep isn't over yet: leave it out.
    if back == 0 and s.end > ts - 1800:
        continue
    sessions.append(s)
    last_night = s if back == 0 else last_night
store.add_sleep(sessions)

# Today: minute heart rate over 24 h, with last night's lows and an evening workout yesterday.
w_start = mid(today) - int(5.3 * 3600)  # yesterday, 18:42
for m in range(ts - 86400, ts + 1, 60):
    asleep = last_night and last_night.start <= m < last_night.end
    hour = datetime.fromtimestamp(m).hour + datetime.fromtimestamp(m).minute / 60
    if asleep:
        depth = math.sin((m - last_night.start) / (last_night.end - last_night.start) * math.pi)
        hr = 57 - 6 * depth + random.gauss(0, 1.6)
        S.append((p.HEART_RATE_LOW, m, round(hr - random.uniform(1, 4))))
    elif w_start <= m < w_start + 42 * 60:
        ramp = min(1, (m - w_start) / 420)
        hr = 96 + 46 * ramp + 8 * math.sin(m / 190) + random.gauss(0, 3)
    else:
        hr = 70 + 9 * math.sin((hour - 9) / 24 * 2 * math.pi) + 6 * math.sin(m / 1700) + random.gauss(0, 3.2)
    S.append((p.HEART_RATE, m, round(hr)))
    if not asleep and m >= mid(today):
        S.append((p.CALORIES, m, round(random.uniform(0.2, 1.9), 2)))
for m in range(mid(today), ts, 300):
    if not (last_night and last_night.start <= m < last_night.end):
        S.append((p.STRESS, m, max(8, min(92, round(34 + 16 * math.sin(m / 5200) + random.gauss(0, 9))))))
S.append((p.TEMPERATURE, ts - 600, 33.9))
S.append((p.SPO2, ts - 2400, 98))
S.append((p.HRV, ts - 5400, 61))
store.add_samples(S)


def workout(start, kind, minutes, avg, mx, kcal, load, zones):
    return p.Workout(
        start=start,
        end=start + minutes * 60,
        kind=kind,
        automatic=False,
        seconds=minutes * 60,
        calories=kcal,
        avg_hr=avg,
        max_hr=mx,
        min_hr=avg - 30,
        aerobic=3.1,
        anaerobic=1.2,
        load=load,
        zone_max=[104, 123, 142, 161, 180],
        zone_seconds=[z * 60 for z in zones],
    )


for w in [
    workout(w_start, 0x01, 42, 138, 157, 412, 64, [1, 3, 9, 22, 7, 0]),
    workout(mid(today - timedelta(days=3)) + int(7.25 * 3600), 0x08, 55, 126, 148, 488, 51, [2, 6, 21, 24, 2, 0]),
    workout(mid(today - timedelta(days=5)) + int(12.5 * 3600), 0x03, 38, 104, 121, 196, 18, [6, 24, 8, 0, 0, 0]),
]:
    store.add_workout(w, b"demo")
off = int(now.utcoffset().total_seconds())
total = 58.0
rows = []
for back in range(7, -1, -1):
    gain = random.choice([4, 9, 12, 16, 7, 21, 11, 14])
    total = total - 9 + gain
    rows.append(
        p.Pai(
            timestamp=mid(today - timedelta(days=back)) + 20 * 3600 if back else ts - 900,
            utc_offset=off,
            low=3,
            moderate=gain / 2,
            high=gain / 2 - 1.5,
            minutes_low=40,
            minutes_moderate=22,
            minutes_high=9,
            today=float(gain),
            total=round(total, 1),
        )
    )
store.add_pai(rows)

snap = summary.build(store, now)
state = {
    "type": "state",
    "version": json.loads(Path("manifest.json").read_text())["version"],
    "timezone": "Europe/London",
    "status": "connected",
    "message": "",
    "syncing": False,
    "live": True,
    "lastSync": ts - 180,
    "syncMinutes": 10,
    "device": {"name": "Amazfit Helio Strap", "address": "AA:BB:CC:DD:EE:FF", "firmware": "3.27.0.1"},
    "battery": {"level": 82, "charging": False, "lastCharge": ts - 3 * 86400, "at": ts - 200},
    "signal": {"rssi": -64, "at": ts - 400},
    "heartRate": {"bpm": 68, "at": ts},
    **snap,
}
out.write_text(json.dumps({"connected": state, "hr": {"type": "hr", "bpm": 68, "at": ts}}))
print(
    "sleep",
    snap["sleep"]["score"],
    snap["sleep"]["asleepMinutes"],
    "naps",
    len(snap["naps"]),
    "workouts",
    len(snap["workouts"]["recent"]),
    "recovery",
    {k: v for k, v in snap["recovery"].items() if k != "nights"},
    "steps",
    snap["today"]["steps"],
    "week sleep",
    len(snap["week"]["sleep"]),
)
