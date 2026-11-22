# History types Gadgetbridge doesn't decode

The Helio Strap serves history types that Gadgetbridge's `HuamiFetchDataType`
doesn't list, and fields inside known types that it doesn't read. They were
dumped with Helios's own client on 1 November 2026 (firmware 3.25.0.3) and
again on 18 November 2026 (firmware 3.27.0.1, after three weeks of wear),
acknowledged with "keep on device" like every other fetch, and compared with
the types Helios already decodes and with the figures the Zepp app holds for
the same days. Helios shows a reading only once its meaning is confirmed.

| Type | Seen | What it is | Confidence | In Helios |
|---|---|---|---|---|
| `0x55` | ~1.2 MB, ~69 h | Heart rate every second | Confirmed | Yes: each minute's low and high |
| `0x57` | ~1.9 MB, ~69 h | The same seconds, in five-minute blocks | Confirmed | No: `0x55` has them |
| `0x58` | 12 B a minute | Steps, active calories and a stride distance | Calories confirmed | Yes: active calories |
| `0x27` | 20 KB | The algorithms' diagnostic log | High | No: not readings |
| `0x4a` | 2.9 KB a day | Daily index of the `0x27` entries | High | No |
| `0x3b` | 1.4 KB | 28-day training-load state | Daily load confirmed, rest unknown | No: workouts carry their load |
| `0x39` | 569 B a day | Daily record, almost all zero | Unknown | No |
| `0x4e` | 9 B a record | A few values a day, in and out of sleep | Unknown | No |
| `0x40` | 15 B, one record | Eleven byte values around 100 to 111 | Unknown | No |

Every type from `0x01` to `0xff` was tried. Debug logs (`0x07`, which
Gadgetbridge names) announce a length and then send nothing for minutes, so
Helios never asks for them. Statistics (`0x2c`), which Gadgetbridge names but
doesn't parse, is a protobuf event log: the strap's serial number, the paired
phone, connection events. It holds no health readings.

Two types that Gadgetbridge does decode were new to Helios in 0.5: workout
summaries (`0x05`, one protobuf message a fetch, `WorkoutSummary` in
Gadgetbridge's `huami.proto`) and, unused, workout details (`0x06`). The
strap files workouts it notices by itself, marked as automatic; the one
sampled had a type (223) that Gadgetbridge's list doesn't name, so Helios
calls it a workout. Its duration, average heart rate and calories matched
the Zepp app's record of the same workout.

## `0x55`: heart rate every second

Five-byte records: `u32` Unix time, `u8` bpm. One a second while the strap is
worn, plus a second reading at `:01` of most minutes. The strap held about 30
hours on the first dump and about 69 on the second, and the oldest record
moved forward between fetches, so older seconds are dropped. A fetch from a
whole minute returns records from that minute on.

`0x57` carries the same readings in another shape: blocks of a `u32` time, a
`u16` count (always 300) and that many bytes, one a second, `0xff` where there
is none. Over 54,000 seconds present in both, the two agree (0.02 bpm apart
on average).

Confirmed against the strap's own per-minute heart rate (activity history,
`0x01`): over 1,559 minutes the mean of each minute's per-second readings is
within 1.1 bpm of the activity value on average (standard deviation 1.2). A
second fetch returned the same records again, and a sync with the daemon
stored 1,833 minutes from one 545 KB batch. The per-minute history in turn
matches Zepp exactly: the app's lowest, highest and average heart rate for a
day are the activity history's, not this one's. By the second the same day
reached several bpm past both per-minute extremes, in smooth runs rather than
single spikes. The strap smooths these readings itself: 98.8% of
second-to-second steps are 2 bpm or less. A few jumps of 10 to 33 bpm do occur
(about 1 in 8,000 steps).

Helios stores each minute's lowest and highest rate (`HEART_RATE_LOW`,
`HEART_RATE_HIGH`), not every second: the activity history already has a rate
per minute, and a year of seconds would be about 30 million rows. The rates
are medians of five consecutive readings, so a jump of a second or two can't
set an extreme; across a day that moves a minute's low or high by about
0.5 bpm on average.

The cursor stops just past the last record. A fetch starts on a whole minute,
so the next one repeats that minute, and its extremes merge with the stored
ones (the lower low, the higher high) instead of replacing them, since the
strap may have dropped the minute's first seconds in between. On the strap, a
fetch 45 seconds after another reported nothing new, and one two minutes later
returned the repeated minute from its first second, with no gap. A record
stamped more than ten minutes ahead of the computer's clock is neither stored
nor allowed to move the cursor.

The panel shows the night's lowest rate under the sleep section's average:
the lowest of these minute lows outside awake stages. Minutes the strap no
longer had seconds for when Helios fetched them count by their per-minute
rate. Zepp shows no per-second figure, so this one has no Zepp counterpart;
it rests on the per-minute agreement above.

## `0x27`: algorithm diagnostic log

Version byte `2`, then 485-byte entries: `u32` time, `0x80`, a kind byte
(`0x5a` or `0x5c`), 479 bytes of payload. Some payloads are plain text from
the algorithm library (version 1.040), one line per module: `ALG_CAL`,
`ALG_AHR`, `SYS min act`, `SPO2 min`, `STRESS min`, `PAI daily`,
`SLEP v3 embedding`, `ALG_SLEP`, `SLEEP`, `HRE min hr`. Its PAI line matches the PAI
record (`0x0d`) for the same day. The other payloads are binary,
incompressible, and repeat byte for byte under later timestamps, so they look
like snapshots of internal state rather than measurements. Nothing here is a
reading Zepp shows.

## `0x4a`: index of the diagnostic log

One 2,896-byte record per day from local midnight: `u32` midnight, `u32`, `u32`,
the bytes `63 01 00 n`, then `n` 12-byte entries (`u32` time, `u32` kind,
`u32` zero), the rest zero. The entries are exactly the `0x27` timestamps and
kind bytes, carried over from earlier days. The two `u32` header fields are
not identified.

## `0x58`: steps, active calories, stride distance

Twelve bytes a minute, with no timestamps, like the activity history: byte 0
is `1`, byte 8 repeats the activity history's steps for the minute, byte 9 is
the active energy burned in kcal, and the last two bytes are a `u16` distance
in centimetres. Bytes 4 to 7 are flags that follow the pace (set through a
run, for instance).

The calories are confirmed: summed over a day they equal the app's active
calories for that day on 11 days of 13, a day with a workout among them, and
are 1 kcal above on the other two; the strap's realtime counter (below)
agrees. The distance is the steps times one
fixed stride whatever the pace, which is not the distance Zepp
shows: the app applies a stride to each bout by its kind, and came out about
10% apart on the days compared. Helios stores the calories and leaves the
distance alone.

## `0x3b`: 28-day training-load state

One protobuf message (no framing). Top-level varints and floats (fields 1 to
33), a timestamp (field 26), and a 28-slot ring (field 27) of per-day packed
arrays: year, month and day; timestamps; eight float arrays; and three
constant varint arrays. The arrays were all zero until the first workout.
After it, the day's slot in the first float array held the workout's load
(the summary's rounded figure, unrounded), and two more arrays held values
from that day on that look like running totals or targets. With one workout
there is nothing to tell those apart, so only the daily load is identified,
and a workout's own summary already carries it.

## `0x39`: daily record

One 569-byte record on some days: `u32` local midnight, then 565 bytes, all
zero except one constant byte. Still empty after three weeks.

## `0x4e`: sparse values

Nine-byte records: `u32` time, `i8` UTC offset in quarter hours, `u32` value
between about 2,500 and 16,000. Three to six a day, roughly two hours apart,
most of them during sleep and naps. The values don't follow heart rate, HRV,
skin temperature or breathing rate over the same windows, they are not the
gaps between the records, and they are not BioCharge (below).

## `0x40`: one record

A single 15-byte record in three weeks: a `u32` time in the middle of a
night, then eleven bytes between 100 and 111. Nothing to compare it with.

## Inside the sleep report (`0x48`)

Gadgetbridge reads the sleep and wake times, the score and the stages from
the 594-byte report. The rest of it, as found on the strap:

| Offset | Field |
|---|---|
| `0x0a`, `0x0c` | Fell asleep, woke (`u16` minutes from the midnight before the report's day) |
| `0x0e`, `0x10` | Went to bed, got up, in the same units |
| `0x15`, `0x16` | Average heart rate, score |
| `0x17` | Number of naps; then from `0x18` six bytes a nap: start, end, length |
| `0x54`, `0x55` | Number of stages in the main sleep (at most 50), and in the second table |
| `0x56` | 50 five-byte stages of the main sleep: start, end, kind |
| `0x150` | 50 more: the gaps (kind 128) and stages of the day's naps |
| `0x24a` | Minutes of REM, light, deep and awake in the main sleep |

The bed times are confirmed against the app: sleep start minus bed start is
its "time to fall asleep" on 15 nights of 16 (the other was a report the
strap revised after the app's copy). The naps are the app's
nap list, and their stages are what it draws for them.

A main sleep with more than 50 stages keeps the first 49 and folds everything
after them into the 50th, labelled with one kind. The report's own totals
still count the real stages, so the two disagree, by hours on a long night.
The app's copy of such a night has the same 50 stages. Helios marks it unstaged when the totals and the stages differ by
more than ten minutes, and takes the totals as the truth.

The strap decides which sleep of a day is the main one and which are naps,
and revises that as the day goes: a six-hour sleep was a night in the
morning's report and a nap in the evening's, once a longer sleep had followed.

## Realtime steps (endpoint `0x0016`)

Outside the history fetch, the request Gadgetbridge sends for realtime steps
(`0x03`) answers with the day so far: after two header bytes and a flag,
`u32` steps, `u32` metres (the same stride distance as `0x58`) and `u32`
active kcal. All three equalled the day's sums from the history types.
Helios doesn't use it: the history gives the same totals with their minutes.

## Checked against the Zepp app

Figures the Zepp app holds for the same days:

- Heart rate, steps, stress and SpO2 match the types Helios already decodes.
  Daily steps are equal to the step on 17 days of 19; on the other two the
  app's copy of the day stops partway through it.
- Active calories match `0x58`, and sleep score, stage totals, time to fall
  asleep, naps and the workout match the records described above.
- Readiness: the app keeps, for each night, the mean HRV during the main
  sleep, the resting heart rate filed as it ends, and the mean skin
  temperature during it, and scores each. Helios's own means of the HRV and
  temperature history over the same sleep agree with the app's to within
  rounding (HRV to the millisecond or one off, temperature to 0.02 °C) on
  every night compared, and its baselines for HRV and resting heart rate are
  the plain mean of the earlier nights, appearing after seven. So the panel
  shows those readings beside that mean. The scores themselves (readiness
  and its parts) and the app's calibrated skin-temperature deviation are in
  no history type and are not a simple function of the readings; Helios
  doesn't show or imitate them.
- BioCharge (labelled Hybrid charge in the app) is a continuous 0 to 100 curve
  that climbs through the night and falls through the day. Nothing in any
  type matches it. It may be computed by the app from the synced history or
  served outside the history fetch; neither is confirmed.
- Training load: the day's load in `0x3b` matches the workout's. Training
  status had nothing to compare after one workout.
- Distance: see `0x58`. The app's figure isn't the strap's.

## Checking again

To look again, stop the service (`systemctl --user stop helios`), call
`Strap.fetch(kind, since)` for each type from a script, and start the service
afterwards. `fetch` acknowledges with "keep on device", so Zepp still gets
everything. Skip `0x07`. Keep the dumps out of the repository: they are
health data.
