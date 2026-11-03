"""Claude Code 훅 진입점. settings.json 에서 `python3 hook.py` 로 부른다(async).

표준 입력의 훅 JSON 을 받아 board.db 에 세션 상태·턴을 적는다.
어떤 오류가 나도 Claude 를 막지 않도록 항상 0 으로 끝난다.
"""
import json
import re
import os
import socket
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csl import config, store, terminals  # noqa: E402

WAIT_NOTIFICATIONS = {
    "permission_prompt": "권한 승인 대기",
    "elicitation_dialog": "입력 대기",
    "elicitation_url_dialog": "브라우저 입력 대기",
}
# agent_needs_input 은 「<다른 세션 제목> needs your input: …」 — 백그라운드 세션이 답을 기다린다는 소식을
# 데몬을 띄운 세션에 전해 주는 알림이다. 이 세션이 막힌 게 아니므로 확인 필요로 바꾸지 않는다
# (해당 백그라운드 세션은 자기 Stop 훅으로 「새 응답」이 된다)
WAIT_TOOLS = {
    "AskUserQuestion": "질문에 답변 대기",
    "ExitPlanMode": "계획 승인 대기",
}


TERMINAL_ENV = (
    ("program", "TERM_PROGRAM"),
    ("bundle", "__CFBundleIdentifier"),
    ("entrypoint", "CLAUDE_CODE_ENTRYPOINT"),
    ("iterm_session", "ITERM_SESSION_ID"),
    ("term_session", "TERM_SESSION_ID"),
)


def ancestor_tty():
    """훅에는 터미널이 없으니 조상 프로세스(claude)의 tty 를 찾는다."""
    pid = os.getppid()
    for _ in range(6):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,tty=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return None
        if len(out) < 2:
            return None
        if out[1] not in ("??", "-"):
            return "/dev/" + out[1]
        pid = int(out[0])
        if pid <= 1:
            return None
    return None


def claude_ancestry():
    """훅을 띄운 claude 프로세스 쪽 조상들의 명령줄(가까운 것부터). 못 읽으면 빈 목록."""
    cmds, pid = [], os.getppid()
    for _ in range(8):
        try:
            out = subprocess.run(["ps", "-ww", "-o", "ppid=,command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        parts = out.split(None, 1)
        if len(parts) < 2:
            break
        cmds.append(parts[1])
        pid = int(parts[0])
        if pid <= 1:
            break
    return cmds


def _is_claude(cmd):
    first = cmd.split(None, 1)[0] if cmd.strip() else ""
    return os.path.basename(first) == "claude" or "/claude/versions/" in first


_RESUME = re.compile(r"--resume\s+(?:\S*/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\.jsonl)?(?:\s|$)")


def link_continuation(conn, sid, cmds=None):
    """`--resume <이전> --fork-session` 으로 새 ID 를 받은 세션이면 이전 세션과 잇는다(세션마다 한 번).

    Claude Code 가 대화를 백그라운드 세션(claude daemon 의 --bg-pty-host)으로 옮기면 새 ID 로 이어지고,
    원래 터미널은 그 세션의 화면이 된다. 이때 훅 환경 변수는 데몬을 띄운 다른 터미널의 것이라 믿을 수 없다.
    그래서 백그라운드로 이어진 경우엔 이전 세션의 터미널·대화·넥스트 액션·고정·보드에서 붙인 이름을
    새 세션으로 옮기고 이전 세션은 끝난 것으로 둔다. 일반 터미널에서 포크한 경우엔 둘 다 살아 있을 수 있어
    잇기만 한다.
    """
    row = conn.execute("SELECT link_checked FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if row is None or row["link_checked"]:
        return
    conn.execute("UPDATE sessions SET link_checked=1 WHERE session_id=?", (sid,))
    cmds = claude_ancestry() if cmds is None else cmds
    claude_cmd = ""
    for c in cmds:   # 가까운 조상부터 — 자기 claude 를 지나면 멈춘다(그 세션 안에서 띄운 `claude -p` 가 바깥 세션의 --resume 을 줍지 않게)
        if "--session-id " + sid in c or "--resume" in c:
            claude_cmd = c
            break
        if _is_claude(c) and "--bg-" not in c:   # 백그라운드 예비·pty 호스트는 건너 올라간다
            break
    m = _RESUME.search(claude_cmd)
    if not m or "--fork-session" not in claude_cmd or m.group(1) == sid:
        return
    old = m.group(1)
    prev = conn.execute("SELECT terminal, pinned, title, title_source FROM sessions WHERE session_id=?", (old,)).fetchone()
    if prev is None:
        return
    moved = any("--bg-pty-host" in c for c in cmds)
    conn.execute("UPDATE sessions SET continued_from=?, branch_kind=? WHERE session_id=?", (old, "moved" if moved else "fork", sid))
    if moved:
        move_session(conn, old, sid)


def move_session(conn, old, sid, turns=True, terminal=True):
    """백그라운드로 옮겨져 새 ID 로 이어진 세션: 이전 세션의 대화·넥스트 액션·감시·위임·곁질문·고정·보드에서 붙인 이름을
    새 세션으로 옮기고 이전 세션은 끝난 것으로 둔다. turns=False 는 새 세션의 기록을 따로 가져온 경우(복사된 대화가
    이미 들어 있어 옮기면 겹친다), terminal=False 는 이전 세션의 탭이 새 세션의 화면이 아닌 경우."""
    prev = conn.execute("SELECT terminal, pinned, title, title_source FROM sessions WHERE session_id=?", (old,)).fetchone()
    if prev is None:
        return
    t = store.now()
    if turns:
        conn.execute("UPDATE turns SET session_id=? WHERE session_id=?", (sid, old))
    conn.execute("UPDATE notes SET session_id=? WHERE session_id=?", (sid, old))
    # 브레인이 맡긴 일 감시·인박스 카드 위임·곁질문도 따라온다(옛 세션엔 더 응답이 생기지 않아 알림이 멈췄다)
    conn.execute("UPDATE brain_watch SET target_sid=? WHERE target_sid=?", (sid, old))
    conn.execute("UPDATE brain_watch SET brain_sid=? WHERE brain_sid=?", (sid, old))
    conn.execute("UPDATE inbox SET delegated_session=? WHERE delegated_session=?", (sid, old))
    conn.execute("UPDATE asides SET session_id=? WHERE session_id=?", (sid, old))
    if terminal:
        conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (prev["terminal"], sid))
    conn.execute("UPDATE sessions SET pinned=CASE WHEN pinned>0 THEN pinned ELSE ? END WHERE session_id=?",
                 (prev["pinned"] or 0, sid))
    if prev["title_source"] == "board":
        conn.execute("UPDATE sessions SET title=?, title_source='board' WHERE session_id=?", (prev["title"], sid))
    conn.execute("UPDATE sessions SET state='ended', state_detail=?, state_at=?, ended_at=?, pinned=0 WHERE session_id=?",
                 ("백그라운드 세션으로 이어짐", t, t, old))


_UNSET = object()


def record_terminal(conn, sid, tty=_UNSET):
    info = {key: os.environ[var] for key, var in TERMINAL_ENV if os.environ.get(var)}
    info.update(terminals.env_info(os.environ))   # 조작할 수 있는 터미널(csl/terminals)의 탭 식별자
    tty = ancestor_tty() if tty is _UNSET else tty
    if tty:
        info["tty"] = tty
    conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(info), sid))


def ensure_session(conn, data, refresh_git=False):
    sid = data["session_id"]
    cwd = data.get("cwd") or ""
    t = store.now()
    row = conn.execute("SELECT session_id FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if row is None:
        g = store.git_info(cwd)
        # 첫 요청을 품고 시작한 세션은 SessionStart(async)·UserPromptSubmit 이 거의 동시에 온다 — 둘 다 「없음」을 보고 넣으면
        # 늦은 쪽이 UNIQUE 위반으로 훅 전체(첫 요청 턴 포함)를 잃었다. 이미 있으면 그대로 둔다
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, cwd, project_root, project_name, worktree, branch,"
            " transcript_path, state, state_at, started_at, last_activity)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, cwd, g["project_root"], g["project_name"], g["worktree"], g["branch"],
             data.get("transcript_path"), "idle", t, t, t),
        )
    elif refresh_git:
        g = store.git_info(cwd)
        conn.execute(
            "UPDATE sessions SET cwd=?, project_root=?, project_name=?, worktree=?, branch=?,"
            " transcript_path=COALESCE(?, transcript_path), ended_at=NULL, archived=0 WHERE session_id=?",
            (cwd, g["project_root"], g["project_name"], g["worktree"], g["branch"],
             data.get("transcript_path"), sid),
        )


def settle(conn, sid, state, detail, started):
    """응답 훅(Stop·StopFailure)의 상태. 늦게 처리돼도 그 뒤에 온 요청(작업 중)이나 종료를 덮지 않는다
    (끝난 세션이 「완료」로 되살아나 보내기 대상이 되던 것). 다시 열린 세션은 SessionStart 가 먼저 idle 로 바꾼다."""
    row = conn.execute("SELECT state FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if row is None or row["state"] == "ended":
        return
    if conn.execute("SELECT 1 FROM turns WHERE session_id=? AND response_at IS NULL AND status='running' AND prompt_at > ?",
                    (sid, started)).fetchone():
        return
    set_state(conn, sid, state, detail)


def set_state(conn, sid, state, detail=None):
    t = store.now()
    conn.execute(
        "UPDATE sessions SET state=?, state_detail=?, state_at=?, last_activity=? WHERE session_id=?",
        (state, detail, t, t, sid),
    )


def finish_turn(conn, sid, response, status, transcript_path=None, extras=_UNSET, until=None):
    """응답을 기다리는 턴(running) 중 가장 최근 것에 응답을 붙인다. until: 이 응답 훅이 시작된 시각 — Stop 은 async 라
    무거운 사전 읽기 동안 다음 요청이 먼저 기록될 수 있다. 그 뒤에 들어온 요청은 이 응답의 것이 아니다.
    backfill 이 넣은 중단 턴(interrupted, 응답 없음)·되돌린 턴은 기다리는 턴이 아니다."""
    t = store.now()
    pending = conn.execute(
        "SELECT id, prompt_at FROM turns WHERE session_id=? AND response_at IS NULL AND status='running'"
        " AND COALESCE(prompt_at, 0) <= ? ORDER BY prompt_at DESC, id DESC",
        (sid, until if until is not None else t),
    ).fetchall()
    if pending:
        if extras is _UNSET:
            extras = _extras(transcript_path, pending[0]["prompt_at"], response, t)
        conn.execute(
            "UPDATE turns SET response=?, response_at=?, status=?, extras=? WHERE id=?",
            (store.mask(response, private=False), t, status, extras, pending[0]["id"]),
        )
        # 앞선 미응답 요청은 중단됐거나 다음 요청과 함께 처리된 것 — 한 문장으로
        rest = [row["id"] for row in pending[1:]]
        if rest:
            conn.execute("UPDATE turns SET status='merged', response_at=? WHERE id IN (%s)" % ",".join("?" * len(rest)), (t, *rest))
        return pending[0]["id"]
    if response:
        # 훅 설치 전에 시작된 턴처럼 요청 기록이 없는 응답
        return conn.execute(
            "INSERT INTO turns (session_id, prompt, prompt_at, response, response_at, status)"
            " VALUES (?,?,?,?,?,?)",
            (sid, None, None, store.mask(response, private=False), t, status),
        ).lastrowid
    return None


def _extras(path, since, response, until):
    try:   # 기록 파일 형식이 바뀌어도 응답 기록은 막지 않는다
        return store.turn_extras(path, since, response, until)
    except Exception:  # noqa: BLE001
        return None


def prefetch(conn, event, sid, data, started=None):
    """쓰기 잠금을 잡기 전에 할 느린 일(기록 파일 끝 읽기·ps)을 미리 해 둔다.

    훅은 한 트랜잭션으로 쓰는데, 첫 쓰기 뒤에 파일을 몇 MB 읽거나 ps 를 여러 번 부르면 그동안 다른 세션의 훅
    (특히 동기 훅인 UserPromptSubmit — 사용자가 Enter 를 친 뒤 기다린다)이 잠금을 기다린다. 여기서는 읽기만 한다.
    """
    pre, path = {}, data.get("transcript_path")
    row = conn.execute("SELECT terminal, link_checked, continued_from,"
                       " EXISTS (SELECT 1 FROM turns t WHERE t.session_id = sessions.session_id) AS has_turns"
                       " FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if event in ("SessionStart", "Stop"):
        pre["meta"] = store.scan_transcript_meta(path)
    if event == "SessionStart" or (event == "UserPromptSubmit" and (row is None or row["terminal"] is None)):
        pre["tty"] = ancestor_tty()
    if event in ("Stop", "UserPromptSubmit"):
        pre["model"] = store.last_model(path, tail_bytes=512 * 1024)
    if event in ("Stop", "StopFailure"):
        p = conn.execute("SELECT prompt_at FROM turns WHERE session_id=? AND response_at IS NULL AND status='running'"
                         " AND COALESCE(prompt_at, 0) <= ? ORDER BY prompt_at DESC, id DESC LIMIT 1",
                         (sid, started if started is not None else store.now())).fetchone()
        response = data.get("last_assistant_message") or (data.get("error") if event == "StopFailure" else None)
        pre["extras"] = _extras(path, p["prompt_at"], response, store.now()) if p else None
    if event != "SessionStart" and (row is None or not row["link_checked"]):
        pre["cmds"] = claude_ancestry()   # 처음 보는 세션: 이어받은 세션인지 볼 명령줄
    # 갈래인지는 아직 대화 턴이 없을 때(첫 요청 전·첫 요청 때)만 본다. /fork 는 미리 띄워 둔 예비 프로세스를 꺼내
    # 쓰므로 세션이 몇 분 전부터 있을 수 있다 — 시각으로 거르면 놓친다(실측)
    if row is None or (not row["continued_from"] and not row["has_turns"]):
        path = data.get("transcript_path")
        origin = store.fork_origin(path)
        if not origin and is_background(pre.get("cmds")):
            origin = guess_origin(conn, sid, path, data.get("cwd"))
        pre["fork"] = closest_parent(conn, sid, path, origin)
    return pre


def guess_origin(conn, sid, path, cwd, root=None, days=3, skip=()):
    """/fork 는 원본 표시(forkedFrom)를 달지 않는다(실측 — 갈래의 표시는 /branch 가 단 것이 복사된 것). 복사본은 원본의
    첫 메시지부터 그대로 시작하므로, 같은 폴더의 최근 세션 중 맨 앞 메시지 ID 가 겹치는 세션을 원본 후보로 본다.
    root 를 주면 폴더 대신 같은 저장소에서 찾는다 — 워크트리 하위 폴더에서 /fork 한 백그라운드 세션은 저장소 루트에서
    뜬다(실측). 겹치는 세션이 여럿이면(같은 원본의 다른 갈래) 아직 어디에도 이어지지 않은 터미널 세션을 먼저 본다."""
    mine = set(store.head_uuids(path))
    if not mine:
        return None
    where, key = ("project_root = ?", root) if root else ("cwd = ?", cwd or "")
    for r in conn.execute("SELECT session_id, transcript_path FROM sessions WHERE session_id != ? AND " + where +
                          " AND transcript_path IS NOT NULL AND last_activity > ?"
                          " ORDER BY continued_from IS NOT NULL, bg, last_activity DESC LIMIT 30",
                          (sid, key, store.now() - days * 86400)):
        if r["session_id"] in skip:
            continue
        if mine & set(store.head_uuids(r["transcript_path"])):
            return (r["session_id"], None)
    return None


def closest_parent(conn, sid, path, origin):
    """forkedFrom 은 처음 갈라진 원본만 가리킨다 — 갈래에서 다시 /fork 하면 그 갈래가 직접 쓴 줄엔 표시가 없다(실측).
    그래서 원본과 그 갈래들 중 이 기록과 메시지가 가장 많이 겹치는 세션을 진짜 원본으로, 마지막으로 겹친 시각을
    갈라진 시각으로 고른다. 읽기만 한다(쓰기 잠금 전)."""
    if not origin:
        return origin
    root = origin[0]
    rows = conn.execute("SELECT session_id, continued_from, transcript_path, branch_kind FROM sessions"
                        " WHERE session_id = ? OR branch_kind IN ('branch', 'fork')", (root,)).fetchall()
    kids = {}
    for r in rows:
        kids.setdefault(r["continued_from"], []).append(r)
    family, todo = [], [root]
    while todo:   # 원본에서 시작해 갈래들을 따라 내려간다
        cur = todo.pop()
        family += [r for r in rows if r["session_id"] == cur]
        todo += [k["session_id"] for k in kids.get(cur, []) if k["session_id"] != sid]
    if len(family) <= 1 and origin[1]:
        return origin   # 갈래가 없는 원본이고 갈라진 시각도 안다(/branch) — 더 볼 것 없다
    mine = store.transcript_uuids(path)
    best = origin
    best_n = 0
    for r in family:
        theirs = store.transcript_uuids(r["transcript_path"])
        shared = [ts for u, ts in theirs.items() if u in mine]
        if len(shared) > best_n:
            best_n = len(shared)
            best = (r["session_id"], max([x for x in shared if x] or [origin[1] or 0]) or origin[1])
    return best


def is_background(cmds):
    """Claude Code 감독 프로세스 아래의 백그라운드 세션(/fork·claude --bg)인지. 그 훅의 터미널 환경 변수는
    데몬을 띄운 다른 터미널 것이라 기록하면 안 된다. CLAUDE_JOB_DIR 는 백그라운드 세션에만 붙는다."""
    return bool(os.environ.get("CLAUDE_JOB_DIR")) or any("--bg-pty-host" in c or "--bg-spare" in c for c in cmds or [])


def link_fork(conn, sid, origin, bg):
    """/branch·/fork 갈래를 원본에 잇는다(원본이 기록에 있을 때만, 한 번)."""
    if not origin or origin[0] == sid:
        return
    row = conn.execute("SELECT continued_from FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if row is None or row["continued_from"] or not conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (origin[0],)).fetchone():
        return
    conn.execute("UPDATE sessions SET continued_from=?, branch_at=?, branch_kind=? WHERE session_id=?",
                 (origin[0], origin[1], "fork" if bg else "branch", sid))


def ensure_server():
    try:
        with socket.create_connection(("127.0.0.1", store.PORT), timeout=0.3):
            return
    except OSError:
        pass
    server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    os.makedirs(store.DATA_DIR, exist_ok=True)
    log = open(os.path.join(store.DATA_DIR, "server.log"), "ab")
    subprocess.Popen(
        [sys.executable, server], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, env={**os.environ, "CSL_SKIP": "1"},
    )


ROW_ONLY_EVENTS = {"PostToolUse", "PreModelSwitch", "PostModelSwitch", "PostCompact", "SessionEnd"}


def handle(data):
    """기록하고, 훅이 Claude Code 에 돌려줄 출력(dict)이 있으면 돌려준다."""
    event = data.get("hook_event_name")
    sid = data.get("session_id")
    if not event or not sid:
        return None
    out = None
    started = store.now()   # 사전 읽기 전에 — 응답 훅이 늦게 처리돼도 그 뒤 요청과 가른다
    conn = store.connect()
    try:
        # 세션을 만들지 않는 이벤트인데 기록에 없는 세션(훅 설치 전부터 돌던 세션 등) — 할 일이 없다.
        # 그대로 가면 도구를 부를 때마다 ps·기록 파일 읽기를 되풀이하고 없는 행을 읽다 오류가 났다
        if event in ROW_ONLY_EVENTS and not conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone():
            return None
        pre = prefetch(conn, event, sid, data, started)
        with conn:
            if event == "SessionStart":
                cmds = claude_ancestry()
                if any("--bg-spare" in c for c in cmds):
                    # 백그라운드 세션용으로 미리 띄워 두는 예비 프로세스 — 사용자가 연 세션이 아니다
                    return None
                ensure_session(conn, data, refresh_git=True)
                # 백그라운드 세션의 훅 환경은 데몬을 띄운 다른 터미널 것이다. 이미 아는 터미널(이어받은 Orca 탭)을
                # 컴팩트·재개 때 덮어쓰지 않는다
                # 백그라운드 세션(/fork·--bg)의 훅 환경은 데몬을 띄운 다른 터미널 것이다 — 적지 않고(이미 아는 터미널은
                # 그대로: 이어받은 탭·「열기」로 붙인 탭) 백그라운드라고만 표시한다
                if is_background(cmds):
                    conn.execute("UPDATE sessions SET bg=1 WHERE session_id=?", (sid,))
                else:
                    record_terminal(conn, sid, pre.get("tty", _UNSET))
                # 브레인이 /clear 로 새 세션이 되면 옛 브레인 세션에 남은(안 끝낸) 넥스트 액션을 새 세션으로 — 브레인 패널은 지금
                # 브레인 세션의 할 일만 보여 줘서, 그대로 두면 회의 회신 같은 할 일이 보이지 않게 묻혔다
                cwd = os.path.realpath(data.get("cwd") or "")
                if cwd and cwd == os.path.realpath(config.get(conn, "brain_dir")):
                    conn.execute("UPDATE notes SET session_id=? WHERE done=0 AND session_id IN"
                                 " (SELECT session_id FROM sessions WHERE cwd=? AND session_id != ?)", (sid, data.get("cwd"), sid))
                if data.get("source") != "compact":   # 컴팩트 뒤 다시 시작 — 상태는 PreCompact/PostCompact 가 맡는다
                    set_state(conn, sid, "idle", data.get("source"))
                    store.add_event(conn, sid, "start", data.get("source"), dedupe=5)
                store.apply_transcript_meta(conn, sid, data.get("transcript_path"), pre.get("meta"))
            elif event == "UserPromptSubmit":
                ensure_session(conn, data, refresh_git=True)
                if conn.execute("SELECT terminal FROM sessions WHERE session_id=?", (sid,)).fetchone()[0] is None:
                    if is_background(pre.get("cmds")):
                        conn.execute("UPDATE sessions SET bg=1 WHERE session_id=?", (sid,))
                    else:
                        record_terminal(conn, sid, pre.get("tty", _UNSET))
                t = store.now()
                notice = store.is_task_notice(data.get("prompt"))
                # 작업 도중 끝난 백그라운드 작업 알림은 진행 중인 턴에 끼어든 것 — 새 턴을 만들지 않는다(응답 앞 알림 줄로 보인다)
                if not (notice and conn.execute("SELECT 1 FROM turns WHERE session_id=? AND response_at IS NULL"
                                                " AND status='running'", (sid,)).fetchone()):
                    conn.execute(
                        "INSERT INTO turns (session_id, prompt_id, prompt, prompt_at, status) VALUES (?,?,?,?,?)",
                        (sid, data.get("prompt_id"), store.mask(data.get("prompt") or ""), t, "running"),
                    )
                set_state(conn, sid, "working")
                conn.execute("UPDATE sessions SET compact_at=NULL WHERE session_id=?", (sid,))
                # 터미널에서 다음 요청을 쳤다면 직전 응답은 본 것이다. 백그라운드 작업 완료 알림(<task-notification>)은
                # Claude Code 가 스스로 넣는 것이라 사용자가 본 것이 아니다
                if not notice:
                    conn.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (t, sid))
            elif event == "Stop":
                ensure_session(conn, data)
                tid = finish_turn(conn, sid, data.get("last_assistant_message"), "done", data.get("transcript_path"),
                                  pre.get("extras", _UNSET), until=started)
                settle(conn, sid, "done", None, started)
                if data.get("last_assistant_message"):
                    store.add_event(conn, sid, "response", data["last_assistant_message"], tid)
                store.apply_transcript_meta(conn, sid, data.get("transcript_path"), pre.get("meta"))
            elif event == "StopFailure":
                ensure_session(conn, data)
                tid = finish_turn(conn, sid, data.get("last_assistant_message") or data.get("error"), "failed",
                                  data.get("transcript_path"), pre.get("extras", _UNSET), until=started)
                settle(conn, sid, "error", store.mask(data.get("error") or ""), started)
                store.add_event(conn, sid, "error", data.get("error") or data.get("last_assistant_message"), tid)
            elif event == "Notification":
                label = WAIT_NOTIFICATIONS.get(data.get("notification_type"))
                if label:
                    ensure_session(conn, data)
                    set_state(conn, sid, "waiting", store.mask(data.get("message") or label))
                    store.add_event(conn, sid, "waiting", data.get("message") or label, dedupe=10)
            elif event == "PermissionRequest":
                ensure_session(conn, data)
                set_state(conn, sid, "waiting", "권한 승인 대기: %s" % (data.get("tool_name") or ""))
                store.add_event(conn, sid, "waiting", "권한 승인 대기: %s" % (data.get("tool_name") or ""), dedupe=10)
            elif event == "PreToolUse":
                label = WAIT_TOOLS.get(data.get("tool_name"))
                if label:
                    ensure_session(conn, data)
                    set_state(conn, sid, "waiting", label)
                    qs = (data.get("tool_input") or {}).get("questions") if data.get("tool_name") == "AskUserQuestion" else None
                    if isinstance(qs, list) and qs:
                        conn.execute("UPDATE sessions SET ask=? WHERE session_id=?", (json.dumps(
                            {"id": data.get("tool_use_id"), "questions": store.mask_json(qs, private=False)}, ensure_ascii=False), sid))
                        store.add_event(conn, sid, "question", " / ".join(
                            str((q or {}).get("question") or "") for q in qs if isinstance(q, dict)))
                    else:
                        store.add_event(conn, sid, "waiting", label, dedupe=10)
            elif event == "PostToolUse":
                # 승인·답변이 끝나 도구가 실행됐으면 다시 작업 중
                conn.execute(
                    "UPDATE sessions SET state='working', state_detail=NULL, state_at=?"
                    " WHERE session_id=? AND state='waiting'",
                    (store.now(), sid),
                )
                if data.get("tool_name") == "AskUserQuestion":
                    conn.execute("UPDATE sessions SET ask=NULL WHERE session_id=?", (sid,))
            elif event == "PreModelSwitch":
                # 보드가 방금(20초 안) 요청한 전환이면 캐시 확인 창을 건너뛴다. 그 밖에는 Claude Code 기본 동작
                row = conn.execute("SELECT model_switch_at FROM sessions WHERE session_id=?", (sid,)).fetchone()
                if row and row[0] and store.now() - row[0] < 20:
                    conn.execute("UPDATE sessions SET model_switch_at=NULL WHERE session_id=?", (sid,))
                    out = {"hookSpecificOutput": {"hookEventName": "PreModelSwitch", "permissionDecision": "allow"}}
            elif event == "PostModelSwitch":
                if data.get("to_model"):
                    conn.execute("UPDATE sessions SET model=? WHERE session_id=? AND model IS NOT ?", (data["to_model"], sid, data["to_model"]))
            elif event == "PreCompact":
                ensure_session(conn, data)
                store.start_compact(conn, sid, data.get("trigger") or "manual")
            elif event == "PostCompact":
                store.finish_compact(conn, sid)
                store.add_event(conn, sid, "compact", data.get("trigger"))
            elif event == "SessionEnd":
                t = store.now()
                conn.execute(
                    "UPDATE sessions SET state='ended', state_detail=?, state_at=?, ended_at=? WHERE session_id=?",
                    (data.get("reason"), t, t, sid),
                )
                store.add_event(conn, sid, "end", data.get("reason"))
            if event in ("UserPromptSubmit", "Stop", "StopFailure", "SessionEnd", "SessionStart"):
                conn.execute("UPDATE sessions SET ask=NULL WHERE session_id=? AND ask IS NOT NULL", (sid,))
            # 모델·effort: SessionStart 의 model, 도구·응답 이벤트의 effort.level, 응답 뒤 기록 파일의 마지막 모델
            if event == "SessionStart" and data.get("model"):
                conn.execute("UPDATE sessions SET model=? WHERE session_id=? AND model IS NOT ?", (data["model"], sid, data["model"]))
            # 응답 직후엔 기록 파일이 덜 써졌을 수 있어 다음 요청·도구 실행 때도(모를 때만) 다시 읽는다
            if event in ("Stop", "UserPromptSubmit") or (event == "PostToolUse" and not conn.execute(
                    "SELECT model FROM sessions WHERE session_id=?", (sid,)).fetchone()[0]):
                m = pre["model"] if "model" in pre else store.last_model(data.get("transcript_path"), tail_bytes=512 * 1024)
                if m:
                    conn.execute("UPDATE sessions SET model=? WHERE session_id=? AND model IS NOT ?", (m, sid, m))
            level = (data.get("effort") or {}).get("level") if isinstance(data.get("effort"), dict) else None
            if level:
                conn.execute("UPDATE sessions SET effort=? WHERE session_id=? AND effort IS NOT ?", (level, sid, level))
            pm = data.get("permission_mode")   # 권한 모드 — 바뀔 때만 쓴다
            if pm:
                conn.execute("UPDATE sessions SET permission_mode=? WHERE session_id=? AND permission_mode IS NOT ?", (pm, sid, pm))
            link_continuation(conn, sid, cmds if event == "SessionStart" else pre.get("cmds"))
            link_fork(conn, sid, pre.get("fork"), is_background(cmds if event == "SessionStart" else pre.get("cmds")))
            # Remote Control 이 연결돼 있으면 Claude Code 가 훅에 이 변수를 넣어 준다(v2.1.199+). 바뀔 때만 쓴다
            if event != "SessionEnd":
                rc = os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID") or None
                conn.execute("UPDATE sessions SET remote=? WHERE session_id=? AND remote IS NOT ?", (rc, sid, rc))
    finally:
        conn.close()
    if event in ("SessionStart", "UserPromptSubmit"):
        ensure_server()
    return out


def main():
    if os.environ.get("CSL_SKIP"):
        return
    try:
        data = json.load(sys.stdin)
        out = handle(data)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception:  # noqa: BLE001 - 훅 실패가 Claude 를 막으면 안 된다
        try:
            os.makedirs(store.DATA_DIR, exist_ok=True)
            with open(os.path.join(store.DATA_DIR, "hook-error.log"), "a") as f:
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass


if __name__ == "__main__":
    main()
    sys.exit(0)
