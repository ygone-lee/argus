"""에이전트 연결 상태: Claude Code·Codex 에 Argus 훅이 들어 있는지 보고, 보드에서 누르면 넣는다.

보드 설정 창 「에이전트 연결」과, 빠진 게 있을 때 보드 위에 뜨는 안내 띠가 쓴다. 확인은 설치 프로그램(install.py)의 함수를
그대로 쓴다. 보드에서 누른 설치는:
- Claude Code: install.py 를 다시 돌린다(훅·상태줄 탭·스킬 링크 — 여러 번 돌려도 한 벌, 바꾸기 전 원본 백업).
  업데이트를 받은 뒤 훅이 새로 늘었을 때도 이것으로 맞춘다.
- Codex: ~/.codex/hooks.json 에 훅만 넣는다(처음 설치할 땐 Codex 가 없었다가 나중에 깐 경우). Codex 는 새 훅을
  한 번 승인받아야 돌린다 — 승인은 보드가 대신할 수 없어 안내만 한다(Codex CLI 의 /hooks).
"""
import importlib.util
import json
import os
import subprocess
import time

from csl import codex, config

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_inst = None
_cache = {"at": 0.0, "v": None}


def inst():
    """install.py 를 모듈로(불러오기만 해서는 아무것도 바꾸지 않는다)."""
    global _inst
    if _inst is None:
        spec = importlib.util.spec_from_file_location("argus_install", os.path.join(REPO, "install.py"))
        _inst = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_inst)
    return _inst


def status():
    i = inst()
    try:
        with open(i.SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    cmd = (settings.get("statusLine") or {}).get("command") or ""
    got = set(codex.hooks_state()[1])
    trusted, ours = codex.hooks_trust()
    return {
        "claude": {"found": bool(i.claude_bin()), "hooks": i.count_ours(settings), "hooks_total": len(i.HOOKS),
                   "statusline": i.TAP_MARK in cmd or i._script_has_tap(cmd), "settings": i.home(i.SETTINGS)},
        "codex": {"found": bool(codex.binary()) or os.path.isdir(codex.HOME), "home": os.path.isdir(codex.HOME),
                  "hooks": len(got & set(codex.HOOK_EVENTS)), "hooks_total": len(codex.HOOK_EVENTS),
                  "trusted": trusted, "ours": ours, "hooks_path": i.home(codex.HOOKS_PATH)},
        "tmux": bool(i.tmux_bin()),
    }


def cached():
    if time.time() - _cache["at"] > 30 or _cache["v"] is None:
        _cache["v"], _cache["at"] = status(), time.time()
    return _cache["v"]


def forget():
    _cache["at"] = 0.0


def issues(conn):
    """보드 위 안내 띠에 낼 것(빠진 것만). key 는 「나중에」로 접을 때 쓰고, 상태가 바뀌면(숫자 포함) 다시 뜬다."""
    s, out = cached(), []
    c, x = s["claude"], s["codex"]
    if c["hooks"] < c["hooks_total"]:
        out.append({"key": "claude-hooks-%d" % c["hooks"], "action": "claude",
                    "text": "Claude Code 훅이 %d/%d 개만 들어 있습니다 — 세션 상태가 보드에 제대로 오지 않을 수 있습니다" % (c["hooks"], c["hooks_total"])})
    if x["found"] and config.get(conn, "codex_enabled"):
        if x["hooks"] < x["hooks_total"]:
            out.append({"key": "codex-hooks-%d" % x["hooks"], "action": "codex",
                        "text": "Codex 에 Argus 훅이 없습니다 — 「확인 필요」 같은 상태가 몇 초 늦게 보입니다" if not x["hooks"]
                        else "Codex 훅이 %d/%d 개만 들어 있습니다" % (x["hooks"], x["hooks_total"])})
        elif x["ours"] and x["trusted"] < x["ours"]:
            out.append({"key": "codex-trust-%d" % x["trusted"], "action": None,
                        "text": "Codex 가 Argus 훅을 아직 승인하지 않았습니다(%d/%d) — Codex CLI 에서 /hooks 를 열어 승인해 주세요" % (x["trusted"], x["ours"])})
    return out


def install_claude():
    """install.py 를 다시 돌린다(묻지 않는 모드 — tmux 설치·새 Codex 스킬은 건너뛴다). (ok, 메시지)."""
    i = inst()
    try:
        r = subprocess.run([i.PYTHON, os.path.join(REPO, "install.py"), "--no-open", "--tmux", "skip", "--codex-skills", "skip"],
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "설치 프로그램을 실행하지 못했습니다: %s" % e
    finally:
        forget()
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return False, "설치 프로그램이 실패했습니다: " + " ".join(tail)
    return True, "Claude Code 훅·상태줄 탭·스킬을 맞췄습니다 · 열려 있는 세션도 다음 요청부터 반영됩니다"


def install_codex():
    i = inst()
    if not os.path.isdir(codex.HOME):
        return False, "Codex 를 한 번 실행한 뒤(~/.codex 가 생긴 뒤) 다시 눌러 주세요"
    try:
        msg = i.codex_hooks()
    finally:
        forget()
    if not msg or msg.startswith("not installed"):
        return False, "Codex 훅을 넣지 못했습니다" + (": " + msg.split("—", 1)[-1].strip() if msg else "")
    return True, "Codex 훅을 넣었습니다 · Codex CLI 에서 /hooks 를 열어 한 번 승인해 주세요"
