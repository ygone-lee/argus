"""Google Calendar 커넥터(정보 전용): 인박스·할 일은 만들지 않고, 브레인이 필요할 때 일정을 조회한다.

브레인은 `./argus calendar [오늘|내일|이번주|N일]` 로 일정을 본다 — 현황을 정리할 때, 일정 요청에 답장 초안을 쓸 때
비는 시간을 확인할 때 등. gws CLI(이미 로그인된 계정)를 쓰므로 비밀값은 없다.
일정 제목·시각·주최자·장소·내 응답(수락·미회신 등)만 가져오고 설명 본문은 가져오지 않는다.
"""
import datetime
import json
import time

from csl.connectors import Connector, Field, gws_json


def _run(args):
    return gws_json(args)


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


RESPONSE = {"accepted": "수락", "needsAction": "미회신", "declined": "거절", "tentative": "미정"}


class Calendar(Connector):
    type = "calendar"
    name = "Google Calendar"
    icon = "📅"
    role = "info"   # 인박스로 모으지 않고 조회만
    description = "정보 전용 — 브레인이 일정을 조회해 현황 정리·답장 초안(비는 시간)에 씁니다. 인박스·할 일은 만들지 않음"
    default_every = 0
    fields = [
        Field("calendar", "캘린더", "text", default="primary", help="캘린더 ID. 기본은 내 기본 캘린더(primary)"),
    ]

    def test(self, ctx):
        c = _run(["calendar", "calendarList", "get", "--params", json.dumps({"calendarId": ctx.get("calendar", "primary")})])
        ctx.state["account"] = c.get("summary") or c.get("id")
        return ctx.state["account"] or "Calendar"

    def poll(self, ctx):
        return []   # 정보 전용 — 모으지 않는다

    def query(self, ctx, start, end):
        """start~end(유닉스 시각) 사이의 일정 목록. 반복 일정은 회차별로, 시작 시각 순."""
        d = _run(["calendar", "events", "list", "--params", json.dumps({
            "calendarId": ctx.get("calendar", "primary"), "timeMin": _iso(start), "timeMax": _iso(end),
            "singleEvents": True, "orderBy": "startTime", "maxResults": 250})])
        out = []
        for e in d.get("items") or []:
            s, en = e.get("start") or {}, e.get("end") or {}
            me = next((a for a in e.get("attendees") or [] if a.get("self")), None)
            org = e.get("organizer") or {}
            if s.get("dateTime"):
                a = datetime.datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone()
                b = datetime.datetime.fromisoformat(en["dateTime"].replace("Z", "+00:00")).astimezone() if en.get("dateTime") else a
                when, ts = a.strftime("%m/%d(%a) %H:%M") + "–" + b.strftime("%H:%M"), a.timestamp()
            else:
                when, ts = (s.get("date") or "") + " 종일", time.mktime(time.strptime(s.get("date") or "1970-01-01", "%Y-%m-%d"))
            out.append({"ts": ts, "when": when, "title": e.get("summary") or "(제목 없음)",
                        "organizer": "나" if org.get("self") else (org.get("displayName") or org.get("email") or ""),
                        "response": RESPONSE.get((me or {}).get("responseStatus"), "") if me else "",
                        "where": e.get("location") or ("Meet" if e.get("hangoutLink") else ""),
                        "attendees": len(e.get("attendees") or []), "link": e.get("htmlLink") or ""})
        return out


CONNECTOR = Calendar
