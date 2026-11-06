"""세션이 아닌 보드 칸: 터미널 칸(그냥 셸)과 마크다운 칸(문서 + AI 편집).

- 터미널 칸: 보드 터미널 서버(tmux -L argus-web)에 bterm-… 이름으로 로그인 셸을 띄운다. 오른쪽 터미널 드로어의
  셸 탭(shell-…)과 같은 서버지만 이름이 달라 드로어 목록에는 나오지 않는다. 칸을 지우면 그 셸도 닫는다.
- 마크다운 칸: 고른 폴더의 .argus/boards/<이름>.md 에 저장한다(그 저장소의 .git/info/exclude 에 .argus/ 를 넣어 커밋에
  섞이지 않게). 칸에서 바로 고치고, AI 대화창에 요청하면 claude -p 를 한 번 돌려 그 파일만 고치게 한다
  (그 파일의 Read·Edit·Write 만 허용, 나머지 도구는 묻지 않고 막는다). 고치기 전 내용을 남겨 되돌릴 수 있다.
- 보드 순서·고정·보관은 세션 칸과 같이 다룬다 — 화면에서는 session_id 가 bd-<id> 인 칸이다.
"""
import json
import os
import re
import subprocess
import threading
import time
import uuid

from csl import config, store

TIMEOUT = 300
HISTORY = 6          # AI 에게 함께 넘기는 앞선 대화 수
MAX_DOC = 400_000    # 칸에서 저장할 수 있는 문서 크기(글자)
_lock = threading.Lock()
RUNNING = set()      # AI 가 고치는 중인 보드 id

SYSTEM = ("너는 Argus 보드의 마크다운 문서 편집 도우미다. 이 폴더의 문서 파일 하나만 다룬다(다른 파일·명령·웹은 막혀 있다). "
          "사용자가 고쳐 달라고 하면 먼저 그 파일을 Read 로 읽고 Edit(또는 전체를 새로 쓸 때 Write)로 고친다. "
          "질문만 하면 문서를 고치지 않고 답한다. 사용자에게 되묻지 않는다 — 애매하면 가장 그럴듯하게 고치고 그렇게 했다고 말한다. "
          "마지막 답에는 무엇을 바꿨는지 한두 문장으로 쓴다. 답은 반드시 사용자의 이번 요청과 같은 언어로 쓴다(요청이 한국어면 한국어).")


def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS boards (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, cwd TEXT, path TEXT, shell TEXT,
        pinned REAL DEFAULT 0, archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS board_msgs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, board_id INTEGER, role TEXT, text TEXT, status TEXT, changed INTEGER DEFAULT 0,
        prev TEXT, model TEXT, cost REAL, at REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_board_msgs ON board_msgs(board_id, id)")


def bid_of(sid):
    """칸 id(bd-12) → 보드 id(12). 보드 칸이 아니면 None."""
    m = re.fullmatch(r"bd-(\d+)", sid or "")
    return int(m.group(1)) if m else None


def _web():
    from csl import terminals
    web = terminals.registry().get("web")
    return web if web and web.available() else None


def _slug(title):
    s = re.sub(r"[^\w\-]+", "-", (title or "").strip()).strip("-")[:60]
    return s or time.strftime("note-%Y%m%d-%H%M")


def _exclude(cwd):
    """그 폴더가 git 저장소 안이면 .git/info/exclude 에 .argus/ 를 넣는다(이미 있으면 그대로). 커밋에 섞이지 않게."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode != 0 or not r.stdout.strip():
        return
    gdir = r.stdout.strip()
    gdir = gdir if os.path.isabs(gdir) else os.path.join(cwd, gdir)
    path = os.path.join(gdir, "info", "exclude")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = open(path).read() if os.path.exists(path) else ""
        if not any(ln.strip() in (".argus/", ".argus", "/.argus/") for ln in old.splitlines()):
            with open(path, "a") as f:
                f.write(("" if not old or old.endswith("\n") else "\n") + "# Argus 보드 문서(마크다운 칸)\n.argus/\n")
    except OSError:
        pass


def create(conn, kind, cwd, title):
    """(보드 dict, 오류)."""
    cwd = os.path.realpath(os.path.expanduser(cwd or ""))
    if not os.path.isdir(cwd):
        return None, "폴더가 없습니다: " + cwd
    t = store.now()
    title = (title or "").strip()[:120]
    if kind == "md":
        d = os.path.join(cwd, ".argus", "boards")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            return None, "문서 폴더를 만들지 못했습니다: %s" % e
        _exclude(cwd)
        name, n = _slug(title), 1
        path = os.path.join(d, name + ".md")
        while os.path.exists(path):
            n += 1
            path = os.path.join(d, "%s-%d.md" % (name, n))
        with open(path, "w") as f:
            f.write("# %s\n\n" % (title or os.path.splitext(os.path.basename(path))[0]))
        title = title or os.path.splitext(os.path.basename(path))[0]
        cur = conn.execute("INSERT INTO boards (kind, title, cwd, path, created_at, updated_at) VALUES ('md',?,?,?,?,?)",
                           (title, cwd, path, t, t))
    elif kind == "term":
        web = _web()
        if not web:
            return None, "tmux 가 없어 터미널을 열 수 없습니다(brew install tmux)"
        name = web.new_board_shell(cwd)
        if not name:
            return None, "터미널을 열지 못했습니다"
        cur = conn.execute("INSERT INTO boards (kind, title, cwd, shell, created_at, updated_at) VALUES ('term',?,?,?,?,?)",
                           (title or os.path.basename(cwd) or cwd, cwd, name, t, t))
    else:
        return None, "알 수 없는 칸 종류입니다"
    return get(conn, cur.lastrowid), None


def _row(conn, bid):
    return conn.execute("SELECT * FROM boards WHERE id=?", (bid,)).fetchone()


def _project(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else cwd
    except (OSError, subprocess.SubprocessError):
        return cwd


_ROOTS = {}


def _view(conn, r, shells=None):
    """화면에 넘기는 칸 모양(세션 칸과 같은 이름의 필드 몇 개 + board 종류)."""
    root = _ROOTS.get(r["cwd"])
    if root is None:
        root = _ROOTS[r["cwd"]] = _project(r["cwd"])
    out = {"session_id": "bd-%d" % r["id"], "board": r["kind"], "board_id": r["id"], "title": r["title"], "cwd": r["cwd"],
           "project_root": root, "project_name": os.path.basename(root.rstrip("/")) or root,
           "pinned": r["pinned"] or 0, "archived": r["archived"], "started_at": r["created_at"],
           "last_activity": r["updated_at"], "state": "idle"}
    if r["kind"] == "md":
        try:
            st = os.stat(r["path"])
            out["mtime"], out["exists"] = st.st_mtime, True
        except OSError:
            out["mtime"], out["exists"] = 0, False
        out["path"] = r["path"]
        last = conn.execute("SELECT id, status FROM board_msgs WHERE board_id=? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        out["msg_sig"] = "%s:%s" % (last["id"], last["status"]) if last else ""
        out["ai_running"] = r["id"] in RUNNING
        if out["ai_running"]:
            out["state"] = "working"
    else:
        out["shell"] = r["shell"]
        out["alive"] = r["shell"] in shells if shells is not None else True
    return out


def get(conn, bid):
    r = _row(conn, bid)
    return _view(conn, r) if r else None


def listing(conn, show_archived):
    rows = conn.execute("SELECT * FROM boards" + ("" if show_archived else " WHERE archived=0") + " ORDER BY id").fetchall()
    web = _web() if any(r["kind"] == "term" for r in rows) else None
    shells = set(web.board_shells()) if web else set()
    return [_view(conn, r, shells) for r in rows]


def set_pin(conn, sid, value):
    bid = bid_of(sid)
    if bid is not None:
        conn.execute("UPDATE boards SET pinned=? WHERE id=?", (value, bid))
    return bid is not None


def set_archived(conn, bid, archived):
    conn.execute("UPDATE boards SET archived=?, updated_at=? WHERE id=?", (1 if archived else 0, store.now(), bid))


def rename(conn, bid, title):
    title = (title or "").strip()[:120]
    if title:
        conn.execute("UPDATE boards SET title=?, updated_at=? WHERE id=?", (title, store.now(), bid))
    return bool(title)


def _trash(path):
    """파일을 macOS 휴지통(~/.Trash)으로 옮긴다 — 되살릴 수 있게. 이름이 겹치면 뒤에 시각을 붙인다. 옮긴 곳 또는 None."""
    trash = os.path.expanduser("~/.Trash")
    if not os.path.isfile(path) or not os.path.isdir(trash):
        return None
    base, ext = os.path.splitext(os.path.basename(path))
    dest = os.path.join(trash, base + ext)
    if os.path.exists(dest):
        dest = os.path.join(trash, "%s %s%s" % (base, time.strftime("%H.%M.%S"), ext))
    try:
        os.rename(path, dest)
    except OSError:
        return None
    return dest


def remove(conn, bid, trash_file=False):
    """칸을 지운다. 터미널 칸은 그 셸도 닫는다. 마크다운 파일은 그대로 두고, trash_file 이면 휴지통으로 옮긴다
    (그 칸이 만든 .argus/boards/ 안의 파일만). (성공, 휴지통으로 옮긴 곳 또는 None)."""
    r = _row(conn, bid)
    if not r:
        return False, None
    moved = None
    if trash_file and r["kind"] == "md" and r["path"] and os.sep + os.path.join(".argus", "boards") + os.sep in r["path"]:
        moved = _trash(r["path"])
    if r["kind"] == "term" and r["shell"]:
        web = _web()
        if web:
            web.kill_board_shell(r["shell"])
    conn.execute("DELETE FROM board_msgs WHERE board_id=?", (bid,))
    conn.execute("DELETE FROM boards WHERE id=?", (bid,))
    return True, moved


def reopen_shell(conn, bid):
    """닫힌 터미널 칸의 셸을 같은 폴더에서 다시 띄운다."""
    r = _row(conn, bid)
    web = _web()
    if not r or r["kind"] != "term" or not web:
        return None
    name = web.new_board_shell(r["cwd"])
    if name:
        conn.execute("UPDATE boards SET shell=?, updated_at=? WHERE id=?", (name, store.now(), bid))
    return name


# ---------------------------------------------------------------- 마크다운 문서
def read_doc(conn, bid):
    r = _row(conn, bid)
    if not r or r["kind"] != "md":
        return None
    try:
        with open(r["path"]) as f:
            text = f.read()
        mtime = os.stat(r["path"]).st_mtime
    except OSError:
        text, mtime = "", 0
    msgs = [dict(m) for m in conn.execute(
        "SELECT id, role, text, status, changed, model, cost, at, prev IS NOT NULL AS undoable FROM board_msgs"
        " WHERE board_id=? ORDER BY id DESC LIMIT 60", (bid,))][::-1]
    return {"text": text, "mtime": mtime, "path": r["path"], "msgs": msgs, "ai_running": bid in RUNNING}


def write_doc(conn, bid, text, base_mtime):
    """(성공, 오류, 새 mtime). base_mtime 이 지금 파일과 다르면(그사이 AI·다른 곳에서 고쳤다) 쓰지 않는다."""
    r = _row(conn, bid)
    if not r or r["kind"] != "md":
        return False, "문서 칸이 아닙니다", 0
    if bid in RUNNING:
        return False, "AI 가 문서를 고치는 중입니다 — 끝난 뒤 저장해 주세요", 0
    if not isinstance(text, str) or len(text) > MAX_DOC:
        return False, "문서가 너무 큽니다", 0
    try:
        cur = os.stat(r["path"]).st_mtime
    except OSError:
        cur = 0
    if base_mtime is not None and cur and abs(cur - float(base_mtime)) > 0.001:
        return False, "그사이 문서가 바뀌었습니다 — 새로 불러온 뒤 다시 고쳐 주세요", cur
    tmp = r["path"] + ".tmp-argus"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, r["path"])
    conn.execute("UPDATE boards SET updated_at=? WHERE id=?", (store.now(), bid))
    return True, None, os.stat(r["path"]).st_mtime


def undo(conn, bid, mid):
    """AI 가 고치기 전 내용으로 되돌린다."""
    r = _row(conn, bid)
    m = conn.execute("SELECT prev FROM board_msgs WHERE id=? AND board_id=?", (mid, bid)).fetchone()
    if not r or not m or m["prev"] is None:
        return False, "되돌릴 내용이 없습니다"
    if bid in RUNNING:
        return False, "AI 가 고치는 중입니다"
    with open(r["path"], "w") as f:
        f.write(m["prev"])
    t = store.now()
    conn.execute("UPDATE board_msgs SET prev=NULL WHERE id=?", (mid,))
    conn.execute("INSERT INTO board_msgs (board_id, role, text, status, at) VALUES (?,?,?,?,?)",
                 (bid, "note", "AI 가 고치기 전으로 되돌렸습니다", "done", t))
    conn.execute("UPDATE boards SET updated_at=? WHERE id=?", (t, bid))
    return True, None


def ask(conn, bid, text, model=None, effort=None):
    """AI 에게 문서 고치기를 맡긴다(따로 돈다). model·effort 는 칸에서 고른 것 — 모델은 화면 이름(Sonnet 5.5 등)이고
    없으면 설정 board_ai_model, effort 는 비우면 모델 기본값. 답 줄의 model 칸에 「Opus 5.5 · high」처럼 남긴다. (성공, 오류)."""
    r = _row(conn, bid)
    if not r or r["kind"] != "md":
        return False, "문서 칸이 아닙니다"
    text = (text or "").strip()
    if not text:
        return False, "요청을 적어 주세요"
    with _lock:
        if bid in RUNNING:
            return False, "앞선 요청을 처리하는 중입니다"
        RUNNING.add(bid)
    t = store.now()
    conn.execute("INSERT INTO board_msgs (board_id, role, text, status, at) VALUES (?,?,?,?,?)", (bid, "user", text, "done", t))
    mid = conn.execute("INSERT INTO board_msgs (board_id, role, text, status, model, at) VALUES (?,?,?,?,?,?)",
                       (bid, "ai", "", "running", _label(conn, model, effort), t)).lastrowid
    conn.execute("UPDATE boards SET updated_at=? WHERE id=?", (t, bid))
    conn.commit()
    threading.Thread(target=_run, args=(bid, mid), daemon=True).start()
    return True, None


def _label(conn, model, effort):
    if model not in config.CARDCHAT_MODELS and model not in config.MODELS:
        model = config.get(conn, "board_ai_model") or "sonnet"
    return model + (" · " + effort if effort in config.EFFORTS else "")


def _model_args(label):
    """답 줄에 남긴 「Opus 5.5 · high」 → claude 인자. 예전 줄(sonnet 등 별칭만)도 그대로 된다."""
    name, _, effort = (label or "sonnet").partition(" · ")
    args = ["--model", config.CARDCHAT_MODELS.get(name, name)]
    return args + ["--effort", effort] if effort in config.EFFORTS else args


def _run(bid, mid):
    from csl.daily import _claude_bin, _clean_env
    conn = store.connect()
    status, reply, cost, changed, prev = "error", "", None, 0, None
    try:
        r = _row(conn, bid)
        model = conn.execute("SELECT model FROM board_msgs WHERE id=?", (mid,)).fetchone()["model"]
        hist = conn.execute("SELECT role, text FROM board_msgs WHERE board_id=? AND id<? AND role IN ('user','ai') AND text<>''"
                            " ORDER BY id DESC LIMIT ?", (bid, mid, HISTORY + 1)).fetchall()[::-1]
        request = hist[-1]["text"] if hist else ""
        before = hist[:-1]
        name = os.path.basename(r["path"])
        try:
            prev = open(r["path"]).read()
        except OSError:
            prev = ""
        prompt = ("문서 파일: ./%s\n\n" % name +
                  ("앞선 대화(참고):\n" + "\n".join("%s: %s" % ("사용자" if h["role"] == "user" else "AI", h["text"][:1500])
                                               for h in before) + "\n\n" if before else "") +
                  "이번 요청:\n" + request)
        allow = ["Read(./%s)" % name, "Edit(./%s)" % name, "Write(./%s)" % name]
        cmd = [_claude_bin(), "-p", prompt, *_model_args(model), "--session-id", str(uuid.uuid4()), "--no-session-persistence",
               "--output-format", "json", "--permission-mode", "dontAsk", "--append-system-prompt", SYSTEM,
               "--allowedTools", *allow, "--disallowedTools", "Bash", "WebFetch", "WebSearch", "NotebookEdit", "Task"]
        try:
            p = subprocess.run(cmd, cwd=os.path.dirname(r["path"]), env=_clean_env(), capture_output=True, text=True,
                               timeout=TIMEOUT, stdin=subprocess.DEVNULL)
            try:
                d = json.loads(p.stdout or "{}")
            except ValueError:
                d = {}
            reply = (d.get("result") or "").strip()
            cost = d.get("total_cost_usd")
            if p.returncode == 0 and not d.get("is_error") and reply:
                status = "done"
            else:
                reply = (reply or p.stderr or p.stdout or "실패").strip()[:800]
        except subprocess.TimeoutExpired:
            reply = "%d초 안에 끝나지 않았습니다" % TIMEOUT
        except OSError as e:
            reply = "claude 를 실행하지 못했습니다: %s" % e
        try:
            changed = int(open(r["path"]).read() != prev)
        except OSError:
            changed = 0
    except Exception as e:   # 기록은 남기고 칸이 「고치는 중」에 머물지 않게
        reply = "오류: %s" % e
    finally:
        RUNNING.discard(bid)
        try:
            with conn:
                conn.execute("UPDATE board_msgs SET text=?, status=?, cost=?, changed=?, prev=? WHERE id=?",
                             (reply, status, cost, changed, prev if changed else None, mid))
                conn.execute("UPDATE boards SET updated_at=? WHERE id=?", (store.now(), bid))
        finally:
            conn.close()
