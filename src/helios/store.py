# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""SQLite history of everything fetched from the strap."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .parsers import Pai, Sample, SleepSession, Workout

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    metric INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (metric, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sleep (
    start INTEGER PRIMARY KEY,
    end INTEGER NOT NULL,
    reported INTEGER NOT NULL,
    score INTEGER NOT NULL,
    avg_hr INTEGER NOT NULL,
    minutes TEXT NOT NULL,
    stages TEXT NOT NULL,
    nap INTEGER NOT NULL DEFAULT 0,
    bed_start INTEGER,
    bed_end INTEGER,
    day INTEGER  -- the midnight of the strap's report; NULL in rows from before 0.5
);

CREATE TABLE IF NOT EXISTS workouts (
    start INTEGER PRIMARY KEY,
    end INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    automatic INTEGER NOT NULL,
    seconds INTEGER NOT NULL,
    calories INTEGER, avg_hr INTEGER, max_hr INTEGER, min_hr INTEGER,
    distance REAL, aerobic REAL, anaerobic REAL, load INTEGER,
    zones TEXT NOT NULL,
    raw BLOB NOT NULL  -- the strap's summary, for fields decoded later
);

CREATE TABLE IF NOT EXISTS pai (
    ts INTEGER PRIMARY KEY,
    utc_offset INTEGER NOT NULL,
    low REAL, moderate REAL, high REAL,
    minutes_low INTEGER, minutes_moderate INTEGER, minutes_high INTEGER,
    today REAL NOT NULL,
    total REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cursor (
    kind INTEGER PRIMARY KEY,
    since INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# A database made by an earlier version may hold a table nothing here uses any
# more. It is left as it is.
SCHEMA_VERSION = 2
# An upper time bound that excludes nothing.
FOREVER = 1 << 62
# How far ahead of this computer's clock (seconds) a time from the strap may
# be and still be believed. The strap's clock is set from the phone's, so a
# real difference is seconds; a record stamped further ahead is a bad record.
# Nothing later than this is stored, shown, or allowed to move a fetch cursor.
CLOCK_SLACK = 600
SLEEP_COLUMNS_ADDED = {
    "nap": "INTEGER NOT NULL DEFAULT 0",
    "bed_start": "INTEGER",
    "bed_end": "INTEGER",
    "day": "INTEGER",
}
SLEEP_KIND = 0x48  # the history type whose cursor a schema change rewinds
REFETCH_DAYS = 60


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database from an earlier version up to this schema."""
        have = {row[1] for row in self.db.execute("PRAGMA table_info(sleep)")}
        for column, kind in SLEEP_COLUMNS_ADDED.items():
            if column not in have:
                self.db.execute(f"ALTER TABLE sleep ADD COLUMN {column} {kind}")
        if self.meta("schema", 1) < SCHEMA_VERSION:  # type: ignore[operator]
            # Sleep reports are read again for what earlier versions skipped
            # (naps, bed times), as far back as the strap still has them.
            if self.db.execute("SELECT 1 FROM cursor WHERE kind = ?", (SLEEP_KIND,)).fetchone():
                self.set_cursor(SLEEP_KIND, int(time.time()) - REFETCH_DAYS * 86400)
            self.set_meta("schema", SCHEMA_VERSION)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # The connection is in autocommit mode, so `with self.db` would not
        # open a transaction; batches and delete-then-insert pairs need one.
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # ---- writes --------------------------------------------------------------

    def add_samples(self, samples: list[Sample]) -> None:
        if samples:
            with self.transaction():
                self.db.executemany("INSERT OR REPLACE INTO samples VALUES (?, ?, ?)", samples)

    def merge_samples(self, samples: list[Sample], keep: str) -> None:
        """Add samples, keeping the lower ("MIN") or higher ("MAX") of a new
        value and one already stored for the same metric and time."""
        if keep not in ("MIN", "MAX"):
            raise ValueError(keep)
        if samples:
            with self.transaction():
                self.db.executemany(
                    "INSERT INTO samples VALUES (?, ?, ?) ON CONFLICT (metric, ts) "
                    f"DO UPDATE SET value = {keep}(value, excluded.value)",
                    samples,
                )

    def add_sleep(self, sessions: list[SleepSession]) -> None:
        with self.transaction():
            for s in sorted(sessions, key=lambda s: s.reported):
                # A day's later report replaces its earlier ones outright, so
                # a nap the strap has since dropped or merged doesn't linger;
                # and it revises any session it overlaps, whatever its day.
                self.db.execute("DELETE FROM sleep WHERE day = ? AND reported < ?", (s.day, s.reported))
                self.db.execute(
                    "DELETE FROM sleep WHERE start < ? AND end > ? AND reported <= ?", (s.end, s.start, s.reported)
                )
                newer = self.db.execute(
                    "SELECT 1 FROM sleep WHERE reported > ? AND (day = ? OR (start < ? AND end > ?))",
                    (s.reported, s.day, s.end, s.start),
                ).fetchone()
                if newer:
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO sleep (start, end, reported, score, avg_hr, minutes, stages, nap, "
                    "bed_start, bed_end, day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        s.start,
                        s.end,
                        s.reported,
                        s.score,
                        s.avg_hr,
                        json.dumps(s.minutes),
                        json.dumps(s.stages),
                        int(s.nap),
                        s.bed_start,
                        s.bed_end,
                        s.day,
                    ),
                )

    def add_workout(self, w: Workout, raw: bytes) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO workouts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                w.start,
                w.end,
                w.kind,
                int(w.automatic),
                w.seconds,
                w.calories,
                w.avg_hr,
                w.max_hr,
                w.min_hr,
                w.distance,
                w.aerobic,
                w.anaerobic,
                w.load,
                json.dumps({"max": w.zone_max, "seconds": w.zone_seconds}),
                raw,
            ),
        )

    def add_pai(self, rows: list[Pai]) -> None:
        with self.transaction():
            for p in sorted(rows, key=lambda p: p.timestamp):
                # One row per local day: the newest reading of a day replaces the others.
                day = (p.timestamp + p.utc_offset) // 86400
                if self.db.execute(
                    "SELECT 1 FROM pai WHERE (ts + utc_offset) / 86400 = ? AND ts > ?", (day, p.timestamp)
                ).fetchone():
                    continue
                self.db.execute("DELETE FROM pai WHERE (ts + utc_offset) / 86400 = ?", (day,))
                self.db.execute(
                    "INSERT OR REPLACE INTO pai VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p.timestamp,
                        p.utc_offset,
                        p.low,
                        p.moderate,
                        p.high,
                        p.minutes_low,
                        p.minutes_moderate,
                        p.minutes_high,
                        p.today,
                        p.total,
                    ),
                )

    def reset_cursors(self) -> None:
        self.db.execute("DELETE FROM cursor")

    def cursor(self, kind: int, default: int) -> int:
        row = self.db.execute("SELECT since FROM cursor WHERE kind = ?", (kind,)).fetchone()
        return row[0] if row else default

    def set_cursor(self, kind: int, since: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (kind, since))

    def set_meta(self, key: str, value: object) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def meta(self, key: str, default: object = None) -> object:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def prune(self, keep_days: int) -> None:
        if keep_days > 0:
            self.db.execute("DELETE FROM samples WHERE ts < ?", (int(time.time()) - keep_days * 86400,))

    # ---- reads ---------------------------------------------------------------

    def latest(self, metric: int, since: int = 0, until: int = FOREVER) -> tuple[int, float] | None:
        row = self.db.execute(
            "SELECT ts, value FROM samples WHERE metric = ? AND ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (metric, since, until),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def series(self, metric: int, start: int, end: int, bucket: int) -> list[list[float]]:
        """Bucketed [bucket start, mean, min, max] rows."""
        rows = self.db.execute(
            "SELECT (ts / ?) * ? AS b, AVG(value), MIN(value), MAX(value) FROM samples "
            "WHERE metric = ? AND ts >= ? AND ts < ? GROUP BY b ORDER BY b",
            (bucket, bucket, metric, start, end),
        ).fetchall()
        return [[b, round(avg, 1), lo, hi] for b, avg, lo, hi in rows]

    def stats(self, metric: int, start: int, end: int) -> dict[str, float] | None:
        row = self.db.execute(
            "SELECT AVG(value), MIN(value), MAX(value), SUM(value), COUNT(*) FROM samples "
            "WHERE metric = ? AND ts >= ? AND ts < ?",
            (metric, start, end),
        ).fetchone()
        if not row or not row[4]:
            return None
        return {"avg": round(row[0], 1), "min": row[1], "max": row[2], "sum": row[3], "count": row[4]}

    def daily(self, metric: int, days: list[tuple[int, int]], aggregate: str = "AVG") -> list[list[float]]:
        """[day start, value] for each (start, end) local day that has data.

        Days are passed in rather than derived from one UTC offset, so a week
        spanning a DST change still splits at local midnight."""
        if aggregate not in ("AVG", "SUM", "MIN", "MAX"):
            raise ValueError(aggregate)
        out = []
        for start, end in days:
            (value,) = self.db.execute(
                f"SELECT {aggregate}(value) FROM samples WHERE metric = ? AND ts >= ? AND ts < ?", (metric, start, end)
            ).fetchone()
            if value is not None:
                out.append([start, round(value, 1)])
        return out

    def sleep_sessions(self, since: int, until: int = FOREVER) -> list[dict]:
        rows = self.db.execute(
            "SELECT start, end, score, avg_hr, minutes, stages, nap, bed_start, bed_end FROM sleep "
            "WHERE end >= ? AND start <= ? ORDER BY start",
            (since, until),
        ).fetchall()
        return [
            {
                "start": start,
                "end": end,
                "score": score,
                "avgHr": avg_hr,
                "minutes": json.loads(minutes),
                "stages": json.loads(stages),
                "nap": bool(nap),
                "bedStart": bed_start,
                "bedEnd": bed_end,
            }
            for start, end, score, avg_hr, minutes, stages, nap, bed_start, bed_end in rows
        ]

    def workouts(self, since: int, until: int = FOREVER) -> list[dict]:
        rows = self.db.execute(
            "SELECT start, end, kind, automatic, seconds, calories, avg_hr, max_hr, aerobic, anaerobic, load, zones "
            "FROM workouts WHERE end >= ? AND start <= ? ORDER BY start DESC",
            (since, until),
        ).fetchall()
        return [
            {
                "start": start,
                "end": end,
                "kind": kind,
                "automatic": bool(automatic),
                "seconds": seconds,
                "calories": calories,
                "avgHr": avg_hr,
                "maxHr": max_hr,
                "aerobic": aerobic,
                "anaerobic": anaerobic,
                "load": load,
                "zones": json.loads(zones).get("seconds", []),
            }
            for start, end, kind, automatic, seconds, calories, avg_hr, max_hr, aerobic, anaerobic, load, zones in rows
        ]

    def pai_days(self, since: int, until: int = FOREVER) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, utc_offset, today, total, minutes_low, minutes_moderate, minutes_high FROM pai "
            "WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (since, until),
        ).fetchall()
        return [
            {"ts": ts, "offset": off, "today": round(today, 1), "total": round(total, 1), "minutes": [lo, mod, hi]}
            for ts, off, today, total, lo, mod, hi in rows
            # Versions before 0.7 stored whatever the strap sent, infinity included.
            if math.isfinite(today) and math.isfinite(total)
        ]
