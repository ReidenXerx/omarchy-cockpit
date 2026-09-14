"""cockpit_agents -- Claude Code sessions as the Agents section and cockpit-agentd both see them.

Claude Code keeps a status record for every running session in ~/.claude/sessions/<pid>.json:
"busy" for the whole of a turn, tool calls included; "waiting" when something needs you -- a
question, a permission or other dialog, a sandbox request -- with the reason in `waitingFor`;
"shell" when the turn is over but a background command it started is still running; and "idle"
once it is done. The record also names the session (`sessionId`, what `claude --resume` takes)
and the folder it works in (`cwd`). That record is the truth for those sessions. It is tied to a
window by walking up from the session's process to the one that owns a Hyprland window, and a
record is trusted only while its pid still names the process that wrote it (the start time must
match).

cockpit-agentd keeps the history of those sessions in
~/.local/state/omarchy-cockpit/agent-history.json -- the chats you may want back once they have
ended -- and is the only thing that writes it.
"""
import os
import pathlib
import re
import stat
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import plugin_safety as safe  # noqa: E402
import cockpit_common as common  # noqa: E402

TITLE_MAX = 256
NAME_MAX = 64
CLIENTS_MAX = 512

CLAUDE_SESSIONS = pathlib.Path(common.HOME) / ".claude" / "sessions"
SESSIONS_MAX = 64
SESSION_FILE_MAX = 16 * 1024
SESSION_LIMITS = {"max_depth": 4, "max_items": 256, "max_string": 4096}
SESSION_FILE = re.compile(r"([1-9][0-9]{0,9})\.json")
# A status is a short lowercase word. Ones this version does not know yet are shown as written
# rather than dropped: a session vanishing from the hub is worse than a plain label.
STATUS_WORD = re.compile(r"[a-z][a-z_]{0,23}")
PARENT_DEPTH = 32
# What Claude Code puts in front of its terminal title: two glyphs that alternate while it
# animates and one while it is still. Used to pick its window out of several that belong
# to one terminal process, and to take the task text from the title.
CLAUDE_TITLE_GLYPHS = ("◐", "◑", "✳")
# `waitingFor` as Claude Code writes it, shortened for a glance. A reason not listed here
# is shown as written.
WAITING_FOR = {
    "input needed": "question",
    "dialog open": "prompt open",
    "sandbox request": "sandbox request",
    "worker request": "worker request",
    "goal proposal": "goal proposal",
}

HISTORY_NAME = "agent-history.json"
HISTORY_MAX = 50
HISTORY_FILE_MAX = 256 * 1024
TIME_MAX = 1e11

# Finding the branch: at most this many folders up from where a session works, never above
# $HOME, and only small files -- HEAD is one line, a .git file names one folder.
GIT_WALK_MAX = 32
HEAD_MAX = 256
GITDIR_MAX = 4096
BRANCH_MAX = 64
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
REF_FORBIDDEN = "~^:?*[\\"


def _has_control(text):
    return any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in text)


# ------------------------------------------------------------------ small helpers

# A leading non-alphanumeric glyph followed by text is the shape every one of these
# tools uses. Matching the SHAPE rather than a list of glyphs is what makes this survive
# a tool changing its spinner.
def split_glyph(title):
    t = (title or "").strip()
    if not t:
        return "", ""
    first = t[0]
    if first.isalnum() or first in "/~.(<[\"'":
        return "", t
    return first, t[1:].strip()


def elapsed(age):
    return f"{age // 3600}h {age % 3600 // 60}m" if age >= 3600 else (f"{age // 60}m" if age >= 60 else f"{age}s")


def text_field(raw, key, limit):
    value = raw.get(key)
    return common.clean_text(value, limit) if isinstance(value, str) else ""


# ------------------------------------------------------------------ processes and windows

def window_info(client):
    if not isinstance(client, dict) or not client.get("mapped"):
        return None
    address = client.get("address")
    if not isinstance(address, str) or not common.ADDRESS.fullmatch(address):
        return None
    pid = client.get("pid")
    workspace = client.get("workspace")
    cls = client.get("class")
    return {
        "address": address,
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 else None,
        "class": cls if isinstance(cls, str) else "",
        "title": (client.get("title") if isinstance(client.get("title"), str) else "")[:TITLE_MAX],
        "ws": common.clean_text(workspace.get("name", "?"), 32) if isinstance(workspace, dict) else "?",
    }


def window_for(pid, by_pid):
    """The window a process runs in: the nearest ancestor that owns one."""
    for _ in range(PARENT_DEPTH):
        candidates = by_pid.get(pid)
        if candidates:
            if len(candidates) == 1:
                return candidates[0]
            # One terminal process with several windows (a foot server, ghostty, kitty in
            # single-instance mode). Only a window titled the way Claude Code titles its
            # terminal can be told apart, and only if exactly one is.
            titled = [w for w in candidates if split_glyph(w["title"])[0] in CLAUDE_TITLE_GLYPHS]
            return titled[0] if len(titled) == 1 else None
        fields = common.proc_stat_fields(pid)
        if fields is None or len(fields) < 2 or not fields[1].isdigit():
            return None
        pid = int(fields[1])
        if pid <= 1:
            return None
    return None


# ------------------------------------------------------------------ Claude Code sessions

def parse_session(raw, pid):
    """The fields used from one session record, checked; None if unusable."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("pid"), bool) or raw.get("pid") != pid:
        return None
    proc_start = raw.get("procStart")
    if isinstance(proc_start, int) and not isinstance(proc_start, bool):
        proc_start = str(proc_start)
    if not isinstance(proc_start, str) or not common.PROC_START.fullmatch(proc_start):
        return None
    status = raw.get("status")
    if not isinstance(status, str) or not STATUS_WORD.fullmatch(status):
        return None   # an older Claude Code without status: its title still shows it
    kind = raw.get("kind")
    session_id = raw.get("sessionId")
    cwd = raw.get("cwd")
    return {
        "pid": pid,
        "procStart": proc_start,
        "status": status,
        "waitingFor": text_field(raw, "waitingFor", 64),
        "needs": text_field(raw, "needs", 160),
        "since": common.number(raw.get("statusUpdatedAt")) / 1000,
        "name": text_field(raw, "name", NAME_MAX),
        "kind": kind if isinstance(kind, str) else "",
        # What `claude --resume` takes, and where the chat has to be resumed from.
        "sessionId": session_id if isinstance(session_id, str) and common.SESSION_ID.fullmatch(session_id) else "",
        "cwd": cwd if common.home_path(cwd) is not None else "",
    }


def read_sessions():
    """Live Claude Code sessions. A missing directory, a foreign or symlinked file, a record
    over its caps or one whose process is gone is simply not a session."""
    try:
        entries = safe.list_dir(CLAUDE_SESSIONS, max_entries=4 * SESSIONS_MAX)
    except (OSError, safe.UnsafeError):
        return []
    sessions = []
    for name, st in entries:
        match = SESSION_FILE.fullmatch(name)
        if not match or not stat.S_ISREG(st.st_mode):
            continue
        try:
            raw = safe.read_json(CLAUDE_SESSIONS / name, SESSION_FILE_MAX, default=None, **SESSION_LIMITS)
        except (OSError, safe.UnsafeError):
            continue
        session = parse_session(raw, int(match.group(1)))
        # A pid is reused after its process exits; the start time is what proves the record
        # still describes the process running now.
        if session is not None and common.proc_start(session["pid"]) == session["procStart"]:
            sessions.append(session)
            if len(sessions) >= SESSIONS_MAX:
                break
    return sessions


def reason_of(session):
    """Why a waiting session waits, as short as it can be said."""
    return session["needs"] or WAITING_FOR.get(session["waitingFor"], session["waitingFor"])


# ------------------------------------------------------------------ where a session works

def project_of(cwd):
    """The folder's name, "~" for your home itself, "" when cwd is not a usable path."""
    path = common.home_path(cwd)
    if path is None:
        return ""
    if path == common.HOME:
        return "~"
    return common.clean_text(os.path.basename(path), NAME_MAX)


def _read_text(path, cap):
    """A small file's stripped text, or None: missing, too big, not ours, reached through a
    symlink, not UTF-8, or holding control characters inside it."""
    try:
        blob = safe.read_file(path, cap)
    except (OSError, safe.UnsafeError):
        return None
    if blob is None:
        return None
    try:
        text = blob.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return None if _has_control(text) else text


def _ref_name_ok(name):
    return 0 < len(name) <= 255 and not any(ch in REF_FORBIDDEN or ord(ch) <= 32 or ord(ch) == 127 for ch in name)


def _branch_from_head(git_dir):
    text = _read_text(os.path.join(git_dir, "HEAD"), HEAD_MAX)
    if text is None:
        return ""
    if text.startswith("ref: refs/heads/"):
        name = text[len("ref: refs/heads/"):]
        return common.clean_text(name, BRANCH_MAX) if _ref_name_ok(name) else ""
    return text[:7] if SHA.fullmatch(text) else ""   # a detached HEAD: the commit, shortened


def _gitdir(dot_git, directory):
    """Where a .git FILE points (a worktree, a submodule), when that is inside your home. A
    relative gitdir is relative to the folder holding the file."""
    text = _read_text(dot_git, GITDIR_MAX)
    if text is None or not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:"):].strip()
    if not target:
        return None
    return common.home_path(os.path.normpath(os.path.join(directory, target)))


def branch_of(cwd):
    """The branch checked out where a session works, from the nearest .git's HEAD at or above
    cwd, never looking above $HOME. "" when there is none or anything about it is odd: a
    symlinked .git, a HEAD too big or not text, a gitdir outside your home."""
    directory = common.home_path(cwd)
    if directory is None:
        return ""
    for _ in range(GIT_WALK_MAX):
        dot_git = os.path.join(directory, ".git")
        kind = common.lstat_kind(dot_git)
        if kind == stat.S_IFDIR:
            return _branch_from_head(dot_git)
        if kind == stat.S_IFREG:
            gitdir = _gitdir(dot_git, directory)
            return _branch_from_head(gitdir) if gitdir else ""
        if kind is not None:
            return ""   # a symlink or something stranger: not followed
        parent = os.path.dirname(directory)
        if directory == common.HOME or parent == directory:
            return ""
        directory = parent
    return ""


# ------------------------------------------------------------------ history

def history_path():
    return common.persistent_dir() / HISTORY_NAME


def _time(value):
    return max(0.0, min(TIME_MAX, common.number(value)))


def load_history(path=None):
    """{sessionId: {"cwd", "title", "name", "startedAt", "lastSeen", "endedAt"}}, validated and
    capped to the HISTORY_MAX most recently seen; {} when missing or unusable."""
    try:
        raw = safe.read_json(path or history_path(), HISTORY_FILE_MAX, default={}, max_depth=4,
                             max_items=8 * HISTORY_MAX + 64, max_string=4 * TITLE_MAX)
    except (OSError, safe.UnsafeError):
        return {}
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    history = {}
    for session_id, entry in (sessions.items() if isinstance(sessions, dict) else ()):
        if not common.SESSION_ID.fullmatch(session_id) or not isinstance(entry, dict):
            continue
        cwd = entry.get("cwd")
        history[session_id] = {
            "cwd": cwd if common.home_path(cwd) is not None else "",
            "title": text_field(entry, "title", TITLE_MAX),
            "name": text_field(entry, "name", NAME_MAX),
            "startedAt": _time(entry.get("startedAt")),
            "lastSeen": _time(entry.get("lastSeen")),
            "endedAt": _time(entry.get("endedAt")),
        }
    newest = sorted(history, key=lambda s: history[s]["lastSeen"], reverse=True)[:HISTORY_MAX]
    return {s: history[s] for s in newest}


def save_history(history, path=None):
    """Write the HISTORY_MAX most recently seen sessions. Refuses a symlinked or foreign file."""
    target = pathlib.Path(path or history_path())
    newest = sorted(history, key=lambda s: history[s]["lastSeen"], reverse=True)[:HISTORY_MAX]
    safe.ensure_dir(target.parent)
    safe.write_json(target, {"sessions": {s: history[s] for s in newest}})
