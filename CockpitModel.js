.pragma library

// Pure display logic for the cockpit. No QML, no I/O.

var DEFAULT_CAP = 6
var RAW_MAX = 4 * 1024 * 1024     // the runner caps its document at 2 MB
var SECTIONS_MAX = 64
var ROWS_MAX = 200
var TEXT_MAX = 1024
var ACTION_MAX = 8 * 1024
var LAUNCH_MAX = 16 * 1024

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function text(value, limit) {
  if (typeof value !== "string" && typeof value !== "number") return ""
  return String(value).slice(0, limit)
}

function rowsOf(section) {
  return isObject(section) && Array.isArray(section.rows) ? section.rows.slice(0, ROWS_MAX) : []
}

// ok is false when the runner's output was unusable (killed by the watchdog, say), so
// the panel can keep what it last showed instead of blanking.
function parse(raw) {
  var empty = { ok: false, sections: [], notes: {} }
  if (typeof raw !== "string" || raw.length > RAW_MAX) return empty
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { return empty }
  if (!isObject(doc)) return empty
  var sections = Array.isArray(doc.sections) ? doc.sections.slice(0, SECTIONS_MAX).filter(isObject) : []
  var notes = {}
  if (isObject(doc.notes)) {
    var names = Object.keys(doc.notes).slice(0, SECTIONS_MAX)
    for (var i = 0; i < names.length; i++) notes[text(names[i], 64)] = text(doc.notes[names[i]], 300)
  }
  return { ok: true, sections: sections, notes: notes }
}

// Actions are structured objects the runner has already validated; the panel hands them
// to `cockpit action` over stdin unchanged, and that helper validates them again. Anything
// else -- an old shell-string action -- is simply not clickable.
function actionJson(action) {
  if (!isObject(action)) return ""
  var s = JSON.stringify(action)
  return s.length <= ACTION_MAX ? s : ""
}

// A section can legitimately return dozens of rows -- sixteen dirty repos is normal --
// and a hub you have to scroll is not a glance. Cap each section and say what was cut.
function flatten(sections, cap, expanded) {
  var limit = cap === undefined ? DEFAULT_CAP : cap
  var out = []
  for (var i = 0; i < sections.length; i++) {
    var s = sections[i]
    var rows = rowsOf(s)
    if (rows.length === 0) continue
    var name = text(s.section, 64)
    var open = !!(expanded && expanded[name])
    out.push({ kind: "header", section: name, glyph: text(s.glyph, 16),
               count: rows.length, expanded: open })
    var shown = open ? rows.length : Math.min(rows.length, limit)
    for (var j = 0; j < shown; j++) {
      var r = isObject(rows[j]) ? rows[j] : {}
      out.push({
        kind: "row",
        section: name,
        glyph: text(r.glyph, 16),
        title: text(r.title, TEXT_MAX),
        detail: text(r.detail, TEXT_MAX),
        state: text(r.state, 8) || "idle",
        progress: typeof r.progress === "number" ? Math.max(0, Math.min(100, r.progress)) : -1,
        action: actionJson(r.action)
      })
    }
    if (rows.length > shown) {
      out.push({ kind: "more", section: name, hidden: rows.length - shown })
    }
  }
  return out
}

// What the bar glyph should say without opening anything: the number of things actually
// in motion. Dirty repos and pending updates are not "in motion" -- they are true all
// day and a permanent badge is a badge you stop seeing.
function liveCount(sections) {
  var n = 0
  for (var i = 0; i < sections.length; i++) {
    var rows = rowsOf(sections[i])
    for (var j = 0; j < rows.length; j++) {
      if (isObject(rows[j]) && rows[j].state === "busy") n++
    }
  }
  return n
}

function summary(sections) {
  var parts = []
  for (var i = 0; i < sections.length; i++) {
    var rows = rowsOf(sections[i])
    if (rows.length) parts.push(rows.length + " " + text(sections[i].section, 64).toLowerCase())
  }
  return parts.length ? parts.join(" · ") : "Nothing in flight"
}

function brokenNotes(notes) {
  var out = []
  for (var name in notes) {
    var note = notes[name]
    if (note !== "ok" && note !== "cached") out.push(name + ": " + note)
  }
  return out
}

// `cockpit action` answers a terminal action with the argv to start detached (a terminal
// must outlive the helper). Only that exact shape, for that exact program, is accepted.
function terminalLaunch(raw, launcher) {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > LAUNCH_MAX) return null
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { return null }
  if (!isObject(doc) || !Array.isArray(doc.exec) || doc.exec.length !== 2) return null
  if (doc.exec[0] !== launcher || typeof doc.exec[1] !== "string" || doc.exec[1].length === 0) return null
  return [doc.exec[0], doc.exec[1]]
}
