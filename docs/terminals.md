# Adding a terminal

Argus records every Claude Code session no matter where it runs — that part only needs hooks. To **act** on a session
(jump to its tab, send a message, answer a prompt, switch the model, start a new session), the board has to talk to the
terminal app the session lives in. Each supported terminal is **one Python file** in `csl/terminals/`. Orca
(`csl/terminals/orca.py`) and tmux (`csl/terminals/tmux.py`) are supported today; this guide shows how to add another.

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

[`csl/terminals/tmux.py`](../csl/terminals/tmux.py) is a complete implementation in about 200 lines — every call is a
plain `tmux` command and a pane id (`%12`) is the handle. It is the best starting point for a new terminal. Things it
had to get right that are easy to miss:

- **Write bytes, not keys.** `tmux send-keys -l` looked right in a raw test, but Claude Code turns on extended keys
  inside tmux, and tmux then re-encodes a newline as a Ctrl+J key code that Claude Code drops — multi-line messages
  arrived as one line. Writing through a buffer (`load-buffer` + `paste-buffer -r`, without `-p`) puts the exact bytes
  into the pane. Test `write()` with a real Claude Code session, not only with a raw reader.
- **The foreground command may not be called `claude`.** tmux reported Claude Code as `2.1.317` (the versioned binary),
  so `info()` also checks the pane's tty for a foreground `claude` process.
- **Nested terminals.** tmux usually runs inside another terminal, so both sets of environment variables are present.
  `inner = True` makes `env_info()` record tmux first; typing into the outer tab would reach whichever pane tmux is showing.
- **Record enough to reach the right server.** The hook stores `TMUX` (socket path) next to `TMUX_PANE`, and every
  call for that pane passes `-S <socket>`, so sessions on a non-default server (`tmux -L name`) work.
- **Pressing Enter.** `submit()` waits 0.3 s between the text and Enter; sent together, Claude Code can treat the burst
  as a paste and take Enter as a newline.
- **Opening a session.** `create()` opens a window with the user's login shell and types the command into it, so `PATH`
  is the user's and an error message stays readable — running the command directly would close the window on failure.

### Variant: the board terminal (`web.py`)

[`csl/terminals/web.py`](../csl/terminals/web.py) reuses all of tmux by overriding one method, `_t()`, so every tmux
call goes to the board's own server (`-L argus-web`). The server sets `ARGUS_WEB=1` for every pane; since tmux and the
board terminal both see `TMUX_PANE`, `claims(environ)` lets the board terminal take only panes that have it (it is in
`ORDER` before tmux). `auto_default = False` keeps it from becoming the default terminal — the new-session dialog asks
for it. It has no outside app, so `switch()` returns False and the board opens the terminal in the column instead.

## Testing

Start a Claude Code session in your terminal, then poke at your implementation from the repository root:

```sh
/usr/bin/python3 -c '
from csl import terminals
t = terminals.registry()["tmux"]   # or your terminal's name
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

### Shell tabs (terminal panel)

`web.py` also hosts the right-hand terminal panel: `shells()` / `new_shell(cwd)` / `kill_shell(name)` manage plain login shells named `shell-<n>` on the same `-L argus-web` server (with `ARGUS_WEB=1`, so a `claude` started there is recorded as a board-terminal session). The server exposes them at `GET/POST /api/shells` and `DELETE /api/shells/<name>` (same-origin + `X-Argus-Term` token) and attaches the browser at `/api/term/shell/ws?name=…`.

### Handles carry the tmux socket

Pane ids are numbered per tmux server (`%0` exists on every server), so a tmux handle is `%<n>@<socket path>` (plain `%<n>` means the default server). `handle_of` builds it from `TMUX_PANE` and the first field of `TMUX`, and `create` returns it from `#{pane_id}@#{socket_path}`. An earlier version kept a global pane → socket map, which let an old record on another server redirect commands meant for the default server's `%0`.

### Attaching in the board

`attach_argv(handle)` returns the command the board runs in a PTY to show a pane in a column. The tmux implementation creates a grouped session (`argus-view-…`) sharing the original session's windows, selects the pane's window there, and attaches with `; set-option destroy-unattached on` so the view disappears when the column closes; `release(argv)` removes it if the attach never happened. The board terminal attaches to its own session directly. Clients of `argus-view-…` sessions are ignored when looking for the terminal window to switch to.
