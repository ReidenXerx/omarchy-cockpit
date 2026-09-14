#!/usr/bin/python3
"""python3 tests/hub_test.py -- what 1.3 added to the runner: row buttons, rows you dismiss until
they change, and the new actions (open a local address or a folder, stop a process, resume a
Claude Code chat). Sandboxes under $XDG_RUNTIME_DIR. Nothing here opens a browser, a folder or a
terminal, and the only process stopped is one this test started."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.dont_write_bytecode = True   # never leave __pycache__ inside the plugin
sys.path.insert(0, str(BIN))
import plugin_safety as safe  # noqa: E402
import cockpit_common as common  # noqa: E402


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cockpit = load(BIN / "cockpit", "cockpit_runner_hub")
git_provider = load(ROOT / "providers" / "40-git", "provider_git_hub")

SESSION = "0b4ed805-06b8-4dd7-bf77-103b0ede0142"
GIT_TEST_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "PATH": "/usr/bin", "HOME": os.environ.get("HOME", "/")}


def git(repo, *args):
    subprocess.run(["/usr/bin/git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, env=GIT_TEST_ENV)


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cockpit-hub-test-", dir=safe.runtime_dir()))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(mock.patch.stopall)
        self.store = self.root / "state" / "dismissed.json"
        mock.patch.object(cockpit, "DISMISSED", self.store).start()
        mock.patch.object(cockpit, "CACHE", self.root / "cache").start()


# ------------------------------------------------------------------ buttons and dismissing

class Dismissals(Sandbox):
    def setUp(self):
        super().setUp()
        self.builtin = self.root / "builtin"
        self.builtin.mkdir()
        mock.patch.object(cockpit, "BUILTIN_DIR", self.builtin).start()
        mock.patch.object(cockpit, "BUILTIN_PROVIDERS", {}).start()
        mock.patch.object(cockpit, "USER_DIR", self.root / "user").start()

    def provider(self, rows, name="40-fake", **doc):
        cockpit.BUILTIN_PROVIDERS[name] = 6
        path = self.builtin / name
        path.write_text("#!/usr/bin/python3\nimport json\nprint(json.dumps(%r))\n" % dict(doc, section="Fake", rows=rows))
        os.chmod(path, 0o644)

    def section(self):
        sections, notes = cockpit.collect(force=True)
        self.assertEqual(len(sections), 1, notes)
        return sections[0][1], notes

    def click(self, action):
        self.assertIsNone(cockpit.perform(common.validate_action(action)))

    def test_a_dismissed_row_stays_hidden_until_its_marker_changes(self):
        repo = {"title": "repo", "dismiss": {"key": "/home/x/repo", "marker": "m1"}}
        self.provider([repo, {"title": "plain"}])
        doc, _ = self.section()
        self.assertEqual((doc["provider"], doc["dismissed"]), ("40-fake", 0))
        first = doc["rows"][0]
        self.assertNotIn("dismiss", first)
        button = first["buttons"][-1]
        self.assertEqual(button["glyph"], cockpit.DISMISS_GLYPH)
        self.assertEqual(button["action"], {"kind": "dismiss", "provider": "40-fake", "key": "/home/x/repo", "marker": "m1"})
        self.assertNotIn("buttons", doc["rows"][1])

        self.click(button["action"])
        doc, _ = self.section()
        self.assertEqual([r["title"] for r in doc["rows"]], ["plain"])
        self.assertEqual(doc["dismissed"], 1)
        doc, _ = self.section()                     # and again: nothing forgets it
        self.assertEqual(doc["dismissed"], 1)

        self.provider([dict(repo, dismiss={"key": "/home/x/repo", "marker": "m2"}), {"title": "plain"}])
        doc, _ = self.section()
        self.assertEqual([r["title"] for r in doc["rows"]], ["repo", "plain"])
        self.assertEqual(doc["rows"][0]["buttons"][-1]["action"]["marker"], "m2")
        self.assertEqual(json.loads(self.store.read_text()), {}, "a dismissal that no longer matches is forgotten")

    def test_a_section_with_every_row_dismissed_keeps_its_count_and_undismiss_brings_them_back(self):
        rows = [{"title": f"r{i}", "dismiss": {"key": f"k{i}", "marker": ""}} for i in range(3)]
        self.provider(rows)
        for i in range(3):
            self.click({"kind": "dismiss", "provider": "40-fake", "key": f"k{i}", "marker": ""})
        doc, _ = self.section()
        self.assertEqual((doc["rows"], doc["dismissed"]), ([], 3))
        self.click({"kind": "undismiss", "provider": "40-fake"})
        doc, _ = self.section()
        self.assertEqual((len(doc["rows"]), doc["dismissed"]), (3, 0))

    def test_providers_cannot_dismiss_rows_themselves_and_buttons_are_checked(self):
        dismiss = {"kind": "dismiss", "provider": "40-fake", "key": "k", "marker": ""}
        folder = {"kind": "open-folder", "path": common.HOME}
        self.provider([
            {"title": "sneaky", "action": dismiss},
            {"title": "buttons", "buttons": [
                {"glyph": "x" * 40, "label": "l" * 100, "confirm": "Sure?\x1b[2J" + "c" * 300, "action": folder},
                {"label": "shell", "action": "rm -rf ~"},
                {"label": "undismiss", "action": {"kind": "undismiss", "provider": "40-fake"}},
                {"label": "fourth", "action": folder}]},
            {"title": "bad marker", "dismiss": {"key": "k", "marker": "\x07"}},
            {"title": "bad key", "dismiss": {"key": "", "marker": ""}},
        ])
        doc, notes = self.section()
        sneaky, buttons, bad_marker, bad_key = doc["rows"]
        self.assertNotIn("action", sneaky)
        self.assertEqual(len(buttons["buttons"]), 1, "three considered, two refused, the fourth never looked at")
        kept = buttons["buttons"][0]
        self.assertEqual((len(kept["glyph"]), len(kept["label"]), len(kept["confirm"])), (16, 48, 160))
        self.assertTrue(kept["confirm"].startswith("Sure?�[2J"))
        self.assertNotIn("buttons", bad_marker)
        self.assertNotIn("buttons", bad_key)
        self.assertIn("ignored 3 row actions", notes["40-fake"])

    def test_the_store_is_validated_bounded_and_never_followed(self):
        self.store.parent.mkdir()
        self.store.write_text(json.dumps({
            "40-git": {"/home/x/a": {"marker": "m", "at": 5}, "": {"marker": "m"}, "/home/x/b": {"marker": 7},
                       "/home/x/c": "nope"},
            "../evil": {"k": {"marker": "m", "at": 1}},
            "45-ports": {f"k{i}": {"marker": "", "at": i} for i in range(cockpit.DISMISSED_PER_PROVIDER + 40)}}))
        store = cockpit.load_dismissed()
        self.assertEqual(store["40-git"], {"/home/x/a": {"marker": "m", "at": 5.0}})
        self.assertNotIn("../evil", store)
        self.assertEqual(len(store["45-ports"]), cockpit.DISMISSED_PER_PROVIDER)
        self.store.write_text("{" * 10000)
        self.assertEqual(cockpit.load_dismissed(), {})

        self.store.unlink()
        victim = self.root / "victim"
        victim.write_text("keep")
        self.store.symlink_to(victim)
        self.assertEqual(cockpit.load_dismissed(), {})
        with self.assertRaises(safe.UnsafeError):
            cockpit.dismiss("40-git", "/home/x/a", "m")
        self.assertEqual(victim.read_text(), "keep")

    def test_old_dismissals_are_forgotten(self):
        now = time.time()
        cockpit.save_dismissed({"40-git": {"old": {"marker": "", "at": now - cockpit.DISMISSED_KEEP_SEC - 1},
                                           "new": {"marker": "", "at": now}}}, now=now)
        self.assertEqual(list(cockpit.load_dismissed()["40-git"]), ["new"])


class States(Sandbox):
    def test_a_row_that_needs_you_keeps_its_state(self):
        # Until 1.3 the runner knew only ok/busy/warn/idle, so every "needs you" row reached the panel as idle:
        # no urgent dot, no tint.
        doc, _ = cockpit.sanitize({"rows": [{"title": "a", "state": "needs"}, {"title": "b", "state": "NEEDS"},
                                            {"title": "c", "state": "busy"}]}, "10-agents")
        self.assertEqual([r["state"] for r in doc["rows"]], ["needs", "idle", "busy"])


# ------------------------------------------------------------------ the new actions

class NewActions(Sandbox):
    GOOD = [
        {"kind": "open-url", "url": "http://127.0.0.1:5173/"},
        {"kind": "open-url", "url": "http://localhost:8080"},
        {"kind": "open-url", "url": "http://[::1]:65535/docs/v1.2/index.html"},
        {"kind": "open-folder", "path": common.HOME + "/Projects/api"},
        {"kind": "stop-process", "pid": 4121, "start": "8812734"},
        {"kind": "resume-agent", "session": SESSION, "cwd": common.HOME + "/Projects/api"},
        {"kind": "dismiss", "provider": "40-git", "key": common.HOME + "/Projects/api", "marker": "3f2a"},
        {"kind": "dismiss", "provider": "10-agents", "key": SESSION, "marker": ""},
        {"kind": "undismiss", "provider": "40-git"},
    ]
    BAD = [
        {"kind": "open-url", "url": "https://127.0.0.1:5173/"},
        {"kind": "open-url", "url": "http://example.org:80/"},
        {"kind": "open-url", "url": "http://127.0.0.1/"},
        {"kind": "open-url", "url": "http://127.0.0.1:0/"},
        {"kind": "open-url", "url": "http://127.0.0.1:65536/"},
        {"kind": "open-url", "url": "http://127.0.0.1:80/?next=javascript:x"},
        {"kind": "open-url", "url": "http://127.0.0.1:80@evil.org/"},
        {"kind": "open-url", "url": "file:///etc/passwd"},
        {"kind": "open-url", "url": ["http://127.0.0.1:80/"]},
        {"kind": "open-folder", "path": "/etc"},
        {"kind": "open-folder", "path": common.HOME + "/../etc"},
        {"kind": "open-folder", "path": "Projects"},
        {"kind": "open-folder", "path": common.HOME, "argv": []},
        {"kind": "stop-process", "pid": 1, "start": "1"},
        {"kind": "stop-process", "pid": True, "start": "1"},
        {"kind": "stop-process", "pid": "4121", "start": "1"},
        {"kind": "stop-process", "pid": 4121, "start": 1},
        {"kind": "stop-process", "pid": 4121, "start": "-1"},
        {"kind": "stop-process", "pid": 4121},
        {"kind": "resume-agent", "session": "--dangerously-skip-permissions", "cwd": common.HOME},
        {"kind": "resume-agent", "session": SESSION.upper(), "cwd": common.HOME},
        {"kind": "resume-agent", "session": SESSION, "cwd": "/tmp"},
        {"kind": "resume-agent", "session": SESSION, "cwd": common.HOME + "/a\nb"},
        {"kind": "dismiss", "provider": "../40-git", "key": "k", "marker": ""},
        {"kind": "dismiss", "provider": "40-git", "key": "", "marker": ""},
        {"kind": "dismiss", "provider": "40-git", "key": "k" * 257, "marker": ""},
        {"kind": "dismiss", "provider": "40-git", "key": "k", "marker": "m" * 129},
        {"kind": "dismiss", "provider": "40-git", "key": "k"},
        {"kind": "undismiss", "provider": "40-git", "key": "k"},
    ]

    def test_allow_list(self):
        for action in self.GOOD:
            with self.subTest(action=action):
                self.assertEqual(common.validate_action(action), action)
        for action in self.BAD:
            with self.subTest(action=action):
                with self.assertRaises(common.ActionError):
                    common.validate_action(action)

    def test_a_local_address_or_a_folder_is_handed_to_the_panel_through_uwsm(self):
        mock.patch.object(common, "HOME", str(self.root)).start()
        folder = self.root / "api"
        folder.mkdir()
        opener = [safe.tool("uwsm-app"), "--", safe.tool("xdg-open")]
        self.assertEqual(cockpit.perform(common.validate_action({"kind": "open-url", "url": "http://127.0.0.1:5173/"})),
                         {"exec": opener + ["http://127.0.0.1:5173/"]})
        self.assertEqual(cockpit.perform(common.validate_action({"kind": "open-folder", "path": str(folder)})),
                         {"exec": opener + [str(folder)]})
        with self.assertRaises(common.ActionError):
            cockpit.perform(common.validate_action({"kind": "open-folder", "path": str(self.root / "gone")}))
        (self.root / "link").symlink_to(folder)
        with self.assertRaises(common.ActionError):
            cockpit.perform(common.validate_action({"kind": "open-folder", "path": str(self.root / "link")}))

    def test_stopping_a_process_checks_that_it_is_still_the_same_one_and_yours(self):
        sleeper = subprocess.Popen(["/usr/bin/sleep", "60"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        start = common.proc_start(sleeper.pid)
        self.assertIsNotNone(start)
        with self.assertRaises(common.ActionError):
            cockpit.stop_process(sleeper.pid, str(int(start) + 1))
        self.assertTrue(alive(sleeper.pid))
        with self.assertRaises((common.ActionError, OSError)):
            cockpit.stop_process(1, common.proc_start(1) or "1")
        with self.assertRaises(common.ActionError):
            cockpit.stop_process(os.getppid(), common.proc_start(os.getppid()))
        with self.assertRaises(common.ActionError):
            cockpit.stop_process(4194303, "1")

        (self.root / "cache").mkdir()
        for name in ("45-ports", "30-jobs", "40-git"):
            (self.root / "cache" / f"{name}.json").write_text("{}")
        cockpit.perform(common.validate_action({"kind": "stop-process", "pid": sleeper.pid, "start": start}))
        self.assertEqual(sleeper.wait(timeout=5), -15)
        self.assertEqual(sorted(p.name for p in (self.root / "cache").iterdir()), ["40-git.json"])

    def resume_fixture(self):
        mock.patch.object(common, "HOME", str(self.root)).start()
        claude = self.root / "bin" / "claude"
        claude.parent.mkdir()
        claude.write_text("#!/bin/sh\n")
        os.chmod(claude, 0o700)
        mock.patch.object(cockpit, "CLAUDE_PROGRAMS", (claude,)).start()
        projects = self.root / "projects"
        mock.patch.object(cockpit, "CLAUDE_PROJECTS", projects).start()
        cwd = self.root / "work.dir" / "api"
        cwd.mkdir(parents=True)
        folder = projects / re.sub(r"[^A-Za-z0-9-]", "-", str(cwd))
        folder.mkdir(parents=True)
        transcript = folder / f"{SESSION}.jsonl"
        transcript.write_text("{}\n")
        return claude, cwd, transcript

    def resume(self, cwd):
        return cockpit.perform(common.validate_action({"kind": "resume-agent", "session": SESSION, "cwd": str(cwd)}))

    def test_resuming_a_chat_opens_claude_in_its_folder(self):
        claude, cwd, transcript = self.resume_fixture()
        self.assertEqual(self.resume(cwd), {"exec": [
            safe.tool("uwsm-app"), "--", safe.tool("xdg-terminal-exec"), f"--dir={cwd}", "--",
            str(claude), "--resume", SESSION]})
        elsewhere = transcript.parent.parent / "renamed-project"
        elsewhere.mkdir()
        transcript.rename(elsewhere / transcript.name)
        self.assertEqual(self.resume(cwd)["exec"][-1], SESSION, "found in another project folder")

    def test_resuming_refuses_a_missing_chat_folder_or_an_untrusted_claude(self):
        claude, cwd, transcript = self.resume_fixture()
        transcript.unlink()
        with self.assertRaisesRegex(common.ActionError, "no longer on disk"):
            self.resume(cwd)
        transcript.write_text("{}\n")
        os.chmod(claude, 0o777)
        with mock.patch.object(safe, "has_tool", lambda name: False):
            with self.assertRaisesRegex(common.ActionError, "not found"):
                self.resume(cwd)
        os.chmod(claude, 0o700)
        shutil.rmtree(cwd)
        with self.assertRaisesRegex(common.ActionError, "folder is gone"):
            self.resume(cwd)

    def test_a_failed_click_that_nothing_would_show_becomes_a_notification(self):
        told = []
        mock.patch.object(safe, "spawn", lambda argv, **kw: told.append(argv)).start()
        code = cockpit.action_main(io.BytesIO(b'{"kind": "stop-process", "pid": 4194303, "start": "1"}\n'))
        self.assertEqual(code, 2)
        self.assertEqual(told, [["notify-send", "--app-name=Cockpit", "--", "Cockpit", "that process has already exited"]])
        told.clear()
        self.store.parent.mkdir()
        (self.root / "victim").write_text("keep")
        self.store.symlink_to(self.root / "victim")
        code = cockpit.action_main(io.BytesIO(b'{"kind": "dismiss", "provider": "40-git", "key": "k", "marker": ""}\n'))
        self.assertEqual((code, told), (2, []))


# ------------------------------------------------------------------ git rows

class GitMarker(Sandbox):
    def setUp(self):
        super().setUp()
        mock.patch.object(common, "HOME", str(self.root)).start()
        mock.patch.object(git_provider, "HOME", str(self.root)).start()
        roots = self.root / "git-roots"
        roots.write_text(str(self.root / "proj") + "\n")
        mock.patch.object(git_provider, "ROOTS_FILE", roots).start()
        mock.patch.object(git_provider, "DIRTY_WITHIN_DAYS_FILE", self.root / "no-such-file").start()
        self.repo = self.root / "proj" / "api"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main", ".")
        (self.repo / "f.txt").write_text("hello\n")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-qm", "init")

    def rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            git_provider.main()
        return json.loads(out.getvalue())["rows"]

    def marker(self):
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["dismiss"]["marker"]

    def test_a_repo_row_can_be_dismissed_and_any_new_work_brings_it_back(self):
        (self.repo / "f.txt").write_text("changed\n")
        row = self.rows()[0]
        self.assertEqual(row["dismiss"]["key"], str(self.repo))
        self.assertEqual(row["buttons"], [{"glyph": git_provider.FOLDER_GLYPH, "label": "Open the folder",
                                           "action": {"kind": "open-folder", "path": str(self.repo)}}])
        first = row["dismiss"]["marker"]
        self.assertEqual(self.marker(), first, "nothing changed, nothing to bring back")
        stamp = time.time() + 5
        os.utime(self.repo / "f.txt", (stamp, stamp))
        edited = self.marker()
        self.assertNotEqual(edited, first)
        (self.repo / "new.txt").write_text("x\n")
        self.assertNotEqual(self.marker(), edited)
        doc, _ = cockpit.sanitize({"rows": self.rows()}, "40-git")
        self.assertIn("dismiss", doc["rows"][0])
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "work")
        self.assertEqual(self.rows(), [], "clean and nothing unpushed: no row at all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
