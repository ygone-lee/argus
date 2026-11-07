"""OpenAI Codex(CLI·데스크톱 앱) 세션을 보드에 모은다.

Codex 는 세션(스레드) 목록을 ~/.codex/state_*.sqlite 의 threads 표에, 대화를 rollout 파일(진행 중인 세션:
sessions/YYYY/MM/DD/rollout-*.jsonl)과 그것을 옮겨 둔 thread_history_*.sqlite(지난 세션 — 옛 rollout 은 .zst 로
압축돼 있다)에 둔다. 여기서는 그것을 읽기만 한다(Codex 의 파일·DB 는 고치지 않는다).

- sync(): 서버가 몇 초마다 부른다. 바뀐 스레드를 세션·턴으로 넣고 상태(작업 중·완료)를 맞춘다 — 훅이 없어도
  (데스크톱 앱·훅을 아직 승인 안 한 CLI) 보드에 보인다.
- handle_hook(): ~/.codex/hooks.json 에 건 훅(csl/hook.py --agent codex)이 부른다. 입력 모양은 Claude Code 훅과
  거의 같다(session_id·turn_id·prompt·last_assistant_message·tool_name …). 확인 필요(권한 승인)처럼 기록 파일에
  남지 않는 상태와 바로바로 바뀌는 상태를 맡는다.
- activity()·context_info()·rate_limits(): 칸의 작업 과정·컨텍스트·사용량.

Codex 내부 형식이라 버전에 따라 바뀔 수 있다 — 못 읽으면 조용히 빈 값으로 둔다.
"""
import glob
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.parse

from csl import store

HOME = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
HOOKS_PATH = os.path.join(HOME, "hooks.json")
# 처음 가져올 때 이 날수 안에 움직인 스레드만(그 뒤로는 바뀐 것만 따라간다)
FIRST_SYNC_DAYS = 30
# 이만큼 아무 일이 없던 스레드는 「종료」로 둔다(데스크톱 앱 스레드는 끝이 없어 칸이 계속 쌓인다). 새 턴이 오면 다시 살아난다
STALE_SECS = 12 * 3600
# 보드에서 고를 수 있는 Codex 모델·추론 단계(-m · -c model_reasoning_effort=…). 모델 목록은 Codex 의 models_cache 에서 읽는다
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
AGENT = "codex"


def available():
    return os.path.isdir(HOME)


def binary():
    """codex 실행 파일(없으면 None). 서버는 로그인 셸의 PATH 를 못 받을 수 있어 흔한 자리도 본다."""
    from shutil import which
    for p in (which("codex"), os.path.expanduser("~/.local/bin/codex"), "/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ------------------------------------------------------------------ Codex DB 읽기
def _db_path(prefix):
    """가장 새 버전의 DB(state_5.sqlite → 나중에 state_6 …)."""
    best = None
    for p in glob.glob(os.path.join(HOME, prefix + "_*.sqlite")):
        m = re.search(r"_(\d+)\.sqlite$", p)
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), p)
    return best[1] if best else None


def _open(prefix):
    """읽기 전용 연결(없으면 None). Codex 가 쓰는 중인 WAL DB 라 쓰지 않고, 잠겨 있으면 짧게 기다린다."""
    path = _db_path(prefix)
    if not path:
        return None
    uri = "file:%s?mode=ro" % urllib.parse.quote(path)
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:
        try:   # -shm 을 열 수 없는 환경(샌드박스 등) — 마지막 체크포인트까지의 내용만이라도
            conn = sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(path), uri=True, timeout=2)
        except sqlite3.Error:
            return None
    conn.row_factory = sqlite3.Row
    return conn


def threads(since_ms=0, ids=None):
    """Codex 스레드(dict) — updated 가 since_ms 보다 새 것, 또는 ids 의 것. 서브에이전트 스레드는 뺀다(부모 칸의 작업 과정)."""
    conn = _open("state")
    if conn is None:
        return []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        upd = "COALESCE(updated_at_ms, updated_at*1000)" if "updated_at_ms" in cols else "updated_at*1000"
        if ids:
            rows = conn.execute("SELECT *, %s AS upd_ms FROM threads WHERE id IN (%s)" % (upd, ",".join("?" * len(ids))),
                                list(ids)).fetchall()
        else:
            rows = conn.execute("SELECT *, %s AS upd_ms FROM threads WHERE %s > ? ORDER BY %s" % (upd, upd, upd),
                                (since_ms,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("agent_role") or d.get("agent_nickname") or d.get("attached_thread_parent_id"):
            continue
        out.append(d)
    return out


def thread_of(tid):
    got = threads(ids=[tid])
    return got[0] if got else None


def is_desktop(th):
    """데스크톱 앱(또는 VS Code 확장)에서 연 스레드 — 터미널이 없다. 0.167 부터 CLI 스레드도 source 가 vscode 로
    찍힐 수 있어 만든 곳(originator: codex-tui·codex_exec …)을 먼저 본다."""
    th = th or {}
    orig = (th.get("originator") or "").lower()
    if orig:
        return not (orig.startswith("codex-tui") or orig.startswith("codex_cli") or "exec" in orig or orig == "codex")
    return th.get("source") not in ("cli", "exec")


def fork_origin(path):
    """rollout 첫 줄(session_meta)의 (갈라 온 스레드 id, 갈라진 시각) 또는 (None, None)."""
    if not path or not os.path.isfile(path):
        return None, None
    try:
        with open(path, "rb") as f:
            d = json.loads(f.readline() or b"{}")
    except (OSError, ValueError):
        return None, None
    p = d.get("payload") or {}
    return p.get("forked_from_id"), _ts(p.get("timestamp"))


# ------------------------------------------------------------------ 요청 글 다듬기
_REQ = re.compile(r"^## My request(?: for Codex)?:\s*\n", re.M)


def clean_prompt(text):
    """데스크톱 앱이 붙이는 머리말(첨부 파일 목록·참고 대화)을 떼고 사용자가 쓴 요청만."""
    if not text:
        return text
    m = None
    for m in _REQ.finditer(text):
        pass
    if m:
        text = text[m.end():]
    return text.strip()


def _content_text(content):
    """UserMessage·AgentMessage 의 content([{type:text, text}, {type:image …}]) → 글. 이미지는 [Image #n]."""
    if isinstance(content, str):
        return content
    parts, n = [], 0
    for c in content or []:
        if not isinstance(c, dict):
            continue
        t = (c.get("type") or "").lower()
        if t == "text" and c.get("text"):
            parts.append(c["text"])
        elif "image" in t:
            n += 1
            parts.append("[Image #%d]" % n)
    return "".join(p if not p.startswith("[Image #") else ("\n" + p if parts and parts[0] is not p else p) for p in parts)


def _item_text(it):
    """thread item(대화 한 조각)의 글 — rollout 의 PascalCase·DB 의 camelCase 둘 다."""
    if it.get("text") is not None:
        return it.get("text") or ""
    return _content_text(it.get("content"))


def _norm(t):
    return (t or "").replace("_", "").lower()   # UserMessage · userMessage · user_message → usermessage


# ------------------------------------------------------------------ 턴 읽기
def _ts(v):
    """초·밀리초·ISO 문자열 → epoch 초."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    return store.parse_ts(v)


def _rollout_lines(path):
    try:
        with open(path, "rb") as f:
            for raw in f:
                try:
                    yield json.loads(raw)
                except ValueError:
                    continue
    except OSError:
        return


_CALLS = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")
_CALL_ITEMS = ("commandexecution", "mcptoolcall", "filechange", "websearch")


def _turns_from_rollout(path):
    """진행 중·최근 세션의 rollout(jsonl) → 턴 목록(dict: turn_id, prompt, prompt_at, response, response_at, status, items)."""
    turns, by_id = [], {}

    def turn(tid, at=None):
        t = by_id.get(tid)
        if t is None:
            t = {"turn_id": tid, "prompt": None, "prompt_at": at, "response": None, "final": None, "response_at": None,
                 "status": "running", "items": []}
            by_id[tid] = t
            turns.append(t)
        return t
    cur = None
    for d in _rollout_lines(path):
        if d.get("type") == "response_item" and cur:
            # 모델이 부른 도구 — 끝나기 전(작업 중·중단)에도 바로 남는다. 작업 과정은 이것으로 그린다
            p = d.get("payload") or {}
            if p.get("type") in _CALLS:
                turn(cur)["items"].append(dict(p, type="_call", call=p.get("type"), _at=_ts(d.get("timestamp"))))
            continue
        if d.get("type") != "event_msg":
            continue
        p = d.get("payload") or {}
        kind, at = p.get("type"), _ts(d.get("timestamp"))
        tid = p.get("turn_id") or cur
        if kind == "task_started":
            cur = p.get("turn_id")
            t = turn(cur, _ts(p.get("started_at")) or at)
            t["prompt_at"] = t["prompt_at"] or _ts(p.get("started_at")) or at
        elif kind == "item_completed" and tid:
            it = p.get("item") or {}
            if _norm(it.get("type")) in _CALL_ITEMS:
                continue   # 위의 도구 호출과 같은 것 — 두 번 그리지 않는다
            t = turn(tid, at)
            it = dict(it, _at=_ts(p.get("completed_at_ms")) or at)
            t["items"].append(it)
            k = _norm(it.get("type"))
            if k == "usermessage" and t["prompt"] is None:
                t["prompt"] = _item_text(it)
                t["prompt_at"] = t["prompt_at"] or at
            elif k == "agentmessage":
                txt = _item_text(it)
                if txt.strip():
                    t["response"] = txt
                    if (it.get("phase") or "") in ("final_answer", "final"):
                        t["final"] = txt
        elif kind == "task_complete" and tid:
            t = turn(tid, at)
            t["status"] = "done"
            t["response_at"] = _ts(p.get("completed_at")) or at
            if p.get("last_agent_message"):
                t["final"] = p["last_agent_message"]
        elif kind in ("turn_aborted", "task_aborted") and tid:
            t = turn(tid, at)
            t["status"] = "interrupted"
            t["response_at"] = at
        elif kind in ("error", "stream_error", "task_failed") and tid and kind != "stream_error":
            t = turn(tid, at)
            t["status"] = "failed"
            t["response_at"] = at
            t["error"] = p.get("message") or ""
    for t in turns:
        t["response"] = t.pop("final") or t["response"]
    return turns


def _turns_from_db(tid):
    """지난 세션: thread_history DB 의 턴·항목 → 턴 목록(_turns_from_rollout 과 같은 모양)."""
    conn = _open("thread_history")
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT turn_id, status, started_at, completed_at, final_agent_item_id, error_json"
                            " FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal", (tid,)).fetchall()
        items = conn.execute("SELECT turn_id, item_id, item_json, created_at_ms, completed_at_ms FROM thread_items"
                             " WHERE thread_id=? ORDER BY rollout_ordinal", (tid,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    by_turn = {}
    for r in items:
        try:
            it = json.loads(r["item_json"])
        except ValueError:
            continue
        it["_id"] = r["item_id"]
        it["_at"] = _ts(r["completed_at_ms"] or r["created_at_ms"])
        by_turn.setdefault(r["turn_id"], []).append(it)
    out = []
    status_map = {"completed": "done", "interrupted": "interrupted", "failed": "failed", "inProgress": "running",
                  "in_progress": "running"}
    for r in rows:
        its = by_turn.get(r["turn_id"], [])
        prompt = next((_item_text(i) for i in its if _norm(i.get("type")) == "usermessage"), None)
        agent = [i for i in its if _norm(i.get("type")) == "agentmessage" and _item_text(i).strip()]
        final = next((i for i in agent if i.get("_id") == r["final_agent_item_id"]), None) \
            or next((i for i in reversed(agent) if i.get("phase") in ("final_answer", "final")), None) \
            or (agent[-1] if agent else None)
        t = {"turn_id": r["turn_id"], "prompt": prompt, "prompt_at": _ts(r["started_at"]),
             "response": _item_text(final) if final else None, "response_at": _ts(r["completed_at"]),
             "status": status_map.get(r["status"], "done"), "items": its}
        if r["error_json"]:
            try:
                t["error"] = json.loads(r["error_json"]).get("message") or ""
            except (ValueError, AttributeError):
                t["error"] = ""
        out.append(t)
    return out


def _rollout(th):
    """읽을 수 있는(압축 안 된) rollout 경로 또는 None."""
    p = (th or {}).get("rollout_path") or ""
    return p if p.endswith(".jsonl") and os.path.isfile(p) else None


_TURN_CACHE = {}


def thread_turns(th):
    """스레드의 턴 목록. 진행 중이면 rollout, 아니면 DB. rollout 이 그대로면 기억한 값(칸이 몇 초마다 묻는다)."""
    path = _rollout(th)
    if path:
        try:
            st = os.stat(path)
            key = (st.st_mtime, st.st_size)
        except OSError:
            key = None
        hit = _TURN_CACHE.get(path)
        if key and hit and hit[0] == key:
            return hit[1]
        turns = _turns_from_rollout(path)
        if key and turns:
            if len(_TURN_CACHE) > 40:
                _TURN_CACHE.clear()
            _TURN_CACHE[path] = (key, turns)
        if turns:
            return turns
    return _turns_from_db(th["id"])


# ------------------------------------------------------------------ 작업 과정(칸의 진행 단계·「작업 과정 보기」)
def _cmd_text(it):
    c = it.get("command")
    if isinstance(c, list):
        # ["/bin/zsh", "-lc", "실제 명령"] — 셸 감싸기는 떼고 명령만
        c = c[-1] if len(c) >= 3 and c[-2] in ("-lc", "-c") else " ".join(map(str, c))
    return str(c or "")


_EXEC_CMD = re.compile(r"""\bcmd\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.S)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def _unescape(v):
    """JS 문자열 안의 \\n·\\" 를 푼다(한글은 그대로)."""
    if "\\" not in v:
        return v
    try:
        return json.loads('"%s"' % v.replace('"', '\\"').replace('\\\\"', '\\"'))
    except ValueError:
        return v.replace("\\n", "\n").replace('\\"', '"')


def _call_step(it):
    """rollout 의 도구 호출(response_item) → 작업 과정 한 줄."""
    at, call, name = it.get("_at"), it.get("call"), it.get("name") or ""
    if call == "local_shell_call":
        cmd = ((it.get("action") or {}).get("command")) or []
        cmd = _cmd_text({"command": cmd})
        return {"kind": "tool", "at": at, "name": "Bash", "brief": store.mask(" ".join(cmd.split())[:160]), "detail": store.mask(cmd)}
    if call == "web_search_call":
        q = (it.get("action") or {}).get("query") or ""
        return {"kind": "tool", "at": at, "name": "WebSearch", "brief": store.mask(q), "detail": ""}
    raw = it.get("input") if call == "custom_tool_call" else it.get("arguments")
    raw = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
    if name == "apply_patch" or "*** Begin Patch" in raw[:200]:
        files = _PATCH_FILE.findall(raw)
        return {"kind": "tool", "at": at, "name": "Edit", "brief": ", ".join("/".join(f.strip().split("/")[-2:]) for f in files[:4]),
                "detail": store.mask(raw[:4000])}
    cmds = [_unescape(m.group(2)) for m in _EXEC_CMD.finditer(raw)]
    if cmds:   # 코드 모드(exec)의 tools.exec_command({cmd: …}) — 셸 명령
        return {"kind": "tool", "at": at, "name": "Bash", "brief": store.mask(" ".join(cmds[0].split())[:160]),
                "detail": store.mask("\n".join(cmds)[:4000])}
    try:
        args = json.loads(raw) if call == "function_call" else None
    except ValueError:
        args = None
    if isinstance(args, dict):
        for key in ("cmd", "command"):
            v = args.get(key)
            if v:
                c = _cmd_text({"command": v})
                return {"kind": "tool", "at": at, "name": "Bash", "brief": store.mask(" ".join(c.split())[:160]), "detail": store.mask(c)}
        brief = next((" ".join(str(v).split())[:160] for v in args.values() if isinstance(v, str) and v.strip()), "")
    else:
        brief = " ".join(raw.split())[:160]
    ns = (it.get("namespace") or "").replace("mcp__", "")
    return {"kind": "tool", "at": at, "name": "%s · %s" % (ns, name) if ns else name or "tool", "brief": store.mask(brief),
            "detail": store.mask(raw[:4000])}


def _step(it):
    """thread item → 보드의 작업 과정 한 줄({kind:text|tool …}) 또는 None. Claude 의 도구 이름에 맞춰 아이콘이 같게."""
    k, at = _norm(it.get("type")), it.get("_at")
    if k == "call":
        return _call_step(it)
    if k == "agentmessage":
        txt = _item_text(it)
        return {"kind": "text", "at": at, "text": store.mask(txt)} if txt.strip() else None
    if k == "commandexecution":
        cmd = _cmd_text(it)
        out = it.get("aggregated_output") or it.get("aggregatedOutput") or ""
        detail = cmd + ("\n\n" + out[-3000:] if out else "")
        return {"kind": "tool", "at": at, "name": "Bash", "brief": store.mask(" ".join(cmd.split())[:160]),
                "detail": store.mask(detail)}
    if k == "filechange":
        changes = it.get("changes") or []
        paths = [c.get("path") for c in changes if isinstance(c, dict) and c.get("path")] if isinstance(changes, list) \
            else list(changes) if isinstance(changes, dict) else []
        brief = ", ".join("/".join(str(p).rstrip("/").split("/")[-2:]) for p in paths[:4])
        return {"kind": "tool", "at": at, "name": "Edit", "brief": brief,
                "detail": store.mask(json.dumps(changes, ensure_ascii=False, indent=2)[:4000])}
    if k == "mcptoolcall":
        args = it.get("arguments")
        brief = ""
        if isinstance(args, dict):
            brief = next((" ".join(str(v).split())[:160] for v in args.values() if isinstance(v, str) and v.strip()), "")
        return {"kind": "tool", "at": at, "name": "%s · %s" % (it.get("server") or "mcp", it.get("tool") or ""),
                "brief": store.mask(brief), "detail": store.mask(json.dumps(args, ensure_ascii=False, indent=2)[:4000])}
    if k == "websearch":
        q = it.get("query") or ((it.get("action") or {}).get("query") if isinstance(it.get("action"), dict) else "")
        return {"kind": "tool", "at": at, "name": "WebSearch", "brief": store.mask(q or ""), "detail": ""}
    if k == "imageview":
        p = str(it.get("path") or "").replace("file://", "")
        return {"kind": "tool", "at": at, "name": "Read", "brief": "/".join(p.split("/")[-2:]), "detail": p}
    if k in ("imagegeneration", "extension") and (it.get("savedPath") or it.get("kind") == "image_gen.generation"):
        return {"kind": "tool", "at": at, "name": "ImageGen", "brief": store.mask((it.get("revisedPrompt") or "")[:160]),
                "detail": it.get("savedPath") or ""}
    if k == "contextcompaction":
        return {"kind": "text", "at": at, "text": "(대화 요약 — 컨텍스트 압축)"}
    return None


def activity(sid, since, until=None, limit=200):
    """since~until(epoch 초) 사이 Codex 의 중간 메시지·도구 실행을 시간순으로(store.read_activity 와 같은 모양)."""
    th = thread_of(sid)
    if not th:
        return []
    items = []
    for t in thread_turns(th):
        if t["prompt_at"] and until is not None and t["prompt_at"] > until:
            break
        if t["response_at"] and t["response_at"] < since:
            continue
        final = t.get("response")
        for it in t["items"]:
            at = it.get("_at")
            if at is None or at < since or (until is not None and at > until):
                continue
            st = _step(it)
            # 마지막 답은 응답 칸에 이미 보인다 — 작업 과정에선 뺀다
            if st and not (st["kind"] == "text" and final and _item_text(it) == final):
                items.append(st)
    return items[-limit:]


# ------------------------------------------------------------------ 컨텍스트·사용량
_TOKEN_CACHE = {}


def _last_token_count(path):
    """rollout 끝부분의 마지막 token_count 이벤트(payload) 또는 None. 파일이 그대로면 기억한 값."""
    if not path or not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    hit = _TOKEN_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    found = None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - 512 * 1024))
            data = f.read()
        for raw in reversed(data.splitlines()):
            if b'"token_count"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if (d.get("payload") or {}).get("type") == "token_count":
                found = d["payload"]
                break
    except OSError:
        return None
    _TOKEN_CACHE[path] = (key, found)
    return found


def context_info(transcript_path):
    """{used, size, pct} — 마지막 요청이 쓴 입력 토큰 / 모델 컨텍스트 창. 모르면 None."""
    p = _last_token_count(transcript_path)
    info = (p or {}).get("info") or {}
    last, win = info.get("last_token_usage") or {}, info.get("model_context_window")
    used = last.get("input_tokens")
    if not used or not win:
        return None
    return {"used": used, "size": win, "pct": round(used * 100 / win, 1), "at": _mtime(transcript_path), "src": "codex"}


def rate_limits():
    """가장 최근 rollout 의 Codex 사용량 한도 — [{label, pct, resets_at}] 또는 []."""
    paths = sorted(glob.glob(os.path.join(HOME, "sessions", "*", "*", "*", "rollout-*.jsonl")), key=_mtime, reverse=True)
    for path in paths[:3]:
        p = _last_token_count(path)
        rl = (p or {}).get("rate_limits")
        if not isinstance(rl, dict):
            continue
        out = []
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                mins = w.get("window_minutes") or 0
                label = "주간" if mins >= 7 * 1440 - 1 else ("%d시간" % round(mins / 60) if mins >= 60 else "%d분" % mins)
                pct, reset = float(w["used_percent"]), w.get("resets_at")
                if reset and reset <= time.time():   # 마지막 Codex 기록 뒤에 한도가 초기화됨 — 옛 값 대신 0
                    pct, reset = 0.0, None
                out.append({"label": label, "pct": pct, "resets_at": reset, "window_minutes": mins})
        if out:
            return {"limits": out, "plan": rl.get("plan_type"), "at": _mtime(path),
                    "reached": rl.get("rate_limit_reached_type")}
    return None


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def models():
    """Codex 가 알려 준 모델 목록([{slug, name}]) — ~/.codex/models_cache.json. 못 읽으면 빈 목록(Codex 기본 모델로 띄움)."""
    try:
        with open(os.path.join(HOME, "models_cache.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    items = d.get("models") if isinstance(d, dict) else d
    out = []
    for m in items or []:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or m.get("id") or m.get("model")
        if not slug or m.get("visibility") in ("hide", "hidden") or m.get("hidden"):
            continue
        out.append({"slug": slug, "name": m.get("display_name") or m.get("displayName") or slug})
    return out


# ------------------------------------------------------------------ 살아 있는 프로세스
def _ps():
    try:
        # comm 을 마지막 열로 — 앞에 두면 macOS ps 가 16자로 자른다
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            rows.append((int(parts[0]), int(parts[1]) if parts[1].isdigit() else 0, parts[2], ""))
    return rows


_RUN = {"at": 0, "pids": set(), "desktop": False, "cwds": {}}


def _cwds(pids):
    """codex CLI 프로세스들의 작업 폴더 {폴더: pid} — 훅이 없을 때 세션과 프로세스를 짝짓는 데."""
    if not pids:
        return {}
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, sorted(pids)))],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    got, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid:
            got[os.path.realpath(line[1:])] = pid
    return got


def running_pids():
    """(살아 있는 codex CLI 프로세스 pid 집합, 데스크톱 앱이 떠 있는지). 3초 기억."""
    if time.time() - _RUN["at"] > 3:
        pids, desktop = set(), False
        for pid, _ppid, comm, args in _ps():
            base = os.path.basename(comm)
            if base == "codex" and ".app/" not in comm and "app-server" not in comm:   # 데몬은 화면이 아니다
                pids.add(pid)
            # 데스크톱 앱(Codex — 번들 com.openai.codex, 앱 이름은 ChatGPT.app 또는 Codex.app)의 본 프로세스
            if re.search(r"/(ChatGPT|Codex)\.app/Contents/MacOS/(ChatGPT|Codex)$", comm):
                desktop = True
        cwds = _cwds(pids) if pids != _RUN["pids"] or time.time() - _RUN["at"] > 30 else _RUN["cwds"]
        _RUN.update(at=time.time(), pids=pids, desktop=desktop, cwds=cwds)
    return _RUN["pids"], _RUN["desktop"]


def codex_ancestor():
    """훅을 부른 codex 프로세스 (pid, 실행 경로·인자) — 부모를 거슬러 올라가며. 못 찾으면 (None, None).
    데스크톱 앱 안의 codex 는 경로에 .app/ 이, 공유 데몬(codex app-server)은 인자에 app-server 가 들어 있다."""
    pid = os.getppid()
    for _ in range(6):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return None, None
        parts = out.split(None, 1)
        if len(parts) < 2:
            return None, None
        if os.path.basename(parts[1].split()[0]) == "codex":
            return pid, parts[1].strip()
        try:
            pid = int(parts[0])
        except ValueError:
            return None, None
        if pid <= 1:
            return None, None
    return None, None


def _tuis():
    """터미널에서 도는 codex 화면(TUI) 프로세스 [(pid, tty, 인자)] — 데몬·데스크톱 앱 것은 뺀다."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,tty=,args="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or parts[1] in ("??", "-"):
            continue
        exe = parts[2].split()[0]
        if os.path.basename(exe) == "codex" and ".app/" not in exe and " app-server" not in parts[2]:
            rows.append((int(parts[0]), "/dev/" + parts[1], parts[2]))
    return rows


def _pid_terminal(pid):
    """그 프로세스의 환경 변수로 본 터미널 정보(focus.process_terminal 과 같은 방식 — 터미널 키만 읽고 남기지 않는다)."""
    from csl import terminals
    want = {v for t in terminals.registry().values() for v in t.env_keys.values()}
    want |= {var for _k, var in (("program", "TERM_PROGRAM"), ("bundle", "__CFBundleIdentifier"))}
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    env = {}
    for tok in out.split():
        k, eq, v = tok.partition("=")
        if eq and k in want:
            env[k] = v
    info = {key: env[var] for key, var in (("program", "TERM_PROGRAM"), ("bundle", "__CFBundleIdentifier")) if env.get(var)}
    info.update(terminals.env_info(env))
    return info


def tui_for(conn, sid, cwd):
    """공유 데몬이 부른 훅에서: 그 세션을 보여 주는 codex 화면 프로세스 (pid, tty) 또는 (None, None).
    `codex resume <id>` 면 인자로, 아니면 같은 폴더에서 도는 것 중 다른 세션이 쓰지 않는 가장 최근 것(없으면 잡힌 것이라도)."""
    tuis = _tuis()
    hit = [t for t in tuis if sid in t[2]]
    if hit:
        return hit[0][0], hit[0][1]
    cwds = _cwds({t[0] for t in tuis})
    want = os.path.realpath(cwd) if cwd else ""
    same = {pid for path, pid in cwds.items() if path == want}
    claimed = set()
    for r in conn.execute("SELECT terminal FROM sessions WHERE agent='codex' AND session_id != ? AND state != 'ended'"
                          " AND terminal LIKE '%codex_pid%'", (sid,)):
        try:
            claimed.add(json.loads(r["terminal"]).get("codex_pid"))
        except (ValueError, AttributeError):
            pass
    # 다른 세션이 쓰지 않는 화면을 먼저. 없으면 잡혀 있던 화면 — 같은 화면이 /fork·/new 로 다른 대화로 넘어간 것
    # (그 전 세션은 _detach_others 가 뗀다)
    cand = sorted((t for t in tuis if t[0] in same and t[0] not in claimed), reverse=True) \
        or sorted((t for t in tuis if t[0] in same), reverse=True)
    return (cand[0][0], cand[0][1]) if cand else (None, None)


def is_running(session, pids=None, desktop=None):
    """그 Codex 세션이 지금 열려 있는지. CLI 는 훅이 적어 둔 codex pid, 데스크톱 스레드는 앱이 떠 있는지."""
    if pids is None:
        pids, desktop = running_pids()
    term = session.get("terminal")
    if isinstance(term, str):
        try:
            term = json.loads(term)
        except ValueError:
            term = None
    term = term or {}
    if term.get("detached"):   # 그 탭이 다른 대화로 넘어갔다
        return False
    if term.get("codex_desktop"):
        return bool(desktop)
    pid = term.get("codex_pid")
    if pid:
        return pid in pids
    # 훅이 없어 프로세스를 모른다 — 같은 폴더에서 codex CLI 가 돌고 있고 최근에 움직인 세션이면 그것으로 본다
    cwd = os.path.realpath(session["cwd"]) if session.get("cwd") else ""
    return bool(cwd and cwd in _RUN["cwds"] and time.time() - (session.get("last_activity") or 0) < STALE_SECS)


# ------------------------------------------------------------------ 보내기(보드 입력창·「끝나면 보내기」 예약)
def tab_of(sess):
    """그 세션의 codex 가 돌고 있는 터미널 Target 또는 None."""
    from csl import focus
    tg = focus.target_of(sess)
    if not tg:
        return None
    info = tg.terminal.info(tg.handle) or {}
    return tg if info.get("agent") == "codex" or (info.get("agent") is None and is_running(sess)) else None


def busy(tg):
    """화면에 작업 중 표시(… esc to interrupt)가 있으면 True."""
    return any("esc to interrupt" in line for line in tg.terminal.screen(tg.handle))


def paste(tg, text, paths=()):
    """Codex 입력창에 붙여 넣고 Enter. 붙여넣기 괄호로 감싸야 줄바꿈이 보내기가 되지 않는다.
    이미지 경로를 붙여 넣으면 Codex 가 첨부로 바꾼다. (성공, 오류)"""
    # 입력창이 안 보이면(선택 창·대화 기록 찾기 「Find:」 등이 떠 있음) — 글·/ 가 그 창으로 들어가지 않게 Esc 로 입력창을 되찾는다
    for _ in range(3):
        if ready(tg) or not tg.terminal.screen(tg.handle):
            break
        tg.terminal.write(tg.handle, "\x1b")
        time.sleep(0.35)
    payload = text + ("\n" if text and paths else "") + "\n".join(paths)
    if not tg.terminal.write(tg.handle, "\x1b[200~" + payload + "\x1b[201~"):
        return False, "터미널에 쓰지 못했습니다"
    time.sleep(0.6)   # 빠르게 들어온 글 바로 뒤의 Enter 는 Codex 가 붙여넣기의 줄바꿈으로 본다
    if not tg.terminal.write(tg.handle, "\r"):
        return False, "Enter 를 보내지 못했습니다"
    return True, ""


def queue(sid, text):
    """`codex queue` — 그 스레드에 메시지를 넣는다(데스크톱 앱의 대화·터미널 밖). (성공, 오류)"""
    b = binary()
    if not b:
        return False, "codex 명령을 찾지 못했습니다"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE"))}
    env["CSL_SKIP"] = "1"
    try:
        r = subprocess.run([b, "queue", "--thread", sid, "--message", text], capture_output=True, text=True, timeout=40,
                           env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:200]
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "실패").strip()[-300:]
    return True, ""


# ------------------------------------------------------------------ 슬래시 명령(입력창 자동완성)
# Codex CLI 0.167 의 / 목록(설명은 Codex 그대로). interactive 는 터미널에 창을 띄우는 명령 — 보드가 보낸 뒤 그 터미널을 연다.
# Codex 실행 파일에서 목록을 읽을 수 없어 적어 둔다(새 명령은 목록에 없어도 그대로 쳐서 보낼 수 있다)
_I, _B = "interactive", "builtin"
COMMANDS = [{"name": n, "desc": d, "kind": k, "hint": h} for n, d, k, h in (
    ("model", "choose what model and reasoning effort to use", _I, ""),
    ("fast", "2x speed, increased usage", _B, ""),
    ("permissions", "choose what Codex is allowed to do", _I, ""),
    ("compact", "summarize conversation to prevent hitting the context limit", _B, ""),
    ("review", "review my current changes and find issues", _I, ""),
    ("diff", "show git diff (including untracked files)", _I, ""),
    ("status", "show current session configuration and token usage", _I, ""),
    ("fork", "fork the current chat", _I, ""),
    ("side", "start a side conversation in an ephemeral fork", _I, ""),
    ("new", "start a new chat during a conversation", _B, ""),
    ("clear", "clear the terminal and start a new chat", _B, ""),
    ("resume", "resume a saved chat", _I, ""),
    ("rename", "rename the current thread", _B, "<name>"),
    ("goal", "set or view the goal for a long-running task", _B, "<goal>"),
    ("plan", "switch to Plan mode", _B, ""),
    ("recap", "summarize the current conversation now", _B, ""),
    ("init", "create an AGENTS.md file with instructions for Codex", _B, ""),
    ("agents", "open the agent command center", _I, ""),
    ("subagents", "switch between this session's subagents", _I, ""),
    ("skills", "use skills to improve how Codex performs specific tasks", _I, ""),
    ("mention", "mention a file", _I, ""),
    ("worktree", "start or continue a conversation in a new worktree", _I, ""),
    ("presets", "run or save reusable prompts and slash commands", _I, ""),
    ("memories", "configure memory use and generation", _I, ""),
    ("mcp", "list MCP tools; use /mcp verbose or /mcp login <name>", _I, ""),
    ("plugins", "browse plugins", _I, ""),
    ("hooks", "view and manage lifecycle hooks", _I, ""),
    ("approve", "approve one retry of a recent auto-review denial", _I, ""),
    ("usage", "view account usage or use a usage limit reset", _I, ""),
    ("ps", "list background terminals", _I, ""),
    ("stop", "stop all background terminals", _B, ""),
    ("copy", "copy the last response or part of it", _I, ""),
    ("export", "export the conversation as markdown", _B, ""),
    ("cd", "change the current working directory", _B, "<dir>"),
    ("pwd", "show the current working directory", _I, ""),
    ("app", "continue this session in the Desktop app", _B, ""),
    ("archive", "archive this session", _B, ""),
    ("config", "configure Codex preferences", _I, ""),
    ("experimental", "toggle experimental features", _I, ""),
    ("statusline", "configure which items appear in the status line", _I, ""),
    ("warnings", "view retained warnings and diagnostic details", _I, ""),
    ("import", "import setup, this project, and recent chats from Claude Code", _I, ""),
    ("vim", "toggle Vim mode for the composer", _B, ""),
    ("exit", "exit Codex", _B, ""),
)]


# ------------------------------------------------------------------ 모델·추론 단계 바꾸기(이 세션만)
# Codex 의 /model 은 모델 목록 → (모델에 따라) 추론 단계 목록 순서다. 추론 단계 창에서 「s」는 이 세션에만, Enter 는 기본값까지
# 바꾼다 — 늘 s 로 고른다. 화면을 읽어 커서 줄을 확인하며 화살표로 옮기고, 확인이 어긋나면 Esc 로 닫고 아무것도 바꾸지 않는다
EFFORT_LABELS = {"minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high",
                 "max": "Max", "ultra": "Ultra"}
CHOOSE_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")   # 보드 메뉴에 보일 단계(모델이 지원하는 것만 고를 수 있다)
_ROW = re.compile(r"^\s*(›)?\s*(\d+)\.\s+(.+?)(?:\s{2,}|$)")
_FOOT = re.compile(r"^\s{2}(\S.*?) (minimal|low|medium|high|xhigh|extra high|max|ultra)(?: · |$)", re.I)


def _rows(tg):
    """(제목 줄, [(번호, 이름, 커서인지)]) — 지금 화면의 선택 창."""
    lines = tg.terminal.screen(tg.handle)
    title, rows = "", []
    for line in lines:
        st = line.strip()
        if st.startswith(("Select ", "Advanced Reasoning")):
            title, rows = st, []
            continue
        m = _ROW.match(line)
        if m and title:
            name = re.sub(r"\s*\((current|default)\)", "", m.group(3)).strip()
            rows.append((int(m.group(2)), name, bool(m.group(1))))
    return title, rows


def _wait_rows(tg, want, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        title, rows = _rows(tg)
        if rows and want in title:
            return title, rows
        time.sleep(0.2)
    return None, []


def _move_to(tg, want, want_title):
    """선택 창의 커서를 이름이 want 인 줄로. 성공하면 True."""
    for _ in range(14):
        title, rows = _rows(tg)
        if want_title not in (title or ""):
            return False
        cur = next((r for r in rows if r[2]), None)
        target = next((r for r in rows if r[1].lower() == want.lower()), None)
        if not cur or not target:
            return False
        if cur[0] == target[0]:
            return True
        tg.terminal.write(tg.handle, "\x1b[B" if target[0] > cur[0] else "\x1b[A")
        time.sleep(0.25)
    return False


_CHANGED = re.compile(r"Model changed to (\S+)(?: (minimal|low|medium|high|xhigh|extra high|max|ultra))?\s+for this session", re.I)


def changed_to(tg):
    """바꾼 뒤 Codex 가 찍는 「Model changed to <id> <단계> for this session only」의 (모델 id, 단계) — 마지막 것."""
    for line in reversed(tg.terminal.screen(tg.handle)):
        m = _CHANGED.search(line)
        if m:
            e = (m.group(2) or "").lower() or None
            return m.group(1), "xhigh" if e == "extra high" else e
    return None, None


def footer(tg):
    """입력창 아래 줄의 (모델 이름, 추론 단계) — 예: ("GPT-6-Astra", "medium"). 모르면 (None, None)."""
    for line in reversed(tg.terminal.screen(tg.handle)):
        m = _FOOT.match(line)
        if m:
            e = m.group(2).lower()
            return m.group(1), "xhigh" if e == "extra high" else e
    return None, None


def _esc_all(tg):
    """떠 있는 창을 닫는다 — 입력창이 돌아오면 멈춘다. 빈 입력창에서 Esc 를 두 번 누르면 Codex 가 「이전 메시지로 돌아가기」
    (대화 기록 보기)를 열고, 거기서 / 가 「찾기」가 되어 다음에 보낸 /quit 등이 검색어로 들어갔다."""
    for _ in range(3):
        if ready(tg) and not picker(tg):
            return
        tg.terminal.write(tg.handle, "\x1b")
        time.sleep(0.35)


def set_model_effort(tg, model_name=None, effort=None):
    """(성공, 문구). model_name 은 /model 창의 이름(예: GPT-6.1-Sol). 바꾼 뒤 아래 줄로 확인한다."""
    if busy(tg):
        return False, "작업 중이라 바꾸지 않았습니다"
    if effort and effort not in EFFORT_LABELS:
        return False, "모르는 추론 단계입니다"
    cur_model, cur_effort = footer(tg)
    ok, err = paste(tg, "/model")
    if not ok:
        return False, err
    title, rows = _wait_rows(tg, "Select Model")
    if not rows:
        _esc_all(tg)
        return False, "모델 선택 창을 읽지 못했습니다"
    want_model = model_name or next((r[1] for r in rows if r[2]), None)
    if model_name is None and cur_model:   # 모델은 그대로 — 지금 모델 줄에서
        want_model = next((r[1] for r in rows if r[1].lower() == cur_model.lower()), want_model)
    if not want_model or not _move_to(tg, want_model, "Select Model"):
        _esc_all(tg)
        return False, "그 모델을 선택 창에서 찾지 못했습니다: %s" % (model_name or "")
    tg.terminal.write(tg.handle, "\r")
    title, rows = _wait_rows(tg, "Select Reasoning", timeout=2.5)
    if rows:
        label = EFFORT_LABELS.get(effort or cur_effort or "")
        if not label:   # 지금 단계를 모르면 커서 줄(현재 값) 그대로
            label = next((r[1] for r in rows if r[2]), None)
        if label in ("Max", "Ultra"):
            if not _move_to(tg, "More reasoning…", "Select Reasoning"):
                _esc_all(tg)
                return False, "이 모델은 %s 단계를 고를 수 없습니다" % label
            tg.terminal.write(tg.handle, "\r")
            title, rows = _wait_rows(tg, "Advanced Reasoning", timeout=2.5)
            if not rows or not _move_to(tg, label, "Advanced Reasoning"):
                _esc_all(tg)
                return False, "이 모델은 %s 단계를 고를 수 없습니다" % label
        elif not label or not _move_to(tg, label, "Select Reasoning"):
            _esc_all(tg)
            return False, "이 모델은 %s 단계를 고를 수 없습니다" % (effort or "")
        tg.terminal.write(tg.handle, "s")   # 이 세션만(Enter 는 기본값까지 바꾼다)
    time.sleep(1.0)
    # 확인: Codex 가 찍는 바뀐 결과 줄(이 세션만) — 없으면 아래 줄(max·ultra 는 아래 줄 대신 큰 글씨가 뜬다)
    slug, got_effort = changed_to(tg)
    got_model = None
    if slug:
        got_model = next((m["name"] for m in models() if m["slug"] == slug), slug)
    else:
        got_model, got_effort = footer(tg)
    if model_name and (got_model or "").lower() != model_name.lower() and (slug or "") != slug_of(model_name):
        return False, "바꾼 뒤 확인하지 못했습니다(지금: %s)" % (got_model or "알 수 없음")
    if effort and got_effort and got_effort != effort:
        return False, "추론 단계를 맞추지 못했습니다(지금: %s)" % got_effort
    return True, "바꿨습니다 · %s%s (이 세션만)" % (got_model or model_name or "", " · " + got_effort if got_effort else "")


def fork(tg, new_worktree=False):
    """/fork — 이 탭이 갈라진 새 대화로 넘어간다. 어디서 돌릴지 묻는 창이 뜨면(git 저장소) 고른다. (성공, 문구)"""
    if busy(tg):
        return False, "작업 중에는 가를 수 없습니다"
    ok, err = paste(tg, "/fork")
    if not ok:
        return False, err
    want = "Create new Git worktree" if new_worktree else "Use current Git worktree"
    end = time.time() + 3
    while time.time() < end:
        lines = tg.terminal.screen(tg.handle)
        if any("Where should the forked conversation run" in l for l in lines):
            for _ in range(6):
                rows = [(int(m.group(2)), m.group(3).strip(), bool(m.group(1))) for m in map(_ROW.match, tg.terminal.screen(tg.handle)) if m]
                cur = next((r for r in rows if r[2]), None)
                target = next((r for r in rows if r[1].startswith(want)), None)
                if not cur or not target:
                    break
                if cur[0] == target[0]:
                    tg.terminal.write(tg.handle, "\r")
                    return True, "갈랐습니다 · 이 탭은 갈래 대화로 넘어갔고, 원본은 꺼진 채 남습니다" + (" (새 워크트리)" if new_worktree else "")
                tg.terminal.write(tg.handle, "\x1b[B" if target[0] > cur[0] else "\x1b[A")
                time.sleep(0.25)
            _esc_all(tg)
            return False, "갈래 위치 창을 읽지 못했습니다"
        time.sleep(0.2)
    return True, "갈랐습니다 · 이 탭은 갈래 대화로 넘어갔고, 원본은 꺼진 채 남습니다"


# ------------------------------------------------------------------ 보드에서 / 명령 실행(선택 창·출력을 보드 창으로)
# Codex 의 선택 창은 「제목 · › N. 이름  설명 … · enter select · esc back」 모양이 같다. 보드가 그 목록을 읽어 보여 주고,
# 고르면 화살표로 그 줄에 가서 키(Enter·s …)를 누른다. 다음 창이 뜨면 또 보여 주고, 입력창으로 돌아오면 새로 찍힌 글을 돌려준다
# 번호 목록이 아닌 전체 화면 창(탭·검색·토글)이라 보드에서 그리지 않고 터미널로 여는 명령
TERMINAL_ONLY = {"resume", "agents", "subagents", "side", "mention", "statusline", "title", "keymap", "theme", "pets", "voice", "tui",
                 "hooks", "config", "plugins", "presets", "warnings", "experimental", "skills", "memories", "import"}
_PICK_ROW = re.compile(r"^\s*(›)?\s*(\d+)\.\s+(.+?)(?:\s{2,}(\S.*))?$")
_HINT = re.compile(r"(enter|esc|space|tab|[a-z])\s+([a-z][\w /-]*?)(?=\s+·|\s*$)", re.I)


def picker(tg):
    """지금 화면의 선택 창 {title, sub, rows:[{n, label, desc, current, cursor}], keys:[{key, label}]} 또는 None.
    칸이 좁으면 긴 설명이 여러 줄로 접힌다 — 번호 줄 아래의 깊게 들여 쓴 줄은 그 선택지 설명으로 붙인다."""
    lines = tg.terminal.screen(tg.handle)
    hint_i = next((i for i in range(len(lines) - 1, -1, -1) if "esc" in lines[i] and "·" in lines[i]
                   and re.search(r"\b(enter|esc)\b", lines[i])), None)
    if hint_i is None:
        return None
    rows, pending, i = [], [], hint_i - 1
    while i >= 0:
        line = lines[i]
        m = _PICK_ROW.match(line)
        if m:
            label = m.group(3).strip()
            cur = bool(re.search(r"\((current|default)\)", label))
            desc = " ".join([(m.group(4) or "").strip()] + pending).strip()
            rows.insert(0, {"n": int(m.group(2)), "label": re.sub(r"\s*\((current|default)\)", "", label).strip(),
                            "desc": desc, "current": cur, "cursor": bool(m.group(1))})
            pending = []
        elif not line.strip():
            if rows:   # 목록 위 빈 줄 — 그 위는 제목
                break
        elif len(line) - len(line.lstrip()) >= 6 and not line.strip().startswith(("›", "•")):
            pending.insert(0, line.strip())   # 접힌 설명 줄
        else:
            break
        i -= 1
    if not rows:
        return None
    # 제목: 목록 바로 위(빈 줄을 건너)에 붙어 있는 줄 묶음 — 그 위의 지난 출력은 섞지 않는다
    while i >= 0 and not lines[i].strip():
        i -= 1
    head = []
    while i >= 0 and lines[i].strip() and len(head) < 3:
        head.insert(0, lines[i].strip())
        i -= 1
    keys = [{"key": k.lower(), "label": v.strip()} for k, v in _HINT.findall(lines[hint_i])]
    return {"title": head[0] if head else "", "sub": " ".join(head[1:]),
            "rows": rows, "keys": [k for k in keys if k["key"] not in ("esc",)]}


def _clean_output(lines, cmd):
    out = []
    for line in lines:
        st = line.strip()
        if not st or st == "/" + cmd or st.startswith(("› ", "← for agents", "? for shortcuts")) or _FOOT.match(line) or st.startswith("Worked for"):
            continue
        out.append(line.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


_BASE = {}   # 탭 → 명령을 보내기 직전 화면(선택 창을 고른 뒤 새로 찍힌 글을 가려낼 기준)


def _new_lines(before, after):
    """after 에 새로 생긴 줄 — 선택 창에 가려졌다 다시 보이는 지난 줄은 빼고, 마지막 공통 줄 뒤의 것만."""
    import difflib
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    start = 0
    for b in blocks:
        if any(x.strip() for x in after[b.b:b.b + b.size]) and not all(
                x.strip().startswith(("› ", "← for agents")) or _FOOT.match(x) for x in after[b.b:b.b + b.size] if x.strip()):
            start = max(start, b.b + b.size)
    return after[start:]


def run_command(tg, text, wait=1.6):
    """/명령을 보내고 결과 — {"picker": …} 또는 {"output": 새로 찍힌 글}."""
    import difflib
    before = tg.terminal.screen(tg.handle)
    _BASE[tg.handle] = before
    ok, err = paste(tg, text)
    if not ok:
        return {"error": err}
    end, last = time.time() + wait + 2.5, None
    time.sleep(wait)
    while time.time() < end:   # 화면이 멈출 때까지(출력이 길면 조금 더)
        cur = tg.terminal.screen(tg.handle)
        pk = picker(tg)
        if pk:
            return {"picker": pk}
        if cur == last:
            break
        last = cur
        time.sleep(0.4)
    after = tg.terminal.screen(tg.handle)
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    new = [l for op, _a1, _a2, b1, b2 in sm.get_opcodes() if op in ("insert", "replace") for l in after[b1:b2]]
    return {"output": _clean_output(new, text.lstrip("/").split()[0])}


def choose(tg, n, key="enter"):
    """선택 창에서 n 번 줄을 key 로 고른다. 다음 창이 뜨면 {"picker"}, 아니면 {"output"}."""
    import difflib
    pk = picker(tg)
    if not pk:
        return {"error": "선택 창이 닫혔습니다"}
    for _ in range(20):
        cur = next((r for r in pk["rows"] if r["cursor"]), None)
        if not cur:
            return {"error": "선택 창을 읽지 못했습니다"}
        if cur["n"] == n:
            break
        tg.terminal.write(tg.handle, "\x1b[B" if n > cur["n"] else "\x1b[A")
        time.sleep(0.2)
        pk = picker(tg) or pk
    else:
        return {"error": "그 줄로 가지 못했습니다"}
    tg.terminal.write(tg.handle, "\r" if key == "enter" else key[:1])
    time.sleep(1.2)
    pk2 = picker(tg)
    if pk2 and pk2 != pk:
        return {"picker": pk2}
    after = tg.terminal.screen(tg.handle)
    base = _BASE.pop(tg.handle, [])
    seen = {ln.strip() for ln in base if ln.strip()}   # 고른 뒤 결과 — 명령 전 화면에 이미 있던 줄(가려졌다 다시 보이는 것)은 뺀다
    return {"output": _clean_output([ln for ln in _new_lines(base, after) if ln.strip() not in seen], "")}


def cancel(tg):
    _esc_all(tg)


_SESSION_LINE = re.compile(r"Session:\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def ready(tg):
    """입력창(› …)과 아래 줄이 보이면 True — 신뢰·업데이트 같은 시작 창이 닫혔다."""
    lines = tg.terminal.screen(tg.handle)
    return any(line.startswith("› ") and not re.match(r"›\s*\d+\.", line) for line in lines) and footer(tg)[0] is not None


def session_id_of(tg, timeout=90):
    """막 띄운 codex 의 세션 ID — Codex 는 첫 요청 전에는 기록(스레드)을 만들지 않아 훅·동기화로는 모른다.
    입력창이 뜨면(시작 창은 사람이 고른다) /status 를 보내 화면의 Session: 줄을 읽는다. 못 읽으면 None."""
    end = time.time() + timeout
    while time.time() < end and not ready(tg):
        if tg.terminal.exited(tg.handle) is not False:
            return None   # 탭이 닫혔거나 셸이 끝났다(뜨는 동안은 전면 프로세스가 아직 셸이라 그것으로는 판단하지 않는다)
        time.sleep(0.5)
    if not ready(tg):
        return None
    ok, _ = paste(tg, "/status")
    if not ok:
        return None
    for _ in range(10):
        time.sleep(0.4)
        for line in tg.terminal.screen(tg.handle):
            m = _SESSION_LINE.search(line)
            if m:
                return m.group(1)
    return None


def slug_of(name):
    """/model 창의 이름(GPT-6.1-Sol) → 모델 id(gpt-6.1-sol). 모르면 소문자로."""
    for m in models():
        if m["name"].lower() == (name or "").lower() or m["slug"] == name:
            return m["slug"]
    return (name or "").lower().replace(" ", "-") or None


def desktop_session(sess):
    return "codex_desktop" in (sess.get("terminal") or "")


# ------------------------------------------------------------------ 보드 DB 에 넣기
# 보드에서 띄운 codex(보드 터미널 핸들 → {cwd, at, terminal}) — 훅이 없을 때 새 스레드를 그 탭에 잇는다
LAUNCHES = {}


def _title(th):
    t = th.get("name") or th.get("title") or ""
    t = clean_prompt(t) if t.startswith("#") else t
    return " ".join(t.split())[:120] or None


def _upsert_session(conn, th, live):
    sid = th["id"]
    cwd = th.get("cwd") or ""
    t_upd = (th.get("upd_ms") or 0) / 1000 or store.now()
    rollout = th.get("rollout_path")
    row = conn.execute("SELECT session_id, agent, title_source, terminal FROM sessions WHERE session_id=?", (sid,)).fetchone()
    term = {"codex_desktop": True} if is_desktop(th) else None
    if row is None:
        g = store.git_info(cwd)
        started = _ts(th.get("created_at_ms") or th.get("created_at")) or t_upd
        state = "idle" if live else "ended"
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, agent, cwd, project_root, project_name, worktree, branch,"
            " transcript_path, state, state_at, started_at, last_activity, last_seen_at, ended_at, source, terminal, model, effort)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, AGENT, cwd, g["project_root"], g["project_name"], g["worktree"], th.get("git_branch") or g["branch"],
             rollout, state, t_upd, started, t_upd, t_upd, None if live else t_upd, "codex",
             json.dumps(term) if term else None, th.get("model"), th.get("reasoning_effort")))
    else:
        if not conn.execute("SELECT project_root FROM sessions WHERE session_id=?", (sid,)).fetchone()[0]:
            g = store.git_info(cwd)   # 보드 탭과 먼저 이어진 세션(폴더 정보 없이 들어감)
            conn.execute("UPDATE sessions SET cwd=?, project_root=?, project_name=?, worktree=?, branch=? WHERE session_id=?",
                         (cwd, g["project_root"], g["project_name"], g["worktree"], th.get("git_branch") or g["branch"], sid))
        conn.execute("UPDATE sessions SET agent=?, transcript_path=COALESCE(?, transcript_path),"
                     " model=COALESCE(?, model), effort=COALESCE(?, effort) WHERE session_id=?",
                     (AGENT, rollout, th.get("model"), th.get("reasoning_effort"), sid))
        if term and not row["terminal"]:
            conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(term), sid))
    # /fork 로 갈라 온 스레드 — 원본 칸의 갈래로 묶는다(원본이 기록에 있을 때만, 한 번)
    cur = conn.execute("SELECT continued_from FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if cur and not cur[0]:
        parent, at = fork_origin(rollout)
        if parent and parent != sid and conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (parent,)).fetchone():
            conn.execute("UPDATE sessions SET continued_from=?, branch_kind='fork', branch_at=? WHERE session_id=?",
                         (parent, at or store.now(), sid))
    title = _title(th)
    if title:   # 보드·사용자가 붙인 이름은 그대로
        conn.execute("UPDATE sessions SET title=?, title_source='ai' WHERE session_id=? AND title IS NOT ?"
                     " AND COALESCE(title_source, '') NOT IN ('custom', 'board')", (title, sid, title))


def _upsert_turns(conn, sid, turns):
    """턴을 넣거나 맞춘다(prompt_id = Codex turn_id). 바뀐 게 있으면 (새 응답 턴 id 목록, 마지막 턴)."""
    responded = []
    for t in turns:
        if t["prompt"] is None and t["response"] is None and t["status"] == "done":
            continue
        prompt = store.mask(clean_prompt(t["prompt"])) if t["prompt"] is not None else None
        resp = store.mask(t["response"] or t.get("error") or "", private=False) or None
        row = conn.execute("SELECT id, status, response_at, response, prompt FROM turns WHERE session_id=? AND prompt_id=?",
                           (sid, t["turn_id"])).fetchone()
        if row is not None and row["prompt"] is None and prompt is not None:
            # 턴 시작을 요청 글보다 먼저 읽은 경우 — 요청 글이 기록되면 채운다
            conn.execute("UPDATE turns SET prompt=? WHERE id=?", (prompt, row["id"]))
        if row is None:
            tid = conn.execute(
                "INSERT INTO turns (session_id, prompt_id, prompt, prompt_at, response, response_at, status) VALUES (?,?,?,?,?,?,?)",
                (sid, t["turn_id"], prompt, t["prompt_at"], resp if t["status"] != "running" else None,
                 t["response_at"] if t["status"] != "running" else None, t["status"])).lastrowid
            if t["status"] != "running" and resp:
                responded.append(tid)
        elif row["status"] == "running" and t["status"] != "running":
            conn.execute("UPDATE turns SET response=?, response_at=?, status=? WHERE id=?",
                         (resp, t["response_at"] or store.now(), t["status"], row["id"]))
            if resp:
                responded.append(row["id"])
        elif row["status"] != "running" and resp and not row["response"]:
            conn.execute("UPDATE turns SET response=? WHERE id=?", (resp, row["id"]))
    return responded


def _settle_state(conn, sid, turns, live, t_upd):
    """턴으로 상태를 맞춘다: 마지막 턴이 도는 중이면 작업 중(확인 필요는 훅이 맡으니 그대로), 끝났으면 완료.
    보관(archived)은 건드리지 않는다. 끝난(ended) 세션은 그 뒤에 새 요청이 왔을 때만 다시 살린다(종료 직후 되살아나지 않게)."""
    row = conn.execute("SELECT state, state_at, ended_at FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if row is None:
        return
    st, now = row["state"], store.now()
    last = turns[-1] if turns else None
    newer_than_end = bool(last and (last["prompt_at"] or 0) > (row["ended_at"] or 0) + 1)
    if last and last["status"] == "running" and now - (last["prompt_at"] or now) < 6 * 3600:
        if not live:
            # 프로세스가 사라졌는데 끝 표시 없이 남은 턴(탭을 닫음·강제 종료) — 작업 중으로 매달아 두지 않는다
            conn.execute("UPDATE turns SET status='interrupted', response_at=COALESCE(response_at, ?) WHERE session_id=?"
                         " AND prompt_id=? AND status='running'", (now, sid, last["turn_id"]))
            if st in ("working", "waiting"):
                conn.execute("UPDATE sessions SET state='done', state_detail=NULL, state_at=? WHERE session_id=?", (now, sid))
        elif st not in ("working", "waiting") and (st != "ended" or newer_than_end):
            conn.execute("UPDATE sessions SET state='working', state_detail=NULL, state_at=?, ended_at=NULL"
                         " WHERE session_id=?", (now, sid))
    elif last and st in ("working", "waiting"):
        # 훅이 방금 작업 중으로 바꿨는데 기록엔 아직 새 턴이 안 보이는 순간 — 끝난 지난 턴으로 완료라 하지 않는다
        if (last["response_at"] or last["prompt_at"] or 0) + 1 < (row["state_at"] or 0):
            return
        state = {"failed": "error", "interrupted": "done"}.get(last["status"], "done")
        conn.execute("UPDATE sessions SET state=?, state_detail=?, state_at=? WHERE session_id=?",
                     (state, (last.get("error") or None) if state == "error" else None, now, sid))
    elif not live and st not in ("ended",) and now - t_upd > STALE_SECS:
        conn.execute("UPDATE sessions SET state='ended', state_at=?, ended_at=? WHERE session_id=?", (now, now, sid))
    elif live and st == "ended" and newer_than_end and now - t_upd < STALE_SECS:
        conn.execute("UPDATE sessions SET state='idle', state_at=?, ended_at=NULL WHERE session_id=?", (now, sid))


_SYNC = {"mark": None, "mtimes": {}}


def sync(conn, force=False):
    """바뀐 Codex 스레드를 보드 DB 에. 서버가 몇 초마다 부른다. 바뀐 게 없으면 DB 를 열지도 않는다."""
    if not available():
        return 0
    state_db = _db_path("state")
    if not state_db:
        return 0
    mark = tuple(_mtime(state_db + x) for x in ("", "-wal"))
    live_rollouts = {}
    for p in glob.glob(os.path.join(HOME, "sessions", "*", "*", "*", "rollout-*.jsonl")):
        m = _mtime(p)
        if time.time() - m < 2 * 86400:
            live_rollouts[p] = m
    changed_rollouts = {p for p, m in live_rollouts.items() if _SYNC["mtimes"].get(p) != m}
    if not force and mark == _SYNC["mark"] and not changed_rollouts:
        return 0
    since_ms = store.get_setting(conn, "codex_sync_ms", None)
    if since_ms is None:
        since_ms = (time.time() - FIRST_SYNC_DAYS * 86400) * 1000
    ths = {th["id"]: th for th in threads(since_ms - 5000)}   # 몇 초 겹쳐 읽는다(같은 초에 바뀐 것) — 넣기는 여러 번 해도 같다
    ids_by_rollout = [p for p in changed_rollouts]
    if ids_by_rollout:
        m = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
        extra = [m.search(p).group(1) for p in ids_by_rollout if m.search(p)]
        extra = [x for x in extra if x not in ths]
        for th in threads(ids=extra) if extra else []:
            ths[th["id"]] = th
    deleted = set(store.get_setting(conn, "codex_deleted", []) or [])
    pids, desktop = running_pids()
    n, top, failed = 0, since_ms, None
    first = store.get_setting(conn, "codex_sync_ms", None) is None
    for th in ths.values():
        sid = th["id"]
        t_upd = (th.get("upd_ms") or 0) / 1000
        if sid in deleted:
            # 보드에서 지운 대화 — 그 뒤 새 요청이 오면(다시 이어 씀) 다시 들어오고, 아니면 그대로 둔다
            last = (thread_turns(th) or [{}])[-1]
            if not last or (last.get("prompt_at") or 0) <= _deleted_at(conn, sid):
                continue
        try:
            turns = thread_turns(th)
            if not turns and not th.get("has_user_event", 1):
                continue
            with conn:
                _link_launch(conn, th, conn.execute("SELECT terminal FROM sessions WHERE session_id=?", (sid,)).fetchone())
                existing = conn.execute("SELECT terminal FROM sessions WHERE session_id=?", (sid,)).fetchone()
                sess = {"terminal": existing["terminal"] if existing else (json.dumps({"codex_desktop": True}) if is_desktop(th) else None),
                        "cwd": th.get("cwd"), "last_activity": t_upd}
                live = (is_running(sess, pids, desktop) and (time.time() - t_upd < STALE_SECS or not is_desktop(th))) \
                    or bool(sess["terminal"] and tab_of(sess))   # 보드 터미널 탭에서 codex 가 도는 중
                _upsert_session(conn, th, live or time.time() - t_upd < STALE_SECS)
                responded = _upsert_turns(conn, sid, turns)
                for tid in responded:
                    r = conn.execute("SELECT response, response_at FROM turns WHERE id=?", (tid,)).fetchone()
                    # 처음 가져온 지난 대화의 응답은 활동(새 응답 알림)에 넣지 않는다
                    if r and not first and store.now() - (r["response_at"] or 0) < 600:
                        store.add_event(conn, sid, "response", r["response"] or "", tid)
                last_at = max([t_upd] + [t["response_at"] or t["prompt_at"] or 0 for t in turns[-1:]])
                conn.execute("UPDATE sessions SET last_activity=MAX(COALESCE(last_activity, 0), ?) WHERE session_id=?", (last_at, sid))
                _settle_state(conn, sid, turns, live, t_upd)
            n += 1
            top = max(top, th.get("upd_ms") or 0)
        except Exception:  # noqa: BLE001 - 스레드 하나의 형식 문제가 나머지 동기화를 멈추면 안 된다
            import traceback
            traceback.print_exc()
            failed = t_upd * 1000 if failed is None else min(failed, t_upd * 1000)
            continue
    if failed is not None:   # 못 읽은 스레드가 다음에 다시 읽히도록 그 앞에서 멈춘다
        top = min(top, failed - 1)
    with conn:
        store.set_setting(conn, "codex_sync_ms", top)
    _SYNC["mark"] = mark
    _SYNC["mtimes"].update({p: live_rollouts[p] for p in changed_rollouts})
    return n


def _deleted_at(conn, sid):
    """지운 시각(모르면 지금) — 지운 뒤에 온 요청만 다시 가져온다."""
    at = store.get_setting(conn, "codex_deleted_at", {}) or {}
    return at.get(sid) or store.now()


def _link_launch(conn, th, existing):
    """보드에서 띄운 codex 탭과 새 스레드를 잇는다(훅이 없을 때). 같은 폴더·띄운 뒤 생긴 첫 스레드."""
    if (existing and existing["terminal"]) or is_desktop(th):   # 앱의 대화는 보드 탭이 아니다
        return
    created = _ts(th.get("created_at_ms") or th.get("created_at")) or 0
    for handle, x in list(LAUNCHES.items()):
        if time.time() - x["at"] > 600:
            LAUNCHES.pop(handle, None)
            continue
        if x.get("resume") and x["resume"] != th["id"]:
            continue
        if not x.get("resume") and (os.path.realpath(x["cwd"]) != os.path.realpath(th.get("cwd") or "") or created < x["at"] - 5):
            continue
        LAUNCHES.pop(handle, None)
        term = dict(x["terminal"])
        if x.get("fork_of"):   # 보드에서 가른 갈래 — 원본에 잇는다(rollout 의 forked_from_id 를 못 읽어도)
            conn.execute("INSERT OR IGNORE INTO sessions (session_id, agent, cwd, state, state_at, started_at, last_activity, source)"
                         " VALUES (?,?,?,?,?,?,?,?)", (th["id"], AGENT, th.get("cwd") or "", "idle", store.now(),
                                                       created or store.now(), store.now(), "codex"))
            conn.execute("UPDATE sessions SET continued_from=COALESCE(continued_from, ?), branch_kind='fork',"
                         " branch_at=COALESCE(branch_at, ?) WHERE session_id=?", (x["fork_of"], store.now(), th["id"]))
        conn.execute("INSERT OR IGNORE INTO sessions (session_id, agent, cwd, state, state_at, started_at, last_activity, source)"
                     " VALUES (?,?,?,?,?,?,?,?)", (th["id"], AGENT, th.get("cwd") or "", "idle", store.now(), created or store.now(),
                                                   store.now(), "codex"))
        conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(term), th["id"]))
        return


# ------------------------------------------------------------------ 훅
def handle_hook(data):
    """Codex 훅 입력 → 보드 DB. hook.py 의 Claude 처리와 같은 도구(ensure_session·finish_turn …)를 쓴다."""
    from csl import hook   # 순환 import 방지(hook 이 codex 를 부른다)
    event, sid = data.get("hook_event_name"), data.get("session_id")
    if not event or not sid:
        return None
    started = store.now()
    conn = store.connect()
    try:
        known = conn.execute("SELECT terminal FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if event in ("PostToolUse", "PreToolUse", "SessionEnd", "PostCompact", "SubagentStart", "SubagentStop") and not known:
            return None
        with conn:
            if event in ("SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest", "PreCompact", "Interrupt"):
                hook.ensure_session(conn, data, refresh_git=event == "SessionStart")
                conn.execute("UPDATE sessions SET agent=?, source=CASE WHEN source='hook' THEN 'codex' ELSE source END"
                             " WHERE session_id=? AND agent IS NOT ?", (AGENT, sid, AGENT))
            if event in ("SessionStart", "UserPromptSubmit"):
                _record_terminal(conn, sid, known, data.get("cwd"))
            if event == "SessionStart":
                hook.set_state(conn, sid, "idle", data.get("source"))
                store.add_event(conn, sid, "start", data.get("source"), dedupe=5)
                conn.execute("UPDATE sessions SET ended_at=NULL, archived=0 WHERE session_id=?", (sid,))
            elif event == "UserPromptSubmit":
                t = store.now()
                if not conn.execute("SELECT 1 FROM turns WHERE session_id=? AND prompt_id=?", (sid, data.get("turn_id"))).fetchone():
                    conn.execute("INSERT INTO turns (session_id, prompt_id, prompt, prompt_at, status) VALUES (?,?,?,?,?)",
                                 (sid, data.get("turn_id"), store.mask(clean_prompt(data.get("prompt") or "")), t, "running"))
                hook.set_state(conn, sid, "working")
                conn.execute("UPDATE sessions SET last_seen_at=?, compact_at=NULL WHERE session_id=?", (t, sid))
            elif event == "Stop":
                msg = data.get("last_assistant_message")
                row = conn.execute("SELECT id, status, response FROM turns WHERE session_id=? AND prompt_id=?",
                                   (sid, data.get("turn_id"))).fetchone() if data.get("turn_id") else None
                tid, fresh = None, False
                if row and row["status"] == "running":   # 그 턴(turn_id)에 바로 — 늦게 온 다음 요청과 섞이지 않게
                    conn.execute("UPDATE turns SET response=?, response_at=?, status='done' WHERE id=?",
                                 (store.mask(msg, private=False), store.now(), row["id"]))
                    tid, fresh = row["id"], True
                elif row:   # 기록 동기화가 먼저 끝난 것으로 맞춰 놓았다 — 응답이 비었으면 채우기만(새 턴·알림을 또 만들지 않는다)
                    if msg and not row["response"]:
                        conn.execute("UPDATE turns SET response=? WHERE id=?", (store.mask(msg, private=False), row["id"]))
                elif not data.get("turn_id"):
                    tid = hook.finish_turn(conn, sid, msg, "done", extras=None, until=started)
                    fresh = tid is not None
                hook.settle(conn, sid, "done", None, started)
                if msg and fresh:
                    store.add_event(conn, sid, "response", msg, tid)
            elif event == "Interrupt":
                t = store.now()
                conn.execute("UPDATE turns SET status='interrupted', response_at=? WHERE session_id=? AND status='running'"
                             " AND response_at IS NULL", (t, sid))
                hook.set_state(conn, sid, "done", "중단됨")
            elif event == "PermissionRequest":
                label = "권한 승인 대기: %s" % _tool_label(data)
                hook.set_state(conn, sid, "waiting", store.mask(label))
                store.add_event(conn, sid, "waiting", label, dedupe=10)
            elif event == "PostToolUse":
                conn.execute("UPDATE sessions SET state='working', state_detail=NULL, state_at=? WHERE session_id=? AND state='waiting'",
                             (store.now(), sid))
            elif event == "PreCompact":
                store.start_compact(conn, sid, data.get("trigger") or "manual")
            elif event == "PostCompact":
                store.finish_compact(conn, sid)
                store.add_event(conn, sid, "compact", data.get("trigger"))
            elif event == "SessionEnd":
                t = store.now()
                conn.execute("UPDATE sessions SET state='ended', state_detail=?, state_at=?, ended_at=? WHERE session_id=?",
                             (data.get("reason"), t, t, sid))
                store.add_event(conn, sid, "end", data.get("reason"))
            if data.get("model"):
                conn.execute("UPDATE sessions SET model=? WHERE session_id=? AND model IS NOT ?", (data["model"], sid, data["model"]))
            pm = data.get("permission_mode")
            if pm:
                conn.execute("UPDATE sessions SET permission_mode=? WHERE session_id=? AND permission_mode IS NOT ?", (pm, sid, pm))
    finally:
        conn.close()
    if event in ("SessionStart", "UserPromptSubmit"):
        hook.ensure_server()
    return None


def _tool_label(data):
    name = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    if isinstance(inp, dict):
        cmd = inp.get("command") or inp.get("cmd")
        if isinstance(cmd, list):
            cmd = cmd[-1] if cmd else ""
        if isinstance(cmd, str) and cmd.strip():
            return "%s — %s" % (name or "명령", " ".join(cmd.split())[:80])
    return name


def _record_terminal(conn, sid, known, cwd=None):
    """훅을 부른 codex 프로세스와 터미널(보드 터미널이면 그 탭)을 적는다. 이미 아는 탭 정보는 남긴다."""
    from csl import hook, terminals
    try:
        old = json.loads(known["terminal"]) if known and known["terminal"] else {}
    except ValueError:
        old = {}
    pid, path = codex_ancestor()
    if pid and " app-server" in (path or "") and ".app/" not in path.split()[0]:
        # 공유 데몬(codex app-server)이 부른 훅 — 환경 변수·tty 는 데몬을 처음 띄운 다른 터미널 것이다.
        # 이 세션을 보여 주는 codex 화면 프로세스를 찾아 그 프로세스의 터미널을 적는다. 못 찾으면 아는 것을 그대로 둔다
        tpid, tty = tui_for(conn, sid, cwd)
        if not tpid:
            return
        info = _pid_terminal(tpid)
        info.update(tty=tty, codex_pid=tpid)
        conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(info), sid))
        _detach_others(conn, sid, info)
        return
    info = {key: os.environ[var] for key, var in hook.TERMINAL_ENV if os.environ.get(var)}
    info.update(terminals.env_info(os.environ))
    tty = hook.ancestor_tty()
    if tty:
        info["tty"] = tty
    if pid and ".app/" not in path.split()[0]:
        info["codex_pid"] = pid
    elif not info.get("backend") and not tty:
        info["codex_desktop"] = True   # 터미널 없이 데스크톱 앱 안의 codex 가 부른 훅
    if old.get("web_pane") and not info.get("backend"):
        info = dict(old, **{k: v for k, v in info.items() if k in ("codex_pid",)})
    conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?", (json.dumps(info), sid))
    _detach_others(conn, sid, info)


def _detach_others(conn, sid, info):
    """같은 codex 화면(탭)이 다른 대화로 넘어갔다(/fork·/new·/resume) — 그 탭을 가리키던 다른 Codex 세션은 떼어
    「꺼진 세션」으로 둔다. 그대로 두면 그 세션으로 보낸 글이 지금 탭의 다른 대화로 들어간다."""
    keys = [(k, info[k]) for k in ("codex_pid", "web_pane", "orca_handle", "tmux_pane") if info.get(k)]
    if not keys:
        return
    for r in conn.execute("SELECT session_id, terminal FROM sessions WHERE agent='codex' AND session_id != ?"
                          " AND terminal IS NOT NULL", (sid,)).fetchall():
        try:
            t = json.loads(r["terminal"]) or {}
        except ValueError:
            continue
        if any(t.get(k) == v for k, v in keys):
            conn.execute("UPDATE sessions SET terminal=? WHERE session_id=?",
                         (json.dumps({"detached": True, "cwd_hint": t.get("cwd_hint")}), r["session_id"]))


# ------------------------------------------------------------------ 훅 설치(~/.codex/hooks.json)
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest", "PostToolUse", "Interrupt",
               "PreCompact", "PostCompact", "SessionEnd")
MARK = "/csl/hook.py"


def hook_command(python, hook_path):
    return "%s %s --agent codex" % (python, hook_path)


def hooks_state():
    """(설치됐는지, 우리 훅이 걸린 이벤트들)."""
    try:
        with open(HOOKS_PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False, []
    got = []
    for ev, groups in ((d.get("hooks") or {}).items()):
        for g in groups or []:
            if any(MARK in (h.get("command") or "") for h in g.get("hooks") or []):
                got.append(ev)
    return bool(got), got


def hooks_trust():
    """(Codex 가 승인한 우리 훅 수, 우리 훅 수). Codex 는 hooks.json 의 새 훅을 한 번 승인받아야 돌리고(/hooks),
    승인은 config.toml 의 [hooks.state."<hooks.json 경로>:<이벤트>:<그룹 번호>:<훅 번호>"] trusted_hash 로 남는다.
    해시가 지금 명령과 맞는지는 알 수 없어(Codex 의 계산 방식) 승인 기록이 있는지만 본다. enabled = false 면 꺼진 것으로."""
    try:
        with open(HOOKS_PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return 0, 0
    ours = []
    for ev, groups in (d.get("hooks") or {}).items():
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", ev).lower()
        for gi, g in enumerate(groups or []):
            for hi, h in enumerate(g.get("hooks") or []):
                if MARK in (h.get("command") or ""):
                    ours.append("%s:%s:%d:%d" % (HOOKS_PATH, snake, gi, hi))
    try:
        with open(os.path.join(HOME, "config.toml"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    state = {}
    for m in re.finditer(r'^\[hooks\.state\."([^"]+)"\][ \t]*\n((?:[^\[\n].*\n?|\n)*)', text, re.M):
        state[m.group(1)] = m.group(2)
    ok = sum(1 for k in ours if "trusted_hash" in state.get(k, "") and not re.search(r"^\s*enabled\s*=\s*false", state[k], re.M))
    return ok, len(ours)


def install_hooks(python, hook_path):
    """우리 훅을 넣는다(다른 훅 — Orca 등 — 은 그대로). 이미 같은 명령이면 손대지 않는다. 바꿨으면 True."""
    try:
        with open(HOOKS_PATH) as f:
            d = json.load(f)
    except FileNotFoundError:
        d = {}
    except ValueError:
        raise RuntimeError("%s 을 읽지 못했습니다(JSON 형식 오류) — 고친 뒤 다시 해 주세요" % HOOKS_PATH)
    hooks = d.setdefault("hooks", {})
    cmd = hook_command(python, hook_path)
    changed = False
    for ev in HOOK_EVENTS:
        groups = hooks.setdefault(ev, [])
        mine = [g for g in groups if any(MARK in (h.get("command") or "") for h in g.get("hooks") or [])]
        want = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if mine == [want]:
            continue
        hooks[ev] = [g for g in groups if g not in mine] + [want]
        changed = True
    if changed:
        os.makedirs(HOME, exist_ok=True)
        if os.path.exists(HOOKS_PATH):
            with open(HOOKS_PATH) as f, open(HOOKS_PATH + ".argus.bak", "w") as b:
                b.write(f.read())
        tmp = HOOKS_PATH + ".argus.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, HOOKS_PATH)
    return changed


def uninstall_hooks():
    try:
        with open(HOOKS_PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    hooks = d.get("hooks") or {}
    changed = False
    for ev in list(hooks):
        keep = [g for g in hooks[ev] or [] if not any(MARK in (h.get("command") or "") for h in g.get("hooks") or [])]
        if keep != hooks[ev]:
            changed = True
            if keep:
                hooks[ev] = keep
            else:
                del hooks[ev]
    if changed:
        tmp = HOOKS_PATH + ".argus.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, HOOKS_PATH)
    return changed
