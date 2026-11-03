# Adding a terminal

Argus records every Claude Code session no matter where it runs — that part only needs hooks. To **act** on a session
(jump to its tab, send a message, answer a prompt, switch the model, start a new session), the board has to talk to the
terminal app the session lives in. Each supported terminal is **one Python file** in `csl/terminals/`. Orca
(`csl/terminals/orca.py`) is the only one today; this guide shows how to add another.

## How the pieces fit

```
 board / Brain / hooks
        │  never call a terminal app directly
        ▼
 csl/focus.py      Claude Code know-how: finds the input line, reads selection dialogs and the /model picker,
        │          types multi-line text, answers question cards — built on four primitives:
        │          screen · write · submit · info
        ▼
 csl/terminals/<name>.py   your Terminal subclass: how to read, type into, find and open tabs in one app
```

`focus.py` does all the Claude Code–specific work, so a terminal implementation stays small: it only has to read the
visible screen as text and write keystrokes to the right tab.

**Handles.** Your implementation decides what identifies a tab (Orca: `term_…`, tmux: `%12`). The hook stores it with
the session, the board passes it back to your methods, and `valid()` checks it before it reaches a command line.

## What to implement, by feature

Methods you leave alone keep the base-class default and the feature reports "not supported" — so you can start small.

| Level | Unlocks | Methods |
|---|---|---|
| 1 · find | the session is linked to its tab; **T** / "jump to terminal" | `name`, `label`, `env_keys`, `available`, `handle_of`, `remember`, `valid`, `switch`, `activate` (+ `find` for sessions started before the hooks) |
| 2 · drive | send messages and images, answer permission prompts and question cards, stop, rename, model / effort / permission mode, rewind, branch | `screen`, `write`, `submit` (the default is fine), `info`, `exited` |
| 3 · open | "+ New session" from the board and the Brain, closing tabs when a session ends | `create`, `projects`, `close`, `terminals` |

### The contract, method by method

| Method | Return | Notes |
|---|---|---|
| `available()` | bool | Is the app / CLI installed? Cheap — called often. |
| `env_keys` | `{record_key: ENV_VAR}` | Environment variables your terminal puts into Claude Code's process (tab / pane id). The hook saves them into the session's terminal record, with `backend = name`. |
| `handle_of(term)` | handle or `None` | Read the handle back out of that record. |
| `remember(term, handle)` | dict | Return a copy of the record with the handle stored (used after `find()`). |
| `valid(handle)` | bool | Strict format check — the handle is passed to subprocess arguments. |
| `owns(term)` | bool | Default: `handle_of(term)` is set. Override to also claim records by `TERM_PROGRAM` / bundle id. |
| `find(title, cwd, project_root)` | handle or `None` | Locate a tab when no handle was recorded, by tab title (= Claude session title) and path. Return `None` unless exactly one tab matches. |
| `switch(handle)` | bool | Bring that tab to the front, app included. |
| `activate()` | bool | Bring the app to the front. |
| `info(handle)` | `{"agent", "title"}` or `None` | `agent` is `"claude"` when Claude Code is the foreground program. The board refuses to type into a tab where it is not — the text would run as a shell command. `None` = the tab is gone. |
| `exited(handle)` | `True` / `False` / `None` | The tab is still there but its shell has exited (`None` = unknown). |
| `screen(handle)` | list of str | **The visible screen as plain text, one string per row, top to bottom, no ANSI escapes.** At least the bottom of the viewport — Claude Code's input box and dialogs are drawn there and `focus.py` parses them. |
| `write(handle, data)` | bool | Write `data` to the tab's PTY **exactly as given**: control characters, `\x1b[A` arrows, `\x1b` Esc, `\n` (Ctrl+J), bracketed-paste markers `\x1b[200~ … \x1b[201~`. Never add Enter or quoting. |
| `submit(handle, text)` | `(ok, stages, error)` | Put a one-line prompt into Claude Code and press Enter. The default writes `text`, then `\r`. If your terminal can report "prompt accepted / turn started", return those in `stages` (Orca returns `turn_started`). |
| `create(cwd, title, command)` | `(handle, error)` | New tab in `cwd` running `command` (the `claude …` command line the board built). Don't steal focus if you can avoid it. |
| `projects()` | `[{"path", "name", "branch", "terminals", "at"}]` | Folders to suggest in the "New session" dialog, most relevant first. |
| `terminals()` | `[{"handle", "title", "path"}]` | Open tabs, used by `find()` and tab cleanup. |
| `close(handle)` | bool | Close the tab (after a session ends with `/exit`). |

## Registering it

1. Create `csl/terminals/<name>.py` with a `Terminal` subclass and end it with `TERMINAL = YourClass`. It is found
   automatically.
2. `ORDER` in `csl/terminals/__init__.py` decides the default for new sessions and which implementation claims a
   session first when several `env_keys` are present (for example tmux running inside Orca). Unlisted terminals come
   after the listed ones, in name order.
3. Restart the board. Sessions started **after** that record your handle; older ones are matched with `find()`.

## Example: tmux

A tmux implementation — every call is a plain `tmux` command, and a pane id (`%12`) is the handle. Its reading,
writing, `exited`, `create` and `close` were exercised against a real tmux server while writing this guide; `switch`
and `find` depend on how you attach to tmux, so try them in your own setup (see "Testing" below). `find()` matches the
pane title, which follows the title Claude Code sets — check `#{pane_title}` shows your session name.

```python
"""tmux. Session record key: tmux_pane (TMUX_PANE, e.g. "%12")."""
import os
import re
import shutil
import subprocess

from csl.terminals import Terminal

_PANE = re.compile(r"^%\d+$")


def _tmux(*args, timeout=6):
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)
        return r if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class Tmux(Terminal):
    name = "tmux"
    label = "tmux"
    env_keys = {"tmux_pane": "TMUX_PANE"}

    def available(self):
        return bool(shutil.which("tmux"))

    def handle_of(self, term):
        h = (term or {}).get("tmux_pane")
        return h if self.valid(h) else None

    def remember(self, term, handle):
        return dict(term, backend=self.name, tmux_pane=handle)

    def valid(self, handle):
        return bool(handle and _PANE.match(handle))

    def terminals(self):
        r = _tmux("list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}\t#{pane_current_path}")
        rows = [line.split("\t") for line in (r.stdout.splitlines() if r else [])]
        return [{"handle": h, "title": t, "path": p} for h, t, p in rows if self.valid(h)]

    def find(self, title, cwd, project_root):
        hits = [t for t in self.terminals() if title and t["title"].strip().lower() == title.strip().lower()]
        if len(hits) > 1:
            hits = [t for t in hits if t["path"] in (cwd, project_root)] or hits
        return hits[0]["handle"] if len(hits) == 1 else None

    def switch(self, handle):
        # select the window and pane; bringing the terminal app that hosts tmux to the front is up to activate()
        return self.valid(handle) and bool(_tmux("switch-client", "-t", handle)) and bool(_tmux("select-pane", "-t", handle))

    def info(self, handle):
        r = self.valid(handle) and _tmux("display-message", "-p", "-t", handle, "#{pane_current_command}\t#{pane_title}")
        if not r:
            return None
        cmd, _, title = r.stdout.rstrip("\n").partition("\t")
        return {"agent": "claude" if "claude" in cmd else "", "title": title}

    def exited(self, handle):
        r = self.valid(handle) and _tmux("display-message", "-p", "-t", handle, "#{pane_dead}")
        return None if not r else r.stdout.strip() == "1"

    def screen(self, handle):
        r = self.valid(handle) and _tmux("capture-pane", "-p", "-t", handle)   # no -e: plain text, visible area only
        return r.stdout.rstrip("\n").split("\n") if r else []

    def write(self, handle, data):
        # -l sends the string literally (no key-name lookup), control characters included
        return self.valid(handle) and bool(_tmux("send-keys", "-t", handle, "-l", "--", data, timeout=15))

    def create(self, cwd, title, command):
        fmt = ("-d", "-P", "-F", "#{pane_id}", "-c", cwd)
        # new-window needs a running tmux server; with none, start a session instead
        r = _tmux("new-window", *fmt, "-n", title, command, timeout=15) or \
            _tmux("new-session", *fmt, "-s", title, command, timeout=15)
        handle = r.stdout.strip() if r else ""
        return (handle, None) if self.valid(handle) else (None, "tmux could not open a window")

    def projects(self):
        counts = {}
        for t in self.terminals():
            if t["path"]:
                counts[t["path"]] = counts.get(t["path"], 0) + 1
        return [{"path": p, "name": os.path.basename(p.rstrip("/")), "branch": "", "terminals": n, "at": 0}
                for p, n in sorted(counts.items(), key=lambda kv: -kv[1])]

    def close(self, handle):
        return self.valid(handle) and bool(_tmux("kill-pane", "-t", handle))


TERMINAL = Tmux
```

## Testing

Start a Claude Code session in your terminal, then poke at your implementation from the repository root:

```sh
/usr/bin/python3 -c '
from csl import terminals
t = terminals.registry()["tmux"]
tabs = t.terminals(); print(tabs)
h = tabs[0]["handle"]
print(t.info(h))
print("\n".join(t.screen(h)[-15:]))   # should end with Claude Code input box, as plain text
'
```

Then check, in this order, on a throwaway session:

1. `info()` says `agent: "claude"` while Claude Code runs, and not after you quit it.
2. `screen()` shows the same text you see, without escape codes, including the input box at the bottom.
3. `write(h, "hello")` puts `hello` in the input box **without** sending it; `write(h, "\x1b")` clears it (Esc).
4. `submit(h, "say hi")` sends a prompt and Claude starts working.
5. On the board: **T** jumps to the tab, a message from the message box arrives, and the model chip (`/model` picker)
   switches the model — this exercises arrow keys and screen reading together.
6. "+ New session" opens a tab in the chosen folder.

If the board can find and switch to tabs but sending fails, the problem is almost always `screen()` (escape codes left
in, only part of the viewport, or rows in the wrong order) or `write()` (the terminal interprets or drops control
characters).

## Good to know

- Terminal actions run on the board's server threads: give every subprocess call a timeout.
- `focus.py` refuses to type when Claude Code isn't the foreground program, when a dialog is open that the text would
  land in, or when the text starts with `!` (Claude Code's bash mode). You don't need to re-implement these checks.
- Sessions in apps without an implementation (iTerm2, Terminal.app, VS Code, the Claude desktop app) can still be
  brought to the front by bundle id / tty, but not driven.
