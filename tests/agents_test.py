#!/usr/bin/python3
"""python3 tests/agents_test.py -- the Agents section beyond what tests/cockpit_test.py covers:
where a session works (project and branch), the chats that ended and how they are offered
back, and cockpit-agentd's session history, alerts and notifications. Sandboxes live under
$XDG_RUNTIME_DIR with $HOME pointed into them; nothing here notifies anyone, focuses a window
or resumes a chat."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.dont_write_bytecode = True   # never leave __pycache__ inside the plugin
sys.path.insert(0, str(BIN))
import plugin_safety as safe  # noqa: E402
import cockpit_common as common  # noqa: E402
import cockpit_agents as agents  # noqa: E402


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cockpit = load(BIN / "cockpit", "cockpit_runner")
agentd = load(BIN / "cockpit-agentd", "cockpit_agentd")
provider = load(ROOT / "providers" / "10-agents", "provider_agents")


def sid(n):
    return f"00000000-0000-4000-8000-{n:012x}"


A, B = sid(0xA1), sid(0xB2)   # clear of the small ids the tests number their histories with


def live(session_id, status, since, cwd="", name="demo", waiting_for="", needs="", pid=4242, kind="interactive"):
    """A session as cockpit_agents.read_sessions gives it."""
    return {"pid": pid, "procStart": "1", "status": status, "waitingFor": waiting_for, "needs": needs,
            "since": since, "name": name, "kind": kind, "sessionId": session_id, "cwd": cwd}


class Home(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cockpit-agents-test-", dir=safe.runtime_dir()))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(mock.patch.stopall)
        self.home = self.folder(self.root / "home")
        mock.patch.object(common, "HOME", str(self.home)).start()

    def folder(self, path):
        """A folder, every level of it private, as plugin_safety wants what it reads."""
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            current.mkdir(mode=0o700, exist_ok=True)
        return path

    def repo(self, relative, head="ref: refs/heads/main\n"):
        path = self.folder(self.home / relative)
        self.head(path, head)
        return path

    def head(self, project, content):
        self.folder(project / ".git")
        path = project / ".git" / "HEAD"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)


# ------------------------------------------------------------------ where a session works

class Where(Home):
    def test_project_and_branch_of_a_repo_and_of_a_folder_inside_it(self):
        repo = self.repo("Projects/api")
        inner = self.folder(repo / "web" / "pages")
        self.assertEqual(agents.branch_of(str(repo)), "main")
        self.assertEqual(agents.branch_of(str(inner)), "main")
        self.assertEqual(agents.project_of(str(repo)), "api")
        self.assertEqual(agents.project_of(str(self.home)), "~")
        for bad in ("", "relative/path", "/etc", str(self.home) + "/../x", None, 5):
            with self.subTest(bad=bad):
                self.assertEqual((agents.project_of(bad), agents.branch_of(bad)), ("", ""))

    def test_a_detached_head_and_refs_that_are_not_branches(self):
        repo = self.repo("a", head="0123456789abcdef0123456789abcdef01234567\n")
        self.assertEqual(agents.branch_of(str(repo)), "0123456")
        (repo / ".git" / "HEAD").write_text("ref: refs/remotes/origin/main\n")
        self.assertEqual(agents.branch_of(str(repo)), "")
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/" + "b" * 100 + "\n")
        self.assertEqual(agents.branch_of(str(repo)), "b" * agents.BRANCH_MAX)

    def test_a_worktree_and_a_submodule_follow_their_gitdir_file(self):
        main = self.repo("main")
        worktree_git = self.folder(main / ".git" / "worktrees" / "wt")
        (worktree_git / "HEAD").write_text("ref: refs/heads/feature/login\n")
        worktree = self.folder(self.home / "wt")
        (worktree / ".git").write_text(f"gitdir: {worktree_git}\n")
        self.assertEqual(agents.branch_of(str(worktree)), "feature/login")
        module_git = self.folder(main / ".git" / "modules" / "lib")
        (module_git / "HEAD").write_text("ref: refs/heads/vendored\n")
        submodule = self.folder(main / "lib")
        (submodule / ".git").write_text("gitdir: ../.git/modules/lib\n")
        self.assertEqual(agents.branch_of(str(submodule)), "vendored")

    def test_nothing_is_looked_for_above_home(self):
        self.head(self.root, "ref: refs/heads/above\n")
        self.assertEqual(agents.branch_of(str(self.folder(self.home / "plain"))), "")

    def test_odd_repositories_give_no_branch(self):
        outside = self.folder(self.root / "outside")
        (outside / "HEAD").write_text("ref: refs/heads/escaped\n")
        real = self.repo("real")
        cases = {
            "gitdir outside home": lambda p: (p / ".git").write_text(f"gitdir: {outside}\n"),
            "gitdir climbing out": lambda p: (p / ".git").write_text("gitdir: ../../outside\n"),
            "a .git file that names nothing": lambda p: (p / ".git").write_text("hello\n"),
            "huge HEAD": lambda p: self.head(p, "ref: refs/heads/" + "x" * 5000),
            "control character": lambda p: self.head(p, "ref: refs/heads/ma" + chr(27) + "in\n"),
            "two lines": lambda p: self.head(p, "ref: refs/heads/main\nref: refs/heads/other\n"),
            "forbidden ref characters": lambda p: self.head(p, "ref: refs/heads/a~1\n"),
            "not UTF-8": lambda p: self.head(p, bytes([0xFF, 0xFE, 0x0A])),
            "symlinked git folder": lambda p: (p / ".git").symlink_to(real / ".git"),
            "symlinked HEAD": lambda p: (self.folder(p / ".git") / "HEAD").symlink_to(real / ".git" / "HEAD"),
            "HEAD is a folder": lambda p: self.folder(p / ".git" / "HEAD"),
        }
        for label, make in cases.items():
            with self.subTest(label):
                project = self.folder(self.home / label.replace(" ", "-").replace(".", ""))
                make(project)
                self.assertEqual(agents.branch_of(str(project)), "")


# ------------------------------------------------------------------ the chats that ended

class Ended(Home):
    def setUp(self):
        super().setUp()
        self.sessions = self.folder(self.root / "claude-sessions")
        mock.patch.object(agents, "CLAUDE_SESSIONS", self.sessions).start()
        mock.patch.object(provider, "SEEN", self.root / "agent-titles.json").start()
        mock.patch.object(provider, "LIVE", self.root / "agent-state.json").start()
        mock.patch.object(common, "hypr_clients", lambda: []).start()
        self.project = self.repo("Projects/api")
        self.now = time.time()

    def rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            provider.main()
        return json.loads(out.getvalue())["rows"]

    def entry(self, ended_ago, cwd=None, title="Fix the login form", name="login"):
        return {"cwd": str(cwd or self.project), "title": title, "name": name, "startedAt": self.now - 7200,
                "lastSeen": self.now - (ended_ago or 0), "endedAt": self.now - ended_ago if ended_ago else 0.0}

    def test_chats_that_ended_today_are_offered_back_newest_first(self):
        history = {sid(i): self.entry(60 * (i + 1)) for i in range(10)}
        history[sid(20)] = self.entry(25 * 3600)                      # over a day ago
        history[sid(21)] = self.entry(30, cwd="/etc")                 # not in your home
        history[sid(22)] = self.entry(30, cwd=self.home / "gone")     # its folder is gone
        history[sid(23)] = self.entry(None)                           # never seen ending
        history["not-a-session-id"] = self.entry(30)
        agents.save_history(history)
        rows = self.rows()
        self.assertEqual([r["dismiss"]["key"] for r in rows], [sid(i) for i in range(provider.ENDED_MAX)])
        resume = {"kind": "resume-agent", "session": sid(0), "cwd": str(self.project)}
        self.assertEqual(rows[0], {
            "glyph": provider.ENDED_GLYPH, "title": "Fix the login form", "detail": "ended 1m ago · api (main)",
            "state": "idle", "action": resume,
            "buttons": [{"glyph": provider.RESUME_GLYPH, "label": "Resume this chat in a terminal", "action": resume},
                        {"glyph": provider.FOLDER_GLYPH, "label": "Open the project folder",
                         "action": {"kind": "open-folder", "path": str(self.project)}}],
            "dismiss": {"key": sid(0), "marker": str(int(history[sid(0)]["endedAt"]))}})
        # The runner takes every one of them as it is: nothing dropped, nothing reshaped.
        doc, dropped = cockpit.sanitize({"rows": rows}, "10-agents")
        self.assertEqual((doc["rows"], dropped), (rows, 0))

    def test_an_ended_chat_goes_by_its_task_then_its_name_then_its_project(self):
        agents.save_history({sid(1): self.entry(60, title="", name="login"), sid(2): self.entry(120, title="", name="")})
        self.assertEqual([r["title"] for r in self.rows()], ["login", "api"])

    def test_a_chat_that_ended_over_a_day_ago_is_not_offered(self):
        agents.save_history({sid(1): self.entry(provider.ENDED_WITHIN + 5)})
        self.assertEqual(self.rows(), [])

    def test_a_running_chat_is_not_also_offered_back_and_comes_first(self):
        sleeper = subprocess.Popen(["/usr/bin/sleep", "60"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        (self.sessions / f"{sleeper.pid}.json").write_text(json.dumps({
            "pid": sleeper.pid, "procStart": common.proc_start(sleeper.pid), "status": "busy", "sessionId": sid(1),
            "cwd": str(self.project), "kind": "interactive", "name": "",
            "statusUpdatedAt": int((time.time() - 125) * 1000)}))
        agents.save_history({sid(1): self.entry(600), sid(2): self.entry(60)})
        rows = self.rows()
        self.assertEqual([(r["glyph"], r["title"]) for r in rows],
                         [(provider.STATUS_GLYPHS["busy"], "api"), (provider.ENDED_GLYPH, "Fix the login form")])
        self.assertEqual(rows[0]["detail"], "working for 2m · api (main) · no window")
        self.assertEqual(rows[1]["dismiss"]["key"], sid(2))


# ------------------------------------------------------------------ history

class History(Home):
    def entry(self, seen, **fields):
        entry = {"cwd": str(self.home), "title": "t", "name": "n", "startedAt": 1.0, "lastSeen": seen, "endedAt": 0.0}
        entry.update(fields)
        return entry

    def test_saved_privately_and_loaded_within_bounds(self):
        agents.save_history({sid(i): self.entry(1000.0 + i, title="t" * 300, name="n" * 100) for i in range(60)})
        path = agents.history_path()
        self.assertEqual(path, self.home / ".local" / "state" / "omarchy-cockpit" / "agent-history.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        loaded = agents.load_history()
        self.assertEqual(set(loaded), {sid(i) for i in range(10, 60)})
        self.assertEqual((len(loaded[sid(59)]["title"]), len(loaded[sid(59)]["name"])), (256, 64))

    def test_what_does_not_fit_is_dropped_or_clamped(self):
        path = agents.history_path()
        safe.ensure_dir(path.parent)
        path.write_text(json.dumps({"sessions": {
            sid(1): {"cwd": "/etc", "title": 7, "name": 5, "startedAt": "soon", "lastSeen": 1e300, "endedAt": -4},
            "../../etc": {"cwd": str(self.home)},
            sid(2): "not an entry"}}))
        self.assertEqual(agents.load_history(), {sid(1): {"cwd": "", "title": "", "name": "", "startedAt": 0.0,
                                                           "lastSeen": agents.TIME_MAX, "endedAt": 0.0}})
        for broken in ("{" * 5000, json.dumps({"sessions": []}), json.dumps([1, 2]),
                       json.dumps({"sessions": {sid(3): {"title": ["too", "deep"]}}}),
                       " " * (agents.HISTORY_FILE_MAX + 1)):
            path.write_text(broken)
            self.assertEqual(agents.load_history(), {})

    def test_a_symlinked_history_is_neither_read_nor_written_through(self):
        path = agents.history_path()
        safe.ensure_dir(path.parent)
        victim = self.root / "victim.json"
        victim.write_text(json.dumps({"sessions": {sid(1): self.entry(5.0)}}))
        path.symlink_to(victim)
        self.assertEqual(agents.load_history(), {})
        with self.assertRaises(safe.UnsafeError):
            agents.save_history({sid(2): self.entry(6.0)})
        self.assertIn(sid(1), victim.read_text())


# ------------------------------------------------------------------ alert decisions

class Alerts(unittest.TestCase):
    def setUp(self):
        self.last = {}

    def step(self, previous, sessions, now, focused=None, windows=None):
        return agentd.track(previous, sessions, now, focused, self.last, windows or {})

    @staticmethod
    def kinds(alerts):
        return [(kind, session["sessionId"]) for kind, session in alerts]

    def test_whatever_is_happening_at_start_is_not_news(self):
        alerts, statuses = self.step(None, [live(A, "waiting", 100), live(B, "idle", 90)], 105)
        self.assertEqual(alerts, [])
        self.assertEqual(statuses, {A: {"status": "waiting", "turnSince": 100}, B: {"status": "idle", "turnSince": 0.0}})

    def test_needs_you_once_per_wait_and_not_again_within_half_a_minute(self):
        _, s = self.step(None, [live(A, "busy", 100)], 100)
        alerts, s = self.step(s, [live(A, "waiting", 110, waiting_for="input needed")], 110)
        self.assertEqual(self.kinds(alerts), [("needs", A)])
        alerts, s = self.step(s, [live(A, "waiting", 110)], 112)
        self.assertEqual(alerts, [])
        _, s = self.step(s, [live(A, "busy", 114)], 114)
        alerts, s = self.step(s, [live(A, "waiting", 120)], 120)
        self.assertEqual(alerts, [], "a second question within 30 s")
        _, s = self.step(s, [live(A, "busy", 130)], 130)
        alerts, s = self.step(s, [live(A, "waiting", 141)], 141)
        self.assertEqual(self.kinds(alerts), [("needs", A)])

    def test_finished_only_after_a_turn_of_a_minute_or_more(self):
        _, s = self.step(None, [live(A, "idle", 900), live(B, "idle", 900)], 1000)
        _, s = self.step(s, [live(A, "busy", 1000), live(B, "busy", 1000)], 1002)
        alerts, s = self.step(s, [live(A, "waiting", 1020), live(B, "busy", 1000)], 1020)
        self.assertEqual(self.kinds(alerts), [("needs", A)])
        alerts, s = self.step(s, [live(A, "busy", 1030), live(B, "idle", 1030)], 1030)
        self.assertEqual(alerts, [], "B worked for half a minute")
        _, s = self.step(s, [live(A, "shell", 1050), live(B, "idle", 1030)], 1050)
        alerts, s = self.step(s, [live(A, "idle", 1065), live(B, "idle", 1030)], 1066)
        self.assertEqual(self.kinds(alerts), [("finished", A)], "A's turn, questions included, ran 65 s")

    def test_a_session_that_appears_waiting_is_news_but_one_that_appears_idle_is_not(self):
        _, s = self.step(None, [], 100)
        alerts, s = self.step(s, [live(A, "waiting", 101), live(B, "idle", 101)], 102)
        self.assertEqual(self.kinds(alerts), [("needs", A)])

    def test_nothing_about_the_session_you_are_looking_at(self):
        windows = {A: "0x55AA"}
        _, s = self.step(None, [live(A, "busy", 0)], 0, focused="0x55aa", windows=windows)
        alerts, s = self.step(s, [live(A, "waiting", 5)], 5, focused="0x55aa", windows=windows)
        self.assertEqual(alerts, [])
        _, s = self.step(s, [live(A, "busy", 10)], 10, focused="0x77bb", windows=windows)
        alerts, s = self.step(s, [live(A, "waiting", 15)], 15, focused="0x77bb", windows=windows)
        self.assertEqual(self.kinds(alerts), [("needs", A)], "an alert held back starts no cooldown")

    def test_a_session_without_an_id_raises_nothing(self):
        _, s = self.step(None, [live("", "busy", 0)], 0)
        alerts, s = self.step(s, [live("", "waiting", 5)], 5)
        self.assertEqual((alerts, s), ([], {}))


# ------------------------------------------------------------------ the daemon's session watch

class Watch(Home):
    def setUp(self):
        super().setUp()
        self.live, self.mapped, self.sent = [], [], []
        self.writes = 0
        self.tracker = agentd.Tracker()
        self.notifier = mock.Mock()
        self.notifier.send.side_effect = lambda title, body, session: self.sent.append(
            (title, body, session["sessionId"]))
        real_save = agents.save_history

        def counting_save(history, path=None):
            self.writes += 1
            real_save(history, path)

        mock.patch.object(agents, "save_history", counting_save).start()
        self.project = self.repo("Projects/api")

    def watch(self, history=None):
        return agentd.SessionWatch(self.tracker, self.notifier, {} if history is None else history, 0.0,
                                   read_sessions=lambda: list(self.live), map_windows=self.map_windows)

    def map_windows(self, sessions):
        self.mapped.append(sorted(s["sessionId"] for s in sessions))
        return {s["sessionId"]: {"address": "0x" + s["sessionId"][-4:], "title": "✳ Refactor the parser"}
                for s in sessions}

    def test_history_is_written_when_a_chat_starts_or_ends_and_rarely_otherwise(self):
        w = self.watch()
        self.live = [live(A, "busy", 1.0, cwd=str(self.project), name="parser")]
        w.poll(0.0)
        self.assertEqual(self.writes, 1)
        self.assertEqual(agents.load_history()[A], {"cwd": str(self.project), "title": "Refactor the parser",
                                                    "name": "parser", "startedAt": 0.0, "lastSeen": 0.0, "endedAt": 0.0})
        w.poll(2.0)
        self.assertEqual(self.writes, 1, "nothing changed")
        self.tracker.windows["0x" + A[-4:]] = {"title": "◐ Write the tests", "changedAt": 3.0, "changes": 1}
        w.poll(4.0)
        self.assertEqual((self.writes, w.history[A]["title"]), (1, "Write the tests"))
        w.poll(agentd.HISTORY_TITLES_EVERY + 1)
        self.assertEqual(self.writes, 2, "a changed task is written within five minutes")
        self.live = []
        w.poll(400.0)
        self.assertEqual(self.writes, 3)
        self.assertEqual(agents.load_history()[A]["endedAt"], 400.0)
        self.assertEqual(self.mapped, [[A]], "windows are matched when a session appears, not on every reading")

    def test_windows_are_matched_again_when_one_opens_or_closes(self):
        w = self.watch()
        self.live = [live(A, "busy", 1.0)]
        w.poll(0.0)
        w.poll(2.0)
        self.tracker.handle(b"openwindow>>55cc,1,foot,x", 3.0)
        w.poll(4.0)
        self.live.append(live(B, "idle", 1.0))
        w.poll(6.0)
        self.assertEqual(self.mapped, [[A], [A], [A, B]])

    def test_a_chat_that_ended_unwatched_ended_when_last_seen_and_can_come_back(self):
        history = {A: {"cwd": str(self.project), "title": "t", "name": "n", "startedAt": 10.0, "lastSeen": 50.0,
                       "endedAt": 0.0}}
        w = self.watch(history)
        w.poll(500.0)
        self.assertEqual((w.history[A]["endedAt"], self.writes), (50.0, 1))
        self.live = [live(A, "busy", 501.0, cwd=str(self.project))]
        w.poll(502.0)
        self.assertEqual((w.history[A]["endedAt"], w.history[A]["startedAt"], self.writes), (0.0, 502.0, 2))
        self.live = []
        w.poll(504.0)
        self.assertEqual(w.history[A]["endedAt"], 504.0)

    def test_at_most_fifty_chats_are_remembered(self):
        history = {sid(i): {"cwd": "", "title": "", "name": "", "startedAt": 0.0, "lastSeen": float(i),
                            "endedAt": float(i)} for i in range(1, 51)}
        w = self.watch(history)
        self.live = [live(A, "busy", 1.0)]
        w.poll(100.0)
        self.assertEqual(len(w.history), agents.HISTORY_MAX)
        self.assertNotIn(sid(1), w.history)
        self.assertIn(A, w.history)

    def test_alerts_say_where_and_what_and_are_quiet_at_start(self):
        w = self.watch()
        self.live = [live(A, "waiting", 1.0, cwd=str(self.project), waiting_for="input needed")]
        w.poll(2.0)
        self.assertEqual(self.sent, [])
        self.live = [live(A, "busy", 3.0, cwd=str(self.project))]
        w.poll(4.0)
        self.live = [live(A, "waiting", 5.0, cwd=str(self.project), waiting_for="input needed")]
        w.poll(40.0)
        self.live = [live(A, "busy", 41.0, cwd=str(self.project))]
        w.poll(42.0)
        self.live = [live(A, "idle", 110.0, cwd=str(self.project))]
        w.poll(111.0)
        self.assertEqual(self.sent, [("Claude needs you", "api: question", A),
                                     ("Claude finished", "api: Refactor the parser", A)])


# ------------------------------------------------------------------ notifications

class Notifications(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.calls = []
        self.release = threading.Event()
        self.answer = b"focus\n"
        mock.patch.object(safe, "run", self.fake_run).start()

    def fake_run(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append((argv, kw))
        if argv[0] == "timeout":
            self.release.wait(10)
            return safe.Result(0, self.answer, b"", False, False)
        return safe.Result(0, b"ok", b"", False, False)

    def settle(self, notifier):
        deadline = time.monotonic() + 10
        while notifier.waiting and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(notifier.waiting, 0)

    def test_switched_off_nothing_runs(self):
        notifier = agentd.Notifier(enabled=False)
        self.assertFalse(notifier.send("Claude needs you", "api: question", {"sessionId": A}))
        self.assertEqual(self.calls, [])

    def test_at_most_four_wait_for_a_click_and_show_focuses_the_window(self):
        notifier = agentd.Notifier(resolve=lambda session: "0x55aa")
        sent = [notifier.send("Claude needs you", f"api: question {i}", {"sessionId": A}) for i in range(6)]
        self.assertEqual(sent, [True] * agentd.NOTIFY_MAX + [False] * 2)
        self.release.set()
        self.settle(notifier)
        shown = [argv for argv, _ in self.calls if argv[0] == "timeout"]
        self.assertEqual(len(shown), agentd.NOTIFY_MAX)
        self.assertEqual(shown[0][:4], ["timeout", "--kill-after=2", str(agentd.NOTIFY_WAIT), safe.tool("notify-send")])
        self.assertEqual(shown[0][4:9], ["--app-name=Cockpit", "-A", "focus=Show", "--", "Claude needs you"])
        focused = [argv for argv, _ in self.calls if argv[0] == "hyprctl"]
        self.assertEqual(focused, [common.focus_window_argv("0x55aa")] * agentd.NOTIFY_MAX)
        self.assertTrue(notifier.send("Claude finished", "api: done", {"sessionId": A}), "room again once answered")
        self.settle(notifier)

    def test_a_notification_closed_without_show_focuses_nothing(self):
        self.answer = b""
        self.release.set()
        notifier = agentd.Notifier(resolve=lambda session: "0x55aa")
        notifier.send("Claude finished", "api: done", {"sessionId": A})
        self.settle(notifier)
        self.assertEqual([argv[0] for argv, _ in self.calls], ["timeout"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
