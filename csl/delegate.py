"""카드 맡기기: 인박스 카드를 세션에 맡기고, 끝나면 결과 요약·답장 초안까지 이어 간다.

흐름(카드의 stage):
  sent        세션에 프롬프트를 보냄
  working     세션이 작업 중
  waiting     세션이 승인·질문을 기다림(사용자가 그 세션에서 답해야 함)
  summarizing 세션 응답이 끝나 결과를 정리하는 중(claude -p, 도구 없음)
  needs       세션이 내 답을 기다리는 질문으로 끝남 — 그 세션에 답하면 다시 이어 본다
  reply       결과 요약·답장 초안을 카드에 넣음 → 카드 상태도 reply(「답장만 남음」)
  error       세션이 끝났거나 정리에 실패

단계가 바뀔 때마다 카드 기록(inbox_log)에 남는다. 지켜보기는 서버의 작은 스레드가 몇 초마다 한다(start_watcher).
요약은 세션에 다시 묻지 않고 기록된 응답만 claude -p 로 정리하므로 그 세션의 맥락·사용량을 건드리지 않는다.
"""
import json
import os
import re
import subprocess
import threading
import time

from csl import config, store
from csl.daily import _claude_bin, _clean_env

RUN_DIR = os.path.join(store.DATA_DIR, "delegate-run")
WATCH_EVERY = 8
SUMMARY_TIMEOUT = 180
ACTIVE = ("sent", "working", "waiting", "needs")

SUMMARY = """아래는 인박스 카드(슬랙·메일 요청)와, 그 요청을 맡은 Claude Code 세션이 한 일(요청·응답 기록)이다.
요청자에게 보낼 답장을 준비하려고 한다. JSON 하나로만 답하라(설명·코드 블록 없이):
{"status": "done" 또는 "needs_input", "summary": "세션이 한 일과 결과를 2~4줄로(한국어)",
 "reply_draft": "요청자에게 슬랙·메일로 그대로 보낼 답장 — 원문 언어로(일본어 요청엔 일본어)", "todo": "내가 할 일 한 줄"}
- status: 세션이 일을 끝냈으면 done. 세션이 승인·결정·정보를 나에게 묻고 멈췄으면 needs_input(그때 reply_draft 는 빈 문자열,
  todo 에 「세션 제목」에 ○○ 답하기).
- 사람 이름에 호칭(님·씨·さん·様)을 붙이지 않는다. 확정되지 않은 것(배포 일정 등)은 단정하지 말고 빈칸(○일)으로.
- 기록에 없는 것은 쓰지 않는다. 내부 경로·비밀값·운영 DB 세부는 답장에 넣지 않는다.
"""

_lock = threading.Lock()
_busy = set()   # 정리 중인 카드 id


def start(conn, card, session, prompt):
    """카드를 맡긴 것으로 기록한다(보내기는 서버가 이미 했다)."""
    t = store.now()
    conn.execute("UPDATE inbox SET delegated_session=?, delegated_at=?, stage='sent', stage_turn=NULL, status='doing',"
                 " updated_at=? WHERE id=?", (session["session_id"], t, t, card["id"]))
    store.card_log(conn, card["id"], "delegate", "「%s」 세션에 맡김: %s" % (session.get("title") or session["session_id"][:8], prompt[:300]),
                   session["session_id"])


def _stage(conn, card, stage, text=None):
    conn.execute("UPDATE inbox SET stage=?, updated_at=? WHERE id=?", (stage, store.now(), card["id"]))
    store.card_log(conn, card["id"], "stage:" + stage, text, card["delegated_session"])


def _turns_since(conn, sid, since):
    return [dict(r) for r in conn.execute(
        "SELECT id, prompt, prompt_at, response, response_at, status FROM turns WHERE session_id=?"
        " AND COALESCE(prompt_at, response_at) >= ? ORDER BY COALESCE(prompt_at, response_at), id", (sid, since - 3))]


def _turns_since_many(conn, since_of):
    """{session_id: 맡긴 시각} → {session_id: 그 시각 뒤의 턴들}. 한 쿼리로(가장 이른 시각부터) 읽고 세션별로 나눈다."""
    if not since_of:
        return {}
    sids = list(since_of)
    rows = conn.execute(
        "SELECT id, session_id, prompt, prompt_at, response, response_at, status FROM turns WHERE session_id IN (%s)"
        " AND COALESCE(prompt_at, response_at) >= ? ORDER BY COALESCE(prompt_at, response_at), id"
        % ",".join("?" * len(sids)), (*sids, min(since_of.values()) - 3)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["session_id"], []).append({k: r[k] for k in ("id", "prompt", "prompt_at", "response", "response_at", "status")})
    return out


def tick():
    """맡긴 카드들의 세션을 한 번 살핀다."""
    conn = store.connect()
    try:
        # 카드와 맡긴 세션을 한 번에, 그 세션들의 맡긴 뒤 턴도 한 번에 읽는다(카드마다 쿼리하지 않게)
        cards = [dict(r) for r in conn.execute(
            "SELECT i.*, s.session_id AS s_sid, s.title AS s_title, s.state AS s_state, s.state_detail AS s_detail"
            " FROM inbox i LEFT JOIN sessions s ON s.session_id = i.delegated_session"
            " WHERE i.stage IN (%s)" % ",".join("?" * len(ACTIVE)), ACTIVE)]
        cards = [c for c in cards if c["id"] not in _busy]
        turns_of = _turns_since_many(conn, {c["delegated_session"]: c["delegated_at"] for c in cards if c["s_sid"]})
        for card in cards:
            s = {"session_id": card.pop("s_sid"), "title": card.pop("s_title"), "state": card.pop("s_state"),
                 "state_detail": card.pop("s_detail")}
            if not s["session_id"]:
                s = None
            with conn:
                if not s:
                    _stage(conn, card, "error", "맡긴 세션을 찾지 못함")
                    continue
                if s["state"] == "ended":
                    _stage(conn, card, "error", "맡긴 세션이 종료됨")
                    continue
                turns = [x for x in turns_of.get(card["delegated_session"], [])
                         if (x["prompt_at"] or x["response_at"]) >= card["delegated_at"] - 3]
                done = [t for t in turns if t["response_at"] and t["status"] in ("done", "failed", "interrupted")]
                if s["state"] == "waiting" and card["stage"] != "waiting":
                    _stage(conn, card, "waiting", "세션이 확인을 기다림: %s" % (s["state_detail"] or ""))
                elif s["state"] == "working" and card["stage"] in ("sent", "waiting", "needs"):
                    _stage(conn, card, "working", "세션이 작업 중")
                elif s["state"] in ("done", "idle", "error") and done and done[-1]["id"] != card["stage_turn"]:
                    conn.execute("UPDATE inbox SET stage='summarizing', stage_turn=?, updated_at=? WHERE id=?",
                                 (done[-1]["id"], store.now(), card["id"]))
                    store.card_log(conn, card["id"], "stage:summarizing", "세션 응답이 끝남 — 결과 정리 중",
                                   card["delegated_session"])
                    with _lock:
                        _busy.add(card["id"])
                    threading.Thread(target=_summarize, args=(card["id"],), daemon=True).start()
    finally:
        conn.close()


def _record(turns):
    out = []
    for t in turns:
        if t["prompt"]:
            note = store.notice_line(t["prompt"])
            out.append(note if note else "[요청] " + t["prompt"][:3000])
        if t["response"]:
            out.append("[응답] " + t["response"][:6000])
    return "\n\n".join(out)[-24000:]


def _summarize(card_id):
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM inbox WHERE id=?", (card_id,)).fetchone()
        if row is None:   # 정리 차례가 오기 전에 카드가 지워졌다
            return
        card = dict(row)
        s = conn.execute("SELECT title FROM sessions WHERE session_id=?", (card["delegated_session"],)).fetchone()
        turns = _turns_since(conn, card["delegated_session"], card["delegated_at"])
        text = "## 카드\n" + "\n".join("- %s: %s" % (k, card.get(f)) for k, f in (
            ("출처", "channel"), ("보낸 사람", "author"), ("요약", "summary"), ("할 일", "todo"),
            ("지금 답장 초안", "reply_draft")) if card.get(f)) + \
            "\n\n## 맡긴 세션: %s\n%s" % (s["title"] if s else "", _record(turns))
        model = config.CARDCHAT_MODELS.get(config.get(conn, "cardchat_model"), "claude-sonnet-5-5")
        os.makedirs(RUN_DIR, exist_ok=True)
        try:
            r = subprocess.run([_claude_bin(), "-p", SUMMARY, "--model", model, "--no-session-persistence", "--tools", "",
                                "--output-format", "json"], input=text, capture_output=True, text=True,
                               timeout=SUMMARY_TIMEOUT, cwd=RUN_DIR, env=_clean_env())
            out = (json.loads(r.stdout or "{}").get("result") or "").strip()
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            out, r = "", None
            err = str(e)
        m = re.search(r"\{[\s\S]*\}", out)
        try:
            res = json.loads(m.group(0)) if m else None
        except ValueError:
            res = None
        if not conn.execute("SELECT 1 FROM inbox WHERE id=?", (card_id,)).fetchone():
            return   # 요약하는 사이(최대 몇 분) 카드가 지워졌으면 기록을 남기지 않는다
        with conn:
            if not res:
                _stage(conn, card, "error", "결과 정리 실패: %s" % ((r.stderr if r else err) or out or "빈 응답")[-300:])
                return
            summary = store.mask(str(res.get("summary") or ""))[:2000]
            if res.get("status") == "needs_input":
                conn.execute("UPDATE inbox SET todo=COALESCE(NULLIF(?, ''), todo), updated_at=? WHERE id=?",
                             (store.mask(str(res.get("todo") or ""))[:500], store.now(), card_id))
                _stage(conn, card, "needs", "세션이 내 답을 기다림 — %s" % summary)
                return
            draft = store.mask(str(res.get("reply_draft") or ""))[:4000]
            conn.execute("UPDATE inbox SET reply_draft=COALESCE(NULLIF(?, ''), reply_draft), todo=COALESCE(NULLIF(?, ''), todo),"
                         " status='reply', updated_at=? WHERE id=?",
                         (draft, store.mask(str(res.get("todo") or ""))[:500], store.now(), card_id))
            _stage(conn, card, "reply", "결과: %s" % summary)
            if draft:
                store.card_log(conn, card_id, "draft", "답장 초안을 고침:\n" + draft, card["delegated_session"])
    finally:
        conn.close()
        with _lock:
            _busy.discard(card_id)


RELAY_MAX_AGE = 3 * 3600   # 이보다 오래 답이 없으면 지켜보기를 그만둔다


def relay_tick():
    """브레인이 보낸 세션이 답했거나 확인을 기다리면, 쉬고 있는 브레인에게 알린다(브레인은 스스로 깨어나지 못한다)."""
    from csl import brain, focus
    conn = store.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT w.*, s.session_id AS s_sid, s.title AS s_title, s.state AS s_state, s.state_detail AS s_detail,"
            " EXISTS (SELECT 1 FROM turns t WHERE t.session_id = w.target_sid AND t.response_at IS NOT NULL"
            "  AND COALESCE(t.prompt_at, t.response_at) >= w.sent_at - 3) AS answered"
            " FROM brain_watch w LEFT JOIN sessions s ON s.session_id = w.target_sid WHERE w.status='open'")]
        if not rows:   # 지켜볼 게 없으면 브레인 세션을 찾지도 않는다(8초마다 도는 감시)
            return
        b = brain.current(conn)
        for w in rows:
            if not w["answered"] and w.get("prompt"):
                # 보낸 글이 다른 세션의 턴으로 기록됐으면(그 탭에 실제로 떠 있던 게 다른 세션 — 백그라운드로 옮겨진 세션의
                # 옛 기록 등) 그 세션을 지켜본다. 같은 글로 시작하는 보낸 뒤의 첫 요청
                hit = conn.execute(
                    "SELECT session_id FROM turns WHERE prompt_at >= ? AND session_id != ? AND substr(prompt, 1, 200) = substr(?, 1, 200)"
                    " ORDER BY prompt_at LIMIT 1", (w["sent_at"] - 5, w["target_sid"], w["prompt"].strip())).fetchone()
                if hit:
                    with conn:
                        conn.execute("UPDATE brain_watch SET target_sid=? WHERE id=?", (hit[0], w["id"]))
                    continue   # 다음 차례에 그 세션 기준으로
            with conn:
                if time.time() - w["sent_at"] > RELAY_MAX_AGE:
                    conn.execute("UPDATE brain_watch SET status='expired' WHERE id=?", (w["id"],))
                    continue
                s = {"session_id": w["s_sid"], "title": w["s_title"], "state": w["s_state"], "state_detail": w["s_detail"]}
                if not s["session_id"] or s["state"] == "ended":
                    conn.execute("UPDATE brain_watch SET status='gone' WHERE id=?", (w["id"],))
                    continue
                what = None
                if w["answered"] and s["state"] in ("done", "idle", "error"):
                    what = "답했습니다"
                elif s["state"] == "waiting" and w.get("note") != "waiting":
                    what = "확인을 기다립니다(%s)" % (s["state_detail"] or "")
                if not what:
                    continue
                # 브레인이 쉬고 있을 때만 넣는다(작업 중이면 다음 차례에)
                if not b or b["session_id"] != w["brain_sid"] or b["state"] not in ("idle", "done"):
                    continue
                tg = focus.target_of(dict(b))
                if not tg or not focus.claude_alive(dict(b), tg) or focus.dialog_text(tg):
                    continue
                msg = ("%s 「%s」 세션(ID %s)이 %s. `./argus session %s` 로 읽고, 내가 물었던 것에 맞춰 짧게 전해 줘. "
                       "이 차례에는 다른 세션에 보내지 마." % (brain.RELAY_PREFIX, s["title"] or "", s["session_id"][:8], what,
                                                       s["session_id"][:8]))
                ok, _, _ = focus.send_prompt(tg, msg)
                if ok:
                    if what.startswith("확인"):
                        conn.execute("UPDATE brain_watch SET note='waiting', notified_at=? WHERE id=?", (time.time(), w["id"]))
                    else:
                        conn.execute("UPDATE brain_watch SET status='done', notified_at=? WHERE id=?", (time.time(), w["id"]))
                    return   # 한 번에 하나 — 브레인이 그 알림을 처리하는 동안은 작업 중이라 다음 것은 그 뒤에
    finally:
        conn.close()


def start_watcher():
    def loop():
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001 - 지켜보기가 서버를 죽이면 안 된다
                pass
            try:
                relay_tick()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(WATCH_EVERY)
    threading.Thread(target=loop, daemon=True).start()
