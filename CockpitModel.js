.pragma library

// Pure display logic for the cockpit. No QML, no I/O.

var DEFAULT_CAP = 6

function parse(raw) {
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { return { sections: [], notes: {} } }
  if (!doc || typeof doc !== "object") return { sections: [], notes: {} }
  var sections = Array.isArray(doc.sections) ? doc.sections : []
  return { sections: sections, notes: doc.notes || {} }
}

// A section can legitimately return dozens of rows -- sixteen dirty repos is normal --
// and a hub you have to scroll is not a glance. Cap each section and say what was cut.
function flatten(sections, cap, expanded) {
  var limit = cap === undefined ? DEFAULT_CAP : cap
  var out = []
  for (var i = 0; i < sections.length; i++) {
    var s = sections[i]
    var rows = Array.isArray(s.rows) ? s.rows : []
    if (rows.length === 0) continue
    var open = !!(expanded && expanded[s.section])
    out.push({ kind: "header", section: s.section, glyph: s.glyph || "",
               count: rows.length, expanded: open })
    var shown = open ? rows.length : Math.min(rows.length, limit)
    for (var j = 0; j < shown; j++) {
      var r = rows[j]
      out.push({
        kind: "row",
        section: s.section,
        glyph: String(r.glyph || ""),
        title: String(r.title || ""),
        detail: String(r.detail || ""),
        state: String(r.state || "idle"),
        progress: typeof r.progress === "number" ? Math.max(0, Math.min(100, r.progress)) : -1,
        action: String(r.action || "")
      })
    }
    if (rows.length > shown) {
      out.push({ kind: "more", section: s.section, hidden: rows.length - shown })
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
    var s = sections[i]
    var rows = Array.isArray(s.rows) ? s.rows : []
    for (var j = 0; j < rows.length; j++) {
      if (rows[j].state === "busy") n++
    }
  }
  return n
}

function summary(sections) {
  var parts = []
  for (var i = 0; i < sections.length; i++) {
    var s = sections[i]
    var rows = Array.isArray(s.rows) ? s.rows : []
    if (rows.length) parts.push(rows.length + " " + String(s.section).toLowerCase())
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
