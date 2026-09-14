"""cockpit_common -- what the runner, the daemon and the built-in providers share.

Everything that touches the outside world goes through plugin_safety. This module adds
the cockpit-specific parts on top: where state lives, bounded /proc walking, the git
invocation a hostile repository cannot turn into code execution, and the structured row
actions (the only thing a click can do).
"""
import os
import pathlib
import re
import stat
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import plugin_safety as safe  # noqa: E402

HOME = safe.home_dir()

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
PROC_START = re.compile(r"[0-9]{1,20}")


def _under(path, anchor):
    return path == anchor or path.startswith(anchor + "/")


def state_dir():
    """$XDG_RUNTIME_DIR/omarchy-cockpit. Session state on tmpfs: the daemon and the fast
    providers rewrite their files every second or two and every plugin_safety write
    fsyncs -- free in memory, real disk I/O anywhere else."""
    return pathlib.Path(safe.runtime_dir()) / "omarchy-cockpit"


def persistent_dir():
    """~/.local/state/omarchy-cockpit: what has to outlive a logout -- the rows you dismissed and the agent
    sessions you may want back. Written when those change, never on a timer."""
    return pathlib.Path(HOME) / ".local" / "state" / "omarchy-cockpit"


def clean_text(value, limit):
    """Display text: strings and numbers only, control characters (terminal escapes
    included) replaced, length capped."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return _CONTROL.sub("�", str(value)[:limit])


def number(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return float(value)


def lstat_kind(path):
    try:
        return stat.S_IFMT(os.lstat(path).st_mode)
    except (OSError, ValueError):
        return None


def home_path(value):
    """An absolute, normalised path inside $HOME with no control characters, else None."""
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        return None
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        return None
    return value if _under(value, HOME) else None


# ------------------------------------------------------------------ system sources

PID_SCAN_MAX = 65536
FD_SCAN_MAX = 4096
CLIENTS_OUTPUT_MAX = 4 * 1024 * 1024


def pids(limit=PID_SCAN_MAX):
    """Numeric /proc entries, capped. A directory listing only; contents are read with
    safe.read_system_file."""
    out = []
    try:
        with os.scandir("/proc") as it:
            for entry in it:
                if entry.name.isdigit():
                    out.append(entry.name)
                    if len(out) >= limit:
                        break
    except OSError:
        pass
    return out


def proc_read(pid, name, cap):
    """Text of /proc/<pid>/<name>, or None when gone, unreadable or over cap."""
    try:
        blob = safe.read_system_file(f"/proc/{pid}/{name}", cap)
    except (OSError, safe.UnsafeError):
        return None
    return None if blob is None else blob.decode("utf-8", "replace")


def proc_stat_fields(pid):
    """/proc/<pid>/stat fields after the command name (state first), or None."""
    content = proc_read(pid, "stat", 4096)
    if not content or ")" not in content:
        return None
    return content[content.rfind(")") + 2:].split()


def proc_start(pid):
    """When the process started (field 22 of /proc/<pid>/stat, clock ticks since boot) as a string, or None. A pid
    is reused once its process exits; the start time is what tells the two apart."""
    fields = proc_stat_fields(pid)
    if fields is None or len(fields) <= 19 or not PROC_START.fullmatch(fields[19]):
        return None
    return fields[19]


def open_files(pid, limit=FD_SCAN_MAX, with_stat=True):
    """[(fd, target, stat or None)] for a process's descriptors we may inspect. The stat
    goes through the /proc magic link, so it describes the open file itself rather than
    whatever now sits at its old path."""
    out = []
    base = f"/proc/{pid}/fd"
    try:
        with os.scandir(base) as it:
            for entry in it:
                if len(out) >= limit:
                    break
                if not entry.name.isdigit():
                    continue
                try:
                    target = os.readlink(f"{base}/{entry.name}")
                    st = os.stat(f"{base}/{entry.name}") if with_stat else None
                except OSError:
                    continue
                out.append((entry.name, target, st))
    except OSError:
        pass   # another user's process, or it exited in between
    return out


def uptime():
    try:
        return float((safe.read_system_file("/proc/uptime", 128) or b"0").split()[0])
    except (OSError, ValueError, IndexError, safe.UnsafeError):
        return 0.0


def hypr_clients():
    """`hyprctl -j clients` as a list, or None. Bounded in time, bytes and structure."""
    try:
        r = safe.run(["hyprctl", "-j", "clients"], timeout=4, max_output=CLIENTS_OUTPUT_MAX)
        if not r.ok:
            return None
        clients = safe.loads(r.stdout, max_depth=8, max_items=200000, max_string=1 << 16)
    except (OSError, ValueError, safe.UnsafeError):
        return None
    return clients if isinstance(clients, list) else None


# ------------------------------------------------------------------ git

# A repository is data, and its .git/config can name programs: a filesystem monitor,
# hooks, and clean/process filter drivers that `git status` runs on any stat-dirty file.
# Hooks and the monitor are switched off by name. Filter drivers have repository-chosen
# names, so every one defined outside the user's own global config is blanked on the
# command line, which outranks the repository's config. Verified against git 2.55: with
# only the first two flags a hostile clean filter still runs during `git status`; with
# the blanking nothing does.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
GIT_BASE = ["--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
_FILTER_KEY = re.compile(r"filter\.(.+)\.(clean|smudge|process)")
FILTER_DRIVERS_MAX = 32


def _trusted_git_config_files():
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    trusted = {os.path.join(HOME, ".gitconfig"), os.path.join(xdg, "git", "config")}
    if os.environ.get("GIT_CONFIG_GLOBAL"):
        trusted.add(os.environ["GIT_CONFIG_GLOBAL"])
    return trusted


def git_hardening(repo, timeout=4):
    """Global git options that keep a repository's own config from running anything, or
    None when that config could not be inspected safely (then git must not run there)."""
    try:
        r = safe.run(["git", *GIT_BASE, "-C", str(repo), "config", "--null", "--show-origin",
                      "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
                     env=GIT_ENV, timeout=timeout, max_output=64 * 1024)
    except (OSError, safe.UnsafeError):
        return None
    if r.timed_out or r.truncated or r.returncode not in (0, 1):   # 1: no filter keys
        return None
    trusted = _trusted_git_config_files()
    names = set()
    fields = r.stdout.split(b"\0")
    for i in range(0, len(fields) - 1, 2):
        origin = fields[i].decode("utf-8", "replace")
        key = fields[i + 1].decode("utf-8", "replace").split("\n", 1)[0]
        match = _FILTER_KEY.fullmatch(key)
        if not match:
            return None
        if origin.startswith("file:") and origin[5:] in trusted:
            continue
        name = match.group(1)
        # "=" would end the key early in `-c key=value`, so such a driver cannot be blanked.
        if "=" in name or len(name) > 128 or _CONTROL.search(name):
            return None
        names.add(name)
    if len(names) > FILTER_DRIVERS_MAX:
        return None
    flags = list(GIT_BASE)
    for name in sorted(names):
        for var in ("clean", "smudge", "process"):
            flags += ["-c", f"filter.{name}.{var}="]
    return flags


def git_command(repo, *args, timeout=4):
    """argv for a hardened git invocation in repo (run it with env=GIT_ENV), or None."""
    flags = git_hardening(repo, timeout=timeout)
    if flags is None:
        return None
    return ["git", *flags, "-C", str(repo), *args]


# ------------------------------------------------------------------ actions

# A row's action is data, never a command line. The kinds:
#   {"kind": "focus-window", "address": "0x55d0c0ffee"}
#   {"kind": "terminal",     "argv": ["git", "-C", "/home/me/repo", "status"]}
#   {"kind": "run",          "argv": ["udisksctl", "mount", "-b", "/dev/sda1"]}
#   {"kind": "open-url",     "url": "http://127.0.0.1:5173/"}             loopback only
#   {"kind": "open-folder",  "path": "/home/me/src/api"}                  inside your home
#   {"kind": "stop-process", "pid": 4121, "start": "8812734"}             yours, and still that process
#   {"kind": "resume-agent", "session": "<uuid>", "cwd": "/home/me/src/api"}
#   {"kind": "dismiss",      "provider": "40-git", "key": "...", "marker": "..."}   the runner's own
#   {"kind": "undismiss",    "provider": "40-git"}                                   the runner's own
# argv[0] must be one of TOOLS and the rest must match one of that tool's shapes, so data
# a provider interpolates (a host, a device, a repo path) can only fill the slot meant for
# it -- never become an option or a second command.
ACTION_MAX = 8 * 1024
ARG_MAX = 512
ARGV_MAX = 16
KINDS = ("focus-window", "terminal", "run", "open-url", "open-folder", "stop-process", "resume-agent",
         "dismiss", "undismiss")
# Only the runner makes these, for the rows it can hide; from a provider they are refused.
RUNNER_KINDS = ("dismiss", "undismiss")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{1,16}")
LOCAL_URL = re.compile(r"http://(?:127\.0\.0\.1|localhost|\[::1\]):([1-9][0-9]{0,4})(?:/[A-Za-z0-9._~/-]*)?")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DISMISS_KEY_MAX = 256
DISMISS_MARKER_MAX = 128
PID_MAX = 4194304

_HOST = r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}"
_DEVICE = r"/dev/[A-Za-z0-9][A-Za-z0-9_.:+-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:+-]*)*"
SHAPES = {
    "checkupdates": [[]],
    "yay": [["-Qua"], ["-Syu"]],
    "paru": [["-Qua"], ["-Syu"]],
    "flatpak": [["update"], ["remote-ls", "--updates"]],
    "tailscale": [["up"], ["status"]],
    "ping": [["-c1", _HOST], ["-c", "[1-9]", _HOST]],
    "udisksctl": [["mount|unmount", "-b", _DEVICE]],
}
TOOLS = ("git",) + tuple(SHAPES)
GIT_STATUS_OPTION = re.compile(
    r"-s|-b|-sb|--short|--branch|--long|--show-stash|--porcelain(?:=v[12])?|"
    r"--untracked-files(?:=(?:no|normal|all))?|--ignored|--no-renames|"
    r"--ahead-behind|--no-ahead-behind")
# Background runs get a deadline; udisksctl may be waiting on a polkit prompt.
RUN_TIMEOUT = {"udisksctl": 120, "ping": 10, "tailscale": 60}
RUN_TIMEOUT_DEFAULT = 60


class ActionError(ValueError):
    pass


def dismiss_key(value):
    """A row's dismissal key (a repo path, a session id), or None."""
    if not isinstance(value, str) or not 0 < len(value) <= DISMISS_KEY_MAX or _CONTROL.search(value):
        return None
    return value


def dismiss_marker(value):
    """What the row looked like when dismissed, as its provider sums it up; may be empty. None if unusable."""
    if not isinstance(value, str) or len(value) > DISMISS_MARKER_MAX or _CONTROL.search(value):
        return None
    return value


def focus_window_argv(address):
    """hyprctl's argv to focus a window. Hyprland 0.56 dispatchers are Lua; the address has to
    match 0x[0-9a-fA-F]{1,16}, so nothing else can reach the expression."""
    if not isinstance(address, str) or not ADDRESS.fullmatch(address):
        raise ActionError("focus-window address must match 0x[0-9a-fA-F]{1,16}")
    return ["hyprctl", "dispatch", 'hl.dsp.focus({ window = "address:%s" })' % address]


def _fields(action, kind, *names):
    if set(action) != {"kind", *names}:
        raise ActionError(f"{kind} takes exactly kind and {', '.join(names)}")


def validate_action(action):
    """The action, normalised, or ActionError."""
    if not isinstance(action, dict):
        raise ActionError("an action must be an object (shell-string actions were removed)")
    kind = action.get("kind")
    if kind not in KINDS:
        raise ActionError(f"unknown action kind {str(kind)[:32]!r}")
    if kind == "focus-window":
        if set(action) != {"kind", "address"}:
            raise ActionError("focus-window takes exactly kind and address")
        address = action.get("address")
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise ActionError("focus-window address must match 0x[0-9a-fA-F]{1,16}")
        return {"kind": kind, "address": address}
    if kind == "open-url":
        _fields(action, kind, "url")
        url = action.get("url")
        match = LOCAL_URL.fullmatch(url) if isinstance(url, str) else None
        if match is None or int(match.group(1)) > 65535:
            raise ActionError("open-url takes only http://127.0.0.1, localhost or [::1] with a port")
        return {"kind": kind, "url": url}
    if kind == "open-folder":
        _fields(action, kind, "path")
        if home_path(action.get("path")) is None:
            raise ActionError("open-folder path must be an absolute, normalised path inside your home directory")
        return {"kind": kind, "path": action["path"]}
    if kind == "stop-process":
        _fields(action, kind, "pid", "start")
        pid, start = action.get("pid"), action.get("start")
        if isinstance(pid, bool) or not isinstance(pid, int) or not 2 <= pid <= PID_MAX:
            raise ActionError("stop-process pid must be a process id")
        if not isinstance(start, str) or not PROC_START.fullmatch(start):
            raise ActionError("stop-process start must be the process start time from /proc/<pid>/stat")
        return {"kind": kind, "pid": pid, "start": start}
    if kind == "resume-agent":
        _fields(action, kind, "session", "cwd")
        session = action.get("session")
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
            raise ActionError("resume-agent session must be a session id")
        if home_path(action.get("cwd")) is None:
            raise ActionError("resume-agent cwd must be an absolute, normalised path inside your home directory")
        return {"kind": kind, "session": session, "cwd": action["cwd"]}
    if kind in RUNNER_KINDS:
        names = ("provider", "key", "marker") if kind == "dismiss" else ("provider",)
        _fields(action, kind, *names)
        provider = action.get("provider")
        if not isinstance(provider, str) or not PROVIDER_NAME.fullmatch(provider):
            raise ActionError(f"{kind} provider must be a provider name")
        if kind == "undismiss":
            return {"kind": kind, "provider": provider}
        if dismiss_key(action.get("key")) is None or dismiss_marker(action.get("marker")) is None:
            raise ActionError(f"dismiss key must be 1..{DISMISS_KEY_MAX} and marker 0..{DISMISS_MARKER_MAX} "
                              "characters without control characters")
        return {"kind": kind, "provider": provider, "key": action["key"], "marker": action["marker"]}
    if set(action) != {"kind", "argv"}:
        raise ActionError(f"{kind} takes exactly kind and argv")
    argv = action.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= ARGV_MAX:
        raise ActionError(f"argv must be a list of 1..{ARGV_MAX} strings")
    for arg in argv:
        if not isinstance(arg, str) or len(arg) > ARG_MAX or "\0" in arg:
            raise ActionError(f"each argument must be a string of at most {ARG_MAX} characters without NUL")
    tool, args = argv[0], argv[1:]
    if tool not in TOOLS:
        raise ActionError(f"{tool[:32]!r} is not an allowed action program")
    if tool == "git":
        _check_git(args)
    elif not any(_matches(shape, args) for shape in SHAPES[tool]):
        raise ActionError(f"arguments not allowed for {tool}")
    return {"kind": kind, "argv": list(argv)}


def _matches(shape, args):
    return len(shape) == len(args) and all(re.fullmatch(p, a) for p, a in zip(shape, args))


def _check_git(args):
    # Only `git -C <repo in $HOME> status [options]`: other subcommands take pagers, diff
    # drivers and editors from the repository's config, and global options could undo the
    # hardening flags that are put in front.
    if len(args) < 3 or args[0] != "-C" or args[2] != "status":
        raise ActionError("git actions must be: git -C <repo> status [options]")
    if home_path(args[1]) is None:
        raise ActionError("git repo must be an absolute, normalised path inside your home directory")
    for opt in args[3:]:
        if not GIT_STATUS_OPTION.fullmatch(opt):
            raise ActionError(f"git status option {opt[:32]!r} is not allowed")


def action_or_none(action):
    """For providers: attach an action only if the runner will accept it."""
    try:
        return validate_action(action)
    except ActionError:
        return None
