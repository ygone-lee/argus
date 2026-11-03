"""Gmail 커넥터: gws CLI(Google Workspace CLI, 이미 로그인된 계정)로 받은편지함의 새 메일을 모은다.

비밀값은 없다 — 인증은 gws 가 맡는다(`gws auth login`). 제목·보낸 사람·시각만 모으고 본문은 가져오지 않는다.
검색식(Gmail 검색 문법)으로 범위를 정한다. 기본: 받은편지함의 읽지 않은 메일(프로모션·소셜 제외).
"""
import email.utils
import time

from csl.connectors import Connector, Field, Item, gws_json

OVERLAP = 10 * 60


def _run(args):
    return gws_json(args, lists=True)


class Gmail(Connector):
    type = "gmail"
    name = "Gmail"
    icon = "✉️"
    description = "gws CLI 로 로그인한 Gmail 의 새 메일(제목·보낸 사람만, 본문은 안 가져옴)"
    default_every = 5
    fields = [
        Field("query", "검색식", "text", default="in:inbox is:unread -category:promotions -category:social newer_than:3d",
              help="Gmail 검색 문법. 이 조건에 맞는 메일만 인박스로"),
        Field("max", "한 번에 최대", "int", default=50, help="한 번 가져올 때 최대 메일 수"),
    ]

    def test(self, ctx):
        p = _run(["gmail", "users", "getProfile", "--params", '{"userId":"me"}'])
        ctx.state["account"] = p.get("emailAddress")
        return p.get("emailAddress") or "Gmail"

    def poll(self, ctx):
        if not ctx.state.get("account"):
            self.test(ctx)
        q = ctx.get("query", self.fields[0].default)
        d = _run(["gmail", "+triage", "--max", str(int(ctx.get("max", 50))), "--query", q, "--format", "json"])
        since = ctx.since - OVERLAP
        out = []
        for m in d.get("messages") or []:
            try:
                ts = email.utils.parsedate_to_datetime(m.get("date")).timestamp()
            except (TypeError, ValueError):
                ts = time.time()
            if ts <= since:
                continue
            out.append(Item(m["id"], ts, m.get("subject") or "(제목 없음)", m.get("from") or "", "",
                            "https://mail.google.com/mail/u/0/#all/" + m["id"], "mail", ctx.state.get("account") or ""))
        return out


CONNECTOR = Gmail
