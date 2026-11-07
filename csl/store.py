"""Argus 저장소: SQLite 스키마, 민감정보 마스킹, git·기록 파일 해석.

훅(hook.py)·서버(server.py)·과거 기록 가져오기(backfill.py)가 함께 쓴다.
macOS 기본 /usr/bin/python3(3.9)에서도 돌도록 표준 라이브러리만 쓴다.
"""
import json
import os
import re
import sqlite3
import subprocess
import time

DATA_DIR = os.path.expanduser(os.environ.get("CSL_DATA_DIR", "~/.claude/session-log"))
DB_PATH = os.path.join(DATA_DIR, "board.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
# 이 날수 넘게 아무 일도 없던 세션과 그 대화는 지운다(고정했거나 남은 넥스트 액션이 있으면 둔다)
# 스키마를 바꾸면 올린다. 연결할 때마다 마이그레이션을 다시 확인하지 않도록(훅은 도구 호출마다 연결한다)
SCHEMA_VERSION = 24
PORT = int(os.environ.get("CSL_PORT", "7799"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    cwd             TEXT,
    project_root    TEXT,
    project_name    TEXT,
    worktree        TEXT,
    branch          TEXT,
    transcript_path TEXT,
    title           TEXT,
    title_source    TEXT,
    recap           TEXT,
    recap_at        REAL,
    state           TEXT,
    state_detail    TEXT,
    state_at        REAL,
    started_at      REAL,
    last_activity   REAL,
    last_seen_at    REAL DEFAULT 0,
    ended_at        REAL,
    archived        INTEGER DEFAULT 0,
    pinned          INTEGER DEFAULT 0,
    source          TEXT DEFAULT 'hook'
);
CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    prompt_id   TEXT,
    prompt      TEXT,
    prompt_at   REAL,
    response    TEXT,
    response_at REAL,
    status      TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, prompt_at);
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    text        TEXT NOT NULL,
    done        INTEGER DEFAULT 0,
    created_at  REAL,
    updated_at  REAL,
    done_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    # WAL 에선 NORMAL 이어도 DB 가 깨지지 않는다(커밋마다 하던 디스크 동기화를 체크포인트 때로). 대신 OS 가 죽거나
    # 전원이 나가면 마지막 몇 건을 잃을 수 있다 — 로컬 기록이라 받아들인다
    conn.execute("PRAGMA synchronous=NORMAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        # 버전을 올린 직후 훅 여러 개가 동시에 처음 연결해도 한 프로세스만 옮긴다: 쓰기 잠금을 먼저 잡고 다시 확인
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                _migrate(conn)
                conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return conn


def _migrate(conn):
    # executescript 는 시작할 때 COMMIT 을 해 위의 잠금을 풀어 버린다 — 문장마다 실행한다(SCHEMA 에 따옴표 속 ; 없음)
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "terminal" not in cols:   # 세션을 띄운 터미널 정보(JSON) — 「터미널로 이동」용
        conn.execute("ALTER TABLE sessions ADD COLUMN terminal TEXT")
    if "remote" not in cols:     # Remote Control 세션 ID(연결돼 있으면). 훅마다 환경 변수로 갱신
        conn.execute("ALTER TABLE sessions ADD COLUMN remote TEXT")
    for col, decl in (("model", "TEXT"), ("effort", "TEXT"), ("model_switch_at", "REAL")):
        if col not in cols:   # 세션 모델·effort, 보드가 모델 전환을 요청한 시각(확인 창 건너뛰기용)
            conn.execute("ALTER TABLE sessions ADD COLUMN %s %s" % (col, decl))
    if "rc_requested_at" not in cols:   # 보드가 이 세션에 /remote-control 을 보낸 시각(두 번 보내지 않게)
        conn.execute("ALTER TABLE sessions ADD COLUMN rc_requested_at REAL")
    if "continued_from" not in cols:   # 이 세션이 이어받은(--resume … --fork-session) 이전 세션 ID
        conn.execute("ALTER TABLE sessions ADD COLUMN continued_from TEXT")
    # 갈래(/branch·/fork): 원본은 continued_from, 종류(branch·fork·moved), 갈라진 시각. bg=백그라운드 세션(터미널 탭 없음)
    for col, decl in (("branch_kind", "TEXT"), ("branch_at", "REAL"), ("bg", "INTEGER DEFAULT 0"),
                      ("permission_mode", "TEXT")):   # 권한 모드(훅 입력 permission_mode — auto·default·acceptEdits·plan …)
        if col not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN %s %s" % (col, decl))
    if "team_agent" not in cols:   # 에이전트 팀의 팀원 이름(--agent-name). 팀장은 continued_from, branch_kind='team'
        conn.execute("ALTER TABLE sessions ADD COLUMN team_agent TEXT")
    if "link_checked" not in cols:     # 이어받은 세션인지 프로세스 명령줄로 한 번 확인했는지
        conn.execute("ALTER TABLE sessions ADD COLUMN link_checked INTEGER DEFAULT 0")
    if "ask" not in cols:   # 터미널에 떠 있는 질문 창(AskUserQuestion)의 질문·선택지(JSON) — 보드에서 답하기용
        conn.execute("ALTER TABLE sessions ADD COLUMN ask TEXT")
    # 컴팩트(/compact·자동 요약) 시작 시각·계기(manual/auto), 지난번 컴팩트에 걸린 시간(ms)
    for col, decl in (("compact_at", "REAL"), ("compact_trigger", "TEXT"), ("compact_ms", "INTEGER")):
        if col not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN %s %s" % (col, decl))
    if "agent" not in cols:   # 어떤 에이전트의 세션인지: claude(Claude Code) · codex(OpenAI Codex CLI·데스크톱 앱)
        conn.execute("ALTER TABLE sessions ADD COLUMN agent TEXT DEFAULT 'claude'")
    if "extras" not in {r[1] for r in conn.execute("PRAGMA table_info(turns)")}:
        # 마지막 메시지 앞의 중간 메시지·보낸 파일·끼어든 알림(JSON). 훅은 마지막 메시지만 주므로 기록 파일에서 읽는다
        conn.execute("ALTER TABLE turns ADD COLUMN extras TEXT")
    ncols = {r[1] for r in conn.execute("PRAGMA table_info(notes)")}
    if "images" not in ncols:   # 넥스트 액션에 붙인 이미지 경로(JSON)
        conn.execute("ALTER TABLE notes ADD COLUMN images TEXT")
    if "queued_at" not in ncols:   # 「끝나면 보내기」 예약 시각(csl/sendqueue.py) — 응답이 끝나면 건 순서대로 보낸다
        conn.execute("ALTER TABLE notes ADD COLUMN queued_at REAL")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    # 커넥터(csl/connectors)가 모은 항목. 브레인이 카드로 만들면 done=1 로 바꾸고 본문을 지운다
    conn.execute("""CREATE TABLE IF NOT EXISTS feed_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, instance TEXT, source TEXT, account TEXT, kind TEXT,
        title TEXT, author TEXT, text TEXT, link TEXT, ts REAL, created_at REAL, done INTEGER DEFAULT 0)""")
    # (아래에서 컬럼을 더하기 전에 만들어야 한다 — 빈 DB 에서 처음 시작할 때) 슬랙 인박스: 브레인(brain) 세션이 슬랙 언급을 읽고 남긴 카드. 원문은 두지 않고 요약·링크만
    conn.execute("""CREATE TABLE IF NOT EXISTS inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, slack_at REAL, channel TEXT, author TEXT,
        permalink TEXT, summary TEXT, urgency TEXT, sensitive INTEGER DEFAULT 0,
        rec_type TEXT, rec_session TEXT, rec_folder TEXT, rec_model TEXT, rec_reason TEXT, prompt TEXT,
        status TEXT DEFAULT 'new', created_at REAL, updated_at REAL)""")
    icols = {r[1] for r in conn.execute("PRAGMA table_info(inbox)")}
    if "source" not in icols:   # 카드의 출처(slack·gmail …)
        conn.execute("ALTER TABLE inbox ADD COLUMN source TEXT")
    for col in ("todo", "reply_draft"):   # 「내가 할 일」 카드: 할 일 한 줄, 답장 초안
        if col not in icols:
            conn.execute("ALTER TABLE inbox ADD COLUMN %s TEXT" % col)
    # 카드 맡기기(csl/delegate.py): 맡긴 세션·시각·진행 단계, 마지막으로 정리한 턴
    for col, decl in (("delegated_session", "TEXT"), ("delegated_at", "REAL"), ("stage", "TEXT"), ("stage_turn", "INTEGER")):
        if col not in icols:
            conn.execute("ALTER TABLE inbox ADD COLUMN %s %s" % (col, decl))
    # 브레인이 다른 세션에 보낸 것 — 그 세션이 답하면(또는 확인을 기다리면) 브레인에게 알려 준다(csl/delegate.py)
    conn.execute("""CREATE TABLE IF NOT EXISTS brain_watch (
        id INTEGER PRIMARY KEY AUTOINCREMENT, brain_sid TEXT, target_sid TEXT, sent_at REAL, status TEXT DEFAULT 'open',
        notified_at REAL, note TEXT)""")
    # 인박스 정리 일꾼(csl/inboxrun.py)의 회차 — 시작·끝·상태·새 카드 수·마지막 보고(요약)
    conn.execute("""CREATE TABLE IF NOT EXISTS inbox_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL, finished_at REAL, trigger TEXT, status TEXT, model TEXT,
        summary TEXT, error TEXT, new_cards INTEGER, new_high INTEGER, cost REAL)""")
    # 보낸 글 — 받은 쪽이 다른 세션으로 기록되면(같은 탭에 다른 세션이 떠 있는 등) 그 글로 진짜 받은 세션을 찾는다
    if "prompt" not in {r[1] for r in conn.execute("PRAGMA table_info(brain_watch)")}:
        conn.execute("ALTER TABLE brain_watch ADD COLUMN prompt TEXT")
    # 카드 기록: 만들어짐·고쳐짐·상태 변경·맡김·세션 진행·요약 — 카드가 어떻게 바뀌어 왔는지
    conn.execute("""CREATE TABLE IF NOT EXISTS inbox_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, inbox_id INTEGER, at REAL, kind TEXT, text TEXT, session_id TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_log ON inbox_log(inbox_id, at)")
    if "thread_key" not in icols:   # 같은 대화(슬랙 스레드·하루치 DM)의 카드를 묶는 키 — 칸반에서 한 묶음으로
        conn.execute("ALTER TABLE inbox ADD COLUMN thread_key TEXT")
    if "chat_session" not in icols:   # 카드 대화(csl/cardchat.py)의 Claude Code 세션 ID(--resume 용)
        conn.execute("ALTER TABLE inbox ADD COLUMN chat_session TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS inbox_chat (
        id INTEGER PRIMARY KEY AUTOINCREMENT, inbox_id INTEGER, role TEXT, text TEXT, model TEXT, at REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_chat ON inbox_chat(inbox_id, id)")
    conn.execute("CREATE TABLE IF NOT EXISTS daily_reports (day TEXT PRIMARY KEY, status TEXT, content TEXT,"
                 " error TEXT, model TEXT, created_at REAL, sessions INTEGER, turns INTEGER)")
    _ensure_fts(conn)
    _ensure_events(conn)
    # 곁질문(/btw): Claude Code 는 훅도 기록도 남기지 않고 터미널 위 창에만 답을 보여 준다 — 보드가 화면에서 읽어 둔다
    conn.execute("""CREATE TABLE IF NOT EXISTS asides (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, question TEXT, answer TEXT, status TEXT,
        at REAL, answered_at REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asides_session ON asides(session_id, at)")
    # 턴을 「시각 순」으로 읽는 쿼리(세션 상세·마지막 응답·맡긴 세션 감시·하루 정리)가 세션의 턴 전부를 읽어 정렬하지 않게.
    # 식은 쿼리와 글자 그대로 같아야 쓰인다: COALESCE(prompt_at, response_at)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_at ON turns(session_id, COALESCE(prompt_at, response_at))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_resp ON turns(session_id, response_at) WHERE response IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_at ON turns(COALESCE(prompt_at, response_at), session_id)")
    # 예전 슬랙 수집기의 원문 표·설정 — 커넥터로 옮겨진 DB 에서만 지운다(옮기기 전이면 그대로)
    if conn.execute("SELECT 1 FROM settings WHERE key='connectors_migrated' AND value='true'").fetchone():
        conn.execute("DROP TABLE IF EXISTS slack_raw")
        conn.execute("DELETE FROM settings WHERE key IN ('slack_feeds', 'slack_feed_state', 'slack_polled_at', 'slack_poll_minutes')")


EVENT_KINDS = ("response", "waiting", "question", "error", "compact", "start", "end")


def _ensure_events(conn):
    """세션 활동 기록(활동 화면)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
        return
    conn.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, kind TEXT, text TEXT, turn_id INTEGER, at REAL)""")
    conn.execute("CREATE INDEX idx_events_at ON events(at)")
    conn.execute("CREATE INDEX idx_events_session ON events(session_id, at)")


_SLACK_MSG = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")


def thread_key(permalink, channel_label="", ts=None):
    """인박스 카드를 묶는 키. 슬랙 스레드면 채널+스레드 시작, 스레드 없는 DM·그룹 DM 은 채널+그날, 그 밖은 None(혼자)."""
    m = _SLACK_MSG.search(permalink or "")
    if not m:
        return None
    channel, msg_ts = m.group(1), m.group(2) + "." + m.group(3)
    th = re.search(r"thread_ts=(\d+\.\d+)", permalink)
    if th:
        return "slack:%s:%s" % (channel, th.group(1))
    if (channel_label or "").startswith(("DM", "그룹 DM")) or channel.startswith("D"):
        return "slack:%s:day:%s" % (channel, time.strftime("%Y-%m-%d", time.localtime(float(ts or msg_ts))))
    return "slack:%s:%s" % (channel, msg_ts)   # 스레드의 첫 메시지 — 뒤에 달린 답글 카드와 같은 키


def card_log(conn, inbox_id, kind, text=None, session_id=None):
    """인박스 카드 기록 한 줄(카드 펼침의 「기록」)."""
    conn.execute("INSERT INTO inbox_log (inbox_id, at, kind, text, session_id) VALUES (?,?,?,?,?)",
                 (inbox_id, now(), kind, mask((text or "")[:1000]) or None, session_id))


def add_event(conn, sid, kind, text=None, turn_id=None, dedupe=0):
    """활동 하나 기록. dedupe 초 안에 같은 세션·종류가 있으면 넘긴다(권한 창은 알림·요청 훅이 둘 다 온다)."""
    t = now()
    if dedupe and conn.execute("SELECT 1 FROM events WHERE session_id=? AND kind=? AND at>=?",
                               (sid, kind, t - dedupe)).fetchone():
        return
    conn.execute("INSERT INTO events (session_id, kind, text, turn_id, at) VALUES (?,?,?,?,?)",
                 (sid, kind, mask((text or "")[:400], private=kind != "response") or None, turn_id, t))


FTS_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS turns_fts_ai AFTER INSERT ON turns BEGIN"
    " INSERT INTO turns_fts(rowid, prompt, response) VALUES (new.id, new.prompt, new.response); END",
    "CREATE TRIGGER IF NOT EXISTS turns_fts_ad AFTER DELETE ON turns BEGIN"
    " INSERT INTO turns_fts(turns_fts, rowid, prompt, response) VALUES ('delete', old.id, old.prompt, old.response); END",
    "CREATE TRIGGER IF NOT EXISTS turns_fts_au AFTER UPDATE OF prompt, response ON turns BEGIN"
    " INSERT INTO turns_fts(turns_fts, rowid, prompt, response) VALUES ('delete', old.id, old.prompt, old.response);"
    " INSERT INTO turns_fts(rowid, prompt, response) VALUES (new.id, new.prompt, new.response); END",
)


def _ensure_fts(conn):
    """요청·응답 전문 검색 색인(FTS5). 세 글자 단위(trigram)로 잘라 한국어·일본어도 띄어쓰기와 상관없이 찾는다.

    turns 를 그대로 가리키는 색인(external content)이라 본문을 두 벌 두지 않고, 트리거가 쓰기·지우기를 따라간다.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='turns_fts'").fetchone():
        return
    conn.execute("CREATE VIRTUAL TABLE turns_fts USING fts5(prompt, response, content='turns', content_rowid='id',"
                 " tokenize='trigram')")
    for sql in FTS_TRIGGERS:
        conn.execute(sql)
    conn.execute("INSERT INTO turns_fts(turns_fts) VALUES ('rebuild')")   # 이미 쌓인 대화도 색인


# ---------------------------------------------------------------- 오래된 기록 정리

def cleanup(conn, t=None):
    """쌓이기만 하는 기록을 지운다. 지운 개수(dict)를 돌려준다.

    - 요청도 넥스트 액션도 없는 빈 세션(고정 안 함): 끝난 뒤 하루 지나면. 끝나지 않은 빈 세션은 지우지 않는다 —
      /clear 로 새로 시작해 아직 아무것도 묻지 않은 세션(브레인 등)이 하루 뒤 지워져 꺼진 것처럼 보였다
    - 마지막 활동이 설정 retention_days(기본 90일, CSL_RETENTION_DAYS) 넘은 세션과 그 대화: 고정했거나 남은 넥스트 액션이 있으면 둔다
    - 완료한 지 30일 지난 넥스트 액션
    - uploads/ 의 30일 지난 이미지 중 넥스트 액션이 가리키지 않는 것
    - 끝난(완료·무시) 인박스 카드와 그 기록·카드 대화: 마지막으로 바뀐 지 inbox_days(기본 7일) — 슬랙·메일 요약이라 오래 두지 않는다
    - 지워진 세션의 곁질문·상태줄 파일, 끝난 지 7일 지난 브레인 지켜보기
    Claude Code 의 기록 파일(~/.claude/projects)은 건드리지 않는다.
    """
    from csl import config   # 설정 창의 보관 기간
    t = t or now()
    keep_days, notes_days, uploads_days = (config.get(conn, "retention_days"), config.get(conn, "done_notes_days"),
                                           config.get(conn, "uploads_days"))
    out = {}
    out["empty_sessions"] = conn.execute(
        "DELETE FROM sessions WHERE pinned=0 AND state='ended' AND last_activity < ?"
        " AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=sessions.session_id)"
        " AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.session_id=sessions.session_id)",
        (t - 86400,)).rowcount
    old = [r[0] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE pinned=0 AND last_activity < ?"
        " AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.session_id=sessions.session_id AND n.done=0)",
        (t - keep_days * 86400,))]
    out["old_sessions"], out["old_turns"] = len(old), 0
    for i in range(0, len(old), 200):
        chunk = old[i:i + 200]
        marks = ",".join("?" * len(chunk))
        out["old_turns"] += conn.execute("DELETE FROM turns WHERE session_id IN (%s)" % marks, chunk).rowcount
        conn.execute("DELETE FROM notes WHERE session_id IN (%s)" % marks, chunk)
        conn.execute("DELETE FROM sessions WHERE session_id IN (%s)" % marks, chunk)
    out["done_notes"] = conn.execute("DELETE FROM notes WHERE done=1 AND done_at < ?", (t - notes_days * 86400,)).rowcount
    out["events"] = conn.execute("DELETE FROM events WHERE at < ? OR session_id NOT IN (SELECT session_id FROM sessions)",
                                 (t - config.get(conn, "events_days") * 86400,)).rowcount
    out["inbox"] = conn.execute("DELETE FROM inbox WHERE status IN ('done', 'ignored') AND COALESCE(updated_at, created_at) < ?",
                                (t - config.get(conn, "inbox_days") * 86400,)).rowcount
    out["inbox_log"] = conn.execute("DELETE FROM inbox_log WHERE inbox_id NOT IN (SELECT id FROM inbox)").rowcount
    out["inbox_chat"] = conn.execute("DELETE FROM inbox_chat WHERE inbox_id NOT IN (SELECT id FROM inbox)").rowcount
    out["asides"] = conn.execute("DELETE FROM asides WHERE session_id NOT IN (SELECT session_id FROM sessions)").rowcount
    out["brain_watch"] = conn.execute("DELETE FROM brain_watch WHERE status != 'open' AND sent_at < ?", (t - 7 * 86400,)).rowcount
    out["inbox_runs"] = conn.execute("DELETE FROM inbox_runs WHERE started_at < ?", (t - config.get(conn, "inbox_days") * 86400,)).rowcount
    live = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
    out["status_files"] = 0
    if os.path.isdir(STATUS_DIR):   # 상태줄 파일: 기록에 없는 세션 것(하루 넘게 안 바뀐 것만 — 사용량은 가장 새 파일에서 읽는다)
        for name in os.listdir(STATUS_DIR):
            path = os.path.join(STATUS_DIR, name)
            try:
                stale = os.path.getmtime(path) < t - 86400
                # 기록에 없는 세션 것, 그리고 상태줄이 쓰다 남긴 임시 파일
                if stale and (name.endswith(".tmp") or (name.endswith(".json") and name[:-5] not in live)):
                    os.remove(path)
                    out["status_files"] += 1
            except OSError:
                pass
    keep = set()
    for (imgs,) in conn.execute("SELECT images FROM notes WHERE images IS NOT NULL"):
        try:
            keep.update(os.path.basename(p) for p in json.loads(imgs))
        except ValueError:
            pass
    out["uploads"] = 0
    if os.path.isdir(UPLOAD_DIR):
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            if name not in keep and os.path.isfile(path) and os.path.getmtime(path) < t - uploads_days * 86400:
                os.remove(path)
                out["uploads"] += 1
    return out


def maybe_cleanup():
    """하루 한 번 정리하고, 많이 지웠으면 파일 크기도 줄인다(VACUUM). 서버 스케줄러가 부른다."""
    conn = connect()
    try:
        today = time.strftime("%Y-%m-%d")
        if get_setting(conn, "cleanup_day") == today:
            return None
        with conn:
            out = cleanup(conn)
            set_setting(conn, "cleanup_day", today)
        if out["old_turns"] + out["empty_sessions"] + out["done_notes"] > 300:
            conn.execute("VACUUM")
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- 마스킹

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]
# 사용자가 <private>…</private> 로 감싼 부분: Claude 에게는 그대로 가지만 기록(보드·검색·브레인·하루 정리)에는 남기지 않는다.
# 닫는 태그가 없으면 끝까지
_PRIVATE = re.compile(r"(?is)<private>.*?(?:</private>|\Z)")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=\-]{16,}")
# 키 이름은 앞뒤에 붙은 말까지(DATABASE_PASSWORD·XAI_API_KEY·AWS_SECRET_ACCESS_KEY·clientSecret). 뒤에 붙는 말은 _·- 로
# 이어진 것만 — max_tokens=128000 처럼 「tokens」는 비밀값이 아니다. "hasToken":true} 같은 참·거짓·null 값도 남긴다
_KEY_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_\-]*?(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|cookie)"
    r"(?:[_\-][A-Za-z0-9]+)*[\"']?\s*[:=]\s*[\"']?)(?!(?:true|false|null|none)\b)([^\s\"',;]{6,})"
)
_BASIC = re.compile(r"(?i)(authorization\s*:\s*basic\s+)[A-Za-z0-9+/=]{8,}")
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]{3,})(@)")   # postgresql://user:비밀번호@host


def mask(text, private=True):
    """비밀값을 가린다. private=True 면 <private>…</private> 도(닫지 않았으면 끝까지 — 사용자가 쓴 글).
    Claude 응답은 private=False: 응답이 <private> 라는 말을 꺼내기만 해도 그 뒤 본문이 통째로 지워지던 것을 막는다."""
    if not text:
        return text
    if private:
        text = _PRIVATE.sub("[private]", text)
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[MASKED]", text)
    text = _BEARER.sub(r"\1[MASKED]", text)
    text = _BASIC.sub(r"\1[MASKED]", text)
    text = _URL_CRED.sub(r"\1[MASKED]\3", text)
    text = _KEY_VALUE.sub(r"\1[MASKED]", text)
    return text



def mask_json(value, private=True):
    """JSON 값 안의 글자열만 가린다. 직렬화한 글에 정규식을 걸면 이스케이프(\\" 등)를 잘라 JSON 이 깨질 수 있다."""
    if isinstance(value, str):
        return mask(value, private)
    if isinstance(value, list):
        return [mask_json(v, private) for v in value]
    if isinstance(value, dict):
        return {k: mask_json(v, private) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- git

def git_info(cwd):
    """cwd 의 프로젝트 루트(워크트리는 원본 저장소)·워크트리 경로·브랜치."""
    info = {"project_root": cwd, "project_name": os.path.basename(cwd.rstrip("/")) or cwd,
            "worktree": None, "branch": None}
    if not cwd or not os.path.isdir(cwd):
        # 지워진 워크트리: <프로젝트>/.claude/worktrees/<이름> 이면 원래 프로젝트로 묶는다
        if cwd and "/.claude/worktrees/" in cwd:
            root = cwd.split("/.claude/worktrees/", 1)[0]
            info.update(project_root=root, project_name=os.path.basename(root), worktree=cwd)
        return info
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return info
    lines = out.stdout.strip().splitlines()
    if out.returncode != 0 or len(lines) < 2:
        return info
    toplevel = lines[0]
    common = lines[1]
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(cwd, common))
    root = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    info["project_root"] = root
    info["project_name"] = os.path.basename(root.rstrip("/")) or root
    info["worktree"] = toplevel if os.path.realpath(toplevel) != os.path.realpath(root) else None
    if len(lines) >= 3 and lines[2] != "HEAD":
        info["branch"] = lines[2]
    return info


# ---------------------------------------------------------------- 기록 파일(JSONL)

def fork_origin(path, head_bytes=8 * 1024 * 1024):
    """/branch·/fork 로 갈라져 나온 세션이면 (원본 세션 ID, 갈라진 시각), 아니면 None.

    갈래의 기록 파일 앞부분은 원본에서 복사한 줄들이고 줄마다 forkedFrom {sessionId, messageUuid} 가 붙는다(실측
    2.1.316). 마지막 복사 줄의 원본·시각이 갈라진 곳이다. 복사가 끝나면(forkedFrom 없는 줄이 나오면) 더 읽지 않는다.
    """
    if not path or not os.path.isfile(path):
        return None
    parent = at = None
    try:
        with open(path, "rb") as f:
            read = 0
            for n, raw in enumerate(f):
                read += len(raw)
                if read > head_bytes:
                    break
                if b'"forkedFrom"' not in raw:
                    if parent or n >= 40:   # 복사 줄은 맨 앞에 몰려 있다 — 앞 40줄에 없으면 갈래가 아니다(큰 기록을 끝까지 읽지 않게)
                        break
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                ff = d.get("forkedFrom") or {}
                if ff.get("sessionId"):
                    parent = ff["sessionId"]
                    at = parse_ts(d.get("timestamp")) or at
    except OSError:
        return None
    return (parent, at) if parent else None


def head_uuids(path, n=30, head_bytes=2 * 1024 * 1024):
    """기록 파일 맨 앞 메시지 ID n 개(복사본은 원본의 첫 메시지부터 그대로 시작한다)."""
    out = []
    try:
        with open(path, "rb") as f:
            read = 0
            for raw in f:
                read += len(raw)
                if read > head_bytes or len(out) >= n:
                    break
                m = re.search(rb'"uuid":"([0-9a-f-]{36})"', raw)
                if m:
                    out.append(m.group(1).decode())
    except OSError:
        pass
    return out


_PROMPT_IMGS = {}   # (기록 파일, prompt_id) → {N: (형식, base64)} — 최근 것 몇 개만
_IMG_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def prompt_images(path, prompt_id):
    """요청에 붙인 이미지들 {[Image #N] 의 N: (형식, base64 글)}. 대화 기록의 그 요청(promptId) 사용자 메시지에서 꺼낸다.

    Claude Code 는 요청 글엔 [Image #N] 만 남기고 이미지는 메시지에 base64 로 넣는다(보드에서 보냈든 터미널에 붙였든).
    N 은 imagePasteIds 순서대로, 없으면 1부터. 도구 결과(Read 등) 속 이미지는 tool_result 안에 있어 맨 위 블록만 본다."""
    key = (path, prompt_id)
    if key in _PROMPT_IMGS:
        return _PROMPT_IMGS[key]
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if prompt_id not in line or '"image"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                content = (d.get("message") or {}).get("content")
                if d.get("type") != "user" or d.get("promptId") != prompt_id or not isinstance(content, list):
                    continue
                imgs = [b.get("source") or {} for b in content if isinstance(b, dict) and b.get("type") == "image"]
                imgs = [s for s in imgs if s.get("type") == "base64" and s.get("media_type") in _IMG_TYPES and s.get("data")]
                ids = d.get("imagePasteIds") or []
                for k, s in enumerate(imgs):
                    n = ids[k] if k < len(ids) and isinstance(ids[k], int) else len(out) + 1
                    out[n] = (s["media_type"], s["data"])
    except OSError:
        return {}
    _PROMPT_IMGS[key] = out
    if len(_PROMPT_IMGS) > 24:
        _PROMPT_IMGS.pop(next(iter(_PROMPT_IMGS)))
    return out


def transcript_uuids(path, head_bytes=8 * 1024 * 1024):
    """기록 파일(앞 8MB)의 메시지 ID → 시각. 갈래가 원본의 어느 가지에서 갈라졌는지 맞춰 볼 때 쓴다."""
    out = {}
    try:
        with open(path, "rb") as f:
            read = 0
            for raw in f:
                read += len(raw)
                if read > head_bytes:
                    break
                m = re.search(rb'"uuid":"([0-9a-f-]{36})"', raw)
                if m:
                    t = re.search(rb'"timestamp":"([^"]+)"', raw)
                    out[m.group(1).decode()] = parse_ts(t.group(1).decode()) if t else None
    except OSError:
        pass
    return out


def scan_transcript_meta(path, tail_bytes=4 * 1024 * 1024):
    """기록 파일 끝부분에서 세션 제목(custom/ai)과 최근 recap(away_summary)을 찾는다.

    Claude Code 내부 형식이라 버전에 따라 바뀔 수 있다. 못 찾으면 빈 dict.
    """
    meta = {}
    if not path or not os.path.isfile(path):
        return meta
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return meta
    recap_cmd = False   # 바로 앞 줄이 /recap 명령이었는지 — 그 결과(로컬 명령 출력)도 recap 이다
    for raw in data.splitlines():
        if b'"custom-title"' not in raw and b'"ai-title"' not in raw and b'"away_summary"' not in raw \
                and b"/recap</command-name>" not in raw and not (recap_cmd and b"<local-command-stdout>" in raw):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = d.get("type")
        if b"/recap</command-name>" in raw:
            recap_cmd = True
            continue
        if recap_cmd and b"<local-command-stdout>" in raw:   # 시스템 줄 또는 사용자 메시지 줄
            recap_cmd = False
            c = d.get("content") or (d.get("message") or {}).get("content") or ""
            m = re.search(r"<local-command-stdout>(.*?)</local-command-stdout>", c if isinstance(c, str) else "", re.S)
            if m and m.group(1).strip():
                meta["recap"] = m.group(1).strip()
                meta["recap_at"] = parse_ts(d.get("timestamp"))
            continue
        if t == "custom-title" and d.get("customTitle"):
            meta["custom_title"] = d["customTitle"]
        elif t == "ai-title" and d.get("aiTitle"):
            meta["ai_title"] = d["aiTitle"]
        elif t == "system" and d.get("subtype") == "away_summary" and d.get("content"):
            meta["recap"] = d["content"]
            meta["recap_at"] = parse_ts(d.get("timestamp"))
    return meta


def parse_ts(value):
    if not value:
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", value.strip()):   # 슬랙 ts("1793340707.642139")
        value = float(value)
    if isinstance(value, (int, float)):   # 상태줄 입력은 epoch 초(또는 밀리초)일 수 있다
        return value / 1000.0 if value > 1e11 else float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def apply_transcript_meta(conn, session_id, path, meta=None):
    """meta 를 주면 그걸 쓴다(훅이 쓰기 잠금을 잡기 전에 미리 읽어 둔 것)."""
    meta = scan_transcript_meta(path) if meta is None else meta
    if not meta:
        return
    title = meta.get("custom_title") or meta.get("ai_title")
    row = conn.execute("SELECT title_source FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row and row[0] == "board":
        title = None   # 보드에서만 바꾼 이름은 기록 파일 제목으로 덮어쓰지 않는다
    elif row and row[0] == "custom" and not meta.get("custom_title"):
        title = None   # /rename 한 이름이 읽는 범위(끝 4MB) 밖으로 밀려나도 자동 제목으로 되돌리지 않는다
    if title:
        conn.execute(
            "UPDATE sessions SET title=?, title_source=? WHERE session_id=?",
            (mask(title), "custom" if meta.get("custom_title") else "ai", session_id),
        )
    if meta.get("recap"):
        # 더 새 것만 — 보드에서 /recap 으로 넣은 요약을 그보다 앞선 기록 파일의 요약이 덮지 않게
        conn.execute(
            "UPDATE sessions SET recap=?, recap_at=? WHERE session_id=? AND COALESCE(recap_at, 0) <= COALESCE(?, 0) + 1",
            (mask(meta["recap"]), meta.get("recap_at"), session_id, meta.get("recap_at")),
        )


def tail_custom_title(path, tail_bytes=64 * 1024):
    """기록 파일 맨 끝에서 가장 최근 /rename 이름. /rename 은 훅이 오지 않아 서버가 이걸로 바로 따라간다."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return None
    for raw in reversed(data.splitlines()):
        if b'"custom-title"' in raw:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("type") == "custom-title" and d.get("customTitle"):
                return d["customTitle"]
    return None


def _compact_boundary(raw):
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if d.get("type") != "system" or d.get("subtype") != "compact_boundary":
        return None
    m = d.get("compactMetadata") or {}
    return {"at": parse_ts(d.get("timestamp")), "ms": m.get("durationMs"), "post": m.get("postTokens"),
            "trigger": m.get("trigger")}


def tail_compact_boundary(path, tail_bytes=256 * 1024):
    """기록 파일 끝부분의 가장 최근 컴팩트 경계 {at(끝난 시각), ms, post(요약 뒤 토큰), trigger}."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return None
    for raw in reversed(data.splitlines()):
        if b'"compact_boundary"' in raw:
            b = _compact_boundary(raw)
            if b:
                return b
    return None


TASK_NOTICE = "<task-notification>"
_EXTRA_TEXT_MAX = 4000
_EXTRAS_MAX = 80


def is_task_notice(prompt):
    """백그라운드 작업이 끝났을 때 Claude Code 가 대화에 넣는 알림(요청 훅으로 온다)."""
    return (prompt or "").lstrip().startswith(TASK_NOTICE)


def notice_line(prompt):
    """작업 알림이면 「[백그라운드 작업 끝남 — 사용자 요청 아님] 요약」 한 줄, 아니면 None. 일지·위임 요약·검색이
    이 알림을 사용자가 보낸 요청으로 다루지 않게 한다."""
    if not is_task_notice(prompt):
        return None
    def tag(name):
        m = re.search(r"<%s>([\s\S]*?)</%s>" % (name, name), prompt)
        return m.group(1).strip() if m else ""
    status = {"completed": "끝남", "failed": "실패", "killed": "중단", "stopped": "중단"}.get(tag("status").lower(), tag("status") or "끝남")
    summary = re.sub(r"^Background command ", "", tag("summary"))
    return "[백그라운드 작업 %s — 사용자 요청 아님]%s" % (status, " " + summary if summary else "")


_TS = re.compile(rb'"timestamp":"([^"]+)"')


def _tail_since(path, since, limit=64 * 1024 * 1024):
    """기록 파일 끝에서 since 이전 기록이 처음 나올 때까지 거슬러 올라간 부분(바이트)."""
    size = os.path.getsize(path)
    n = 1024 * 1024
    with open(path, "rb") as f:
        while True:
            start = max(0, size - n)
            f.seek(start)
            data = f.read()
            if start == 0 or n >= limit:
                return data
            m = _TS.search(data, data.find(b"\n") + 1)
            first = parse_ts(m.group(1).decode()) if m else None
            if first is not None and first < since - 10:
                return data
            n *= 2


_BASH_TAG = re.compile(r"<bash-(input|stdout|stderr)>(.*?)</bash-\1>", re.S)


def bash_inputs(path, since):
    """since 뒤에 터미널에서 「!」로 실행한 셸 명령들 [{prompt_id, at, cmd, out}] 시각순.

    「!」 명령은 UserPromptSubmit 훅 없이 기록 파일에만 사용자 메시지로 남는다 —
    「<bash-input>명령</bash-input>」, 이어서 같은 promptId 로 「<bash-stdout>…</bash-stdout><bash-stderr>…」.
    Claude 가 그 결과를 보고 이어 응답하므로, 보드가 이걸 턴으로 만들어야 그 응답이 대화에 붙는다."""
    if not path or not since:
        return []
    try:   # 도구를 쓸 때마다 불릴 수 있다 — 마지막 턴 뒤만, 많아야 16MB
        data = _tail_since(path, since, limit=16 * 1024 * 1024)
    except OSError:
        return []
    if b"<bash-input>" not in data:
        return []
    out = {}
    for raw in data.splitlines():
        if b"<bash-" not in raw or b'"type":"user"' not in raw.replace(b" ", b""):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
            continue
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, str):
            continue
        ts = parse_ts(d.get("timestamp"))
        if not ts or ts <= since:
            continue
        pid = d.get("promptId") or d.get("uuid")
        for kind, body in _BASH_TAG.findall(content):
            x = out.setdefault(pid, {"prompt_id": pid, "at": ts, "cmd": "", "out": ""})
            if kind == "input":
                x["cmd"] = body.strip()
            elif body.strip():
                x["out"] += ("\n" if x["out"] else "") + body.strip()
    return sorted((x for x in out.values() if x["cmd"]), key=lambda x: x["at"])


def bash_prompt(x):
    """「!」 명령 턴의 요청 글: 명령과 그 출력(길면 앞부분)."""
    text = "! " + x["cmd"]
    if x["out"]:
        o = x["out"] if len(x["out"]) <= 4000 else x["out"][:4000] + "\n…"
        text += "\n\n```\n" + o.replace("```", "ˋˋˋ") + "\n```"
    return text


def turn_extras(path, since, final_text, until=None):
    """한 턴에서 마지막 메시지 앞에 나온 것들: 중간 메시지(text), 사용자에게 보낸 파일(files), 끼어든 알림(notice).

    Stop 훅은 마지막 메시지만 준다. 터미널에는 그 앞의 글·SendUserFile 파일도 보이므로 기록 파일에서 채운다.
    since(그 턴 요청 시각) 뒤의 사람 요청부터 읽고, 다음 사람 요청이나 until(응답 시각) 뒤 기록이 나오면 멈춘다.
    작업 도중 끝난 백그라운드 작업 알림은 queued_command 첨부로 남는다. 없으면 None.
    """
    if not path or not since:
        return None
    try:
        data = _tail_since(path, since)
    except OSError:
        return None
    items, seen, started = [], set(), False
    for raw in data.splitlines():
        if b'"timestamp"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        ts = parse_ts(d.get("timestamp"))
        if started and until and ts and ts > until + 2:
            break
        kind = d.get("type")
        att = d.get("attachment") if kind == "attachment" else None
        if started and isinstance(att, dict) and att.get("type") == "queued_command" \
                and is_task_notice(att.get("prompt") if isinstance(att.get("prompt"), str) else ""):
            items.append({"type": "notice", "text": mask(att["prompt"][:2000])})
            continue
        if kind == "user" and not d.get("isMeta"):
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict) \
                    and content[0].get("type") == "tool_result":
                continue
            origin = (d.get("origin") or {}).get("kind")
            if origin == "task-notification":
                if not started and ts and ts >= since - 3:
                    started = True   # 쉬던 세션을 알림이 깨워 시작된 턴
                    continue
                if started:
                    text = content if isinstance(content, str) else " ".join(
                        x.get("text", "") for x in content or [] if isinstance(x, dict))
                    items.append({"type": "notice", "text": mask(text[:2000])})
                continue
            if origin not in (None, "human"):
                continue
            if not started:
                started = bool(ts and ts >= since - 3)
            elif ts and ts > since + 3:
                break   # 다음 요청
            continue
        if not started or kind != "assistant":
            continue
        msg = d.get("message") or {}
        for i, block in enumerate(msg.get("content") or []):
            key = (msg.get("id"), i, block.get("type"))
            if key in seen:
                continue
            seen.add(key)
            if block.get("type") == "text" and block.get("text", "").strip():
                items.append({"type": "text", "text": mask(block["text"].strip()[:_EXTRA_TEXT_MAX])})
            elif block.get("type") == "tool_use" and block.get("name") == "SendUserFile":
                inp = block.get("input") or {}
                files = inp.get("files") or ([inp["file"]] if inp.get("file") else []) or \
                    ([inp["path"]] if inp.get("path") else [])
                files = [p for p in files if isinstance(p, str)]
                if files:
                    items.append({"type": "files", "files": files[:20]})
    # 마지막 메시지는 response 로 따로 있다
    final = (final_text or "").strip()
    while items and items[-1]["type"] == "text" and final and (items[-1]["text"] == mask(final[:_EXTRA_TEXT_MAX])
                                                                or final.endswith(items[-1]["text"])):
        items.pop()
    items = items[-_EXTRAS_MAX:]
    return json.dumps(items, ensure_ascii=False) if items else None


def start_compact(conn, sid, trigger):
    """컴팩트 시작. 사용자가 부른 것(manual)은 쉬고 있던 세션이 바빠지는 것이라 상태도 「작업 중」으로."""
    t = now()
    conn.execute("UPDATE sessions SET compact_at=?, compact_trigger=? WHERE session_id=?", (t, trigger, sid))
    if trigger != "auto":
        conn.execute("UPDATE sessions SET state='working', state_detail=?, state_at=? WHERE session_id=?"
                     " AND state NOT IN ('working','waiting','ended')", (COMPACTING, t, sid))


def finish_compact(conn, sid, ms=None):
    """컴팩트 끝. 컴팩트 때문에 「작업 중」이 됐던 세션은 원래대로(완료)."""
    row = conn.execute("SELECT compact_at FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not row or row[0] is None:
        return
    if ms is None:
        ms = int((now() - row[0]) * 1000)
    conn.execute("UPDATE sessions SET compact_at=NULL, compact_ms=? WHERE session_id=?", (ms, sid))
    conn.execute("UPDATE sessions SET state='done', state_detail=NULL, state_at=? WHERE session_id=?"
                 " AND state='working' AND state_detail=?", (now(), sid, COMPACTING))


COMPACTING = "컴팩트 중"


def now():
    return time.time()


# ---------------------------------------------------------------- 진행 과정(기록 파일 이어 읽기)

def _tool_brief(inp):
    """도구 호출 한 줄 요약."""
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "file_path", "notebook_path", "pattern", "url", "query", "subject", "prompt", "command"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            if key in ("file_path", "notebook_path"):
                parts = v.rstrip("/").split("/")
                v = "/".join(parts[-2:]) if len(parts) > 1 else v
            return v[:160]
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:160]
    return ""


def _tool_detail(name, inp, limit=4000):
    """눌렀을 때 펼쳐 볼 도구 호출 내용. Bash 는 명령 그대로, 나머지는 입력 전체(JSON)."""
    if not isinstance(inp, dict):
        return ""
    if name == "Bash" and isinstance(inp.get("command"), str):
        text = inp["command"]
    else:
        text = json.dumps(inp, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n… (%d자 더)" % (len(text) - limit)


def read_activity(path, since, until=None, tail_bytes=6 * 1024 * 1024, limit=200, sidechain=False):
    """since~until(epoch 초) 사이 메인 대화의 Claude 텍스트·도구 호출을 시간순으로.
    sidechain=True 면 서브에이전트 기록(줄마다 isSidechain)을 읽는다.

    Claude Code 내부 기록 형식이라 버전에 따라 바뀔 수 있다. 못 읽으면 빈 목록.
    """
    items = []
    if not path or not os.path.isfile(path):
        return items
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return items
    for raw in data.splitlines():
        if b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") != "assistant" or (d.get("isSidechain") and not sidechain):
            continue
        ts = parse_ts(d.get("timestamp"))
        if ts is None or ts < since or (until is not None and ts > until):
            continue
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and (block.get("text") or "").strip():
                items.append({"kind": "text", "at": ts, "text": mask(block["text"])})
            elif kind == "tool_use":
                items.append({"kind": "tool", "at": ts, "name": block.get("name") or "",
                              "brief": mask(_tool_brief(block.get("input"))),
                              "detail": mask(_tool_detail(block.get("name"), block.get("input")))})
    return items[-limit:]


# ---------------------------------------------------------------- 세션이 띄운 백그라운드 작업
# run_in_background 로 부른 Agent·Bash. 시작은 그 도구 호출(결과 글에 작업 ID·출력 파일), 끝은 Claude Code 가 대화에 넣는
# <task-notification>(task-id·tool-use-id·status·summary·output-file). 기록 파일이 수백 MB 일 수 있어 처음엔 끝부분만,
# 그다음엔 늘어난 만큼만 읽는다(서버 프로세스가 기억)
_BG = {}
BG_TAIL = 8 * 1024 * 1024
BG_RUNNING_MAX = 12 * 3600   # 끝났다는 알림 없이 이보다 오래된 「진행 중」은 버린다(세션이 죽었거나 기록을 놓침)
BG_DONE_KEEP = 30 * 60       # 끝난 작업은 30분 동안 목록에 남긴다
_OUT_FILE = re.compile(r"(/[^\s'\"<>]+\.output)")
_TN_TAG = re.compile(r"<(task-id|tool-use-id|status|summary|output-file)>(.*?)</\1>", re.S)


def _bg_scan(st, raw):
    try:
        d = json.loads(raw)
    except ValueError:
        return
    ts = parse_ts(d.get("timestamp"))
    content = (d.get("message") or {}).get("content")
    tasks = st["tasks"]
    if d.get("type") == "assistant" and isinstance(content, list):
        for b in content:
            inp = b.get("input") if isinstance(b, dict) and b.get("type") == "tool_use" else None
            if isinstance(inp, dict) and inp.get("run_in_background"):
                agent = b.get("name") == "Agent"
                desc = inp.get("description") or ("" if agent else inp.get("command")) or b.get("name") or ""
                tasks[b.get("id")] = {"id": b.get("id"), "kind": "agent" if agent else "bash", "desc": mask(" ".join(str(desc).split())[:120]),
                                      "started_at": ts, "status": "running", "finished_at": None, "summary": None, "output": None}
        return
    att = d.get("attachment") if isinstance(d.get("attachment"), dict) else {}
    if d.get("type") == "queue-operation" and d.get("operation") == "enqueue" and isinstance(d.get("content"), str):
        texts = [d["content"]]   # 작업 중에 도착한 알림은 대기열에 들어갔다가(enqueue) 첨부(queued_command)로 전해진다
    elif att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
        texts = [att["prompt"]]
    elif d.get("type") != "user":
        return
    else:
        texts = None
    if texts is None:
        texts = [content] if isinstance(content, str) else [
        b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"] if isinstance(content, list) else []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in tasks:
                t = b.get("content")
                t = t if isinstance(t, str) else " ".join(x.get("text") or "" for x in t if isinstance(x, dict)) if isinstance(t, list) else ""
                task = tasks[b["tool_use_id"]]
                m = _OUT_FILE.search(t)
                task["output"] = task["output"] or (m.group(1) if m else None)
                if b.get("is_error"):
                    task.update(status="failed", finished_at=ts, summary=mask(t[:200]))
    for t in texts:
        if not t.lstrip().startswith(TASK_NOTICE):
            continue
        tags = dict(_TN_TAG.findall(t))
        task = tasks.get(tags.get("tool-use-id"))
        if task and not task["finished_at"]:   # 대기열·첨부로 두 번 올 수 있다 — 먼저 온 시각
            task.update(status=(tags.get("status") or "completed").strip().lower(), finished_at=ts,
                        summary=mask(" ".join((tags.get("summary") or "").split())[:200]) or None,
                        output=task["output"] or (tags.get("output-file") or "").strip() or None)


def bg_tasks(path, now_t=None):
    """[{id(도구 호출 ID), kind(agent·bash), desc, started_at, status(running·completed·failed·killed …), finished_at, summary, output}]
    — 진행 중인 것과 끝난 지 30분 안 된 것, 시작 순."""
    if not path:
        return []
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    st = _BG.get(path)
    if st is None or size < st["pos"]:
        st = _BG[path] = {"pos": max(0, size - BG_TAIL), "tasks": {}, "first": True}
    if size > st["pos"]:
        try:
            with open(path, "rb") as f:
                f.seek(st["pos"])
                data = f.read(size - st["pos"])
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end >= 0:
            chunk = data[:end + 1]
            if st.pop("first", False) and st["pos"] > 0:
                chunk = chunk[chunk.find(b"\n") + 1:]   # 끝부분부터 읽으면 첫 줄은 잘려 있다
            for raw in chunk.splitlines():
                if b"run_in_background" in raw or b"tool_result" in raw or b"task-notification" in raw:
                    _bg_scan(st, raw)
            st["pos"] += end + 1
    t = now_t or now()
    return [dict(x) for x in st["tasks"].values()
            if x["started_at"] and (x["status"] == "running" and t - x["started_at"] < BG_RUNNING_MAX
                                    or x["finished_at"] and t - x["finished_at"] < BG_DONE_KEEP)]


def bg_task_view(task, limit=60):
    """작업 하나가 지금 무엇을 하는지: 서브에이전트면 그 기록의 텍스트·도구 호출, 명령이면 출력 끝부분(색 코드 뺌·가림)."""
    out = task.get("output")
    real = os.path.realpath(out) if out else ""
    # 기록에 적힌 경로라도 Claude Code 의 작업 출력 자리만 읽는다
    if not real or not os.path.isfile(real) or not (real.startswith(("/private/tmp/claude-", "/tmp/claude-"))
                                                   or real.startswith(os.path.expanduser("~/.claude/projects/"))):
        return {"items": [], "text": None}
    if task["kind"] == "agent":
        return {"items": read_activity(real, 0, limit=limit, sidechain=True), "text": None}
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as f:
            f.seek(max(0, size - 16000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return {"items": [], "text": None}
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "")
    return {"items": [], "text": mask(text[-12000:], private=False)}


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))


def last_model(path, tail_bytes=2 * 1024 * 1024):
    """기록 파일 끝에서 마지막 응답을 만든 모델 ID(예: claude-opus-5-5)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return None
    for raw in reversed(data.splitlines()):
        if b'"assistant"' not in raw or b'"model"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        m = (d.get("message") or {}).get("model")
        if d.get("type") == "assistant" and not d.get("isSidechain") and m and not m.startswith("<"):
            return m
    return None


STATUS_DIR = os.path.join(DATA_DIR, "status")
_ctx_cache = {}   # transcript_path → (mtime, 토큰 수)


def _status(session_id):
    """상태줄 탭(statusline_tap.py)이 남긴 세션 정보와 그 시각."""
    path = os.path.join(STATUS_DIR, session_id + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), os.path.getmtime(path)
    except (OSError, ValueError):
        return None, 0


def context_info(session_id, transcript_path):
    """컨텍스트 사용량. 상태줄 탭이 있으면 Claude Code 가 준 값(창 크기·%) 그대로,
    없으면 기록 파일의 마지막 응답 토큰 수만(창 크기를 몰라 % 는 없음)."""
    st, at = _status(session_id)
    if st and st.get("ctx_size"):
        used = st.get("ctx_used")
        pct = st.get("ctx_pct")
        if pct is None and used is not None:
            pct = round(used * 100.0 / st["ctx_size"], 1)
        return {"used": used, "size": st["ctx_size"], "pct": pct, "at": at, "src": "statusline"}
    if not transcript_path:
        return None
    try:
        mtime = os.path.getmtime(transcript_path)
    except OSError:
        return None
    hit = _ctx_cache.get(transcript_path)
    if hit and hit[0] == mtime:
        used, model = hit[1]
    else:
        used, model = _last_context_tokens(transcript_path)
        _ctx_cache[transcript_path] = (mtime, (used, model))
    return {"used": used, "size": None, "pct": None, "at": mtime, "src": "transcript", "model": model} if used else None


_goal_cache = {}


def goal_info(path, tail_bytes=2 * 1024 * 1024):
    """/goal 상태: {condition, met, at} 또는 None. 기록 파일 끝의 「Goal set: …」(건 것)·「Goal cleared」(푼 것)·
    goal_status 첨부(met — 확인할 때마다 붙는다, 실측 2.1.316)를 시간순으로 따라간다. 파일이 그대로면 기억한 값."""
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _goal_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    goal = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            data = f.read()
        for raw in data.splitlines():
            if b"Goal set:" not in raw and b"Goal cleared" not in raw and b'"goal_status"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            at = parse_ts(d.get("timestamp"))
            a = d.get("attachment") or {}
            if a.get("type") == "goal_status":
                if goal and a.get("condition", goal["condition"]) == goal["condition"]:
                    goal.update(met=bool(a.get("met")), at=at)
                continue
            content = ((d.get("message") or {}).get("content")) or ""
            if not isinstance(content, str):
                continue
            m = re.search(r"Goal set: (.+?)</local-command-stdout>", content, re.S)
            if m:
                goal = {"condition": mask(m.group(1).strip())[:300], "met": False, "at": at}
            elif "Goal cleared" in content:
                goal = None
    except OSError:
        pass
    _goal_cache[path] = (mtime, goal)
    return goal


def _last_context_tokens(path, tail_bytes=1024 * 1024):
    """(마지막 메인 대화 응답의 입력 토큰 합 = 그때 컨텍스트에 든 양, 그 응답의 모델)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return None, None
    for raw in reversed(data.splitlines()):
        if b'"compact_boundary"' in raw:
            # 응답보다 컴팩트 경계가 나중이면 지금 컨텍스트는 요약본 크기
            b = _compact_boundary(raw)
            if b and b["post"]:
                return b["post"], None
            continue
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        u = (d.get("message") or {}).get("usage") or {}
        total = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if total:
            return total, (d.get("message") or {}).get("model")
    return None, None


def _status_rate_limits():
    """상태줄 탭 중 가장 최근 것의 사용 한도(있으면). (5시간 %, 리셋, 주간 %, 리셋, 시각)"""
    best = None
    try:
        names = os.listdir(STATUS_DIR)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(STATUS_DIR, name)
        try:
            mtime = os.path.getmtime(path)   # 목록을 읽은 사이 지워질 수 있다 — 그 파일만 건너뛴다
        except OSError:
            continue
        if best and mtime <= best[0]:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                rl = json.load(f).get("rate_limits")
        except (OSError, ValueError):
            continue
        five, week = (rl or {}).get("five_hour") or {}, (rl or {}).get("seven_day") or {}
        if five.get("used_percentage") is None and week.get("used_percentage") is None:
            continue
        best = (mtime, five, week)
    if not best:
        return None
    mtime, five, week = best
    return {"five_hour": five.get("used_percentage"), "five_hour_reset": parse_ts(five.get("resets_at")),
            "seven_day": week.get("used_percentage"), "seven_day_reset": parse_ts(week.get("resets_at")),
            "updated_at": mtime}


def read_usage():
    """계정 사용량(5시간·주간). Claude Code 가 상태줄에 넘기는 rate_limits 를 상태줄 탭이 남긴 것. 없으면 None."""
    tap = _status_rate_limits()
    return dict(tap, source="statusline") if tap else None


# ---------------------------------------------------------------- 에이전트 팀
# 에이전트 팀(TeamCreate)의 팀원은 팀장이 tmux 창 등에 띄운 독립 Claude Code 세션이라 훅이 따로 온다.
# 보드에서는 팀장 칸 위 탭으로 묶는다: continued_from=팀장, branch_kind='team', team_agent=팀원 이름.
TEAMS_DIR = os.path.expanduser("~/.claude/teams")
_TEAM_SESSION = re.compile(r"^session-([0-9a-f]{8})$")


def team_head(path, lines=60):
    """대화 기록 앞부분에서 (팀 이름, 팀원 이름). 팀원이 아니면 None. 팀원의 기록 줄에는 teamName·agentName 이 붙는다."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= lines:
                    break
                if '"teamName"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("teamName") and d.get("agentName") and d.get("agentName") != "team-lead":
                    return d["teamName"], d["agentName"]
    except OSError:
        pass
    return None


def team_lead(conn, team, sid):
    """팀 이름 → 팀장 세션 ID: 팀 설정(~/.claude/teams/<팀>/config.json 의 leadSessionId), 없으면 「session-<팀장 앞 8자>」."""
    lead = None
    try:
        with open(os.path.join(TEAMS_DIR, os.path.basename(team), "config.json")) as f:
            lead = json.load(f).get("leadSessionId")
    except (OSError, ValueError, AttributeError):
        pass
    if not lead:
        m = _TEAM_SESSION.match(team or "")
        row = m and conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE ? AND session_id != ?",
                                 (m.group(1) + "%", sid)).fetchone()
        lead = row[0] if row else None
    return lead if isinstance(lead, str) and lead != sid else None


def link_team(conn, sid, lead, agent):
    """팀원 세션을 팀장에 잇는다(아직 어디에도 이어지지 않았을 때만)."""
    if not lead or lead == sid:
        return False
    return conn.execute("UPDATE sessions SET continued_from=?, branch_kind='team', team_agent=? WHERE session_id=?"
                        " AND continued_from IS NULL", (lead, (agent or "")[:60] or None, sid)).rowcount > 0


def link_teams(conn, days=30):
    """서버 시작 때 한 번: 훅이 팀원 표시를 하기 전부터 있던 팀원 세션을 기록 파일 앞부분으로 찾아 잇는다."""
    n = 0
    for r in conn.execute("SELECT session_id, transcript_path FROM sessions WHERE continued_from IS NULL"
                          " AND transcript_path IS NOT NULL AND started_at > ?", (now() - days * 86400,)).fetchall():
        got = team_head(r["transcript_path"])
        if got and link_team(conn, r["session_id"], team_lead(conn, got[0], r["session_id"]), got[1]):
            n += 1
    return n
