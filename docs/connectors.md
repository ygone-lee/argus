# Writing a connector

A **connector** brings things that arrive for you in an outside service — Slack mentions, mail, notifications — into the
Brain's inbox, or lets the Brain look something up on demand (like your calendar). Each kind of connector is **one Python
file** in `csl/connectors/`. Drop the file in, restart the board, and it shows up under ⋮ → 🔌 커넥터 (Connectors) →
"커넥터 추가" (Add connector).

Existing connectors to read alongside this guide:

| File | Kind | Shows |
|---|---|---|
| `csl/connectors/gmail.py` | inbox | The smallest real example: a CLI that is already logged in, no secrets |
| `csl/connectors/slack.py` | inbox | Secrets in the Keychain, paging, resuming from the last item, an extra `followups()` method |
| `csl/connectors/calendar.py` | info | A connector that collects nothing and answers `query()` calls instead |

## How data flows

```
 your service ──poll(ctx)──▶ [Item, Item, …] ──▶ feed_items table ──▶ ./argus inbox raw ──▶ the Brain
                                                   (deduplicated,        (reads them,          (turns them into
                                                    secrets masked)       with source + key)    inbox cards)
```

1. The server calls `tick()` every minute (`csl/daily.py` scheduler). For each enabled **instance** whose interval has
   passed, it calls your connector's `poll(ctx)`.
2. You return a list of `Item`s that are new since `ctx.since`. Argus stores them in `feed_items`, keyed by
   `<instance id>:<item key>` so the same item is never stored twice, masks secrets in the text, and remembers the
   newest timestamp as the next `ctx.since`.
3. When you ask the Brain to tidy the inbox (or the auto check runs), it reads them with `./argus inbox raw`, decides
   what needs you or a session, writes cards, and marks the raw items done (their bodies are deleted then).

An **instance** is one configured copy of a connector kind — for example one Slack connector per workspace. Everything
per-instance (settings, secrets, state) is handled for you.

## A complete example: GitHub notifications

This connector uses the GitHub CLI's existing login (`gh auth login`), so it needs no secret. Save it as
`csl/connectors/github.py`:

```python
"""GitHub notifications connector: review requests, mentions and assignments from `gh api notifications`.

No secret — authentication is the GitHub CLI's own login (`gh auth login`). Only titles and links are collected.
"""
import datetime
import json
import shutil
import subprocess

from csl.connectors import Connector, Field, Item

OVERLAP = 5 * 60   # look a little before the last item each time; duplicates are dropped by key


def _gh(*args):
    r = subprocess.run([shutil.which("gh") or "gh", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("gh failed: " + (r.stderr or r.stdout).strip()[:200] + " — try `gh auth login`")
    return json.loads(r.stdout or "null")


def _ts(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _html(api_url, repo):
    # https://api.github.com/repos/o/r/pulls/12 → https://github.com/o/r/pull/12
    if not api_url:
        return "https://github.com/" + repo
    return api_url.replace("https://api.github.com/repos/", "https://github.com/").replace("/pulls/", "/pull/")


class GitHub(Connector):
    type = "github"                      # unique id; stored with every item as its "source"
    name = "GitHub"
    icon = "🐙"
    description = "Review requests, mentions and assignments (via the gh CLI login)"
    default_every = 5                    # minutes between polls; 0 = only when you press "fetch now"
    fields = [
        Field("reasons", "Reasons", "text", default="review_requested,mention,assign",
              help="Comma-separated notification reasons to keep"),
        Field("host", "Host", "text", default="github.com", required=False,
              help="Your GitHub Enterprise host, if any"),
    ]

    def test(self, ctx):
        """Check the connection and return something short to show (the account). Raise on failure."""
        me = _gh("api", "--hostname", ctx.get("host", "github.com"), "user")
        ctx.state["account"] = me["login"]          # ctx.state is saved for you after test() and poll()
        return me["login"]

    def poll(self, ctx):
        """Return the Items that arrived since ctx.since (unix seconds; 0 on the first run)."""
        if not ctx.state.get("account"):
            self.test(ctx)
        keep = {r.strip() for r in ctx.get("reasons", "").split(",") if r.strip()}
        since = max(0, ctx.since - OVERLAP)
        out = []
        for n in _gh("api", "--hostname", ctx.get("host", "github.com"), "notifications") or []:
            ts = _ts(n["updated_at"])
            if ts <= since or (keep and n.get("reason") not in keep):
                continue
            repo = n["repository"]["full_name"]
            out.append(Item(
                key=n["id"] + ":" + n["updated_at"],    # unique within this instance; same key = same item
                ts=ts,
                title=n["subject"]["title"],
                author=repo,                             # "who" — here the repository
                text="",                                 # optional body; masked and cut to 6,000 chars
                link=_html(n["subject"].get("url"), repo),
                kind=n.get("reason", ""),                # free-form: helps the Brain sort (e.g. review_requested)
                account=ctx.state.get("account", ""),
            ))
        return out


CONNECTOR = GitHub   # the registry looks for this name
```

Restart the board (`./ctl.sh restart`), open ⋮ → 🔌 커넥터, add **GitHub** (saving it also runs `test()`), then press
**지금 가져오기** (Fetch now); **연결 확인** (Check connection) runs `test()` again. `./argus inbox raw` (from the `brain/` folder) shows what was collected.

## The API

### `Connector` (subclass it)

| Attribute / method | Required | What it is |
|---|---|---|
| `type` | yes | Unique id (`"github"`). Also the `source` value the Brain sees. |
| `name`, `icon`, `description` | yes | Shown in the connector window. |
| `fields` | — | Settings form, a list of `Field`s (below). |
| `default_every` | — | Minutes between polls for a new instance (the user can change it). `0` = manual only. |
| `role` | — | `"inbox"` (default) collects items. `"info"` collects nothing; the Brain calls `query()` instead. |
| `test(ctx)` | recommended | Verify the connection and return a short label (account, workspace). Raise to report an error. |
| `poll(ctx)` | yes (inbox) | Return a list of new `Item`s. Raise to report an error — other connectors keep running. |
| `query(ctx, *args)` | yes (info) | Return whatever the Brain asked for (see "Info connectors"). |

### `Field(key, label, kind="text", default="", help="", required=True)`

`kind` is one of `text`, `textarea`, `int`, `bool`, `secret`.

**Secrets** (`kind="secret"`) never touch the settings file or logs: the value goes straight into the macOS Keychain
(`argus-connector-<instance>-<key>`, written through `security -i` so it never appears in a process list). Users can
also type `keychain:<item name>` to reuse an item they already have. Read it with `ctx.secret("token")`. Deleting the
instance deletes the Keychain items Argus created, and leaves ones the user brought.

### `Item(key, ts, title, author="", text="", link="", kind="", account="")`

| Field | Notes |
|---|---|
| `key` | Unique within the instance. Re-returning the same key is harmless — it is ignored. |
| `ts` | Unix seconds of the item. The newest `ts` you return becomes the next `ctx.since`. |
| `title` | One line; cut to 300 characters. |
| `author`, `account`, `kind` | Free-form; they help the Brain decide who it is from and what it is. |
| `text` | Optional body. Secrets are masked; cut to 6,000 characters. Prefer leaving it empty if you don't need it. |
| `link` | Where the card's "원문 ↗" (Original ↗) button goes. |

### `ctx`

| | |
|---|---|
| `ctx.get(key, default)` | A setting from `fields` (empty values return `default`). |
| `ctx.secret(key)` | A secret field's value, read from the Keychain. |
| `ctx.since` | Unix seconds of the newest item collected so far (`0` the first time). |
| `ctx.state` | A dict saved per instance after each `test()` / `poll()` — cursors, account name, caches. `last`, `seen`, `polled_at`, `error`, `added` are used by Argus; pick other names. |

## Info connectors

Set `role = "info"`, return `[]` from `poll()`, and implement `query(ctx, *args)`. Argus calls it through
`connectors.query(conn, "<type>", *args)` on every enabled instance of that type. Today the wiring from the Brain to a
query is per kind: the calendar has a `GET /api/calendar` route in `csl/server.py` and a `./argus calendar` command in
`csl/cli.py`. A new info connector needs the same two small pieces, plus a line in `brain/CLAUDE.md` saying when the
Brain should use it.

## Teaching the Brain about a new source

The Brain reads raw items generically, so a new inbox connector works without changes. If items from your source need
special handling — "ignore bot notifications", "a review request is always a session task" — add a short paragraph to
the inbox procedure in `brain/CLAUDE.md`, next to the Slack and Gmail notes.

## Guidelines

- **Collect as little as you can.** Titles, senders and links are usually enough; the Gmail connector deliberately
  skips message bodies. Whatever you return is stored locally until the Brain processes it.
- **Read-only.** Ask for read scopes only. A connector never posts, replies or marks things read.
- **Resume, don't re-scan.** Use `ctx.since` (minus a small overlap) or a cursor in `ctx.state`, and cap how far back
  you go after a long sleep (the Slack connector stops at 7 days).
- **Fail loudly, but alone.** Raise with a short, actionable message ("try `gh auth login`"); it is shown in the
  connector window, and the other connectors keep polling.
- **Standard library only.** Call an existing CLI (`gh`, `gws`) or `urllib.request` — no new dependencies.
- **Timeouts everywhere.** Every subprocess or HTTP call should have one; `poll()` runs on the server's scheduler.
