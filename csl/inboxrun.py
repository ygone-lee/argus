"""인박스 정리 일꾼: 자동 정리·「지금 정리」를 브레인 대화가 아니라 따로 도는 `claude -p` 로.

브레인 대화창에 「인박스 정리해줘 (자동)」를 넣으면 사용자의 대화 중간에 끼어들고 브레인의 대화 기억에도 쌓였다.
일꾼은 브레인 폴더에서 돌아 같은 규칙(brain/CLAUDE.md 의 인박스 절차·카드 형식)을 읽고, 카드는 같은 `./argus inbox` 로
넣는다. 대화 기록은 남기지 않는다(--no-session-persistence — /resume 목록에 섞이지 않게).

- 회차마다 inbox_runs 에 시작·끝·상태·새 카드 수·마지막 보고(요약)를 남긴다. 보드 인박스 맨 위 「정리 결과」 줄,
  브레인의 `./argus inbox runs` 가 읽는다.
- 일꾼은 인박스 명령과 슬랙 읽기만 쓴다. 브레인 폴더 설정은 `./argus` 전체를 허용하므로 세션을 움직이는 명령을 차단 목록으로
  막고, 서버도 일꾼의 세션 ID(RUN_SIDS)로 온 요청은 막는다(server.brain_guard). 슬랙·메일 본문의 지시로 다른 세션이
  움직이지 않게.
- 한 번에 하나만 돈다. 훅(보드 기록·브레인 명령 알림)은 끈다(CSL_SKIP).
"""
import json
import subprocess
import threading
import uuid

from csl import config, store
from csl.daily import _claude_bin, _clean_env

PROMPT = "인박스 정리해줘 (자동)"   # brain.CHECK_PROMPT 와 같은 글 — 서버 가드가 자동 정리 차례로 알아본다
TIMEOUT = 900
STALE = 20 * 60   # 이보다 오래 「진행 중」이면 죽은 것으로 보고 새로 돌린다
RUN_SIDS = set()  # 지금 도는 일꾼의 Claude 세션 ID — 이 ID 로 온 「세션을 움직이는」 요청은 서버가 막는다
_lock = threading.Lock()

SYSTEM = ("너는 사용자와 대화하는 브레인이 아니라, 보드가 따로 돌리는 인박스 정리 일꾼이다. 이 폴더 CLAUDE.md 의 「인박스 절차」만 한다 "
          "— 카드를 만들고·고치고·닫고 처리한 수집 항목을 표시한다. 다른 세션에 보내기·종료·새 세션·위임 같은 세션을 움직이는 일은 하지 "
          "않는다(막혀 있다). 사용자에게 묻지 않는다 — 애매하면 규칙대로 「내가 할 일」 카드로 남긴다. 마지막 답이 정리 보고로 그대로 "
          "저장돼 보드에 보인다: 첫 줄에 「새 카드 N개 — 내가 할 일 N · 세션 작업 N」, 이어서 급한 것과 참고 몇 건을 짧게.")
ALLOW = ["Read", "Grep", "Glob", "Edit(./inbox/**)", "Write(./inbox/**)",
         "Bash(./argus inbox:*)", "Bash(./argus sessions:*)", "Bash(./argus session:*)", "Bash(./argus calendar:*)",
         "Bash(./argus search:*)", "Bash(./argus notes:*)",
         "mcp__claude_ai_Slack__slack_search_public_and_private", "mcp__claude_ai_Slack__slack_search_public",
         "mcp__claude_ai_Slack__slack_search_channels", "mcp__claude_ai_Slack__slack_search_users",
         "mcp__claude_ai_Slack__slack_read_thread", "mcp__claude_ai_Slack__slack_read_channel",
         "mcp__claude_ai_Slack__slack_read_user_profile"]
# 브레인 폴더 설정의 「./argus 전체 허용」을 넘어서는 차단 — 세션을 움직이거나 바꾸는 명령, 카드 위임
DENY = ["Bash(./argus %s:*)" % c for c in (
    "send", "remote", "quit", "archive", "unarchive", "new", "pin", "unpin", "rename", "stop", "model", "perm", "rewind",
    "answer", "focus", "switch", "attach", "close-branch", "agents", "settings", "open", "note", "inbox delegate")] + [
    "WebFetch", "NotebookEdit", "mcp__claude_ai_Slack__slack_send_message", "mcp__claude_ai_Slack__slack_send_message_draft",
    "mcp__claude_ai_Slack__slack_schedule_message", "mcp__claude_ai_Slack__slack_add_reaction"]


def running(conn):
    return conn.execute("SELECT id FROM inbox_runs WHERE status='running' AND started_at > ?", (store.now() - STALE,)).fetchone()


def start(trigger="manual"):
    """일꾼을 하나 띄운다. 이미 도는 중이면 None, 아니면 회차 id."""
    with _lock:
        conn = store.connect()
        try:
            if running(conn):
                return None
            with conn:
                conn.execute("UPDATE inbox_runs SET status='error', error='끝나지 않아 버림' WHERE status='running'")
                rid = conn.execute("INSERT INTO inbox_runs (started_at, trigger, status, model) VALUES (?,?,?,?)",
                                   (store.now(), trigger, "running", config.get(conn, "brain_model") or "sonnet")).lastrowid
                if trigger == "auto":
                    store.set_setting(conn, "brain_slack_auto_at", store.now())
        finally:
            conn.close()
    threading.Thread(target=_run, args=(rid,), daemon=True).start()
    return rid


def _run(rid):
    from csl import brain   # brain 이 focus 를 부르므로 늦게
    sid = str(uuid.uuid4())
    RUN_SIDS.add(sid)
    conn = store.connect()
    status, summary, err, cost = "error", None, None, None
    try:
        model = conn.execute("SELECT model FROM inbox_runs WHERE id=?", (rid,)).fetchone()[0]
        cmd = [_claude_bin(), "-p", PROMPT, "--model", model, "--session-id", sid, "--no-session-persistence",
               "--output-format", "json", "--permission-mode", "dontAsk", "--append-system-prompt", SYSTEM,
               "--allowedTools", *ALLOW, "--disallowedTools", *DENY]
        try:
            r = subprocess.run(cmd, cwd=brain.brain_dir(), env=_clean_env(), capture_output=True, text=True,
                               timeout=TIMEOUT, stdin=subprocess.DEVNULL)
            try:
                d = json.loads(r.stdout or "{}")
            except ValueError:
                d = {}
            summary = (d.get("result") or "").strip() or None
            cost = d.get("total_cost_usd")
            if r.returncode == 0 and not d.get("is_error") and summary:
                status = "done"
            else:
                err = (summary or r.stderr or r.stdout or "실패").strip()[:500]
        except subprocess.TimeoutExpired:
            err = "%d초 안에 끝나지 않았습니다" % TIMEOUT
        except OSError as e:
            err = "claude 를 실행하지 못했습니다: %s" % e
    finally:
        RUN_SIDS.discard(sid)
        try:
            with conn:
                started = conn.execute("SELECT started_at FROM inbox_runs WHERE id=?", (rid,)).fetchone()[0]
                new = conn.execute("SELECT COUNT(*), SUM(urgency='high') FROM inbox WHERE created_at >= ?", (started,)).fetchone()
                conn.execute("UPDATE inbox_runs SET finished_at=?, status=?, summary=?, error=?, new_cards=?, new_high=?, cost=?"
                             " WHERE id=?", (store.now(), status, store.mask(summary or "", private=False) or None, err,
                                             new[0] or 0, new[1] or 0, cost, rid))
        finally:
            conn.close()


def runs(conn, limit=10):
    return [dict(r) for r in conn.execute("SELECT * FROM inbox_runs ORDER BY id DESC LIMIT ?", (limit,))]
