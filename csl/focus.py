"""Claude Code 세션의 터미널 다루기: 앞으로 가져오기, 보내기, 화면 해석(입력칸·선택 창·모델 창·질문 창).

터미널 앱을 직접 부르지 않고 csl/terminals 의 공통 인터페이스(Target = 구현체 + 핸들)만 쓴다 — 새 터미널은
그 인터페이스를 구현해 붙인다. 여기 남은 건 터미널과 무관한 Claude Code 쪽 규칙(화면 모양·키 순서)이다.

조작할 수 없는 터미널은 앞으로 가져오기만 한다:
- iTerm2: ITERM_SESSION_ID 의 고유 ID 로 세션 선택(AppleScript).
- Terminal.app: tty 로 탭 선택(AppleScript).
- 그 밖(Claude 데스크톱·VS Code 등): 앱만 앞으로(open -b).

AppleScript 는 처음 실행할 때 macOS 가 「자동화」 권한을 묻는다.
값은 모두 형식을 검사한 뒤 인자로만 넘긴다(셸·스크립트 문자열에 끼우지 않음).
"""
import functools
import json
import os
import re
import shlex
import threading
import time

from csl import config, terminals
from csl.terminals import Target
from csl.terminals.orca import _run, norm_title as _norm_title   # 같은 하위 프로세스 실행·탭 제목 정규화

CLAUDE_DESKTOP_BUNDLE = "com.anthropic.claudefordesktop"

_TTY = re.compile(r"^/dev/ttys\d+$")
_ID = re.compile(r"^[\w-]+$")
_BUNDLE = re.compile(r"^[\w.-]+$")

ITERM_SCRIPT = """
on run argv
  set target to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique id of s is target then
            select w
            tell t to select
            tell s to select
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
"""

TERMINAL_SCRIPT = """
on run argv
  set target to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is target then
          set selected of t to true
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "notfound"
end run
"""


# 대화 속 파일 경로 열기. 목록에 없는 형식은 열지 않고 Finder 에서 보여 주기만 한다 — 실행될 수 있는 파일(.command·.app·.sh,
# Python Launcher 로 실행되는 .py 등)과 열면 바로 풀리는 압축 파일(.zip)을 막기 위해서다.
# HTML 도 보드 서버로 내보내지 않고 기본 브라우저(file://)로 연다 — 보드와 같은 출처가 되면 그 페이지가 보드 API 를 부를 수 있다.
OPEN_EXTS = {
    # 이미지
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".heic", ".heif", ".jxl", ".bmp", ".tif", ".tiff",
    ".svg", ".ico", ".icns", ".psd", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
    # 음성
    ".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".caf", ".flac", ".ogg", ".oga", ".opus", ".mid", ".midi",
    # 영상·자막
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp", ".wmv", ".flv",
    ".srt", ".vtt", ".ass", ".ssa",
    # 문서·웹
    ".pdf", ".html", ".htm", ".epub", ".rtf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".key", ".numbers", ".pages", ".hwp", ".hwpx",
    # 글·데이터
    ".md", ".txt", ".log", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".yaml", ".yml",
    # 글꼴(서체 관리자에서 미리보기만, 설치는 따로 눌러야 함)
    ".ttf", ".otf", ".woff", ".woff2",
}


def open_path(path, reveal=False):
    """(결과, 방식). 결과: ok·missing·fail, 방식: open·reveal"""
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.exists(path):
        return "missing", None
    ext = os.path.splitext(path)[1].lower()
    # 폴더는 Finder 로 연다. 다만 .app·.pkg·.workflow 처럼 확장자가 붙은 폴더는 번들이라 열면 실행된다 — 보여 주기만
    how = "open" if not reveal and ((os.path.isdir(path) and not ext) or (not os.path.isdir(path) and ext in OPEN_EXTS)) else "reveal"
    r = _run(["open", path] if how == "open" else ["open", "-R", path])
    if how == "open" and not (r and r.returncode == 0):
        # 열 앱이 없는 형식(예: .opus, .mkv 를 못 여는 Mac)이면 Finder 에서라도 보여 준다
        how = "reveal"
        r = _run(["open", "-R", path])
    return ("ok" if r and r.returncode == 0 else "fail"), how


def _activate(bundle):
    if bundle and _BUNDLE.match(bundle):
        r = _run(["open", "-b", bundle])
        return bool(r and r.returncode == 0)
    return False


def _osascript(script, arg):
    r = _run(["osascript", "-e", script, arg], timeout=10)
    if r is None:
        return "error", "osascript 실행 실패"
    if r.returncode != 0:
        return "error", (r.stderr or "").strip()[:200]
    return r.stdout.strip(), ""


def _terminal(session):
    """세션에 기록된 터미널 정보(dict)."""
    try:
        return json.loads(session.get("terminal") or "{}")
    except ValueError:
        return {}


def target_of(session):
    """기록된 터미널 정보만으로 Target(찾기 없이). 모르면 None."""
    term = _terminal(session)
    t = terminals.of_record(term)
    h = t.handle_of(term) if t else None
    return Target(t, h) if h else None


def handle_of(session):
    """보드 화면용: 기록된 핸들 문자열(보드에서 방금 띄운 세션 찾기에 쓴다)."""
    tg = target_of(session)
    return tg.handle if tg else None


# ---------------------------------------------------------------- 지금 돌고 있는 Claude Code 프로세스
# Claude Code 는 돌고 있는 세션마다 ~/.claude/sessions/<pid>.json({"pid", "sessionId", "kind", …})을 둔다.
# 공개된 형식이 아니라 언제든 바뀔 수 있다 — 읽지 못하면 「모름」(None)으로 두고 예전 방식(기록·제목)만 쓴다.
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
_PS_TTY = re.compile(r"^ttys\d+$")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def running_sessions():
    """{세션 ID: {"pid", "kind", "bridge"}} — 지금 살아 있는 Claude Code 프로세스. 실행 목록을 읽을 수 없으면 None(모름).
    bridge: 지금 연결된 Remote Control 세션 ID(연결 안 됐으면 "") — 훅 환경 변수보다 정확하다(자동 연결도 보인다)."""
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return None
    out, readable = {}, False
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, n)) as f:
                d = json.load(f)
            pid, sid = d["pid"], d["sessionId"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(sid, str):
            continue
        readable = True
        if _pid_alive(pid):
            out[sid] = {"pid": pid, "kind": d.get("kind") or "", "bridge": d.get("bridgeSessionId") or ""}
    return out if readable else None


def process_terminal(session_id):
    """그 세션을 돌리는 Claude Code 프로세스의 환경 변수로 본 터미널 정보({"backend", 핸들 키…, "tty"}). 모르면 {}.

    탭 제목이 같거나 비었거나 바뀌어도 정확한 탭이 나온다. 백그라운드 세션(kind bg)은 띄운 쪽(claude agents 탭)의
    값을 물려받아 틀리므로 보지 않는다. 환경 변수에는 토큰 같은 값도 있으니 터미널 구현체가 적어 둔 키만 골라 쓰고
    아무 데도 남기지 않는다."""
    x = (running_sessions() or {}).get(session_id or "")
    if not x or x["kind"] == "bg":
        return {}
    want = {v for t in terminals.registry().values() for v in t.env_keys.values()}
    r = _run(["ps", "eww", "-o", "tty=,command=", "-p", str(x["pid"])])
    if not r or r.returncode != 0 or not r.stdout.strip():
        return {}
    toks = r.stdout.split()
    env = {}
    for tok in toks[1:]:
        k, eq, v = tok.partition("=")
        if eq and k in want:
            env[k] = v   # 명령 인자 뒤에 환경 변수가 온다 — 같은 이름이면 뒤의 것(환경 변수)
    got = terminals.env_info(env)
    if got and _PS_TTY.match(toks[0]):
        got["tty"] = "/dev/" + toks[0]
    return got


def resolve_target(session):
    """(Target 또는 None, 새로 찾았으면 세션에 다시 저장할 터미널 정보).

    그 세션을 돌리는 프로세스가 알려 주는 탭이 먼저다 — 기록된 탭에서 지금은 다른 세션이 돌 수 있다(/resume·/clear).
    프로세스로 모르면 기록된 핸들, 그것도 없으면 탭 제목·경로로 찾는다."""
    term = _terminal(session)
    got = process_terminal(session.get("session_id"))
    t = terminals.of_record(got) if got else None
    h = t.handle_of(got) if t else None
    if h and t.available() and t.info(h) is not None:
        new = dict(term, **got)
        return Target(t, h), (new if new != term else None)
    tg = target_of(session)
    if tg:
        return tg, None
    rec = terminals.of_record(term)
    for t in ([rec] if rec else []) + [x for x in terminals.registry().values() if x is not rec and x.available()]:
        h = t.find(session.get("title"), session.get("cwd"), session.get("project_root"))
        if h:
            return Target(t, h), t.remember(term, h)
    return None, None


def focus(session):
    """반환: (성공 여부, 안내 문구, 새로 찾은 터미널 정보 또는 None)."""
    term = _terminal(session)
    program = term.get("program") or ""
    bundle = term.get("bundle") or ""

    # 조작할 수 있는 터미널 — 기록된 핸들, 안 되면 제목·경로로 찾기. 터미널 정보가 아예 없는 세션(가져온 과거 기록)도 시도
    rec = terminals.of_record(term)
    if rec or not term:
        tg, found = resolve_target(session)
        if tg and tg.terminal.switch(tg.handle):
            return True, "%s 탭으로 이동했습니다" % tg.terminal.label, found
        if rec:
            rec.activate()
            return False, "%s 를 앞으로 가져왔지만 이 세션의 탭을 찾지 못했습니다(탭을 닫았거나 제목이 바뀜)" % rec.label, None

    if program == "iTerm.app" and term.get("iterm_session"):
        uid = term["iterm_session"].split(":")[-1]
        if _ID.match(uid):
            out, err = _osascript(ITERM_SCRIPT, uid)
            if out == "ok":
                return True, "iTerm 탭으로 이동했습니다", None
            if out == "error":
                return False, "iTerm 제어 실패: " + err + " (시스템 설정 > 개인정보 보호 > 자동화 확인)", None

    if program == "Apple_Terminal" and term.get("tty") and _TTY.match(term["tty"]):
        out, err = _osascript(TERMINAL_SCRIPT, term["tty"])
        if out == "ok":
            return True, "터미널 탭으로 이동했습니다", None
        if out == "error":
            return False, "터미널 제어 실패: " + err + " (시스템 설정 > 개인정보 보호 > 자동화 확인)", None

    if "desktop" in (term.get("entrypoint") or "") and _activate(CLAUDE_DESKTOP_BUNDLE):
        return True, "Claude 데스크톱을 앞으로 가져왔습니다(세션은 직접 선택)", None

    if _activate(bundle):
        return True, "앱을 앞으로 가져왔습니다(탭은 직접 선택)", None
    if not term:
        return False, "이 세션의 터미널 정보가 아직 없습니다. 그 터미널에서 요청을 한 번 보내면 기록됩니다", None
    return False, "이 터미널은 이동을 지원하지 않습니다(%s)" % (program or "알 수 없음"), None


# ---------------------------------------------------------------- 보드에서 보내기

def claude_alive(session, t):
    """그 터미널 전면에서 아직 Claude Code 가 돌고 있는지.

    Claude 가 종료돼 셸만 남았는데 보내면 글이 셸 명령으로 실행되므로 반드시 확인한다.
    tty 를 알면 전면 프로세스로, 모르면 터미널이 알려 주는 에이전트·탭 제목으로 판단한다.
    """
    tty = _terminal(session).get("tty")
    if tty and _TTY.match(tty):
        r = _run(["ps", "-t", tty[len("/dev/"):], "-o", "stat=,comm="])
        if not r or r.returncode != 0:
            return False
        for line in r.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and "+" in parts[0] and os.path.basename(parts[1].strip()) == "claude":
                return True
        return False
    info = t.terminal.info(t.handle) if t else None
    if not info:
        return False
    if info.get("agent") == "claude" or (
            bool(session.get("title")) and _norm_title(info.get("title")) == _norm_title(session.get("title"))):
        return True
    # 「claude attach」로 붙인 탭(백그라운드 갈래)은 터미널이 에이전트로 알아보지 못하고 제목도 다르다 —
    # 화면에 Claude Code 입력칸(가로줄 사이 ❯)이 보이면 돌고 있는 것으로 본다
    return input_line(t) is not None


# ---------------------------------------------------------------- 한 터미널엔 한 번에 하나씩
# 보드 요청(보내기·되돌리기·모델·권한·답하기)·예약 보내기·위임 전달·브레인이 각자 스레드에서 같은 터미널에 키를 넣는다.
# 「창·작업 중 확인 → 쓰기」 사이에 다른 쪽이 끼면 두 글이 한 요청으로 합쳐지거나 선택 창에 글이 들어간다 — 터미널마다
# 잠금 하나로 줄 세운다. 같은 스레드는 다시 잡을 수 있다(되돌리기 안의 send_prompt 등)
_TERM_LOCKS = {}
_TERM_LOCKS_GUARD = threading.Lock()


def hold(t):
    """t 터미널의 잠금(RLock). `with focus.hold(t):` 로 확인과 쓰기를 한 덩어리로 묶는다."""
    with _TERM_LOCKS_GUARD:
        return _TERM_LOCKS.setdefault(t.handle if t else None, threading.RLock())


def _serial(fn):
    @functools.wraps(fn)
    def run(t, *a, **k):
        if not t:
            return fn(t, *a, **k)
        with hold(t):
            return fn(t, *a, **k)
    return run


# 빈 입력칸에 「!」가 들어가면 Claude Code 가 bash 모드로 바뀌어 Enter 에 그 줄을 셸 명령으로 바로 실행한다(권한 확인 없음,
# 붙여넣기·타이핑 모두 — v2.1.316 실측). 브레인이 슬랙 글을 옮기거나 위임·예약으로 보내는 글이 그 통로가 되지 않게 막는다.
# 사람이 칸 입력창에 직접 쳐서 바로 보내는 것만 허용(allow_bang — 서버가 보드 화면의 직접 보내기인지 확인한 뒤)
BANG_ERROR = "「!」로 시작하는 글은 Claude Code 가 셸 명령으로 바로 실행하므로 보드에서는 보내지 않습니다"


def bang(text):
    return (text or "").lstrip().startswith("!")


@_serial
def send_prompt(t, text, allow_bang=False):
    """한 줄 요청을 입력창에 넣고 Enter. (성공 여부, 단계 목록, 오류 문구)

    선택·확인 창이 떠 있으면 보내지 않는다 — 붙여넣은 글과 Enter 가 그 창의 선택이 된다."""
    if not t:
        return False, [], "터미널을 찾지 못했습니다"
    if bang(text) and not allow_bang:
        return False, [], BANG_ERROR
    box = dialog_text(t)
    if box:
        return False, [], "터미널에 선택 창이 떠 있습니다: " + box
    if t.terminal.checks_submit:
        return t.terminal.submit(t.handle, text)
    # 글을 넣고, 입력칸에 실제로 들어간 것을 본 뒤에만 Enter — 확인한 직후 선택 창(Claude Code 가 스스로 띄우는 설정 창 등)이
    # 뜨면 글이 그 창으로 들어가고 Enter 가 엉뚱한 선택이 된다
    t0 = time.time()
    if not t.terminal.write(t.handle, text):
        return False, [], "입력 전송 실패"
    if not _landed(t, text):
        return False, [], NOT_LANDED
    time.sleep(max(0.0, 0.3 - (time.time() - t0)))   # 한 번에 보내면 붙여넣기로 보고 Enter 를 줄바꿈으로 받는다 — 글을 다 받은 뒤 Enter
    if not t.terminal.write(t.handle, "\r"):
        return False, [], "Enter 전송 실패(입력창에 글이 남아 있을 수 있음)"
    return True, [], ""


NOT_LANDED = "글이 입력칸에 들어가지 않아 Enter 는 누르지 않았습니다 — 터미널에 선택 창이 떴는지 확인해 주세요(칸의 「⌨ 터미널」)"


def _landed(t, text, wait=2.0):
    """보낸 글이 입력칸에 들어갔는지 — Enter 를 누르기 전에 화면을 다시 본다(글이 다 그려질 때까지 잠깐 기다림).
    첫 줄 앞부분(빈칸 무시)이 입력칸에 보이거나, 길어서 [Pasted text …]·이미지 [Image #…] 로 접혔으면 들어간 것.
    화면을 못 읽거나 입력칸이 안 보이면(창이 떴다) False."""
    lines = [ln for ln in IMAGE_TOKEN.sub("", text).splitlines() if ln.strip()]
    want = re.sub(r"\s+", "", re.sub(r"^\s*!", "", lines[0] if lines else ""))[:30]
    want_end = re.sub(r"\s+", "", lines[-1])[-30:] if len(lines) > 1 else ""
    end = time.time() + wait
    while True:
        screen = _screen(t)
        got = _input_line_of(screen)
        if got is not None:
            g = re.sub(r"\s+", "", got)
            if (want and want in g) or "[Pastedtext" in g or "[Image#" in g or (not want and g):
                return True
        elif want_end:
            # 여러 줄 글이 화면보다 길면 입력칸 윗부분(위 가로줄과 「❯」 첫 줄)이 화면 위로 밀려 안 보인다 —
            # 그때는 아래 가로줄 바로 위에 글의 마지막 줄이 보이면 들어간 것
            tail = _input_tail(screen)
            if tail is not None and want_end in re.sub(r"\s+", "", tail):
                return True
        if time.time() >= end:
            return False
        time.sleep(0.25)


IMAGE_TOKEN = re.compile(r"\[이미지 (\d+)\]")


@_serial
def type_text(t, text, image_paths, allow_bang=False):
    """여러 줄·이미지 메시지는 붙여넣기 대신 타이핑하듯 보낸다. (성공 여부, 오류 문구)

    붙여넣기(bracketed paste)로 넣으면 Claude Code 는 여러 줄을 [Pasted text] 덩어리로 받아
    <pasted_content> 태그로 감싼다. 그래서
    - 글은 PTY 에 그대로 쓰고(줄바꿈은 LF = Ctrl+J, Claude Code 에서 줄바꿈 입력)
    - 이미지 경로만 붙여넣기 괄호로 감싸 [Image #N] 첨부로 인식시키고
    - Enter(CR)는 따로 보낸다.
    """
    if not t:
        return False, "터미널을 찾지 못했습니다"
    if bang(text) and not allow_bang:
        return False, BANG_ERROR
    box = dialog_text(t)
    if box:
        return False, "터미널에 선택 창이 떠 있습니다: " + box
    # 탭은 공백으로, 나머지 제어 문자(CR·ESC·Ctrl+C …)는 뺀다 — PTY 에 그대로 쓰므로 키 입력으로 실행된다. 줄바꿈(LF)만 남긴다
    typed = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text.replace("\t", "    "))
    # 보드 입력창의 [이미지 N] 자리에 N 번째 이미지를 넣는다(그 자리에 [Image #…] 로 붙는다). 자리가 없는 이미지는 끝에
    used = set()

    def put(m):
        i = int(m.group(1)) - 1
        if 0 <= i < len(image_paths) and i not in used:
            used.add(i)
            return "\x1b[200~" + image_paths[i] + "\x1b[201~"
        return m.group(0)
    typed = IMAGE_TOKEN.sub(put, typed)
    if typed and typed.split()[-1].startswith("@"):
        typed += " "   # 끝이 @파일 이면 자동완성 목록이 Enter 를 가로채지 않게
    for i, p in enumerate(image_paths):
        if i not in used:
            typed += (" " if typed else "") + "\x1b[200~" + p + "\x1b[201~"
    t0 = time.time()
    if not _raw(t, typed):
        return False, "입력 전송 실패"
    if not _landed(t, text):   # 선택 창이 끼어들었으면 Enter 를 누르지 않는다(send_prompt 와 같은 이유)
        return False, NOT_LANDED
    time.sleep(max(0.0, 0.3 - (time.time() - t0)))   # 입력창이 글을 다 받은 뒤 Enter
    if not _raw(t, "\r"):
        return False, "Enter 전송 실패(입력창에 글이 남아 있을 수 있음)"
    return True, ""


def _screen(t):
    return t.terminal.screen(t.handle) if t else []


# ---------------------------------------------------------------- 새 세션(보드에서 Claude Code 시작)
NEW_MODELS = set(config.MODELS)


def open_projects():
    """새 세션 창의 열린 프로젝트(기본 터미널이 알려 주는 것). 터미널이 모르면 []."""
    t = terminals.default()
    return t.projects() if t else []


def new_claude_terminal(cwd, model=None, extra="", terminal=None):
    """cwd 에서 claude 를 띄운 터미널을 기본 터미널(terminal 을 주면 그 터미널 — "web" 은 보드 터미널)에 새로 만든다.
    (Target, 오류 문구)

    첫 메시지는 여기서 넘기지 않는다 — 세션이 뜬 뒤 보드의 보내기로 보내야 여러 줄·이미지가 그대로 간다.
    """
    t = terminals.registry().get(terminal) if terminal else terminals.default()
    if t and not t.available():
        return None, "%s 을(를) 쓸 수 없습니다(tmux 가 필요합니다)" % t.label
    if not t:
        return None, "새 세션을 띄울 수 있는 터미널이 없습니다(지원: %s)" % ", ".join(
            x.label for x in terminals.registry().values())
    cwd = os.path.realpath(os.path.expanduser(cwd or ""))
    if not os.path.isdir(cwd):
        return None, "폴더가 없습니다: " + cwd
    cmd = "cd %s && claude" % shlex.quote(cwd)
    if model:
        if model not in NEW_MODELS:
            return None, "모르는 모델입니다"
        cmd += " --model " + model
    if extra:
        cmd += " " + extra
    # Claude 가 정상 종료(/exit)하면 셸도 끝낸다 — 끝난 탭은 서버가 닫는다(server.reap_tabs). 오류로 끝나면 셸이 남아 메시지를 볼 수 있다
    cmd += " && exit"
    handle, err = t.create(cwd, os.path.basename(cwd) or cwd, cmd)
    return (Target(t, handle), None) if handle else (None, err)


# ---------------------------------------------------------------- 모델·effort 바꾸기(이 세션만)
# `/model 이름`·`/effort 단계` 를 바로 치면 기본값(사용자 설정)까지 바뀐다. 그래서 /model 선택 창을 열어
# 화면을 읽고, 화살표로 모델 줄·effort 를 맞춘 뒤 다시 확인하고 `s`(이 세션만)를 누른다.
# 확인이 하나라도 어긋나면 Esc 로 닫고 아무것도 바꾸지 않는다.
EFFORT_LEVELS = config.EFFORTS
_MODEL_ROW = re.compile(r"^\s*(❯)?\s*(?:[↑↓]\s*)?(\d+)\.\s+(.+?)(?:\s{2,}|$)")
_EFFORT_LINE = re.compile(r"(low|medium|high|extra high|xhigh|max)\s+effort", re.I)
UP, DOWN, LEFT, RIGHT, ESC = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C", "\x1b"


def input_line(t):
    """Claude Code 입력칸에 지금 보이는 글. 비어 있으면 "", 입력칸이 안 보이면(선택 창 등) None.

    응답 뒤 Claude Code 가 흐리게 띄우는 다음 요청 제안(prompt suggestion)이 여기 보인다. 화면 읽기는 색을 빼고
    주므로 제안인지 사용자가 터미널에 쳐 둔 글인지는 구분하지 못한다.
    """
    if not t:
        return None
    return _input_line_of(_screen(t))


def _input_line_of(lines):
    """화면 줄에서 입력칸 글(입력칸이 안 보이면 None)."""
    rules = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("──")]
    # 입력칸은 가로줄 두 개 사이, 첫 줄이 ❯ 로 시작한다(셸 모드 — 「!」를 친 뒤 — 면 「!」로)
    for a, b in zip(reversed(rules[:-1]), reversed(rules[1:])):
        body = lines[a + 1:b]
        if body and body[0].startswith(("❯", "!")):
            first = body[0][1:].replace("\xa0", " ").strip()
            rest = [ln.replace("\xa0", " ")[2:].rstrip() for ln in body[1:]]
            return "\n".join([first] + rest).strip()
    return None


def _input_tail(lines):
    """입력칸 윗부분이 화면 위로 밀려 첫 줄(❯)이 안 보일 때, 맨 아래 가로줄 위에 보이는 입력칸 글. 가로줄이 없으면 None."""
    rules = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("──")]
    if not rules:
        return None
    b = rules[-1]
    a = rules[-2] if len(rules) > 1 else -1
    body = [ln.replace("\xa0", " ")[2:].rstrip() for ln in lines[a + 1:b]]
    return "\n".join(body).strip() or None


_BUSY = re.compile(r"…\s*\((?:\d+h\s*)?(?:\d+m\s*)?\d+s\b")   # 「✽ Honking… (29s · ↓ 2.7k tokens)」 — 끝나면 「Brewed for 45s」


def is_busy(t):
    """입력칸 위에 진행 줄(…(N초)이 보이면 Claude 가 아직 작업 중."""
    lines = _screen(t)
    rules = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("──")]
    top = rules[-2] if len(rules) >= 2 else len(lines)
    return any(_BUSY.search(ln) for ln in lines[max(0, top - 6):top])


def dialog_text(t, wait=0.0):
    """입력칸 대신 선택·확인 창이 떠 있으면 그 창의 글(아래쪽 몇 줄), 입력칸이 보이면 None.

    /remote-control 처럼 바로 끝나지 않고 사용자 선택을 기다리는 명령이 있다 — 그 창에 글을 보내면 엉뚱한 선택이 된다.
    """
    if wait:
        time.sleep(wait)
    screen = _screen(t)   # 한 번만 읽는다(읽을 때마다 터미널 CLI 하위 프로세스)
    if _input_line_of(screen) is not None:
        return None
    lines = [ln.replace("\xa0", " ").rstrip() for ln in screen]
    body = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("──")]
    return " / ".join(body[-6:])[:400] or "(화면을 읽지 못함)"


# ---------------------------------------------------------------- 곁질문(/btw) 답 읽기
# /btw 는 대화에 들어가지 않는 곁질문이다. 훅도 기록 파일도 없이 입력칸 자리에 창이 떠서 지금까지의 곁질문 목록과
# 마지막 질문의 답을 보여 주고, Esc 를 누를 때까지 입력을 막는다. 실측(Claude Code 2.1.314):
#   ▔▔▔ (창 위 경계)
#       /btw 이전 질문…            ← 들여쓰기 4칸, 지난 질문들(답은 접힘)
#       /btw 이번 질문
#         답 첫 줄…                 ← 들여쓰기 6칸. 만드는 중엔 「✳ Answering…」
#       ⇧←/→ to browse · c to copy · f to fork · x to clear history · Esc to close   ← 「c to copy」가 보이면 다 쓴 것
_BTW_FOOT = "Esc to close"


def _btw_view(lines, question):
    """(다 썼는지, 답 글 또는 None). 곁질문 창이 아니면 (False, None)."""
    lines = [ln.replace("\xa0", " ").rstrip() for ln in lines]
    foot = next((i for i in range(len(lines) - 1, -1, -1) if _BTW_FOOT in lines[i] and "/btw" not in lines[i]), None)
    if foot is None:
        return False, None
    # 안내 줄이 터미널 폭보다 길면 두세 줄로 꺾인다(지난 곁질문이 있으면 「x to clear history」까지 붙어 길어진다) —
    # 「· 」로 이어지는 윗줄을 붙여 한 줄로 읽고, 본문은 그 위에서 끊는다
    foot_text, top = lines[foot], foot
    while top > 0 and top > foot - 3 and re.search(r"\bto (browse|copy|fork|clear|scroll)\b", lines[top - 1]) and "/btw" not in lines[top - 1]:
        top -= 1
        foot_text = lines[top].rstrip() + " " + foot_text.strip()
    if "browse" not in foot_text and "copy" not in foot_text and "scroll" not in foot_text:
        return False, None
    want = re.sub(r"\s+", "", question)[:20]
    head = next((i for i in range(top - 1, -1, -1)
                 if lines[i].strip().startswith("/btw") and re.sub(r"\s+", "", lines[i].strip()[4:]).startswith(want)), None)
    if head is None:
        return False, None
    body = []
    for ln in lines[head + 1:top]:
        if ln.strip().startswith("/btw") and not ln.startswith("      "):
            break   # 다음 질문(이 질문은 답이 접힌 지난 것)
        body.append(ln)
    while body and body[0].startswith("    ") and not body[0].startswith("      "):
        body = body[1:]   # 질문이 길어 다음 줄로 넘친 부분(들여쓰기 4칸)
    text = "\n".join(ln[6:] if ln.startswith("      ") else ln.strip() for ln in body).strip()
    if "Answering" in text and len(text) < 40:
        return False, ""
    return "c to copy" in foot_text, text


def read_btw(t, question, timeout=150, find_wait=45):
    """곁질문 창에서 답을 읽고 창을 닫는다(Esc — 곁질문은 닫아도 목록에 남는다). (답 또는 None, 오류 문구)
    find_wait: 창이 이 안에 안 보이면 포기한다. 세션이 한창 작업 중이면 창이 늦게 뜬다(실측 8초로는 모자랐다)."""
    end, last, same = time.time() + timeout, None, 0
    while time.time() < end:
        time.sleep(1)
        done, text = _btw_view(_screen(t), question)
        if text is None and last is None and time.time() > end - timeout + find_wait:
            return None, "곁질문 창을 찾지 못했습니다(터미널에서 닫혔거나 모양이 바뀜)"
        if done and text:
            same = same + 1 if text == last else 0
            last = text
            if same >= 1:   # 두 번 연속 같으면 다 그려진 것
                with hold(t):
                    if _btw_view(_screen(t), question)[0]:
                        _raw(t, ESC)
                return text, None
        elif text is not None:
            last = text or last
    return None, "%d초 안에 답이 끝나지 않았습니다 — 터미널에서 확인해 주세요" % timeout


def _raw(t, data):
    """키·글을 PTY 에 그대로(Enter 없이)."""
    return bool(t) and t.terminal.write(t.handle, data)


def _picker(lines):
    """(모델 줄 목록[(라벨, 커서 여부)], 지금 effort 또는 None). 선택 창이 아니면 (None, None)."""
    if not any("Select model" in ln for ln in lines):
        return None, None
    rows, effort = [], None
    for ln in lines:
        m = _MODEL_ROW.match(ln)
        if m:
            label = m.group(3).replace("✔", "").strip()
            rows.append((label, bool(m.group(1))))
        e = _EFFORT_LINE.search(ln)
        if e and "adjust" in ln:
            v = e.group(1).lower()
            effort = "xhigh" if v in ("extra high", "xhigh") else v
    return rows, effort


def _norm_model(label):
    return re.sub(r"[^a-z0-9.]", "", (label or "").lower())


@_serial
def set_model_effort(t, model_label=None, effort=None):
    """(성공 여부, 안내 문구). model_label 은 선택 창의 이름(예: 'Sonnet 5.5'), effort 는 EFFORT_LEVELS 중 하나."""
    if not t or (not model_label and not effort):
        return False, "바꿀 내용이 없습니다"
    if effort and effort not in EFFORT_LEVELS:
        return False, "effort 는 " + "·".join(EFFORT_LEVELS) + " 중 하나여야 합니다"
    ok, _, err = send_prompt(t, "/model")
    if not ok:
        return False, "선택 창을 열지 못했습니다: " + err

    def abort(msg):
        _raw(t, ESC)
        return False, msg

    rows = cur_effort = None
    for _ in range(15):   # 선택 창이 그려질 때까지
        time.sleep(0.2)
        rows, cur_effort = _picker(_screen(t))
        if rows:
            break
    if not rows:
        return abort("모델 선택 창이 열리지 않았습니다")

    if model_label:
        want = _norm_model(model_label)
        target = next((i for i, (lab, _) in enumerate(rows) if _norm_model(lab) == want), None)
        cursor = next((i for i, (_, c) in enumerate(rows) if c), None)
        if target is None:
            # 새 모델은 그 모델을 아는 Claude Code 버전으로 뜬 세션의 선택 창에만 나온다 — 업데이트 전에 뜬 세션이면 다시 시작
            return abort("이 세션의 /model 선택 창에 '%s' 가 없습니다 — 업데이트 전에 뜬 세션이면 ⋮ → ↻ 세션 다시 시작 뒤 다시 골라 주세요" % model_label)
        if cursor is None:
            return abort("선택 창의 커서 위치를 읽지 못했습니다")
        if target != cursor:
            _raw(t, (DOWN if target > cursor else UP) * abs(target - cursor))
            time.sleep(0.4)
            rows, cur_effort = _picker(_screen(t))
            now = next((lab for lab, c in (rows or []) if c), None)
            if _norm_model(now) != want:
                return abort("모델 줄을 맞추지 못했습니다(지금: %s)" % now)

    note = ""
    if effort:
        if cur_effort is None:
            note = " · 이 모델은 effort 를 지원하지 않아 effort 는 그대로"
        elif cur_effort != effort:
            d = EFFORT_LEVELS.index(effort) - EFFORT_LEVELS.index(cur_effort)
            _raw(t, (RIGHT if d > 0 else LEFT) * abs(d))
            time.sleep(0.4)
            _, cur_effort = _picker(_screen(t))
            if cur_effort != effort:
                return abort("effort 를 맞추지 못했습니다(지금: %s)" % cur_effort)

    _raw(t, "s")   # 이 세션만(기본값은 그대로)
    for _ in range(10):
        time.sleep(0.3)
        lines = _screen(t)
        if not any("Select model" in ln for ln in lines):
            return True, "바꿨습니다(이 세션만)" + note
    return abort("선택 창이 닫히지 않았습니다")


# ---------------------------------------------------------------- 되돌리기(/rewind)
# 실측(2.1.316): /rewind 는 내 요청 목록(맨 아래 「(current)」에 커서)을 띄운다. 위로 올려 고르고 Enter 면
# 「Confirm you want to restore to the point before …」 아래 번호 선택지(1. Restore conversation · 2. Summarize from here ·
# 3. Summarize up to here · 4. Never mind — 코드가 바뀌었으면 코드 되돌리기 선택지가 더 붙는다). 고른 요청과 그 뒤가 지워지고,
# 그 요청은 터미널 입력칸에 다시 들어간다. 확인이 어긋나면 Esc 로 닫고 아무것도 바꾸지 않는다
REWIND_LABELS = {
    "conversation": "restore conversation",
    "both": "restore code and conversation",
    "code": "restore code",
    "summarize_from": "summarize from here",
    "summarize_upto": "summarize up to here",
}
_OPTION = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+?)\s*$")


def _norm_text(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().lower()


def _rewind_items(lines):
    """(커서가 있는 줄의 글, 목록이 보이는지). 목록은 「Rewind」 머리 아래 들여쓴 요청들."""
    try:
        top = max(i for i, ln in enumerate(lines) if ln.strip() == "Rewind")
    except ValueError:
        return None, False
    for ln in lines[top + 1:]:
        s = ln.strip()
        if s.startswith("❯"):
            return s[1:].strip(), True
    return None, True


def _rewind_view(lines):
    """되돌리기 목록 화면(「Rewind」 머리 아래 전부) — 커서가 움직였는지 비교용."""
    try:
        top = max(i for i, ln in enumerate(lines) if ln.strip() == "Rewind")
    except ValueError:
        return None
    return tuple(ln.rstrip() for ln in lines[top:])


def rewind_key(prompt):
    """되돌리기 목록에 보이는 모양으로 맞춘 요청 글(내용이 있는 첫 줄, 공백 정리). 빈 줄로 시작하는 글(이미지를 붙이고
    줄을 바꾼 뒤 쓴 글 등)은 첫 줄이 비어 「되돌릴 요청 글이 없습니다」가 났다 — 목록도 빈 줄은 건너뛰고 보여 준다."""
    first = next((ln for ln in (prompt or "").splitlines() if ln.strip()), "")
    return _norm_text(first)


def _rewind_match(shown, key):
    """목록 줄이 그 요청인지. 목록은 화면 폭에서 잘라 「…」를 붙인다(실측) — 잘렸으면 앞부분, 아니면 글 전체가 같아야 한다.
    짧은 요청(「ok」)이 그 글로 시작하는 다른 요청과 맞던 것을 막는다."""
    c = _norm_text(shown)
    if c.endswith("…"):
        c = c[:-1].rstrip()
        return len(c) >= 8 and key.startswith(c)
    return c == key


@_serial
def rewind(t, prompt, mode, skip=0):
    """prompt(되돌릴 요청의 글) 앞으로 되돌린다. (성공 여부, 안내 문구)

    skip: 그 요청 뒤에 같은 글(rewind_key)의 요청이 몇 번 더 있는지 — 목록은 아래(최신)부터 훑으므로 그만큼 건너뛴다."""
    want = REWIND_LABELS.get(mode)
    if not t or not want:
        return False, "되돌리기 방식을 고르세요"
    target = rewind_key(prompt)
    if not target:
        return False, "되돌릴 요청 글이 없습니다"
    ok, _, err = send_prompt(t, "/rewind")
    if not ok:
        return False, "되돌리기 창을 열지 못했습니다: " + err

    def abort(msg):
        _raw(t, ESC)
        return False, msg

    cur = shown = None
    for _ in range(15):
        time.sleep(0.2)
        cur, shown = _rewind_items(_screen(t))
        if shown and cur:
            break
    if not shown:
        return abort("되돌리기 창이 열리지 않았습니다")
    for _ in range(200):   # 위로 올리며 그 요청을 찾는다(목록은 오래된 것이 위)
        if cur and _rewind_match(cur, target):
            if skip <= 0:
                break
            skip -= 1   # 같은 글의 더 최근 요청 — 지나친다
        before = _rewind_view(_screen(t))
        _raw(t, UP)
        time.sleep(0.15)
        lines = _screen(t)
        # 맨 위에 닿았는지는 목록 화면 전체(커서 자리·「↑ N more」)로 본다 — 같은 글의 요청이 붙어 있으면 커서 줄 글은 그대로다
        if _rewind_view(lines) == before:
            time.sleep(0.3)
            lines = _screen(t)
            if _rewind_view(lines) == before:
                return abort("목록에서 그 요청을 찾지 못했습니다(이미 되돌렸거나 오래된 요청)")
        cur, _ = _rewind_items(lines)
    else:
        return abort("목록에서 그 요청을 찾지 못했습니다")
    _raw(t, "\r")
    rows = []
    for _ in range(15):
        time.sleep(0.2)
        lines = _screen(t)
        if any("Confirm you want to restore" in ln for ln in lines):
            rows = [m.groups() for m in (_OPTION.match(ln) for ln in lines[-12:]) if m]
            if rows:
                break
    if not rows:
        return abort("되돌리기 확인 창을 읽지 못했습니다")
    labels = [_norm_text(r[2]) for r in rows]
    idx = next((i for i, lab in enumerate(labels) if lab == want), None)
    if idx is None:
        return abort("이 지점에서는 그 방식을 고를 수 없습니다(선택지: %s)" % " · ".join(r[2] for r in rows))
    cursor = next((i for i, r in enumerate(rows) if r[0]), 0)
    if idx != cursor:
        _raw(t, (DOWN if idx > cursor else UP) * abs(idx - cursor))
        time.sleep(0.3)
        rows2 = [m.groups() for m in (_OPTION.match(ln) for ln in _screen(t)[-12:]) if m]
        now = next((_norm_text(r[2]) for r in rows2 if r[0]), None)
        if now != want:
            return abort("선택지를 맞추지 못했습니다(지금: %s)" % now)
    _raw(t, "\r")
    for _ in range(25):
        time.sleep(0.3)
        if not any("Confirm you want to restore" in ln for ln in _screen(t)):
            if mode in ("conversation", "both") and not _clear_restored(t):
                return True, "되돌렸습니다 — 다만 입력칸이 보이지 않아 터미널 입력칸의 되돌린 요청은 비우지 못했습니다(터미널에서 확인해 주세요)"
            return True, "되돌렸습니다"
    return abort("확인 창이 닫히지 않았습니다")


def _clear_restored(t):
    """되돌린 요청이 입력칸에 다시 들어온다 — 비운다(보드 입력창으로 옮겨 준다). 눌렀으면 True.

    Ctrl+U 는 화면에 접힌 한 줄씩만 지우고(줄바꿈도 한 번 더) 몰아 보내면 하나도 안 먹는다. Ctrl+C 는 한 번에 통째로 비운다.
    다만 비웠든 원래 비어 있었든 「Press Ctrl-C again to exit」를 3초쯤 띄우고, 그사이 한 번 더 누르면 Claude 가 꺼진다
    (v2.1.316 실측) — 그래서 확인하려고 다시 누르지 않는다. 입력칸이 보이고 다른 창이 없을 때, 요청이 들어올 틈을 두고 한 번만."""
    for _ in range(10):
        time.sleep(0.3)
        if _input_line_of(_screen(t)) is not None:
            break
    else:
        return False
    time.sleep(0.8)
    return _raw(t, "\x03")


# ---------------------------------------------------------------- 입력칸 대신 뜬 선택 창(권한 확인·계획 승인 …)
# 실측(v2.1.316): 「Bash command / 설명 / ╌╌ / 명령 / ╌╌ / Do you want to proceed? / ❯ 1. Yes / 2. … / 4. No / Esc to cancel」.
# 숫자 키를 누르면 그 줄이 바로 골라진다(Enter 없이).
def prompt_view(t):
    """{"head": [설명 줄…], "options": [{"n", "label", "hint", "on"}]} 또는 None(입력칸이 보이거나 고를 줄이 없음)."""
    lines = [ln.replace("\xa0", " ").rstrip() for ln in _screen(t)]
    if _input_line_of(lines) is not None:
        return None
    idx = [i for i, ln in enumerate(lines) if _OPTION.match(ln)]
    if not idx:
        return _plain_menu(lines)
    # 창은 화면 맨 아래 — 마지막 선택지 위의 마지막 경계(── · ▔▔) 아래 줄만 본다. 그 위 대화에 있는 번호 목록
    # (「1. … 2. …」)을 선택지로 잘못 읽어 종료 창(「Exit and stop tasks」 등)을 못 찾던 것
    borders = [i for i, ln in enumerate(lines[:idx[-1]]) if ln.lstrip().startswith(("──", "▔"))]
    if borders and any(i > borders[-1] for i in idx):
        idx = [i for i in idx if i > borders[-1]]
    first = idx[0]
    # 창 위 경계: 입력칸 자리의 가로줄(──) 또는 창 테두리(▔▔ — 종료 때 워크트리 묻는 창 등)
    top = max([i for i, ln in enumerate(lines[:first]) if ln.lstrip().startswith(("──", "▔"))] or [-1])
    head = [ln.strip() for ln in lines[top + 1:first]
            if ln.strip() and not ln.lstrip().startswith(("╌", "─", "▔")) and not ln.strip().startswith("Tip:")]
    options = []
    for i in idx:
        on, n, text = _OPTION.match(lines[i]).groups()
        # 「Yes, and switch to auto mode · 설명」·「Keep worktree    Stays at /…」 — 이름과 설명을 가른다
        label, hint = (re.split(r"\s{2,}| · ", text, maxsplit=1) + [""])[:2]
        options.append({"n": int(n), "label": label.strip(), "hint": hint.strip(), "on": bool(on)})
    return {"head": head[-8:], "options": options}


_MENU_FOOT = "Enter to confirm"


def _plain_menu(lines):
    """번호 없는 선택 창(첫 실행의 폴더 신뢰 등 — 실측 v2.1.317):
       「 Quick safety check: … / ❯ No, exit /   Yes, I trust this folder / Enter to confirm · Esc to cancel」
    커서(❯) 줄과 그 위아래로 같은 들여쓰기(커서 자리만큼 안쪽)인 줄들이 선택지다. 없으면 None."""
    foot = next((i for i in range(len(lines) - 1, -1, -1) if _MENU_FOOT in lines[i]), None)
    if foot is None:
        return None
    cur = [i for i in range(foot) if lines[i].lstrip().startswith("❯ ")]
    if not cur:
        return None
    c = cur[-1]
    col = len(lines[c]) - len(lines[c].lstrip()) + 2   # 선택지 글이 시작하는 열

    def item(i):
        ln = lines[i]
        return bool(ln.strip()) and (ln.lstrip().startswith("❯ ") or len(ln) - len(ln.lstrip()) == col)
    start, end = c, c
    while start - 1 >= 0 and item(start - 1) and not lines[start - 1].lstrip().startswith("❯"):
        start -= 1
    while end + 1 < foot and item(end + 1):
        end += 1
    top = max([i for i, ln in enumerate(lines[:start]) if ln.lstrip().startswith(("──", "▔"))] or [-1])
    head = [ln.strip() for ln in lines[top + 1:start] if ln.strip() and not ln.lstrip().startswith(("╌", "─", "▔"))]
    options = [{"n": k + 1, "label": lines[i].strip().lstrip("❯").strip(), "hint": "", "on": i == c}
               for k, i in enumerate(range(start, end + 1))]
    return {"head": head[-8:], "options": options, "plain": True}


@_serial
def answer_prompt(t, n, label):
    """선택 창에서 n 번(그 줄 글이 label 일 때만)을 고른다. Esc 는 보내지 않는다. (성공 여부, 안내 문구)"""
    v = prompt_view(t)
    if not v:
        return False, "선택 창이 없습니다(이미 골랐거나 닫힘)"
    opt = next((o for o in v["options"] if o["n"] == n), None)
    if not opt or _norm_text(opt["label"]) != _norm_text(label):
        return False, "선택 창이 바뀌었습니다 — 다시 확인해 주세요"
    if v.get("plain"):
        # 번호 없는 창: 화살표로 커서를 옮기고 그 줄이 맞는지 다시 본 뒤 Enter
        cur = next((o["n"] for o in v["options"] if o["on"]), 1)
        if n != cur:
            _raw(t, (DOWN if n > cur else UP) * abs(n - cur))
            time.sleep(0.3)
            v2 = prompt_view(t)
            now = next((o["label"] for o in (v2 or {}).get("options", []) if o["on"]), None)
            if _norm_text(now or "") != _norm_text(label):
                return False, "선택지를 맞추지 못했습니다(지금: %s) — 터미널에서 확인해 주세요" % (now or "알 수 없음")
            v = v2
        _raw(t, "\r")
    elif not 1 <= n <= 9:
        return False, "이 번호는 키로 고를 수 없습니다"
    else:
        _raw(t, str(n))
    for _ in range(10):
        time.sleep(0.3)
        if prompt_view(t) != v:
            return True, "「%s」를 골랐습니다" % opt["label"]
    return False, "골랐지만 창이 그대로입니다 — 터미널에서 확인해 주세요"

# ---------------------------------------------------------------- 권한 모드(Shift+Tab 으로 돌아가며 바뀐다)
# 실측: auto → manual → accept edits → plan → auto … 입력칸 아래 줄에 「⏵⏵ auto mode on」·「⏸ manual mode on」·
# 「⏵⏵ accept edits on」·「⏸ plan mode on」. 훅 입력의 permission_mode 와 이름이 다르다
PERM_FOOT = (("auto mode on", "auto"), ("manual mode on", "default"), ("accept edits on", "acceptEdits"),
             ("plan mode on", "plan"), ("bypass permissions on", "bypassPermissions"))
SHIFT_TAB = "\x1b[Z"


def permission_mode(t):
    """화면 맨 아래 줄에서 지금 권한 모드(훅 이름으로), 모르면 None."""
    for ln in reversed(_screen(t)[-4:]):
        low = ln.lower()
        for key, name in PERM_FOOT:
            if key in low:
                return name
    return None


@_serial
def set_permission_mode(t, target):
    """Shift+Tab 을 눌러 가며 target 모드에 맞춘다. (성공 여부, 지금 모드)

    누를 때마다 아래 줄이 바뀐 것을 확인하고 다음을 누른다(늦게 그려지면 한 번 더 눌러 지나치던 것을 막음). 못 읽거나
    안 바뀌면 그 자리에서 멈추고, 한 바퀴 돌아 처음 모드로 오면(그 세션의 순환에 없는 모드) 거기서 멈춘다."""
    if target not in {n for _, n in PERM_FOOT}:
        return False, None
    start = cur = permission_mode(t)
    if cur is None:
        return False, None
    for _ in range(len(PERM_FOOT)):
        if cur == target:
            return True, cur
        _raw(t, SHIFT_TAB)
        prev, cur = cur, None
        for _ in range(8):
            time.sleep(0.25)
            cur = permission_mode(t)
            if cur is not None and cur != prev:
                break
        if cur is None or cur == prev:
            return False, cur
        if cur == start:
            return False, cur
    return cur == target, cur


# ---------------------------------------------------------------- 질문 창(AskUserQuestion) 답하기
# 보드에서 고른 답을 그 터미널의 질문 창에 키로 넣는다. 한 단계마다 화면을 다시 읽어 질문·선택 상태를 확인하고,
# 어긋나면 그 자리에서 멈춘다(Esc 로 창을 닫지 않는다 — 닫으면 질문 자체가 취소된다). 실측(Claude Code 2.1.314):
#   단일 선택  숫자 → 고르고 다음 질문으로(질문이 하나뿐이면 바로 제출)
#   다중 선택  숫자 → 체크 토글(커서는 그대로), 맨 아래 「Submit」 줄에서 Enter → 다음 질문 또는 검토 화면
#   직접 입력  「Type something」 줄에 커서를 두고 글자를 치면 그 줄에 들어간다(단일 선택은 숫자로 그 줄에 커서가 간다)
#   검토 화면  「Review your answers」 → 1(Submit answers)
_ASK_ROW = re.compile(r"^(❯)?\s*(\d+)\.\s+(?:\[(.)\]\s+)?(.*?)\s*$")
_ASK_SUBMIT = re.compile(r"^(❯)?\s+Submit\s*$")
_ask_lock = threading.Lock()


def ask_on_screen(t):
    """질문 창(AskUserQuestion)이 터미널에 떠 있으면 True, 평소 입력칸만 보이면 False, 화면을 못 읽으면 None."""
    lines = _screen(t)
    if not lines:
        return None
    if _ask_view(lines):
        return True
    return False if _input_line_of(lines) is not None else None


def _ask_view(lines):
    """질문 창 화면 해석. {'kind': 'question'|'review', ...} 또는 None(창이 없음)."""
    # v2.1.316 은 질문 글 앞에 세로줄(「│ 질문…」)을 그린다 — 떼고 읽는다(그대로 두면 질문 글이 달라 「창이 안 보인다」로 멈췄다)
    lines = [re.sub(r"^\s*│\s?", "", ln.replace("\xa0", " ")).rstrip() for ln in lines]
    if not any("Enter to select" in ln or "Ready to submit your answers" in ln for ln in lines[-25:]):
        return None
    top = None
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if s.startswith("←") or (s[:1] in ("☐", "☒") and len(s) < 80):
            top = i
            break
    if top is None:
        return None
    body = lines[top + 1:]
    first = next((ln.strip() for ln in body if ln.strip()), "")   # v2.1.318 은 제목 위에 빈 줄을 하나 더 그린다
    if first == "Review your answers":
        answers, cur, in_answer = [], None, False
        for ln in body[body.index(next(ln for ln in body if ln.strip())) + 1:]:
            s = ln.strip()
            if s.startswith("Ready to submit"):
                break
            if s.startswith("●"):
                cur, in_answer = [s[1:].strip(), ""], False
                answers.append(cur)
            elif s.startswith("→") and cur is not None:
                cur[1], in_answer = s[1:].strip(), True
            elif s and cur is not None:   # 칸 너비에서 감긴 줄 — 답이면 답에, 아니면 질문에 잇는다
                cur[1 if in_answer else 0] += " " + s
        return {"kind": "review", "answers": answers}
    question, rows, submit_cursor, started = [], [], False, False
    # 선택지 아래 가로줄 밑의 「N. Chat about this」 — 답하지 않고 대화로(질문을 거절하고 입력칸으로 돌아간다)
    chat = next((int(m.group(2)) for m in (_ASK_ROW.match(ln) for ln in body) if m and m.group(4).strip() == "Chat about this"), None)
    for ln in body:
        m = _ASK_ROW.match(ln)
        if m and int(m.group(2)) == len(rows) + 1:
            started = True
            rows.append({"n": int(m.group(2)), "label": m.group(4), "checked": m.group(3) not in (None, " "),
                         "box": m.group(3) is not None, "cursor": bool(m.group(1)), "more": []})
        elif _ASK_SUBMIT.match(ln):
            submit_cursor = bool(_ASK_SUBMIT.match(ln).group(1))
        elif started and rows and ln.strip() and not ln.startswith("─"):
            rows[-1]["more"].append(ln.strip())   # 그 선택지의 설명 줄, 또는 직접 입력한 글이 감긴 줄
        elif not started and ln.strip():
            question.append(ln.strip())
        elif ln.startswith("─"):
            break
    return {"kind": "question", "question": " ".join(question), "rows": rows, "submit_cursor": submit_cursor, "chat": chat}


def _norm_q(s):
    return re.sub(r"\s+", "", s or "")


def _other_text(row):
    """직접 입력 줄의 글 전체(칸 너비에서 감긴 줄까지 — 빈칸은 뺀 비교용)."""
    return _norm_q(row["label"] + "".join(row.get("more") or [])) if row else ""


def _other_ok(row, other):
    got, want = _other_text(row), _norm_q(other)
    # 아주 긴 글은 창이 앞뒤만 보여 줄 수 있다 — 앞부분과 끝부분이 맞으면 들어간 것
    return got == want or (len(want) > 300 and got.startswith(want[:100]) and want[-30:] in got)


@_serial
def answer_ask(t, questions, answers):
    """(성공 여부, 안내 문구). answers[i] = {"choices": [1-기반 번호…], "other": "직접 입력"}."""
    if not t:
        return False, "터미널을 찾지 못했습니다"
    if not _ask_lock.acquire(blocking=False):
        return False, "다른 답을 넣는 중입니다"
    try:
        return _answer_ask(t, questions, answers)
    finally:
        _ask_lock.release()


@_serial
def decline_ask(t, questions):
    """질문 창에서 「Chat about this」 — 답하지 않고 대화로. 첫 질문과 같은 창일 때만, 그 번호 키만(Esc 없음). (성공 여부, 안내)"""
    v = _ask_view(_screen(t))
    qs = {_norm_q(q.get("question"))[:40] for q in questions}
    if not v or v["kind"] != "question" or not any(_norm_q(v["question"]).startswith(q) for q in qs):
        return False, "질문 창이 터미널에 보이지 않습니다"
    if not v.get("chat") or not 1 <= v["chat"] <= 9:
        return False, "「Chat about this」 줄이 없습니다(이 Claude Code 에는 없는 기능일 수 있음)"
    _raw(t, str(v["chat"]))
    for _ in range(16):
        time.sleep(0.25)
        if _ask_view(_screen(t)) is None:
            return True, "질문에 답하지 않고 대화로 넘겼습니다 — 하고 싶은 말을 입력창에 쓰세요"
    return False, "눌렀지만 질문 창이 그대로입니다 — 터미널에서 확인해 주세요"


RC_DISCONNECT = "Disconnect this session"


@_serial
def rc_disconnect(t):
    """원격(Remote Control)이 켜진 세션에 /remote-control 을 보내 상태 창을 열고 「Disconnect this session」을 고른다.
    상태 창(v2.1.318 실측): 「Remote Control / … / Disconnect this session / Show QR code … / ❯ Continue / Enter to select · Esc to continue」
    — 번호 없이 커서(❯)로 고른다. (성공 여부, 안내)"""
    if not t:
        return False, "터미널을 찾지 못했습니다"
    box = dialog_text(t)
    if box:
        return False, "터미널에 선택 창이 떠 있습니다: " + box
    if not (t.terminal.write(t.handle, "/remote-control") and _landed(t, "/remote-control")):
        return False, "입력 전송 실패"
    time.sleep(0.3)
    t.terminal.write(t.handle, "\r")

    def opts():
        lines = [ln.replace("\xa0", " ").rstrip() for ln in _screen(t)]
        if not any(RC_DISCONNECT in ln for ln in lines):
            return None
        rows = [ln for ln in lines if ln.strip().lstrip("❯").strip().startswith((RC_DISCONNECT, "Show QR code", "Continue"))]
        names = [r.strip().lstrip("❯").strip() for r in rows]
        cur = next((i for i, r in enumerate(rows) if r.strip().startswith("❯")), None)
        tgt = next((i for i, n in enumerate(names) if n.startswith(RC_DISCONNECT)), None)
        return cur, tgt
    v = None
    for _ in range(16):
        time.sleep(0.25)
        v = opts()
        if v and v[0] is not None:
            break
    if not v or v[0] is None or v[1] is None:
        if v:
            _raw(t, ESC)
        return False, "원격 상태 창을 찾지 못했습니다(이미 꺼져 있을 수 있음)"
    cur, tgt = v
    if cur != tgt:
        _raw(t, (UP if tgt < cur else DOWN) * abs(cur - tgt))
        time.sleep(0.35)
        v = opts()
        if not v or v[0] != v[1]:
            _raw(t, ESC)
            return False, "「Disconnect」 줄로 가지 못했습니다 — 터미널에서 확인해 주세요"
    _raw(t, "\r")
    for _ in range(12):
        time.sleep(0.25)
        if opts() is None:
            return True, "원격(Remote Control)을 껐습니다"
    return False, "끄기를 눌렀지만 상태 창이 그대로입니다 — 터미널에서 확인해 주세요"


OTHER_PLACEHOLDER = "Type something."   # 빈 직접 입력 줄의 자리 글(v2.1.318 실측)


def _clear_other(t, row, view, n):
    """직접 입력 줄에 남은 글(앞선 시도·사용자가 쓰다 만 것)을 Ctrl+U 로 지운다. 못 지웠으면 안내 문구, 지웠으면 None.
    빈 줄의 자리 글(「Type something」 등)은 그대로 둬도 된다 — 지운 뒤 글이 바뀌었는지로만 본다."""
    before = _other_text(row)
    if not before or before == _norm_q(OTHER_PLACEHOLDER):   # 빈 줄(자리 글만)
        return None
    _raw(t, "\x15")
    v = view(lambda v: v and len(v["rows"]) > n and _other_text(v["rows"][n]) != before, tries=8)
    after = _other_text(v["rows"][n]) if v and len(v["rows"]) > n else before
    if after != before and (after == _norm_q(OTHER_PLACEHOLDER) or len(after) < len(before)):
        return None
    return "직접 입력 칸에 이미 다른 글이 있어 넣지 않았습니다"


def _answer_ask(t, questions, answers):
    def view(pred, tries=16):
        v = None
        for _ in range(tries):
            v = _ask_view(_screen(t))
            if pred(v):
                return v
            time.sleep(0.25)
        return v

    def stop(msg):
        return False, msg + " — 터미널에서 마저 답해 주세요"

    def is_q(i):
        return lambda v: v and v["kind"] == "question" and _norm_q(v["question"]).startswith(
            _norm_q(questions[i]["question"])[:40])

    needs_review = len(questions) > 1 or any(q.get("multiSelect") for q in questions)

    def review_ok(v):
        """검토 화면의 답이 보드에서 고른 답과 같은지 — (같은지, 다른 답 안내)."""
        for (qt, got), q, a in zip(v["answers"], questions, answers):
            opts = q.get("options") or []
            labels = [opts[int(c) - 1]["label"] for c in sorted({int(c) for c in a.get("choices") or []}) if 1 <= int(c) <= len(opts)]
            other = re.sub(r"\s+", " ", (a.get("other") or "")).strip()
            expect = ", ".join(labels + ([other] if other else []))
            if _norm_q(got) != _norm_q(expect):
                return False, "검토 화면의 답(%s)이 고른 답(%s)과 다릅니다" % (got, expect)
        return len(v["answers"]) == len(questions), "검토 화면의 답 수가 질문 수와 다릅니다"

    # 앞선 시도가 검토 화면(Review your answers)까지 와서 멈춘 경우: 답이 같으면 바로 제출, 다르면 ← 로 첫 질문까지 돌아가 다시 고른다
    cur = _ask_view(_screen(t))
    if cur and cur["kind"] == "review":
        ok, _ = review_ok(cur)
        if ok:
            _raw(t, "1")
            return (True, "답을 보냈습니다") if view(lambda v: v is None) is None else stop("제출했지만 질문 창이 아직 닫히지 않았습니다")
        for _ in range(len(questions) + 1):
            _raw(t, LEFT)
            time.sleep(0.3)
            if is_q(0)(_ask_view(_screen(t))):
                break
    if not view(is_q(0)) or not is_q(0)(_ask_view(_screen(t))):
        return stop("질문 창이 터미널에 보이지 않습니다")
    for i, q in enumerate(questions):
        v = view(is_q(i))
        if not is_q(i)(v):
            return stop("%d번째 질문 화면을 찾지 못했습니다" % (i + 1))
        n = len(q.get("options") or [])
        a = answers[i]
        other = re.sub(r"\s+", " ", (a.get("other") or "")).strip()
        choices = sorted({int(c) for c in a.get("choices") or [] if 1 <= int(c) <= n})
        if len(v["rows"]) < n + 1:
            return stop("선택지 줄을 읽지 못했습니다")
        last = i == len(questions) - 1
        if not q.get("multiSelect"):
            if other:
                # 이미 직접 입력 줄에 있으면(앞선 시도가 글을 넣다 멈춘 경우 등) 번호를 누르지 않는다 — 누르면 숫자가 글로 들어갔다
                if not v["rows"][n]["cursor"]:
                    _raw(t, str(n + 1))
                v = view(lambda v: v and v["kind"] == "question" and len(v["rows"]) > n and v["rows"][n]["cursor"])
                if not (v and len(v["rows"]) > n and v["rows"][n]["cursor"]):
                    return stop("직접 입력 줄로 가지 못했습니다")
                if not _other_ok(v["rows"][n], other):
                    err = _clear_other(t, v["rows"][n], view, n)
                    if err:
                        return stop(err)
                    _raw(t, other)
                    v = view(lambda v: v and len(v["rows"]) > n and _other_ok(v["rows"][n], other))
                    if not (v and len(v["rows"]) > n and _other_ok(v["rows"][n], other)):
                        return stop("직접 입력한 글이 확인되지 않습니다")
                _raw(t, "\r")
            elif len(choices) == 1:
                _raw(t, str(choices[0]))
            else:
                return stop("단일 선택 질문에 답이 하나가 아닙니다")
        else:
            for c in choices:
                _raw(t, str(c))
                time.sleep(0.2)
            if other:
                _raw(t, str(n + 1))
                time.sleep(0.25)
                v = _ask_view(_screen(t))
                cur = next((r["n"] for r in (v or {}).get("rows", []) if r["cursor"]), None)
                if cur is None:
                    return stop("커서 위치를 읽지 못했습니다")
                if cur != n + 1:
                    _raw(t, (DOWN if n + 1 > cur else UP) * abs(n + 1 - cur))
                    time.sleep(0.3)
                _raw(t, other)
                time.sleep(0.3)
            want = set(choices)

            def boxes_ok(v):
                if not (v and v["kind"] == "question" and len(v["rows"]) > n):
                    return False
                got = {r["n"] for r in v["rows"][:n] if r["checked"]}
                o = v["rows"][n]
                return got == want and o["checked"] == bool(other) and (not other or _other_ok(o, other))
            v = view(boxes_ok)
            if not boxes_ok(v):
                return stop("체크 상태가 고른 답과 다릅니다")
            cur = next((r["n"] for r in v["rows"] if r["cursor"]), None)
            if not v["submit_cursor"]:
                steps = (n + 2 - cur) if cur else n + 2
                _raw(t, DOWN * steps)
                v = view(lambda v: v and v["kind"] == "question" and v["submit_cursor"])
                if not (v and v["submit_cursor"]):
                    return stop("Submit 줄로 가지 못했습니다")
            _raw(t, "\r")
        if last:
            break
        v = view(is_q(i + 1))
        if not is_q(i + 1)(v):
            return stop("다음 질문으로 넘어가지 않았습니다")
    if needs_review:
        v = view(lambda v: v and v["kind"] == "review")
        if not (v and v["kind"] == "review"):
            return stop("검토 화면이 나오지 않았습니다")
        ok, why = review_ok(v)
        if not ok:
            return stop(why)
        _raw(t, "1")
    v = view(lambda v: v is None)
    if v is not None:
        return stop("답을 넣었지만 질문 창이 아직 닫히지 않았습니다")
    return True, "답을 보냈습니다"
