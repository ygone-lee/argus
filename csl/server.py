"""Argus 보드 웹 서버. 127.0.0.1 에서만 열리고 외부로 아무것도 보내지 않는다.

    /usr/bin/python3 csl/server.py          # http://127.0.0.1:7799
"""
import base64
import binascii
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

from csl import brain, cardchat, commands, config, connectors, daily, delegate, focus, gitreport, hook, inboxrun, locate, notesreport, sendqueue, store, terminals  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
SESSION_COLUMNS = (
    "session_id, cwd, project_root, project_name, worktree, branch, title, title_source, recap, recap_at,"
    " state, state_detail, state_at, started_at, last_activity, last_seen_at, ended_at, archived, pinned, source,"
    " remote, rc_requested_at, model, effort, transcript_path, compact_at, compact_trigger, compact_ms, terminal, ask,"
    " continued_from, branch_kind, branch_at, bg, permission_mode"
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
    row = conn.execute("SELECT session_id, title, title_source, cwd, project_root, terminal, state, remote, rc_requested_at, ask"
                       " FROM sessions WHERE session_id=?", (sid,)).fetchone()
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


def terminal_target(conn, sess, need_alive=True):
    """보드에서 글을 보낼 터미널(csl/terminals 의 Target). (Target, None) 또는 (None, 오류 문구)

    찾은 터미널은 이 요청이 끝날 때까지 잡아 둔다(예약 보내기·위임·브레인이 그사이 같은 터미널에 키를 넣지 않게).
    그 전에 이 요청의 DB 쓰기를 커밋한다 — 터미널 조작(수 초) 내내 쓰기 잠금을 쥐면 모든 세션의 훅이 막힌다."""
    conn.commit()
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
    """그 세션의 돌고 있는 백그라운드 프로세스(claude agents 항목) 또는 None."""
    return next((a for a in agents_list(fresh=True) if a.get("sessionId") == sid and a.get("pid")), None)


def attach_background(conn, sess, wait=25):
    """백그라운드 세션(/fork·claude --bg)은 터미널 탭이 없거나, 붙였던 탭에서 빠져나와 셸만 남은 게 보통이다 —
    거절하지 않고 새 탭에 `claude attach` 로 붙여 그 탭을 이 세션의 터미널로 기억하고, 입력칸이 뜰 때까지 기다린다.
    (Target, None) · (None, 오류) · 백그라운드로 돌고 있는 세션이 아니면 (None, None)."""
    row = conn.execute("SELECT bg, cwd FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()
    x = live_agent(sess["session_id"]) if row and row["bg"] else None
    if not x:
        return None, None
    tg, err = focus.new_claude_terminal(x.get("cwd") or row["cwd"] or "", extra="attach " + shlex.quote(x["id"]))
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
        " s.title, s.project_name, s.project_root, s.cwd, s.state, s.archived,"
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
            "state": r["state"], "archived": r["archived"], "from_end": r["from_end"],
        })
    return out


SLACK_LINK = re.compile(r"[ \t]*<?https://[\w.-]+\.slack\.com/archives/[^\s>]+>?")


def strip_slack_links(text):
    """카드 프롬프트에서 슬랙 메시지 링크를 뺀다 — 받는 세션엔 쓸모없고 원문은 카드의 「원문 ↗」로 본다."""
    text = SLACK_LINK.sub("", text or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def capture_btw(t, aside_id, question):
    """곁질문(/btw) 답을 터미널 화면에서 읽어 저장하고 창을 닫는다(백그라운드)."""
    answer, err = focus.read_btw(t, question)
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
    r"/api/sessions/([\w\-]+)/(?:send|remote|quit|archive|pin|rename|interrupt|model|permission|rewind|answer|prompt|focus|switch|attach|close-branch)"
    r"|/api/sessions/new|/api/agents/[0-9a-f]{8}/\w+|/api/(?:config|settings|open|daily)|/api/notes/\d+|/api/inbox/\d+/delegate")


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
        "   AND t4.prompt NOT LIKE '<task-notification>%' AND t4.prompt_at <= COALESCE(pt.prompt_at, e.at)"
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
REAPED = set()  # 닫은 탭(다시 보지 않게)


def reap_tabs():
    """보드가 띄운 탭은 `claude … && exit` 라 Claude 가 정상 종료하면 셸도 끝난다. Orca 는 그 탭을 빈 채로 남기므로 닫는다.

    살펴보는 탭: 이번에 서버가 띄운 것 + 10분 안에 끝난 세션의 탭. 실시간 목록(한 번 조회)에 없는 것만 하나씩 확인해서,
    셸이 끝났으면(connected=false) 닫는다 — 셸이 살아 있는(오류로 끝나 셸이 남은·사용자가 연) 탭은 건드리지 않는다."""
    t = terminals.default()
    if not t:
        return
    cands = set(LAUNCHED)
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


def _dirty_check(d):
    if not os.path.isdir(d):
        return None
    try:
        r = subprocess.run(["git", "-C", d, "status", "--porcelain=v1", "--untracked-files=normal"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return DIRTY.get(d)
    if r.returncode != 0:
        return None
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    files, new = len(lines), sum(1 for ln in lines if ln.startswith("??"))   # ?? = 추적 안 된 새 파일(폴더는 한 줄)
    add = dele = 0
    if files:
        st = _git(d, "diff", "HEAD", "--shortstat", timeout=10)
        m1, m2 = re.search(r"(\d+) insertion", st), re.search(r"(\d+) deletion", st)
        add, dele = int(m1.group(1)) if m1 else 0, int(m2.group(1)) if m2 else 0
    return {"files": files, "new": new, "add": add, "del": dele, "at": store.now()}


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
    for r in rows:
        s = dict(r)
        handle, is_brain = focus.handle_of(s), brain.is_brain(s.get("cwd"))
        # 요청도 기록 파일도 없는 세션은 아직 아무것도 안 한 것 — 백그라운드 데몬이 미리 띄워 둔 세션 등이라 숨긴다
        if not s["turn_count"] and not s["pinned"] and not s["open_notes"] and s["state"] != "waiting" \
                and not os.path.isfile(r["transcript_path"] or "") \
                and handle not in LAUNCHED \
                and not is_brain:   # 보드에서 방금 띄운 세션·브레인은 빈 칸으로 보인다
            continue
        follow_rename(conn, s)
        follow_compact(conn, s)
        s["term_handle"] = handle   # 보드에서 새로 띄운 세션을 찾는 데 쓴다
        s["brain"] = is_brain       # 브레인 세션은 칸 대신 브레인 패널에
        s.pop("terminal", None)
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
                                                   "new_default_model", "new_default_effort", "brain_model", "brain_permission_mode")}
    # 할 일 카드가 생긴 시각들 — 보드가 「마지막으로 인박스를 본 뒤 새로 온 카드」를 센다(본 시각은 브라우저에 기억)
    inbox_new_at = [r[0] for r in conn.execute("SELECT created_at FROM inbox WHERE status='new'")]
    return {"sessions": sessions, "notes": notes, "settings": settings, "usage": store.read_usage(), "now": store.now(),
            "inbox_new": len(inbox_new_at), "inbox_new_at": inbox_new_at}


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
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
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
        if method in ("POST", "PATCH"):
            body = self._json_body()
            if body is None:
                return self._send(415, {"error": "JSON 객체 본문(application/json)이 필요합니다"})
        conn = store.connect()
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
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

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
            return self._send(200, {"commands": commands.all_commands(root)})

        # ---------------------------------------------------------- 브레인·슬랙 인박스
        if method == "POST" and path == "/api/brain/start":
            if brain.current(conn):
                return self._send(409, {"error": "브레인이 이미 켜져 있습니다"})
            tg, err = brain.start(body.get("model") or None, body.get("permission_mode") or None)
            if not tg:
                return self._send(502, {"error": err})
            LAUNCHED[tg.handle] = store.now()
            return self._send(200, {"ok": True, "handle": tg.handle})
        if method == "POST" and path == "/api/brain/settings":
            every, err = config.validate("brain_slack_every", body.get("slack_every") or 0)
            if err:
                return self._send(400, {"error": err})
            store.set_setting(conn, "brain_slack_every", every)
            return self._send(200, {"ok": True})
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
            if not fields:
                return self._send(400, {"error": "바꿀 내용이 없습니다"})
            iid = int(m.group(1))
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
            tg, err = focus.new_claude_terminal(cwd, model or None, extra=" ".join(extra))
            if not tg:
                return self._send(502, {"error": err})
            LAUNCHED[tg.handle] = store.now()
            return self._send(200, {"ok": True, "handle": tg.handle})
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
            # 세션 종료 — 터미널에서 Claude Code 를 닫은 것처럼: /exit 로 정상 종료시키고 탭을 닫는다.
            # 작업 중이면 force 일 때만 먼저 Esc 로 멈춘다. 종료가 선택 창(바꾼 게 있는 워크트리를 남길지 등)을 띄우면 거기서 멈추고 사람이 고른다
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            why = brain_guard(conn, body, sess["session_id"])
            if why:
                return self._send(403, {"error": why})
            if sess["state"] == "ended":
                return self._send(409, {"error": "이미 종료된 세션입니다"})
            bg = conn.execute("SELECT bg FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()["bg"]   # session_row 엔 없다
            x = live_agent(sess["session_id"]) if bg else None
            if x:
                # 백그라운드 세션 — 탭에 `claude attach` 로 붙어 있어도 거기서 /exit 를 치면 보기만 떨어지고 세션은 계속 돈다
                # (칸이 그대로 남았다). claude stop 으로 멈추고, 붙어 있던 탭이 있으면 그 탭도 닫는다
                if (sess["state"] == "working" or x.get("state") == "working") and not body.get("force"):
                    return self._send(409, {"error": "작업 중입니다", "need_force": True})
                try:
                    r = subprocess.run([daily._claude_bin(), "stop", x["id"]], capture_output=True, text=True, timeout=20,
                                       env=daily._clean_env())
                except (OSError, subprocess.SubprocessError) as e:
                    return self._send(502, {"error": str(e)[:200]})
                AGENTS["at"] = 0
                if r.returncode != 0:
                    return self._send(502, {"error": (r.stderr or r.stdout or "멈추지 못했습니다").strip()[:300]})
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
                return self._send(200, {"ok": True, "message": "백그라운드 세션을 멈췄습니다" + (" · 터미널 탭도 닫았습니다" if closed else "")})
            if bg and not focus.handle_of(sess):
                return self._send(409, {"error": "그 백그라운드 세션의 프로세스를 찾지 못했습니다"})
            handle, err = terminal_target(conn, sess)
            if not handle:
                return self._send(409, {"error": err})
            box = focus.dialog_text(handle)
            if box:
                if focus.prompt_view(handle):   # 고를 수 있는 창이면 칸의 🔐 카드로 고르게
                    _mark_prompt(conn, sess["session_id"], "종료 중 — 터미널이 묻고 있습니다")
                    return self._send(409, {"error": "터미널이 고를 것을 묻고 있습니다 — 칸의 🔐 확인 필요 카드에서 골라 주세요"})
                return self._send(409, {"error": "터미널에 선택 창이 떠 있습니다 — 먼저 골라 주세요: " + box})
            if sess["state"] == "working" or focus.is_busy(handle):
                if not body.get("force"):
                    return self._send(409, {"error": "작업 중입니다", "need_force": True})
                focus._raw(handle, focus.ESC)   # 작업을 멈추고(Claude Code 의 중단 키)
                for _ in range(20):
                    time.sleep(0.4)
                    if not focus.is_busy(handle) and focus.input_line(handle) is not None:
                        break
                else:
                    return self._send(409, {"error": "작업을 멈추지 못했습니다 — 터미널에서 확인해 주세요"})
            ok, _, err = focus.send_prompt(handle, "/exit")
            if not ok:
                return self._send(502, {"error": "/exit 를 보내지 못했습니다: " + (err or "")})
            for _ in range(40):   # Claude 가 끝날 때까지(워크트리 정리 등) — 선택 창이 뜨면 사람에게 맡긴다
                time.sleep(0.5)
                if not focus.claude_alive(sess, handle):
                    break
                # 닫히는 도중엔 입력칸이 사라진 화면이 잠깐 보인다 — 번호 고르기 줄(❯ 1. …)이 있을 때만 선택 창으로 본다
                lines = focus._screen(handle)
                opts = [ln.strip() for ln in lines[-15:] if focus._OPTION.match(ln)]
                if opts and focus._input_line_of(lines) is None:
                    # 바꾼 게 있는 워크트리를 남길지 등 — 「확인 필요」로 두어 칸에 🔐 카드가 뜨게(고르면 Claude 가 끝나고 탭은 서버가 닫는다)
                    _mark_prompt(conn, sess["session_id"], "종료 중 — 터미널이 묻고 있습니다")
                    return self._send(200, {"ok": True, "message": "종료하는 중 터미널이 고를 것을 묻습니다 — 칸의 🔐 확인 필요 카드에서 골라 주세요"})
            else:
                return self._send(409, {"error": "Claude 가 아직 끝나지 않았습니다 — 터미널에서 확인해 주세요"})
            # 보드가 띄운 탭은 셸까지 끝났고, 직접 연 탭은 셸이 남는다 — 어느 쪽이든 탭을 닫는다(Claude 를 닫은 뒤라 잃을 것은 없다)
            REAPED.add(handle.handle)
            LAUNCHED.pop(handle.handle, None)
            closed = handle.terminal.close(handle.handle)
            return self._send(200, {"ok": True, "message": "세션을 종료했습니다" + (" · 터미널 탭도 닫았습니다" if closed else "(탭은 닫지 못함 — 직접 닫아 주세요)")})
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
                if r.returncode != 0:
                    return self._send(502, {"error": (r.stderr or r.stdout or "실패").strip()[:300]})
                return self._send(200, {"ok": True, "message": "멈췄습니다(대화는 남아 있어 다시 열 수 있습니다)" if act == "stop"
                                        else "목록에서 지웠습니다"})
            if not x.get("pid"):
                return self._send(409, {"error": "프로세스가 없는 기록입니다(이미 끝남) — 열 수 없습니다. 「목록에서 지우기」로 정리하세요"})
            tg, err = focus.new_claude_terminal(x.get("cwd") or "", extra="attach " + aid)
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
        if path == "/api/activity" and method == "GET":
            # 작업 중인 여러 세션의 진행 과정을 한 번에: items=<세션>:<시작 시각>,…
            want = {}
            for part in ((query.get("items") or [""])[0]).split(",")[:40]:
                sid, _, since = part.partition(":")
                if re.fullmatch(r"[\w\-]+", sid):
                    want[sid] = float(since or 0)
            paths = {r["session_id"]: r["transcript_path"] for r in conn.execute(
                "SELECT session_id, transcript_path FROM sessions WHERE session_id IN (%s)" % ",".join("?" * len(want)),
                list(want))} if want else {}
            return self._send(200, {"items": {sid: store.read_activity(paths[sid], want[sid] - 1) for sid in want if sid in paths}})
        m = re.fullmatch(r"/api/sessions/([\w\-]+)", path)
        if m and method == "GET":
            limit = max(1, min(int((query.get("limit") or ["300"])[0]), 1000))
            detail = session_detail(conn, m.group(1), limit)
            return self._send(200, detail) if detail else self._send(404, {"error": "no session"})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/send", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            why = brain_guard(conn, body, sess["session_id"])
            if why:
                return self._send(403, {"error": why})
            if sess["state"] == "waiting":
                # 승인·질문 창에 글자가 들어가면 엉뚱한 선택이 될 수 있다. 다만 창을 거절·중단하면 훅이 오지 않아
                # 「확인 필요」가 남는다 — 화면에 입력칸이 보이면(창이 없으면) 지난 상태로 보고 보낸다
                if focus.input_line(focus.target_of(sess)) is None:
                    return self._send(409, {"error": "확인 필요 상태입니다. 터미널에서 승인·답변을 먼저 해 주세요"})
                conn.execute("UPDATE sessions SET state='done', state_detail=NULL, ask=NULL WHERE session_id=?",
                             (sess["session_id"],))
                sess["state"] = "done"
            if sess["state"] == "ended":
                return self._send(409, {"error": "종료된 세션입니다"})
            text = CONTROL_CHARS.sub("", (body.get("text") or "").replace("\r\n", "\n")).strip()
            images = body.get("images") or []
            if not text and not images:
                return self._send(400, {"error": "보낼 내용이 없습니다"})
            if len(text) > 20000:
                return self._send(400, {"error": "메시지가 너무 깁니다(2만 자 이하)"})
            if focus.bang(text):
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
            if (store.get_setting(conn, "auto_remote", False) and not body.get("from")
                    and not sess["remote"] and not sess["rc_requested_at"]):
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
                ok, err = focus.type_text(handle, text, paths)
                stages = []
            else:
                ok, stages, err = focus.send_prompt(handle, text)
            if not ok:
                return self._send(502, {"error": "전송 실패: " + err})
            if re.match(r"/compact(\s|$)", text) and sess["state"] != "working":
                # PreCompact 훅이 곧 오지만, 훅을 아직 못 읽은(설정 변경 전에 연) 세션도 바로 보이게
                store.start_compact(conn, sess["session_id"], "manual")
                return self._send(200, {"ok": True, "message": "컨텍스트를 요약하는 중입니다", "stages": stages, "images": 0})
            src = body.get("from")
            if src and src != sess["session_id"]:
                row = conn.execute("SELECT cwd FROM sessions WHERE session_id=?", (src,)).fetchone()
                if row and brain.is_brain(row["cwd"]) and not text.startswith("/"):
                    conn.execute("UPDATE brain_watch SET status='replaced' WHERE target_sid=? AND status='open'", (sess["session_id"],))
                    conn.execute("INSERT INTO brain_watch (brain_sid, target_sid, sent_at, prompt) VALUES (?,?,?,?)",
                                 (src, sess["session_id"], store.now(), store.mask(text)[:2000]))
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
            if stopped:
                t = store.now()
                conn.execute("UPDATE turns SET status='interrupted', response=COALESCE(response, '(중단됨)'), response_at=COALESCE(response_at, ?)"
                             " WHERE session_id=? AND response_at IS NULL AND status='running'", (t, sess["session_id"]))
                conn.execute("UPDATE sessions SET state='done', state_detail='중단됨', state_at=?, compact_at=NULL WHERE session_id=?",
                             (t, sess["session_id"]))
                store.add_event(conn, sess["session_id"], "error", "사용자가 중단함")
            return self._send(200, {"ok": True, "stopped": stopped,
                                    "message": "중단했습니다" if stopped else "중단 키를 보냈습니다 — 지금 단계가 정리되는 대로 멈춥니다"})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/remote", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            why = brain_guard(conn, body, sess["session_id"])
            if why:
                return self._send(403, {"error": why})
            if sess["remote"]:
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
            if sess["state"] in ("idle", "done", "error"):
                handle, _ = terminal_target(conn, sess)
                if handle:
                    sent, _, _ = focus.send_prompt(handle, "/rename " + title)
            conn.execute("UPDATE sessions SET title=?, title_source=? WHERE session_id=?",
                         (title, "custom" if sent else "board", sess["session_id"]))
            msg = "세션 이름을 바꿨습니다(Claude Code·탭 제목도 함께)" if sent else \
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
            tg, err = terminal_target(conn, sess)
            if not tg:
                return self._send(409, {"error": err})
            if focus.dialog_text(tg):
                return self._send(409, {"error": "터미널에 선택 창이 떠 있습니다 — 먼저 닫아 주세요"})
            if m.group(2) == "permission":
                ok, now = focus.set_permission_mode(tg, body.get("mode"))
                if now:
                    conn.execute("UPDATE sessions SET permission_mode=? WHERE session_id=?", (now, sess["session_id"]))
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
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/close-branch", path)
        if m and method == "POST":
            # 갈래 탭 닫기: 보드에서 보관(숨김). 백그라운드 갈래(/fork)면 Claude Code 쪽 세션도 멈추고 목록에서 뺀다.
            # 이 갈래에서 또 갈라진 갈래는 한 단계 위 원본에 다시 잇는다. 원본·지금 터미널에서 도는 갈래는 닫지 않는다
            row = conn.execute("SELECT session_id, continued_from, branch_kind, bg, state, title FROM sessions WHERE session_id=?",
                               (m.group(1),)).fetchone()
            if row is None or row["branch_kind"] not in ("branch", "fork"):
                return self._send(400, {"error": "갈래만 닫을 수 있습니다(원본은 칸 메뉴의 보관으로)"})
            if not row["bg"] and row["state"] != "ended":
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
        m = re.fullmatch(r"/api/sessions/([\w\-]+)/(switch|attach)", path)
        if m and method == "POST":
            sess = session_row(conn, m.group(1))
            if sess is None:
                return self._send(404, {"error": "no session"})
            full = conn.execute("SELECT cwd, bg, state FROM sessions WHERE session_id=?", (sess["session_id"],)).fetchone()
            if m.group(2) == "attach":
                # 백그라운드 갈래(/fork)를 새 터미널 탭에 붙인다 — 그 탭을 이 세션의 터미널로 기억해 보드에서 바로 보낸다.
                # attach 는 claude agents 의 id 를 받는다(세션 ID 앞자리와 다르다)
                x = live_agent(sess["session_id"])
                if not x:
                    return self._send(409, {"error": "백그라운드로 돌고 있지 않습니다 — 「전환」으로 이어 여세요"})
                tg, err = focus.new_claude_terminal(x.get("cwd") or full["cwd"], extra="attach " + shlex.quote(x["id"]))
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
                tg, err = focus.new_claude_terminal(full["cwd"], extra="--resume " + shlex.quote(sess["session_id"]))
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
            ok, message, found = focus.focus(row)
            if found:   # 제목으로 새로 찾은 Orca 핸들은 다음을 위해 기억
                conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(found), row["session_id"]))
            return self._send(200, {"ok": ok, "message": message})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/activity", path)
        if m and method == "GET":
            row = conn.execute("SELECT transcript_path FROM sessions WHERE session_id=?", (m.group(1),)).fetchone()
            if row is None:
                return self._send(404, {"error": "no session"})
            since = float((query.get("since") or ["0"])[0]) - 1
            until = query.get("until")
            items = store.read_activity(row["transcript_path"], since, float(until[0]) + 1 if until else None)
            return self._send(200, {"items": items})

        if path == "/api/pins" and method == "POST":
            # 끌어 놓기로 바꾼 고정 순서. order 의 순서대로 1, 2, 3… 을 넣어 맨 앞부터 그 순서로 선다
            # (새로 고정하는 칸은 고정 시각이 들어가 맨 뒤에 붙는다). unpin 은 고정을 푼다
            order, unpin = body.get("order") or [], body.get("unpin") or []
            ids = [x for x in order + unpin if isinstance(x, str) and re.fullmatch(r"[\w\-]+", x)]
            if len(ids) != len(order) + len(unpin) or len(ids) > 200:
                return self._send(400, {"error": "잘못된 세션 목록입니다"})
            conn.executemany("UPDATE sessions SET pinned=? WHERE session_id=?", [(i + 1, sid) for i, sid in enumerate(order)])
            conn.executemany("UPDATE sessions SET pinned=0 WHERE session_id=?", [(sid,) for sid in unpin])
            return self._send(200, {"ok": True})

        m = re.fullmatch(r"/api/sessions/([\w\-]+)/(seen|archive|pin)", path)
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
            return self._send(200, {"ok": True})
        if m and method == "DELETE":
            conn.execute("DELETE FROM notes WHERE id=?", (int(m.group(1)),))
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def main():
    store.connect().close()
    httpd = ThreadingHTTPServer(("127.0.0.1", store.PORT), Handler)
    daily.start_scheduler()
    delegate.start_watcher()   # 세션에 맡긴 인박스 카드 지켜보기
    sendqueue.start()          # 넥스트 액션 「끝나면 보내기」
    threading.Thread(target=_reap_loop, daemon=True).start()   # 정상 종료한 세션의 빈 탭 닫기
    threading.Thread(target=_dirty_loop, daemon=True).start()  # 칸 머리 변경 보기 버튼용 커밋 안 한 변경 수
    print("Argus: http://127.0.0.1:%d" % store.PORT, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
