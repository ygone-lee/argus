"""Argus 보드 웹 서버. 127.0.0.1 에서만 열리고 외부로 아무것도 보내지 않는다.

    /usr/bin/python3 csl/server.py          # http://127.0.0.1:7799
"""
import base64
import binascii
import datetime
import errno
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csl import agentsetup, boards, brain, cardchat, codex, commands, config, connectors, daily, delegate, devlog, focus, gitreport, hook, inboxrun, locate, notesreport, sendqueue, skills, store, terminals, update, webterm  # noqa: E402
from csl.terminals import tmux as tmux_term  # noqa: E402
from csl.terminals import web as web_term  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
ATTACHABLE = ("web", "tmux")   # 보드 칸 안(xterm.js)에서 열 수 있는 터미널


TERM_APPS = {"iTerm.app": "iTerm2", "Apple_Terminal": "터미널.app", "vscode": "VS Code", "ghostty": "Ghostty",
             "WarpTerminal": "Warp", "WezTerm": "WezTerm", "Hyper": "Hyper", "Tabby": "Tabby", "kitty": "kitty"}


def term_label(raw, tg):
    """세션이 도는 곳의 이름 — 조작할 수 있는 터미널이면 그 이름, 아니면 훅이 기록한 앱(TERM_PROGRAM 등). 모르면 ""."""
    if tg:
        return tg.terminal.label
    try:
        term = json.loads(raw or "{}") or {}
    except ValueError:
        term = {}
    if term.get("entrypoint") == "claude-desktop":
        return "Claude 데스크톱 앱"
    prog = term.get("program") or ""
    return TERM_APPS.get(prog, prog[:24])


def security_headers():
    """모든 응답에 붙이는 보안 헤더. 보드는 슬랙·메일 본문·대화 기록 같은 바깥 글을 그리고, 브라우저 안에 터미널이 있다 —
    - CSP: 보드 파일(static)의 스크립트만 실행(인라인·eval·바깥 스크립트 금지), 연결은 이 서버(웹소켓 포함)로만,
      이미지는 이 서버와 data: 만. 만에 하나 글이 HTML 로 새어 들어와도 스크립트가 돌거나 바깥으로 보낼 수 없다
    - frame-ancestors·X-Frame-Options: 다른 사이트가 보드를 iframe 에 깔고 클릭·키 입력을 유도하지 못하게(클릭재킹)
    - Referrer-Policy: 바깥 링크를 열 때 보드 주소를 넘기지 않는다"""
    ws = " ".join("ws://%s:%d" % (h, store.PORT) for h in sorted(ALLOWED_HOSTS))
    csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
           "font-src 'self' data:; connect-src 'self' " + ws + "; media-src 'self' data:; object-src 'none'; frame-src 'none'; "
           "worker-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return (("Content-Security-Policy", csp), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"))
SESSION_COLUMNS = (
    "session_id, cwd, project_root, project_name, worktree, branch, title, title_source, recap, recap_at,"
    " state, state_detail, state_at, started_at, last_activity, last_seen_at, ended_at, archived, pinned, source,"
    " remote, rc_requested_at, model, effort, transcript_path, compact_at, compact_trigger, compact_ms, terminal, ask,"
    " continued_from, branch_kind, branch_at, bg, permission_mode, team_agent, agent"
)

MAX_BODY = 40 * 1024 * 1024
# 보드에서 고를 수 있는 모델(/model 선택 창의 이름 그대로)
MODEL_CHOICES = tuple(config.CARDCHAT_MODELS)   # 선택 창의 모델 이름(카드 대화와 같은 목록)
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
UPLOAD_DIR = store.UPLOAD_DIR
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def save_images(images):
    """data URL 이미지들을 uploads/ 에 저장하고 경로 목록을 돌려준다. 형식이 틀리면 ValueError."""
    if len(images) > MAX_IMAGES:
        raise ValueError("이미지는 한 번에 %d장까지 보낼 수 있습니다" % MAX_IMAGES)
    paths = []
    os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
    for img in images:
        # 이미 올려 둔 파일(넥스트 액션에 붙인 이미지)은 uploads/ 안의 것만 그대로 쓴다
        existing = (img or {}).get("path")
        if existing:
            full = os.path.realpath(existing)
            if not full.startswith(os.path.realpath(UPLOAD_DIR) + os.sep) or not os.path.isfile(full):
                raise ValueError("붙여 둔 이미지 파일이 없습니다")
            paths.append(full)
            continue
        data_url = (img or {}).get("data") or ""
        if "," not in data_url:
            raise ValueError("이미지 데이터가 올바르지 않습니다")
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("이미지 데이터가 올바르지 않습니다")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("이미지 한 장은 15MB 이하여야 합니다")
        ext = next((e for magic, e in IMAGE_MAGIC if raw.startswith(magic)), None)
        if ext is None and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            ext = "webp"
        if ext is None:
            raise ValueError("PNG·JPEG·GIF·WebP 이미지만 보낼 수 있습니다")
        path = os.path.join(UPLOAD_DIR, "%s-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3), ext))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        paths.append(path)
    return paths


def session_row(conn, sid):
    row = conn.execute("SELECT session_id, title, title_source, cwd, project_root, terminal, state, remote, rc_requested_at, ask,"
                       " agent, model, effort, last_activity FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return dict(row) if row else None


_HELD = threading.local()   # 이 요청 스레드가 잡은 터미널 잠금 — 응답을 보낸 뒤 _dispatch 가 푼다


_SLACK_KEY = re.compile(r"([CDG][A-Z0-9]{6,}):(\d{9,11}\.\d{6})")


def _thread_root(thread_key):
    """카드 thread_key 의 스레드 시작 ts. DM 은 「하루치」로 묶어 키 끝이 날짜(2026-10-29)라 스레드가 아니다 — None."""
    root = (thread_key or "").rsplit(":", 1)[-1] if (thread_key or "").startswith("slack:") else ""
    return root if re.fullmatch(r"\d{9,11}\.\d{6}", root) else None


def slack_link(conn, key, thread_key=None):
    """카드 key(채널:ts)로 슬랙 메시지 주소 — 브레인이 permalink 를 비워 둔 카드용. 워크스페이스 주소는 같은 채널(없으면 아무)
    카드·수집 항목에 남은 주소에서 빌린다. 모르면 "". """
    m = _SLACK_KEY.search(key or "")
    if not m:
        return ""
    ch, ts = m.groups()
    row = None
    for sql in ("SELECT permalink FROM inbox WHERE permalink LIKE ? LIMIT 1", "SELECT link FROM feed_items WHERE link LIKE ? LIMIT 1"):
        row = row or conn.execute(sql, ("%/archives/" + ch + "/%",)).fetchone()
    row = row or conn.execute("SELECT permalink FROM inbox WHERE permalink LIKE 'https://%.slack.com/%' LIMIT 1").fetchone()
    dm = re.match(r"https://([\w.-]+\.slack\.com)/", row[0] or "") if row else None
    if not dm:
        return ""
    root = _thread_root(thread_key)
    return "https://%s/archives/%s/p%s%s" % (dm.group(1), ch, ts.replace(".", ""), "?thread_ts=" + root if root and root != ts else "")


def _mark_prompt(conn, sid, detail):
    """훅 없이 뜬 선택 창(종료 중 워크트리 남기기 등) — 「확인 필요」로 표시해 칸에 🔐 카드가 뜨게 한다."""
    conn.execute("UPDATE sessions SET state='waiting', state_detail=?, state_at=?, ask=NULL WHERE session_id=? AND state != 'ended'",
                 (detail, store.now(), sid))
    conn.commit()


def _hold_terminal(tg):
    lk = focus.hold(tg)
    if not lk.acquire(timeout=60):
        return False
    if not hasattr(_HELD, "locks"):
        _HELD.locks = []
    _HELD.locks.append(lk)
    return True


def _release_terminals():
    for lk in getattr(_HELD, "locks", [])[::-1]:
        lk.release()
    _HELD.locks = []


NOT_RUNNING = "이 세션은 지금 어느 터미널에서도 실행 중이 아닙니다(탭을 닫았거나 Claude Code 가 꺼졌습니다)"


def not_running(conn, sid):
    """Claude Code 실행 목록으로 보아 그 세션을 돌리는 프로세스가 없으면 True. 목록을 못 읽으면 False(모름).

    탭을 강제로 닫거나 재부팅하면 종료 훅이 오지 않아 보드에는 「대기」로 남는다 — 그런 세션을 가려낸다.
    방금(1분 안) 활동한 세션은 믿지 않는다: 막 뜨는 중이거나 실행 목록이 늦게 바뀐 것일 수 있다."""
    run = focus.running_sessions()
    if run is None or sid in run:
        return False
    row = conn.execute("SELECT last_activity FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return bool(row) and store.now() - (row[0] or 0) > 60


def terminal_target(conn, sess, need_alive=True):
    """보드에서 글을 보낼 터미널(csl/terminals 의 Target). (Target, None) 또는 (None, 오류 문구)

    찾은 터미널은 이 요청이 끝날 때까지 잡아 둔다(예약 보내기·위임·브레인이 그사이 같은 터미널에 키를 넣지 않게).
    그 전에 이 요청의 DB 쓰기를 커밋한다 — 터미널 조작(수 초) 내내 쓰기 잠금을 쥐면 모든 세션의 훅이 막힌다."""
    conn.commit()
    if need_alive and not_running(conn, sess["session_id"]):
        return None, NOT_RUNNING
    tg, found = focus.resolve_target(sess)
    if not tg:
        if need_alive:
            tg, err = attach_background(conn, sess)
            if tg or err:
                return tg, err
        return None, "이 세션의 터미널 탭을 찾지 못했습니다. 그 터미널에서 요청을 한 번 보내면 연결됩니다"
    if not _hold_terminal(tg):
        return None, "그 터미널에서 다른 조작이 진행 중입니다 — 잠시 뒤 다시 해 주세요"
    if found:
        conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(found), sess["session_id"]))
        conn.commit()
        sess["terminal"] = json.dumps(found)
    if need_alive and not focus.claude_alive(sess, tg):
        tg, err = attach_background(conn, sess)
        if tg or err:
            return tg, err
        return None, "그 터미널에서 Claude Code 가 실행 중이 아닙니다(종료됐거나 셸만 남음)"
    return tg, None


DIFF_MAX = 400000           # 보여 줄 diff 글자 수
UNTRACKED_MAX = (60, 200000)   # 새 파일: 최대 개수, 파일 하나 최대 바이트


def _git(d, *a, timeout=15):
    try:
        r = subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _untracked_diff(d, budget):
    """추적 안 된 새 파일을 「새 파일」 diff 로(git diff 엔 안 나온다). 바이너리·큰 파일은 한 줄로만."""
    names = [n for n in _git(d, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if n]
    root = os.path.realpath(d)
    parts, skipped = [], max(0, len(names) - UNTRACKED_MAX[0])
    for n in names[:UNTRACKED_MAX[0]]:
        full = os.path.realpath(os.path.join(d, n))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            continue
        head = "diff --git a/%s b/%s\nnew file mode 100644\n" % (n, n)
        try:
            size = os.path.getsize(full)
            with open(full, "rb") as f:
                raw = f.read(UNTRACKED_MAX[1] + 1)
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            parts.append(head + "Binary files /dev/null and b/%s differ\n" % n)
            continue
        if size > UNTRACKED_MAX[1] or budget <= 0:
            parts.append(head + "--- /dev/null\n+++ b/%s\n@@ -0,0 +1,0 @@ (너무 커서 내용은 생략 — %d바이트)\n" % (n, size))
            continue
        text = raw.decode("utf-8", "replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        body = "".join("+" + ln + "\n" for ln in lines)
        budget -= len(body)
        parts.append(head + "--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n%s" % (n, len(lines), body))
    return "".join(parts), skipped


def _base_ref(d):
    """기본 브랜치(origin/HEAD → origin/main·main·master 순으로 있는 것)."""
    ref = _git(d, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
    for cand in ([ref] if ref else []) + ["origin/main", "main", "origin/master", "master"]:
        if _git(d, "rev-parse", "--verify", "--quiet", cand + "^{commit}").strip():
            return cand
    return None


DIFF_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
DIFF_IMAGE_MAX = 20 * 1024 * 1024


def diff_image(d, rel, side, mode):
    """변경 보기의 이미지 파일 한쪽: (형식, 바이트) 또는 None. 읽기만 한다.

    old = 비교 기준(커밋 안 한 변경이면 HEAD, 브랜치 전체면 기본 브랜치와 갈라진 곳)의 그 파일,
    new = 지금 작업 트리의 그 파일. 경로는 저장소 맨 위 기준(git diff)이거나 세션 폴더 기준(추적 안 된 새 파일).
    SVG 는 같은 출처에서 열면 스크립트가 돌 수 있어 다루지 않는다(글이라 diff 로 보인다)."""
    ctype = DIFF_IMAGE_TYPES.get(os.path.splitext(rel or "")[1].lower())
    if not ctype or rel.startswith(("/", "-")) or "\0" in rel or ".." in rel.split("/"):
        return None
    top = _git(d, "rev-parse", "--show-toplevel").strip() or d
    if side == "old":
        against = "HEAD"
        if mode == "branch":
            base = _base_ref(d)
            against = (base and _git(d, "merge-base", "HEAD", base).strip()) or "HEAD"
        try:
            r = subprocess.run(["git", "-C", top, "show", "%s:%s" % (against, rel)], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return (ctype, r.stdout) if r.returncode == 0 and 0 < len(r.stdout) <= DIFF_IMAGE_MAX else None
    for root in (top, d):
        root = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root, rel))
        if full.startswith(root + os.sep) and os.path.isfile(full) and os.path.getsize(full) <= DIFF_IMAGE_MAX:
            try:
                with open(full, "rb") as f:
                    return ctype, f.read()
            except OSError:
                return None
    return None


def git_diff(d, mode="work"):
    """변경 보기 데이터: diff(통합 형식)·바뀐 파일 수 기준·브랜치 모드면 기준과 커밋 목록."""
    out = {"mode": "work", "base": None, "commits": [], "note": None}
    against = "HEAD"
    if mode == "branch":
        base = _base_ref(d)
        mb = base and _git(d, "merge-base", "HEAD", base).strip()
        if not mb:
            out["note"] = "기본 브랜치(main 등)를 찾지 못해 커밋 안 한 변경만 보여 줍니다"
        else:
            out.update(mode="branch", base=base)
            against = mb
            out["commits"] = [dict(zip(("sha", "subject", "at"), ln.split("\x1f", 2)))
                              for ln in _git(d, "log", "--format=%h\x1f%s\x1f%ct", "-n", "60", mb + "..HEAD").splitlines() if ln]
            if not out["commits"] and _git(d, "rev-parse", "HEAD").strip() == mb:
                out["note"] = "이 브랜치에는 %s 에 없는 커밋이 없습니다 — 커밋 안 한 변경만 있습니다" % base
    diff = _git(d, "diff", against, "--no-color", "--no-ext-diff", "-M")
    extra, skipped = _untracked_diff(d, DIFF_MAX - len(diff))
    diff += extra
    out.update(diff=store.mask(diff[:DIFF_MAX], private=False), cut=len(diff) > DIFF_MAX, untracked_skipped=skipped,
               head=_git(d, "log", "-1", "--format=%h %s").strip(),
               status=_git(d, "status", "--porcelain=v1"), stat=_git(d, "diff", against, "--stat", "--no-color", "-M"))   # ./argus diff 가 쓴다
    return out


def live_agent(sid):
    """그 세션의 돌고 있는 백그라운드 프로세스(claude agents 항목) 또는 None.
    백그라운드 항목만 — 대화형(터미널에서 도는) 항목도 pid 가 있지만 attach 에 쓸 id 가 없다(예전 /fork 세션을 터미널에서
    이어 열면 bg 표시가 남은 채 대화형으로 잡혀, 다시 시작이 KeyError 'id' 로 실패했다). 멈춘 백그라운드는 pid 가 없다."""
    return next((a for a in agents_list(fresh=True) if a.get("sessionId") == sid and a.get("kind") == "background"
                 and a.get("pid") and a.get("id")), None)


def attach_background(conn, sess, wait=25):
    """백그라운드 세션(/fork·claude --bg)은 터미널 탭이 없거나, 붙였던 탭에서 빠져나와 셸만 남은 게 보통이다 —
    거절하지 않고 새 탭에 `claude attach` 로 붙여 그 탭을 이 세션의 터미널로 기억하고, 입력칸이 뜰 때까지 기다린다.
    (Target, None) · (None, 오류) · 백그라운드로 돌고 있는 세션이 아니면 (None, None)."""
    row = conn.execute("SELECT bg, cwd FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()
    x = live_agent(sess["session_id"]) if row and row["bg"] else None
    if not x:
        return None, None
    tg, err = focus.new_claude_terminal(x.get("cwd") or row["cwd"] or "", extra="attach " + shlex.quote(x["id"]), terminal="web")
    if not tg:
        return None, err
    LAUNCHED[tg.handle] = store.now()
    sess["terminal"] = json.dumps(tg.terminal.remember({}, tg.handle))
    conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (sess["terminal"], sess["session_id"]))
    conn.commit()
    if not _hold_terminal(tg):
        return None, "새 탭에서 다른 조작이 진행 중입니다 — 잠시 뒤 다시 해 주세요"
    end = time.time() + wait
    while time.time() < end:
        if focus.input_line(tg) is not None:
            return tg, None
        time.sleep(0.5)
    return None, "백그라운드 세션을 새 터미널 탭에 붙였지만 입력칸이 뜨지 않았습니다 — 그 탭을 확인해 주세요"


def _like(word):
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(text, words, width=70):
    low = text.lower()
    i = min([low.find(w.lower()) for w in words if low.find(w.lower()) >= 0] or [0])
    start, end = max(0, i - width), min(len(text), i + width * 2)
    return ("…" if start else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")


def _phrase(word):
    return '"' + word.replace('"', '""') + '"'


def search_turns(conn, q, sid=None, who="all", limit=60, sort="recent", project=None, exclude=None, any_word=False, same_project=None):
    """요청·응답 전문에서 단어(공백으로 나눈 것 모두 포함)를 찾는다. 최근 것부터(sort=rank 면 관련도 순).

    세 글자 이상인 단어는 전문 검색 색인(turns_fts, trigram)으로, 두 글자 이하는 색인이 못 찾으므로 LIKE 로 거른다.
    same_project: 이 세션과 같은 프로젝트(워크트리는 원래 저장소로 묶음)의 세션에서만.
    any_word: 단어 중 하나라도 든 턴(세 글자 이상 단어만, 관련도 순) — 모두 든 결과가 없을 때 넓혀 찾기용.
    """
    words = [w for w in q.split() if w][:6]
    if not words:
        return []
    long_words = [w for w in words if len(w) >= 3]
    short_words = [w for w in words if len(w) < 3]
    joiner = " AND "
    if any_word and long_words:
        joiner, short_words, sort = " OR ", [], "rank"
    params = []

    def cond(col):
        parts = []
        if long_words:
            parts.append("t.id IN (SELECT rowid FROM turns_fts WHERE turns_fts MATCH ?)")
            params.append(col + " : (" + joiner.join(_phrase(w) for w in long_words) + ")")
        for w in short_words:
            parts.append("t." + col + " LIKE ? ESCAPE '\\'")
            params.append(_like(w))
        return "(" + " AND ".join(parts) + ")"
    sides = []
    if who in ("all", "me"):   # 백그라운드 작업 알림(<task-notification>)은 사용자가 쓴 요청이 아니라 빼고 찾는다
        sides.append("(" + cond("prompt") + " AND t.prompt NOT LIKE '<task-notification>%')")
    if who in ("all", "claude"):
        sides.append(cond("response"))
    where = "(" + " OR ".join(sides) + ")"
    if sid:
        where += " AND t.session_id = ?"
        params.append(sid)
    if exclude:   # 찾고 있는 세션 자신(방금 보낸 질문이 걸리지 않게)
        where += " AND t.session_id != ?"
        params.append(exclude)
    if same_project:
        where += (" AND COALESCE(s.project_root, s.cwd) = (SELECT COALESCE(project_root, cwd) FROM sessions"
                  " WHERE session_id = ?)")
        params.append(same_project)
    if project:   # 프로젝트 이름 또는 경로 일부
        where += " AND (s.project_name LIKE ? ESCAPE '\\' OR COALESCE(s.project_root, s.cwd) LIKE ? ESCAPE '\\')"
        params += [_like(project), _like(project)]
    order = "COALESCE(t.prompt_at, t.response_at) DESC, t.id DESC"
    if sort == "rank" and long_words:   # bm25: 작을수록 관련도가 높다
        order = ("(SELECT bm25(turns_fts) FROM turns_fts WHERE turns_fts MATCH ? AND rowid = t.id), " + order)
        params.append(joiner.join(_phrase(w) for w in long_words))
    rows = conn.execute(
        "SELECT t.id, t.session_id, t.prompt, t.response, t.prompt_at, t.response_at,"
        " s.title, s.project_name, s.project_root, s.cwd, s.state, s.archived, s.agent,"
        # 그 세션에서 뒤에서 몇 번째 턴인지(보드가 최근 N턴만 불러오므로 몇 턴까지 불러올지 계산용)
        " (SELECT COUNT(*) FROM turns t2 WHERE t2.session_id = t.session_id AND"
        "   (COALESCE(t2.prompt_at, t2.response_at) > COALESCE(t.prompt_at, t.response_at)"
        "    OR (COALESCE(t2.prompt_at, t2.response_at) = COALESCE(t.prompt_at, t.response_at) AND t2.id >= t.id))) AS from_end"
        " FROM turns t JOIN sessions s ON s.session_id = t.session_id"
        " WHERE " + where + " ORDER BY " + order + " LIMIT ?",
        params + [limit],
    ).fetchall()
    out = []
    for r in rows:
        prompt, resp = r["prompt"] or "", r["response"] or ""
        if store.is_task_notice(prompt):
            prompt = ""   # 응답 쪽에서 찾은 것 — 알림 글을 「나」의 발췌로 보이지 않게
        # 단어가 더 많이 든 쪽을 발췌(일부 일치 검색이면 요청·응답 어느 쪽도 모든 단어를 갖지 않을 수 있다)
        hits_p = sum(w.lower() in prompt.lower() for w in words)
        hits_r = sum(w.lower() in resp.lower() for w in words)
        in_prompt = who == "me" or (who == "all" and hits_p > 0 and hits_p >= hits_r)
        text = prompt if in_prompt else resp
        out.append({
            "turn_id": r["id"], "session_id": r["session_id"], "in": "prompt" if in_prompt else "response",
            "snippet": _snippet(text, words), "at": (r["prompt_at"] if in_prompt else r["response_at"]) or r["prompt_at"],
            "title": r["title"], "project_name": r["project_name"], "project_root": r["project_root"] or r["cwd"],
            "state": r["state"], "archived": r["archived"], "from_end": r["from_end"], "agent": r["agent"] or "claude",
        })
    return out


SLACK_LINK = re.compile(r"[ \t]*<?https://[\w.-]+\.slack\.com/archives/[^\s>]+>?")


def strip_slack_links(text):
    """카드 프롬프트에서 슬랙 메시지 링크를 뺀다 — 받는 세션엔 쓸모없고 원문은 카드의 「원문 ↗」로 본다."""
    text = SLACK_LINK.sub("", text or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def capture_btw(t, aside_id, question, **kw):
    """곁질문(/btw) 답을 터미널 화면에서 읽어 저장하고 창을 닫는다(백그라운드)."""
    answer, err = focus.read_btw(t, question, **kw)
    conn = store.connect()
    try:
        with conn:
            conn.execute("UPDATE asides SET answer=?, status=?, answered_at=? WHERE id=?",
                         (store.mask(answer or err or ""), "done" if answer else "error", store.now(), aside_id))
    finally:
        conn.close()


def mark_dialog(conn, sid, box):
    """훅이 오지 않는 선택 창(예: /remote-control 확인)을 「확인 필요」로 표시한다. 창을 닫으면 다음 보내기 때 화면을 보고 풀린다."""
    conn.execute("UPDATE sessions SET state='waiting', state_detail=?, state_at=? WHERE session_id=?",
                 (store.mask("터미널 선택 창: " + box)[:300], store.now(), sid))


BRAIN_ACTIONS = re.compile(
    r"/api/sessions/([\w\-]+)/(?:send|remote|quit|import|archive|pin|rename|interrupt|model|permission|rewind|answer|prompt|focus|switch|attach|close-branch)"
    r"|/api/sessions/new|/api/agents/[0-9a-f]{8}/\w+|/api/(?:config|settings|open|daily)|/api/notes/\d+|/api/inbox/\d+/delegate")


RESUME_PERMS = {"default", "acceptEdits", "plan", "auto", "manual", "dontAsk"}


def remove_claude_worktree(path):
    """claude --worktree 가 만든 워크트리(<저장소>/.claude/worktrees/<이름>)와 그 브랜치(worktree-<이름>)를 지운다.
    이어 연(--resume) 워크트리 세션은 종료 때 Claude Code 가 워크트리를 묻지 않아서, 세션 종료에서 「정리하기」를 고르면 보드가 지운다.
    다른 곳의 워크트리·다른 이름의 브랜치는 건드리지 않는다. 성공하면 True"""
    m = re.fullmatch(r"(/.+?)/\.claude/worktrees/([^/]+)/?", path or "")
    if not m or not os.path.isdir(path):
        return False
    main, name = m.groups()
    run = lambda *a: subprocess.run(["git", "-C"] + list(a), capture_output=True, text=True, timeout=30)
    branch = run(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    # -f 두 번: 커밋 안 한 변경이 있어도, Claude Code 가 걸어 둔 잠금(lock)이 남아 있어도 지운다(사용자가 고른 정리)
    if run(main, "worktree", "remove", "-f", "-f", path).returncode != 0:
        return False
    if branch == "worktree-" + name:
        run(main, "branch", "-D", branch)
    return True


def resume_flags(conn, sid):
    """이어 열 때(--resume) 그 세션이 쓰던 모델·effort·권한 모드를 넘기는 claude 인자(끝에 빈칸). 모르는 값은 뺀다.
    권한 확인 없이(bypassPermissions)였으면 그대로 — 그 모드는 켤 때 허락 옵션이 있어야 해서 함께 붙인다(--allow-…: 나중에
    Shift+Tab·보드 메뉴로 다른 모드로 돌아갈 수도 있게)."""
    r = conn.execute("SELECT model, effort, permission_mode FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not r:
        return ""
    out = []
    if re.fullmatch(r"claude-[a-z0-9.\-]+|" + "|".join(config.MODELS), r["model"] or ""):
        out.append("--model " + r["model"])
    if (r["effort"] or "") in config.EFFORTS:
        out.append("--effort " + r["effort"])
    if (r["permission_mode"] or "") in RESUME_PERMS:
        out.append("--permission-mode " + r["permission_mode"])
    elif r["permission_mode"] == "bypassPermissions":
        out.append(BYPASS_FLAGS)
    return "".join(x + " " for x in out)


BYPASS_FLAGS = "--allow-dangerously-skip-permissions --permission-mode bypassPermissions"


STALE_ASK = "터미널에 질문 창이 없어 보드에 남아 있던 질문을 지웠습니다 — 입력창을 쓸 수 있습니다"


def clear_stale_ask(conn, sid, min_age=4):
    """보드가 기억한 질문(AskUserQuestion)이 터미널에 없으면(평소 입력칸이 보임) 지운다. 지웠으면 True.

    질문 훅은 왔는데 터미널에는 창이 뜨지 않는 경우가 있다(응답 뒤 Claude Code 가 따로 돌린 작업이 같은 세션으로 부른 것 등).
    그러면 칸은 「질문에 답할 차례」로 입력창이 막히고, 답을 보내도 넣을 창이 없다. 훅 직후에는 창이 아직 그려지는 중일 수
    있어 min_age 초가 지난 질문만 본다."""
    row = conn.execute("SELECT state, state_at, ask, terminal FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not row or row["state"] != "waiting" or not row["ask"] or store.now() - (row["state_at"] or 0) < min_age:
        return False
    tg = focus.target_of(dict(row, session_id=sid))
    if not tg or focus.ask_on_screen(tg) is not False:
        return False
    conn.execute("UPDATE sessions SET ask=NULL, state='done', state_detail=NULL, state_at=? WHERE session_id=? AND ask=?",
                 (store.now(), sid, row["ask"]))
    return True


def restart_server():
    """보드 서버를 같은 프로세스 자리에서 새 코드로 다시 띄운다(업데이트 뒤). launchd 가 띄운 것이면 그대로 launchd 소속으로 남는다.
    열린 연결(터미널 웹소켓 등)은 끊기고 화면이 다시 붙는다."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


def live_remote(sid):
    """지금 연결된 원격(Remote Control) 세션 ID, 연결 안 됐으면 "", 실행 목록을 못 읽으면 None(모름)."""
    run = focus.running_sessions()
    if run is None or sid not in run:
        return None
    return run[sid].get("bridge") or ""


def brain_guard(conn, body, target_sid):
    """브레인(./argus send·remote·quit·archive·new)가 보낸 요청이면 막을 이유(문자열), 아니면 None.

    자동 정리(인박스 정리해줘 (자동)) 중에는 보내지 못한다 — 그때 읽는 슬랙·메일 본문의 지시로 다른 세션이 움직이지 않게.
    """
    src = body.get("from")
    if not src:
        return None
    if src in inboxrun.RUN_SIDS:
        return "인박스 정리 일꾼은 세션을 움직이거나 설정을 바꿀 수 없습니다"
    if src == target_sid:
        return "자기 자신에게는 보낼 수 없습니다"
    row = conn.execute("SELECT cwd FROM sessions WHERE session_id=?", (src,)).fetchone()
    if not row or not brain.is_brain(row["cwd"]):
        return None
    last = conn.execute("SELECT prompt FROM turns WHERE session_id=? AND prompt IS NOT NULL"
                        " ORDER BY COALESCE(prompt_at, response_at) DESC, id DESC LIMIT 1", (src,)).fetchone()
    if last and (last["prompt"] or "").strip().startswith(brain.AUTO_PROMPTS):
        return "자동 정리·자동 알림 차례에는 다른 세션을 움직일 수 없습니다(보내기·원격·종료·보관·새 세션 — 사용자가 직접 시킨 대화에서만)"
    return None


def list_events(conn, kinds=None, unseen=False, project=None, before=None, limit=200, since=None):
    """활동 목록(최근 것부터). 세션 이름·프로젝트·안 봤는지·그 턴이 뒤에서 몇 번째인지(보드에서 그 턴까지 불러오기용)."""
    where, params = ["1=1"], []
    if kinds:
        where.append("e.kind IN (%s)" % ",".join("?" * len(kinds)))
        params += kinds
    if unseen:
        where.append("e.at > COALESCE(s.last_seen_at, 0) AND e.kind IN ('response','waiting','question','error')")
    if project:
        where.append("COALESCE(s.project_root, s.cwd) = ?")
        params.append(project)
    if before:
        where.append("e.at < ?")
        params.append(before)
    if since:
        where.append("e.at >= ?")
        params.append(since)
    rows = conn.execute(
        "SELECT e.id, e.session_id, e.kind, e.text, e.turn_id, e.at, s.title, s.project_name, s.cwd,"
        " COALESCE(s.project_root, s.cwd) AS project_root, s.worktree, s.branch, s.state, s.archived,"
        # 에이전트 팀의 팀원: 제목이 없어 팀원 이름과 팀장 세션 제목으로 보인다
        " CASE WHEN s.branch_kind = 'team' THEN s.team_agent END AS team_agent,"
        " CASE WHEN s.branch_kind = 'team' THEN (SELECT l.title FROM sessions l WHERE l.session_id = s.continued_from) END AS lead_title,"
        " (e.at > COALESCE(s.last_seen_at, 0)) AS unseen,"
        " (SELECT COUNT(*) FROM turns t2, turns t WHERE t.id = e.turn_id AND t2.session_id = t.session_id AND"
        "   (COALESCE(t2.prompt_at, t2.response_at) > COALESCE(t.prompt_at, t.response_at)"
        "    OR (COALESCE(t2.prompt_at, t2.response_at) = COALESCE(t.prompt_at, t.response_at) AND t2.id >= t.id))) AS from_end,"
        # 문맥: 이 활동을 끌어낸 턴(pt — 응답·오류는 그 턴, 확인 필요·질문은 그때 진행 중이던 마지막 턴)과
        # 그때까지 내가 보낸 마지막 요청(rp, 앞 300자). 백그라운드 작업 알림(<task-notification>)으로 시작된 턴이면
        # 알림은 따로(notice — 요약 태그가 작업 ID·출력 경로 뒤라 길게) 싣고, 요청은 그 앞의 내 요청을 보여 준다
        " substr(rp.prompt, 1, 300) AS prompt, rp.prompt_at AS prompt_at,"
        " CASE WHEN pt.prompt LIKE '<task-notification>%' THEN substr(pt.prompt, 1, 2000) END AS notice"
        " FROM events e JOIN sessions s ON s.session_id = e.session_id"
        " LEFT JOIN turns pt ON e.kind IN ('response', 'waiting', 'question', 'error') AND pt.id = COALESCE(e.turn_id,"
        "  (SELECT t3.id FROM turns t3 WHERE t3.session_id = e.session_id AND t3.prompt IS NOT NULL AND t3.prompt_at <= e.at"
        "   ORDER BY t3.prompt_at DESC, t3.id DESC LIMIT 1))"
        " LEFT JOIN turns rp ON e.kind IN ('response', 'waiting', 'question', 'error') AND rp.id ="
        "  (SELECT t4.id FROM turns t4 WHERE t4.session_id = e.session_id AND t4.prompt IS NOT NULL"
        "   AND t4.prompt NOT LIKE '<task-notification>%' AND t4.prompt NOT LIKE '<agent-message%'"
        "   AND t4.prompt NOT LIKE '<teammate-message%' AND t4.prompt NOT LIKE '[자동 알림]%'"   # 에이전트·Argus 가 넣은 것 — 내 요청이 아님
        "   AND t4.prompt_at <= COALESCE(pt.prompt_at, e.at)"
        "   ORDER BY t4.prompt_at DESC, t4.id DESC LIMIT 1)"
        " WHERE " + " AND ".join(where) +
        " ORDER BY e.at DESC, e.id DESC LIMIT ?", params + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["brain"] = brain.is_brain(d.pop("cwd"))
        d["unseen"] = bool(d["unseen"]) and d["kind"] in ("response", "waiting", "question", "error")
        out.append(d)
    return out


TURN_COLUMNS = "id, session_id, prompt, prompt_at, response, response_at, status"


def turn_context(conn, turn_id, around=0):
    """턴 하나의 전문(중간 메시지 포함)과 같은 세션의 앞뒤 턴 around 개씩."""
    t = conn.execute("SELECT " + TURN_COLUMNS + ", extras FROM turns WHERE id=?", (turn_id,)).fetchone()
    if not t:
        return None
    s = conn.execute("SELECT session_id, title, project_name, COALESCE(project_root, cwd) AS root, cwd, branch, state"
                     " FROM sessions WHERE session_id=?", (t["session_id"],)).fetchone()
    turn = dict(t)
    try:
        extras = json.loads(turn.pop("extras") or "null") or []
    except ValueError:
        extras = []
    turn["middle"] = [x.get("text") for x in extras if isinstance(x, dict) and x.get("type") == "text" and x.get("text")]
    at = t["prompt_at"] or t["response_at"] or 0
    key = "COALESCE(prompt_at, response_at)"
    before = conn.execute("SELECT " + TURN_COLUMNS + " FROM turns WHERE session_id=? AND (" + key + " < ? OR (" + key +
                          " = ? AND id < ?)) ORDER BY " + key + " DESC, id DESC LIMIT ?",
                          (t["session_id"], at, at, turn_id, around)).fetchall() if around else []
    after = conn.execute("SELECT " + TURN_COLUMNS + " FROM turns WHERE session_id=? AND (" + key + " > ? OR (" + key +
                         " = ? AND id > ?)) ORDER BY " + key + ", id LIMIT ?",
                         (t["session_id"], at, at, turn_id, around)).fetchall() if around else []
    return {"session": dict(s) if s else {"session_id": t["session_id"]}, "turn": turn,
            "before": [dict(r) for r in reversed(before)], "after": [dict(r) for r in after]}


_title_seen = {}   # session_id → 마지막으로 이름을 확인한 기록 파일 수정 시각


def follow_rename(conn, s):
    """/rename 은 훅이 없어 다음 응답 때까지 보드 제목이 그대로다. 기록 파일이 바뀌었으면 끝부분만 봐서 따라간다."""
    path = s.get("transcript_path")
    if not path or s.get("title_source") == "board":
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    if _title_seen.get(s["session_id"]) == mtime:
        return
    _title_seen[s["session_id"]] = mtime
    title = store.tail_custom_title(path)
    if title and store.mask(title) != s.get("title"):
        s["title"], s["title_source"] = store.mask(title), "custom"
        conn.execute("UPDATE sessions SET title=?, title_source='custom' WHERE session_id=?", (s["title"], s["session_id"]))


LAUNCHED = {}   # 보드에서 띄운 Orca 터미널 핸들 → 시각(서버가 켜져 있는 동안)
IMPORTING = {}  # 보드 터미널로 가져오는 중인 세션 → 시작 시각(종료 → 이어 열기 사이에도 칸을 보이게)
RESTARTING = set()  # 그중 「세션 다시 시작」인 것 — 칸 가운데 안내 문구를 「다시 시작하는 중」으로
# 보드가 띄운 브레인 탭 {"handle", "terminal", "at"} — 세션은 훅(SessionStart)으로 잡히는데, 처음 실행이면 그 전에
# 폴더 신뢰·권한 모드 같은 확인 창이 터미널에 뜬다. 그동안 브레인 패널이 그 탭 화면을 읽어 보드에서 고르게 한다
BRAIN_LAUNCH = {}


def brain_launch_target():
    """보드가 띄운 브레인 탭(15분 안) Target 또는 None."""
    h, name = BRAIN_LAUNCH.get("handle"), BRAIN_LAUNCH.get("terminal")
    if not h or store.now() - BRAIN_LAUNCH.get("at", 0) > 900:
        return None
    t = terminals.registry().get(name)
    return terminals.Target(t, h) if t and t.valid(h) else None
REAPED = set()  # 닫은 탭(다시 보지 않게)


def reap_tabs():
    """보드가 띄운 탭은 `claude … && exit` 라 Claude 가 정상 종료하면 셸도 끝난다. Orca 는 그 탭을 빈 채로 남기므로 닫는다.

    살펴보는 탭: 이번에 서버가 띄운 것 + 10분 안에 끝난 세션의 탭. 실시간 목록(한 번 조회)에 없는 것만 하나씩 확인해서,
    셸이 끝났으면(connected=false) 닫는다 — 셸이 살아 있는(오류로 끝나 셸이 남은·사용자가 연) 탭은 건드리지 않는다."""
    t = terminals.default()
    if not t:
        return
    cands = {h for h in LAUNCHED if t.valid(h)}   # 그 터미널의 탭만(보드 터미널 %N 은 Orca 탭이 아니다 — 지우면 칸이 숨었다)
    conn = store.connect()
    try:
        for (term,) in conn.execute("SELECT terminal FROM sessions WHERE state='ended' AND ended_at > ? AND terminal IS NOT NULL",
                                    (store.now() - 600,)):
            try:
                h = (json.loads(term) or {}).get("orca_handle")
            except ValueError:
                h = None
            if h:
                cands.add(h)
    finally:
        conn.close()
    cands -= REAPED
    if not cands:
        return
    live = {x["handle"] for x in t.terminals()}
    for h in cands - live:
        ex = t.exited(h)
        if ex:
            t.close(h)
        if ex is not False:   # 닫았거나 탭이 없다
            REAPED.add(h)
            LAUNCHED.pop(h, None)


# ---------------------------------------------------------------- 커밋 안 한 변경 수(칸 머리의 변경 보기 버튼)
# 보드에 보이는 세션 폴더마다 git status·diff --shortstat 을 백그라운드에서 20초마다 — 같은 폴더(같은 저장소의 여러 세션)는
# 한 번만. 보드 목록(list_state)은 기억한 값만 읽어 느려지지 않는다. 2분 넘게 안 보인 폴더는 더 확인하지 않는다
DIRTY = {}          # 폴더 → {"files", "new", "add", "del", "at"} (git 저장소가 아니면 None)
DIRTY_WANT = {}     # 폴더 → 마지막으로 보드가 보여 준 시각
DIRTY_WAKE = threading.Event()
DIRTY_EVERY = 20


def dirty_of(d):
    if not d:
        return None
    if d not in DIRTY_WANT:
        DIRTY_WAKE.set()   # 처음 보는 폴더는 바로 확인
    DIRTY_WANT[d] = store.now()
    return DIRTY.get(d)


_LINES_SEEN = {}   # 새 파일 경로 → (크기, 수정 시각, 줄 수) — 20초마다 다시 읽지 않게


def _untracked_stats(d):
    """새 파일(추적 안 됨) 수와 줄 수 — 변경 보기(_untracked_diff)와 같은 기준: 파일 하나하나, 최대 개수까지,
    너무 큰 파일·바이너리는 줄 수 0."""
    names = [n for n in _git(d, "ls-files", "--others", "--exclude-standard", "-z", timeout=10).split("\0") if n]
    root = os.path.realpath(d)
    lines = 0
    for n in names[:UNTRACKED_MAX[0]]:
        full = os.path.realpath(os.path.join(d, n))
        if not full.startswith(root + os.sep):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        seen = _LINES_SEEN.get(full)
        if seen and seen[:2] == (st.st_size, st.st_mtime):
            lines += seen[2]
            continue
        k = 0
        if st.st_size <= UNTRACKED_MAX[1]:
            try:
                with open(full, "rb") as f:
                    raw = f.read(UNTRACKED_MAX[1] + 1)
            except OSError:
                raw = b"\0"
            if b"\0" not in raw[:8192] and raw:
                k = raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)
        _LINES_SEEN[full] = (st.st_size, st.st_mtime, k)
        lines += k
    if len(_LINES_SEEN) > 5000:
        _LINES_SEEN.clear()
    return len(names), lines


def _dirty_check(d):
    if not os.path.isdir(d):
        return None
    try:
        r = subprocess.run(["git", "-C", d, "status", "--porcelain=v1", "--untracked-files=no"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return DIRTY.get(d)
    if r.returncode != 0:
        return None
    # 변경 보기와 같은 수로: 바뀐 추적 파일 + 새 파일 하나하나(git status 는 새 폴더를 한 줄로 묶어 적게 보였다)
    changed = len([ln for ln in r.stdout.splitlines() if ln.strip()])
    new, new_lines = _untracked_stats(d)
    add = dele = 0
    if changed:
        st = _git(d, "diff", "HEAD", "--shortstat", timeout=10)
        m1, m2 = re.search(r"(\d+) insertion", st), re.search(r"(\d+) deletion", st)
        add, dele = int(m1.group(1)) if m1 else 0, int(m2.group(1)) if m2 else 0
    return {"files": changed + new, "new": new, "add": add + new_lines, "del": dele, "at": store.now()}


def _dirty_loop():
    while True:
        DIRTY_WAKE.wait(DIRTY_EVERY)
        DIRTY_WAKE.clear()
        cut = store.now() - 120
        for d in [k for k, t in list(DIRTY_WANT.items()) if t >= cut]:
            try:
                DIRTY[d] = _dirty_check(d)
            except Exception:  # noqa: BLE001 - 한 폴더의 오류가 확인 전체를 멈추면 안 된다
                pass
        for d in [k for k, t in list(DIRTY_WANT.items()) if t < cut]:
            DIRTY_WANT.pop(d, None)
            DIRTY.pop(d, None)


# ------------------------------------------------------------------ Codex 세션 조작
# Claude Code 처럼 화면을 읽어 고르지 않는다. 보드 터미널(등 기록된 탭)에서 codex 가 돌면 그 입력창에 붙여 넣고 Enter,
# 아니면(데스크톱 앱 스레드) `codex queue` — Codex 가 그 스레드에 메시지를 넣어 준다
# ------------------------------------------------------------------ Claude Code / 명령을 보드에서
# 보내고 화면을 본다: 번호 선택 창이면 보드 창에 목록(고르면 focus.answer_prompt), 읽기만 하는 창(아래 줄이 「Esc to cancel·close」
# 뿐)이면 그 내용을 보여 주고 닫는다, 바로 끝나는 명령이면 그 결과 줄. 검색·슬라이더·토글 같은 창은 터미널로 넘긴다
_CL_READONLY_FOOT = re.compile(r"^(?:(?:↑/↓|↑↓)\s*to (?:select|scroll|navigate)\s*·\s*)?(?:Enter to (?:view|open)\s*·\s*)?Esc to (?:cancel|close|go back|exit)$", re.I)
_CL_CHROME = re.compile(r"^\s*(?:[─━▔╌]{4,}|\[.*\|.*\]\s*│|Context [░█]|⏵⏵|⏸|❯|\? for shortcuts|.*\(shift\+tab to cycle\))|^\s{40,}\S")


def _claude_picker(pv):
    head = [h for h in pv["head"] if not h.startswith(("❯", "⎿", "│", "╰", "╭"))]
    return {"title": head[0] if head else "", "sub": " ".join(head[1:3]),
            "rows": [{"n": o["n"], "label": o["label"].replace("✔", "").strip(), "raw": o["label"], "desc": o["hint"],
                      "current": "✔" in o["label"], "cursor": o["on"]} for o in pv["options"]], "keys": [], "claude": True}


def _local_command_output(path, name, since):
    """기록 파일 끝에서 since 뒤에 실행된 /name 의 결과(<local-command-stdout>) — 없으면 ""."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
    except OSError:
        return ""
    want, out = ("<command-name>%s</command-name>" % name).encode(), ""
    hit = False
    for raw in data.splitlines():
        if want in raw:
            try:
                hit = (store.parse_ts(json.loads(raw).get("timestamp")) or 0) >= since - 2
            except ValueError:
                hit = False
            continue
        if hit and b"<local-command-std" in raw:   # 시스템 줄(subtype local_command) 또는 사용자 메시지 줄 — 명령마다 다르다
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            c = d.get("content") or (d.get("message") or {}).get("content") or ""
            if not isinstance(c, str):
                continue
            m = re.search(r"<local-command-std(?:out|err)>(.*?)</local-command-std(?:out|err)>", c, re.S)
            if m and m.group(1).strip():
                out = re.sub(r"\x1b\[[0-9;]*m", "", m.group(1)).strip()
            hit = False
    return out


def _claude_after(tg, echo, before=None, transcript=None, since=0):
    """명령을 보낸(골랐다) 뒤 화면 — {"picker"} · {"output"} · {"panel"}(읽고 닫음) · {"terminal": True}(복잡한 창)."""
    lines, last = [], None
    for _ in range(12):   # 화면이 멈출 때까지(사용량 집계처럼 늦게 채워지는 창)
        time.sleep(0.5)
        lines = [ln.replace("\xa0", " ").rstrip() for ln in focus._screen(tg)]
        if lines == last and not any("Scanning" in ln or "Loading" in ln for ln in lines[-12:]):
            break
        last = lines
    pv = focus.prompt_view(tg)
    if pv and pv.get("options") and not any("Search" in h or "⌕" in h for h in pv["head"]):
        return {"picker": _claude_picker(pv)}
    if focus._input_line_of(lines) is not None:
        # 바로 끝난 명령 — 보내기 전 화면에 없던, 입력칸 위의 새 줄(「❯ /명령」 메아리·입력칸·상태 줄은 뺀다)
        # 입력칸(❯) 바로 위 경계까지 — 앞서 열렸던 창이 남긴 가로줄에 속지 않게 입력칸 줄을 기준으로
        prompt_i = next((i for i in range(len(lines) - 1, 0, -1) if lines[i].lstrip().startswith("❯")
                         and lines[i - 1].lstrip().startswith("──")), None)
        end = prompt_i - 1 if prompt_i else len(lines)
        new = codex._new_lines(before or [], lines[:end]) if before is not None else lines[:end]
        out = [ln.rstrip() for ln in new if ln.strip() and not _CL_CHROME.match(ln)]
        text = "\n".join(out[-30:]).strip()
        if transcript and echo:
            # 화면은 다시 그려지며 비기도 한다(/focus 등) — 기록 파일의 그 명령 결과(로컬 명령 출력)가 더 정확하다
            text = _local_command_output(transcript, echo.split()[0], since) or text
        return {"output": text}
    foot = next((i for i in range(len(lines) - 1, -1, -1) if "Esc to" in lines[i] or "esc to" in lines[i]), None)
    if foot is None:
        return {"terminal": True}
    if not _CL_READONLY_FOOT.match(lines[foot].strip()):
        return {"terminal": True}   # 검색·슬라이더·토글 — 터미널에서
    top = max([i for i in range(foot) if lines[i].lstrip().startswith(("▔", "──", "╭"))] or [-1])
    text = "\n".join(ln for ln in lines[top + 1:foot] if ln.strip() and not ln.lstrip().startswith("▔")).strip()
    _claude_close(tg)   # 읽기만 하는 창 — 보드에 옮겼으니 닫는다
    return {"panel": text}


def _claude_close(tg):
    """입력칸이 돌아올 때까지 Esc(창에 따라 처음 Esc 는 검색만 지우거나 탭을 벗어난다) — 최대 세 번."""
    for _ in range(3):
        if focus._input_line_of(focus._screen(tg)) is not None and not focus.prompt_view(tg):
            return True
        focus._raw(tg, focus.ESC)
        time.sleep(0.5)
    return focus._input_line_of(focus._screen(tg)) is not None


def recap_text(lines, i, first, col, min_indent):
    """화면의 recap: 첫 줄(i) 글 + 이어지는 줄들(min_indent 칸 이상, 첫 줄 글이 시작하는 칸(col) 이하로 들여 쓴 줄).
    훨씬 깊이 들여 쓴 줄은 이어지는 줄이 아니다 — Claude Code 가 오른쪽 끝에 찍는 알림(「✔ Update installed · Restart
    to update」 등)이 recap 바로 아래 줄에 붙어 나온다."""
    parts = [first]
    for ln in lines[i + 1:]:
        indent = len(ln) - len(ln.lstrip())
        if not ln.strip() or not min_indent <= indent <= col + 1 or ln.lstrip().startswith(("❯", "─")):
            break
        parts.append(ln.strip())
    return " ".join(p for p in parts if p)


def run_recap(conn, sess):
    """/recap — Claude Code·Codex 모두 요약을 터미널에만 찍고(대화 턴·훅이 아님) 보드에는 남지 않았다.
    보내고 요약 줄이 나올 때까지 기다렸다가 칸의 recap 자리에 넣는다. (HTTP 코드, 본문)."""
    cx = sess.get("agent") == "codex"
    if cx:
        tg = codex_tab(conn, sess)
        if not tg:
            return 409, {"error": "Codex 가 도는 터미널을 찾지 못했습니다"}
    else:
        tg, err = terminal_target(conn, sess)
        if not tg:
            return 409, {"error": err}
    if sess["state"] in ("working", "waiting"):
        return 409, {"error": "작업 중에는 요약하지 않습니다 — 끝난 뒤에"}
    scr = lambda: [ln.replace("\xa0", " ").rstrip() for ln in tg.terminal.screen(tg.handle)]
    before = scr()
    if cx:
        ok, err = codex.paste(tg, "/recap")
    else:
        ok, _, err = focus.send_prompt(tg, "/recap")
    if not ok:
        return 502, {"error": "전송 실패: " + err}
    text, end = "", time.time() + 45
    while time.time() < end and not text:
        time.sleep(1.0)
        lines = scr()
        if cx:   # 「↳ Recap: …」 + 들여 쓴 이어지는 줄
            idx = [i for i, ln in enumerate(lines) if "Recap:" in ln and ln not in before]
            if idx:
                i = idx[-1]
                head = lines[i].split("Recap:", 1)[1]
                text = recap_text(lines, i, head.strip(), len(lines[i]) - len(head.lstrip()), 3)
        else:    # 「❯ /recap」 다음 「⎿ …」 + 들여 쓴 이어지는 줄
            echo = [i for i, ln in enumerate(lines) if ln.strip() == "❯ /recap"]
            if echo and len(lines) > echo[-1] + 1 and lines[echo[-1] + 1].strip().startswith("⎿"):
                first = lines[echo[-1] + 1]
                body = first.split("⎿", 1)[1]
                text = recap_text(lines, echo[-1] + 1, body.strip(), len(first) - len(body.lstrip()), 5)
    if not text:
        return 504, {"error": "요약이 나오지 않았습니다 — 터미널을 확인해 주세요"}
    t = store.now()
    conn.execute("UPDATE sessions SET recap=?, recap_at=? WHERE session_id=?", (store.mask(text, private=False), t, sess["session_id"]))
    return 200, {"ok": True, "recap": text, "message": "요약했습니다 · 칸 위 recap 에 보입니다"}


_CL_BASE = {}   # 탭 → 명령을 보내기 직전 화면(고른 뒤 새로 찍힌 결과를 가려낼 기준)
_CL_AT = {}     # 탭 → 명령을 보낸 시각(기록 파일에서 그 뒤 결과만)


def _cl_transcript(conn, sess):
    r = conn.execute("SELECT transcript_path FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()
    return r[0] if r else None


def claude_cmd(conn, sess, body):
    tg, err = terminal_target(conn, sess)
    if not tg:
        return 409, {"error": err}
    if body.get("cancel"):
        _claude_close(tg)
        return 200, {"ok": True}
    if body.get("pick") is not None:
        ok, msg = focus.answer_prompt(tg, int(body["pick"]), body.get("label") or "")
        if not ok:
            return 502, {"error": msg}
        base = _CL_BASE.pop(tg.handle, None)
        return 200, _claude_after(tg, body.get("echo") or "", base, _cl_transcript(conn, sess), _CL_AT.get(tg.handle, 0))
    text = CONTROL_CHARS.sub("", body.get("text") or "").strip()
    if not text.startswith("/"):
        return 400, {"error": "/ 명령만"}
    if sess["state"] in ("working", "waiting"):
        return 409, {"error": "작업 중에는 보드에서 실행하지 않습니다 — 끝난 뒤에(또는 터미널에서)"}
    if focus.dialog_text(tg):
        return 409, {"error": "터미널에 선택 창이 떠 있습니다 — 먼저 닫아 주세요"}
    before = [ln.replace("\xa0", " ").rstrip() for ln in focus._screen(tg)]
    _CL_BASE[tg.handle] = before
    _CL_AT[tg.handle] = store.now()
    ok, _, err = focus.send_prompt(tg, text)
    if not ok:
        return 502, {"error": "전송 실패: " + err}
    return 200, _claude_after(tg, text, before, _cl_transcript(conn, sess), _CL_AT[tg.handle])


_USAGE_LINE = re.compile(r"^Current week \((.+?)\):\s*([\d.]+)% used(?:\s*·\s*resets\s+(.+?))?(?:\s*\([^)]*\))?\s*$")
_USAGE_RESET = re.compile(r"^(?:([A-Z][a-z]{2})\s+(\d{1,2}))?(?:\s*at)?\s*(?:(\d{1,2})(?::(\d{2}))?\s*(am|pm))?$", re.I)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
USAGE_DIR = os.path.join(store.DATA_DIR, "usage-check")   # claude -p 를 돌리는 곳(프로젝트 폴더가 아니게 — 늘 같은 곳)
USAGE_EVERY = 30 * 60
_usage_lock = threading.Lock()
_usage_tried = [0.0]


def _usage_reset(text):
    """「Nov 16 at 5:59am」·「3:59pm」 → epoch 초(그 Mac 의 시간대로 찍힌다). 못 읽으면 None."""
    m = _USAGE_RESET.match((text or "").strip())
    if not m or not (m.group(1) or m.group(3)):
        return None
    now = datetime.datetime.now()
    month = _MONTHS.index(m.group(1).lower()) + 1 if m.group(1) and m.group(1).lower() in _MONTHS else now.month
    day = int(m.group(2)) if m.group(2) else now.day
    hour = int(m.group(3) or 0) % 12 + (12 if (m.group(5) or "").lower() == "pm" else 0)
    try:
        dt = datetime.datetime(now.year, month, day, hour, int(m.group(4) or 0))
    except ValueError:
        return None
    if dt < now - datetime.timedelta(days=2):   # 해가 바뀌는 때(12월 → 1월)
        dt = dt.replace(year=now.year + 1)
    return dt.timestamp()


def fetch_scoped_usage():
    """모델별 주간 한도(Fable 등)를 새로 받는다 — `claude -p "/usage"`. 모델을 부르지 않는 로컬 명령이라 토큰을 쓰지 않고(2~3초),
    세션 기록도 남기지 않는다(--no-session-persistence). 보드 훅·MCP·사용자 설정(다른 훅)은 끈다. (성공, 오류 문구)"""
    if not _usage_lock.acquire(blocking=False):
        return False, "이미 받는 중입니다"
    try:
        _usage_tried[0] = time.time()
        os.makedirs(USAGE_DIR, exist_ok=True)
        try:
            r = subprocess.run([daily._claude_bin(), "-p", "/usage", "--no-session-persistence", "--strict-mcp-config",
                                "--setting-sources", "local", "--output-format", "json"],
                               capture_output=True, text=True, timeout=60, cwd=USAGE_DIR, env=daily._clean_env())
            d = json.loads(r.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            return False, "claude -p 를 실행하지 못했습니다: %s" % e
        if d.get("is_error") or not d.get("result"):
            return False, "사용량을 받지 못했습니다: %s" % str(d.get("result") or r.stderr or "")[:200]
        items, at = [], time.time()
        for ln in d["result"].splitlines():
            m = _USAGE_LINE.match(ln.strip())
            if m and m.group(1).lower() != "all models":
                items.append({"label": m.group(1), "pct": float(m.group(2)), "resets_at": _usage_reset(m.group(3)), "at": at})
        store.SCOPED_FETCHED.update(at=at, items=items)
        return True, ""
    finally:
        _usage_lock.release()


def auto_refresh_usage():
    """보드가 상태를 물을 때 — 모델별 주간 한도가 30분 넘게 묵었으면 뒤에서 새로 받는다(30분에 한 번만 시도)."""
    if time.time() - _usage_tried[0] < USAGE_EVERY:
        return
    scoped = store.scoped_limits()
    if scoped and all(time.time() - (x.get("at") or 0) < USAGE_EVERY for x in scoped):
        return
    _usage_tried[0] = time.time()
    threading.Thread(target=fetch_scoped_usage, daemon=True).start()


def refresh_usage(conn):
    """(HTTP 코드, 본문) — 상태 막대의 Fable 을 눌렀을 때·./argus usage refresh."""
    ok, err = fetch_scoped_usage()
    return (200, {"ok": True, "usage": store.read_usage()}) if ok else (502, {"error": err})


def codex_tab(conn, sess):
    """그 Codex 세션의 codex 가 돌고 있는 터미널 Target 또는 None. 찾으면 이 요청이 끝날 때까지 잡아 둔다."""
    conn.commit()
    tg = codex.tab_of(sess)
    return tg if tg and _hold_terminal(tg) else None


def watch_from_brain(conn, body, target_sid, text):
    """브레인(./argus send)이 보낸 글이면 그 세션의 답을 지켜본다 — 답하면 브레인에게 [자동 알림](delegate.relay_tick)."""
    src = body.get("from")
    if not src or src == target_sid or text.startswith("/"):
        return
    row = conn.execute("SELECT cwd FROM sessions WHERE session_id=?", (src,)).fetchone()
    if row and brain.is_brain(row["cwd"]):
        conn.execute("UPDATE brain_watch SET status='replaced' WHERE target_sid=? AND status='open'", (target_sid,))
        conn.execute("INSERT INTO brain_watch (brain_sid, target_sid, sent_at, prompt) VALUES (?,?,?,?)",
                     (src, target_sid, store.now(), store.mask(text)[:2000]))
        conn.commit()


def codex_send(conn, sess, body):
    text = CONTROL_CHARS.sub("", (body.get("text") or "").replace("\r\n", "\n")).strip()
    images = body.get("images") or []
    if not text and not images:
        return 400, {"error": "보낼 내용이 없습니다"}
    if len(text) > 20000:
        return 400, {"error": "메시지가 너무 깁니다(2만 자 이하)"}
    if sess["state"] == "waiting":
        return 409, {"error": "확인 필요 상태입니다. Codex 에서 승인·답변을 먼저 해 주세요"}
    try:
        paths = save_images(images)
    except ValueError as e:
        return 400, {"error": str(e)}
    tg = codex_tab(conn, sess)
    if tg:
        ok, err = codex.paste(tg, text, paths)
        if not ok:
            return 502, {"error": "전송 실패: " + err}
        watch_from_brain(conn, body, sess["session_id"], text)
        msg = "보냈습니다" if sess["state"] != "working" else "보냈습니다 · 작업 중이라 Codex 가 이어서 처리합니다"
        return 200, {"ok": True, "message": msg, "stages": [], "images": len(paths)}
    desktop = codex.desktop_session(sess)
    if not desktop and not codex.is_running(sess):
        # 터미널 CLI 세션인데 codex 가 꺼져 있다 — 보드가 「새 탭에서 이어 열기」를 묻는다
        return 409, {"error": "Codex 가 실행 중이 아닙니다", "resume": True}
    # 데스크톱 앱의 대화, 또는 어느 터미널인지 모르는(훅 없이 띄운) CLI — Codex 가 그 스레드에 넣어 준다
    if paths:
        text += "\n\n" + "\n".join("첨부 이미지: " + p for p in paths)
    ok, err = codex.queue(sess["session_id"], text)
    if not ok:
        return 502, {"error": "전송 실패: " + err}
    watch_from_brain(conn, body, sess["session_id"], text)
    return 200, {"ok": True, "message": "Codex 앱의 그 대화에 보냈습니다" if desktop else "Codex 에 보냈습니다",
                 "stages": [], "images": len(paths)}


def codex_model(conn, sess, model, effort):
    """Codex 의 모델·추론 단계(이 세션만) — 보드 터미널 등 조작할 수 있는 탭에서 /model 창으로."""
    names = {m["name"] for m in codex.models()}
    if model and model not in names:
        return 400, {"error": "모르는 모델입니다"}
    tg = codex_tab(conn, sess)
    if not tg:
        return 409, {"error": "Codex 앱의 대화는 앱에서 바꿔 주세요" if codex.desktop_session(sess)
                     else "Codex 가 도는 터미널을 찾지 못했습니다"}
    ok, msg = codex.set_model_effort(tg, model, effort)
    if not ok:
        return 502, {"error": msg}
    slug, got_effort = codex.changed_to(tg)
    if not slug:
        got_model, got_effort = codex.footer(tg)
        slug = codex.slug_of(got_model) if got_model else None
    if slug:
        conn.execute("UPDATE sessions SET model=? WHERE session_id=?", (slug, sess["session_id"]))
    if got_effort:
        conn.execute("UPDATE sessions SET effort=? WHERE session_id=?", (got_effort, sess["session_id"]))
    return 200, {"ok": True, "message": msg}


def codex_new_chat(conn, sess, cmd):
    """Codex 의 /clear·/new — 같은 탭에서 새 대화를 시작한다. Codex 는 이때 훅도 기록도 남기지 않아(첫 요청 전까지) 보드가
    직접: 보내고 새 세션 ID 를 읽어 빈 칸을 만들고, 이전 대화는 Claude Code 의 /clear 처럼 「종료(clear)」로 둔다."""
    tg = codex_tab(conn, sess)
    if not tg:
        return 409, {"error": "Codex 가 도는 터미널을 찾지 못했습니다"}
    if sess["state"] in ("working", "waiting") or codex.busy(tg):
        return 409, {"error": "작업 중에는 새 대화를 시작하지 않습니다 — 끝난 뒤에"}
    ok, err = codex.paste(tg, "/" + cmd)
    if not ok:
        return 502, {"error": "전송 실패: " + err}
    time.sleep(1.0)
    new = codex.session_id_of(tg, timeout=20)
    if not new or new == sess["session_id"]:
        return 502, {"error": "새 대화의 세션을 읽지 못했습니다 — 터미널을 확인해 주세요"}
    t = store.now()
    g = store.git_info(sess["cwd"] or "")
    with conn:
        conn.execute("INSERT OR IGNORE INTO sessions (session_id, agent, cwd, project_root, project_name, worktree, branch,"
                     " state, state_at, started_at, last_activity, last_seen_at, source, terminal, model, effort)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (new, "codex", sess["cwd"], g["project_root"], g["project_name"], g["worktree"], g["branch"], "idle", t, t, t, t,
                      "codex", json.dumps(tg.terminal.remember({}, tg.handle)), sess.get("model"), sess.get("effort")))
        conn.execute("UPDATE sessions SET terminal=?, state='ended', state_detail=?, state_at=?, ended_at=? WHERE session_id=?",
                     (json.dumps({"detached": True}), cmd, t, t, sess["session_id"]))
        store.add_event(conn, sess["session_id"], "end", cmd)
        store.add_event(conn, new, "start", cmd)
    LAUNCHED[tg.handle] = t   # 요청이 없어도 빈 칸으로 보이게
    return 200, {"ok": True, "session_id": new, "message": "새 대화를 시작했습니다 · 이전 대화는 종료됨(%s)" % cmd}


def codex_interrupt(conn, sess):
    if sess["state"] != "working":
        return 409, {"error": "작업 중이 아닙니다"}
    tg = codex_tab(conn, sess)
    if not tg:
        return 409, {"error": "Codex 앱의 대화는 앱에서 멈춰 주세요" if codex.desktop_session(sess)
                     else "Codex 가 도는 터미널을 찾지 못했습니다"}
    if not tg.terminal.write(tg.handle, "\x1b"):   # Codex 의 중단 키(Esc)
        return 502, {"error": "중단 키를 보내지 못했습니다"}
    stopped = False
    for _ in range(10):
        time.sleep(0.4)
        if not codex.busy(tg):
            stopped = True
            break
    if stopped:
        t = store.now()
        conn.execute("UPDATE turns SET status='interrupted', response=COALESCE(response, '(중단됨)'), response_at=COALESCE(response_at, ?)"
                     " WHERE session_id=? AND response_at IS NULL AND status='running'", (t, sess["session_id"]))
        conn.execute("UPDATE sessions SET state='done', state_detail='중단됨', state_at=? WHERE session_id=?", (t, sess["session_id"]))
        store.add_event(conn, sess["session_id"], "error", "사용자가 중단함")
    return 200, {"ok": True, "stopped": stopped,
                 "message": "중단했습니다" if stopped else "중단 키를 보냈습니다 — 지금 단계가 정리되는 대로 멈춥니다"}


def codex_launch(conn, cwd, model=None, effort=None, resume=None, prompt=None):
    """보드 터미널에 codex 를 띄운다(resume: 그 스레드를 이어 열기). (Target, 오류)."""
    b = codex.binary()
    if not b:
        return None, "codex 명령을 찾지 못했습니다 — Codex CLI 를 설치해 주세요(npm i -g @openai/codex 등)"
    t = terminals.registry().get("web")
    if not t or not t.available():
        return None, "보드 터미널(tmux)을 쓸 수 없습니다"
    cwd = os.path.realpath(os.path.expanduser(cwd or ""))
    if not os.path.isdir(cwd):
        return None, "폴더가 없습니다: " + cwd
    cmd = "cd %s && %s" % (shlex.quote(cwd), shlex.quote(b))
    if resume:
        cmd += " resume " + shlex.quote(resume)
    if model:
        if not re.fullmatch(r"[\w.\-\[\]]{1,64}", model):
            return None, "모르는 모델입니다"
        cmd += " -m " + shlex.quote(model)
    if effort:
        if effort not in codex.EFFORTS:
            return None, "모르는 추론 단계입니다"
        cmd += " -c " + shlex.quote('model_reasoning_effort="%s"' % effort)
    if prompt:   # 첫 요청 — Codex 는 첫 요청이 와야 대화 기록(스레드)을 만든다. 탭은 셸 자동완성이 되니 공백으로
        cmd += " -- " + shlex.quote(prompt.replace("\t", "    "))
    cmd += " && exit"   # 정상 종료(/quit)하면 셸도 끝낸다 — 빈 탭은 서버가 닫는다
    handle, err = t.create(cwd, os.path.basename(cwd) or cwd, cmd)
    if not handle:
        return None, err
    tg = terminals.Target(t, handle)
    LAUNCHED[handle] = store.now()
    codex.LAUNCHES[handle] = {"cwd": cwd, "at": store.now(), "resume": resume, "terminal": t.remember({}, handle)}
    if not resume and not prompt:
        # 첫 요청 없이 띄웠다 — Codex 는 첫 요청 전엔 기록도 훅도 없어 칸이 생기지 않는다. 입력창이 뜨면 세션 ID 를 읽어
        # 빈 칸을 바로 만든다(보드 칸 입력창에서 첫 요청을 보내면 같은 ID 의 대화가 된다)
        threading.Thread(target=_codex_claim, args=(tg, cwd), daemon=True).start()
    return tg, None


def _codex_claim(tg, cwd):
    sid = codex.session_id_of(tg)
    if not sid:
        return
    conn = store.connect()
    try:
        with conn:
            g = store.git_info(cwd)
            t = store.now()
            conn.execute("INSERT OR IGNORE INTO sessions (session_id, agent, cwd, project_root, project_name, worktree, branch,"
                         " state, state_at, started_at, last_activity, last_seen_at, source, terminal)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, "codex", cwd, g["project_root"], g["project_name"], g["worktree"], g["branch"], "idle", t, t, t, t,
                          "codex", json.dumps(tg.terminal.remember({}, tg.handle))))
        codex.LAUNCHES.pop(tg.handle, None)
    finally:
        conn.close()


def session_family(conn, sid):
    """원본·갈래 묶음(칸 하나)의 세션 id 들(sid 포함) — continued_from 을 위아래로."""
    seen, todo = set(), [sid]
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        r = conn.execute("SELECT continued_from FROM sessions WHERE session_id=?", (x,)).fetchone()
        if r and r[0]:
            todo.append(r[0])
        todo += [c[0] for c in conn.execute("SELECT session_id FROM sessions WHERE continued_from=?", (x,))]
    return seen


def delete_sessions(conn, sid):
    """끝난 세션(과 그 칸의 갈래들)을 보드 기록에서 지운다 — 칸·대화·넥스트 액션·활동. Claude Code·Codex 의 기록은 그대로.
    (HTTP 코드, 본문). 아직 도는 것이 있으면 지우지 않는다."""
    ids = session_family(conn, sid)
    rows = [dict(r) for r in conn.execute("SELECT session_id, state, agent, terminal, cwd, last_activity, title FROM sessions"
                                          " WHERE session_id IN (%s)" % ",".join("?" * len(ids)), list(ids))]
    running = focus.running_sessions() or {}
    for r in rows:
        alive = codex.tab_of(r) if r["agent"] == "codex" else r["session_id"] in running
        if r["state"] in ("working", "waiting") or alive:
            return 409, {"error": "아직 열려 있는 세션입니다(「%s」) — 종료한 뒤 지워 주세요" % (r["title"] or r["session_id"][:8])}
    marks, args = ",".join("?" * len(ids)), list(ids)
    for table in ("turns", "notes", "events", "asides"):
        conn.execute("DELETE FROM %s WHERE session_id IN (%s)" % (table, marks), args)
    conn.execute("DELETE FROM sessions WHERE session_id IN (%s)" % marks, args)
    gone = [r["session_id"] for r in rows if r["agent"] == "codex"]
    if gone:   # Codex 기록 동기화가 다시 가져오지 않게 기억해 둔다(그 대화를 다시 이어 쓰면 새로 들어온다)
        old = store.get_setting(conn, "codex_deleted", []) or []
        store.set_setting(conn, "codex_deleted", (old + gone)[-1000:])
        at = store.get_setting(conn, "codex_deleted_at", {}) or {}
        at.update({g: store.now() for g in gone})
        store.set_setting(conn, "codex_deleted_at", dict(sorted(at.items(), key=lambda kv: kv[1])[-1000:]))
    return 200, {"ok": True, "message": "보드에서 지웠습니다(세션 %d개 · %s 기록은 그대로)" % (
        len(rows), "Codex" if gone and len(gone) == len(rows) else "Claude Code·Codex")}


def codex_family(conn, sid):
    """원본·갈래(/fork) 묶음의 세션 id 들(sid 포함)."""
    seen, todo = set(), [sid]
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        r = conn.execute("SELECT continued_from FROM sessions WHERE session_id=?", (x,)).fetchone()
        if r and r[0]:
            todo.append(r[0])
        todo += [c[0] for c in conn.execute("SELECT session_id FROM sessions WHERE continued_from=? AND agent='codex'", (x,))]
    return seen


def codex_resume(conn, sess):
    sid = sess["session_id"]
    if codex_tab(conn, sess):
        return 409, {"error": "이미 터미널에서 실행 중입니다"}
    # 같은 가족(원본·갈래)이 열려 있는 탭이 있으면 그 탭에서 /resume <id> 로 바꾼다 — Codex 공유 데몬은 그 탭이 연 적 있는
    # 대화를 계속 잡고 있어, 새 탭에서 열면 「다른 앱에서 열려 있음(읽기 전용)」이 된다
    for other in codex_family(conn, sid) - {sid}:
        o = session_row(conn, other)
        tg = codex.tab_of(o) if o else None
        if not tg:
            continue
        if o["state"] in ("working", "waiting") or codex.busy(tg):
            return 409, {"error": "같은 탭의 「%s」가 작업 중입니다 — 끝난 뒤 바꿔 주세요" % (o["title"] or "")}
        if not _hold_terminal(tg):
            return 409, {"error": "그 터미널에서 다른 조작이 진행 중입니다 — 잠시 뒤 다시 해 주세요"}
        ok, err = codex.paste(tg, "/resume " + sid)
        if not ok:
            return 502, {"error": err}
        conn.execute("UPDATE sessions SET terminal=?, state=CASE WHEN state='ended' THEN 'idle' ELSE state END, ended_at=NULL,"
                     " archived=0 WHERE session_id=?", (o["terminal"], sid))
        conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps({"detached": True}), other))
        time.sleep(1.5)
        return 200, {"ok": True, "ready": True, "handle": tg.handle, "message": "같은 탭에서 이 대화로 바꿨습니다"}
    cwd = sess["cwd"] or ""
    if not os.path.isdir(cwd):
        return 409, {"error": "세션 폴더가 없습니다: " + cwd}
    tg, err = codex_launch(conn, cwd, resume=sid)
    if not tg:
        return 502, {"error": err}
    term = tg.terminal.remember({}, tg.handle)
    conn.execute("UPDATE sessions SET terminal=?, state=CASE WHEN state='ended' THEN 'idle' ELSE state END,"
                 " state_detail=CASE WHEN state='ended' THEN NULL ELSE state_detail END, ended_at=NULL,"
                 " archived=0 WHERE session_id=?", (json.dumps(term), sid))
    conn.commit()
    ready, end = False, time.time() + 25
    while time.time() < end:   # 입력창(›)이 뜰 때까지 — 그래야 이어서 보낸 글이 들어간다
        if any(line.startswith("› ") for line in tg.terminal.screen(tg.handle)):
            ready = True
            break
        time.sleep(0.5)
    return 200, {"ok": True, "ready": ready, "handle": tg.handle,
                 "message": "보드 터미널에서 이어 열었습니다" if ready else "보드 터미널에서 여는 중입니다 — 터미널에 선택 창이 떠 있으면 먼저 골라 주세요"}


def codex_import(conn, sess, body):
    # 끝내고 다시 여는 사이(잠깐 「종료」)에도 칸을 두도록 가져오는 중으로 표시하고, 끝나면(성공·실패 모두) 바로 지운다
    IMPORTING[sess["session_id"]] = store.now()
    try:
        return _codex_import(conn, sess, body)
    finally:
        IMPORTING.pop(sess["session_id"], None)


def _codex_import(conn, sess, body):
    """보드 터미널로 가져오기: Orca·tmux 에서 도는 Codex 를 끝내고(/quit) 보드 터미널에서 같은 대화로 이어 연다(codex resume).
    보드가 조작할 수 없는 터미널이면 그쪽에서 끝낸 뒤 다시 누르게, 꺼져 있으면 바로 이어 연다. restart: 보드 터미널 것도 다시."""
    if codex.desktop_session(sess):
        return 409, {"error": "Codex 앱의 대화는 보드 터미널로 옮기지 않습니다 — 앱에서 이어 가 주세요"}
    tg = codex_tab(conn, sess)
    if tg and tg.terminal.name == "web" and not body.get("restart"):
        return 409, {"error": "이미 보드 터미널에서 돌고 있습니다"}
    if tg:
        if sess["state"] in ("working", "waiting") or codex.busy(tg):
            return 409, {"error": "작업 중이거나 확인을 기다리는 세션입니다 — 끝난 뒤 가져와 주세요"}
        if tg.terminal.name not in ("orca", "tmux", "web"):
            return 409, {"error": "보드가 이 터미널을 조작할 수 없습니다 — 그 터미널에서 /quit 로 끝낸 뒤 다시 눌러 주세요"}
        tg.terminal.write(tg.handle, "/quit")
        time.sleep(0.8)
        tg.terminal.write(tg.handle, "\r")
        _release_terminals()
        for _ in range(24):   # codex 가 끝날 때까지
            time.sleep(0.5)
            info = tg.terminal.info(tg.handle) or {}
            if info.get("agent") != "codex":
                break
        else:
            return 409, {"error": "그 터미널의 Codex 가 끝나지 않았습니다 — 터미널을 확인해 주세요"}
    elif codex.is_running(sess) and not body.get("restart"):
        return 409, {"error": "보드가 이 터미널을 조작할 수 없습니다 — 그 터미널에서 /quit 로 끝낸 뒤 다시 눌러 주세요"}
    return codex_resume(conn, dict(sess, state="ended"))


def codex_quit(conn, sess, body):
    tg = codex_tab(conn, sess)
    if not tg:
        if codex.desktop_session(sess):
            return 409, {"error": "Codex 앱의 대화는 앱에서 닫아 주세요"}
        t = store.now()
        conn.execute("UPDATE sessions SET state='ended', state_at=?, ended_at=? WHERE session_id=?", (t, t, sess["session_id"]))
        return 200, {"ok": True, "message": "종료로 표시했습니다(실행 중인 Codex 가 없었습니다)"}
    if sess["state"] == "working" and not body.get("force"):
        return 409, {"error": "작업 중입니다", "need_force": True}
    if sess["state"] == "working":
        tg.terminal.write(tg.handle, "\x1b")
        time.sleep(0.8)
    gone = lambda: (tg.terminal.info(tg.handle) or {}).get("agent") != "codex"
    # /quit(입력창을 되찾은 뒤 — 찾기·선택 창이 떠 있으면 / 가 거기로 들어갔다) → 안 끝나면 한 번 더 → Ctrl+C 두 번
    for attempt in ("quit", "quit", "ctrl-c"):
        if attempt == "quit":
            codex.paste(tg, "/quit")
        else:
            tg.terminal.write(tg.handle, "\x03")
            time.sleep(0.5)
            tg.terminal.write(tg.handle, "\x03")
        for _ in range(6):
            time.sleep(0.5)
            if gone():
                break
        if gone():
            break
    if not gone():
        return 502, {"error": "Codex 가 끝나지 않았습니다 — 터미널을 확인해 주세요"}
    t = store.now()
    conn.execute("UPDATE sessions SET state='ended', state_detail='quit', state_at=?, ended_at=? WHERE session_id=?",
                 (t, t, sess["session_id"]))
    store.add_event(conn, sess["session_id"], "end", "quit")
    return 200, {"ok": True, "message": "Codex 를 종료했습니다"}


_CODEX_INFO = {"at": 0, "v": None}


def codex_info(conn):
    """보드가 새 세션 창·칸에 쓰는 Codex 정보(설치·모델 목록·훅). 30초 기억 — 상태 요청마다 파일을 읽지 않게."""
    if time.time() - _CODEX_INFO["at"] > 30:
        _CODEX_INFO["v"] = {"available": bool(codex.binary()), "models": codex.models(), "efforts": codex.EFFORTS,
                            "hooks": codex.hooks_state()[0]}
        _CODEX_INFO["at"] = time.time()
    return dict(_CODEX_INFO["v"], available=_CODEX_INFO["v"]["available"] and bool(config.get(conn, "codex_enabled")))


def activity_of(row, since, until=None):
    """세션의 작업 과정(중간 메시지·도구 호출) — 에이전트마다 기록 형식이 달라 읽는 곳이 다르다."""
    if row["agent"] == "codex":
        return codex.activity(row["session_id"], since, until)
    return store.read_activity(row["transcript_path"], since, until)


def _codex_loop():
    """Codex 스레드(CLI·데스크톱 앱)를 보드 DB 로 — 바뀐 게 있을 때만 읽는다."""
    conn = store.connect()
    while True:
        try:
            if config.get(conn, "codex_enabled"):
                codex.sync(conn)
        except Exception:  # noqa: BLE001 - Codex 형식이 바뀌어도 서버는 계속
            traceback.print_exc()
            time.sleep(30)
        time.sleep(3)


def _recap_loop():
    """쉬고 있는 Claude Code 세션의 recap(away_summary). Claude Code 는 응답이 끝나고 몇 분~수십 분 쉬면 요약을 기록 파일에
    쓰는데, 그때는 훅이 오지 않는다(훅은 다음 요청의 응답이 끝날 때 읽는다 — 그때면 이미 지난 recap 이라 칸에 안 보인다).
    그래서 쉬는 세션의 기록 파일이 바뀌면 끝부분만 읽어 넣는다."""
    conn, seen = store.connect(), {}
    while True:
        time.sleep(15)
        try:
            rows = conn.execute("SELECT session_id, transcript_path, state_at FROM sessions WHERE agent='claude'"
                                " AND state IN ('done', 'idle', 'error') AND archived=0 AND transcript_path IS NOT NULL").fetchall()
            changed = False
            for r in rows:
                try:
                    mt = os.path.getmtime(r["transcript_path"])
                except OSError:
                    continue
                if seen.get(r["session_id"]) == mt or mt <= (r["state_at"] or 0):
                    seen[r["session_id"]] = mt
                    continue
                seen[r["session_id"]] = mt
                meta = store.scan_transcript_meta(r["transcript_path"], tail_bytes=256 * 1024)
                if meta.get("recap_at") and meta["recap_at"] > (r["state_at"] or 0) - 1:   # 쉬기 시작한 뒤에 쓴 요약만
                    changed = store.apply_recap(conn, r["session_id"], meta) or changed
            if changed:
                conn.commit()
            else:
                conn.rollback()
        except Exception:  # noqa: BLE001 - 기록 파일 형식이 바뀌어도 서버는 계속
            traceback.print_exc()
            time.sleep(30)


def _reap_loop():
    while True:
        try:
            reap_tabs()
        except Exception:  # noqa: BLE001 - 탭 정리가 서버를 죽이면 안 된다
            pass
        time.sleep(3)


def follow_compact(conn, s):
    """컴팩트가 끝났는지 기록 파일의 컴팩트 경계로도 확인한다(PostCompact 훅이 없던 세션·중간에 취소한 경우)."""
    if not s.get("compact_at"):
        return
    b = store.tail_compact_boundary(s.get("transcript_path")) if s.get("transcript_path") else None
    if b and b["at"] and b["at"] >= s["compact_at"] - 2:
        store.finish_compact(conn, s["session_id"], b["ms"])
    elif store.now() - s["compact_at"] > 15 * 60:   # 취소됐거나 실패 — 15분 넘게 매달려 있지 않게
        store.finish_compact(conn, s["session_id"])
    else:
        return
    row = conn.execute("SELECT state, state_detail, state_at, compact_at, compact_ms FROM sessions WHERE session_id=?",
                       (s["session_id"],)).fetchone()
    s.update(dict(row))


def estimate_context_windows(conn, sessions):
    """상태줄 값이 없어 창 크기를 모르는 세션의 % 를 추정한다.

    같은 모델 세션 중 하나라도 200k 를 넘겨 쓴 적이 있으면 그 모델은 1M 창이다(근거가 있는 경우만).
    알게 된 모델은 설정에 남겨 둔다. 그 밖엔 기본 200k 로 본다. 모델 ID 에 [1m] 이 붙어 있으면 1M.
    """
    known = set(store.get_setting(conn, "ctx_1m_models", []) or [])
    before = set(known)
    def model_of(s):
        return (s.get("model") or (s.get("ctx") or {}).get("model") or "").split("[")[0]
    for s in sessions:
        c, model = s.get("ctx"), model_of(s)
        if c and model and ((c.get("size") or 0) > 200000 or (c.get("used") or 0) > 200000):
            known.add(model)
    if known != before:
        store.set_setting(conn, "ctx_1m_models", sorted(known))
    for s in sessions:
        c = s.get("ctx")
        if not c or c.get("pct") is not None or not c.get("used"):
            continue
        model = s.get("model") or ""
        # 200k 를 넘겨 쓰고 있으면 그 세션은 1M 창이다
        size = 1000000 if ("[1m]" in model or model_of(s) in known or c["used"] > 200000) else 200000
        c.update(size=size, pct=round(c["used"] * 100.0 / size, 1), estimated=True)


# 세션마다 마지막 요청(작업 알림 제외)·첫 요청·마지막 응답(앞 400자와 길이 — 보드·브레인은 앞 220자까지만 쓴다).
# 각 하위 쿼리는 idx_turns_session(session_id, prompt_at)·idx_turns_session_resp 를 탄다
_LP = ("FROM turns t WHERE t.session_id=s.session_id AND t.prompt IS NOT NULL AND t.prompt NOT LIKE '<task-notification>%'"
       " ORDER BY t.prompt_at DESC, t.id DESC LIMIT 1")
_LR = "FROM turns t WHERE t.session_id=s.session_id AND t.response IS NOT NULL ORDER BY t.response_at DESC, t.id DESC LIMIT 1"
LAST_TURN_COLUMNS = (
    " (SELECT t.prompt " + _LP + ") AS lp_prompt, (SELECT t.prompt_at " + _LP + ") AS lp_at,"
    " (SELECT t.prompt FROM turns t WHERE t.session_id=s.session_id AND t.prompt IS NOT NULL"
    "  AND t.prompt NOT LIKE '<task-notification>%' ORDER BY t.prompt_at, t.id LIMIT 1) AS fp_prompt,"
    " (SELECT substr(t.response, 1, 400) " + _LR + ") AS lr_response, (SELECT length(t.response) " + _LR + ") AS lr_len,"
    " (SELECT t.response_at " + _LR + ") AS lr_at"
)


def list_state(conn, days, show_archived):
    since = store.now() - days * 86400
    archived_clause = "" if show_archived else "AND s.archived=0"
    rows = conn.execute(
        "SELECT " + ", ".join("s." + c.strip() for c in SESSION_COLUMNS.split(",")) + ","
        " (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.session_id) AS turn_count,"
        " (SELECT COUNT(*) || ':' || COALESCE(MAX(answered_at), 0) FROM asides a WHERE a.session_id=s.session_id) AS aside_sig,"
        " (SELECT COUNT(*) FROM notes n WHERE n.session_id=s.session_id AND n.done=0) AS open_notes," + LAST_TURN_COLUMNS + ","
        # 갈래: 원본의 몇 번째 턴 뒤에서 갈라졌는지(원본에서 갈라진 시각까지의 요청 수)
        " (SELECT COUNT(*) FROM turns t5 WHERE t5.session_id = s.continued_from AND t5.prompt IS NOT NULL"
        "  AND t5.prompt_at <= s.branch_at) AS branch_turns" +
        " FROM sessions s WHERE (s.last_activity >= ? OR s.pinned>0 OR s.state IN ('working','waiting')"
        "  OR EXISTS (SELECT 1 FROM notes n WHERE n.session_id=s.session_id AND n.done=0)) " + archived_clause +
        " ORDER BY s.last_activity DESC",
        (since,),
    ).fetchall()
    sessions = []
    running = focus.running_sessions()
    cx_pids, cx_desktop = codex.running_pids() if any(r["agent"] == "codex" for r in rows) else (set(), False)
    for r in rows:
        s = dict(r)
        cx = s.get("agent") == "codex"
        tg, is_brain = focus.target_of(s), brain.is_brain(s.get("cwd"))
        handle = tg.handle if tg else None
        # 요청도 기록 파일도 없는 세션은 아직 아무것도 안 한 것 — 백그라운드 데몬이 미리 띄워 둔 세션 등이라 숨긴다
        if not s["turn_count"] and not s["pinned"] and not s["open_notes"] and s["state"] != "waiting" \
                and not os.path.isfile(r["transcript_path"] or "") \
                and handle not in LAUNCHED \
                and not is_brain:   # 보드에서 방금 띄운 세션·브레인은 빈 칸으로 보인다
            continue
        if not cx:   # Claude Code 기록 파일에서 /rename·컴팩트 경계를 따라간다
            follow_rename(conn, s)
            follow_compact(conn, s)
        s["term_handle"] = handle   # 보드에서 새로 띄운 세션을 찾는 데 쓴다
        s["term_web"] = bool(tg and tg.terminal.name == "web")   # 보드 터미널(xterm.js)에서 도는 세션 — 칸 안에서 터미널을 연다
        s["term_attach"] = tg.terminal.name if tg and tg.terminal.name in ATTACHABLE else ""   # 칸 안에서 열 수 있는 터미널(tmux 도)
        s["term_name"] = tg.terminal.name if tg else ""   # 어느 터미널(orca·tmux·web …) — 보드 터미널로 가져오기 안내에
        s["importing"] = store.now() - IMPORTING.get(s["session_id"], 0) < 180   # 가져오는 중 — 잠깐 종료로 보여도 칸을 둔다
        s["restarting"] = s["importing"] and s["session_id"] in RESTARTING
        s["brain"] = is_brain       # 브레인 세션은 칸 대신 브레인 패널에
        s["term_label"] = term_label(s.get("terminal"), tg)   # 어디서 도는지(보드 터미널이 아니면 칸에 띠로 보인다)
        # 프로세스가 살아 있는지(Claude Code 실행 목록) — 모르면 True. 꺼졌는데 「대기」로 남은 세션을 칸에 표시한다
        if cx:
            # Codex: CLI 는 훅이 적은 codex 프로세스, 데스크톱 스레드는 앱이 떠 있는지. 보드 터미널에서 띄웠으면 그 탭이 살아 있는지
            s["running"] = codex.is_running(s, cx_pids, cx_desktop) or bool(
                handle and (tg.terminal.info(handle) or {}).get("agent") == "codex")
            s["desktop"] = "codex_desktop" in (s.get("terminal") or "")
            s["remote"] = None
        else:
            s["running"] = running is None or s["session_id"] in running
            # 원격(Remote Control): 실행 목록이 알려 주는 지금 연결된 원격 세션 — 훅 기록(환경 변수)보다 정확하다(자동 연결도 보인다)
            if running is not None:
                s["remote"] = (running.get(s["session_id"]) or {}).get("bridge") or None
        s.pop("terminal", None)
        if cx:
            s["ctx"] = codex.context_info(s.get("transcript_path"))
            s["cx_perm"] = codex.perm_mode(s.get("transcript_path"), s.get("cwd"), s["session_id"])   # 권한 상태(자동 검토·Full Access …) — 칸 머리 칩
            s["goal"], s["bg_tasks"] = None, []
        else:
            s["ctx"] = store.context_info(s["session_id"], s.get("transcript_path"))
            s["goal"] = store.goal_info(s.get("transcript_path"))   # /goal (기록 파일이 그대로면 기억한 값)
            # 그 세션이 띄운 백그라운드 작업(서브에이전트·백그라운드 명령) — 진행 중·끝난 지 30분 안 된 것. 늘어난 만큼만 읽는다
            s["bg_tasks"] = [{k: x[k] for k in ("id", "kind", "desc", "started_at", "status", "finished_at", "summary")}
                             for x in store.bg_tasks(s.get("transcript_path"))] if s["state"] != "ended" else []
        # 커밋 안 한 변경 수 — 백그라운드에서 확인해 둔 값(변경 보기와 같은 폴더: 워크트리 없으면 cwd)
        s["dirty"] = dirty_of(s.get("worktree") or s.get("cwd")) if s["state"] != "ended" and not is_brain else None
        # 마지막·첫 요청과 마지막 응답은 위 SELECT 의 하위 쿼리로 한 번에(세션마다 쿼리 3개씩 돌던 것)
        s["last_prompt"] = {"prompt": s.pop("lp_prompt"), "prompt_at": s.pop("lp_at")} if s["lp_prompt"] is not None else None
        s["first_prompt"] = s.pop("fp_prompt")
        s["last_response"] = {"response": s.pop("lr_response"), "len": s.pop("lr_len"), "response_at": s.pop("lr_at")} \
            if s["lr_at"] is not None else None
        for k in ("lp_prompt", "lp_at", "lr_response", "lr_len", "lr_at"):
            s.pop(k, None)
        last_response = s["last_response"]
        s["unread"] = bool(last_response and last_response["response_at"] > (s["last_seen_at"] or 0))
        sessions.append(s)
    estimate_context_windows(conn, sessions)
    notes = [dict(n) for n in conn.execute(
        "SELECT * FROM notes WHERE done=0 OR done_at >= ? ORDER BY done, created_at DESC",
        (store.now() - 86400,),
    ).fetchall()]
    settings = {k: config.get(conn, k) for k in ("auto_remote", "auto_daily", "brain_slack_every", "board_cols", "cardchat_model", "cardchat_effort", "done_cards_days",
                                                   "new_default_model", "new_default_effort", "brain_model", "brain_permission_mode", "slack_team")}
    # 할 일 카드가 생긴 시각들 — 보드가 「마지막으로 인박스를 본 뒤 새로 온 카드」를 센다(본 시각은 브라우저에 기억)
    inbox_new_at = [r[0] for r in conn.execute("SELECT created_at FROM inbox WHERE status='new'")]
    auto_refresh_usage()
    return {"sessions": sessions, "notes": notes, "settings": settings, "usage": store.read_usage(), "now": store.now(),
            "codex_usage": codex.rate_limits() if codex.available() else None,
            "codex": codex_info(conn) if codex.available() else None,
            "inbox_new": len(inbox_new_at), "inbox_new_at": inbox_new_at, "boards": boards.listing(conn, show_archived),
            "update": update.summary(conn), "setup": setup_issues(conn), "dev": devlog.enabled()}


def setup_issues(conn):
    """보드 위 안내 띠: 빠진 훅·승인(agentsetup). 읽다 실패해도 상태 요청은 살린다."""
    try:
        return agentsetup.issues(conn)
    except Exception:  # noqa: BLE001
        return []


AGENTS = {"at": 0, "items": []}


AGENT_STATE = {"working": "working", "blocked": "waiting", "done": "done", "idle": "idle"}


FORK_SEEN = set()   # 원본 찾기를 이미 해 본 백그라운드 세션(서버가 떠 있는 동안 한 번씩)


def sync_agents(conn, items):
    """돌고 있는(pid 있는) 백그라운드 세션인데 보드 기록은 「종료」면 되살린다.

    백그라운드 세션은 미리 띄워 둔 예비 프로세스(--bg-spare)를 꺼내 쓰는데, 훅은 그 프로세스의 시작을 무시한다 —
    끝난 세션을 백그라운드로 다시 열면 첫 요청 전까지 보드엔 종료로 남아 칸이 안 보였다.
    같은 이유로 /fork 갈래가 원본에 이어지지 않아 같은 이름의 칸이 따로 둘 섰다 — 여기서 원본을 찾아 갈래로 잇는다."""
    for x in items:
        st = AGENT_STATE.get(x.get("state"))
        if not x.get("pid") or not st or not x.get("sessionId"):
            continue
        cur = conn.execute("UPDATE sessions SET state=?, state_detail='background', state_at=?, ended_at=NULL, bg=1"
                           " WHERE session_id=? AND state='ended'", (st, store.now(), x["sessionId"]))
        if x["sessionId"] not in FORK_SEEN:
            FORK_SEEN.add(x["sessionId"])
            link_bg_fork(conn, x["sessionId"], x.get("id"))
        if cur.rowcount or conn.in_transaction:
            conn.commit()


def link_bg_fork(conn, sid, agent_id=None):
    """훅을 거치지 않은 백그라운드 세션을 제자리에 둔다.

    - 에이전트 id 는 처음 백그라운드로 보낸 세션의 ID 앞자리다(실측). 지금 세션 ID 와 다르면 그 세션이 백그라운드로
      옮겨져 새 ID 로 이어진 것 — 그 세션의 넥스트 액션·고정 등을 옮기고 끝난 것으로 둔다(대화는 새 기록에 복사돼 있다).
    - 그다음 /fork 갈래면 원본에 잇는다(갈래 탭으로 묶임). 원본은 앞 메시지가 겹치는 같은 저장소의 세션 — 갈라진 지
      며칠 지나 처음 보일 수도 있어 2주까지 본다."""
    row = conn.execute("SELECT transcript_path, cwd, project_root, continued_from, branch_kind FROM sessions WHERE session_id=?",
                       (sid,)).fetchone()
    if row is None or row["continued_from"] or row["branch_kind"] or not row["transcript_path"]:
        return
    skip = {sid}
    if agent_id and re.fullmatch(r"[0-9a-f]{8}", agent_id) and not sid.startswith(agent_id):
        pred = conn.execute("SELECT session_id, continued_from, branch_at, branch_kind FROM sessions WHERE session_id LIKE ?"
                            " AND session_id != ? AND state != 'ended'", (agent_id + "%", sid)).fetchall()
        if len(pred) == 1:
            p = pred[0]
            hook.move_session(conn, p["session_id"], sid, turns=False, terminal=False)
            skip.add(p["session_id"])
            if p["continued_from"] and p["branch_kind"] in ("branch", "fork"):   # 옮기기 전 세션이 갈래였으면 그 자리를 물려받는다
                conn.execute("UPDATE sessions SET continued_from=?, branch_at=?, branch_kind=? WHERE session_id=?",
                             (p["continued_from"], p["branch_at"], p["branch_kind"], sid))
                return
    origin = store.fork_origin(row["transcript_path"]) or hook.guess_origin(
        conn, sid, row["transcript_path"], row["cwd"], root=row["project_root"] or row["cwd"], days=14, skip=skip)
    if origin and origin[0] in skip:
        return
    hook.link_fork(conn, sid, hook.closest_parent(conn, sid, row["transcript_path"], origin), True)


def agents_list(fresh=False):
    """`claude agents --json`(대화형·백그라운드 세션 목록). 5초 기억 — 보드가 자주 묻는다."""
    if fresh or store.now() - AGENTS["at"] > 5:
        try:
            r = subprocess.run([daily._claude_bin(), "agents", "--json"], capture_output=True, text=True, timeout=15,
                               env=daily._clean_env())
            AGENTS["items"] = json.loads(r.stdout or "[]") if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError):
            AGENTS["items"] = []
        AGENTS["at"] = store.now()
    return [dict(x) for x in AGENTS["items"]]


INBOX_STATUS_NAMES = {"new": "할 일", "doing": "진행 중", "sent": "세션 입력창에 넣음", "reply": "답장만 남음", "done": "완료", "ignored": "완료"}


def session_detail(conn, sid, limit=300):
    return sessions_detail(conn, {sid: limit}).get(sid)


def sessions_detail(conn, want):
    """{session_id: 최근 턴 수} → {session_id: {session, turns, notes, asides}}. 세션이 몇 개든 쿼리는 네 번.

    보드가 화면에 보이는 칸들의 대화를 한 번에 받는다(칸마다 요청·쿼리하지 않게). 없는 세션은 빠진다.
    """
    if not want:
        return {}
    sids = list(want)
    marks = ",".join("?" * len(sids))
    out = {r["session_id"]: {"session": dict(r), "turns": [], "notes": [], "asides": []} for r in conn.execute(
        "SELECT " + SESSION_COLUMNS + " FROM sessions WHERE session_id IN (%s)" % marks, sids)}
    if not out:
        return out
    # 세션마다 최근 N 턴: 창 함수로 세션별 순번을 매겨 한 번에 자른다(각 세션의 N 은 want 에서)
    lims = ",".join("(?, ?)" for _ in sids)
    for r in conn.execute(
            "WITH want(sid, lim) AS (VALUES %s)"
            " SELECT t.* FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY session_id"
            "  ORDER BY COALESCE(prompt_at, response_at) DESC, id DESC) AS rn FROM turns WHERE session_id IN (%s)) t"
            " JOIN want ON want.sid = t.session_id WHERE t.rn <= want.lim"
            " ORDER BY t.session_id, COALESCE(t.prompt_at, t.response_at), t.id" % (lims, marks),
            [x for sid in sids for x in (sid, int(want[sid]))] + sids):
        if r["session_id"] in out:
            d = dict(r)
            d.pop("rn", None)
            out[r["session_id"]]["turns"].append(d)
    for n in conn.execute("SELECT * FROM notes WHERE session_id IN (%s) ORDER BY done, created_at DESC" % marks, sids):
        if n["session_id"] in out:
            out[n["session_id"]]["notes"].append(dict(n))
    for a in conn.execute("SELECT session_id, id, question, answer, status, at, answered_at FROM asides"
                          " WHERE session_id IN (%s) ORDER BY at" % marks, sids):
        if a["session_id"] in out:
            d = dict(a)
            d.pop("session_id")
            out[a["session_id"]]["asides"].append(d)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "Argus/1"

    def log_message(self, fmt, *args):  # 요청마다 로그를 남기지 않는다
        pass

    # ---------------------------------------------------------- 공통
    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
        dev = getattr(self, "_dev", None)
        if dev and code >= 400 and isinstance(body, dict):   # 개발용: 보내기 실패를 .dev/ 에 남긴다(devlog — .dev/ 가 있을 때만)
            self._dev = None
            name = devlog.record("send", dev["sess"], error=body.get("error"), text=dev["text"], status=code)
            if name:
                body = dict(body, devlog=name)
        self._sent_code = code   # 다른 경로를 불러 응답을 대신 보낸 뒤(예: 권한 → 다시 시작) 성공했는지 보려고
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in security_headers():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        # 다른 사이트의 폼 전송(CSRF)은 application/json 을 보낼 수 없다
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length > MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None   # 배열·글자 하나 같은 본문은 받지 않는다(body.get 에서 끊겼다)

    def _dispatch(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        url = urlparse(self.path)
        path = url.path
        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        body = None
        if method in ("POST", "PATCH", "DELETE"):
            update.LAST_UI[0] = time.time()   # 보드에서 무언가 했다 — 자동 업데이트는 한동안 조용할 때만
        if method in ("POST", "PATCH"):
            body = self._json_body()
            if body is None:
                return self._send(415, {"error": "JSON 객체 본문(application/json)이 필요합니다"})
        conn = store.connect()
        self._dev = None
        try:
            with conn:
                return self._api(conn, method, path, parse_qs(url.query), body)
        except ValueError as e:
            # 쿼리의 숫자 자리(limit·days·since·start …)에 숫자가 아닌 값 — 연결을 그냥 끊지 말고 400 으로 알린다
            return self._send(400, {"error": "잘못된 값: %s" % str(e)[:200]})
        except Exception as e:  # noqa: BLE001 - 필드 모양이 다른 본문 등 — 응답 없이 연결이 끊기지 않게(쓰기는 with conn 이 되돌린다)
            sys.stderr.write(traceback.format_exc())
            try:
                return self._send(500, {"error": "처리하지 못했습니다: %s" % type(e).__name__})
            except OSError:
                return None
        finally:
            conn.close()
            _release_terminals()

    def do_GET(self):
        m = re.fullmatch(r"/api/term/([\w\-]+)/ws", urlparse(self.path).path)
        if m and (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self._term_ws(m.group(1), parse_qs(urlparse(self.path).query))
        self._dispatch("GET")

    def _term_ok(self, token, need_origin=True):
        """보드 터미널을 다루는 요청: 보드 화면(같은 출처)에서 왔고, 서버를 띄울 때마다 바뀌는 토큰이 맞아야 한다.
        need_origin=False: 브라우저는 같은 출처 GET 에 Origin 을 싣지 않는다 — 실렸으면 맞아야 하고, 없으면 토큰 헤더로 가린다
        (다른 사이트는 미리 묻기(preflight) 없이 그 헤더를 실을 수 없고, 응답도 읽지 못한다)."""
        origins = {"http://%s:%d" % (h, store.PORT) for h in ALLOWED_HOSTS}
        origin = self.headers.get("Origin")
        if origin is None and not need_origin:
            origin = next(iter(origins))
        return self._host_ok() and origin in origins and webterm.token_ok(token)

    def _term_ws(self, sid, query):
        """보드 터미널(xterm.js) 웹소켓: 그 세션의 tmux 세션에 붙는다. 같은 출처·토큰·보드 터미널 세션만.
        sid 가 shell 이면 오른쪽 터미널 드로어의 셸 탭(name=shell-…)."""
        if not self._term_ok((query.get("token") or [""])[0]):
            return self._send(403, {"error": "forbidden"})
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._send(400, {"error": "웹소켓 요청이 아닙니다"})
        if sid == "shell":
            web, name = terminals.registry().get("web"), (query.get("name") or [""])[0]
            if not web or not (web.has_shell(name) or web.has_board_shell(name)):   # 드로어 셸 탭 · 보드의 터미널 칸
                return self._send(404, {"error": "그 셸 탭이 없습니다"})
            return self._ws_attach(key, name, query)
        if sid == "launch":   # 막 띄운 세션(훅이 기록하기 전 — 폴더 신뢰 확인 창 등): 보드가 띄운 창만
            h, tname = (query.get("handle") or [""])[0], (query.get("term") or ["web"])[0]
            t = terminals.registry().get(tname) if tname in ATTACHABLE else None
            tg = terminals.Target(t, h) if t and t.valid(h) and h in LAUNCHED else None
        else:
            conn = store.connect()
            try:
                row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
            finally:
                conn.close()
            tg = focus.target_of(dict(row)) if row else None
        # 칸 안에서 열 수 있는 것: 보드 터미널 세션과, 사용자의 tmux 에서 도는 (보드에 기록된) Claude 세션의 창
        if not tg or tg.terminal.name not in ATTACHABLE:
            return self._send(404, {"error": "칸 안에서 열 수 있는 터미널(보드 터미널·tmux)이 아닙니다"})
        argv, err = tg.terminal.attach_argv(tg.handle)
        if not argv:
            return self._send(410, {"error": err})
        try:
            return self._ws_attach(key, argv, query)
        finally:
            tg.terminal.release(argv)   # tmux: 칸 안에서 보려고 만든 묶음 세션을 치운다(원래 세션은 그대로)

    def _ws_attach(self, key, argv, query):
        """웹소켓으로 올리고 attach 명령(argv — 문자열이면 보드 터미널 서버의 그 tmux 세션 이름)을 PTY 로 이어 준다."""
        if isinstance(argv, str):
            tmux_bin = tmux_term._bin()
            if not tmux_bin:
                return self._send(410, {"error": "tmux 가 없습니다"})
            argv = [tmux_bin, "-u"] + web_term.SERVER + ["attach-session", "-t", "=" + argv]
        try:
            cols, rows = int((query.get("cols") or ["120"])[0]), int((query.get("rows") or ["36"])[0])
        except ValueError:
            cols, rows = 120, 36
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", webterm.accept_key(key))
        self.end_headers()
        self.close_connection = True
        webterm.relay(webterm.WebSocket(self.rfile, self.wfile), argv, cols, rows)

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _boards(self, conn, method, path, body, query=None):
        """세션이 아닌 보드 칸(csl/boards.py): 만들기 · 문서 읽기·저장 · AI 편집·되돌리기 · 이름 · 지우기.
        터미널 칸을 만들거나 다시 여는 것은 셸을 띄우는 길이라 셸 탭과 같이 같은 출처·토큰(X-Argus-Term)을 요구한다."""
        term_ok = lambda: self._term_ok(self.headers.get("X-Argus-Term") or "")
        if path == "/api/boards" and method == "POST":
            kind = body.get("kind")
            if kind == "term" and not term_ok():
                return self._send(403, {"error": "forbidden"})
            b, err = boards.create(conn, kind, body.get("cwd") or "", body.get("title") or "")
            return self._send(200, {"ok": True, "board": b}) if b else self._send(400, {"error": err})
        m = re.fullmatch(r"/api/boards/(\d+)(?:/(doc|ai|undo|rename|reopen))?", path)
        if not m:
            return self._send(404, {"error": "not found"})
        bid, action = int(m.group(1)), m.group(2)
        if not conn.execute("SELECT 1 FROM boards WHERE id=?", (bid,)).fetchone():
            return self._send(404, {"error": "그 칸이 없습니다"})
        if action is None and method == "GET":
            d = boards.read_doc(conn, bid)
            return self._send(200, d) if d is not None else self._send(400, {"error": "문서 칸이 아닙니다"})
        if action is None and method == "DELETE":
            # file=trash: 마크다운 문서 파일도 휴지통으로(되살릴 수 있게 완전히 지우지는 않는다)
            _, moved = boards.remove(conn, bid, trash_file=((query or {}).get("file") or [""])[0] == "trash")
            return self._send(200, {"ok": True, "trashed": moved})
        if method != "POST":
            return self._send(405, {"error": "method"})
        if action == "doc":
            ok, err, mtime = boards.write_doc(conn, bid, body.get("text"), body.get("mtime"))
            return self._send(200, {"ok": True, "mtime": mtime}) if ok else self._send(409, {"error": err, "mtime": mtime})
        if action == "ai":
            ok, err = boards.ask(conn, bid, body.get("text"), body.get("model"), body.get("effort"))
            return self._send(200, {"ok": True}) if ok else self._send(409, {"error": err})
        if action == "undo":
            ok, err = boards.undo(conn, bid, int(body.get("mid") or 0))
            return self._send(200, {"ok": True}) if ok else self._send(409, {"error": err})
        if action == "rename":
            return self._send(200, {"ok": True}) if boards.rename(conn, bid, body.get("title")) else self._send(400, {"error": "이름을 적어 주세요"})
        if action == "reopen":
            if not term_ok():
                return self._send(403, {"error": "forbidden"})
            name = boards.reopen_shell(conn, bid)
            return self._send(200, {"ok": True, "shell": name}) if name else self._send(502, {"error": "터미널을 열지 못했습니다"})
        return self._send(404, {"error": "not found"})

    def _import_route(self, conn, sess, body):
        """보드 터미널로 가져오기·세션 다시 시작(restart)의 앞부분 — 확인을 마치고 _import 로."""
        sid = sess["session_id"]
        # restart: 이미 보드 터미널에 있는 세션도 같은 방법(끝내고 같은 대화로 이어 열기)으로 다시 연다 — 환경이 꼬인 세션을
        # 깨끗한 환경에서 다시 띄울 때(실행 중인 프로세스의 환경 변수는 바꿀 수 없다)
        restart = bool(body.get("restart"))
        if sess.get("agent") == "codex":
            return self._send(*codex_import(conn, sess, body))
        if brain.is_brain(sess["cwd"]) and not restart:
            return self._send(409, {"error": "브레인은 늘 보드 터미널에서 돕니다 — 브레인 패널에서 다시 시작해 주세요"})
        tg = focus.target_of(sess)
        if tg and tg.terminal.name == "web" and sess["state"] != "ended" and not not_running(conn, sid) and not restart:
            return self._send(409, {"error": "이미 보드 터미널에서 돌고 있습니다"})
        full = conn.execute("SELECT bg, cwd, worktree FROM sessions WHERE session_id=?", (sid,)).fetchone()
        x = live_agent(sid) if full["bg"] else None
        if x:
            tg2, err = focus.new_claude_terminal(x.get("cwd") or full["cwd"], extra="attach " + shlex.quote(x["id"]), terminal="web")
            if not tg2:
                return self._send(502, {"error": err})
            LAUNCHED[tg2.handle] = store.now()
            conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(tg2.terminal.remember({}, tg2.handle)), sid))
            return self._send(200, {"ok": True, "message": "백그라운드 세션을 보드 터미널에 붙였습니다"})
        # 이어 열 폴더 — 지워진 워크트리면 저장소 루트에서(Claude Code 가 워크트리 묶음을 풀고 그 폴더에서 이어 연다)
        cwd = full["cwd"] or ""
        if not os.path.isdir(cwd):
            root = conn.execute("SELECT project_root FROM sessions WHERE session_id=?", (sid,)).fetchone()[0] or ""
            if "/.claude/worktrees/" in cwd and os.path.isdir(root):
                cwd = root
            else:
                return self._send(409, {"error": "세션 폴더가 없습니다: " + cwd})
        IMPORTING[sid] = store.now()
        if body.get("restart"):
            RESTARTING.add(sid)
        try:
            return self._import(conn, sid, sess, tg, cwd, body)
        finally:
            IMPORTING.pop(sid, None)
            RESTARTING.discard(sid)

    def _import(self, conn, sid, sess, tg, cwd, body):
        """보드 터미널로 가져오기의 본체 — 지금 터미널에서 끝내고(Orca·tmux) 보드 터미널에서 같은 세션으로 이어 연다."""
        running = sid in (focus.running_sessions() or {})
        if running:
            if sess["state"] in ("working", "waiting"):
                return self._send(409, {"error": "작업 중이거나 확인을 기다리는 세션입니다 — 끝난 뒤 가져와 주세요"})
            if not tg or tg.terminal.name not in ("orca", "tmux", "web"):
                return self._send(409, {"error": "보드가 이 터미널을 조작할 수 없습니다 — 그 터미널에서 /exit 로 끝낸 뒤 다시 눌러 주세요"})
            # stop_tasks: 그 세션에서 도는 백그라운드 작업(개발 서버 등)도 끄고 옮긴다 — 아니면 종료가 물을 때 취소한다.
            # 세션 다시 시작(restart)은 확인 창에서 「백그라운드 작업은 끊깁니다」를 보고 누른 것이라 늘 끄고 다시 연다 — 아니면
            # 백그라운드 셸(개발 서버 등)이 도는 세션은 종료가 「Background work is running」을 물어 매번 취소됐다
            code, res = self._quit(conn, sid, {"worktree": "keep", "abort_prompt": True,
                                               "stop_tasks": bool(body.get("stop_tasks") or body.get("restart"))})
            if code != 200 or res.get("pending"):
                return self._send(409 if code == 200 else code, {"error": "종료하지 못해 가져오지 않았습니다: " + (res.get("error") or res.get("message") or "")})
            for _ in range(20):   # 프로세스 목록에서 빠질 때까지
                if sid not in (focus.running_sessions() or {}):
                    break
                time.sleep(0.5)
        tg2, err = focus.new_claude_terminal(cwd, extra=resume_flags(conn, sid) + "--resume " + shlex.quote(sid), terminal="web")
        if not tg2:
            return self._send(502, {"error": err})
        LAUNCHED[tg2.handle] = store.now()
        conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(tg2.terminal.remember({}, tg2.handle)), sid))
        conn.commit()
        ready, end = False, time.time() + 30
        while time.time() < end:
            st = conn.execute("SELECT state FROM sessions WHERE session_id=?", (sid,)).fetchone()[0]
            if st != "ended" and focus.input_line(tg2) is not None:
                ready = True
                break
            time.sleep(0.5)
        return self._send(200, {"ok": True, "ready": ready, "mode": body.get("mode"),
                                "message": (body.get("done_msg") or "보드 터미널로 가져왔습니다") if ready else
                                "보드 터미널에서 여는 중입니다 — 선택 창이 떠 있으면 칸의 「⌨ 터미널」에서 골라 주세요"})

    def _quit(self, conn, sid, body):
        """세션 종료(⋮ → 세션 종료·보드 터미널로 가져오기). (HTTP 코드, 응답 본문).
        body: force(작업 중이면 멈추고) · worktree(keep·remove — 종료 때 워크트리를 묻는 창에 답할 것)"""
        # 세션 종료 — 터미널에서 Claude Code 를 닫은 것처럼: /exit 로 정상 종료시키고 탭을 닫는다.
        # 작업 중이면 force 일 때만 먼저 Esc 로 멈춘다. 종료가 선택 창(바꾼 게 있는 워크트리를 남길지 등)을 띄우면 거기서 멈추고 사람이 고른다
        sess = session_row(conn, sid)
        if sess is None:
            return (404, {"error": "no session"})
        why = brain_guard(conn, body, sess["session_id"])
        if why:
            return (403, {"error": why})
        if sess["state"] == "ended":
            return (409, {"error": "이미 종료된 세션입니다"})
        if sess.get("agent") == "codex":
            return codex_quit(conn, sess, body)
        bg = conn.execute("SELECT bg FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()["bg"]   # session_row 엔 없다
        x = live_agent(sess["session_id"]) if bg else None
        if x:
            # 백그라운드 세션 — 탭에 `claude attach` 로 붙어 있어도 거기서 /exit 를 치면 보기만 떨어지고 세션은 계속 돈다
            # (칸이 그대로 남았다). claude stop 으로 멈추고, 붙어 있던 탭이 있으면 그 탭도 닫는다
            if (sess["state"] == "working" or x.get("state") == "working") and not body.get("force"):
                return (409, {"error": "작업 중입니다", "need_force": True})
            try:
                r = subprocess.run([daily._claude_bin(), "stop", x["id"]], capture_output=True, text=True, timeout=20,
                                   env=daily._clean_env())
            except (OSError, subprocess.SubprocessError) as e:
                return (502, {"error": str(e)[:200]})
            AGENTS["at"] = 0
            if r.returncode != 0:
                return (502, {"error": (r.stderr or r.stdout or "멈추지 못했습니다").strip()[:300]})
            # 멈춘 백그라운드 세션은 SessionEnd 훅이 오지 않을 수 있다 — 여기서 종료로 둔다
            t = store.now()
            conn.execute("UPDATE sessions SET state='ended', state_detail='stopped', state_at=?, ended_at=? WHERE session_id=?",
                         (t, t, sess["session_id"]))
            store.add_event(conn, sess["session_id"], "end", "stopped")
            conn.commit()
            tg = focus.target_of(sess)
            closed = False
            if tg:
                REAPED.add(tg.handle)
                LAUNCHED.pop(tg.handle, None)
                closed = tg.terminal.close(tg.handle)
            return (200, {"ok": True, "message": "백그라운드 세션을 멈췄습니다" + (" · 터미널 탭도 닫았습니다" if closed else "")})
        if bg and not focus.handle_of(sess):
            return (409, {"error": "그 백그라운드 세션의 프로세스를 찾지 못했습니다"})
        handle, err = terminal_target(conn, sess)
        if not handle:
            return (409, {"error": err})
        box = focus.dialog_text(handle)
        if box and body.get("abort_prompt"):   # 가져오기: 사람이 고를 창이 떠 있으면 건드리지 않는다
            return (409, {"error": "터미널에 선택 창이 떠 있거나 화면을 읽지 못했습니다 — 그 터미널을 확인해 주세요"})
        if box:
            if focus.prompt_view(handle):   # 고를 수 있는 창이면 칸의 🔐 카드로 고르게
                _mark_prompt(conn, sess["session_id"], "종료 중 — 터미널이 묻고 있습니다")
                return (409, {"error": "터미널이 고를 것을 묻고 있습니다 — 칸의 🔐 확인 필요 카드에서 골라 주세요"})
            return (409, {"error": "터미널에 선택 창이 떠 있습니다 — 먼저 골라 주세요: " + box})
        if sess["state"] == "working" or focus.is_busy(handle):
            if not body.get("force"):
                return (409, {"error": "작업 중입니다", "need_force": True})
            focus._raw(handle, focus.ESC)   # 작업을 멈추고(Claude Code 의 중단 키)
            for _ in range(20):
                time.sleep(0.4)
                if not focus.is_busy(handle) and focus.input_line(handle) is not None:
                    break
            else:
                return (409, {"error": "작업을 멈추지 못했습니다 — 터미널에서 확인해 주세요"})
        ok, _, err = focus.send_prompt(handle, "/exit")
        if not ok:
            return (502, {"error": "/exit 를 보내지 못했습니다: " + (err or "")})
        # 워크트리 세션: 종료할 때 Claude Code 가 묻는 「Keep worktree / Remove worktree」에 보드에서 고른 대로 답한다
        want_wt = {"keep": "keep worktree", "remove": "remove worktree"}.get(body.get("worktree") or "")
        wt_done = None
        stopped_tasks = False
        for _ in range(40):   # Claude 가 끝날 때까지(워크트리 정리 등) — 선택 창이 뜨면 사람에게 맡긴다
            time.sleep(0.5)
            if not focus.claude_alive(sess, handle):
                break
            # 닫히는 도중엔 입력칸이 사라진 화면이 잠깐 보인다 — 번호 고르기 줄(❯ 1. …)이 있을 때만 선택 창으로 본다
            lines = focus._screen(handle)
            opts = [ln.strip() for ln in lines[-15:] if focus._OPTION.match(ln)]
            if opts and focus._input_line_of(lines) is None and want_wt and not wt_done:
                v = focus.prompt_view(handle)
                o = next((o for o in (v or {}).get("options", []) if o["label"].lower().startswith(want_wt)), None)
                if o:
                    ok_wt, _ = focus.answer_prompt(handle, o["n"], o["label"])
                    if ok_wt:
                        wt_done = body.get("worktree")
                        continue
            if opts and focus._input_line_of(lines) is None and body.get("stop_tasks") and not stopped_tasks:
                # 「Background work is running — Exit and stop tasks」: 사용자가 백그라운드 작업(개발 서버 등)도 끄고 종료하기로 했다
                v = focus.prompt_view(handle) or {}
                o = next((o for o in v.get("options", []) if o["label"].lower().startswith("exit and stop tasks")), None)
                if o and focus.answer_prompt(handle, o["n"], o["label"])[0]:
                    stopped_tasks = True
                    continue
            if opts and focus._input_line_of(lines) is None and body.get("abort_prompt"):
                # 가져오기: 종료가 예상 밖의 것을 묻는다(「Background work is running」 — 돌고 있는 개발 서버 등을 끌지).
                # 사람이 정할 일이라 Esc 로 종료를 취소하고 원래대로 둔다
                v = focus.prompt_view(handle) or {}
                focus._raw(handle, focus.ESC)
                time.sleep(0.8)
                why = " / ".join(v.get("head") or [])[:200]
                return (409, {"error": "종료하면서 터미널이 묻는 것이 있어 취소했습니다" + (": " + why if why else ""), "aborted": True})
            if opts and focus._input_line_of(lines) is None:
                # 바꾼 게 있는 워크트리를 남길지 등 — 「확인 필요」로 두어 칸에 🔐 카드가 뜨게(고르면 Claude 가 끝나고 탭은 서버가 닫는다)
                _mark_prompt(conn, sess["session_id"], "종료 중 — 터미널이 묻고 있습니다")
                return (200, {"ok": True, "pending": True, "message": "종료하는 중 터미널이 고를 것을 묻습니다 — 칸의 🔐 확인 필요 카드에서 골라 주세요"})
        else:
            return (409, {"error": "Claude 가 아직 끝나지 않았습니다 — 터미널에서 확인해 주세요"})
        # 보드가 띄운 탭은 셸까지 끝났고, 직접 연 탭은 셸이 남는다 — 어느 쪽이든 탭을 닫는다(Claude 를 닫은 뒤라 잃을 것은 없다)
        REAPED.add(handle.handle)
        LAUNCHED.pop(handle.handle, None)
        closed = handle.terminal.close(handle.handle)
        if want_wt == "remove worktree" and not wt_done:
            # 이어 연 워크트리 세션은 Claude Code 가 묻지 않고 끝난다 — 고른 대로 보드가 정리한다
            wt = conn.execute("SELECT worktree FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()[0]
            if wt and remove_claude_worktree(wt):
                wt_done = "remove"
        wt_msg = {"keep": " · 워크트리는 남겼습니다", "remove": " · 워크트리를 정리했습니다"}.get(wt_done or "", "")
        return (200, {"ok": True, "message": "세션을 종료했습니다" + wt_msg + (" · 터미널 탭도 닫았습니다" if closed else "(탭은 닫지 못함 — 직접 닫아 주세요)")})


    def _static(self, path):
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC_DIR, name))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    # ---------------------------------------------------------- API
    def _api(self, conn, method, path, query, body):
        # 브레인(./argus)이 세션을 움직이거나 바꾸는 요청 — 자동 정리·자동 알림 차례면 한곳에서 막는다
        # (그때 읽는 슬랙·메일 본문의 지시로 다른 세션·설정이 움직이지 않게. 사용자가 브레인 대화에서 시킨 것만)
        # 보드에서 그 세션에 보내거나 답하면(사람 — 브레인·CLI 가 아님) 그 세션을 본 것이다. 알림(🔔)의 「안 봄」은
        # 보낸 시각까지로 센다 — 확인 필요·질문에 보드 카드로 답해도 알림이 남던 것
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/(?:send|answer|prompt)", path)
        if m and method == "POST" and isinstance(body, dict) and not body.get("from"):
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (store.now(), m.group(1)))
        if method in ("POST", "PATCH", "DELETE") and isinstance(body, dict) and body.get("from"):
            g = BRAIN_ACTIONS.fullmatch(path)
            if g:
                why = brain_guard(conn, body, g.group(1) or "")
                if why:
                    return self._send(403, {"error": why})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/commands", path)
        if m and method == "GET":
            row = conn.execute("SELECT project_root, cwd FROM sessions WHERE session_id=?", (m.group(1),)).fetchone()
            root = (row["project_root"] or row["cwd"]) if row else None
            if row and conn.execute("SELECT agent FROM sessions WHERE session_id=?", (m.group(1),)).fetchone()[0] == "codex":
                return self._send(200, {"commands": codex.COMMANDS})
            return self._send(200, {"commands": commands.all_commands(root)})

        # ---------------------------------------------------------- 브레인·슬랙 인박스
        if method == "POST" and path == "/api/brain/start":
            if brain.current(conn):
                return self._send(409, {"error": "브레인이 이미 켜져 있습니다"})
            tg, err = brain.start(body.get("model") or None, body.get("permission_mode") or None)
            if not tg:
                return self._send(502, {"error": err})
            LAUNCHED[tg.handle] = store.now()
            BRAIN_LAUNCH.clear()
            BRAIN_LAUNCH.update(handle=tg.handle, terminal=tg.terminal.name, at=store.now())
            return self._send(200, {"ok": True, "handle": tg.handle, "terminal": tg.terminal.name, "label": tg.terminal.label})
        if path == "/api/brain/launch" and method == "GET":
            # 띄운 브레인이 세션으로 잡히기 전: 확인 창(선택지)·뜨는 중·입력칸까지 뜸(곧 잡힘)·탭이 닫힘
            if brain.current(conn):
                BRAIN_LAUNCH.clear()
                return self._send(200, {"state": "running"})
            tg = brain_launch_target()
            if not tg:
                return self._send(200, {"state": "none"})
            if tg.terminal.info(tg.handle) is None:
                BRAIN_LAUNCH.clear()
                return self._send(200, {"state": "gone"})
            # handle·terminal: 보드 터미널·tmux 면 화면이 패널의 창(xterm)으로 그 터미널을 연다
            where = {"handle": tg.handle, "terminal": tg.terminal.name, "label": tg.terminal.label}
            v = focus.prompt_view(tg)
            if v:
                return self._send(200, dict(where, state="prompt", prompt=v))
            if focus.input_line(tg) is not None:
                return self._send(200, dict(where, state="ready"))
            return self._send(200, dict(where, state="starting", screen=focus.dialog_text(tg)))
        if path in ("/api/brain/launch/answer", "/api/brain/launch/focus") and method == "POST":
            tg = brain_launch_target()
            if not tg:
                return self._send(409, {"error": "보드가 띄운 브레인 탭을 찾지 못했습니다 — 다시 시작해 주세요"})
            if path.endswith("/focus"):
                ok = tg.terminal.switch(tg.handle)
                return self._send(200 if ok else 502, {"ok": ok, "message": "브레인 터미널로 이동했습니다"} if ok
                                  else {"error": "터미널 탭으로 이동하지 못했습니다"})
            ok, msg = focus.answer_prompt(tg, int(body.get("n") or 0), body.get("label") or "")
            return self._send(200 if ok else 409, {"ok": True, "message": msg} if ok else {"error": msg})
        if method == "POST" and path == "/api/brain/settings":
            every, err = config.validate("brain_slack_every", body.get("slack_every") or 0)
            if err:
                return self._send(400, {"error": err})
            store.set_setting(conn, "brain_slack_every", every)
            return self._send(200, {"ok": True})
        if path == "/api/setup" and method == "GET":
            agentsetup.forget()
            return self._send(200, {"status": agentsetup.cached(), "issues": agentsetup.issues(conn)})
        m = re.fullmatch(r"/api/setup/(claude|codex)", path)
        if m and method == "POST":
            # ~/.claude/settings.json·~/.codex/hooks.json 을 고친다 — 보드 화면(같은 출처·토큰)에서 사용자가 누른 것만
            if not self._term_ok(self.headers.get("X-Argus-Term") or ""):
                return self._send(403, {"error": "보드 설정 창에서만 바꿀 수 있습니다"})
            ok, msg = agentsetup.install_claude() if m.group(1) == "claude" else agentsetup.install_codex()
            return self._send(200 if ok else 409, {"ok": True, "message": msg} if ok else {"error": msg})
        if path == "/api/skills" and method == "GET":
            return self._send(200, {"items": skills.listing(), "codex": skills.codex_available()})
        m = re.fullmatch(r"/api/skills/([\w\-]+)/(claude|codex)", path)
        if m and method == "POST":
            # 홈 폴더(~/.claude/skills·~/.codex/skills)에 파일을 만들거나 지운다 — 보드 화면(같은 출처·토큰)에서 사용자가 누른 것만.
            # 세션이 ./argus 로 부르거나 다른 사이트가 보내는 것은 막는다
            if not self._term_ok(self.headers.get("X-Argus-Term") or ""):
                return self._send(403, {"error": "보드 설정 창에서만 바꿀 수 있습니다"})
            try:
                fn = skills.remove if (body or {}).get("action") == "remove" else skills.install
                ok, msg = fn(m.group(1), m.group(2))
            except OSError as e:
                ok, msg = False, "실패: %s" % e
            return self._send(200 if ok else 409, {"ok": ok, "message": msg} if ok else {"error": msg})
        if path == "/api/config" and method == "GET":
            return self._send(200, {"items": config.describe(conn)})
        if path == "/api/config" and method == "POST":
            # 설정 창에서 바꾼 값들. 하나라도 틀리면 아무것도 바꾸지 않는다
            vals = {}
            for k, v in (body or {}).items():
                if k == "from":   # 브레인(./argus settings)이 붙이는 요청한 세션 — 설정 값이 아니다
                    continue
                val, err = config.validate(k, v)
                if err:
                    return self._send(400, {"error": err})
                vals[k] = val
            for k, v in vals.items():
                store.set_setting(conn, k, v)
            return self._send(200, {"ok": True, "items": config.describe(conn)})
        if path == "/api/inbox" and method == "GET":
            # 할 일(new)·진행 중(doing·sent)은 모두, 완료(done·ignored)는 설정한 기간 안의 것만
            rows = conn.execute("SELECT i.*, (SELECT COUNT(*) FROM inbox_log l WHERE l.inbox_id=i.id) AS log_count,"
                                " (SELECT title FROM sessions s WHERE s.session_id=i.delegated_session) AS delegated_title"
                                " FROM inbox i WHERE status IN ('new','sent','doing','reply') OR updated_at >= ?"
                                " ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'doing' THEN 1 WHEN 'sent' THEN 1 WHEN 'reply' THEN 1 ELSE 2 END,"
                                " slack_at DESC LIMIT 300",
                                (store.now() - config.get(conn, "done_cards_days") * 86400,)).fetchall()
            items = [dict(r) for r in rows]
            for x in items:   # 링크 없이 만든 슬랙 카드 — key 로 만들어 채워 둔다(「원문 ↗」)
                if (x.get("source") or "slack") == "slack" and not x.get("permalink"):
                    link = slack_link(conn, x["key"], x.get("thread_key"))
                    if link:
                        x["permalink"] = link
                        conn.execute("UPDATE inbox SET permalink=? WHERE id=? AND COALESCE(permalink, '')=''", (link, x["id"]))
            return self._send(200, {"items": items, "checked_at": store.get_setting(conn, "brain_slack_checked_at", 0)})
        if path == "/api/inbox" and method == "POST":
            # 브레인이 ./argus inbox add 로 남기는 카드. 같은 메시지(key)는 한 번만 — replace 면 내용(분류)만 고친다
            # 종류(recommend.type): me(내가 할 일) · session(그 세션에 맡길 일) · new(새 세션) · none(예전 카드)
            key = (body.get("key") or "").strip()[:200]
            summary = (body.get("summary") or "").strip()
            if not key or not summary:
                return self._send(400, {"error": "key·summary 가 필요합니다"})
            rec = body.get("recommend") or {}
            rtype = rec.get("type") if rec.get("type") in ("me", "session", "new", "none") else "me"
            t = store.now()
            vals = {"slack_at": store.parse_ts(body.get("slack_at")) or t, "channel": (body.get("channel") or "")[:200],
                    "author": (body.get("author") or "")[:200], "permalink": (body.get("permalink") or "")[:500],
                    "summary": store.mask(summary)[:2000],
                    "urgency": body.get("urgency") if body.get("urgency") in ("high", "normal", "low") else "normal",
                    "sensitive": 1 if body.get("sensitive") else 0, "rec_type": rtype,
                    "rec_session": (rec.get("session_id") or "")[:80] or None, "rec_folder": (rec.get("folder") or "")[:500] or None,
                    "rec_model": (rec.get("model") or "")[:20] or None, "rec_reason": store.mask(rec.get("reason") or "")[:500],
                    "prompt": strip_slack_links(store.mask(body.get("prompt") or ""))[:8000], "source": (body.get("source") or "slack")[:20],
                    "todo": store.mask(body.get("todo") or "")[:500], "reply_draft": store.mask(body.get("reply_draft") or "")[:4000],
                    "updated_at": t}
            vals["thread_key"] = store.thread_key(vals["permalink"], vals["channel"], vals["slack_at"])
            if body.get("replace"):
                cur = conn.execute("UPDATE inbox SET " + ", ".join(k + "=?" for k in vals) + " WHERE key=?",
                                   list(vals.values()) + [key])
                if cur.rowcount:
                    rid = conn.execute("SELECT id FROM inbox WHERE key=?", (key,)).fetchone()[0]
                    store.card_log(conn, rid, "update", "브레인이 내용을 고침 — " + vals["summary"][:300])
                    return self._send(200, {"ok": True, "added": False, "replaced": True})
            cols = ["key", "status", "created_at"] + list(vals)
            cur = conn.execute("INSERT OR IGNORE INTO inbox (" + ", ".join(cols) + ") VALUES (" + ",".join("?" * len(cols)) + ")",
                               [key, "new", t] + list(vals.values()))
            if cur.rowcount:
                store.card_log(conn, cur.lastrowid, "create", "브레인이 카드를 만듦 — " + vals["summary"][:300])
            return self._send(200, {"ok": True, "added": cur.rowcount > 0})
        m = re.fullmatch(r"/api/inbox/(\d+)", path)
        if m and method == "PATCH":
            # 상태 바꾸기, 또는 카드 대화에서 고른 글로 답장 초안·할 일 덮어쓰기
            fields = {}
            if "status" in body:
                if body["status"] not in ("new", "doing", "sent", "reply", "done", "ignored"):
                    return self._send(400, {"error": "status"})
                fields["status"] = body["status"]
            for k, limit in (("reply_draft", 4000), ("todo", 500)):
                if k in body:
                    fields[k] = store.mask(str(body[k] or "")).strip()[:limit] or None
            iid = int(m.group(1))
            picked = None
            if "rec_session" in body:
                # 브레인이 고른 세션이 아닐 때: 맡기기·입력창에·넥스트 액션에 보낼 세션을 사용자가 바꾼다
                picked = session_row(conn, str(body["rec_session"] or ""))
                if picked is None or picked["state"] == "ended":
                    return self._send(400, {"error": "그 세션이 없거나 종료됐습니다"})
                fields["rec_session"] = picked["session_id"]
                fields["rec_reason"] = None   # 브레인이 적은 추천 이유는 원래 세션 이야기라 지운다
                card = conn.execute("SELECT rec_type FROM inbox WHERE id=?", (iid,)).fetchone()
                if card and card["rec_type"] == "new":
                    fields["rec_type"] = "session"   # 새 세션 추천이었으면 이제 그 세션 작업
            if not fields:
                return self._send(400, {"error": "바꿀 내용이 없습니다"})
            prev = conn.execute("SELECT status FROM inbox WHERE id=?", (iid,)).fetchone()
            conn.execute("UPDATE inbox SET %s, updated_at=? WHERE id=?" % ", ".join(k + "=?" for k in fields),
                         (*fields.values(), store.now(), iid))
            names = INBOX_STATUS_NAMES
            if "status" in fields and prev and prev[0] != fields["status"]:
                store.card_log(conn, iid, "status", "%s → %s" % (names.get(prev[0], prev[0]), names.get(fields["status"], fields["status"])))
                if fields["status"] in ("done", "ignored", "new"):   # 직접 끝내거나 되돌리면 맡기기 지켜보기도 멈춘다
                    conn.execute("UPDATE inbox SET stage=CASE WHEN stage IS NULL THEN NULL ELSE 'closed' END WHERE id=?", (iid,))
            for k, label in (("reply_draft", "답장 초안"), ("todo", "할 일")):
                if k in fields:
                    store.card_log(conn, iid, "edit", "%s을 고침(카드 대화): %s" % (label, (fields[k] or "")[:300]))
            if picked:
                store.card_log(conn, iid, "edit", "맡길 세션을 바꿈: %s" % (picked["title"] or picked["session_id"][:8]),
                               session_id=picked["session_id"])
            return self._send(200, {"ok": True})
        if path == "/api/inbox/status" and method == "POST":
            # 칸반에서 묶음(같은 스레드)을 끌어 옮길 때: 카드 여러 장의 상태를 한 번에
            status = body.get("status")
            ids = [int(x) for x in (body.get("ids") or [])[:200] if str(x).isdigit()]
            if status not in ("new", "doing", "sent", "reply", "done", "ignored") or not ids:
                return self._send(400, {"error": "status·ids"})
            marks = ",".join("?" * len(ids))
            prev = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM inbox WHERE id IN (%s)" % marks, ids)}
            changed = [i for i in ids if i in prev and prev[i] != status]
            if changed:
                cm = ",".join("?" * len(changed))
                conn.execute("UPDATE inbox SET status=?, updated_at=? WHERE id IN (%s)" % cm, (status, store.now(), *changed))
                for i in changed:
                    store.card_log(conn, i, "status", "%s → %s" % (INBOX_STATUS_NAMES.get(prev[i], prev[i]),
                                                                    INBOX_STATUS_NAMES.get(status, status)))
                if status in ("done", "ignored", "new"):   # 직접 끝내거나 되돌리면 맡기기 지켜보기도 멈춘다
                    conn.execute("UPDATE inbox SET stage=CASE WHEN stage IS NULL THEN NULL ELSE 'closed' END WHERE id IN (%s)" % cm,
                                 changed)
            return self._send(200, {"ok": True, "changed": len(changed)})
        m = re.fullmatch(r"/api/inbox/(\d+)/log", path)
        if m and method == "GET":
            rows = conn.execute("SELECT at, kind, text, session_id FROM inbox_log WHERE inbox_id=? ORDER BY at, id",
                                (int(m.group(1)),)).fetchall()
            return self._send(200, {"items": [dict(r) for r in rows]})
        m = re.fullmatch(r"/api/inbox/(\d+)/delegate", path)
        if m and method == "POST":
            # 카드를 세션에 맡긴다: 그 세션에 프롬프트를 보내고, 끝나면 delegate 가 결과 요약·답장 초안을 카드에 넣는다
            card = conn.execute("SELECT * FROM inbox WHERE id=?", (int(m.group(1)),)).fetchone()
            sess = session_row(conn, body.get("session_id") or "")
            prompt = CONTROL_CHARS.sub("", (body.get("prompt") or "").replace("\r\n", "\n")).strip()
            if not card or not sess:
                return self._send(404, {"error": "카드나 세션이 없습니다"})
            if not prompt:
                return self._send(400, {"error": "보낼 프롬프트가 없습니다"})
            if focus.bang(prompt):
                return self._send(400, {"error": focus.BANG_ERROR})
            if sess["state"] in ("waiting", "ended"):
                return self._send(409, {"error": "그 세션이 확인 필요·종료 상태라 맡길 수 없습니다"})
            why = brain_guard(conn, body, sess["session_id"])   # 브레인 자동 정리 중에는 맡기지 못한다
            if why:
                return self._send(403, {"error": why})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409, {"error": err})
            box = focus.dialog_text(handle)
            if box:
                return self._send(409, {"error": "그 터미널에 선택 창이 떠 있습니다: " + box})
            ok, err = focus.type_text(handle, prompt, []) if "\n" in prompt else focus.send_prompt(handle, prompt)[::2]
            if not ok:
                return self._send(502, {"error": "전송 실패: " + (err or "")})
            delegate.start(conn, dict(card), sess, prompt)
            return self._send(200, {"ok": True, "message": "「%s」 세션에 맡겼습니다 · 끝나면 결과와 답장 초안을 카드에 넣습니다" %
                                    (sess.get("title") or sess["session_id"][:8])})
        if path == "/api/inbox/run" and method == "POST":
            # 「지금 정리」 — 브레인 대화가 아니라 정리 일꾼으로(결과는 인박스 맨 위 「정리 결과」)
            rid = inboxrun.start("manual")
            return self._send(200, {"ok": True, "started": bool(rid), "message": "정리를 시작했습니다" if rid else "이미 정리하는 중입니다"})
        if path == "/api/locate" and method == "POST":
            # Finder 에서 끌어 놓은 파일의 원래 경로(브라우저는 이름·크기·수정 시각만 준다) — 그 세션 폴더부터 찾는다(csl/locate.py)
            files = body.get("files") if isinstance(body.get("files"), list) else []
            row = conn.execute("SELECT worktree, cwd, project_root FROM sessions WHERE session_id=?", (body.get("session_id") or "",)).fetchone()
            roots = [row["worktree"], row["project_root"], row["cwd"]] if row else []
            extra = [r[0] for r in conn.execute("SELECT COALESCE(NULLIF(worktree, ''), project_root) FROM sessions WHERE project_root IS NOT NULL"
                                                " GROUP BY 1 ORDER BY MAX(last_activity) DESC LIMIT 15")]
            conn.commit()   # 찾는 몇 초 동안 읽기 트랜잭션을 쥐지 않게
            return self._send(200, {"results": locate.locate([f for f in files if isinstance(f, dict)], roots, extra)})
        if path == "/api/inbox/runs" and method == "GET":
            limit = max(1, min(int((query.get("limit") or ["10"])[0]), 50))
            return self._send(200, {"runs": inboxrun.runs(conn, limit),
                                    "idle_at": store.get_setting(conn, "inbox_auto_idle_at", 0)})   # 자동 정리가 새 항목이 없어 건너뛴 마지막 시각
        m = re.fullmatch(r"/api/inbox/(\d+)/files", path)
        if m and method == "GET":
            # 카드 원문(과 그 스레드)의 첨부 파일 — 슬랙 커넥터로 그때 읽는다(카드엔 요약만 남겨서)
            card = conn.execute("SELECT key, thread_key FROM inbox WHERE id=?", (int(m.group(1)),)).fetchone()
            km = _SLACK_KEY.search(card["key"] or "") if card else None
            if not km:
                return self._send(400, {"error": "슬랙 메시지 카드가 아닙니다"})
            ch, ts = km.groups()
            root = _thread_root(card["thread_key"])
            name, res = connectors.followups(conn, ch, root or ts, root, include_self=True)
            if name is None:
                return self._send(502, {"error": "원문을 읽지 못했습니다: %s" % res})
            files = [dict(f, who=x["who"], at=x["ts"]) for x in res for f in x.get("files") or []]
            return self._send(200, {"files": files, "link": slack_link(conn, card["key"], card["thread_key"])})
        m = re.fullmatch(r"/api/inbox/(\d+)/chat", path)
        if m:   # 카드 대화(csl/cardchat.py)
            iid = int(m.group(1))
            if method == "GET":
                return self._send(200, cardchat.messages(conn, iid))
            if method == "POST":
                text = CONTROL_CHARS.sub("", (body.get("text") or "")).strip()
                if not text or len(text) > 8000:
                    return self._send(400, {"error": "질문을 적어 주세요(8천 자 이하)"})
                ok, err = cardchat.ask(conn, iid, text, body.get("model") or None,
                                       body.get("effort") if "effort" in body else None)
                return self._send(200 if ok else 409, {"ok": True} if ok else {"error": err})
            if method == "DELETE":
                if not cardchat.clear(conn, iid):
                    return self._send(409, {"error": "답을 받는 중에는 지울 수 없습니다"})
                return self._send(200, {"ok": True})
        # ---------------------------------------------------------- 커넥터(csl/connectors)
        if path == "/api/feed" and method == "GET":
            # 커넥터가 모은 항목(아직 카드로 안 만든 것). 브레인이 ./argus inbox raw 로 읽는다
            rows = conn.execute("SELECT key, instance, source, account, kind, title, author, text, link, ts FROM feed_items"
                                " WHERE done=0 ORDER BY ts LIMIT 300").fetchall()
            return self._send(200, {"items": [dict(r) for r in rows], **connectors.describe(conn)})
        if path == "/api/feed/done" and method == "POST":
            keys = body.get("keys") or []
            q = "UPDATE feed_items SET done=1, text=NULL"
            if body.get("all"):
                conn.execute(q + " WHERE done=0")
            elif keys:
                conn.executemany(q + " WHERE key=?", [(k,) for k in keys[:500]])
            return self._send(200, {"ok": True})
        if path == "/api/git-report" and method == "GET":
            # 놓친 git 작업(커밋·푸시·PR) — 브레인의 ./argus git, 하루 정리. 5분 캐시, refresh=1 이면 새로
            data = gitreport.report(conn, refresh=(query.get("refresh") or [""])[0] == "1")
            return self._send(200, {"text": gitreport.text(data), **data})
        if path == "/api/notes-report" and method == "GET":
            # 세션마다 남은(완료 안 된) 넥스트 액션 — 브레인의 ./argus notes, 하루 정리
            groups = notesreport.collect(conn)
            return self._send(200, {"text": notesreport.text(groups), "groups": groups})
        if path == "/api/events" and method == "GET":
            # 활동(세션들의 새 응답·확인 필요·질문·오류…) — 활동 화면과 ./argus events
            kinds = [k for k in ((query.get("kind") or [""])[0]).split(",") if k in store.EVENT_KINDS]
            try:
                before = float((query.get("before") or ["0"])[0]) or None
                since = float((query.get("since") or ["0"])[0]) or None
                limit = max(1, min(int((query.get("limit") or ["200"])[0]), 500))
            except ValueError:
                return self._send(400, {"error": "숫자 형식"})
            items = list_events(conn, kinds or None, (query.get("unseen") or [""])[0] == "1",
                                (query.get("project") or [""])[0] or None, before, limit, since)
            return self._send(200, {"items": items})
        if path == "/api/events/seen-all" and method == "POST":
            # 활동의 「모두 읽음」: 안 본 활동이 있는 세션을 모두 본 것으로(보드의 「새 응답」 표시도 함께 풀린다)
            t = store.now()
            q = "UPDATE sessions SET last_seen_at=? WHERE COALESCE(last_seen_at, 0) < ?"
            args = [t, t]
            if body.get("project"):
                q += " AND COALESCE(project_root, cwd) = ?"
                args.append(body["project"])
            n = conn.execute(q, args).rowcount
            return self._send(200, {"ok": True, "sessions": n})
        if path == "/api/inbox/followups" and method == "GET":
            # 슬랙 메시지 뒤에 이어진 대화(내 답장 포함) — 브레인의 ./argus inbox thread
            channel = (query.get("channel") or [""])[0]
            ts = (query.get("ts") or [""])[0]
            thread_ts = (query.get("thread_ts") or [None])[0]
            if not re.fullmatch(r"[A-Z0-9]+", channel) or not re.fullmatch(r"\d+\.\d+", ts) or \
                    (thread_ts and not re.fullmatch(r"\d+\.\d+", thread_ts)):
                return self._send(400, {"error": "channel·ts 형식"})
            name, res = connectors.followups(conn, channel, ts, thread_ts)
            if name is None:
                return self._send(502, {"error": res})
            for m in res:
                m["text"] = store.mask(m["text"])
            return self._send(200, {"connector": name, "messages": res})
        if path == "/api/calendar" and method == "GET":
            # 정보 전용 캘린더 커넥터에 일정을 묻는다(브레인의 ./argus calendar). start·end 는 유닉스 시각
            start = float((query.get("start") or [store.now()])[0])
            end = float((query.get("end") or [start + 86400])[0])
            try:
                res = connectors.query(conn, "calendar", start, min(end, start + 62 * 86400))
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": str(e)[:300]})
            return self._send(200, {"calendars": [{"name": n, "events": ev} for n, ev in res]})
        if path == "/api/connectors" and method == "GET":
            return self._send(200, connectors.describe(conn))
        if path == "/api/connectors" and method == "POST":
            try:
                inst, missing = connectors.upsert(conn, body or {})
            except (ValueError, RuntimeError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True, "id": inst["id"], "missing": missing})
        m = re.fullmatch(r"/api/connectors/([\w\-]+)(?:/(test|poll))?", path)
        if m and method == "DELETE" and not m.group(2):
            return self._send(200 if connectors.remove(conn, m.group(1)) else 404, {"ok": True})
        if m and method == "POST" and m.group(2):
            try:
                if m.group(2) == "test":
                    return self._send(200, {"ok": True, "message": connectors.test(conn, m.group(1))})
                n = connectors.poll_now(conn, m.group(1))
                return self._send(200, {"ok": True, "added": n})
            except Exception as e:  # noqa: BLE001 - 바깥 서비스 오류를 그대로 보여 준다
                return self._send(502, {"error": str(e)[:300]})
        if path == "/api/inbox/meta" and method == "GET":
            keys = [r[0] for r in conn.execute("SELECT key FROM inbox WHERE created_at >= ?", (store.now() - 14 * 86400,))]
            return self._send(200, {"checked_at": store.get_setting(conn, "brain_slack_checked_at", 0), "keys": keys})
        if path == "/api/inbox/checked" and method == "POST":
            prev = store.get_setting(conn, "brain_slack_checked_at", 0)
            store.set_setting(conn, "brain_slack_checked_at", store.now())
            return self._send(200, {"ok": True, "previous": prev})

        if method == "POST" and path == "/api/sessions/new":
            # 새 Claude Code 세션: 그 폴더에서 claude 를 띄운 터미널(기본 터미널)을 만든다. 세션은 훅이 기록한다
            cwd = (body.get("cwd") or "").strip()
            if not cwd:
                return self._send(400, {"error": "폴더를 고르세요"})
            why = brain_guard(conn, body, "")
            if why:
                return self._send(403, {"error": why})
            if body.get("agent") == "codex":
                # 새 Codex 세션 — 보드 터미널에 codex 를 띄운다. 세션은 훅(있으면)이나 Codex 기록 동기화가 찾아 칸에 잇는다
                prompt = CONTROL_CHARS.sub("", (body.get("prompt") or "").replace("\r\n", "\n")).strip()[:20000]
                tg, err = codex_launch(conn, cwd, (body.get("model") or "").strip() or None, body.get("effort") or None,
                                       prompt=prompt or None)
                if not tg:
                    return self._send(502, {"error": err})
                return self._send(200, {"ok": True, "handle": tg.handle, "terminal": tg.terminal.name, "label": tg.terminal.label})
            # 정하지 않았으면 설정의 기본값(새 세션 기본 effort) — 브레인 ./argus new 처럼 effort 없이 여는 경우
            effort = body.get("effort") or config.get(conn, "new_default_effort") or None
            if effort and effort not in config.EFFORTS:
                return self._send(400, {"error": "모르는 effort 입니다"})
            extra = ["--effort " + effort] if effort else []
            if body.get("worktree"):
                # 새 워크트리(claude --worktree): 그 폴더가 git 저장소여야 한다
                try:
                    git_ok = subprocess.run(["git", "-C", os.path.expanduser(cwd), "rev-parse", "--is-inside-work-tree"],
                                            capture_output=True, text=True, timeout=10).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    git_ok = False
                if not git_ok:
                    return self._send(400, {"error": "git 저장소가 아니라 새 워크트리를 만들 수 없습니다 — 「새 워크트리로 시작」을 끄고 다시 해 주세요"})
                extra.append("--worktree")
            # 모델을 아예 안 보냈으면(브레인 ./argus new) 설정의 새 세션 기본 모델. 보드 창은 고른 값을 늘 보낸다("" = Claude Code 설정대로)
            model = body["model"] if "model" in body else config.get(conn, "new_default_model")
            # terminal: "web" 이면 보드 터미널(칸 안의 xterm.js)에. 없으면 기본 터미널(Orca 등)
            # 새 세션은 늘 보드 터미널(보드 전용 tmux 서버 — 칸 안에서 터미널을 연다)에 띄운다
            tg, err = focus.new_claude_terminal(cwd, model or None, extra=" ".join(extra), terminal="web")
            if not tg:
                return self._send(502, {"error": err})
            LAUNCHED[tg.handle] = store.now()
            return self._send(200, {"ok": True, "handle": tg.handle, "terminal": tg.terminal.name, "label": tg.terminal.label})
        if path == "/api/boards" or path.startswith("/api/boards/"):
            return self._boards(conn, method, path, body, query)
        if path == "/api/shells" or path.startswith("/api/shells/"):
            # 오른쪽 터미널 드로어의 셸 탭: 브라우저에서 내 계정으로 아무 명령이나 실행하는 길이라
            # 같은 출처(Origin)·토큰(X-Argus-Term)을 늘 요구한다 — 다른 사이트가 몰래 셸을 띄우거나 닫지 못하게
            if not self._term_ok(self.headers.get("X-Argus-Term") or "", need_origin=method != "GET"):
                return self._send(403, {"error": "forbidden"})
            web = terminals.registry().get("web")
            if not web or not web.available():
                return self._send(503, {"error": "tmux 가 없어 터미널을 열 수 없습니다(brew install tmux)"})
            if method == "GET" and path == "/api/shells":
                return self._send(200, {"shells": web.shells()})
            if method == "POST" and path == "/api/shells":
                cwd = body.get("cwd")
                name = web.new_shell(os.path.expanduser(cwd) if isinstance(cwd, str) and cwd else None)
                if not name:
                    return self._send(502, {"error": "터미널을 열지 못했습니다"})
                return self._send(200, {"ok": True, "name": name})
            m = re.fullmatch(r"/api/shells/([\w\-]+)", path)
            if method == "DELETE" and m:
                if not web.kill_shell(m.group(1)):
                    return self._send(404, {"error": "그 셸 탭이 없습니다"})
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        if method == "GET" and path == "/api/update":
            # 업데이트 확인: 원격(origin)에 받을 커밋·새 릴리스 태그가 있는지. fetch=0 이면 원격을 가져오지 않고 지금 아는 것만
            return self._send(200, update.check(fetch=(query.get("fetch") or ["1"])[0] != "0"))
        if method == "POST" and path == "/api/update":
            # 업데이트 받기(fast-forward 만). 받았으면 응답을 보낸 뒤 보드 서버를 새 코드로 다시 띄운다
            if body.get("from"):
                return self._send(403, {"error": "업데이트는 보드 화면에서만 받을 수 있습니다"})
            ok, msg, n = update.apply()
            if ok:
                threading.Timer(1.0, restart_server).start()
            return self._send(200 if ok else 409, {"ok": True, "message": msg, "commits": n} if ok else {"error": msg})
        if method == "GET" and path == "/api/term/token":
            # 보드 터미널 웹소켓 토큰(서버를 띄울 때마다 새로). Host 검사를 지난 같은 출처 화면만 읽을 수 있다(CORS 없음)
            return self._send(200, {"token": webterm.TOKEN, "available": bool(tmux_term._bin())})
        if method == "GET" and path == "/api/folders":
            # 새 세션 폴더 후보: 터미널(Orca 등)에 열린 프로젝트 먼저, 그다음 최근 세션의 프로젝트(워크트리) 폴더
            projects = focus.open_projects()
            seen, recent = {p["path"] for p in projects}, []
            for r in conn.execute("SELECT COALESCE(worktree, project_root, cwd) AS p, MAX(last_activity) AS t FROM sessions"
                                  " WHERE COALESCE(worktree, project_root, cwd) IS NOT NULL GROUP BY p ORDER BY t DESC LIMIT 40"):
                p = r["p"]
                if p not in seen and os.path.isdir(p) and "/.claude/jobs/" not in p \
                        and "/Library/Application Support/Claude/" not in p:   # 백그라운드 작업·데스크톱 앱 임시 폴더 제외
                    seen.add(p)
                    recent.append(p)
            return self._send(200, {"projects": projects, "recent": recent[:12]})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/ask-check", path)
        if m and method == "POST":
            # 질문 카드가 떠 있는 칸이 몇 초마다 묻는다: 터미널에 그 창이 정말 있는지 — 없으면 지운다
            return self._send(200, {"cleared": clear_stale_ask(conn, m.group(1))})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/answer", path)
        if m and method == "POST":
            # 터미널의 질문 창(AskUserQuestion)에 보드에서 고른 답을 넣는다. 화면을 확인하며 한 단계씩, 어긋나면 멈춤
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            try:
                ask = json.loads(sess["ask"] or "null")
            except ValueError:
                ask = None
            if not ask or sess["state"] != "waiting":
                return self._send(409, {"error": "지금 답할 질문이 없습니다"})
            if (body.get("id") or None) != ask.get("id"):
                return self._send(409, {"error": "질문이 바뀌었습니다 — 새로 고쳐 주세요"})
            if clear_stale_ask(conn, sess["session_id"], min_age=0):
                return self._send(409, {"error": STALE_ASK, "cleared": True})
            if body.get("chat"):   # 「Chat about this」 — 답하지 않고 대화로
                handle, err = terminal_target(conn, sess)
                if not handle:
                    return self._send(409, {"error": err})
                ok, msg = focus.decline_ask(handle, ask["questions"])
                return self._send(200 if ok else 409, {"ok": True, "message": msg} if ok else {"error": msg})
            questions, answers = ask["questions"], body.get("answers") or []
            if len(answers) != len(questions):
                return self._send(400, {"error": "모든 질문에 답해 주세요"})
            for q, a in zip(questions, answers):
                ch, other = a.get("choices") or [], (a.get("other") or "").strip()
                if any(o.get("preview") for o in q.get("options") or []):
                    return self._send(409, {"error": "미리보기가 있는 질문은 터미널에서 답해 주세요"})
                if not ch and not other:
                    return self._send(400, {"error": "답하지 않은 질문이 있습니다"})
                if not q.get("multiSelect") and len(ch) + bool(other) != 1:
                    return self._send(400, {"error": "「%s」는 하나만 고를 수 있습니다" % q.get("header", "")})
                if len(other) > 2000:
                    return self._send(400, {"error": "직접 입력이 너무 깁니다"})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409, {"error": err})
            ok, msg = focus.answer_ask(handle, questions, answers)
            return self._send(200 if ok else 502, {"ok": ok, "message": msg} if ok else {"error": msg})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/suggestion", path)
        if m and method == "GET":
            # 응답 뒤 터미널 입력칸에 보이는 글(Claude Code 의 다음 요청 제안). 쉬고 있는 세션만, 기록된 Orca 탭만 읽는다
            sess = session_row(conn, m.group(1))
            if sess is None or sess["state"] in ("working", "waiting", "ended"):
                return self._send(200, {"text": None})
            return self._send(200, {"text": focus.input_line(focus.target_of(sess)) or None})

        m = re.fullmatch(r"/api/upload/([\w\-]+\.(png|jpg|gif|webp))", path)
        if m and method == "GET":
            # 넥스트 액션 썸네일용. uploads/ 안의 이미지 파일만
            full = os.path.join(UPLOAD_DIR, m.group(1))
            if not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            ctype = {"jpg": "image/jpeg"}.get(m.group(2), "image/" + m.group(2))
            return self._send(200, data, ctype)

        if method == "GET" and path == "/api/sent-file":
            # Claude 가 보낸(SendUserFile)·응답에 적은 이미지 미리보기. 응답이나 중간 메시지에 나온 경로만 연다(임의 파일 읽기 막기)
            raw = (query.get("path") or [""])[0]
            fp = os.path.expanduser(raw)
            ext = os.path.splitext(fp)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                     ".webp": "image/webp"}.get(ext)
            if not ctype or not os.path.isabs(fp) or not os.path.isfile(fp):
                return self._send(404, {"error": "not found"})
            if not conn.execute("SELECT 1 FROM turns WHERE response LIKE ? ESCAPE '\\' OR extras LIKE ? ESCAPE '\\' LIMIT 1",
                                (_like(raw), "%" + _like(json.dumps(raw, ensure_ascii=False)[1:-1]) + "%")).fetchone():
                return self._send(403, {"error": "대화에 나온 이미지가 아닙니다"})
            with open(fp, "rb") as f:
                return self._send(200, f.read(), ctype)

        if method == "GET" and path == "/api/state":
            days = float((query.get("days") or [config.get(conn, "board_days")])[0])
            show_archived = (query.get("archived") or ["0"])[0] == "1"
            return self._send(200, list_state(conn, days, show_archived))

        if path == "/api/daily" and method == "GET":
            day = (query.get("day") or [""])[0]
            if day:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    return self._send(400, {"error": "day 는 YYYY-MM-DD"})
                return self._send(200, daily.get(conn, day))
            return self._send(200, {"days": daily.days(conn)})
        if path == "/api/daily" and method == "POST":
            day = body.get("day") or ""
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return self._send(400, {"error": "day 는 YYYY-MM-DD"})
            started = daily.start(day)
            return self._send(200, {"ok": True, "started": started})

        if method == "GET" and path == "/api/search":
            q = (query.get("q") or [""])[0].strip()[:200]
            sid = (query.get("sid") or [None])[0]
            who = (query.get("who") or ["all"])[0]
            if who not in ("all", "me", "claude"):
                who = "all"
            sort = "rank" if (query.get("sort") or [""])[0] == "rank" else "recent"
            project = (query.get("project") or [""])[0].strip()[:100] or None
            try:
                limit = max(1, min(int((query.get("limit") or ["60"])[0]), 200))
            except ValueError:
                limit = 60
            exclude = (query.get("exclude") or [None])[0]
            any_word = (query.get("any") or [""])[0] == "1"
            same = (query.get("same_project") or [None])[0]
            return self._send(200, {"results": search_turns(conn, q, sid, who, limit, sort, project, exclude, any_word,
                                                            same)})

        m = re.fullmatch(r"/api/turns/(\d+)/image/(\d+)", path)
        if m and method == "GET":
            # 요청에 붙인 이미지([Image #N]) — 대화 기록에서 꺼낸다. i 는 요청 글 속 몇 번째 [Image #…] 인지(번호가 안 맞을 때 순서로)
            row = conn.execute("SELECT t.prompt_id, s.transcript_path FROM turns t JOIN sessions s ON s.session_id = t.session_id"
                               " WHERE t.id=?", (int(m.group(1)),)).fetchone()
            imgs = store.prompt_images(row["transcript_path"], row["prompt_id"]) \
                if row and row["prompt_id"] and row["transcript_path"] else {}
            img = imgs.get(int(m.group(2)))
            if img is None:
                try:
                    img = list(imgs.values())[int((query.get("i") or [""])[0])]
                except (ValueError, IndexError):
                    img = None
            if img is None:
                return self._send(404, {"error": "이미지를 찾지 못했습니다"})
            try:
                data = base64.b64decode(img[1])
            except ValueError:
                return self._send(404, {"error": "이미지를 읽지 못했습니다"})
            return self._send(200, data, img[0], cache="private, max-age=86400")

        m = re.fullmatch(r"/api/turns/(\d+)", path)
        if m and method == "GET":   # 턴 하나 전문(+ 앞뒤 턴 around 개) — argus timeline·turn 용
            try:
                around = max(0, min(int((query.get("around") or ["0"])[0]), 20))
            except ValueError:
                around = 0
            d = turn_context(conn, int(m.group(1)), around)
            return self._send(200, d) if d else self._send(404, {"error": "턴 없음"})

        if path == "/api/agents" and method == "GET":
            # 백그라운드 세션(claude agents --json): 상태·이름·폴더. 보드에 기록된 세션이면 그 제목·최근 요청·응답을 붙인다
            items = [x for x in agents_list() if x.get("kind") == "background"]
            # 「목록에서 지우기」가 claude rm 으로 안 지워진 꺼진 기록(백그라운드 서비스가 꺼져 있을 때)은 보드에서만 숨긴다.
            # 다시 살아나면(pid) 보이고, Claude 쪽 기록이 사라지면 숨김 목록에서도 뺀다
            hidden = store.get_setting(conn, "agents_hidden", [])
            if hidden:
                ids = {x.get("id") for x in items}
                if any(h not in ids for h in hidden):
                    hidden = [h for h in hidden if h in ids]
                    store.set_setting(conn, "agents_hidden", hidden)
                items = [x for x in items if x.get("pid") or x.get("id") not in hidden]
            sync_agents(conn, items)
            known = {r["session_id"]: dict(r) for r in conn.execute(
                "SELECT s.session_id, s.title, s.state, s.archived," + LAST_TURN_COLUMNS +
                " FROM sessions s WHERE s.session_id IN (%s)" % ",".join("?" * len(items)),
                [x.get("sessionId") for x in items])} if items else {}
            for x in items:
                x["alive"] = bool(x.get("pid"))
                k = known.get(x.get("sessionId"))
                if k:
                    lp = k["lp_prompt"] or ""
                    k["lp_prompt"] = " ".join((store.notice_line(lp) if store.is_task_notice(lp) else lp).split())[:200]
                    k["lr_response"] = " ".join((k["lr_response"] or "").split())[:200]
                x["board"] = k
            return self._send(200, {"items": items})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/prompt", path)
        if m and method in ("GET", "POST"):
            # 입력칸 대신 뜬 선택 창(권한 확인 등) — 보기·고르기. 질문 창(AskUserQuestion)은 /answer 가 맡는다
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            if sess.get("agent") == "codex":
                # Codex 승인 창(명령 실행·파일 고치기 …) — 같은 🔐 카드로 보이고 고른다
                tg = codex_tab(conn, sess)
                if not tg:
                    return self._send(409 if method == "POST" else 200,
                                      {"error": "Codex 탭을 찾지 못했습니다"} if method == "POST" else {"prompt": None})
                if method == "GET":
                    return self._send(200, {"prompt": codex.approval_view(tg)})
                ok, msg = codex.answer_approval(tg, int(body.get("n") or 0), body.get("label") or "")
                if ok:
                    # 거절하면 Codex 는 그 턴을 끊고 훅을 보내지 않는다 — 입력창이 돌아왔고 일하지 않으면 「확인 필요」를 걷는다
                    time.sleep(1.2)
                    st = "working" if codex.busy(tg) else "done" if codex.ready(tg) and not codex.approval_view(tg) else None
                    if st:
                        conn.execute("UPDATE sessions SET state=?, state_detail=NULL, state_at=? WHERE session_id=? AND state='waiting'",
                                     (st, store.now(), sess["session_id"]))
                return self._send(200 if ok else 409, {"ok": True, "message": msg} if ok else {"error": msg})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409 if method == "POST" else 200, {"error": err} if method == "POST" else {"prompt": None})
            if method == "GET":
                return self._send(200, {"prompt": focus.prompt_view(handle)})
            ok, msg = focus.answer_prompt(handle, int(body.get("n") or 0), body.get("label") or "")
            if ok:
                # 「No」로 거절하면 Claude Code 는 그 턴을 끊고(Interrupted) 훅을 보내지 않는다 — 입력칸이 돌아왔고 일하지 않으면
                # 「확인 필요」를 걷는다(「Yes」면 도구가 돌며 PostToolUse 가 작업 중으로 바꾼다)
                time.sleep(1.2)
                if focus.input_line(handle) is not None and not focus.is_busy(handle):
                    conn.execute("UPDATE sessions SET state='done', state_detail=NULL, state_at=? WHERE session_id=? AND state='waiting'",
                                 (store.now(), sess["session_id"]))
            return self._send(200 if ok else 409, {"ok": True, "message": msg} if ok else {"error": msg})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/quit", path)
        if m and method == "POST":
            return self._send(*self._quit(conn, m.group(1), body))
        m = re.fullmatch(r"/api/agents/([0-9a-f]{8})/(stop|attach|rm)", path)
        if m and method == "POST":
            aid, act = m.groups()
            x = next((a for a in agents_list(fresh=True) if a.get("id") == aid), None)
            if not x:
                return self._send(404, {"error": "그 백그라운드 세션이 없습니다(이미 끝났거나 지워짐)"})
            if act in ("stop", "rm"):
                # rm 은 프로세스가 없는(기록만 남은) 것만 — 목록에서 지운다. 돌고 있는 것은 멈추기로
                if act == "rm" and x.get("pid"):
                    return self._send(409, {"error": "아직 돌고 있습니다 — 먼저 멈춰 주세요"})
                try:
                    r = subprocess.run([daily._claude_bin(), act, aid], capture_output=True, text=True, timeout=20, env=daily._clean_env())
                except (OSError, subprocess.SubprocessError) as e:
                    return self._send(502, {"error": str(e)[:200]})
                AGENTS["at"] = 0
                if r.returncode != 0 and act == "rm":
                    # 꺼진 기록을 claude rm 이 못 지우는 경우(Claude 백그라운드 서비스가 안 떠 있으면 「service may be restarting」) —
                    # 보드 목록에서만 숨긴다. 기록은 ~/.claude/jobs 에 남고 `claude agents` 에서는 계속 보인다
                    hidden = store.get_setting(conn, "agents_hidden", [])
                    store.set_setting(conn, "agents_hidden", sorted(set(hidden) | {aid}))
                    return self._send(200, {"ok": True, "message": "보드 목록에서 숨겼습니다 — Claude 백그라운드 서비스가 꺼져 있어 "
                                            "Claude 쪽 기록은 남았습니다(`claude rm " + aid + "` 로 지울 수 있음)"})
                if r.returncode != 0:
                    return self._send(502, {"error": (r.stderr or r.stdout or "실패").strip()[:300]})
                return self._send(200, {"ok": True, "message": "멈췄습니다(대화는 남아 있어 다시 열 수 있습니다)" if act == "stop"
                                        else "목록에서 지웠습니다"})
            if not x.get("pid"):
                return self._send(409, {"error": "프로세스가 없는 기록입니다(이미 끝남) — 열 수 없습니다. 「목록에서 지우기」로 정리하세요"})
            tg, err = focus.new_claude_terminal(x.get("cwd") or "", extra="attach " + aid, terminal="web")
            if not tg:
                return self._send(502, {"error": err})
            LAUNCHED[tg.handle] = store.now()
            conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?",
                         (json.dumps(tg.terminal.remember({}, tg.handle)), x.get("sessionId")))
            # 새 탭은 포커스를 뺏지 않고 만들어진다 — 「열기」는 보려고 누른 것이니 그 탭을 앞으로
            shown = tg.terminal.switch(tg.handle)
            return self._send(200, {"ok": True, "message": "새 터미널 탭에 붙였습니다" + ("" if shown else "(탭을 앞으로 가져오지 못함 — Orca 에서 확인)")})
        if path == "/api/details" and method == "GET":
            # 여러 칸 대화를 한 번에: ids=<세션>:<턴 수>,<세션>:<턴 수>…(최대 40개)
            want = {}
            for part in ((query.get("ids") or [""])[0]).split(",")[:40]:
                sid, _, n = part.partition(":")
                if re.fullmatch(r"[\w\-]+", sid):
                    want[sid] = max(1, min(int(n or 300), 1000))
            return self._send(200, {"details": sessions_detail(conn, want)})
        if path == "/api/usage/refresh" and method == "POST":
            return self._send(*refresh_usage(conn))
        if path == "/api/activity" and method == "GET":
            # 작업 중인 여러 세션의 진행 과정을 한 번에: items=<세션>:<시작 시각>,…
            want = {}
            for part in ((query.get("items") or [""])[0]).split(",")[:40]:
                sid, _, since = part.partition(":")
                if re.fullmatch(r"[\w\-]+", sid):
                    want[sid] = float(since or 0)
            rows = {r["session_id"]: r for r in conn.execute(
                "SELECT session_id, transcript_path, agent FROM sessions WHERE session_id IN (%s)" % ",".join("?" * len(want)),
                list(want))} if want else {}
            return self._send(200, {"items": {sid: activity_of(rows[sid], want[sid] - 1) for sid in want if sid in rows}})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)", path)
        if m and method == "GET":
            limit = max(1, min(int((query.get("limit") or ["300"])[0]), 1000))
            detail = session_detail(conn, m.group(1), limit)
            return self._send(200, detail) if detail else self._send(404, {"error": "no session"})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/dev-snapshot", path)
        if m and method == "POST" and devlog.enabled():
            # 개발용: 칸 ⋮ → 🐞 지금 상태 저장 — 오류 없이 이상할 때 화면·세션·보드 쪽 상태를 남긴다
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            devlog.start()
            client = (body or {}).get("client")
            name = devlog.record("manual", sess, error=(body or {}).get("note") or None,
                                 text=(client or {}).get("draft") if isinstance(client, dict) else None, client=client)
            return self._send(200 if name else 500, {"ok": bool(name), "path": name} if name else {"error": "저장하지 못했습니다"})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/send", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            why = brain_guard(conn, body, sess["session_id"])
            if why:
                return self._send(403, {"error": why})
            if devlog.enabled():
                devlog.start()
                self._dev = {"sess": sess, "text": (body or {}).get("text") or ""}
            if sess.get("agent") == "codex":
                return self._send(*codex_send(conn, sess, body))
            # 어느 터미널에서도 돌고 있지 않으면(종료·탭을 닫음) 보드가 「새 탭에서 이어 열기」를 물어본다(resume)
            if sess["state"] == "ended":
                return self._send(409, {"error": "종료된 세션입니다",
                                        "resume": sess["session_id"] not in (focus.running_sessions() or {})})
            if not_running(conn, sess["session_id"]):
                return self._send(409, {"error": NOT_RUNNING, "resume": True})
            if sess["state"] == "waiting":
                # 승인·질문 창에 글자가 들어가면 엉뚱한 선택이 될 수 있다. 다만 창을 거절·중단하면 훅이 오지 않아
                # 「확인 필요」가 남는다 — 화면에 입력칸이 보이면(창이 없으면) 지난 상태로 보고 보낸다
                if focus.input_line(focus.target_of(sess)) is None:
                    return self._send(409, {"error": "확인 필요 상태입니다. 터미널에서 승인·답변을 먼저 해 주세요"})
                conn.execute("UPDATE sessions SET state='done', state_detail=NULL, ask=NULL WHERE session_id=?",
                             (sess["session_id"],))
                sess["state"] = "done"
            text = CONTROL_CHARS.sub("", (body.get("text") or "").replace("\r\n", "\n")).strip()
            images = body.get("images") or []
            if not text and not images:
                return self._send(400, {"error": "보낼 내용이 없습니다"})
            if len(text) > 20000:
                return self._send(400, {"error": "메시지가 너무 깁니다(2만 자 이하)"})
            # 「!」(Claude Code 가 셸 명령으로 바로 실행)는 사람이 칸 입력창에서 직접 보낸 것만: 브레인·CLI(from 이 있음)는 안 되고,
            # 보드 화면만 아는 토큰(X-Argus-Term — 터미널과 같은 것)이 있어야 한다. 예약·위임으로 나가는 글은 다른 길이라 계속 막힌다
            direct = not body.get("from") and webterm.token_ok(self.headers.get("X-Argus-Term") or "")
            if focus.bang(text) and not direct:
                return self._send(400, {"error": focus.BANG_ERROR})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409, {"error": err})
            box = focus.dialog_text(handle)
            if box and focus._btw_view(focus._screen(handle), "")[1] is not None:
                focus._raw(handle, focus.ESC)   # 지난 곁질문(/btw) 창이 열린 채 남아 있으면 닫는다(목록은 남는다)
                box = focus.dialog_text(handle, wait=0.4)
            if box:   # 훅 없이 뜨는 선택 창(/remote-control 등) — 글이 그 창의 선택으로 들어가지 않게
                return self._send(409, {"error": "터미널에 선택 창이 떠 있어 보내지 않았습니다. 터미널에서 먼저 골라 주세요: " + box})
            try:
                paths = save_images(images)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            # 자동 리모트: 대화한 세션만, 꺼져 있고 아직 한 번도 켜 달라고 보낸 적이 없을 때만.
            # 이미 켜진 세션에 /remote-control 을 다시 보내면 상태 창이 떠서 이어 보내는 글을 가로챈다
            rc_sent = False
            # 브레인·카드 맡기기처럼 다른 세션이 보낸 것(from)은 사용자가 그 세션과 직접 대화한 게 아니니 켜지 않는다
            live = live_remote(sess["session_id"])
            if (store.get_setting(conn, "auto_remote", False) and not body.get("from")
                    and not (live if live is not None else sess["remote"]) and not sess["rc_requested_at"]):
                rc_sent, _, _ = focus.send_prompt(handle, "/remote-control")
                if rc_sent:
                    conn.execute("UPDATE sessions SET rc_requested_at=? WHERE session_id=?", (store.now(), sess["session_id"]))
                    conn.commit()
                    box = focus.dialog_text(handle, wait=1.5)
                    if box:   # 켜기가 사용자 선택을 기다린다 — 이어 보낼 글이 그 창으로 들어가지 않게 멈춘다
                        mark_dialog(conn, sess["session_id"], box)
                        return self._send(409, {"error": "리모트 컨트롤을 켜는 중 확인 창이 떠서 메시지는 보내지 않았습니다. "
                                                         "터미널에서 고른 뒤 다시 보내 주세요: " + box})
            if "\n" in text or paths:
                # 여러 줄·이미지는 타이핑 방식(붙여넣기로 보내면 <pasted_content> 태그로 감싸진다)
                ok, err = focus.type_text(handle, text, paths, allow_bang=direct)
                stages = []
            else:
                ok, stages, err = focus.send_prompt(handle, text, allow_bang=direct)
            if not ok:
                return self._send(502, {"error": "전송 실패: " + err})
            if re.match(r"/compact(\s|$)", text) and sess["state"] != "working":
                # PreCompact 훅이 곧 오지만, 훅을 아직 못 읽은(설정 변경 전에 연) 세션도 바로 보이게
                store.start_compact(conn, sess["session_id"], "manual")
                return self._send(200, {"ok": True, "message": "컨텍스트를 요약하는 중입니다", "stages": stages, "images": 0})
            watch_from_brain(conn, body, sess["session_id"], text)
            btw = re.match(r"/btw\s+(.+)", text, re.S)
            if btw:
                aid = conn.execute("INSERT INTO asides (session_id, question, status, at) VALUES (?,?,?,?)",
                                   (sess["session_id"], store.mask(btw.group(1).strip()), "pending", store.now())).lastrowid
                conn.commit()
                threading.Thread(target=capture_btw, args=(handle, aid, btw.group(1).strip()), daemon=True).start()
                return self._send(200, {"ok": True, "message": "곁질문을 보냈습니다 · 답은 대화에 섞이지 않고 이 칸에 따로 보입니다",
                                        "stages": stages, "images": 0})
            msg = "보냈습니다 · Claude 가 작업을 시작했습니다" if "turn_started" in stages \
                else "보냈습니다" if sess["state"] != "working" else "보냈습니다 · 작업 중이라 지금 단계(도구 실행)가 끝나는 대로 Claude 가 읽고 이번 작업에 반영합니다"
            if rc_sent:
                msg += " · 리모트 컨트롤을 켰습니다"
            return self._send(200, {"ok": True, "message": msg, "stages": stages, "images": len(paths)})

        m = re.fullmatch(r"/api/asides/(\d+)/reread", path)
        if m and method == "POST":
            # 답을 못 읽은 곁질문: 창이 아직 터미널에 떠 있으면 다시 읽는다(창이 늦게 떴거나 읽는 사이 놓친 것)
            a = conn.execute("SELECT * FROM asides WHERE id=?", (int(m.group(1)),)).fetchone()
            sess = a and conn.execute("SELECT * FROM sessions WHERE session_id=?", (a["session_id"],)).fetchone()
            t = sess and focus.target_of(dict(sess))
            if not t:
                return self._send(404, {"error": "그 세션의 터미널을 찾지 못했습니다"})
            conn.execute("UPDATE asides SET status='pending', answer=NULL WHERE id=?", (a["id"],))
            conn.commit()
            threading.Thread(target=capture_btw, args=(t, a["id"], a["question"]), kwargs={"timeout": 30, "find_wait": 6}, daemon=True).start()
            return self._send(200, {"ok": True, "message": "곁질문 창을 다시 읽는 중입니다"})
        if path == "/api/open" and method == "POST":
            target = body.get("path") or ""
            if not isinstance(target, str) or not target.startswith(("/", "~/")):
                return self._send(400, {"error": "절대 경로가 아닙니다"})
            result, how = focus.open_path(target, bool(body.get("reveal")))
            if result == "missing":
                return self._send(404, {"error": "파일이 없습니다(지워졌거나 임시 폴더가 비워졌을 수 있음)"})
            return self._send(200 if result == "ok" else 500, {"ok": result == "ok", "how": how})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/interrupt", path)
        if m and method == "POST":
            # 작업 중인 Claude 를 멈춘다 — 터미널에 Esc(Claude Code 의 중단 키). 승인·질문 창이 떠 있으면 Esc 가 그 창을
            # 취소하게 되므로 보내지 않는다
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            if sess.get("agent") == "codex":
                return self._send(*codex_interrupt(conn, sess))
            if sess["state"] != "working" and not sess.get("compact_at"):
                return self._send(409, {"error": "작업 중이 아닙니다"})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409, {"error": err})
            box = focus.dialog_text(handle)
            if box:
                return self._send(409, {"error": "터미널에 선택 창이 떠 있어 중단하지 않았습니다(Esc 가 그 창을 취소합니다): " + box})
            if not focus._raw(handle, focus.ESC):
                return self._send(502, {"error": "중단 키를 보내지 못했습니다"})
            # 중단은 응답 훅(Stop)이 오지 않을 수 있다 — 화면에 입력칸이 돌아오면 보드에서도 끝난 것으로
            stopped = focus.input_line(handle) is not None and not focus.is_busy(handle)
            for _ in range(8):
                if stopped:
                    break
                time.sleep(0.4)
                stopped = not focus.is_busy(handle)
            back = None
            if stopped:
                # 중단하면 그 요청이 터미널 입력칸에 되돌아온다 — 비우고 보드 입력창으로(그대로 두면 다음에 보드가 보내는
                # 글·/model 등이 그 뒤에 이어 붙어 보내졌다)
                last = conn.execute("SELECT prompt FROM turns WHERE session_id=? AND status='running' AND response_at IS NULL"
                                    " ORDER BY id DESC LIMIT 1", (sess["session_id"],)).fetchone()
                if last and last["prompt"] and focus.take_back(handle, last["prompt"]):
                    back = last["prompt"]
                t = store.now()
                conn.execute("UPDATE turns SET status='interrupted', response=COALESCE(response, '(중단됨)'), response_at=COALESCE(response_at, ?)"
                             " WHERE session_id=? AND response_at IS NULL AND status='running'", (t, sess["session_id"]))
                conn.execute("UPDATE sessions SET state='done', state_detail='중단됨', state_at=?, compact_at=NULL WHERE session_id=?",
                             (t, sess["session_id"]))
                store.add_event(conn, sess["session_id"], "error", "사용자가 중단함")
            return self._send(200, {"ok": True, "stopped": stopped, "prompt": back,
                                    "message": ("중단했습니다" + (" — 보낸 요청을 입력창에 되돌려 놓았습니다" if back else ""))
                                    if stopped else "중단 키를 보냈습니다 — 지금 단계가 정리되는 대로 멈춥니다"})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/remote", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            why = brain_guard(conn, body, sess["session_id"])
            if why:
                return self._send(403, {"error": why})
            live = live_remote(sess["session_id"])
            on = live if live is not None else sess["remote"]
            if body.get("off"):
                # 원격 끄기: /remote-control 로 상태 창을 열고 「Disconnect this session」
                if not on:
                    conn.execute("UPDATE sessions SET remote=NULL, rc_requested_at=NULL WHERE session_id=?", (sess["session_id"],))
                    return self._send(200, {"ok": True, "message": "이미 꺼져 있습니다"})
                if sess["state"] in ("working", "waiting"):
                    return self._send(409, {"error": "작업 중이거나 확인을 기다리는 중입니다 — 끝난 뒤 꺼 주세요"})
                handle, err = terminal_target(conn, sess)
                if not handle:
                    return self._send(409, {"error": err})
                ok, msg = focus.rc_disconnect(handle)
                if ok:
                    conn.execute("UPDATE sessions SET remote=NULL, rc_requested_at=NULL WHERE session_id=?", (sess["session_id"],))
                return self._send(200 if ok else 409, {"ok": True, "message": msg} if ok else {"error": msg})
            if on:
                if not sess["remote"]:
                    conn.execute("UPDATE sessions SET remote=? WHERE session_id=?", (on, sess["session_id"]))
                return self._send(200, {"ok": True, "message": "이미 연결돼 있습니다"})
            if sess["state"] == "waiting":
                return self._send(409, {"error": "확인 필요 상태입니다. 터미널에서 승인·답변을 먼저 해 주세요"})
            if sess["state"] == "ended":
                return self._send(409, {"error": "종료된 세션입니다"})
            if sess["rc_requested_at"]:
                # 보낸 뒤로 훅이 한 번도 안 왔으면 켜졌는지 아직 모른다. 이미 켜진 세션에 다시 보내면 상태 창이 떠서 입력을 가로챈다
                last = conn.execute("SELECT last_activity FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()[0]
                if (last or 0) <= sess["rc_requested_at"]:
                    return self._send(409, {"error": "이미 켜 달라고 보냈습니다 — 다음 응답 때 연결이 확인됩니다"})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409, {"error": err})
            box = focus.dialog_text(handle)
            if box:
                return self._send(409, {"error": "터미널에 선택 창이 떠 있습니다. 터미널에서 먼저 골라 주세요: " + box})
            ok, _, err = focus.send_prompt(handle, "/remote-control")
            if not ok:
                return self._send(502, {"error": "전송 실패: " + err})
            conn.execute("UPDATE sessions SET rc_requested_at=? WHERE session_id=?", (store.now(), sess["session_id"]))
            box = focus.dialog_text(handle, wait=1.5)
            if box:   # 켜기가 사용자 선택(확인 창)을 기다린다 — 보드에 「확인 필요」로 보여 주고 그대로 둔다
                mark_dialog(conn, sess["session_id"], box)
                return self._send(200, {"ok": True, "waiting": True,
                                        "message": "리모트 컨트롤 확인 창이 떠 있습니다 — 터미널에서 골라 주세요: " + box})
            return self._send(200, {"ok": True, "message": "리모트 컨트롤을 켰습니다 · 다음 응답 때 연결이 확인됩니다"})

        if path == "/api/settings" and method == "POST":
            if "auto_remote" in body:
                store.set_setting(conn, "auto_remote", bool(body["auto_remote"]))
            if "auto_daily" in body:
                store.set_setting(conn, "auto_daily", bool(body["auto_daily"]))
            return self._send(200, {"ok": True})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/model", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            if sess["state"] not in ("idle", "done", "error"):
                return self._send(409, {"error": "작업 중이거나 확인 필요 상태에서는 바꿀 수 없습니다. 끝난 뒤 다시 해 주세요"})
            model = (body.get("model") or "").strip() or None
            effort = (body.get("effort") or "").strip() or None
            if sess.get("agent") == "codex":
                return self._send(*codex_model(conn, sess, model, effort))
            if model and model not in MODEL_CHOICES:
                return self._send(400, {"error": "모르는 모델입니다"})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409, {"error": err})
            if model:
                # 훅(PreModelSwitch)이 이 표시를 보고 캐시 확인 창 없이 전환을 허용한다
                conn.execute("UPDATE sessions SET model_switch_at=? WHERE session_id=?", (store.now(), sess["session_id"]))
                conn.commit()
            ok, msg = focus.set_model_effort(handle, model, effort)
            if not ok:
                conn.execute("UPDATE sessions SET model_switch_at=NULL WHERE session_id=?", (sess["session_id"],))
                return self._send(502, {"error": msg})
            if model:
                conn.execute("UPDATE sessions SET model=? WHERE session_id=?", (model, sess["session_id"]))
            if effort and "effort 는 그대로" not in msg:
                conn.execute("UPDATE sessions SET effort=? WHERE session_id=?", (effort, sess["session_id"]))
            return self._send(200, {"ok": True, "message": msg})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/delete", path)
        if m and method == "POST":
            if body.get("from"):   # 브레인·CLI 가 아니라 사람이 보드에서만
                return self._send(403, {"error": "보드에서만 지울 수 있습니다"})
            if not conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (m.group(1),)).fetchone():
                return self._send(404, {"error": "no session"})
            return self._send(*delete_sessions(conn, m.group(1)))
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/codex-new-chat", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None or sess.get("agent") != "codex":
                return self._send(404, {"error": "no session"})
            cmd = body.get("cmd") if body.get("cmd") in ("clear", "new") else "clear"
            return self._send(*codex_new_chat(conn, sess, cmd))
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/recap", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            return self._send(*run_recap(conn, sess))
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/claude-cmd", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None or sess.get("agent") == "codex":
                return self._send(404, {"error": "no session"})
            return self._send(*claude_cmd(conn, sess, body))
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/codex-cmd", path)
        if m and method == "POST":
            # Codex 의 / 명령을 보드에서: 보내고 선택 창이 뜨면 그 목록, 아니면 새로 찍힌 글을 돌려준다.
            # pick: 지금 뜬 선택 창에서 고르기 · cancel: 닫기
            sess = session_row(conn, m.group(1))
            if sess is None or sess.get("agent") != "codex":
                return self._send(404, {"error": "no session"})
            tg = codex_tab(conn, sess)
            if not tg:
                return self._send(409, {"error": "Codex 앱의 대화는 앱에서 해 주세요" if codex.desktop_session(sess)
                                        else "Codex 가 도는 터미널을 찾지 못했습니다"})
            if body.get("cancel"):
                codex.cancel(tg)
                return self._send(200, {"ok": True})
            if body.get("pick") is not None:
                res = codex.choose(tg, int(body["pick"]), str(body.get("key") or "enter"))
                codex.note_perm_change(sess["session_id"], res.get("output"))   # /permissions → 칸 머리 칩을 바로
                return self._send(502 if res.get("error") else 200, res)
            text = CONTROL_CHARS.sub("", body.get("text") or "").strip()
            if not text.startswith("/"):
                return self._send(400, {"error": "/ 명령만"})
            if sess["state"] in ("working", "waiting") or codex.busy(tg):
                return self._send(409, {"error": "작업 중에는 보드에서 실행하지 않습니다 — 끝난 뒤에(또는 터미널에서)"})
            res = codex.run_command(tg, text)
            return self._send(502 if res.get("error") else 200, res)
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/codex-fork", path)
        if m and method == "POST":
            # Codex 의 /fork — 이 탭이 갈라진 새 대화로 넘어간다(새 칸은 훅·기록 동기화가 원본의 갈래로 잇는다)
            sess = session_row(conn, m.group(1))
            if sess is None or sess.get("agent") != "codex":
                return self._send(404, {"error": "no session"})
            if sess["state"] in ("working", "waiting"):
                return self._send(409, {"error": "작업 중에는 가를 수 없습니다 — 끝난 뒤에"})
            tg = codex_tab(conn, sess)
            if not tg:
                return self._send(409, {"error": "Codex 가 도는 터미널을 찾지 못했습니다"})
            ok, msg = codex.fork(tg, bool(body.get("worktree")))
            if not ok:
                return self._send(502, {"error": msg})
            # 이 탭은 이제 갈래 대화다 — 원본을 떼어(보낸 글이 갈래로 들어가지 않게) 「꺼진 세션」으로, 탭에 새로 생기는 대화는
            # 원본의 갈래로 잇는다(훅이 없어도). 첫 요청을 주면 바로 보내 갈래 칸이 곧 생긴다
            conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps({"detached": True}), sess["session_id"]))
            conn.commit()
            codex.LAUNCHES[tg.handle] = {"cwd": sess["cwd"], "at": store.now(), "resume": None, "fork_of": sess["session_id"],
                                         "terminal": tg.terminal.remember({}, tg.handle)}
            prompt = CONTROL_CHARS.sub("", (body.get("prompt") or "").replace("\r\n", "\n")).strip()[:20000]
            if prompt:
                time.sleep(1.2)
                ok2, err = codex.paste(tg, prompt)
                if not ok2:
                    return self._send(200, {"ok": True, "message": msg + " · 첫 요청은 보내지 못했습니다: " + err})
                msg += " · 갈래에 첫 요청을 보냈습니다"
            return self._send(200, {"ok": True, "message": msg, "handle": tg.handle, "prompt": bool(prompt)})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/rename", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            title = re.sub(r"\s+", " ", CONTROL_CHARS.sub("", body.get("title") or "")).strip()
            if not title or len(title) > 100:
                return self._send(400, {"error": "이름은 1~100자로 적어 주세요"})
            title = store.mask(title)
            sent = False
            if sess["state"] in ("idle", "done", "error") and sess.get("agent") == "codex":
                tg = codex_tab(conn, sess)   # Codex 도 /rename <이름> — 스레드 이름(앱·resume 목록)까지 바뀐다
                if tg and not codex.busy(tg):
                    sent, _ = codex.paste(tg, "/rename " + title)
            elif sess["state"] in ("idle", "done", "error"):
                handle, _ = terminal_target(conn, sess)
                if handle:
                    sent, _, _ = focus.send_prompt(handle, "/rename " + title)
            conn.execute("UPDATE sessions SET title=?, title_source=? WHERE session_id=?",
                         (title, "custom" if sent else "board", sess["session_id"]))
            msg = ("세션 이름을 바꿨습니다(Codex 대화 이름도 함께)" if sess.get("agent") == "codex" else
                   "세션 이름을 바꿨습니다(Claude Code·탭 제목도 함께)") if sent else \
                "보드의 이름만 바꿨습니다(작업 중이거나 터미널에 연결되지 않아 /rename 은 보내지 않음)" if sess.get("agent") == "codex" else \
                "보드의 이름만 바꿨습니다(작업 중이거나 터미널에 연결되지 않아 /rename 은 보내지 않음)"
            return self._send(200, {"ok": True, "message": msg, "sent": sent})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/(rewind|permission)", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            if sess["state"] in ("working", "waiting"):
                # 권한 모드는 Shift+Tab 으로 돌아가며 바꾸므로 중간 모드(자동·편집 허용)를 잠깐 지난다 — 그사이 도구 호출이 승인되지 않게
                return self._send(409, {"error": "작업 중에는 %s — 끝난 뒤(또는 중단한 뒤)에" %
                                        ("되돌릴 수 없습니다" if m.group(2) == "rewind" else "권한 모드를 바꾸지 않습니다")})
            if sess.get("agent") == "codex" and m.group(2) == "permission":
                # Codex: /permissions 창에서 고른다(auto=Approve for me · default · bypassPermissions=Full Access · readonly)
                ctg = codex_tab(conn, sess)
                if not ctg:
                    return self._send(409, {"error": "Codex 탭을 찾지 못했습니다"})
                ok, msg = codex.set_perm(ctg, body.get("mode"), sess["session_id"])
                return self._send(200 if ok else 409, {"ok": True, "mode": body.get("mode"), "label": msg} if ok else {"error": msg})
            tg, err = terminal_target(conn, sess)
            if not tg:
                return self._send(409, {"error": err})
            if focus.dialog_text(tg):
                return self._send(409, {"error": "터미널에 선택 창이 떠 있습니다 — 먼저 닫아 주세요"})
            if m.group(2) == "permission":
                want = body.get("mode")
                ok, now = focus.set_permission_mode(tg, want)
                if now:
                    conn.execute("UPDATE sessions SET permission_mode=? WHERE session_id=?", (now, sess["session_id"]))
                if not ok and want == "bypassPermissions" and now is not None:
                    # 권한 확인 없이(bypassPermissions)는 켤 때 허락 옵션(--allow-dangerously-skip-permissions)을 붙인 세션의
                    # Shift+Tab 순환에만 있다 — 없으면 세션을 다시 시작해서(같은 대화로) 그 옵션과 함께 연다. restart 로 확인받은 뒤에만
                    if not body.get("restart"):
                        return self._send(409, {"error": "이 세션은 다시 시작해야 권한 확인 없이 모드를 켤 수 있습니다", "need_restart": True})
                    if sess["state"] in ("working", "waiting"):
                        return self._send(409, {"error": "작업 중에는 다시 시작하지 않습니다 — 끝난 뒤에"})
                    conn.execute("UPDATE sessions SET permission_mode='bypassPermissions' WHERE session_id=?", (sess["session_id"],))
                    conn.commit()
                    self._sent_code = None
                    self._import_route(conn, sess, {"restart": True, "from": body.get("from"), "mode": "bypassPermissions",
                                                    "done_msg": "다시 시작했습니다 · 권한 확인 없이(⚠ 전부 허용) 모드로 열었습니다"})
                    if self._sent_code and self._sent_code >= 400:   # 다시 시작하지 못했다 — 모드를 되돌려 둔다
                        conn.execute("UPDATE sessions SET permission_mode=? WHERE session_id=?", (now, sess["session_id"]))
                    return None
                return self._send(200 if ok else 409, {"ok": ok, "mode": now} if ok else
                                  {"error": "권한 모드를 맞추지 못했습니다(지금: %s)" % (now or "알 수 없음")})
            turn = conn.execute("SELECT id, prompt FROM turns WHERE id=? AND session_id=?",
                                (int(body.get("turn_id") or 0), sess["session_id"])).fetchone()
            if not turn or not turn["prompt"]:
                return self._send(400, {"error": "되돌릴 요청을 찾지 못했습니다"})
            mode = body.get("mode")
            # 같은 글의 요청이 그 뒤에 또 있으면 목록에서 그만큼 건너뛴다(목록은 최신부터 훑는다). 작업 알림 턴은 목록에 없다
            key = focus.rewind_key(turn["prompt"])
            skip = sum(1 for r in conn.execute(
                "SELECT prompt FROM turns WHERE session_id=? AND id>? AND prompt IS NOT NULL AND status IS NOT 'rewound'",
                (sess["session_id"], turn["id"])) if not store.is_task_notice(r["prompt"]) and focus.rewind_key(r["prompt"]) == key)
            ok, msg = focus.rewind(tg, turn["prompt"], mode, skip)
            if not ok:
                return self._send(409, {"error": msg})
            if mode in ("conversation", "both"):
                # 그 요청과 뒤의 턴은 대화에서 지워졌다 — 보드에선 접어서 「되돌려 지운 대화」로 남긴다
                conn.execute("UPDATE turns SET status='rewound' WHERE session_id=? AND id >= ?", (sess["session_id"], turn["id"]))
            return self._send(200, {"ok": True, "message": msg, "prompt": turn["prompt"] if mode in ("conversation", "both") else None})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/tasks/([\w\-]+)", path)
        if m and method == "GET":
            # 백그라운드 작업 하나가 지금 무엇을 하는지(서브에이전트 기록의 텍스트·도구 호출, 또는 명령 출력 끝부분)
            row = conn.execute("SELECT transcript_path FROM sessions WHERE session_id=?", (m.group(1),)).fetchone()
            task = next((x for x in store.bg_tasks(row["transcript_path"]) if x["id"] == m.group(2)), None) if row else None
            if not task:
                return self._send(404, {"error": "그 작업을 찾지 못했습니다(끝난 지 30분이 지났거나 기록에 없음)"})
            mate = conn.execute(
                "SELECT transcript_path FROM sessions WHERE continued_from=? AND branch_kind='team' AND team_agent=?"
                " ORDER BY started_at DESC LIMIT 1", (m.group(1), task["teammate"])).fetchone() if task.get("teammate") else None
            if mate:   # 팀원: 그 팀원 세션의 기록에서 Claude 텍스트·도구 호출
                view = {"items": store.read_activity(mate["transcript_path"], (task["started_at"] or 0) - 5, limit=60), "text": None}
            else:
                view = store.bg_task_view(task)
            task.pop("output", None)
            return self._send(200, {"task": task, **view})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/diff", path)
        if m and method == "GET":
            # 변경 보기(입력창의 /diff 도 이 창으로): 그 세션 폴더(워크트리)의 변경. 읽기만, 비밀값은 가린다
            #  mode=work   커밋 안 한 변경(HEAD 기준) + 새로 만든(추적 안 된) 파일
            #  mode=branch 이 브랜치 전체 — 기본 브랜치와 갈라진 곳부터 지금 작업 트리까지(커밋 안 한 것 포함) + 그 사이 커밋
            row = conn.execute("SELECT COALESCE(worktree, cwd) AS dir, branch FROM sessions WHERE session_id=?", (m.group(1),)).fetchone()
            if row is None or not row["dir"] or not os.path.isdir(row["dir"]):
                return self._send(404, {"error": "폴더가 없습니다"})
            mode = (query.get("mode") or ["work"])[0]
            out = git_diff(row["dir"], mode)
            return self._send(200, {"dir": row["dir"], "branch": row["branch"], **out})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/diff-image", path)
        if m and method == "GET":
            # 변경 보기에서 이미지 파일을 그림으로(바뀌기 전·후)
            row = conn.execute("SELECT COALESCE(worktree, cwd) AS dir FROM sessions WHERE session_id=?", (m.group(1),)).fetchone()
            if row is None or not row["dir"] or not os.path.isdir(row["dir"]):
                return self._send(404, {"error": "폴더가 없습니다"})
            img = diff_image(row["dir"], (query.get("path") or [""])[0], (query.get("side") or ["new"])[0],
                             (query.get("mode") or ["work"])[0])
            return self._send(200, img[1], img[0]) if img else self._send(404, {"error": "이미지를 찾지 못했습니다"})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/close-branch", path)
        if m and method == "POST":
            # 갈래 탭 닫기: 보드에서 보관(숨김). 백그라운드 갈래(/fork)면 Claude Code 쪽 세션도 멈추고 목록에서 뺀다.
            # 이 갈래에서 또 갈라진 갈래는 한 단계 위 원본에 다시 잇는다. 원본·지금 터미널에서 도는 갈래는 닫지 않는다
            row = conn.execute("SELECT session_id, continued_from, branch_kind, bg, state, title, agent FROM sessions WHERE session_id=?",
                               (m.group(1),)).fetchone()
            if row is None or row["branch_kind"] not in ("branch", "fork"):
                return self._send(400, {"error": "갈래만 닫을 수 있습니다(원본은 칸 메뉴의 보관으로)"})
            if row["agent"] == "codex":
                # Codex 갈래: 어느 탭에서도 열려 있지 않으면 닫는다(보드에서 숨김 — Codex 기록은 그대로)
                if codex.tab_of(session_row(conn, row["session_id"])):
                    return self._send(409, {"error": "지금 터미널에서 열려 있는 갈래입니다 — 그 탭에서 닫거나(/quit) 다른 대화로 바꾼 뒤 닫아 주세요"})
            elif not row["bg"] and row["state"] != "ended":
                return self._send(409, {"error": "지금 터미널에서 돌고 있는 갈래입니다 — 다른 갈래로 전환한 뒤 닫아 주세요"})
            msg = "갈래를 닫았습니다"
            if row["bg"]:
                for verb in ("stop", "rm"):
                    try:
                        subprocess.run([daily._claude_bin(), verb, row["session_id"][:8]], capture_output=True, text=True,
                                       timeout=20, env=daily._clean_env())
                    except (OSError, subprocess.SubprocessError) as e:
                        msg = "보드에서는 닫았지만 백그라운드 세션 정리에 실패했습니다: %s" % str(e)[:120]
            conn.execute("UPDATE sessions SET continued_from=? WHERE continued_from=? AND branch_kind IN ('branch', 'fork')",
                         (row["continued_from"], row["session_id"]))
            conn.execute("UPDATE sessions SET archived=1 WHERE session_id=?", (row["session_id"],))
            return self._send(200, {"ok": True, "message": msg})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/import", path)
        if m and method == "POST":
            # 보드 터미널로 가져오기: 다른 터미널(Orca·tmux·iTerm2 …)에서 도는 세션을 보드 터미널로 옮긴다.
            # 같은 세션 ID 로 이어 열어(claude --resume) 칸·넥스트 액션이 그대로 이어진다 — 모델·effort·권한 모드도 넘긴다.
            #  · Orca·tmux: 보드가 /exit 로 끝낸다(워크트리는 남긴다) · 그 밖의 앱: 그 터미널에서 직접 끝낸 뒤 다시 누른다
            #  · 백그라운드 세션: 끄지 않고 보드 터미널에서 claude attach 로 붙는다
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            why = brain_guard(conn, body, sess["session_id"])
            if why:
                return self._send(403, {"error": why})
            return self._import_route(conn, sess, body)
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/resume", path)
        if m and method == "POST":
            # 어느 터미널에서도 돌고 있지 않은 세션(종료·탭을 닫음)을 새 터미널 탭에서 `claude --resume` 으로 이어 연다.
            # 보드가 먼저 물어보고 부른다. 입력칸이 뜰 때까지 기다려 ready 를 알려 주면 보드가 쓰던 글을 이어서 보낸다
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            sid = sess["session_id"]
            why = brain_guard(conn, body, sid)
            if why:
                return self._send(403, {"error": why})
            if sess.get("agent") == "codex":
                return self._send(*codex_resume(conn, sess))
            if sid in (focus.running_sessions() or {}):
                return self._send(409, {"error": "이미 다른 터미널에서 실행 중입니다 — 「터미널로 이동」으로 찾아 주세요"})
            if sess["state"] != "ended" and not not_running(conn, sid):
                return self._send(409, {"error": "실행 중인지 확인하지 못해 새로 열지 않았습니다 — 그 터미널을 확인해 주세요"})
            cwd = sess["cwd"] or ""
            if not os.path.isdir(cwd):
                return self._send(409, {"error": "세션 폴더가 없습니다(워크트리를 지웠나요?): " + cwd})
            tg, err = focus.new_claude_terminal(cwd, extra=resume_flags(conn, sid) + "--resume " + shlex.quote(sid), terminal="web")
            if not tg:
                return self._send(502, {"error": err})
            LAUNCHED[tg.handle] = store.now()
            conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(tg.terminal.remember({}, tg.handle)), sid))
            conn.commit()
            # 입력칸이 뜨고, 훅(SessionStart)이 세션을 다시 연 것(종료 → 대기)까지 본다 — 그래야 이어서 보낸 글이 거절되지 않는다
            ready, end = False, time.time() + 30
            while time.time() < end:
                st = conn.execute("SELECT state FROM sessions WHERE session_id=?", (sid,)).fetchone()[0]
                if st != "ended" and focus.input_line(tg) is not None:
                    ready = True
                    break
                time.sleep(0.5)
            return self._send(200, {"ok": True, "ready": ready, "message": "새 터미널 탭에서 이어 열었습니다" if ready else
                                    "새 터미널 탭에서 여는 중입니다 — 탭에 선택 창이 떠 있으면 먼저 골라 주세요"})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/(switch|attach)", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            full = conn.execute("SELECT cwd, bg, state FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()
            if sess.get("agent") == "codex":
                # Codex 갈래 전환: 열려 있지 않은 갈래를 보드 터미널 새 탭에서 이어 연다(codex resume)
                if codex.tab_of(sess):
                    return self._send(409, {"error": "이미 터미널에서 열려 있습니다"})
                code, res = codex_resume(conn, dict(sess, state="ended"))
                if code == 200 and res.get("message", "").startswith("보드 터미널"):
                    res["message"] = "보드 터미널 새 탭에서 이 갈래를 열었습니다"
                return self._send(code, res)
            if m.group(2) == "attach":
                # 백그라운드 갈래(/fork)를 새 터미널 탭에 붙인다 — 그 탭을 이 세션의 터미널로 기억해 보드에서 바로 보낸다.
                # attach 는 claude agents 의 id 를 받는다(세션 ID 앞자리와 다르다)
                x = live_agent(sess["session_id"])
                if not x:
                    return self._send(409, {"error": "백그라운드로 돌고 있지 않습니다 — 「전환」으로 이어 여세요"})
                tg, err = focus.new_claude_terminal(x.get("cwd") or full["cwd"], extra="attach " + shlex.quote(x["id"]), terminal="web")
                if not tg:
                    return self._send(502, {"error": err})
                LAUNCHED[tg.handle] = store.now()
                conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?",
                             (json.dumps(tg.terminal.remember({}, tg.handle)), sess["session_id"]))
                return self._send(200, {"ok": True, "message": "새 터미널 탭에 붙였습니다 — 이제 여기서 보낼 수 있습니다"})
            # 전환: /branch 갈래들은 한 터미널을 같이 쓴다. 지금 그 터미널에서 도는 갈래에 /resume <이 세션> 을 보내 바꾼다
            mine = focus.handle_of(sess)
            live = [r for r in map(dict, conn.execute(
                "SELECT session_id, title, title_source, cwd, project_root, terminal, state, remote, rc_requested_at, ask"
                " FROM sessions WHERE session_id != ? AND state != 'ended' AND terminal IS NOT NULL ORDER BY last_activity DESC",
                (sess["session_id"],))) if mine and focus.handle_of(r) == mine]
            if not live:
                # 그 터미널에 아무 갈래도 없으면(탭을 닫았으면) 새 탭에서 이 세션을 이어 연다
                tg, err = focus.new_claude_terminal(full["cwd"], extra="--resume " + shlex.quote(sess["session_id"]), terminal="web")
                if not tg:
                    return self._send(502, {"error": err})
                LAUNCHED[tg.handle] = store.now()
                return self._send(200, {"ok": True, "message": "새 터미널 탭에서 이 갈래를 열었습니다"})
            cur = live[0]
            if cur["state"] == "working":
                return self._send(409, {"error": "그 터미널의 「%s」가 작업 중입니다 — 끝난 뒤 바꿔 주세요" % (cur["title"] or "")})
            tg, err = terminal_target(conn, cur)
            if not tg:
                return self._send(409, {"error": err})
            if focus.dialog_text(tg):
                return self._send(409, {"error": "그 터미널에 선택 창이 떠 있습니다 — 먼저 닫아 주세요"})
            ok, _, err = focus.send_prompt(tg, "/resume " + sess["session_id"])
            if not ok:
                return self._send(502, {"error": "전환 실패: " + err})
            return self._send(200, {"ok": True, "message": "터미널을 이 갈래로 바꿨습니다"})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/focus", path)
        if m and method == "POST":
            row = session_row(conn, m.group(1))
            if row is None:
                return self._send(404, {"error": "no session"})
            if row["state"] == "ended" or not_running(conn, row["session_id"]):
                return self._send(200, {"ok": False, "message": NOT_RUNNING, "resume": True})
            ok, message, found = focus.focus(row)
            if found:   # 제목으로 새로 찾은 Orca 핸들은 다음을 위해 기억
                conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(found), row["session_id"]))
            return self._send(200, {"ok": ok, "message": message})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/activity", path)
        if m and method == "GET":
            row = conn.execute("SELECT session_id, transcript_path, agent FROM sessions WHERE session_id=?", (m.group(1),)).fetchone()
            if row is None:
                return self._send(404, {"error": "no session"})
            since = float((query.get("since") or ["0"])[0]) - 1
            until = query.get("until")
            items = activity_of(row, since, float(until[0]) + 1 if until else None)
            return self._send(200, {"items": items})

        if path == "/api/pins" and method == "POST":
            # 끌어 놓기로 바꾼 고정 순서. order 의 순서대로 1, 2, 3… 을 넣어 맨 앞부터 그 순서로 선다
            # (새로 고정하는 칸은 고정 시각이 들어가 맨 뒤에 붙는다). unpin 은 고정을 푼다
            order, unpin = body.get("order") or [], body.get("unpin") or []
            ids = [x for x in order + unpin if isinstance(x, str) and re.fullmatch(r"[\w\-]+", x)]
            if len(ids) != len(order) + len(unpin) or len(ids) > 200:
                return self._send(400, {"error": "잘못된 세션 목록입니다"})
            for i, sid in enumerate(order):   # 보드 칸(bd-…)은 boards 표에
                boards.set_pin(conn, sid, i + 1) or conn.execute("UPDATE sessions SET pinned=? WHERE session_id=?", (i + 1, sid))
            for sid in unpin:
                boards.set_pin(conn, sid, 0) or conn.execute("UPDATE sessions SET pinned=0 WHERE session_id=?", (sid,))
            return self._send(200, {"ok": True})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/(seen|archive|pin)", path)
        if m and method == "POST" and boards.bid_of(m.group(1)) is not None:
            bid, action = boards.bid_of(m.group(1)), m.group(2)
            if action == "archive":
                boards.set_archived(conn, bid, body.get("archived", True))
            elif action == "pin":
                boards.set_pin(conn, m.group(1), store.now() if body.get("pinned", True) else 0)
            return self._send(200, {"ok": True})
        if m and method == "POST":
            sid, action = m.groups()
            if action == "seen":
                conn.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (store.now(), sid))
            elif action == "archive":
                why = brain_guard(conn, body, sid)
                if why:
                    return self._send(403, {"error": why})
                conn.execute("UPDATE sessions SET archived=? WHERE session_id=?",
                             (1 if body.get("archived", True) else 0, sid))
            else:
                # 고정한 시각을 넣어 둔다 — 고정 칸끼리는 먼저 고정한 것이 왼쪽(활동과 무관하게 자리 유지)
                conn.execute("UPDATE sessions SET pinned=? WHERE session_id=?",
                             (store.now() if body.get("pinned", True) else 0, sid))
            return self._send(200, {"ok": True})

        if path == "/api/notes/queue-order" and method == "POST":
            # 「끝나면 보내기」 예약 순서 바꾸기: order 의 순서대로 보낸다. 예약 시각(queued_at)을 가장 이른 것부터 다시 매긴다
            sid = body.get("session_id") or ""
            order = body.get("order") or []
            if not isinstance(order, list) or not all(isinstance(x, int) for x in order) or len(order) > 200:
                return self._send(400, {"error": "잘못된 순서입니다"})
            rows = {r["id"]: r["queued_at"] for r in conn.execute(
                "SELECT id, queued_at FROM notes WHERE session_id=? AND done=0 AND queued_at IS NOT NULL", (sid,))}
            if set(order) != set(rows):
                return self._send(409, {"error": "예약 목록이 바뀌었습니다 — 다시 해 주세요"})
            base, t = min(rows.values()), store.now()
            conn.executemany("UPDATE notes SET queued_at=?, updated_at=? WHERE id=?",
                             [(base + i * 0.001, t, nid) for i, nid in enumerate(order)])
            return self._send(200, {"ok": True})
        if path == "/api/notes" and method == "POST":
            text = (body.get("text") or "").strip()
            images = body.get("images") or []
            if not text and not images:
                return self._send(400, {"error": "text required"})
            try:
                paths = save_images(images)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            t = store.now()
            cur = conn.execute(
                "INSERT INTO notes (session_id, text, done, created_at, updated_at, images) VALUES (?,?,0,?,?,?)",
                (body.get("session_id") or None, store.mask(text), t, t, json.dumps(paths) if paths else None),
            )
            return self._send(200, {"id": cur.lastrowid})

        m = re.fullmatch(r"/api/notes/(\d+)", path)
        if m and method == "PATCH":
            nid = int(m.group(1))
            t = store.now()
            if "text" in body:
                conn.execute("UPDATE notes SET text=?, updated_at=? WHERE id=?",
                             (store.mask(body["text"].strip()), t, nid))
            if "done" in body:
                conn.execute("UPDATE notes SET done=?, done_at=?, updated_at=?, queued_at=CASE WHEN ? THEN NULL ELSE queued_at END"
                             " WHERE id=?", (1 if body["done"] else 0, t if body["done"] else None, t, 1 if body["done"] else 0, nid))
            if "session_id" in body:
                conn.execute("UPDATE notes SET session_id=?, updated_at=? WHERE id=?",
                             (body["session_id"] or None, t, nid))
            if "queued" in body:   # 「끝나면 보내기」 예약·풀기(세션이 있는 안 끝난 할 일만)
                row = conn.execute("SELECT text FROM notes WHERE id=?", (nid,)).fetchone()
                if body["queued"] and row and focus.bang(row["text"]):
                    return self._send(400, {"error": focus.BANG_ERROR})
                conn.execute("UPDATE notes SET queued_at=?, updated_at=? WHERE id=? AND done=0 AND session_id IS NOT NULL",
                             (t if body["queued"] else None, t, nid))
            if "editing" in body:   # 보드에서 고치는 중: 예약 보내기가 고치기 전 글을 가져가지 않게 잡아 둔다(글 저장 뒤에 푼다)
                sendqueue.hold(nid, bool(body["editing"]))
            row = conn.execute("SELECT done FROM notes WHERE id=?", (nid,)).fetchone()
            return self._send(200, {"ok": True, "done": bool(row and row["done"])})
        if m and method == "DELETE":
            conn.execute("DELETE FROM notes WHERE id=?", (int(m.group(1)),))
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def _link_teams():
    """훅이 팀원 표시를 하기 전부터 있던 에이전트 팀 팀원 세션을 팀장 칸에 묶는다(서버 시작 때 한 번)."""
    conn = store.connect()
    try:
        with conn:
            n = store.link_teams(conn)
        if n:
            print("Argus: linked %d agent-team member session(s) to their lead" % n, flush=True)
    except Exception:  # noqa: BLE001 — 실패해도 서버는 뜬다
        traceback.print_exc()
    finally:
        conn.close()


def main():
    # 보드를 띄운 세션의 Claude Code·터미널 변수를 버린다 — 여기서 띄우는 터미널(보드 터미널 tmux 서버 등)과 세션이 물려받지 않게
    for k in [k for k in os.environ if terminals.session_env(k)]:
        del os.environ[k]
    c = store.connect()
    with c:
        boards.ensure(c)   # 세션이 아닌 보드 칸(터미널·마크다운)
    c.close()
    threading.Thread(target=_link_teams, daemon=True).start()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", store.PORT), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # 이미 다른 보드 서버가 떠 있다(훅·ctl.sh 가 겹친 것) — 오류가 아니다. 한 줄만 남기고 끝낸다
        print("Argus board is already running on 127.0.0.1:%d — nothing to do" % store.PORT, flush=True)
        return
    daily.start_scheduler()
    delegate.start_watcher()   # 세션에 맡긴 인박스 카드 지켜보기
    sendqueue.start()          # 넥스트 액션 「끝나면 보내기」
    update.start_scheduler(restart_server)   # 몇 시간마다 새 버전 확인 · 자동이면 조용할 때 받기
    threading.Thread(target=_reap_loop, daemon=True).start()   # 정상 종료한 세션의 빈 탭 닫기
    threading.Thread(target=_dirty_loop, daemon=True).start()  # 칸 머리 변경 보기 버튼용 커밋 안 한 변경 수
    threading.Thread(target=_recap_loop, daemon=True).start()  # 쉬는 세션에 Claude Code 가 쓴 recap 가져오기
    if codex.available():
        threading.Thread(target=_codex_loop, daemon=True).start()  # OpenAI Codex 세션 모으기
    threading.Thread(target=skills.refresh_codex, daemon=True).start()  # Codex 에 설치한 스킬 사본을 저장소에 맞추기
    print("Argus: http://127.0.0.1:%d" % store.PORT, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
