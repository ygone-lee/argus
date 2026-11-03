"""~/.claude/settings.json 에 Argus 훅·상태줄 탭을 넣거나(기본) 뺀다(--uninstall). --check 는 설치 상태만 본다.

    /usr/bin/python3 install.py            # 설치(여러 번 실행해도 한 벌만 유지)
    /usr/bin/python3 install.py --check    # 설치 상태 점검(아무것도 바꾸지 않음)
    /usr/bin/python3 install.py --uninstall

다른 훅(Orca 등)과 설정은 그대로 두고 이 도구의 훅 항목만 고친다. settings.json 이 없으면 새로 만들고, JSON 이 깨져
있으면 어디가 틀렸는지 알려 주고 아무것도 바꾸지 않는다.
상태줄 탭(csl/statusline_tap.py): 상태줄 입력에서 세션별 컨텍스트 %·사용 한도를 받아 둔다. 상태줄이 없으면 탭을
상태줄 명령으로 두고(화면엔 아무것도 안 나옴), 있으면 원래 명령을 감싼다(같은 입력·출력 그대로). 상태줄 명령이나
그 스크립트에 이미 탭이 들어 있으면 건드리지 않는다. 원래 상태줄 설정은 statusline-original.json 에 두고 제거 때 되돌린다.
바꾸기 전 원본을 ~/.claude/session-log/settings.json.bak-<시각> 으로 남긴다.
스킬(skills/*): ~/.claude/skills/<이름> 에 저장소 폴더를 가리키는 링크를 둔다(모든 세션이 쓴다). 같은 이름의 진짜
폴더가 이미 있으면 건드리지 않는다.
화면에 내는 글은 영어(README 와 같은 말), 코드 주석은 한국어.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "csl", "hook.py")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
BACKUP_DIR = os.path.expanduser("~/.claude/session-log")
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
TAP = os.path.join(HERE, "csl", "statusline_tap.py")
TAP_MARK = "statusline_tap.py"
ORIGINAL = os.path.join(BACKUP_DIR, "statusline-original.json")
SKILLS_SRC = os.path.join(HERE, "skills")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")
MARK = "/csl/hook.py"   # 저장소 폴더 이름이 바뀌어도(예전 claude-session-log) 이 도구의 훅으로 알아본다
PORT = int(os.environ.get("CSL_PORT", "7799"))
DATA_DIR = os.path.expanduser(os.environ.get("CSL_DATA_DIR", "~/.claude/session-log"))

# (이벤트, matcher, async) — 요청 기록은 순서가 중요해 동기, 나머지는 Claude 를 막지 않게 async
HOOKS = [
    ("SessionStart", None, True),
    ("UserPromptSubmit", None, False),
    ("Stop", None, True),
    ("StopFailure", None, True),
    ("Notification", None, True),
    ("PermissionRequest", None, True),
    ("PreToolUse", "AskUserQuestion|ExitPlanMode", True),
    ("PostToolUse", "*", True),
    ("SessionEnd", None, False),
    # 보드가 요청한 모델 전환만 확인 창 없이(출력이 필요해 동기), 전환 결과 기록
    ("PreModelSwitch", None, False),
    ("PostModelSwitch", None, True),
    # 컴팩트 시작·끝(「컴팩트 중」 표시)
    ("PreCompact", None, True),
    ("PostCompact", None, True),
]


def home(path):
    """화면에 보일 경로: 홈 폴더는 ~ 로."""
    h = os.path.expanduser("~")
    return "~" + path[len(h):] if path == h or path.startswith(h + os.sep) else path


def say(text=""):
    print(text, flush=True)


def fail(title, *lines):
    say("✗ " + title)
    for line in lines:
        say("  " + line)
    sys.exit(1)


# ---------------------------------------------------------------- 미리 확인
def claude_bin():
    for p in (shutil.which("claude"), os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/.claude/local/claude")):
        if p and os.path.isfile(p):
            return p
    return None


def preflight(force=False):
    """설치 전에 확인. 막아야 하는 것은 멈추고, 알기만 하면 되는 것은 경고 목록으로."""
    if sys.platform != "darwin" and not force:
        fail("Argus runs on macOS only.",
             "It uses macOS features for the terminal control, the Keychain and file lookup.",
             "Run with --force to install the hooks anyway (recording should work; driving terminals won't).")
    if sys.version_info < (3, 9):
        fail("Python 3.9 or later is needed (this is %d.%d)." % sys.version_info[:2],
             "Use the macOS system Python: /usr/bin/python3 install.py")
    warnings = []
    if not claude_bin():
        warnings.append("Claude Code (`claude`) wasn't found. Argus records Claude Code sessions, so install it first:"
                        " https://docs.anthropic.com/en/docs/claude-code — the hooks below still get installed.")
    return warnings


def load_settings():
    """(설정 dict, 새로 만들었는지). 깨진 JSON 은 어디가 틀렸는지 알려 주고 멈춘다(아무것도 바꾸지 않음)."""
    if not os.path.exists(SETTINGS):
        return {}, True
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            text = f.read()
        return (json.loads(text) if text.strip() else {}), False
    except ValueError as e:
        fail("%s isn't valid JSON, so nothing was changed." % home(SETTINGS),
             "Line %s, column %s: %s" % (getattr(e, "lineno", "?"), getattr(e, "colno", "?"), getattr(e, "msg", e)),
             "Fix that line (comments and trailing commas aren't allowed in JSON) and run the installer again.")
    except OSError as e:
        fail("Couldn't read %s: %s" % (home(SETTINGS), e))


# ---------------------------------------------------------------- 훅·상태줄·스킬
def strip_ours(hooks):
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if MARK not in (h.get("command") or "")]
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def count_ours(settings):
    return sum(1 for groups in (settings.get("hooks") or {}).values() for g in groups
               for h in g.get("hooks", []) if MARK in (h.get("command") or ""))


def _script_has_tap(command):
    """상태줄 명령이 가리키는 스크립트에 이미 탭 줄을 넣어 두었는지(직접 넣은 경우)."""
    try:
        first = os.path.expandvars(os.path.expanduser(shlex.split(command)[0]))
        if os.path.isfile(first) and os.path.getsize(first) < 256 * 1024:
            with open(first, encoding="utf-8", errors="replace") as f:
                return TAP_MARK in f.read()
    except (ValueError, IndexError, OSError):
        pass
    return False


def install_statusline(settings):
    tap = "%s %s" % (PYTHON, shlex.quote(TAP))
    current = settings.get("statusLine")
    command = (current or {}).get("command") or ""
    if TAP_MARK in command and TAP not in command:
        # 저장소를 옮긴 경우: 감싼 명령은 그대로 두고 탭 경로만 새 위치로
        # 따옴표로 감싼 경로(공백 포함)는 따옴표째, 아니면 공백 전까지 — 앞부분만 바꿔 명령이 깨지던 것
        mark = re.escape(TAP_MARK)
        new = re.sub(r"'[^']*%s'|\"[^\"]*%s\"|\S*%s" % (mark, mark, mark), lambda m: shlex.quote(TAP), command, count=1)
        settings["statusLine"] = dict(current, command=new)
        return "updated to this folder (the repository moved)"
    if TAP_MARK in command or _script_has_tap(command):
        return "already connected"
    if not current or current.get("type") != "command" or not command:
        settings["statusLine"] = {"type": "command", "command": tap}
        return "added (you had none; it shows nothing, it only reads context % and usage limits)"
    with open(ORIGINAL, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    settings["statusLine"] = dict(current, command="%s --wrap %s" % (tap, shlex.quote(command)))
    return "wrapped your existing one — it looks and works the same (%s)" % command


def uninstall_statusline(settings):
    command = (settings.get("statusLine") or {}).get("command") or ""
    if TAP_MARK not in command:
        return "unchanged (if you added the tap line to your own status line script, remove that line yourself)"
    if " --wrap " in command:
        try:
            with open(ORIGINAL, encoding="utf-8") as f:
                settings["statusLine"] = json.load(f)
            os.remove(ORIGINAL)
        except (OSError, ValueError):
            settings["statusLine"] = dict(settings["statusLine"], command=shlex.split(command)[-1])
        return "restored to your original command"
    del settings["statusLine"]
    return "removed"


def sync_skills(remove=False):
    """저장소의 skills/* 를 ~/.claude/skills 에 링크(제거 때는 이 저장소를 가리키는 링크만 지운다)."""
    done, skipped = [], []
    for name in sorted(os.listdir(SKILLS_SRC)) if os.path.isdir(SKILLS_SRC) else []:
        src, dst = os.path.join(SKILLS_SRC, name), os.path.join(SKILLS_DIR, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            continue
        if os.path.islink(dst):
            if remove and os.path.realpath(dst) != os.path.realpath(src):
                continue   # 제거할 땐 이 저장소를 가리키는 링크만 지운다(같은 이름의 다른 스킬 링크는 그대로)
            os.remove(dst)   # 저장소를 옮겼으면 옛 링크를 새로
        elif os.path.exists(dst):
            skipped.append(name)
            continue
        if not remove:
            os.makedirs(SKILLS_DIR, exist_ok=True)
            os.symlink(src, dst)
        done.append(name)
    out = ", ".join(done) or "none"
    if skipped:
        out += " (skipped %s: a folder with that name already exists)" % ", ".join(skipped)
    return out


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".csl-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)


# ---------------------------------------------------------------- 상태 점검
def server_up():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


def session_count():
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(DATA_DIR, "board.db"), uri=True, timeout=2)
        try:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def check():
    """설치 상태를 보여 주기만 한다."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    n, cmd = count_ours(settings), (settings.get("statusLine") or {}).get("command") or ""
    sessions = session_count()
    rows = [
        ("macOS", sys.platform == "darwin", "" if sys.platform == "darwin" else "Argus supports macOS only"),
        ("Python %d.%d" % sys.version_info[:2], sys.version_info >= (3, 9), "" if sys.version_info >= (3, 9) else "needs 3.9+"),
        ("Claude Code", bool(claude_bin()), home(claude_bin() or "") or "not found — install Claude Code first"),
        ("Hooks", n == len(HOOKS), "%d of %d in %s" % (n, len(HOOKS), home(SETTINGS)) + ("" if n else " — run install.py")),
        ("Status line tap", TAP_MARK in cmd or _script_has_tap(cmd), "" if TAP_MARK in cmd or _script_has_tap(cmd)
         else "not connected — context % and usage bars will be estimated or hidden"),
        ("Board server", server_up(), "http://127.0.0.1:%d" % PORT if server_up() else "not running — ./ctl.sh open"),
        ("Recorded sessions", bool(sessions), str(sessions) if sessions is not None else "none yet — send a prompt in Claude Code"),
    ]
    say("Argus install check\n")
    for name, ok, note in rows:
        say("  %s %-18s %s" % ("✓" if ok else "·", name, note))
    say()
    if n != len(HOOKS):
        say("Run /usr/bin/python3 install.py to (re)install the hooks.")
    elif not sessions:
        say("All set — send any prompt in Claude Code and it shows up on the board.")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Install Argus: add its hooks, status line tap and skill to Claude Code (~/.claude/settings.json). "
                    "Safe to run again; your other hooks and settings are kept, and a backup is saved first.")
    ap.add_argument("--uninstall", action="store_true", help="remove the hooks, the status line tap and the skill link "
                    "(your recorded data in ~/.claude/session-log is kept)")
    ap.add_argument("--check", action="store_true", help="show what is installed and running; changes nothing")
    ap.add_argument("--open", action="store_true", help="start the board and open it in the browser when done, without asking")
    ap.add_argument("--no-open", action="store_true", help="don't offer to open the board")
    ap.add_argument("--force", action="store_true", help="install even if this isn't macOS")
    args = ap.parse_args()

    if args.check:
        return check()

    warnings = [] if args.uninstall else preflight(args.force)
    settings, created = load_settings()
    if args.uninstall and created:
        say("Nothing to remove: %s doesn't exist." % home(SETTINGS))
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = None
    if not created:
        backup = os.path.join(BACKUP_DIR, "settings.json.bak-%d" % int(time.time()))
        shutil.copy2(SETTINGS, backup)

    hooks = settings.setdefault("hooks", {})
    strip_ours(hooks)
    if not args.uninstall:
        command = "%s %s" % (shlex.quote(PYTHON), shlex.quote(HOOK))   # 경로에 공백이 있어도 되게
        for event, matcher, is_async in HOOKS:
            entry = {"type": "command", "command": command, "timeout": 5}
            if is_async:
                entry["async"] = True
            group = {"hooks": [entry]}
            if matcher:
                group = {"matcher": matcher, "hooks": [entry]}
            hooks.setdefault(event, []).append(group)
    if not hooks:
        del settings["hooks"]
    statusline = uninstall_statusline(settings) if args.uninstall else install_statusline(settings)
    skills = sync_skills(remove=args.uninstall)
    save_settings(settings)

    if args.uninstall:
        say("Argus removed ✓\n")
        say("  Hooks        removed from %s (your other hooks are untouched)" % home(SETTINGS))
        say("  Status line  %s" % statusline)
        say("  Skill links  removed: %s" % skills)
        if backup:
            say("  Backup       %s" % home(backup))
        say("\nYour recorded data is still in %s — delete that folder to remove it too." % home(DATA_DIR))
        if server_up():
            say("The board is still running; stop it with: ./ctl.sh stop")
        return

    say("Argus installed ✓\n")
    say("  Hooks        %d Claude Code hooks %s %s%s" % (len(HOOKS), "written to new" if created else "added to", home(SETTINGS),
                                                      "" if created else " (your other hooks are untouched)"))
    say("  Status line  %s" % statusline)
    say("  Skill        %s linked into %s" % (skills, home(SKILLS_DIR)))
    if backup:
        say("  Backup       %s" % home(backup))
    for w in warnings:
        say("\n! " + w)
    # 저장소 밖(한 줄 설치 등)에서 실행했으면 명령을 저장소 경로로 적는다 — 그대로 붙여 넣어 쓸 수 있게
    here = "" if os.path.realpath(os.getcwd()) == os.path.realpath(HERE) else home(HERE).rstrip("/") + "/"
    say("""
Next
  1. Open the board:   %(ctl)s open   (http://127.0.0.1:%(port)d)
  2. Send any prompt in Claude Code — its session shows up on the board.
     Sessions that are already open appear from their next prompt; no restart needed.
  3. Optional: import the last week of conversations:
       /usr/bin/python3 %(h)scsl/backfill.py --days 7
  4. Optional: run sessions in the Orca terminal, or inside tmux in any terminal app, to reply and
     answer prompts from the board (see "Working with sessions" in the README).

Check the install any time:  /usr/bin/python3 %(h)sinstall.py --check
Remove it:                   /usr/bin/python3 %(h)sinstall.py --uninstall""" % {"h": here, "ctl": (here or "./") + "ctl.sh", "port": PORT})

    want = args.open
    if not want and not args.no_open and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            want = input("\nOpen the board now? [Y/n] ").strip().lower() in ("", "y", "yes")
        except EOFError:
            want = False
    if want:
        subprocess.run([os.path.join(HERE, "ctl.sh"), "open"])


if __name__ == "__main__":
    main()
