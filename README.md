# Cockpit

An [Omarchy](https://omarchy.org) bar widget: one hub for **what the machine is doing right
now**, so you stop switching workspaces to find out.

Which AI sessions are working and on what. File transfers with real progress. Long-running
jobs you started and forgot. Repos with work that exists on exactly one disk. Whether a
reboot is owed. Extensible with your own providers.

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
| **Agents** | terminal AI sessions: task, workspace, working or last active — click to jump there |
| **Transfers** | `cp` `mv` `rsync` `curl` `wget` `tar` `dd` with real %, rate and ETA |
| **Jobs** | long-running commands you started in a shell, with elapsed time |
| **Git** | unpushed commits always; dirty trees only if touched recently |
| **Updates** | pacman / AUR / flatpak pending |
| **GPU** | discrete GPU power state and what is holding it awake |
| **Tailnet** | which of your machines are up, direct or relayed |
| **Disks** | filesystems past 85%, and volumes you mount on demand |

Sections cap at six rows with a `+N more` you can click. Only things genuinely **in
motion** light the dot on the bar icon — pending updates and dirty repos are true all day,
and a badge that never goes out is one you stop reading.

## Write your own provider

Drop an executable in `~/.config/omarchy/cockpit/providers/`. It prints one JSON object and
appears in the hub. A file there shadows a built-in of the same name, so anything shipped
can be replaced without editing the plugin.

```json
{
  "section": "CI",
  "glyph": "󰜎",
  "priority": 35,
  "intervalSec": 60,
  "timeoutSec": 20,
  "rows": [
    { "glyph": "󰄬", "title": "api-server", "detail": "3 checks passed",
      "state": "ok", "progress": 100, "action": "xdg-open https://..." }
  ]
}
```

`state` is `ok` · `busy` · `warn` · `idle` and only tints the row. `progress` draws a bar
when present. `action` is a shell command run on click. `intervalSec` and `timeoutSec` are
the provider's own budget — scanning repos or asking a mirror what is stale legitimately
costs more than reading `/proc`, and the runner caches each provider separately so an
expensive one is not re-run because a cheap one ticked.

Anything else on stdout, a non-zero exit, or a timeout means that provider is skipped with
a visible note. One broken provider never takes the hub down.

```bash
bin/cockpit --list          # providers found, and where from
bin/cockpit --plain         # human-readable, for checking a provider's output
bin/cockpit --only git      # run one, uncached
```

## Two things worth knowing

**Agent busy-detection needs a daemon.** `hyprctl clients` serves a *cached* window title —
sampled ten times in two seconds it returns the identical glyph while the compositor's own
event socket reports the spinner turning. So `cockpit-agentd` holds `.socket2.sock` open and
records when each title actually last changed. The panel starts it, so it dies with the
shell. Busy-detection compares titles rather than matching a list of known spinner glyphs,
which keeps it working when a tool changes its alphabet.

**"Last active" is not "idle".** The spinner turns while an agent streams output and goes
quiet during a long tool call. So `working` is trustworthy; `last active 4m` means the last
visible sign of life, not proof that nothing is happening. The label says so rather than
claiming idleness.

## Configuration

| file | effect |
|---|---|
| `~/.config/omarchy/cockpit/providers/` | your providers |
| `~/.config/omarchy/cockpit/git-roots` | directories to scan for repos, one per line |
| `~/.config/omarchy/cockpit/dirty-within-days` | how recent a dirty tree must be to show (default 7) |

Rows per section and refresh interval are in the widget's own settings.

## Remove

```bash
bin/cockpit-menu-install remove
omarchy plugin remove reidenxerx.cockpit
```

State lives in `~/.cache/omarchy-cockpit/` and can be deleted. The daemon stops with the
shell; nothing else is left running.

## License

MIT
