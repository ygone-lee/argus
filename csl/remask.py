"""비밀값 가리기 규칙(store.mask)이 바뀐 뒤, 이미 저장된 글에 새 규칙을 다시 적용한다.

    /usr/bin/python3 csl/remask.py           # 바뀔 개수만(표·열별)
    /usr/bin/python3 csl/remask.py --apply   # 적용 → 검색 색인 다시 만들기 → VACUUM

- <private> 는 저장할 때 이미 걸렀으므로 다시 하지 않는다(private=False — 응답이 <private> 를 말하기만 해도 뒤가 지워지지 않게).
- JSON 열(turns.extras·sessions.ask)은 풀어서 글자열만 가린다.
- 행마다 읽은 글과 지금 글이 같을 때만 바꾼다(그사이 훅이 고친 행은 건드리지 않는다). 200행씩 짧게 커밋해 훅을 오래 막지 않는다.
- 검색 색인(FTS)은 지운 원문 조각을 세그먼트에 남기므로 rebuild, 지운 페이지·WAL 에 남은 원문은 VACUUM 으로 없앤다.
- 가린 값은 출력하지 않는다.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csl import store  # noqa: E402

# (표, 열, JSON 여부)
TARGETS = (
    ("turns", "prompt", False), ("turns", "response", False), ("turns", "extras", True),
    ("sessions", "title", False), ("sessions", "recap", False), ("sessions", "state_detail", False), ("sessions", "ask", True),
    ("events", "text", False), ("notes", "text", False), ("asides", "question", False), ("asides", "answer", False),
    ("inbox", "summary", False), ("inbox", "todo", False), ("inbox", "reply_draft", False), ("inbox", "prompt", False),
    ("inbox", "rec_reason", False), ("inbox_log", "text", False), ("inbox_chat", "text", False),
    ("feed_items", "title", False), ("feed_items", "text", False), ("daily_reports", "content", False),
)
BATCH = 200


def _remasked(value, is_json):
    if not value:
        return value
    if is_json:
        try:
            return json.dumps(store.mask_json(json.loads(value), private=False), ensure_ascii=False)
        except ValueError:
            pass   # JSON 이 아니면 글로
    return store.mask(value, private=False)


def changes(conn):
    """{(표, 열): [(rowid, 원래 글, 새 글)]} — 바뀌는 행만."""
    out = {}
    for table, col, is_json in TARGETS:
        rows = []
        for rid, value in conn.execute("SELECT rowid, %s FROM %s WHERE %s IS NOT NULL" % (col, table, col)):
            new = _remasked(value, is_json)
            # JSON 은 다시 직렬화하면 모양(공백·이스케이프)이 달라질 수 있다 — 가린 것이 실제로 있을 때만
            if new != value and (not is_json or "[MASKED]" in new and new.count("[MASKED]") > value.count("[MASKED]")):
                rows.append((rid, value, new))
        if rows:
            out[(table, col)] = rows
    return out


def apply(conn, found):
    done = 0
    for (table, col), rows in found.items():
        for i in range(0, len(rows), BATCH):
            with conn:
                for rid, old, new in rows[i:i + BATCH]:
                    done += conn.execute("UPDATE %s SET %s=? WHERE rowid=? AND %s=?" % (table, col, col), (new, rid, old)).rowcount
    with conn:
        conn.execute("INSERT INTO turns_fts(turns_fts) VALUES ('rebuild')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    return done


def main():
    conn = store.connect()
    try:
        found = changes(conn)
        total = sum(len(v) for v in found.values())
        for (table, col), rows in found.items():
            print("%-13s %-13s %d행" % (table, col, len(rows)))
        print("합계 %d행" % total)
        if "--apply" in sys.argv[1:]:
            print("적용 %d행 · 검색 색인 다시 만듦 · VACUUM" % apply(conn, found) if total else "바꿀 것이 없습니다")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
