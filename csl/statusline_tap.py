"""상태줄 입력(JSON)에서 세션별 컨텍스트 사용량·사용 한도를 받아 둔다.

Claude Code 는 상태줄을 그릴 때마다 상태줄 명령의 stdin 으로 세션 정보를 준다. 훅에는 없는
context_window(창 크기·사용 %)·rate_limits 가 여기엔 있다. 사용자의 상태줄 래퍼가 같은 입력을
이 스크립트에도 흘려 주면(백그라운드로) ~/.claude/session-log/status/<session_id>.json 에 남긴다.

    printf '%s' "$stdin_data" | /usr/bin/python3 .../csl/statusline_tap.py &

상태줄 스크립트가 없거나 직접 고치기 어려우면 상태줄 명령 자체를 이걸로 바꾸고 원래 명령을 감싼다(install.py 가 한다).

    /usr/bin/python3 .../csl/statusline_tap.py --wrap '원래 상태줄 명령'

받은 입력을 그대로 원래 명령에 넘기고 그 출력·종료 코드를 돌려준다. --wrap 이 없으면 아무것도 출력하지 않는다.
상태줄은 자주 다시 그려지므로 내용이 바뀌었을 때만 쓴다. 어떤 오류도 상태줄을 막지 않게 조용히 끝낸다.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

STATUS_DIR = os.path.join(os.path.expanduser(os.environ.get("CSL_DATA_DIR", "~/.claude/session-log")), "status")


def pick(d):
    cw = d.get("context_window") or {}
    cu = cw.get("current_usage") or {}
    used = None
    if isinstance(cu, dict) and cu:
        used = sum(int(cu.get(k) or 0) for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out = {
        "ctx_size": cw.get("context_window_size"),
        "ctx_pct": cw.get("used_percentage"),
        "ctx_used": used,
        "model": (d.get("model") or {}).get("id"),
    }
    if isinstance(d.get("rate_limits"), dict):
        out["rate_limits"] = d["rate_limits"]
    return out


def save(raw):
    try:
        d = json.loads(raw)
        sid = d.get("session_id") or ""
        if not re.fullmatch(r"[\w\-]{8,80}", sid):
            return
        data = pick(d)
        os.makedirs(STATUS_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(STATUS_DIR, sid + ".json")
        body = json.dumps(data, sort_keys=True)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == body:
                    os.utime(path)   # 같은 값이어도 「방금 확인함」은 남긴다
                    return
        except OSError:
            pass
        # 상태줄은 동시에 여러 번 그려진다 — 같은 임시 파일을 나눠 쓰면 덜 쓴 파일이 올라갈 수 있어 프로세스마다 따로
        fd, tmp = tempfile.mkstemp(dir=STATUS_DIR, prefix=sid + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception:  # noqa: BLE001 - 상태줄을 막으면 안 된다
        pass


def main():
    try:
        raw = sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        raw = b""
    save(raw)
    if len(sys.argv) >= 3 and sys.argv[1] == "--wrap":
        # 원래 상태줄 명령에 같은 입력을 넘기고 출력·종료 코드를 그대로(Claude Code 처럼 셸로 실행)
        try:
            r = subprocess.run(sys.argv[2], shell=True, input=raw)
            sys.exit(r.returncode)
        except OSError:
            sys.exit(1)


if __name__ == "__main__":
    main()
