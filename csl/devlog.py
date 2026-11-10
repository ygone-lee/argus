"""개발용 실패 기록: 보드에서 보내기가 실패한 순간의 상황을 파일로 남겨, 나중에 원인을 볼 수 있게 한다.

공식 기능이 아니다 — 저장소에 `.dev/` 폴더가 있을 때만 켜진다(개발하는 Mac 에서만 만들고, .git/info/exclude 로 저장소에
올리지 않는다). 없으면 아무것도 읽거나 쓰지 않는다.

- 자동: 보내기 요청(/api/sessions/<id>/send)이 실패로 끝나면 — 오류 문구, 세션 상태, 터미널, 보내려던 글(앞부분, 비밀값 가림),
  보내기 전 화면과 실패 순간의 화면(tmux 화면 그대로)
- 직접: 칸 ⋮ → 「🐞 지금 상태 저장」 — 오류 없이 이상할 때. 보드 화면 쪽 상태(입력창 글, 최근 알림 문구)도 같이
- `.dev/send-failures/<시각>-<세션 앞 8자리>-<종류>.json`. 7일이 지나거나 50개를 넘으면 오래된 것부터 지운다
"""
import json
import os
import re
import threading
import time

from csl import store

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEV_DIR = os.path.join(REPO, ".dev")
OUT_DIR = os.path.join(DEV_DIR, "send-failures")
KEEP_DAYS, KEEP_MAX = 7, 50
TEXT_MAX = 2000

_trace = threading.local()   # 이 요청 스레드에서 focus 가 남긴 단계(보내기 전 화면 등)


def enabled():
    return os.path.isdir(DEV_DIR)


def start():
    """요청 하나를 시작할 때 — 지난 요청의 단계를 지운다."""
    _trace.steps = []


def step(name, **data):
    """focus 가 보내는 도중 남기는 단계(켜져 있을 때만 부른다). 화면은 줄 목록."""
    if not hasattr(_trace, "steps"):
        _trace.steps = []
    _trace.steps.append(dict(data, step=name, at=round(time.time(), 3)))


def _screen(sess):
    try:
        from csl import focus
        t = focus.target_of(sess)
        return [ln.rstrip() for ln in focus._screen(t)] if t else None
    except Exception as e:  # noqa: BLE001 - 기록이 실패해도 본래 응답은 그대로
        return ["(화면을 읽지 못함: %s)" % e]


def _mask_screen(lines):
    return [store.mask(ln, private=False) for ln in lines] if lines else lines


def _session_info(sess):
    keys = ("session_id", "agent", "title", "state", "state_detail", "state_at", "cwd", "worktree", "branch", "model",
            "effort", "permission_mode", "terminal", "bg", "team_agent", "last_activity")
    info = {k: sess.get(k) for k in keys if k in sess}
    try:
        info["terminal"] = json.loads(info.get("terminal") or "null")
    except (TypeError, ValueError):
        pass
    return info


def record(kind, sess, error=None, text=None, client=None, status=None):
    """기록 파일을 하나 쓰고 그 경로(저장소 기준)를 돌려준다. 꺼져 있거나 실패하면 None."""
    if not enabled() or not sess:
        return None
    try:
        sess = dict(sess)
        t = time.time()
        steps = getattr(_trace, "steps", [])
        for s in steps:
            if "screen" in s:
                s["screen"] = _mask_screen(s["screen"])
        rec = {
            "kind": kind,
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
            "status": status,
            "error": error,
            "text": store.mask(text[:TEXT_MAX]) if isinstance(text, str) else None,
            "text_len": len(text) if isinstance(text, str) else None,
            "text_lines": text.count("\n") + 1 if isinstance(text, str) else None,
            "session": _session_info(sess),
            "steps": steps,
            "screen_now": _mask_screen(_screen(sess)),
            "client": client,
        }
        os.makedirs(OUT_DIR, exist_ok=True)
        name = "%s-%s-%s.json" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(t)),
                                  re.sub(r"[^\w]", "", sess.get("session_id") or "")[:8], kind)
        path = os.path.join(OUT_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        _prune()
        return os.path.relpath(path, REPO)
    except Exception:  # noqa: BLE001 - 개발용 기록이 본래 동작을 막지 않게
        return None
    finally:
        _trace.steps = []


def _prune():
    files = sorted((os.path.join(OUT_DIR, f) for f in os.listdir(OUT_DIR) if f.endswith(".json")), key=os.path.getmtime)
    cutoff = time.time() - KEEP_DAYS * 86400
    for i, p in enumerate(files):
        if os.path.getmtime(p) < cutoff or i < len(files) - KEEP_MAX:
            try:
                os.remove(p)
            except OSError:
                pass
