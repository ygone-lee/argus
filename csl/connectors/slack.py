"""Slack 커넥터: 워크스페이스마다 만든 Slack 앱(사용자 권한만)의 토큰으로 나를 부른 메시지를 모은다.

앱 권한(User Token Scopes): channels:history channels:read groups:history groups:read im:history im:read
mpim:history mpim:read search:read users:read — 설치 후 User OAuth Token(xoxp-…)을 넣는다.
앱은 매니페스트를 실은 링크(SETUP_URL)로 만든다: 워크스페이스 고르기 → Create → Install → 토큰 복사.
마지막으로 받은 메시지 시각부터 이어 받는다(잠들었다 깨도 빠짐없이, 최대 7일). 검색은 100개씩 최대 10페이지,
매번 5분 겹쳐 본다(같은 키는 한 번만). 내 글·봇(users.info 로 판별)은 뺀다.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from csl.connectors import Connector, Field, Item

API = "https://slack.com/api/"
LOOKBACK = 2 * 86400
MAX_GAP = 7 * 86400
OVERLAP = 5 * 60
SEARCH_PAGES = 10
HERE = re.compile(r"<!(here|channel|everyone)>")
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


USER_SCOPES = ["channels:history", "channels:read", "groups:history", "groups:read", "im:history", "im:read",
               "mpim:history", "mpim:read", "search:read", "users:read"]
# 읽기 전용 사용자 권한만 가진 앱. 토큰 회전을 끄면 xoxp- 토큰이 만료되지 않는다(봇 사용자는 없다)
MANIFEST = {
    "display_information": {"name": "Argus", "description": "Collects Slack messages addressed to me for the Argus board (read-only)"},
    "oauth_config": {"scopes": {"user": USER_SCOPES}},
    "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
}
SETUP_URL = "https://api.slack.com/apps?new_app=1&manifest_json=" + urllib.parse.quote(json.dumps(MANIFEST, separators=(",", ":")))


class RateLimited(Exception):
    pass


def _call(tok, method, **params):
    req = urllib.request.Request(API + method + "?" + urllib.parse.urlencode(params), headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(method)
        raise
    if not d.get("ok"):
        if d.get("error") == "ratelimited":
            raise RateLimited(method)
        raise RuntimeError("%s: %s" % (method, d.get("error")))
    return d


class Slack(Connector):
    type = "slack"
    name = "Slack"
    icon = "💬"
    description = "워크스페이스마다 하나. 나를 부른 멘션·DM·그룹 DM, 고른 채널의 @here·@channel"
    fields = [
        Field("token", "User OAuth Token", "secret", prefix="xoxp-",
              help="Slack 앱의 OAuth & Permissions 에 있는 xoxp- 토큰. 이미 키체인에 넣어 두었으면 keychain:항목이름"),
        Field("channels", "@here 볼 채널", "text", required=False,
              help="쉼표로 구분한 채널 이름(# 없이). 이 채널의 @here·@channel 도 모읍니다"),
    ]

    setup = {
        "button": "Slack 앱 만들기 ↗",
        "url": SETUP_URL,
        "steps": [
            "「Slack 앱 만들기」 → 워크스페이스 고르기 → Next → Create (필요한 권한은 미리 채워져 있습니다. 「created by a 3rd party」 경고는 링크로 채운 매니페스트라 늘 나오는 것이니 그대로 진행)",
            "만든 앱 화면의 「Install to Workspace」 → Allow (회사 워크스페이스는 관리자 승인 요청이 될 수 있습니다)",
            "「User OAuth Token」(xoxp-…) 옆 Copy → 아래 칸에 붙여 넣으면 바로 저장하고 연결을 확인합니다. 토큰은 앱 화면 왼쪽 메뉴 Features → OAuth & Permissions → 맨 위 「OAuth Tokens」에 있습니다",
        ],
        "links": [["이미 만든 앱에서 토큰 다시 보기 ↗", "https://api.slack.com/apps"]],
    }

    def _tok(self, ctx):
        tok = ctx.secret("token")
        if not tok:
            raise RuntimeError("토큰이 없습니다(키체인)")
        return tok

    def test(self, ctx):
        a = _call(self._tok(ctx), "auth.test")
        ctx.state.update(uid=a["user_id"], team=a.get("team"), team_id=a.get("team_id"), url=a.get("url"))
        return "%s · %s" % (a.get("team"), a.get("user"))

    def poll(self, ctx):
        tok = self._tok(ctx)
        st = ctx.state
        if not st.get("uid"):
            self.test(ctx)
        uid = st["uid"]
        since = float(st.get("last") or (time.time() - LOOKBACK))
        oldest = max(since - OVERLAP, time.time() - MAX_GAP)
        people = st.setdefault("people", {})
        out = []

        def person(user_id):
            if user_id and user_id not in people:
                try:
                    u = _call(tok, "users.info", user=user_id)["user"]
                    people[user_id] = [u.get("real_name") or (u.get("profile") or {}).get("display_name") or u.get("name") or user_id,
                                       bool(u.get("is_bot") or u.get("is_app_user") or user_id == "USLACKBOT")]
                except RuntimeError:
                    people[user_id] = [user_id, False]
            return people.get(user_id) or ["", False]

        def add(kind, ch_id, label, m, link):
            ts = float(m.get("ts") or 0)
            if ts > oldest:
                st["seen"] = max(float(st.get("seen") or 0), ts)   # 봇·내 글이라 빼도 "여기까지 봤다"는 앞으로
            if ts <= oldest or m.get("user") == uid or m.get("bot_id") or m.get("subtype") in ("bot_message", "channel_join"):
                return
            name, bot = person(m.get("user"))
            if bot:
                return
            out.append(Item(ch_id + ":" + m["ts"], ts, ("DM: " + name) if label == "DM" else label, name, m.get("text") or "",
                            link or "", kind, st.get("team") or ""))

        for q, kind in (("<@%s>" % uid, "mention"), ("to:me", "dm")):
            for page in range(1, SEARCH_PAGES + 1):
                d = _call(tok, "search.messages", query=q, sort="timestamp", sort_dir="desc", count=100, page=page)
                matches = (d.get("messages") or {}).get("matches") or []
                for m in matches:
                    ch = m.get("channel") or {}
                    k = "dm" if ch.get("is_im") else "group_dm" if ch.get("is_mpim") else kind
                    label = "DM" if ch.get("is_im") else ("#" + ch["name"]) if ch.get("name") and not ch.get("is_mpim") else "그룹 DM"
                    add(k, ch.get("id") or "", label, m, m.get("permalink"))
                pages = ((d.get("messages") or {}).get("paging") or {}).get("pages") or 1
                if not matches or float(matches[-1].get("ts") or 0) <= oldest or page >= pages:
                    break

        want = [c.strip().lstrip("#") for c in str(ctx.get("channels", "")).split(",") if c.strip()]
        if want:
            ids = st.setdefault("channel_ids", {})
            if any(n not in ids for n in want):
                cur = ""
                for _ in range(10):
                    d = _call(tok, "users.conversations", types="public_channel,private_channel", limit=200,
                              exclude_archived="true", cursor=cur)
                    for c in d.get("channels") or []:
                        ids[c["name"]] = c["id"]
                    cur = (d.get("response_metadata") or {}).get("next_cursor") or ""
                    if not cur:
                        break
            for n in want:
                if n not in ids:
                    continue
                cur = ""
                for _ in range(20):
                    d = _call(tok, "conversations.history", channel=ids[n], oldest="%.6f" % oldest, limit=200, cursor=cur)
                    for m in d.get("messages") or []:
                        if HERE.search(m.get("text") or ""):
                            link = (st.get("url") or "").rstrip("/") + "/archives/%s/p%s" % (ids[n], m["ts"].replace(".", ""))
                            add("here", ids[n], "#" + n, m, link)
                    cur = (d.get("response_metadata") or {}).get("next_cursor") or ""
                    if not cur or not d.get("has_more"):
                        break
        # 기준 시각은 실제로 받은 마지막 메시지 시각으로만 옮긴다(_poll_one 이 out 의 최댓값으로 last 를 정한다)
        return out

    def followups(self, ctx, channel, ts, thread_ts=None, limit=30, include_self=False):
        """그 메시지 뒤에 이어진 대화 — 스레드 답글(스레드면), DM·그룹 DM 이면 뒤에 온 메시지까지.

        [{"ts", "me": 내가 쓴 글인지, "who", "text"}] 시각순. 이 워크스페이스의 대화가 아니면 RuntimeError.
        브레인이 「내가 이미 답해서 공이 상대에게 넘어갔는지」 판단하는 데 쓴다.
        """
        tok = self._tok(ctx)
        st = ctx.state
        if not st.get("uid"):
            self.test(ctx)
        uid, people = st["uid"], st.setdefault("people", {})
        after = float(ts) - (0.000001 if include_self else 0)   # include_self: 그 메시지 자체도
        msgs = {}
        root = thread_ts or ts
        d = _call(tok, "conversations.replies", channel=channel, ts=root, limit=200)
        for m in d.get("messages") or []:
            if float(m.get("ts") or 0) > after:
                msgs[m["ts"]] = m
        info = _call(tok, "conversations.info", channel=channel).get("channel") or {}
        if not thread_ts and (info.get("is_im") or info.get("is_mpim")):
            d = _call(tok, "conversations.history", channel=channel, oldest=ts, limit=limit, inclusive="true")
            for m in d.get("messages") or []:
                if float(m.get("ts") or 0) > after:
                    msgs[m["ts"]] = m
        def person(u):
            if u and u not in people:
                try:
                    x = _call(tok, "users.info", user=u)["user"]
                    people[u] = [x.get("real_name") or (x.get("profile") or {}).get("display_name") or x.get("name") or u,
                                 bool(x.get("is_bot") or x.get("is_app_user"))]
                except RuntimeError:
                    people[u] = [u, False]
            return people.get(u) or [u or "봇", False]

        def names(text):   # <@U…> 멘션을 @이름 으로(누구에게 한 말인지 읽히게)
            return MENTION.sub(lambda g: "@" + ("나" if g.group(1) == uid else person(g.group(1))[0]), text)
        out = []
        for k in sorted(msgs, key=float)[:limit]:
            m = msgs[k]
            u = m.get("user")
            if m.get("bot_id") or m.get("subtype") in ("bot_message", "channel_join") or person(u)[1]:
                continue   # 봇(번역 봇 등)은 빼고 사람이 쓴 것만
            # 첨부 파일 — 이름·슬랙 파일 페이지(로그인한 브라우저에서 열림)·종류. 내려받기 주소(url_private)는 토큰이 있어야 해 싣지 않는다
            files = [{"name": f.get("name") or f.get("title") or "파일", "link": f.get("permalink") or "",
                      "type": f.get("pretty_type") or f.get("filetype") or ""} for f in m.get("files") or [] if isinstance(f, dict)]
            out.append({"ts": float(k), "me": u == uid, "who": "나" if u == uid else person(u)[0],
                        "text": names(m.get("text") or "")[:600], "files": files})
        return out


CONNECTOR = Slack
