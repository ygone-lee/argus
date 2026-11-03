"""훅 설치 전 대화를 기록 파일(~/.claude/projects/*/*.jsonl)에서 가져온다.

    /usr/bin/python3 csl/backfill.py --days 7

이미 훅으로 기록 중인 세션의 턴은 건드리지 않는다(제목·recap 만 갱신).
가져온 응답은 '확인함'으로 둔다(과거 응답이 새 알림으로 쏟아지지 않게).
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csl import store  # noqa: E402

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
INTERRUPT_MARK = "[Request interrupted by user"
COMPACT_MARK = "⟳ 컨텍스트 압축 후 이어서 진행"


def _text_of(content):
    """user/assistant message.content 에서 사람이 읽는 텍스트만. tool_result 가 섞이면 None."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "\n".join(parts) if parts else None


def _prompt_text(text):
    """실제 사람이 친 요청이면 표시용 문자열, 시스템이 끼운 메시지면 None."""
    s = text.strip()
    if not s:
        return None
    if s.startswith("<command-name>"):
        name = s.split("<command-name>", 1)[1].split("</command-name>", 1)[0]
        args = ""
        if "<command-args>" in s:
            args = s.split("<command-args>", 1)[1].split("</command-args>", 1)[0]
        return (name + " " + args).strip()
    if s.startswith("<") or s.startswith("Caveat:"):
        return None
    return text


def parse_transcript(path):
    info = {"session_id": None, "cwd": None, "branch": None, "turns": [], "first_at": None, "last_at": None}
    turn = None
    with open(path, "rb") as f:
        for raw in f:
            if b'"type":"user"' not in raw and b'"type":"assistant"' not in raw \
                    and b'"type": "user"' not in raw and b'"type": "assistant"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("isSidechain") or d.get("isMeta"):
                continue
            ts = store.parse_ts(d.get("timestamp"))
            info["session_id"] = info["session_id"] or d.get("sessionId")
            info["cwd"] = d.get("cwd") or info["cwd"]
            info["branch"] = d.get("gitBranch") or info["branch"]
            if ts:
                info["first_at"] = info["first_at"] or ts
                info["last_at"] = ts
            msg = d.get("message") or {}
            text = _text_of(msg.get("content"))
            if text is None:
                continue
            if d["type"] == "user":
                if d.get("isCompactSummary"):
                    # 압축 요약은 사람이 친 요청이 아니다. 이어지는 응답을 담을 표시 턴만 만든다
                    turn = {"prompt": COMPACT_MARK, "prompt_at": ts, "response": None, "response_at": None,
                            "status": "done"}
                    info["turns"].append(turn)
                    continue
                if text.startswith(INTERRUPT_MARK):
                    if turn:
                        turn["status"] = "interrupted"
                    continue
                prompt = _prompt_text(text)
                if prompt is None:
                    continue
                turn = {"prompt": prompt, "prompt_at": ts, "response": None, "response_at": None,
                        "status": "done"}
                info["turns"].append(turn)
            elif d["type"] == "assistant" and turn is not None:
                turn["response"] = text
                turn["response_at"] = ts
    return info


def backfill(days):
    since = time.time() - days * 86400
    files = [p for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")) if os.path.getmtime(p) >= since]
    conn = store.connect()
    imported = skipped = 0
    git_cache = {}
    try:
        # 이미 아는 세션·대화가 있는 세션을 한 번에(파일마다 묻지 않게). 이번에 넣은 것도 여기에 더한다
        known = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
        with_turns = {r[0] for r in conn.execute("SELECT DISTINCT session_id FROM turns")}
        for path in sorted(files, key=os.path.getmtime):
            try:
                info = parse_transcript(path)
            except OSError:
                continue
            sid = info["session_id"]
            if not sid or not info["turns"]:
                continue
            with conn:
                has_turns = sid in with_turns
                if sid not in known:
                    known.add(sid)
                    cwd = info["cwd"] or ""
                    if cwd not in git_cache:
                        git_cache[cwd] = store.git_info(cwd)
                    g = git_cache[cwd]
                    last_at = info["last_at"] or os.path.getmtime(path)
                    conn.execute(
                        "INSERT INTO sessions (session_id, cwd, project_root, project_name, worktree, branch,"
                        " transcript_path, state, state_at, started_at, last_activity, last_seen_at, source)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, cwd, g["project_root"], g["project_name"], g["worktree"],
                         info["branch"] if info["branch"] and info["branch"] != "HEAD" else g["branch"],
                         path, "idle", last_at, info["first_at"], last_at, last_at, "backfill"),
                    )
                if has_turns:
                    skipped += 1
                else:
                    conn.executemany(
                        "INSERT INTO turns (session_id, prompt, prompt_at, response, response_at, status)"
                        " VALUES (?,?,?,?,?,?)",
                        [(sid, store.mask(t["prompt"]), t["prompt_at"], store.mask(t["response"], private=False),
                          t["response_at"], t["status"] if t["response"] else "interrupted") for t in info["turns"]])
                    with_turns.add(sid)
                    imported += 1
                store.apply_transcript_meta(conn, sid, path)
    finally:
        conn.close()
    print("가져온 세션 %d개, 이미 기록 중이라 턴은 건너뛴 세션 %d개 (대상 파일 %d개)" % (imported, skipped, len(files)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7, help="최근 며칠 안에 수정된 기록만 (기본 7)")
    backfill(ap.parse_args().days)


if __name__ == "__main__":
    main()
