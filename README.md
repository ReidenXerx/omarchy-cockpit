# Cockpit

![Cockpit](preview.png)

An [Omarchy](https://omarchy.org) bar widget: one hub for **what the machine is doing right
now**, so you stop switching workspaces to find out.

Which AI sessions are working, which are waiting for you, and on what. File transfers with
real progress. Long-running jobs you started and forgot. Repos with work that exists on
exactly one disk. Whether a reboot is owed. Extensible with your own providers.

![bar widget](https://img.shields.io/badge/omarchy-bar--widget-blue)

## Install

```bash
omarchy plugin add https://github.com/ReidenXerx/omarchy-cockpit.git --enable
```

Optionally add its entries to the Omarchy menu:

```bash
~/.config/omarchy/plugins/reidenxerx.cockpit/bin/cockpit-menu-install
```

## What ships

| section | what it tells you |
|---|---|
| **Reboot** | kernel or driver updated since boot, in one row rather than one per package |
| **Agents** | terminal AI sessions: **needs you** (a question or a prompt), **working** or **idle**, with task and workspace — click to jump there |
| **Transfers** | `cp` `mv` `rsync` `curl` `wget` `tar` `dd` with real %, rate and ETA |
| **Jobs** | long-running commands you started in a shell, with elapsed time |
| **Git** | unpushed commits always; dirty trees only if touched recently |
| **Updates** | pacman / AUR / flatpak pending |
| **GPU** | discrete GPU power state and what is holding it awake |
| **Tailnet** | which of your machines are up, direct or relayed |
| **Disks** | filesystems past 85%, and volumes you mount on demand |

Sections cap at six rows with a `+N more` you can click. Only things genuinely **in
motion** light the dot on the bar icon — pending updates and dirty repos are true all day,
and a badge that never goes out is one you stop reading. The dot turns **urgent** while an
agent is waiting on you, and the tooltip leads with how many are.

## Write your own provider

Drop an executable in `~/.config/omarchy/cockpit/providers/`. It prints one JSON object and
appears in the hub. A file there shadows a built-in of the same name, so anything shipped
can be replaced without editing the plugin.

```json
{
  "section": "Work",
  "glyph": "󰜎",
  "priority": 35,
  "intervalSec": 60,
  "timeoutSec": 20,
  "rows": [
    { "glyph": "󰊢", "title": "api-server", "detail": "3 files changed",
      "state": "warn", "progress": 40,
      "action": { "kind": "terminal", "argv": ["git", "-C", "/home/you/src/api-server", "status"] } }
  ]
}
```

`state` is `ok` · `busy` · `warn` · `needs` · `idle`. It tints the row; `busy` lights the
bar dot, and `needs` — something stalled until you act — tints the detail line and turns
the dot urgent. Keep `warn` for things that are true all day. `progress` draws a bar when
present. `intervalSec` and `timeoutSec` are the provider's own budget — scanning repos or
asking a mirror what is stale legitimately costs more than reading `/proc`, and the runner
caches each provider separately so an expensive one is not re-run because a cheap one
ticked.

**Actions are structured** (changed in 1.1 — before that `action` was a shell command
line, and such strings are now ignored with a note in the hub). A click can do one of:

| action | does |
|---|---|
| `{"kind": "focus-window", "address": "0x55d0c0ffee"}` | focuses that Hyprland window |
| `{"kind": "terminal", "argv": [...]}` | runs the command in Omarchy's floating presentation terminal |
| `{"kind": "run", "argv": [...]}` | runs the command in the background, with a deadline |

`argv` is never given to a shell, and only these commands are accepted:

| program | arguments |
|---|---|
| `git` | `-C <absolute path inside your home> status [--short, --branch, --porcelain, …]` |
| `checkupdates` | none |
| `yay`, `paru` | `-Qua` or `-Syu` |
| `flatpak` | `update` or `remote-ls --updates` |
| `tailscale` | `up` or `status` |
| `ping` | `-c1 <host>` or `-c <1-9> <host>` |
| `udisksctl` | `mount` or `unmount`, `-b /dev/<device>` |

Anything else is dropped from the row (the rest of the row still shows).

The runner's limits: 256 KB of output, 200 rows per section, 1 KB per string, 30 seconds.
Anything else on stdout, a non-zero exit, a timeout or an overrun means that provider is
skipped with a visible note, keeping its last good output. One broken provider never takes
the hub down.

```bash
bin/cockpit --list          # providers found, where from, and why one was skipped
bin/cockpit --plain         # human-readable, for checking a provider's output
bin/cockpit --only agents   # run one, uncached
```

## How agent status works

**Claude Code sessions report their own status.** Each running session keeps a small
record in `~/.claude/sessions/<pid>.json`: `busy` for the whole of a turn — tool calls
included — `waiting` when something needs you, with the reason, and `idle` once it is done.
Cockpit reads those records (read-only) and shows:

| status | row |
|---|---|
| waiting on a question | **needs you: question** |
| waiting on a permission or other dialog | **needs you: prompt open**, or the choice itself when the session names it |
| sandbox or worker request | **needs you: sandbox request** / **worker request** |
| turn in progress | **working for 3m** |
| turn over, a background command it started still running | **working in background for 3m** |
| done | **idle for 12m** |
| a status newer than this version knows | shown as written, e.g. **compacting for 1m** |

Each session is tied to its window by walking up from its process to the one that owns a
Hyprland window, so a click jumps to the right terminal. When one terminal process owns
several windows (a foot server, Ghostty, single-instance kitty), the window titled the way
Claude Code titles its terminal is chosen, and only if exactly one is. A session with no
window it can be tied to (tmux, a background job) still shows, without the jump. A record
counts only while its pid still names the process that wrote it — the process start time
has to match — so a leftover file from a crashed session never shows as a live one.

**Other terminal agents are read from their window title.** Codex, opencode, crush and the
rest show a spinner glyph in the title while they stream output. `hyprctl clients` serves a
*cached* title — sampled ten times in two seconds it returns the identical glyph while the
compositor's own event socket reports the spinner turning — so `cockpit-agentd` holds
`.socket2.sock` open and records when each title actually last changed. The panel starts
it, so it dies with the shell. Busy-detection compares titles rather than matching a list
of known spinner glyphs, which keeps it working when a tool changes its alphabet.

A title spinner goes quiet during a long tool call and cannot show a question, so these
rows say **working** or **last active 4m**, never "idle" or "needs you": the label does not
claim more than the title can tell.

## Security

- **No shell.** Helpers run as `/usr/bin/python3 <plugin>/bin/…`; every external program is
  resolved to a root-owned binary in `/usr/bin` and run with `PATH=/usr/bin`, a deadline,
  an output ceiling and a whole-process-group kill (`bin/plugin_safety.py`, shared with
  the author's other plugins). The panel stops any helper that overruns.
- **Clicks carry data, not commands.** A row action travels to `cockpit action` over stdin
  (8 KB cap) and is checked against the allow-list above before anything runs. Window
  addresses must be hex; hosts, devices and repo paths can only fill their own slot.
- **Your providers must be yours.** A user provider is run only if it is a regular file you
  own, not group- or other-writable, reached without symlinks; otherwise the hub says why
  it was skipped.
- **Repositories are untrusted.** Git runs with hooks and the filesystem monitor off, every
  filter driver a repository defines blanked (a hostile `.git/config` can otherwise make
  `git status` run a program), no system config and no optional locks. Git roots must
  resolve inside your home, and the scan below a root never follows a symlink.
- **Bounded input everywhere.** Provider output, the Hyprland event stream (8 KB per event,
  64 KB buffered, 256-character titles, 256 windows), `hyprctl`/`tailscale`/`lsblk` JSON,
  Claude Code session records (64 files, 16 KB and four levels deep each, regular files you
  own reached without symlinks, display text stripped of control characters) and every
  state file are size- and structure-capped. Files are read and written without following
  symlinks, through random temporary files and atomic renames.

## Configuration

| file | effect |
|---|---|
| `~/.config/omarchy/cockpit/providers/` | your providers |
| `~/.config/omarchy/cockpit/git-roots` | directories to scan for repos, one per line (first 64 lines; absolute or `~/` paths inside your home) |
| `~/.config/omarchy/cockpit/dirty-within-days` | how recent a dirty tree must be to show (default 7) |

Rows per section and refresh interval are in the widget's own settings.

## Development

```bash
python3 tests/cockpit_test.py   # runner, actions, daemon, agent sessions, providers, hostile git repos
node tests/model_test.js        # panel display logic
```

## Remove

```bash
bin/cockpit-menu-install remove
omarchy plugin remove reidenxerx.cockpit
```

State lives in `$XDG_RUNTIME_DIR/omarchy-cockpit/` and is gone at logout. Version 1.0 kept
it in `~/.cache/omarchy-cockpit/`, which is no longer used and can be deleted. The daemon
stops with the shell; nothing else is left running. Cockpit never writes to `~/.claude`.

## License

MIT
