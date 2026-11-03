"""남은 넥스트 액션 리포트: 세션마다 완료 표시가 안 된 할 일.

브레인(`./argus notes`)과 하루 정리가 쓴다. 세션이 끝났거나 보관됐어도 할 일이 남아 있으면 넣는다(그래야 묻히지 않는다).
읽기만 한다 — 완료 표시·삭제는 사용자가 보드에서 한다.
"""
import time

from csl import store

OLD_DAYS = 3   # 이보다 오래 남은 할 일은 「오래됨」으로 표시


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def collect(conn):
    """[{session_id, title, project, branch, state, archived, last_activity, notes: [{text, created_at}]}] — 오래된 할 일이 있는 세션 먼저."""
    rows = conn.execute(
        "SELECT n.id, n.text, n.created_at, n.session_id, s.title, s.project_name, s.branch, s.state, s.archived, s.last_activity"
        " FROM notes n LEFT JOIN sessions s ON s.session_id = n.session_id"
        " WHERE n.done = 0 ORDER BY n.created_at").fetchall()
    groups = {}
    for r in rows:
        g = groups.setdefault(r["session_id"], {
            "session_id": r["session_id"], "title": r["title"], "project": r["project_name"], "branch": r["branch"],
            "state": r["state"], "archived": bool(r["archived"]), "last_activity": r["last_activity"], "notes": []})
        g["notes"].append({"id": r["id"], "text": store.mask(r["text"] or ""), "created_at": r["created_at"]})
    return sorted(groups.values(), key=lambda g: g["notes"][0]["created_at"] or 0)


def _age(ts, now):
    d = (now - (ts or now)) / 86400
    return "오늘" if d < 1 else "%d일째" % d


def text(groups):
    now = time.time()
    total = sum(len(g["notes"]) for g in groups)
    if not total:
        return "# 남은 넥스트 액션\n없음"
    old = sum(1 for g in groups for n in g["notes"] if now - (n["created_at"] or now) >= OLD_DAYS * 86400)
    out = ["# 남은 넥스트 액션 — 세션 %d개 · 할 일 %d개%s" % (len(groups), total, " · %d일 넘은 것 %d개" % (OLD_DAYS, old) if old else "")]
    for g in groups:
        if g["session_id"] is None:
            head = "세션 없음"
        else:
            where = " · ".join(x for x in (g["project"], g["branch"]) if x)
            st = "종료" if g["state"] == "ended" else "보관" if g["archived"] else ""
            head = "「%s」 ID %s%s%s" % (_clip(g["title"] or "(제목 없음)", 50), g["session_id"][:8],
                                       " — " + where if where else "", " [%s]" % st if st else "")
        out.append("\n" + head)
        for n in g["notes"]:
            mark = " ⚠" if now - (n["created_at"] or now) >= OLD_DAYS * 86400 else ""
            out.append("- #%s %s (%s%s)" % (n["id"], _clip(n["text"], 160), _age(n["created_at"], now), mark))
    return "\n".join(out)
