"""하루 작업 일지: 그날 모든 세션의 요청·응답 요약을 모아 `claude -p` 로 정리한다.

- 세션 기록을 남기지 않고(--no-session-persistence) 도구도 쓰지 않는다(--tools "").
- 보드 훅을 끄고(CSL_SKIP=1) 프로젝트 폴더가 아닌 곳에서 실행해 보드·/resume 에 잡히지 않는다.
- Claude Code·Orca 환경 변수를 지운 깨끗한 환경에서 실행한다. 서버는 자기를 띄운 세션의 변수
  (CLAUDE_CODE_SESSION_ID·MESSAGING_SOCKET·CHILD_SESSION 등)를 물려받는데, 그대로 넘기면 claude -p 가
  그 세션의 하위 세션처럼 굴며 끝나지 않았다(실측).
- 매일 아침 6시가 지나면 전날 일지가 없을 때 자동으로 만든다(서버가 1분마다 확인, 잠들어 있었으면 깨어난 뒤).
- 일반 대화처럼 계정 사용량을 쓴다.
"""
import os
import re
import shutil
import subprocess
import threading
import json
import time
from datetime import datetime, timedelta

from csl import config, store

RUN_DIR = os.path.join(store.DATA_DIR, "daily-run")
MAX_DIGEST = 90000   # 일지 입력 상한(글자). 넘으면 턴마다 더 짧게 자른다
# 자동 정리 시각·제한 시간은 설정 창(daily_hour·daily_timeout)

INSTRUCTION = """아래는 내가 {day}에 여러 Claude Code 세션에서 나눈 대화 기록(요청과 응답 요약)과 넥스트 액션 메모다.
턴마다 「내 요청 요지 → Claude 응답 첫 문장(결과)」 한 줄, 세션마다 Claude Code 가 만든 recap(그 시점에 하던 일 요약),
[중단]·[실패]·[응답 없음] 표시, 넥스트 액션 메모, 세션의 지금 상태가 들어 있다.
[작업 중 덧붙인 요청]은 진행 중이던 작업 도중에 Claude 에게 전달돼 그 응답에서 함께 처리된 것이니 놓친 것으로 치지 마.
이걸로 하루 작업 일지를 한국어 마크다운으로 정리해 줘. 기록에 있는 것만 쓰고 추측하지 마. 도구는 쓰지 말고 바로 답해.

형식:
## 한 줄 요약
## 한 일
프로젝트별로 묶고, 그 아래 세션(작업)마다 무엇을 했고 결과가 어떻게 됐는지 bullet 로.
## 놓친 것·확인 필요
요청했는데 답을 못 받았거나 중단·실패한 것, 확인 필요 상태로 남은 세션, Claude 가 물어봤는데 답하지 않은 질문,
"나중에"·"다음에" 하기로 하고 미룬 것. 없으면 "없음".
## 커밋·PR 남은 것
맨 아래 「놓친 git 작업」(일지를 만드는 지금 기준)을 그대로 옮기되, 커밋 안 한 변경·푸시 안 한 커밋·PR 없는 브랜치·
머지 안 된 PR 을 bullet 로. 오래된 워크트리는 한 줄로. 배포 여부는 쓰지 마(머지되면 배포에 딸려 나간다). 없으면 "없음".
## 넥스트 액션 남은 것
맨 아래 「남은 넥스트 액션」(지금 기준, 오늘 활동이 없던 세션 것도 포함)을 세션 「제목」별로 bullet 로 옮긴다.
⚠(오래된 것)을 먼저 쓰고, 오늘 대화로 이미 끝난 것 같으면 「끝난 듯 — 완료 표시 필요」라고 덧붙인다. 없으면 "없음".
## 결정·알게 된 것
## 남은 일
끝내지 못한 것, 다음에 할 것(위 넥스트 액션과 겹치는 건 빼고).
## 시간대
대략 언제 무엇을 했는지 몇 줄로.
"""

_running = set()
_lock = threading.Lock()


def day_bounds(day):
    start = datetime.strptime(day, "%Y-%m-%d")
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _first_paragraph(text, n):
    for block in re.split(r"\n\s*\n", (text or "").replace("\r", "")):
        t = block.strip()
        if t and not re.match(r"^(#|\||```|---|>|<)", t):
            return " ".join(t.split())[:n]
    return " ".join((text or "").split())[:n]


def day_recaps(path, start, end, chunk=8 * 1024 * 1024):
    """그날(start~end) 기록 파일에 남은 recap(away_summary) 목록 [(시각, 내용)].

    기록 파일은 시간순으로 쌓이므로 끝에서부터 거꾸로 읽다가 그날 이전 기록이 나오면 멈춘다
    (수백 MB 짜리 기록 파일도 그날 분량만 읽는다).
    """
    out = []
    if not path or not os.path.isfile(path):
        return out
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            pos, carry = size, b""
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + carry
                lines = data.split(b"\n")
                carry = lines[0] if pos > 0 else b""   # 덩어리 경계에서 잘린 첫 줄은 다음 덩어리와 합친다
                body = lines[1:] if pos > 0 else lines
                oldest = None
                for raw in body:
                    if b'"timestamp"' not in raw:
                        continue
                    if b"away_summary" in raw:
                        try:
                            d = json.loads(raw)
                        except ValueError:
                            continue
                        ts = store.parse_ts(d.get("timestamp"))
                        if d.get("subtype") == "away_summary" and ts and start <= ts < end and d.get("content"):
                            out.append((ts, store.mask(d["content"])))
                    elif oldest is None:
                        m = re.search(rb'"timestamp":"([^"]+)"', raw)
                        oldest = store.parse_ts(m.group(1).decode()) if m else None
                if oldest is not None and oldest < start:
                    break
    except OSError:
        return out
    return sorted(set(out))


def _first_sentence(text, n=120):
    """응답의 첫 문장. Claude 는 대개 결론을 첫 문장에 쓰므로 추가 호출 없이 쓸 수 있는 「결과 요약」이다."""
    p = _first_paragraph(text, 600)
    m = re.search(r"^(.+?(?:다|요|니다|습니다)[.!?]|.+?[.!?])(\s|$)", p)
    s = (m.group(1) if m else p).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def git_section(conn):
    """일지 입력 끝에 붙이는 「놓친 git 작업」(지금 기준). 실패해도 일지는 만든다."""
    try:
        from csl import gitreport
        return "\n\n" + gitreport.text(gitreport.report(conn, refresh=True))
    except Exception as e:  # noqa: BLE001
        return "\n\n# 놓친 git 작업\n(가져오지 못함: %s)" % str(e)[:200]


def notes_section(conn):
    """일지 입력 끝에 붙이는 「남은 넥스트 액션」(지금 기준, 모든 세션)."""
    try:
        from csl import notesreport
        return "\n\n" + notesreport.text(notesreport.collect(conn))
    except Exception as e:  # noqa: BLE001
        return "\n\n# 남은 넥스트 액션\n(가져오지 못함: %s)" % str(e)[:200]


def digest(conn, day, prompt_len=80, resp_len=120):
    """(일지 입력 문자열, 세션 수, 턴 수).

    추가 모델 호출 없이 이미 있는 요약만 쓴다:
    - 턴마다 「요청 요지(앞부분) → 응답 첫 문장」 한 줄 — 모든 턴이 빠짐없이 들어간다
    - 세션마다 Claude Code 가 만든 그날의 recap(away_summary) — 흐름 보조
    - 놓친 것(중단·실패·응답 없음), 넥스트 액션, 세션의 지금 상태
    """
    start, end = day_bounds(day)
    rows = conn.execute(
        "SELECT t.session_id, t.prompt, t.response, t.prompt_at, t.response_at, t.status,"
        " s.title, s.project_name, s.branch, s.state, s.transcript_path"
        " FROM turns t JOIN sessions s ON s.session_id = t.session_id"
        " WHERE COALESCE(t.prompt_at, t.response_at) >= ? AND COALESCE(t.prompt_at, t.response_at) < ?"
        " ORDER BY t.session_id, COALESCE(t.prompt_at, t.response_at), t.id",
        (start, end),
    ).fetchall()
    sessions = {}
    for r in rows:
        sessions.setdefault(r["session_id"], []).append(r)

    def hm(ts):
        return time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"

    def short(text, n):
        t = " ".join((text or "").split())
        return t if len(t) <= n else t[:n - 1] + "…"

    parts = []
    # 그날 세션들의 넥스트 액션을 한 번에(세션마다 쿼리하지 않게)
    notes_of = {}
    if sessions:
        sids = list(sessions)
        for n in conn.execute("SELECT session_id, text, done FROM notes WHERE session_id IN (%s) AND (done=0 OR done_at >= ?)"
                              " ORDER BY id" % ",".join("?" * len(sids)), (*sids, start)):
            notes_of.setdefault(n["session_id"], []).append(n)
    for sid, turns in sorted(sessions.items(), key=lambda kv: kv[1][0]["prompt_at"] or kv[1][0]["response_at"] or 0):
        head = turns[0]
        state = {"waiting": "확인 필요(승인·답변 대기)", "working": "작업 중", "error": "오류", "ended": "종료"}.get(head["state"])
        parts.append("## 세션: %s — %s%s%s" % (head["title"] or "(제목 없음)", head["project_name"] or "",
                                             "/" + head["branch"] if head["branch"] else "",
                                             " [지금 상태: %s]" % state if state else ""))
        for ts, text in day_recaps(head["transcript_path"], start, end):
            parts.append("recap %s: %s" % (hm(ts), short(text, 300)))
        for t in turns:
            prompt = (t["prompt"] or "").strip()
            prompt = store.notice_line(prompt) or prompt   # 백그라운드 작업 알림은 요청이 아니라고 적어 둔다
            if re.fullmatch(r"/\S+", prompt) or prompt.startswith("⟳"):
                continue   # /clear 같은 인자 없는 명령, 과거 기록 가져오기가 넣은 압축 지점 표시는 요청이 아니다
            at = t["prompt_at"] or t["response_at"]
            line = "- %s %s" % (hm(at), short(prompt, prompt_len) if prompt else "(요청 기록 없음)")
            if t["response"]:
                mark = {"failed": " [실패]", "interrupted": " [중단]"}.get(t["status"], "")
                line += " →%s %s" % (mark, _first_sentence(t["response"], resp_len))
            elif prompt and t["status"] == "merged":
                # 작업 중에 덧붙여 보낸 요청: Claude Code 가 다음 도구 실행 사이에 끼워 전달(queued_command)해 그 응답에서 함께 처리했다
                line += " → [작업 중 덧붙인 요청 — 진행 중이던 응답에서 함께 처리됨]"
            elif prompt:
                line += " → [진행 중]" if t["status"] == "running" else " → [응답 없음 — 중단됨]"
            parts.append(line)
        for n in notes_of.get(sid, []):
            parts.append("넥스트 액션%s: %s" % (" (완료)" if n["done"] else "", short(n["text"], 200)))
    loose = conn.execute("SELECT text FROM notes WHERE session_id IS NULL AND done=0").fetchall()
    if loose:
        parts.append("## 세션 없는 넥스트 액션")
        parts += ["- " + short(n["text"], 200) for n in loose]
    text = "\n".join(parts)
    if len(text) > MAX_DIGEST and prompt_len > 40:
        return digest(conn, day, prompt_len // 2, resp_len // 2)
    return text, len(sessions), len(rows)


def days(conn, limit=21):
    """활동이 있었던 날짜(최근 순)와 세션·턴 수, 일지 상태."""
    # 날짜(이 컴퓨터 시간대)별로 SQL 에서 센다 — 턴 전체를 파이썬으로 읽지 않게(idx_turns_at 만 훑는다)
    out = {d: {"day": d, "turns": n, "sessions": s} for d, n, s in conn.execute(
        "SELECT date(COALESCE(prompt_at, response_at), 'unixepoch', 'localtime') AS d, COUNT(*), COUNT(DISTINCT session_id)"
        " FROM turns WHERE COALESCE(prompt_at, response_at) IS NOT NULL GROUP BY d ORDER BY d DESC LIMIT ?", (limit,))}
    reports = {r["day"]: r for r in conn.execute("SELECT day, status, created_at FROM daily_reports")}
    rows = []
    for d in sorted(out, reverse=True)[:limit]:
        e = out[d]
        rep = reports.get(d)
        rows.append({"day": d, "turns": e["turns"], "sessions": e["sessions"],
                     "status": ("running" if d in _running else rep["status"] if rep else None)})
    return rows


def get(conn, day):
    row = conn.execute("SELECT * FROM daily_reports WHERE day=?", (day,)).fetchone()
    rep = dict(row) if row else {"day": day, "status": None}
    if day in _running:
        rep["status"] = "running"
    return rep


def start(day):
    """백그라운드로 일지를 만든다. 이미 만드는 중이면 False."""
    with _lock:
        if day in _running:
            return False
        _running.add(day)
    threading.Thread(target=_run, args=(day,), daemon=True).start()
    return True


def _claude_bin():
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _clean_env():
    env = {k: v for k, v in os.environ.items()
           if not (k == "CLAUDECODE" or k.startswith(("CLAUDE_CODE_", "ORCA_")))}
    env["CSL_SKIP"] = "1"
    return env


def _run(day):
    conn = store.connect()
    try:
        model, timeout = config.get(conn, "daily_model"), config.get(conn, "daily_timeout")
        text, n_sessions, n_turns = digest(conn, day)
        if not n_turns:   # 기록 없는 날은 git fetch·gh 호출(놓친 git 작업)까지 가지 않는다
            _save(conn, day, "error", None, "그날 기록된 대화가 없습니다", n_sessions, n_turns)
            return
        text += git_section(conn) + notes_section(conn)
        os.makedirs(RUN_DIR, exist_ok=True)
        log = os.path.join(RUN_DIR, "last-run.jsonl")   # 진행 과정(stream-json). 문제 생기면 여기부터 본다
        try:
            with open(log, "w") as lf:
                r = subprocess.run(
                    [_claude_bin(), "-p", INSTRUCTION.format(day=day), "--model", model,
                     "--no-session-persistence", "--tools", "", "--output-format", "stream-json", "--verbose"],
                    input=text, stdout=lf, stderr=subprocess.PIPE, text=True, timeout=timeout,
                    cwd=RUN_DIR, env=_clean_env(),
                )
        except subprocess.TimeoutExpired:
            _save(conn, day, "error", None, "%d초 안에 끝나지 않았습니다" % timeout, n_sessions, n_turns)
            return
        except (OSError, subprocess.SubprocessError) as e:
            _save(conn, day, "error", None, "claude 실행 실패: %s" % e, n_sessions, n_turns)
            return
        result = None
        with open(log) as lf:
            for line in lf:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "result":
                    result = d
        out = ((result or {}).get("result") or "").strip()
        if r.returncode != 0 or not out or (result or {}).get("is_error"):
            _save(conn, day, "error", None, ((r.stderr or out or "").strip()[-400:] or "빈 응답"), n_sessions, n_turns)
            return
        _save(conn, day, "done", store.mask(out), None, n_sessions, n_turns)
    finally:
        conn.close()
        with _lock:
            _running.discard(day)


def _save(conn, day, status, content, error, n_sessions, n_turns):
    with conn:
        conn.execute(
            "INSERT INTO daily_reports (day, status, content, error, model, created_at, sessions, turns)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET status=excluded.status,"
            " content=COALESCE(excluded.content, daily_reports.content), error=excluded.error, model=excluded.model,"
            " created_at=excluded.created_at, sessions=excluded.sessions, turns=excluded.turns",
            (day, status, content, error, config.get(conn, "daily_model"), store.now(), n_sessions, n_turns),
        )


# ---------------------------------------------------------------- 매일 아침 자동 정리
def _auto_check():
    now = datetime.now()
    if now.hour < config.get_free("daily_hour"):
        return
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = store.connect()
    try:
        if not store.get_setting(conn, "auto_daily", True):
            return
        # 하루에 한 번만 시도(실패해도 같은 날 다시 돌지 않게)
        if store.get_setting(conn, "auto_daily_tried", None) == yesterday:
            return
        row = conn.execute("SELECT status FROM daily_reports WHERE day=?", (yesterday,)).fetchone()
        start_ts, end_ts = day_bounds(yesterday)
        active = conn.execute("SELECT 1 FROM turns WHERE COALESCE(prompt_at, response_at) >= ?"
                              " AND COALESCE(prompt_at, response_at) < ? LIMIT 1", (start_ts, end_ts)).fetchone()
        with conn:
            store.set_setting(conn, "auto_daily_tried", yesterday)
    finally:
        conn.close()
    if active and not (row and row[0] == "done"):
        start(yesterday)


def start_scheduler():
    def loop():
        while True:
            try:
                _auto_check()
            except Exception:  # noqa: BLE001 - 스케줄러가 서버를 죽이면 안 된다
                pass
            try:
                from csl import connectors
                connectors.tick()       # 커넥터 수집(인스턴스마다 정한 간격)
            except Exception:  # noqa: BLE001
                pass
            try:
                from csl import brain
                brain.ensure_name()     # 브레인 세션 이름을 Argus Brain 으로
                brain.auto_tick()       # 브레인의 자동 슬랙 확인(켜 둔 경우만)
            except Exception:  # noqa: BLE001
                pass
            try:
                store.maybe_cleanup()   # 하루 한 번 오래된 기록 정리
            except Exception:  # noqa: BLE001
                pass
            time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()
