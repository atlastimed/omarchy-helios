.pragma library

// Formatting and small derivations for the Helios widget. Pure functions, so
// they can be exercised without the shell (tests/model.test.mjs).

var GLYPH_HEART = "\u{F02D1}"            // nf-md-heart
var GLYPH_HEART_OUTLINE = "\u{F02D5}"    // nf-md-heart_outline
var GLYPH_HEART_OFF = "\u{F1434}"        // nf-md-heart_off_outline
var GLYPH_STRAP = "\u{F0897}"            // nf-md-watch_variant
var GLYPH_SYNC = "\u{F04E6}"             // nf-md-sync
var GLYPH_SETUP = "\u{F0493}"            // nf-md-cog

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

// Time of day. `format` is a Qt time format (the locale's short format in the
// shell); without one, 24-hour HH:mm.
function clock(ts, format) {
  if (!ts) return "—"
  var d = new Date(ts * 1000)
  if (format && typeof format === "function") return format(d)
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes())
}

function weekday(ts) {
  return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(ts * 1000).getDay()]
}

function duration(minutes) {
  if (minutes === null || minutes === undefined || isNaN(minutes)) return "—"
  minutes = Math.round(minutes)
  var h = Math.floor(minutes / 60)
  var m = minutes % 60
  if (h === 0) return m + "m"
  return h + "h " + pad2(m) + "m"
}

function ago(ts, nowMs) {
  if (!ts) return ""
  var seconds = Math.max(0, Math.round(nowMs / 1000 - ts))
  if (seconds < 90) return "just now"
  var minutes = Math.round(seconds / 60)
  if (minutes < 60) return minutes + " min ago"
  var hours = Math.round(minutes / 60)
  if (hours < 24) return hours + " h ago"
  var days = Math.round(hours / 24)
  return days === 1 ? "yesterday" : days + " days ago"
}

// How old a reading is, but only once it is old enough to matter.
function staleNote(item, nowMs) {
  if (!item || !item.at || nowMs / 1000 - item.at < 3600) return ""
  return ago(item.at, nowMs)
}

function joinNotes(a, b) {
  return [a, b].filter(function(s) { return s !== "" }).join(" · ")
}

function number(value, digits) {
  if (value === null || value === undefined || isNaN(value)) return "—"
  return Number(value).toFixed(digits || 0)
}

function grouped(value) {
  if (value === null || value === undefined || isNaN(value)) return "—"
  return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
}

function reading(item, digits) {
  return item ? number(item.value, digits) : "—"
}

var US_TIMEZONE = new RegExp("^(US/|Pacific/Honolulu$|America/(New_York|Chicago|Denver|Los_Angeles|Phoenix"
  + "|Anchorage|Adak|Boise|Detroit|Juneau|Sitka|Metlakatla|Nome|Yakutat|Menominee"
  + "|Indiana/|Kentucky/|North_Dakota/|Indianapolis$|Louisville$))")

function useFahrenheit(setting, localeIsUsImperial, timezone) {
  if (setting === "fahrenheit") return true
  if (setting === "celsius") return false
  // An en_US locale is a common default far from the US, so the timezone
  // has to agree before readings switch to Fahrenheit.
  return !!localeIsUsImperial && US_TIMEZONE.test(String(timezone || ""))
}

function temperature(celsius, fahrenheit) {
  if (celsius === null || celsius === undefined || isNaN(celsius)) return "—"
  return number(fahrenheit ? celsius * 9 / 5 + 32 : celsius, 1)
}

function stressLabel(value) {
  if (value === null || value === undefined) return ""
  if (value < 40) return "Relaxed"
  if (value < 60) return "Normal"
  if (value < 80) return "Medium"
  return "High"
}

function sleepScoreLabel(score) {
  if (!score) return ""
  if (score >= 85) return "Excellent"
  if (score >= 70) return "Good"
  if (score >= 60) return "Fair"
  return "Poor"
}

// The hero's meta line: the current condition, not a description.
function statusLine(status, snapshot, nowMs) {
  var synced = snapshot && snapshot.lastSync ? "synced " + ago(snapshot.lastSync, nowMs) : ""
  switch (status) {
  case "no-daemon": return "Helios service is not running"
  case "starting": return "Starting…"
  case "connecting": return "Connecting…"
  case "connected": return snapshot && snapshot.syncing ? "Syncing…" : ("Connected" + (synced ? " · " + synced : ""))
  case "idle": return "Waiting to sync" + (synced ? " · " + synced : "")
  case "offline": return "Out of range" + (synced ? " · " + synced : "")
  case "needs-auth": return "Strap key missing"
  case "auth-failed": return "Strap key rejected"
  }
  return status
}

// The latest PAI record's line under the total: its day's gain when that day
// is today, otherwise which day the total is from.
function paiNote(pai) {
  if (!pai) return ""
  if (pai.current === false) return "As of " + weekday(pai.ts)
  return "+" + number(pai.today, 1) + " today"
}

// One word for a POSIX shell, whatever the text holds: single-quoted, with
// each single quote inside closed, escaped and reopened.
function shellQuote(text) {
  return "'" + String(text).replace(/'/g, "'\\''") + "'"
}

// A heart rate as the widget will show and time itself by: a number a heart
// can beat at, or null. The range is the one the service keeps history to.
function pulse(bpm) {
  return typeof bpm === "number" && bpm >= 25 && bpm <= 230 ? Math.round(bpm) : null
}

function needsSetup(status) {
  return status === "no-daemon" || status === "needs-auth" || status === "auth-failed"
}

function batteryGlyph(level, charging) {
  if (charging) return "\u{F0084}"           // nf-md-battery_charging
  if (level === null || level === undefined || level < 0) return "\u{F0091}" // battery_unknown
  if (level >= 95) return "\u{F0079}"        // battery (full)
  if (level < 10) return "\u{F008E}"         // battery_outline
  // battery_10 … battery_90 are consecutive codepoints from F007A.
  return String.fromCodePoint(0xF007A + Math.min(8, Math.floor(level / 10) - 1))
}

// A time for a chart tick: on-the-hour 12-hour times drop ":00" ("6 AM"),
// and the narrow spaces some locales use become plain ones.
function hourLabel(text) {
  return String(text).replace(/[\u202f\u00a0]/g, " ").replace(/^(\d{1,2}):00 /, "$1 ")
}

// Hours to label under a 24-hour chart: every six hours, on the hour.
function dayTicks(start, end, format) {
  var ticks = []
  var d = new Date(start * 1000)
  d.setMinutes(0, 0, 0)
  do d.setHours(d.getHours() + 1); while (d.getHours() % 6 !== 0)
  // Six hours on by the clock, not 21,600 seconds: the two differ on the
  // days the clocks change, and the labels are clock times.
  for (; d.getTime() / 1000 < end - 3600; d.setHours(d.getHours() + 6)) {
    ticks.push({ at: d.getTime() / 1000, label: clock(d.getTime() / 1000, format) })
  }
  return ticks
}

var WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"]
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

// One slot per day, `count` of them ending today, filled from
// [dayStart, value] rows. Rows are matched by date, so a week or month that
// spans a clock change still lands each row on its own day.
function daySlots(rows, firstDay, count, valueIndex) {
  // A week or a month; never whatever number arrives.
  count = Math.max(1, Math.min(62, Math.floor(count) || 7))
  var slots = []
  var index = {}
  var d = new Date(firstDay * 1000)
  for (var i = 0; i < count; i++) {
    var day = new Date(d.getFullYear(), d.getMonth(), d.getDate() + i)
    index[day.getFullYear() + "-" + day.getMonth() + "-" + day.getDate()] = i
    slots.push({ day: day.getTime() / 1000, label: WEEKDAYS[day.getDay()], value: null })
  }
  for (var r = 0; r < (rows || []).length; r++) {
    var at = new Date(rows[r][0] * 1000)
    var slot = index[at.getFullYear() + "-" + at.getMonth() + "-" + at.getDate()]
    if (slot !== undefined) slots[slot].value = rows[r][valueIndex === undefined ? 1 : valueIndex]
  }
  return slots
}

// Seven slots ending today.
function weekSlots(rows, weekStart, valueIndex) {
  return daySlots(rows, weekStart, 7, valueIndex)
}

function slotAverage(slots) {
  var sum = 0, n = 0
  for (var i = 0; i < slots.length; i++) if (slots[i].value !== null) { sum += slots[i].value; n++ }
  return n ? sum / n : null
}

// "3 Nov": a day for the ends of a month of bars.
function dayLabel(ts) {
  var d = new Date(ts * 1000)
  return d.getDate() + " " + MONTHS[d.getMonth()]
}

function hasValues(slots) {
  for (var i = 0; i < slots.length; i++) if (slots[i].value !== null) return true
  return false
}

// ---- Recovery, naps and workouts ------------------------------------------------

// A night's reading beside the wearer's usual: "Usual 65", or nothing until
// there are enough nights to have a usual.
function usualNote(reading, digits) {
  if (!reading || reading.baseline === null || reading.baseline === undefined) return ""
  return "Usual " + number(reading.baseline, digits)
}

// Skin temperature against the usual night, signed: "+0.3° vs usual".
function temperatureNote(reading, fahrenheit) {
  if (!reading || reading.value === null || reading.value === undefined) return ""
  if (reading.baseline === null || reading.baseline === undefined) return ""
  var delta = (reading.value - reading.baseline) * (fahrenheit ? 9 / 5 : 1)
  var shown = Math.abs(delta).toFixed(1)
  if (shown === "0.0") return "As usual"
  return (delta > 0 ? "+" : "\u2212") + shown + "\u00b0 vs usual"
}

// "Tue 14:39 → 17:05 · 2h 27m"
function napLine(nap, format) {
  if (!nap) return ""
  return weekday(nap.start) + " " + clock(nap.start, format) + " \u2192 " + clock(nap.end, format)
    + " \u00b7 " + duration(nap.asleepMinutes)
}

function workoutTitle(w) {
  if (!w) return ""
  return w.name || "Workout"
}

// When a workout was and how long: "Tue 22:40 · 37m"
function workoutWhen(w, format) {
  if (!w) return ""
  return weekday(w.start) + " " + clock(w.start, format) + " \u00b7 " + duration(w.seconds / 60)
}

// The figures the strap filed for a workout, those it has: "127 avg · 149 max bpm · 298 kcal · load 45"
function workoutFigures(w) {
  if (!w) return ""
  var parts = []
  if (w.avgHr) parts.push(w.avgHr + " avg" + (w.maxHr ? "" : " bpm"))
  if (w.maxHr) parts.push(w.maxHr + " max bpm")
  if (w.calories) parts.push(grouped(w.calories) + " kcal")
  if (w.load) parts.push("load " + w.load)
  return parts.join(" \u00b7 ")
}

var HEART_ZONES = ["WARM-UP", "FAT BURN", "AEROBIC", "ANAEROBIC", "MAX"]

// Minutes in each of the five heart rate zones. The strap's list starts with
// the time below the first zone, which is not a zone.
function workoutZones(w) {
  var seconds = w && w.zones ? w.zones : []
  var zones = []
  for (var i = 0; i < HEART_ZONES.length; i++) zones.push(Math.round((seconds[i + 1] || 0) / 60))
  return zones
}

// The zones a workout spent time in, for a caption: "AEROBIC 31m · ANAEROBIC 4m"
function workoutZoneLine(w) {
  var zones = workoutZones(w)
  var parts = []
  for (var i = 0; i < zones.length; i++) if (zones[i] > 0) parts.push(HEART_ZONES[i] + " " + duration(zones[i]))
  return parts.join(" \u00b7 ")
}

// Whether the strap ran out of room for a night's stages and folded the rest
// into one unstaged span (it keeps 50 a night).
function hasUnstaged(stages) {
  for (var i = 0; i < (stages || []).length; i++) if (stages[i][2] === "unstaged") return true
  return false
}

// ---- What the pointer is over ---------------------------------------------------

// A heart rate bucket [time, mean, min, max] as a tip: the rate, then when
// and the range it moved through.
function heartTip(point, format) {
  if (!point) return { title: "", detail: "" }
  var range = point[2] !== point[3] ? " \u00b7 " + number(point[2]) + "\u2013" + number(point[3]) : ""
  return { title: number(point[1]) + " bpm", detail: clock(point[0], format) + range }
}

var STAGE_LABELS = { awake: "Awake", rem: "REM", light: "Light sleep", deep: "Deep sleep", unstaged: "Not staged" }

// A sleep stage [start, end, kind] as a tip: what it was, when, how long.
function stageTip(stage, format) {
  if (!stage) return { title: "", detail: "" }
  return {
    title: STAGE_LABELS[stage[2]] || String(stage[2]),
    detail: clock(stage[0], format) + " \u2192 " + clock(stage[1], format) + " \u00b7 " + duration((stage[1] - stage[0]) / 60)
  }
}

// The point of a time series nearest time `t`: its index in `points`
// ([time, ...] rows, oldest first), or -1 where the nearest is more than a
// bucket and a half away, which is a gap in the series. A point is drawn at
// its own time, so that is the time it is nearest by.
function nearestPoint(points, t, bucket) {
  if (!points || points.length === 0) return -1
  var lo = 0, hi = points.length - 1
  while (lo < hi) {
    var mid = (lo + hi) >> 1
    if (points[mid][0] < t) lo = mid + 1
    else hi = mid
  }
  if (lo > 0 && Math.abs(points[lo - 1][0] - t) <= Math.abs(points[lo][0] - t)) lo -= 1
  return Math.abs(points[lo][0] - t) <= bucket * 1.5 ? lo : -1
}

// How strongly the computer last heard the strap, for the panel's last line.
// Left out once the reading is too old to mean anything. At -85 dBm and
// below a link drops on a turn of the wrist.
function signalNote(signal, nowMs) {
  if (!signal || typeof signal.rssi !== "number" || nowMs / 1000 - signal.at > 1800) return ""
  return (signal.rssi <= -85 ? "weak signal " : "signal ") + "\u2212" + Math.abs(signal.rssi) + " dBm"
}
