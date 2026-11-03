"""Argus 브레인(brain): 브레인 폴더(기본 저장소의 brain/)에서 띄우는 Claude Code 세션.

여러 세션의 현황을 읽어 답하고, 슬랙에서 나를 부른 것들을 읽어 인박스 카드(요약·배정 추천·보낼 프롬프트)를 남긴다.
지침은 brain/CLAUDE.md, 권한은 brain/.claude/settings.json. 보드는 이 세션을 칸 대신 왼쪽 브레인 패널에 보여 준다.
폴더·모델·자동 슬랙 확인(간격·시간대·평일만)은 설정 창(csl/config.py). 자동 확인은 브레인이 쉬고 있을 때만 보낸다.
"""
import os
import time
from datetime import datetime

from csl import config, focus, store

CHECK_PROMPT = "인박스 정리해줘 (자동)"
RELAY_PREFIX = "[자동 알림]"   # 브레인이 보낸 세션이 답했을 때 브레인에게 넣는 글의 머리 — 이 차례엔 다른 세션에 보내지 못한다
AUTO_PROMPTS = (CHECK_PROMPT, RELAY_PREFIX)
TITLE = "Argus Brain"   # 브레인 세션 이름(Claude Code·Orca 탭·/resume 목록). 대화마다 바뀌는 자동 제목 대신
_dir_cache = [0.0, None]


def brain_dir():
    """설정 창의 브레인 폴더(목록을 그릴 때 세션마다 부르므로 2초 기억)."""
    if time.time() - _dir_cache[0] > 2:
        _dir_cache[:] = [time.time(), os.path.realpath(config.get_free("brain_dir"))]
    return _dir_cache[1]


def is_brain(cwd):
    return bool(cwd) and os.path.realpath(cwd) == brain_dir()


def current(conn):
    """살아 있는 브레인 세션(가장 최근 것) 행 또는 None."""
    for r in conn.execute("SELECT session_id, cwd, state, terminal, title FROM sessions WHERE state != 'ended' AND archived=0"
                          " ORDER BY last_activity DESC"):
        if is_brain(r["cwd"]):
            return r
    return None


def start(model=None, mode=None):
    """브레인 세션을 새 터미널(기본 터미널)에 띄운다. (Target, 오류). mode: claude --permission-mode(기본은 설정값)"""
    mode = mode if mode in config.BRAIN_PERMS else config.get_free("brain_permission_mode")
    return focus.new_claude_terminal(brain_dir(), model or None, extra="--permission-mode " + mode)


def ensure_name():
    """브레인 세션 이름을 Argus Brain 으로 — 쉬고 있을 때 /rename 을 한 번 보낸다(/clear 로 새 세션이 되면 다시)."""
    conn = store.connect()
    try:
        b = current(conn)
        if not b or b["title"] == TITLE or b["state"] not in ("idle", "done"):
            return
        if store.get_setting(conn, "brain_renamed", None) == b["session_id"]:
            return   # 이 세션엔 이미 보냈다(실패했어도 되풀이하지 않는다)
        tg = focus.target_of(dict(b))
        with conn:
            store.set_setting(conn, "brain_renamed", b["session_id"])
        if tg and focus.claude_alive(dict(b), tg):
            ok, _, _ = focus.send_prompt(tg, "/rename " + TITLE)
            if ok:
                with conn:
                    conn.execute("UPDATE sessions SET title=?, title_source='custom' WHERE session_id=?", (TITLE, b["session_id"]))
    finally:
        conn.close()


def auto_tick():
    """자동 인박스 정리 — 서버 스케줄러가 1분마다 부른다. 브레인 대화가 아니라 따로 도는 정리 일꾼(csl/inboxrun.py)으로.

    대화창에 「인박스 정리해줘 (자동)」를 넣으면 사용자 대화 중간에 끼어들고 브레인의 기억에도 쌓였다."""
    from csl import inboxrun
    conn = store.connect()
    try:
        every = int(config.get(conn, "brain_slack_every") or 0)
        if every <= 0:
            return
        now = datetime.now()
        if config.get(conn, "brain_weekdays_only") and now.weekday() >= 5:
            return
        if not (config.get(conn, "brain_hour_from") <= now.hour < config.get(conn, "brain_hour_to")):
            return
        last = float(store.get_setting(conn, "brain_slack_auto_at", 0) or 0)
        if time.time() - last < every * 60:
            return
        # 커넥터가 모아 둔 새 항목이 있을 때만(없으면 정리할 게 없으니 사용량을 쓰지 않는다). 건너뛴 것도 남겨
        # 보드가 「자동 확인 HH:MM · 새 항목 없어 건너뜀」으로 보이게 — 안 남기면 자동 정리가 멈춘 것처럼 보였다(5분에 한 번만 적는다)
        if not conn.execute("SELECT 1 FROM feed_items WHERE done=0 LIMIT 1").fetchone():
            if time.time() - float(store.get_setting(conn, "inbox_auto_idle_at", 0) or 0) > 300:
                with conn:
                    store.set_setting(conn, "inbox_auto_idle_at", store.now())
            return
    finally:
        conn.close()
    inboxrun.start("auto")
