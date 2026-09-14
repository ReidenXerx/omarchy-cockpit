.pragma library

// Pure display logic for the cockpit. No QML, no I/O.

var DEFAULT_CAP = 6
var RAW_MAX = 4 * 1024 * 1024     // the runner caps its document at 2 MB
var SECTIONS_MAX = 64
var ROWS_MAX = 200
var TEXT_MAX = 1024
var ACTION_MAX = 8 * 1024
var LAUNCH_MAX = 16 * 1024
var BUTTONS_MAX = 4               // three from the provider, and the runner's Dismiss
var LABEL_MAX = 48
var CONFIRM_MAX = 160
var DISMISSED_MAX = 9999

var UWSM_APP = "/usr/bin/uwsm-app"
var XDG_OPEN = "/usr/bin/xdg-open"
var TERMINAL_EXEC = "/usr/bin/xdg-terminal-exec"
var PROVIDER_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
var SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
var LOCAL_URL = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):[1-9][0-9]{0,4}(\/[A-Za-z0-9._~\/-]*)?$/
// Clicks that change what the hub shows keep it open and refresh it; the others take you somewhere else.
var KEEP_OPEN = ["dismiss", "undismiss", "stop-process"]

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

function actionKind(action) {
  return isObject(action) && typeof action.kind === "string" ? action.kind.slice(0, 32) : ""
}

function keepsOpen(kind) {
  return KEEP_OPEN.indexOf(kind) >= 0
}

// A row's buttons as the panel draws them: each with its action ready to send and, when the
// provider wants one, the question to ask before acting.
function buttonsOf(row) {
  var out = []
  var list = isObject(row) && Array.isArray(row.buttons) ? row.buttons : []
  for (var i = 0; i < list.length && out.length < BUTTONS_MAX; i++) {
    var b = list[i]
    var json = isObject(b) ? actionJson(b.action) : ""
    if (!json) continue
    out.push({ glyph: text(b.glyph, 16), label: text(b.label, LABEL_MAX), confirm: text(b.confirm, CONFIRM_MAX),
               action: json, kind: actionKind(b.action) })
  }
  return out
}

// A section can legitimately return dozens of rows -- sixteen dirty repos is normal --
// and a hub you have to scroll is not a glance. Cap each section and say what was cut.
function flatten(sections, cap, expanded) {
  var limit = cap === undefined ? DEFAULT_CAP : cap
  var out = []
  for (var i = 0; i < sections.length; i++) {
    var s = sections[i]
    var rows = rowsOf(s)
    var dismissed = isObject(s) && typeof s.dismissed === "number" && s.dismissed >= 1
      ? Math.min(Math.floor(s.dismissed), DISMISSED_MAX) : 0
    // A section whose every row was dismissed keeps its heading: the count is the way back.
    if (rows.length === 0 && dismissed === 0) continue
    var name = text(s.section, 64)
    var provider = text(s.provider, 64)
    var open = !!(expanded && expanded[name])
    out.push({ kind: "header", section: name, glyph: text(s.glyph, 16), count: rows.length, expanded: open,
               dismissed: dismissed,
               undismiss: dismissed && PROVIDER_NAME.test(provider) ? actionJson({ kind: "undismiss", provider: provider }) : "" })
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
        action: actionJson(r.action),
        actionKind: actionKind(r.action),
        buttons: buttonsOf(r)
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

// Rows that need you now: an agent asking a question or holding a permission prompt. Unlike
// "warn", which marks things that are true all day, this is short-lived and urgent, so it
// is what turns the bar dot urgent and leads the tooltip.
function needsCount(sections) {
  var n = 0
  for (var i = 0; i < sections.length; i++) {
    var rows = rowsOf(sections[i])
    for (var j = 0; j < rows.length; j++) {
      if (isObject(rows[j]) && rows[j].state === "needs") n++
    }
  }
  return n
}

function summary(sections) {
  var parts = []
  var needs = needsCount(sections)
  if (needs) parts.push(needs + (needs === 1 ? " needs you" : " need you"))
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

// An absolute path with no control characters and no "." or ".." or empty segments.
function plainPath(value) {
  if (typeof value !== "string" || value.length < 2 || value.length > 4096 || value.charAt(0) !== "/") return false
  for (var i = 0; i < value.length; i++) {
    var c = value.charCodeAt(i)
    if (c < 32 || (c >= 127 && c < 160)) return false
  }
  return value.split("/").every(function (part, k) { return k === 0 || (part !== "" && part !== "." && part !== "..") })
}

// `cockpit action` answers an action that opens something with the argv to start detached,
// because what it opens has to outlive the helper. Only these shapes, for these programs:
//   the presentation terminal with one command line                            (terminal)
//   uwsm-app -- xdg-open <a local address or a folder>                         (open-url, open-folder)
//   uwsm-app -- xdg-terminal-exec --dir=<folder> -- <claude> --resume <session>  (resume-agent)
function launchArgv(raw, launcher) {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > LAUNCH_MAX) return null
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { return null }
  if (!isObject(doc) || !Array.isArray(doc.exec)) return null
  var a = doc.exec
  for (var i = 0; i < a.length; i++) {
    if (typeof a[i] !== "string" || a[i].length === 0) return null
  }
  if (a.length === 2 && a[0] === launcher) return [a[0], a[1]]
  if (a.length === 4 && a[0] === UWSM_APP && a[1] === "--" && a[2] === XDG_OPEN && (LOCAL_URL.test(a[3]) || plainPath(a[3])))
    return a.slice()
  if (a.length === 8 && a[0] === UWSM_APP && a[1] === "--" && a[2] === TERMINAL_EXEC && a[3].indexOf("--dir=") === 0
      && plainPath(a[3].slice(6)) && a[4] === "--" && plainPath(a[5]) && /\/claude$/.test(a[5])
      && a[6] === "--resume" && SESSION_ID.test(a[7]))
    return a.slice()
  return null
}
