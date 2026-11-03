"""넥스트 액션 예약 보내기: 할 일에 「끝나면 보내기」를 걸어 두면, 그 세션의 응답이 끝났을 때 하나씩 보낸다.

- 예약은 notes.queued_at(건 순서). 세션마다 가장 먼저 건 것 하나만, 세션이 쉬고 있을 때(done·idle) 보낸다.
- 보낸 뒤에는 그 세션의 다음 응답이 끝날 때까지(상태 시각이 보낸 뒤로 바뀔 때까지) 다음 것을 보내지 않는다 —
  보낸 직후엔 훅이 「작업 중」으로 바꾸기 전이라 아직 done 으로 보이기 때문.
- 터미널이 없거나 Claude 가 꺼져 있거나 선택 창이 떠 있으면 기다린다. 보내기 자체가 실패하면 예약을 풀고 활동에 오류로 남긴다.
- 보낸 할 일은 완료로 표시한다(세션 대화에 그 요청이 턴으로 남는다).
- 보내기 직전에 예약을 원자적으로 가져오고(그사이 풀거나 지운 것은 건너뜀), 확인·보내기는 터미널 잠금(focus.hold) 안에서.
"""
import json
import threading
import time

from csl import focus, store

EVERY = 3            # 초
_sent_at = {}        # session_id → 마지막으로 예약을 보낸 시각


def tick():
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT n.id, n.session_id, s.state, s.state_at, s.title, s.cwd, s.project_root, s.terminal"
            " FROM notes n JOIN sessions s ON s.session_id = n.session_id"
            " WHERE n.queued_at IS NOT NULL AND n.done = 0 AND s.state IN ('done', 'idle')"
            " ORDER BY n.queued_at, n.id").fetchall()
        seen = set()
        for r in rows:
            sid = r["session_id"]
            if sid in seen:
                continue
            seen.add(sid)
            if (r["state_at"] or 0) <= _sent_at.get(sid, 0):
                continue   # 방금 보낸 것의 응답이 아직 안 끝났다
            sess = {k: r[k] for k in ("session_id", "title", "cwd", "project_root", "terminal")}
            tg, _ = focus.resolve_target(sess)
            if not tg:
                continue
            with focus.hold(tg):   # 확인부터 보내기까지 다른 조작(보드 보내기·되돌리기 …)이 끼지 않게
                if not focus.claude_alive(sess, tg) or focus.is_busy(tg) or focus.dialog_text(tg):
                    continue   # Claude 가 꺼졌거나 아직 바쁘거나 창이 떠 있으면 다음 차례에
                # 보내기 직전에 예약을 가져온다 — 목록을 읽은 뒤 예약을 풀었거나 지웠거나 완료(직접 보냄)한 할 일은 보내지 않고,
                # 그사이 고친 글은 고친 대로 보낸다
                with conn:
                    claimed = conn.execute("UPDATE notes SET queued_at=NULL, updated_at=? WHERE id=? AND queued_at IS NOT NULL"
                                           " AND done=0", (store.now(), r["id"])).rowcount
                if claimed != 1:
                    continue
                note = conn.execute("SELECT text, images FROM notes WHERE id=?", (r["id"],)).fetchone()
                try:
                    images = json.loads(note["images"]) if note["images"] else []
                except ValueError:
                    images = []
                text = note["text"] or ""
                # 보내기 「직전」 시각을 기준으로 — 빠른 모델은 보내기 확인이 끝나기 전에 답을 마쳐, 끝난 뒤 시각을 적으면
                # 그 응답 완료(state_at)가 기준보다 앞서 다음 예약이 영영 나가지 않았다
                before = store.now()
                if "\n" in text or images:
                    ok, err = focus.type_text(tg, text, images)
                else:
                    ok, _, err = focus.send_prompt(tg, text)
            with conn:
                if ok:
                    # /rename·/model 같은 명령은 턴(훅)이 없어 상태 시각이 안 바뀐다 — 기다리면 다음 예약이 영영 막힌다
                    if not text.lstrip().startswith("/"):
                        _sent_at[sid] = before
                    conn.execute("UPDATE notes SET done=1, done_at=?, updated_at=? WHERE id=?",
                                 (store.now(), store.now(), r["id"]))
                else:
                    store.add_event(conn, sid, "error", "예약한 할 일을 보내지 못했습니다: %s" % (err or "")[:200])
    finally:
        conn.close()


def start():
    def loop():
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001 - 예약 보내기가 서버를 죽이면 안 된다
                pass
            time.sleep(EVERY)
    threading.Thread(target=loop, daemon=True).start()
