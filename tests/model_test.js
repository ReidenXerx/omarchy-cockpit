// node tests/model_test.js -- CockpitModel.js display logic. The file is loaded the way
// QML sees it; only the QML-specific ".pragma library" line is dropped.
"use strict"
const fs = require("fs")
const path = require("path")
const assert = require("assert")

const source = fs.readFileSync(path.join(__dirname, "..", "CockpitModel.js"), "utf8")
  .replace(/^\.pragma library\s*$/m, "")
const M = new Function(source +
  "\nreturn { parse, flatten, liveCount, needsCount, summary, brokenNotes, launchArgv, actionJson, keepsOpen }")()

const LAUNCHER = "/usr/bin/omarchy-launch-floating-terminal-with-presentation"
const UWSM = "/usr/bin/uwsm-app"
const SESSION = "79ed7d8a-876b-4ab1-9897-96d9fcd27110"
const ESC = String.fromCharCode(27)
let failures = 0
function test(name, fn) {
  try { fn(); console.log("ok   " + name) } catch (e) { failures++; console.log("FAIL " + name + "\n     " + e.message) }
}

test("parse rejects what is not a runner document", () => {
  for (const raw of ["", "nope", "[]", "null", "42", undefined, "x".repeat(5 * 1024 * 1024)]) {
    const parsed = M.parse(raw)
    assert.strictEqual(parsed.ok, false)
    assert.deepStrictEqual(parsed.sections, [])
  }
})

test("parse caps sections and notes", () => {
  const sections = Array.from({ length: 100 }, (_, i) => ({ section: "s" + i, rows: [{ title: "t" }] }))
  sections.push("not a section")
  const parsed = M.parse(JSON.stringify({ sections, notes: { git: "e".repeat(1000), weird: { x: 1 } } }))
  assert.strictEqual(parsed.ok, true)
  assert.strictEqual(parsed.sections.length, 64)
  assert.strictEqual(parsed.notes.git.length, 300)
  assert.strictEqual(parsed.notes.weird, "")
})

test("flatten collapses, expands and bounds rows", () => {
  const rows = Array.from({ length: 250 }, (_, i) => ({ title: "r" + i, state: "busy" }))
  const sections = [{ section: "Git", glyph: "g", rows }]
  const closed = M.flatten(sections, 6, {})
  assert.strictEqual(closed.length, 1 + 6 + 1)
  assert.deepStrictEqual(closed[7], { kind: "more", section: "Git", hidden: 194 })
  const open = M.flatten(sections, 6, { Git: true })
  assert.strictEqual(open.length, 1 + 200)
  assert.strictEqual(open[0].count, 200)
})

test("flatten keeps structured actions and drops shell strings", () => {
  const action = { kind: "focus-window", address: "0x55aa" }
  const out = M.flatten([{ section: "A", rows: [
    { title: "structured", action },
    { title: "legacy", action: "hyprctl dispatch focuswindow address:0x1" },
    { title: { evil: true }, detail: 7, progress: 250, state: 42 },
    "not a row",
  ] }], 10, {})
  assert.strictEqual(out[1].action, JSON.stringify(action))
  assert.strictEqual(out[1].actionKind, "focus-window")
  assert.strictEqual(out[2].action, "")
  assert.strictEqual(out[2].actionKind, "")
  assert.strictEqual(out[3].title, "")
  assert.strictEqual(out[3].detail, "7")
  assert.strictEqual(out[3].progress, 100)
  assert.strictEqual(out[4].action, "")
  assert.deepStrictEqual(out[4].buttons, [])
  assert.strictEqual(M.actionJson({ kind: "run", argv: ["x".repeat(9000)] }), "")
})

test("flatten passes the needs state through", () => {
  const out = M.flatten([{ section: "Agents", rows: [{ title: "a", state: "needs" }] }], 6, {})
  assert.strictEqual(out[1].state, "needs")
})

test("flatten carries a row's buttons with their actions and what to ask first", () => {
  const stop = { kind: "stop-process", pid: 4121, start: "8812734" }
  const folder = { kind: "open-folder", path: "/home/me/api" }
  const buttons = [
    { glyph: "s", label: "Stop node", confirm: "Stop node (pid 4121)?", action: stop },
    { glyph: "f", label: "l".repeat(80), action: folder },
    { glyph: "x", label: "a shell string", action: "kill 4121" },
    "not a button",
    { glyph: "d", label: "Dismiss", action: { kind: "dismiss", provider: "45-ports", key: "k", marker: "" } },
    { glyph: "1", action: folder }, { glyph: "2", action: folder },
  ]
  const out = M.flatten([{ section: "Ports", rows: [{ title: "node", buttons }] }], 6, {})
  const got = out[1].buttons
  assert.strictEqual(got.length, 4)
  assert.deepStrictEqual(got[0], { glyph: "s", label: "Stop node", confirm: "Stop node (pid 4121)?",
                                   action: JSON.stringify(stop), kind: "stop-process" })
  assert.strictEqual(got[1].label.length, 48)
  assert.strictEqual(got[1].confirm, "")
  assert.strictEqual(got[2].kind, "dismiss")
  assert.strictEqual(got[3].glyph, "1")
})

test("a section whose rows are all dismissed keeps its heading and a way back", () => {
  const out = M.flatten([{ section: "Git", provider: "40-git", dismissed: 3, rows: [] },
                         { section: "Empty", provider: "x", rows: [] },
                         { section: "Odd", provider: "../x", dismissed: 2.7, rows: [{ title: "r" }] },
                         { section: "Text", provider: "y", dismissed: "4", rows: [] }], 6, {})
  assert.strictEqual(out.length, 3)
  assert.strictEqual(out[0].kind, "header")
  assert.strictEqual(out[0].count, 0)
  assert.strictEqual(out[0].dismissed, 3)
  assert.strictEqual(out[0].undismiss, JSON.stringify({ kind: "undismiss", provider: "40-git" }))
  assert.strictEqual(out[1].dismissed, 2)
  assert.strictEqual(out[1].undismiss, "")
  assert.strictEqual(out[2].title, "r")
})

test("dismissing, bringing back and stopping keep the hub open; the rest close it", () => {
  for (const kind of ["dismiss", "undismiss", "stop-process"]) assert.strictEqual(M.keepsOpen(kind), true, kind)
  for (const kind of ["focus-window", "terminal", "run", "open-url", "open-folder", "resume-agent", "", undefined])
    assert.strictEqual(M.keepsOpen(kind), false, String(kind))
})

test("liveCount, needsCount, summary and brokenNotes", () => {
  const sections = [{ section: "Agents", rows: [{ state: "busy" }, { state: "idle" }, null] },
                    { section: "Git", rows: [{ state: "warn" }] }]
  assert.strictEqual(M.liveCount(sections), 1)
  assert.strictEqual(M.needsCount(sections), 0)
  assert.strictEqual(M.summary(sections), "3 agents · 1 git")
  assert.strictEqual(M.summary([]), "Nothing in flight")
  assert.deepStrictEqual(M.brokenNotes({ a: "ok", b: "cached", c: "timed out after 6s" }), ["c: timed out after 6s"])
})

test("an agent waiting for you is counted apart from work in motion and leads the summary", () => {
  const one = [{ section: "Agents", rows: [{ state: "needs" }, { state: "busy" }] },
               { section: "Git", rows: [{ state: "warn" }] }]
  assert.strictEqual(M.liveCount(one), 1)
  assert.strictEqual(M.needsCount(one), 1)
  assert.strictEqual(M.summary(one), "1 needs you · 2 agents · 1 git")
  const two = [{ section: "Agents", rows: [{ state: "needs" }, { state: "needs" }, "junk", { state: "NEEDS" }] }]
  assert.strictEqual(M.needsCount(two), 2)
  assert.strictEqual(M.summary(two), "2 need you · 4 agents")
})

test("launchArgv accepts the presentation terminal with one command string", () => {
  assert.deepStrictEqual(M.launchArgv(JSON.stringify({ exec: [LAUNCHER, "/usr/bin/checkupdates"] }) + "\n", LAUNCHER),
                         [LAUNCHER, "/usr/bin/checkupdates"])
  for (const raw of ["", "garbage", JSON.stringify({ exec: ["/usr/bin/bash", "-c"] }),
                     JSON.stringify({ exec: [LAUNCHER] }),
                     JSON.stringify({ exec: [LAUNCHER, "a", "b"] }),
                     JSON.stringify({ exec: [LAUNCHER, 5] }),
                     JSON.stringify({ exec: [LAUNCHER, ""] }),
                     JSON.stringify({ exec: [LAUNCHER, "x".repeat(20000)] })]) {
    assert.strictEqual(M.launchArgv(raw, LAUNCHER), null, raw.slice(0, 60))
  }
})

test("launchArgv accepts a local address or a folder for xdg-open, and a resumed chat", () => {
  const open = (target) => JSON.stringify({ exec: [UWSM, "--", "/usr/bin/xdg-open", target] })
  const resume = (dir, claude, session, extra) => JSON.stringify({ exec: [UWSM, "--", "/usr/bin/xdg-terminal-exec",
    "--dir=" + dir, "--", claude, "--resume", session].concat(extra || []) })
  for (const target of ["http://127.0.0.1:5173/", "http://localhost:8080", "http://[::1]:3000/docs/v1", "/home/me/src/api"])
    assert.deepStrictEqual(M.launchArgv(open(target), LAUNCHER), [UWSM, "--", "/usr/bin/xdg-open", target], target)
  const good = resume("/home/me/src/api", "/home/me/.local/bin/claude", SESSION)
  assert.deepStrictEqual(M.launchArgv(good, LAUNCHER), JSON.parse(good).exec)
  for (const raw of [
    open("https://example.org/"), open("http://127.0.0.1:80/?q=1"), open("file:///etc/passwd"), open("relative/dir"),
    open("/home/me/../etc"), open("/home/me/" + ESC + "]0;x"), open("/home/me/"), open("-n"),
    JSON.stringify({ exec: ["/usr/bin/env", "--", "/usr/bin/xdg-open", "/home/me"] }),
    JSON.stringify({ exec: [UWSM, "/usr/bin/xdg-open", "/home/me", "x"] }),
    resume("relative", "/home/me/.local/bin/claude", SESSION),
    resume("/home/me/src/api", "/usr/bin/bash", SESSION),
    resume("/home/me/src/api", "/home/me/.local/bin/claude", "--help"),
    resume("/home/me/src/api", "/home/me/.local/bin/claude", SESSION, ["--dangerously-skip-permissions"]),
    JSON.stringify({ exec: [UWSM, "--", "/usr/bin/xdg-terminal-exec", "--dir=/home/me", "--", "/home/me/.local/bin/claude", "--continue", SESSION] }),
  ]) {
    assert.strictEqual(M.launchArgv(raw, LAUNCHER), null, raw.slice(0, 120))
  }
})

process.exit(failures ? 1 : 0)
