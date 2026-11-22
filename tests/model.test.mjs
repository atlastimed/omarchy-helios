// Tests for Model.js, the widget's pure formatting helpers.
// Run with: TZ=UTC node --test tests/model.test.mjs
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"

const source = readFileSync(new URL("../Model.js", import.meta.url), "utf8").replace(".pragma library", "")
const names = [...source.matchAll(/^function (\w+)/gm)].map((m) => m[1])
const Model = new Function(`${source}\nreturn { ${names.join(", ")}, GLYPH_HEART, WEEKDAYS }`)()

process.env.TZ = "UTC"

test("durations read as hours and padded minutes", () => {
  assert.equal(Model.duration(842), "14h 02m")
  assert.equal(Model.duration(45), "45m")
  assert.equal(Model.duration(null), "—")
})

test("relative times", () => {
  const now = 1_000_000_000_000
  assert.equal(Model.ago(now / 1000 - 30, now), "just now")
  assert.equal(Model.ago(now / 1000 - 300, now), "5 min ago")
  assert.equal(Model.ago(now / 1000 - 2 * 3600, now), "2 h ago")
  assert.equal(Model.ago(now / 1000 - 26 * 3600, now), "yesterday")
})

test("numbers", () => {
  assert.equal(Model.grouped(12345), "12,345")
  assert.equal(Model.number(0.27, 1), "0.3")
  assert.equal(Model.number(undefined), "—")
  assert.equal(Model.reading({ value: 32.43 }, 1), "32.4")
})

test("stress bands match Zepp's", () => {
  assert.equal(Model.stressLabel(39), "Relaxed")
  assert.equal(Model.stressLabel(40), "Normal")
  assert.equal(Model.stressLabel(79), "Medium")
  assert.equal(Model.stressLabel(80), "High")
})

test("status line names the condition", () => {
  const now = 1_000_000_000_000
  assert.equal(Model.statusLine("connected", { lastSync: now / 1000 - 120 }, now), "Connected · synced 2 min ago")
  assert.equal(Model.statusLine("connected", { syncing: true }, now), "Syncing…")
  assert.equal(Model.statusLine("no-daemon", null, now), "Helios service is not running")
})

test("battery glyph steps through the level icons", () => {
  assert.equal(Model.batteryGlyph(100, false), "\u{F0079}")
  assert.equal(Model.batteryGlyph(79, false), String.fromCodePoint(0xf007a + 6))
  assert.equal(Model.batteryGlyph(5, false), "\u{F008E}")
  assert.equal(Model.batteryGlyph(50, true), "\u{F0084}")
})

test("day ticks land on six-hour marks inside the window", () => {
  const start = Date.UTC(2026, 9, 31, 2, 11) / 1000
  const ticks = Model.dayTicks(start, start + 86400)
  assert.deepEqual(ticks.map((t) => t.label), ["06:00", "12:00", "18:00", "00:00"])
})

test("week slots place rows by day and keep gaps", () => {
  const start = Date.UTC(2026, 9, 26) / 1000
  const slots = Model.weekSlots([[start + 86400, 58], [start + 6 * 86400, 61]], start)
  assert.equal(slots.length, 7)
  assert.deepEqual(slots.map((s) => s.value), [null, 58, null, null, null, null, 61])
  assert.equal(slots[0].label, "M")
})

test("Fahrenheit only when locale and timezone are both American", () => {
  assert.equal(Model.useFahrenheit("auto", true, "America/Chicago"), true)
  assert.equal(Model.useFahrenheit("auto", true, "Europe/London"), false)
  assert.equal(Model.useFahrenheit("auto", false, "America/Toronto"), false)
  assert.equal(Model.useFahrenheit("celsius", true, "America/Chicago"), false)
  assert.equal(Model.useFahrenheit("fahrenheit", false, ""), true)
  assert.equal(Model.temperature(33, true), "91.4")
  assert.equal(Model.temperature(32.43, false), "32.4")
})

test("readings older than an hour say how old they are", () => {
  const now = 1_000_000_000_000
  assert.equal(Model.staleNote({ value: 40, at: now / 1000 - 600 }, now), "")
  assert.equal(Model.staleNote({ value: 40, at: now / 1000 - 3 * 3600 }, now), "3 h ago")
  assert.equal(Model.joinNotes("Normal", "3 h ago"), "Normal · 3 h ago")
  assert.equal(Model.joinNotes("Normal", ""), "Normal")
})

test("clock uses the formatter it is given", () => {
  const ts = Date.UTC(2026, 9, 31, 19, 5) / 1000
  assert.equal(Model.clock(ts), "19:05")
  assert.equal(Model.clock(ts, (d) => `${d.getHours() % 12}:05 PM`), "7:05 PM")
  assert.equal(Model.clock(0), "—")
})

test("setup states and week data", () => {
  assert.equal(Model.needsSetup("no-daemon"), true)
  assert.equal(Model.needsSetup("auth-failed"), true)
  assert.equal(Model.needsSetup("offline"), false)
  assert.equal(Model.hasValues([{ value: null }, { value: 3 }]), true)
  assert.equal(Model.hasValues([{ value: null }]), false)
})

test("Fahrenheit timezones are the US ones only", () => {
  assert.equal(Model.useFahrenheit("auto", true, "America/Toronto"), false)
  assert.equal(Model.useFahrenheit("auto", true, "America/Mexico_City"), false)
  assert.equal(Model.useFahrenheit("auto", true, "America/Indiana/Indianapolis"), true)
  assert.equal(Model.useFahrenheit("auto", true, "Pacific/Honolulu"), true)
  assert.equal(Model.useFahrenheit("auto", true, "US/Eastern"), true)
})

test("tick labels", () => {
  assert.equal(Model.hourLabel("6:00 AM"), "6 AM")
  assert.equal(Model.hourLabel("12:00 PM"), "12 PM")
  assert.equal(Model.hourLabel("06:00"), "06:00")
})

test("day slots cover a month and match rows by date", () => {
  const start = Date.UTC(2026, 9, 20) / 1000
  const slots = Model.daySlots([[start, 70], [start + 29 * 86400, 64], [start + 40 * 86400, 1]], start, 30)
  assert.equal(slots.length, 30)
  assert.equal(slots[0].value, 70)
  assert.equal(slots[29].value, 64)
  assert.equal(slots[1].value, null)
  assert.equal(Model.slotAverage(slots), 67)
  assert.equal(Model.slotAverage(Model.daySlots([], start, 7)), null)
  assert.equal(Model.dayLabel(start), "20 Oct")
})

test("a night's reading beside the usual", () => {
  assert.equal(Model.usualNote({ value: 52.4, baseline: 64.7 }), "Usual 65")
  assert.equal(Model.usualNote({ value: 13.9, baseline: 13.6 }, 1), "Usual 13.6")
  assert.equal(Model.usualNote({ value: 52.4, baseline: null }), "")
  assert.equal(Model.usualNote(undefined), "")
  assert.equal(Model.temperatureNote({ value: 34.3, baseline: 34.54 }, false), "−0.2° vs usual")
  assert.equal(Model.temperatureNote({ value: 34.9, baseline: 34.5 }, true), "+0.7° vs usual")
  assert.equal(Model.temperatureNote({ value: 34.52, baseline: 34.5 }, false), "As usual")
  assert.equal(Model.temperatureNote({ value: 34.3, baseline: null }, false), "")
  assert.equal(Model.temperatureNote({ value: null, baseline: 34.5 }, false), "")
})

test("naps and workouts read as one line each", () => {
  const start = Date.UTC(2026, 10, 18, 14, 39) / 1000
  assert.equal(Model.napLine({ start, end: start + 147 * 60, asleepMinutes: 147 }), "Wed 14:39 → 17:06 · 2h 27m")
  const workout = { start, seconds: 2460, name: null, avgHr: 127, maxHr: 149, calories: 1298, load: 45, zones: [0, 240, 1680, 480, 0, 0] }
  assert.equal(Model.workoutTitle(workout), "Workout")
  assert.equal(Model.workoutTitle({ name: "Outdoor running" }), "Outdoor running")
  assert.equal(Model.workoutWhen(workout), "Wed 14:39 · 41m")
  assert.equal(Model.workoutFigures(workout), "127 avg · 149 max bpm · 1,298 kcal · load 45")
  assert.equal(Model.workoutFigures({ avgHr: 120 }), "120 avg bpm")
  assert.equal(Model.workoutFigures({}), "")
  // The strap's first entry is time below the zones, not a zone.
  assert.deepEqual(Model.workoutZones(workout), [4, 28, 8, 0, 0])
  assert.equal(Model.workoutZoneLine(workout), "WARM-UP 4m · FAT BURN 28m · AEROBIC 8m")
  assert.deepEqual(Model.workoutZones({}), [0, 0, 0, 0, 0])
})

test("a folded stage table is noticed", () => {
  assert.equal(Model.hasUnstaged([[0, 60, "light"], [60, 120, "unstaged"]]), true)
  assert.equal(Model.hasUnstaged([[0, 60, "light"]]), false)
  assert.equal(Model.hasUnstaged(undefined), false)
})

test("what the pointer is over reads as a value and a when", () => {
  const at = Date.UTC(2026, 10, 18, 14, 40) / 1000
  assert.deepEqual(Model.heartTip([at, 86.4, 82, 91]), { title: "86 bpm", detail: "14:40 · 82–91" })
  assert.deepEqual(Model.heartTip([at, 70, 70, 70]), { title: "70 bpm", detail: "14:40" })
  assert.deepEqual(Model.heartTip(null), { title: "", detail: "" })
  assert.deepEqual(Model.stageTip([at, at + 32 * 60, "rem"]), { title: "REM", detail: "14:40 → 15:12 · 32m" })
  assert.equal(Model.stageTip([at, at + 60, "unstaged"]).title, "Not staged")
  assert.deepEqual(Model.stageTip(undefined), { title: "", detail: "" })
})

test("signal strength is named weak where links drop, and dropped when stale", () => {
  const now = 1_800_000_000_000
  assert.equal(Model.signalNote({ rssi: -72, at: now / 1000 - 60 }, now), "signal −72 dBm")
  assert.equal(Model.signalNote({ rssi: -88, at: now / 1000 - 60 }, now), "weak signal −88 dBm")
  assert.equal(Model.signalNote({ rssi: -88, at: now / 1000 - 3600 }, now), "")
  assert.equal(Model.signalNote(null, now), "")
  assert.equal(Model.signalNote({}, now), "")
})

test("the point under the pointer is the one drawn there", () => {
  const points = [[0, 60], [600, 61], [1200, 62], [5400, 70]]
  // At each drawn point, that point: not the one before it.
  assert.deepEqual([0, 600, 1200, 5400].map((t) => Model.nearestPoint(points, t, 600)), [0, 1, 2, 3])
  // Between two, the nearer; exactly halfway, the earlier.
  assert.equal(Model.nearestPoint(points, 850, 600), 1)
  assert.equal(Model.nearestPoint(points, 950, 600), 2)
  assert.equal(Model.nearestPoint(points, 900, 600), 1)
  // Before the first and after the last, the end points, within reach.
  assert.equal(Model.nearestPoint(points, -300, 600), 0)
  assert.equal(Model.nearestPoint(points, 6000, 600), 3)
  // In a gap (the strap was off), nothing.
  assert.equal(Model.nearestPoint(points, 3300, 600), -1)
  assert.equal(Model.nearestPoint([], 100, 600), -1)
  assert.equal(Model.nearestPoint(null, 100, 600), -1)
})

test("a path is one word to a shell, whatever is in it", () => {
  assert.equal(Model.shellQuote("/home/me/plugins/helios/setup"), "'/home/me/plugins/helios/setup'")
  assert.equal(Model.shellQuote("/tmp/Sam's config/setup"), "'/tmp/Sam'\\''s config/setup'")
  assert.equal(Model.shellQuote("a'; rm -rf ~; '"), "'a'\\''; rm -rf ~; '\\'''")
  assert.equal(Model.shellQuote('$(id) `id` "x"'), "'$(id) `id` \"x\"'")
})

test("PAI from an earlier day says which day", () => {
  const sat = Date.UTC(2026, 10, 21, 20) / 1000
  assert.equal(Model.paiNote({ ts: sat, today: 5, total: 40, current: true }), "+5.0 today")
  assert.equal(Model.paiNote({ ts: sat, today: 5, total: 40, current: false }), "As of Sat")
  // A service from before the field existed: as it was.
  assert.equal(Model.paiNote({ ts: sat, today: 5, total: 40 }), "+5.0 today")
  assert.equal(Model.paiNote(null), "")
})

test("a total nobody has counted is a dash, not a zero", () => {
  assert.equal(Model.grouped(null), "—")
  assert.equal(Model.grouped(0), "0")
})

// Run `body` with the process in another time zone, then put UTC back.
function inZone(zone, body) {
  process.env.TZ = zone
  try {
    return body()
  } finally {
    process.env.TZ = "UTC"
  }
}

test("day ticks stay on six-hour clock marks when the clocks change", () => {
  for (const zone of ["Europe/London", "America/New_York", "Australia/Sydney"]) {
    inZone(zone, () => {
      // Every day of 2026, a 24-hour window ending at 23:30 local.
      for (let day = 1; day <= 365; day++) {
        const end = new Date(2026, 0, day, 23, 30).getTime() / 1000
        const hours = Model.dayTicks(end - 86400, end).map((t) => new Date(t.at * 1000).getHours())
        assert.ok(hours.length >= 3 && hours.every((h) => h % 6 === 0), `${zone} day ${day}: ${hours}`)
        assert.deepEqual(Model.dayTicks(end - 86400, end).map((t) => t.label.slice(3)), hours.map(() => "00"))
      }
    })
  }
})

test("day slots keep one slot a day across a clock change", () => {
  inZone("Europe/London", () => {
    // The week containing 25 October 2026, when London's clocks go back.
    const first = new Date(2026, 9, 22).getTime() / 1000
    const rows = [0, 1, 2, 3, 4, 5, 6].map((n) => [new Date(2026, 9, 22 + n, 12).getTime() / 1000, n + 1])
    const slots = Model.daySlots(rows, first, 7)
    assert.deepEqual(slots.map((s) => s.value), [1, 2, 3, 4, 5, 6, 7])
    assert.deepEqual(slots.map((s) => new Date(s.day * 1000).getDate()), [22, 23, 24, 25, 26, 27, 28])
  })
})

test("a count of days that isn't one is held to a week or a month", () => {
  assert.equal(Model.daySlots([], 0, 1e6).length, 62)
  assert.equal(Model.daySlots([], 0, -3).length, 1)
  assert.equal(Model.daySlots([], 0, "many").length, 7)
  assert.equal(Model.daySlots([], 0, 30).length, 30)
})

test("only a rate a heart can beat at counts as a pulse", () => {
  assert.equal(Model.pulse(68), 68)
  assert.equal(Model.pulse(68.4), 68)
  for (const bad of [0, 24, 231, 1e6, -5, NaN, Infinity, "68", null, undefined, {}]) assert.equal(Model.pulse(bad), null)
})
