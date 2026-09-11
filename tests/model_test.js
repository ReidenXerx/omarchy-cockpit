// node tests/model_test.js -- CockpitModel.js display logic. The file is loaded the way
// QML sees it; only the QML-specific ".pragma library" line is dropped.
"use strict"
const fs = require("fs")
const path = require("path")
const assert = require("assert")

const source = fs.readFileSync(path.join(__dirname, "..", "CockpitModel.js"), "utf8")
  .replace(/^\.pragma library\s*$/m, "")
const M = new Function(source +
  "\nreturn { parse, flatten, liveCount, needsCount, summary, brokenNotes, terminalLaunch, actionJson }")()

const LAUNCHER = "/usr/bin/omarchy-launch-floating-terminal-with-presentation"
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
  assert.strictEqual(out[2].action, "")
  assert.strictEqual(out[3].title, "")
  assert.strictEqual(out[3].detail, "7")
  assert.strictEqual(out[3].progress, 100)
  assert.strictEqual(out[4].action, "")
  assert.strictEqual(M.actionJson({ kind: "run", argv: ["x".repeat(9000)] }), "")
})

test("flatten passes the needs state through", () => {
  const out = M.flatten([{ section: "Agents", rows: [{ title: "a", state: "needs" }] }], 6, {})
  assert.strictEqual(out[1].state, "needs")
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

test("terminalLaunch accepts only the wrapper with one command string", () => {
  assert.deepStrictEqual(M.terminalLaunch(JSON.stringify({ exec: [LAUNCHER, "/usr/bin/checkupdates"] }) + "\n", LAUNCHER),
                         [LAUNCHER, "/usr/bin/checkupdates"])
  for (const raw of ["", "garbage", JSON.stringify({ exec: ["/usr/bin/bash", "-c"] }),
                     JSON.stringify({ exec: [LAUNCHER] }),
                     JSON.stringify({ exec: [LAUNCHER, "a", "b"] }),
                     JSON.stringify({ exec: [LAUNCHER, 5] }),
                     JSON.stringify({ exec: [LAUNCHER, ""] }),
                     JSON.stringify({ exec: [LAUNCHER, "x".repeat(20000)] })]) {
    assert.strictEqual(M.terminalLaunch(raw, LAUNCHER), null, raw.slice(0, 60))
  }
})

process.exit(failures ? 1 : 0)
