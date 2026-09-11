#!/usr/bin/python3
"""python3 tests/cockpit_test.py -- the runner, row actions, the event daemon and the
providers, exercised against real files, processes, sockets and git repositories in
sandboxes under $XDG_RUNTIME_DIR. Nothing here launches a terminal, focuses a window,
mounts anything or touches the live cockpit's state."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
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


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cockpit = load(BIN / "cockpit", "cockpit_runner")
agentd = load(BIN / "cockpit-agentd", "cockpit_agentd")
git_provider = load(ROOT / "providers" / "40-git", "provider_git")
updates = load(ROOT / "providers" / "50-updates", "provider_updates")
tailscale = load(ROOT / "providers" / "70-tailscale", "provider_tailscale")
disk = load(ROOT / "providers" / "80-disk", "provider_disk")

GIT_TEST_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "PATH": "/usr/bin", "HOME": os.environ.get("HOME", "/")}


def git(repo, *args):
    subprocess.run(["/usr/bin/git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, env=GIT_TEST_ENV)


def make_repo(path, files=("f.txt",)):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main", ".")
    for name in files:
        (path / name).write_text("hello\n")
    git(path, "add", *files)
    git(path, "commit", "-qm", "init")
    return path


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cockpit-test-", dir=safe.runtime_dir()))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(mock.patch.stopall)

    def script(self, path, body, mode=0o700):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/python3\n" + textwrap.dedent(body))
        os.chmod(path, mode)
        return path


# ------------------------------------------------------------------ runner

class Runner(Sandbox):
    def setUp(self):
        super().setUp()
        self.builtin = self.root / "builtin"
        self.builtin.mkdir()
        self.user = self.root / "user"
        mock.patch.object(cockpit, "BUILTIN_DIR", self.builtin).start()
        mock.patch.object(cockpit, "BUILTIN_PROVIDERS", {}).start()
        mock.patch.object(cockpit, "USER_DIR", self.user).start()
        mock.patch.object(cockpit, "CACHE", self.root / "cache").start()

    def builtin_script(self, name, body, timeout=6):
        cockpit.BUILTIN_PROVIDERS[name] = timeout
        return self.script(self.builtin / name, body, mode=0o644)

    def builtin_provider(self, name, doc, timeout=6):
        return self.builtin_script(name, f"import json\nprint(json.dumps({doc!r}))\n", timeout)

    def test_output_is_sanitised(self):
        rows = [{"title": f"row {i}", "state": "busy"} for i in range(250)]
        rows[0] = {"glyph": "x" * 40, "title": "evil\x1b]0;pwned\x07", "detail": 5, "state": "hacked",
                   "progress": 500, "action": "rm -rf ~", "extra": "dropped"}
        rows[1] = {"title": "ok", "action": {"kind": "focus-window", "address": "0x55aa"}}
        rows[2] = {"title": "bad", "action": {"kind": "run", "argv": ["sh", "-c", "id"]}}
        self.builtin_provider("05-fake", {"section": "Fake", "rows": rows, "timeoutSec": 999, "intervalSec": -3})
        sections, notes = cockpit.collect(force=True)
        doc = sections[0][1]
        self.assertEqual(len(doc["rows"]), 200)
        first = doc["rows"][0]
        self.assertEqual(first, {"glyph": "x" * 16, "title": "evil�]0;pwned�", "detail": "5",
                                 "state": "idle", "progress": 100})
        self.assertEqual(doc["rows"][1]["action"], {"kind": "focus-window", "address": "0x55aa"})
        self.assertNotIn("action", doc["rows"][2])
        self.assertEqual((doc["timeoutSec"], doc["intervalSec"]), (30, 1))
        self.assertIn("ignored 2 row actions", notes["05-fake"])

    def test_cache_is_revalidated(self):
        self.builtin_provider("05-fake", {"intervalSec": 3600, "rows": [{"title": "live"}]})
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "05-fake.json").write_text(json.dumps(
            {"at": time.time(), "doc": {"intervalSec": 3600, "rows": [{"title": "cached", "action": "sh -c id"}]}}))
        sections, notes = cockpit.collect()
        self.assertEqual(notes["05-fake"], "cached")
        self.assertEqual(sections[0][1]["rows"], [{"glyph": "", "title": "cached", "detail": "", "state": "idle"}])

    def test_cache_symlink_is_not_followed(self):
        self.builtin_provider("05-fake", {"rows": [{"title": "live"}]})
        cache = self.root / "cache"
        cache.mkdir()
        victim = self.root / "victim"
        victim.write_text("keep")
        (cache / "05-fake.json").symlink_to(victim)
        sections, notes = cockpit.collect()
        self.assertEqual(victim.read_text(), "keep")
        self.assertTrue(notes["05-fake"].startswith("cache not written"), notes)
        self.assertEqual(sections[0][1]["rows"][0]["title"], "live")

    def test_output_ceiling(self):
        self.builtin_script("05-big", "import sys\nsys.stdout.write('x' * 400000)\n")
        _, notes = cockpit.collect(force=True)
        self.assertEqual(notes["05-big"], "output exceeded 256 KB")

    def test_string_and_json_limits(self):
        self.builtin_provider("05-long", {"rows": [{"title": "y" * 2000}]})
        self.builtin_provider("05-deep", {"rows": [{"title": [[[[[[[[["x"]]]]]]]]]}]})
        self.builtin_script("05-text", "print('hello')\n")
        self.builtin_script("05-exit", "import sys\nsys.stderr.write('boom\\nlast words\\n')\nsys.exit(3)\n")
        self.builtin_provider("05-norows", {"section": "x"})
        _, notes = cockpit.collect(force=True)
        self.assertTrue(notes["05-long"].startswith("output exceeds limits"), notes)
        self.assertTrue(notes["05-deep"].startswith("output exceeds limits"), notes)
        self.assertEqual(notes["05-text"], "output was not JSON")
        self.assertEqual(notes["05-exit"], "exit 3: last words")
        self.assertEqual(notes["05-norows"], "missing rows")

    def test_timeout_kills_the_provider_and_its_children(self):
        pidfile = self.root / "child.pid"
        self.builtin_script("05-slow", f"""
            import subprocess, time
            child = subprocess.Popen(["/usr/bin/sleep", "60"])
            open({str(pidfile)!r}, "w").write(str(child.pid))
            time.sleep(60)
        """, timeout=1)
        start = time.monotonic()
        _, notes = cockpit.collect(force=True)
        self.assertLess(time.monotonic() - start, 6)
        self.assertEqual(notes["05-slow"], "timed out after 1s")
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child))

    def test_declared_budget_is_clamped(self):
        provider = cockpit.Provider("x", ["/bin/true"], "user", "x", None)
        self.assertEqual(cockpit.budget_for(provider, {"timeoutSec": 30}), 30)
        self.assertEqual(cockpit.budget_for(provider, None), cockpit.TIMEOUT_DEFAULT)
        doc, _ = cockpit.sanitize({"rows": [], "timeoutSec": 1e9}, "x")
        self.assertEqual(cockpit.budget_for(provider, doc), 30)
        doc, _ = cockpit.sanitize({"rows": [], "timeoutSec": float("nan")}, "x")
        self.assertEqual(cockpit.budget_for(provider, doc), cockpit.TIMEOUT_DEFAULT)

    def test_user_provider_trust(self):
        self.builtin_provider("05-shadowed", {"section": "Builtin", "rows": [{"title": "builtin"}]})
        self.script(self.user / "05-shadowed",
                    'import json\nprint(json.dumps({"section": "Mine", "rows": [{"title": "mine"}]}))\n')
        self.script(self.user / "group-writable", "print('{\"rows\": [{\"title\": \"no\"}]}')\n", mode=0o770)
        elsewhere = self.script(self.root / "elsewhere", "print('{\"rows\": [{\"title\": \"no\"}]}')\n")
        (self.user / "linked").symlink_to(elsewhere)
        (self.user / "README").write_text("not a provider")
        self.script(self.user / "bad name", "print('{}')\n")
        sections, notes = cockpit.collect(force=True)
        self.assertEqual([d["section"] for _, d in sections], ["Mine"])
        self.assertIn("not a regular, non-writable file we own", notes["group-writable"])
        self.assertEqual(notes["linked"], "skipped: is a symlink")
        self.assertIn("letters, digits", notes["bad name"])
        self.assertNotIn("README", notes)
        self.assertEqual(cockpit.discover()["05-shadowed"].argv, [str(self.user / "05-shadowed")])

    def test_symlinked_user_directory_is_refused(self):
        real = self.root / "real"
        self.script(real / "p", "print('{\"rows\": [{\"title\": \"x\"}]}')\n")
        self.user.symlink_to(real)
        sections, notes = cockpit.collect(force=True)
        self.assertEqual(sections, [])
        self.assertTrue(notes["user-providers"].startswith("skipped"), notes)

    def test_user_provider_cap(self):
        for i in range(cockpit.USER_PROVIDERS_MAX + 3):
            self.script(self.user / f"p{i:02d}", "print('{\"rows\": []}')\n")
        found = cockpit.discover()
        self.assertEqual(sum(1 for p in found.values() if p.argv), cockpit.USER_PROVIDERS_MAX)
        self.assertEqual(sum(1 for p in found.values() if p.problem), 3)

    def test_document_budget_drops_whole_sections(self):
        big = {"section": "Big", "rows": [{"title": "z" * 1000}] * 200}
        doc = json.loads(cockpit.document([("a", big), ("b", big)], {"a": "ok", "b": "ok"}, limit=300000))
        self.assertEqual(len(doc["sections"]), 1)
        self.assertEqual(doc["notes"]["b"], "dropped: hub output budget exceeded")

    def test_real_builtins_end_to_end(self):
        """The read-only shipped providers through the real runner, with the runtime
        directory (and so every state file) redirected into the sandbox."""
        (self.root / "hypr").symlink_to(pathlib.Path(safe.runtime_dir()) / "hypr")
        names = ("05-reboot", "10-agents", "20-transfers", "30-jobs", "60-dgpu", "70-tailscale", "80-disk")
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}):
            mock.patch.object(cockpit, "BUILTIN_DIR", ROOT / "providers").start()
            mock.patch.object(cockpit, "BUILTIN_PROVIDERS",
                              {n: t for n, t in cockpit.__dict__["BUILTIN_PROVIDERS"].items()}).start()
            cockpit.BUILTIN_PROVIDERS.update({n: 10 for n in names})
            mock.patch.object(cockpit, "CACHE", self.root / "omarchy-cockpit" / "providers").start()
            sections, notes = cockpit.collect(force=True)
            output = cockpit.document(sections, notes)
        self.assertEqual({k: v for k, v in notes.items() if v != "ok"}, {})
        self.assertLess(len(output), cockpit.HUB_OUTPUT_MAX)
        for _, doc in sections:
            for row in doc["rows"]:
                if "action" in row:
                    self.assertEqual(common.validate_action(row["action"]), row["action"])
        self.assertTrue((self.root / "omarchy-cockpit" / "providers" / "30-jobs.json").is_file())


# ------------------------------------------------------------------ actions

class Actions(Sandbox):
    GOOD = [
        {"kind": "focus-window", "address": "0x55d0c0ffee"},
        {"kind": "terminal", "argv": ["checkupdates"]},
        {"kind": "terminal", "argv": ["yay", "-Qua"]},
        {"kind": "terminal", "argv": ["flatpak", "update"]},
        {"kind": "run", "argv": ["tailscale", "up"]},
        {"kind": "run", "argv": ["ping", "-c1", "100.64.0.7"]},
        {"kind": "run", "argv": ["ping", "-c", "3", "fd7a:115c:a1e0::7"]},
        {"kind": "run", "argv": ["udisksctl", "mount", "-b", "/dev/mapper/luks-1234"]},
        {"kind": "terminal", "argv": ["git", "-C", common.HOME + "/Projects/x", "status", "--short"]},
    ]
    BAD = [
        "hyprctl dispatch focuswindow address:0x1",
        ["checkupdates"],
        {"kind": "shell", "argv": ["checkupdates"]},
        {"kind": "focus-window", "address": "0x1; rm -rf ~"},
        {"kind": "focus-window", "address": "0x" + "a" * 17},
        {"kind": "focus-window", "address": "0x1", "argv": []},
        {"kind": "run", "argv": []},
        {"kind": "run", "argv": "checkupdates"},
        {"kind": "run", "argv": ["sh", "-c", "id"]},
        {"kind": "run", "argv": ["/usr/bin/checkupdates"]},
        {"kind": "run", "argv": ["checkupdates", "--evil"]},
        {"kind": "run", "argv": ["ping", "-c1", "-f"]},
        {"kind": "run", "argv": ["ping", "-c1", "1.1.1.1 ; id"]},
        {"kind": "run", "argv": ["udisksctl", "mount", "-b", "/dev/../etc/shadow"]},
        {"kind": "run", "argv": ["udisksctl", "mount", "-b", "/dev/sda1\0"]},
        {"kind": "terminal", "argv": ["git", "status"]},
        {"kind": "terminal", "argv": ["git", "-C", "relative", "status"]},
        {"kind": "terminal", "argv": ["git", "-C", "/etc", "status"]},
        {"kind": "terminal", "argv": ["git", "-C", common.HOME + "/../etc", "status"]},
        {"kind": "terminal", "argv": ["git", "-C", common.HOME + "/r\n", "status"]},
        {"kind": "terminal", "argv": ["git", "-C", common.HOME + "/r", "push", "--force"]},
        {"kind": "terminal", "argv": ["git", "-C", common.HOME + "/r", "status", "-c", "core.pager=id"]},
        {"kind": "terminal", "argv": ["git", "-c", "core.fsmonitor=id", "-C", common.HOME + "/r", "status"]},
        {"kind": "terminal", "argv": ["yay", "x" * 513]},
        {"kind": "terminal", "argv": ["yay"] + ["-Qua"] * 16},
    ]

    def test_allow_list(self):
        for action in self.GOOD:
            with self.subTest(action=action):
                self.assertEqual(common.validate_action(action), action)
        for action in self.BAD:
            with self.subTest(action=action):
                with self.assertRaises(common.ActionError):
                    common.validate_action(action)

    def test_focus_window_uses_lua_dispatch(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return safe.Result(0, b"ok", b"", False, False)

        with mock.patch.object(safe, "run", fake_run):
            self.assertIsNone(cockpit.perform({"kind": "focus-window", "address": "0x55aa"}))
        self.assertEqual(calls[0][0], ["hyprctl", "dispatch", 'hl.dsp.focus({ window = "address:0x55aa" })'])
        self.assertEqual(calls[0][1]["timeout"], 3)

    def test_run_is_spawned_with_a_deadline(self):
        calls = []
        with mock.patch.object(safe, "spawn", lambda argv, **kw: calls.append((argv, kw))):
            cockpit.perform({"kind": "run", "argv": ["udisksctl", "mount", "-b", "/dev/sda1"]})
            cockpit.perform({"kind": "run", "argv": ["ping", "-c1", "100.64.0.7"]})
        self.assertEqual(calls, [(["/usr/bin/udisksctl", "mount", "-b", "/dev/sda1"], {"timeout": 120, "env": None}),
                                 (["/usr/bin/ping", "-c1", "100.64.0.7"], {"timeout": 10, "env": None})])

    @unittest.skipUnless(safe.has_tool(cockpit.TERMINAL), "presentation terminal wrapper not installed")
    def test_terminal_command_survives_bash_literally(self):
        repo = make_repo(self.root / "we'ird $(touch pwned) `x` repo")
        mock.patch.object(common, "HOME", str(self.root)).start()
        result = cockpit.perform(common.validate_action({"kind": "terminal", "argv": ["git", "-C", str(repo), "status"]}))
        launcher, command = result["exec"]
        self.assertEqual(launcher, "/usr/bin/" + cockpit.TERMINAL)
        # The wrapper runs `bash -c "...; $cmd; ..."`; let bash parse the same text.
        r = subprocess.run(["/usr/bin/bash", "-c", "set -- " + command + '; printf "%s\\0" "$@"'],
                           capture_output=True, cwd=self.root, check=True)
        words = r.stdout.decode().split("\0")[:-1]
        self.assertEqual(words[:3], ["GIT_CONFIG_NOSYSTEM=1", "GIT_OPTIONAL_LOCKS=0", "GIT_TERMINAL_PROMPT=0"])
        self.assertEqual(words[3:5], ["/usr/bin/git", "--no-pager"])
        self.assertIn("core.hooksPath=/dev/null", words)
        self.assertIn("core.fsmonitor=false", words)
        self.assertEqual(words[-3:], ["-C", str(repo), "status"])
        self.assertFalse((self.root / "pwned").exists())


class ActionHelper(Sandbox):
    """`cockpit action` as the panel runs it: a separate process, the action on stdin."""

    def helper(self, payload):
        return subprocess.run(["/usr/bin/python3", str(BIN / "cockpit"), "action"], input=payload,
                              capture_output=True, timeout=20)

    @unittest.skipUnless(safe.has_tool(cockpit.TERMINAL), "presentation terminal wrapper not installed")
    def test_terminal_action_answers_with_an_argv(self):
        r = self.helper(b'{"kind": "terminal", "argv": ["checkupdates"]}\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"exec": ["/usr/bin/" + cockpit.TERMINAL, "/usr/bin/checkupdates"]})

    def test_rejections(self):
        for payload in (b'"hyprctl dispatch exec kitty"\n',
                        b'{"kind": "run", "argv": ["sh", "-c", "id"]}\n',
                        b'{"kind": "run", "argv": ["ping", "-c1", "' + b"a" * 600 + b'"]}\n',
                        b"{" + b" " * 9000 + b"}\n",
                        b"not json\n", b""):
            with self.subTest(payload=payload[:40]):
                r = self.helper(payload)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, b"")

    @unittest.skipUnless(safe.has_tool(cockpit.TERMINAL), "presentation terminal wrapper not installed")
    def test_does_not_wait_for_end_of_input(self):
        p = subprocess.Popen(["/usr/bin/python3", str(BIN / "cockpit"), "action"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p.stdin.write(b'{"kind": "terminal", "argv": ["checkupdates"]}\n')
            p.stdin.flush()
            self.assertEqual(p.wait(timeout=10), 0)   # stdin still open, as Quickshell leaves it
            self.assertIn(b"checkupdates", p.stdout.read())
        finally:
            p.kill()
            p.wait()
            for stream in (p.stdin, p.stdout, p.stderr):
                stream.close()


# ------------------------------------------------------------------ git

class HostileRepo(Sandbox):
    def setUp(self):
        super().setUp()
        mock.patch.object(common, "HOME", str(self.root)).start()
        self.markers = self.root / "markers"
        self.markers.mkdir()
        self.repo = make_repo(self.root / "repo", files=("a.txt", "b.txt"))
        git(self.repo, "config", "filter.evil.clean", f"touch {self.markers}/clean; cat")
        git(self.repo, "config", "filter.Evil2.process", str(self.marker_script("process")))
        git(self.repo, "config", "core.fsmonitor", str(self.marker_script("fsmonitor")))
        for hook in ("post-index-change", "reference-transaction"):
            self.script(self.repo / ".git" / "hooks" / hook, f"open({str(self.markers / hook)!r}, 'w')\n")
        (self.repo / ".gitattributes").write_text("a.txt filter=evil\n")
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "attributes").write_text("b.txt filter=Evil2\n")
        # Stat-dirty but unchanged, so git has to re-read both files through their filters.
        for name in ("a.txt", "b.txt"):
            os.utime(self.repo / name, (2e9, 2e9))

    def marker_script(self, name):
        return self.script(self.root / f"{name}.sh", f"open({str(self.markers / name)!r}, 'w')\n")

    def test_fixture_is_armed(self):
        subprocess.run(["/usr/bin/git", "-C", str(self.repo), "status", "--porcelain"],
                       capture_output=True, env=GIT_TEST_ENV)
        self.assertTrue({"clean", "process", "fsmonitor"} <= set(os.listdir(self.markers)),
                        os.listdir(self.markers))

    def test_provider_git_status_runs_nothing_from_the_repo(self):
        out, partial = git_provider.status(str(self.repo), 8.0)
        self.assertEqual(os.listdir(self.markers), [])
        self.assertFalse(partial)
        self.assertIn("?? .gitattributes", out)

    def test_hardening_blanks_repo_filters_but_not_global_ones(self):
        global_config = self.root / "gitconfig"
        global_config.write_text('[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tprocess = git-lfs filter-process\n')
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}):
            flags = common.git_hardening(self.repo)
        for flag in ("filter.evil.clean=", "filter.Evil2.process=", "core.hooksPath=/dev/null", "core.fsmonitor=false"):
            self.assertIn(flag, flags)
        self.assertFalse([f for f in flags if "lfs" in f])

    def test_unblankable_driver_refuses_the_repo(self):
        git(self.repo, "config", "filter.a=b.clean", "id")
        self.assertIsNone(common.git_hardening(self.repo))
        self.assertIsNone(git_provider.status(str(self.repo), 8.0))


class GitRoots(Sandbox):
    def setUp(self):
        super().setUp()
        self.home = str(self.root)
        mock.patch.object(common, "HOME", self.home).start()
        mock.patch.object(git_provider, "HOME", self.home).start()
        self.roots_file = self.root / "git-roots"
        mock.patch.object(git_provider, "ROOTS_FILE", self.roots_file).start()
        self.outside = pathlib.Path(tempfile.mkdtemp(prefix="cockpit-outside-", dir=safe.runtime_dir()))
        self.addCleanup(shutil.rmtree, self.outside, True)
        make_repo(self.outside / "foreign")

    def test_roots_are_bounded_and_confined_to_home(self):
        make_repo(self.root / "proj" / "a")
        make_repo(self.root / "proj" / "group" / "b")
        (self.root / "proj" / "sneaky").symlink_to(self.outside)
        (self.root / "escape").symlink_to(self.outside)
        lines = ["# comment", "", "~/proj", "relative/path", "/etc", f"{self.home}/../outside",
                 f"{self.home}/escape", str(self.outside)]
        lines += [f"{self.home}/filler{i}" for i in range(80)]
        self.roots_file.write_text("\n".join(lines) + "\n")
        candidates, problem = git_provider.roots()
        self.assertIsNone(problem)
        self.assertLessEqual(len(candidates), git_provider.ROOTS_LINES_MAX)
        self.assertNotIn("relative/path", candidates)
        self.assertEqual(git_provider.find_repos(candidates),
                         [f"{self.home}/proj/a", f"{self.home}/proj/group/b"])

    def test_symlinked_roots_file_falls_back_with_a_notice(self):
        target = self.root / "real-roots"
        target.write_text(str(self.outside) + "\n")
        self.roots_file.symlink_to(target)
        candidates, problem = git_provider.roots()
        self.assertIn("symlink", problem)
        self.assertEqual(candidates[0], f"{self.home}/Projects")

    def test_oversized_roots_file(self):
        self.roots_file.write_text("~/x\n" * 10000)
        _, problem = git_provider.roots()
        self.assertIn("exceeds", problem)


# ------------------------------------------------------------------ daemon

class Daemon(Sandbox):
    def setUp(self):
        super().setUp()
        mock.patch.object(agentd, "STATE", self.root / "agent-state.json").start()
        mock.patch.object(agentd, "LOCK", self.root / "agentd.pid").start()

    def test_line_reader_bounds(self):
        r = agentd.LineReader(line_max=16, buffer_max=64)
        self.assertEqual(r.feed(b"a>>1\nb>>"), [b"a>>1"])
        self.assertEqual(r.feed(b"2\n"), [b"b>>2"])
        self.assertEqual(r.feed(b"x" * 40), [])
        self.assertLessEqual(len(r.buf), 16)
        self.assertEqual(r.feed(b"yyy\nc>>3\n"), [b"c>>3"])            # tail of the long one dropped
        self.assertEqual(r.feed(b"z" * 20 + b"\nd>>4\n"), [b"d>>4"])   # a complete long one too
        self.assertEqual(len(r.feed(b"e>>5\n" * 1000)), 1000)
        self.assertLessEqual(len(r.buf), 64)

    def test_tracker(self):
        t = agentd.Tracker()
        long_title = "⠋ task ".encode() + b"t" * 400
        self.assertTrue(t.handle(b"windowtitlev2>>55aa," + long_title, 1.0))
        self.assertEqual(len(t.windows["0x55aa"]["title"]), agentd.TITLE_MAX)
        self.assertFalse(t.handle(b"windowtitlev2>>55aa," + long_title, 2.0))
        self.assertTrue(t.handle(b"windowtitlev2>>0x55aa,other", 3.0))
        self.assertEqual(t.windows["0x55aa"]["changes"], 2)
        for bad in (b"windowtitlev2>>zz,x", b"windowtitlev2>>" + b"a" * 17 + b",x", b"windowtitlev2>>,x",
                    b"garbage", b"closewindow>>0x0bb", b"closewindow>>nothex"):
            self.assertFalse(t.handle(bad, 4.0), bad)
        self.assertTrue(t.handle(b"closewindow>>55aa", 5.0))
        self.assertEqual(t.windows, {})

    def test_tracker_cap_evicts_least_recently_changed(self):
        t = agentd.Tracker()
        for i in range(1, 301):
            t.handle(f"windowtitlev2>>{i:x},t".encode(), float(i))
        self.assertEqual(len(t.windows), agentd.WINDOWS_MAX)
        self.assertNotIn("0x1", t.windows)
        self.assertIn(f"0x{300:x}", t.windows)

    def test_state_file_is_validated_and_never_followed(self):
        state = self.root / "agent-state.json"
        state.write_text(json.dumps({"55aa": {"title": "x" * 300, "changedAt": 5, "changes": True},
                                     "nothex": {"title": "y", "changedAt": 1}, "0x1": "str"}))
        self.assertEqual(agentd.load_state(), {"0x55aa": {"title": "x" * 256, "changedAt": 5.0, "changes": 0}})
        state.write_text("{" * 5000)
        self.assertEqual(agentd.load_state(), {})
        state.unlink()
        victim = self.root / "victim"
        victim.write_text("keep")
        state.symlink_to(victim)
        self.assertEqual(agentd.load_state(), {})
        with self.assertRaises(safe.UnsafeError):
            agentd.flush(agentd.Tracker({"0x1": {"title": "t", "changedAt": 1.0, "changes": 1}}))
        self.assertEqual(victim.read_text(), "keep")

    def test_lock_ignores_stale_and_unrelated_pids(self):
        lock = self.root / "agentd.pid"
        self.assertFalse(agentd.already_running())
        lock.write_text(str(os.getpid()))
        self.assertFalse(agentd.already_running())
        sleeper = subprocess.Popen(["/usr/bin/sleep", "30"])
        imposter = subprocess.Popen(["/usr/bin/python3", "-c", "import time; time.sleep(30)", "cockpit-agentd"])
        try:
            lock.write_text(str(sleeper.pid))
            self.assertFalse(agentd.already_running())
            time.sleep(0.2)
            lock.write_text(str(imposter.pid))
            self.assertTrue(agentd.already_running())
        finally:
            for p in (sleeper, imposter):
                p.kill()
                p.wait()

    def test_watch_end_to_end_over_a_socket(self):
        path = str(self.root / "s.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)

        def serve():
            conn, _ = server.accept()
            with conn:
                conn.sendall("windowtitlev2>>abc,⠋ working\n".encode())
                conn.sendall(b"windowtitlev2>>def," + b"q" * 20000 + b"\n")
                conn.sendall(b"openwindow>>x,y\n" + "windowtitlev2>>abc,⠙ working\n".encode())

        threading.Thread(target=serve, daemon=True).start()
        mock.patch.object(common, "hypr_clients", lambda: None).start()
        self.assertEqual(agentd.watch(path), 0)
        state = json.loads((self.root / "agent-state.json").read_text())
        self.assertEqual(list(state), ["0xabc"])
        self.assertEqual(state["0xabc"]["changes"], 2)


# ------------------------------------------------------------------ providers

class Providers(Sandbox):
    def test_tailscale_rows_and_actions(self):
        data = {"BackendState": "Running", "Peer": {
            "a": {"Online": True, "HostName": "win\x1b[31m", "OS": "windows", "CurAddr": "1.2.3.4:41641",
                  "TailscaleIPs": ["100.64.0.2", "fd7a::2"], "RxBytes": 5 * 2**20, "TxBytes": "x"},
            "b": {"Online": True, "HostName": "evil", "OS": "linux", "TailscaleIPs": ["-f 1.1.1.1"]},
            "c": {"Online": False, "HostName": "off"},
            "d": "not a peer"}}
        rows = tailscale.rows_for(data)
        self.assertEqual([r["title"] for r in rows], ["win�[31m", "evil"])
        self.assertEqual(rows[0]["action"], {"kind": "run", "argv": ["ping", "-c1", "100.64.0.2"]})
        self.assertNotIn("action", rows[1])
        stopped = tailscale.rows_for({"BackendState": "Stopped"})
        self.assertEqual(common.validate_action(stopped[0]["action"]), stopped[0]["action"])

    def test_disk_unmounted_actions(self):
        devices = [{"name": "sda", "path": "/dev/sda", "fstype": None, "children": [
            {"name": "sda1", "path": "/dev/sda1", "size": "1T", "fstype": "ext4", "mountpoint": None, "label": "Games\x07"},
            {"name": "x", "path": "/dev/../etc/passwd", "size": "1G", "fstype": "ext4", "mountpoint": None, "label": None},
            {"name": "sda3", "path": "/dev/sda3", "fstype": "swap", "mountpoint": None}]}]
        rows = disk.unmounted(devices)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Games� not mounted")
        self.assertEqual(rows[0]["action"], {"kind": "run", "argv": ["udisksctl", "mount", "-b", "/dev/sda1"]})
        self.assertNotIn("action", rows[1])

    def test_updates_use_structured_terminal_actions(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append((argv, kw["timeout"], kw["max_output"]))
            return safe.Result(0, b"a 1 -> 2\nb 1 -> 2\n", b"", False, False)

        out = io.StringIO()
        with mock.patch.object(safe, "run", fake_run), \
                mock.patch.object(safe, "has_tool", lambda n: n in ("checkupdates", "yay", "flatpak")), \
                contextlib.redirect_stdout(out):
            updates.main()
        doc = json.loads(out.getvalue())
        self.assertEqual([r["action"]["argv"] for r in doc["rows"]], [["checkupdates"], ["yay", "-Qua"], ["flatpak", "update"]])
        self.assertTrue(all(t <= 25 and cap for _, t, cap in seen))
        self.assertLessEqual(doc["timeoutSec"], cockpit.TIMEOUT_MAX)

    def test_every_builtin_declares_a_budget_within_the_ceiling(self):
        for name, first_budget in cockpit.BUILTIN_PROVIDERS.items():
            self.assertTrue((ROOT / "providers" / name).is_file(), name)
            self.assertLessEqual(first_budget, cockpit.TIMEOUT_MAX)
            source = (ROOT / "providers" / name).read_text()
            self.assertTrue(source.startswith("#!/usr/bin/python3\n"), name)
            self.assertNotIn("subprocess", source, name)
            self.assertNotIn("shutil.which", source, name)


class Vendoring(unittest.TestCase):
    def test_no_shell_and_no_ambient_interpreter(self):
        for path in [BIN / "cockpit", BIN / "cockpit-agentd", BIN / "cockpit-menu-install",
                     *sorted(p for p in (ROOT / "providers").iterdir() if p.is_file())]:
            source = path.read_text()
            with self.subTest(path=path.name):
                self.assertTrue(source.startswith("#!/usr/bin/python3\n"))
                for banned in ("/usr/bin/env", "shell=True", "os.system", "os.popen", '"sh", "-c"', "import subprocess"):
                    self.assertNotIn(banned, source)
        qml = (ROOT / "Panel.qml").read_text()
        self.assertNotIn('"sh"', qml)
        self.assertNotIn("-c", qml.replace("-cockpit", ""))

    def test_plugin_safety_is_the_vendored_copy(self):
        import hashlib
        digest = hashlib.sha256((BIN / "plugin_safety.py").read_bytes()).hexdigest()
        self.assertEqual(digest, "bf8ffb9ff874caa1958526c1d0a9bd239a55842c979d41cd916675075cf8ec87")


if __name__ == "__main__":
    unittest.main(verbosity=2)
