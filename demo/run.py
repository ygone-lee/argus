#!/usr/bin/python3
"""Argus demo — the real board, filled with made-up sessions.

    /usr/bin/python3 demo/run.py              # http://127.0.0.1:7798
    /usr/bin/python3 demo/run.py --port 7797  # another port
    /usr/bin/python3 demo/run.py --loop       # replay the live part every few minutes (for recording)

It creates a throwaway folder (a data folder and a few tiny git repositories), feeds the real hook code a short script
of Claude Code events — prompts, tool calls, replies, a permission prompt, a question — and serves the real board from
that data. Your own Argus data, Claude Code sessions and terminals are never read or touched: the data folder and port
are separate, terminal lookups are switched off, and the folder is deleted on exit (unless --keep).

Messages you send from a column's message box get a canned reply. Other terminal actions (stop, model, rename, rewind,
answering prompts …) are not simulated and just report an error.
"""
import argparse
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ap = argparse.ArgumentParser(description="Run the Argus board with made-up sessions.")
ap.add_argument("--port", type=int, default=7798)
ap.add_argument("--keep", action="store_true", help="keep the temp folder on exit")
ap.add_argument("--dir", help="make the throwaway folder inside this folder instead of the system temp folder")
ap.add_argument("--loop", action="store_true", help="replay the live part every few minutes")
ARGS = ap.parse_args()

if ARGS.dir:
    os.makedirs(ARGS.dir, exist_ok=True)
BASE = os.path.realpath(tempfile.mkdtemp(prefix="argus-demo-", dir=ARGS.dir))
DATA = os.path.join(BASE, "data")
PROJECTS = os.path.join(BASE, "projects")
TRANSCRIPTS = os.path.join(BASE, "transcripts")

# Before importing Argus: its own data folder and port, and nothing that ties a session to a real terminal.
os.environ["CSL_DATA_DIR"] = DATA
os.environ["CSL_PORT"] = str(ARGS.port)
for k in list(os.environ):
    if k.startswith(("ORCA_", "ITERM_", "CLAUDE_CODE_", "CLAUDECODE")) or k in ("TERM_PROGRAM", "TERM_SESSION_ID", "__CFBundleIdentifier"):
        del os.environ[k]

sys.path.insert(0, ROOT)
from csl import focus, hook, server, store  # noqa: E402

# ------------------------------------------------------------------ switch off everything that looks outside the demo
hook.claude_ancestry = lambda: []
hook.ancestor_tty = lambda: None
hook.record_terminal = lambda *a, **k: None
hook.ensure_server = lambda: None
server.agents_list = lambda fresh=False: []      # `claude agents` would list your real background sessions

OFFSET = [0.0]                                    # seconds in the past while seeding history
_real_time = time.time
store.now = lambda: _real_time() - OFFSET[0]

MODEL = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ tiny git repositories
GITENV = {**os.environ, "GIT_AUTHOR_NAME": "Argus Demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
          "GIT_COMMITTER_NAME": "Argus Demo", "GIT_COMMITTER_EMAIL": "demo@example.com",
          "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, env=GITENV, check=True, capture_output=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def repo(name, files):
    d = os.path.join(PROJECTS, name)
    for rel, text in files.items():
        write(os.path.join(d, rel), text)
    git(d, "init", "-q", "-b", "main")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "Initial commit")
    return d


def make_projects():
    api = repo("shop-api", {
        "README.md": "# shop-api\n\nOrders, payments and webhooks.\n",
        "src/webhooks/retry.py": (
            "import time\n\n\n"
            "def deliver(send, event, attempts=3):\n"
            "    for _ in range(attempts):\n"
            "        if send(event):\n"
            "            return True\n"
            "        time.sleep(1)\n"
            "    return False\n"),
        "tests/test_retry.py": "from src.webhooks.retry import deliver\n\n\ndef test_first_try():\n    assert deliver(lambda e: True, {})\n",
    })
    # uncommitted work, so the column shows the 🧾 changes chip and the diff viewer has something to show
    write(os.path.join(api, "src/webhooks/retry.py"), (
        "import random\nimport time\n\n\n"
        "def backoff(attempt, base=0.5, cap=30.0):\n"
        "    \"\"\"Exponential backoff with full jitter.\"\"\"\n"
        "    return random.uniform(0, min(cap, base * 2 ** attempt))\n\n\n"
        "def deliver(send, event, attempts=5, sleep=time.sleep):\n"
        "    for attempt in range(attempts):\n"
        "        if send(event):\n"
        "            return True\n"
        "        sleep(backoff(attempt))\n"
        "    return False\n"))
    write(os.path.join(api, "tests/test_backoff.py"), (
        "from src.webhooks.retry import backoff, deliver\n\n\n"
        "def test_backoff_is_capped():\n    assert all(backoff(n, cap=5) <= 5 for n in range(20))\n\n\n"
        "def test_gives_up_after_attempts():\n    calls = []\n"
        "    assert not deliver(lambda e: calls.append(e) and False, {}, attempts=3, sleep=lambda s: None)\n"
        "    assert len(calls) == 3\n"))

    web = repo("web", {
        "package.json": "{\n  \"name\": \"web\",\n  \"scripts\": {\"test\": \"vitest run\"}\n}\n",
        "src/LoginForm.tsx": (
            "export function LoginForm({ onSubmit }) {\n"
            "  return (\n    <form onSubmit={onSubmit}>\n"
            "      <input name=\"email\" />\n      <input name=\"password\" type=\"password\" />\n"
            "      <button>Sign in</button>\n    </form>\n  );\n}\n"),
    })
    wt = os.path.join(web, ".claude", "worktrees", "login-validation")
    git(web, "worktree", "add", "-q", "-b", "feat/login-validation", wt)
    write(os.path.join(wt, "src/LoginForm.tsx"), (
        "import { useState } from \"react\";\n\n"
        "const EMAIL = /^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/;\n\n"
        "export function LoginForm({ onSubmit }) {\n"
        "  const [error, setError] = useState(\"\");\n"
        "  const submit = (e) => {\n    const f = new FormData(e.currentTarget);\n"
        "    if (!EMAIL.test(String(f.get(\"email\")))) { e.preventDefault(); return setError(\"Enter a valid email\"); }\n"
        "    if (String(f.get(\"password\")).length < 8) { e.preventDefault(); return setError(\"At least 8 characters\"); }\n"
        "    onSubmit(e);\n  };\n"
        "  return (\n    <form onSubmit={submit} noValidate>\n"
        "      <input name=\"email\" type=\"email\" aria-invalid={!!error} />\n"
        "      <input name=\"password\" type=\"password\" />\n"
        "      {error && <p role=\"alert\">{error}</p>}\n"
        "      <button>Sign in</button>\n    </form>\n  );\n}\n"))

    infra = repo("infra", {
        "main.tf": "module \"network\" {\n  source = \"./modules/network\"\n  az_count = 3\n}\n",
        "modules/network/nat.tf": "resource \"aws_nat_gateway\" \"this\" {\n  count = var.az_count\n}\n",
    })
    docs = repo("docs", {"CHANGELOG.md": "# Changelog\n\n## 2.0.0\n- New auth flow\n- Webhook retries\n"})
    mobile = repo("mobile", {"package.json": "{\n  \"name\": \"mobile\",\n  \"dependencies\": {\"push-sdk\": \"^4.2.0\"}\n}\n"})
    return {"api": api, "web": wt, "infra": infra, "docs": docs, "mobile": mobile}


# ------------------------------------------------------------------ a session = hook events + a transcript file
class Session:
    def __init__(self, cwd, title, model="opus", effort="high", ctx=30, mode="default"):
        self.sid = str(uuid.uuid4())
        self.cwd, self.model, self.effort, self.ctx, self.mode = cwd, MODEL[model], effort, ctx, mode
        self.path = os.path.join(TRANSCRIPTS, self.sid + ".jsonl")
        self.line({"type": "ai-title", "aiTitle": title, "sessionId": self.sid})

    def line(self, d):
        os.makedirs(TRANSCRIPTS, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(d) + "\n")

    def event(self, name, **data):
        hook.handle({"hook_event_name": name, "session_id": self.sid, "cwd": self.cwd, "transcript_path": self.path,
                     "permission_mode": self.mode, "effort": {"level": self.effort}, **data})

    def status(self):
        """What the status line tap would save: context window use and the account's usage limits."""
        now = store.now()
        write(os.path.join(DATA, "status", self.sid + ".json"), json.dumps({
            "ctx_size": 200000, "ctx_pct": self.ctx, "ctx_used": self.ctx * 2000, "model": self.model,
            "rate_limits": {"five_hour": {"used_percentage": 38, "resets_at": now + 2 * 3600 + 14 * 60},
                            "seven_day": {"used_percentage": 21, "resets_at": now + 4 * 86400}}}))

    def start(self):
        self.event("SessionStart", source="startup", model=self.model)
        self.status()

    def prompt(self, text):
        self.event("UserPromptSubmit", prompt=text, prompt_id=str(uuid.uuid4()))

    def assistant(self, *blocks):
        self.line({"type": "assistant", "timestamp": iso(store.now()), "sessionId": self.sid,
                   "message": {"model": self.model, "role": "assistant", "content": list(blocks),
                               "usage": {"input_tokens": 12, "cache_read_input_tokens": self.ctx * 2000}}})

    def tool(self, name, **inp):
        self.assistant({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": name, "input": inp})

    def say(self, text):
        self.assistant({"type": "text", "text": text})

    def reply(self, text):
        self.say(text)
        self.ctx = min(90, self.ctx + 4)
        self.status()
        self.event("Stop", last_assistant_message=text)

    def seen(self):
        with store.connect() as conn:
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (store.now() + 1, self.sid))

    def note(self, text, done=False):
        t = store.now()
        with store.connect() as conn:
            conn.execute("INSERT INTO notes (session_id, text, done, created_at, updated_at, done_at) VALUES (?,?,?,?,?,?)",
                         (self.sid, text, int(done), t, t, t if done else None))

    def recap(self, text):
        self.line({"type": "system", "subtype": "away_summary", "content": text, "timestamp": iso(store.now())})
        with store.connect() as conn:
            store.apply_transcript_meta(conn, self.sid, self.path)


def at(minutes_ago):
    OFFSET[0] = minutes_ago * 60


# ------------------------------------------------------------------ history (what happened before you opened the board)
def seed(p):
    s = {}
    docs = s["docs"] = Session(p["docs"], "Migration guide for v2", model="sonnet", effort="medium", ctx=18)
    at(190); docs.start(); docs.prompt("Draft a migration guide for v2 from CHANGELOG.md — breaking changes first.")
    at(188); docs.tool("Read", file_path=p["docs"] + "/CHANGELOG.md")
    at(186); docs.reply("Drafted **docs/migrating-to-v2.md**:\n\n1. Breaking changes — new auth flow, webhook payload v2\n"
                        "2. Step-by-step upgrade with code samples\n3. FAQ\n\nScreenshots of the new sign-in screen are still placeholders.")
    docs.seen()
    docs.note("Ask design for the new sign-in screenshots")
    docs.note("Check every link before publishing")
    at(150); docs.recap("Drafted the v2 migration guide; screenshots are placeholders and links are unchecked.")

    infra = s["infra"] = Session(p["infra"], "NAT gateway replaced on every plan", model="opus", effort="xhigh", ctx=46)
    at(52); infra.start(); infra.prompt("Why does `terraform plan` want to replace the NAT gateway every time?")
    at(51); infra.tool("Read", file_path=p["infra"] + "/modules/network/nat.tf")
    at(50); infra.tool("Bash", command="terraform plan -no-color | grep -A3 nat_gateway", description="Show the NAT gateway diff")
    at(48); infra.reply("The NAT gateway is replaced because `count = var.az_count` re-indexes the gateways whenever the list of "
                        "availability zones changes order.\n\n**Fix:** switch to `for_each` keyed by AZ name, then move the existing "
                        "state with `terraform state mv` so nothing is recreated. Want me to make the change and write the `state mv` commands?")

    api = s["api"] = Session(p["api"], "Webhook retries with backoff", model="opus", effort="high", ctx=34, mode="acceptEdits")
    at(31); api.start(); api.prompt("Add exponential backoff with jitter to webhook retries, and cover it with tests.")
    at(30); api.tool("Read", file_path=p["api"] + "/src/webhooks/retry.py")
    at(29); api.tool("Edit", file_path=p["api"] + "/src/webhooks/retry.py", old_string="time.sleep(1)", new_string="sleep(backoff(attempt))")
    at(28); api.tool("Write", file_path=p["api"] + "/tests/test_backoff.py", content="…")
    at(27); api.tool("Bash", command="pytest -q", description="Run the tests")
    at(26); api.reply("Done. `deliver()` now waits `backoff(attempt)` — exponential with full jitter, capped at 30 s — and tries "
                      "5 times instead of 3.\n\nAdded `tests/test_backoff.py` (cap and give-up cases). **12 passed.** Not committed yet.")
    api.seen()
    api.note("Mention the retry change in the release notes")

    mobile = s["mobile"] = Session(p["mobile"], "Upgrade push SDK to v5", model="sonnet", effort="medium", ctx=22)
    at(12); mobile.start(); mobile.prompt("Upgrade the push notification SDK to v5.")
    at(11); mobile.tool("Read", file_path=p["mobile"] + "/package.json")
    at(10); mobile.tool("WebSearch", query="push-sdk v5 migration guide breaking changes")
    at(9); mobile.say("v5 drops the legacy token API. Before I change anything, one question:")
    mobile.event("PreToolUse", tool_name="AskUserQuestion", tool_use_id="toolu_demo_question", tool_input={"questions": [{
        "question": "v5 removes the legacy token API. How should existing installs migrate?",
        "header": "Migration", "multiSelect": False, "options": [
            {"label": "Re-register on next launch (Recommended)", "description": "Simplest; tokens refresh silently on first open"},
            {"label": "Keep a v4 fallback for one release", "description": "Safer rollout, more code to delete later"},
            {"label": "Force re-login", "description": "Cleanest state, but users notice"}]}]})

    web = s["web"] = Session(p["web"], "Login form validation", model="opus", effort="high", ctx=27)
    at(4); web.start(); web.prompt("Add client-side validation to the login form: valid email, password at least 8 characters, "
                                   "accessible error message.")
    at(3); web.tool("Read", file_path=p["web"] + "/src/LoginForm.tsx")
    OFFSET[0] = 0
    return s


# ------------------------------------------------------------------ the live part: things change while you watch
def live(s, p, round_no=0):
    web, api, infra = s["web"], s["api"], s["infra"]
    steps = [
        (2, lambda: web.say("I'll validate on submit and keep the browser's own checks off (`noValidate`) so the message is consistent.")),
        (3, lambda: web.tool("Edit", file_path=p["web"] + "/src/LoginForm.tsx", old_string="<form onSubmit={onSubmit}>",
                             new_string="<form onSubmit={submit} noValidate>")),
        (4, lambda: api.prompt(["Run the full test suite and summarize anything flaky.",
                                "Check the retry change against the payments integration tests.",
                                "Write a short summary of today's changes for the PR."][round_no % 3])),
        (3, lambda: web.tool("Write", file_path=p["web"] + "/src/LoginForm.test.tsx", content="…")),
        (2, lambda: api.tool("Bash", command="pytest -q --count=3", description="Run the suite three times")),
        (3, lambda: web.tool("Bash", command="npm test -- LoginForm", description="Run the login form tests")),
        (0, lambda: web.event("PermissionRequest", tool_name="Bash", tool_input={"command": "npm test -- LoginForm"})),
        (7, lambda: web.event("PostToolUse", tool_name="Bash")),
        (3, lambda: api.tool("Read", file_path=p["api"] + "/tests/test_backoff.py")),
        (3, lambda: web.reply("Validation added:\n\n- email must look like `name@domain.tld`\n- password needs 8+ characters\n"
                              "- the error is announced with `role=\"alert\"` and the email field gets `aria-invalid`\n\n"
                              "**6 tests pass** (3 new). Ready for review on `feat/login-validation`.")),
        (4, lambda: api.reply("Ran the suite 3×: **36 passed, 0 flaky.** The slowest test is `test_gives_up_after_attempts` "
                              "(0.4 s) — it now injects `sleep`, so it no longer waits for real.")),
    ]
    for delay, fn in steps:
        time.sleep(delay)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep the demo going
            print("demo step failed:", e, file=sys.stderr)


# ------------------------------------------------------------------ "sending" from the board: a canned reply
SESS_BY_ID = {}
CANNED = [
    ("Sure — on it.", "Done. I made the change, ran the related tests (**all green**) and left it uncommitted for you to review."),
]


def fake_send(handle, text, *a, **k):
    sess = SESS_BY_ID.get(handle)
    if not sess:
        return False, [], "unknown demo session"

    def run():
        sess.prompt(text)
        time.sleep(1.5)
        sess.say(CANNED[0][0])
        time.sleep(1.5)
        sess.tool("Read", file_path=sess.cwd + "/README.md")
        time.sleep(2)
        sess.tool("Bash", command="make test", description="Run the tests")
        time.sleep(2.5)
        sess.reply(CANNED[0][1])
    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.3)   # let the prompt land before the board refreshes
    return True, ["turn_started"], None


def patch_terminal():
    def target(conn, sess, need_alive=True):
        sid = sess["session_id"]
        return (sid, None) if sid in SESS_BY_ID and sess["state"] != "ended" else (None, "Not available in the demo")
    server.terminal_target = target
    focus.dialog_text = lambda *a, **k: None
    focus.input_line = lambda *a, **k: ""
    focus.send_prompt = lambda handle, text, *a, **k: fake_send(handle, text)
    focus.type_text = lambda handle, text, paths=None, *a, **k: fake_send(handle, text)[::2]


# ------------------------------------------------------------------
def main():
    print("Argus demo: building made-up projects and sessions in", BASE)
    p = make_projects()
    store.connect().close()
    s = seed(p)
    SESS_BY_ID.update({x.sid: x for x in s.values()})
    patch_terminal()

    httpd = server.ThreadingHTTPServer(("127.0.0.1", ARGS.port), server.Handler)
    threading.Thread(target=server._dirty_loop, daemon=True).start()

    def play():
        n = 0
        while True:
            time.sleep(4)
            live(s, p, n)
            if not ARGS.loop:
                return
            time.sleep(150)
            n += 1
            for x in (s["web"], s["api"]):   # start the next round from a fresh prompt
                x.prompt(["Tidy up the commit history before review.", "Add a changelog entry.",
                          "Double-check edge cases."][n % 3])
    threading.Thread(target=play, daemon=True).start()

    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))   # kill → clean up too
    print("Argus demo: http://127.0.0.1:%d   (Ctrl+C to stop)" % ARGS.port, flush=True)
    print("  English UI: ⋮ → 🌐 → English")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if ARGS.keep:
            print("kept", BASE)
        else:
            shutil.rmtree(BASE, ignore_errors=True)


if __name__ == "__main__":
    main()
