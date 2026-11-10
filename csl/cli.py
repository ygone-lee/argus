"""Argus 브레인이 쓰는 보드 명령(brain/argus 가 부른다). 보드 서버(127.0.0.1)에 묻고 사람이 읽기 쉬운 글로 찍는다.

    ./argus sessions [--codex|--claude] [--brief] [--all]   지금 세션 목록(보드 순서) — 에이전트(Claude Code·Codex)·모델·
                                    권한·컨텍스트·터미널·변경 파일까지. 세션은 ID 앞자리(8자)로 가리킨다(--brief 는 두 줄씩)
    ./argus usage                  사용량 한도(Claude Code 5시간·주간, Codex 주간 등 — 권장 속도·초기화까지). sessions 맨 위에도 나온다
    ./argus usage refresh          Fable 등 모델별 주간 한도를 지금 새로 받고 보기(평소엔 30분마다 저절로)
    ./argus session <ID앞자리|제목 일부> [턴수]   한 세션의 최근 대화·넥스트 액션
    ./argus search <단어…> [--project 이름|--all] [--me|--claude] [--recent] [--limit N]
                                    모든 세션의 요청·응답 전문 검색(관련도 순). 결과마다 턴 번호 #tN
                                    (argus-recall 스킬로 부르면 기본은 지금 세션의 프로젝트만. 없으면 전체로 넓힌다)
    ./argus timeline <#tN> [앞뒤 턴 수]   그 턴 앞뒤의 흐름(같은 세션)
    ./argus turn <#tN> [--full]     그 턴 전문(요청·중간 메시지·응답)
    ./argus inbox                   인박스(아직 처리 안 한 카드, 카드 전체 내용)
    ./argus inbox meta              지난 확인 시각·이미 카드로 만든 슬랙 메시지 키
    ./argus inbox add <파일>        카드 추가(JSON 하나 또는 목록이 든 파일 — 넣은 뒤 지운다. 파일이 없으면 표준 입력)
    ./argus inbox checked           확인을 마친 시각 기록
    ./argus inbox raw               커넥터(슬랙·Gmail …)가 모은 항목(아직 카드로 안 만든 것)
    ./argus inbox raw-done <키…|--all>  그 메시지들을 처리했다고 표시(원문은 지워진다)
    ./argus inbox thread <카드 key|수집 key|슬랙 링크>  그 슬랙 메시지 뒤에 이어진 대화(내 답장 포함)
    ./argus inbox close <카드 key…>   카드를 처리한 것으로(이미 답했거나 공이 상대에게 넘어감)
    ./argus inbox delegate <카드 key> [세션]   카드를 세션에 맡김(기본: 카드의 추천 세션) — 끝나면 결과·답장 초안이 카드에
    ./argus inbox log <카드 key>     카드 기록(만들어짐·고쳐짐·맡김·세션 진행·요약)
    ./argus calendar [오늘|내일|이번주|다음주|N]   캘린더 일정(정보 전용 커넥터). N 은 오늘부터 N일
    ./argus notes                   세션마다 남은(완료 안 된) 넥스트 액션 — 끝난·보관된 세션 것도, 오래된 것 ⚠
    ./argus git [--refresh]          놓친 git 작업: 커밋 안 한 변경·푸시 안 한 커밋·PR 없는 브랜치·머지 안 된 PR(5분 캐시)
    ./argus events [N시간|오늘|어제] [--all] [--unseen]   세션 활동(새 응답·확인 필요·질문·오류). 기본 최근 24시간
    ./argus send <ID앞자리|제목 일부> <메시지…>   그 세션 입력창에 보내고 Enter(작업 중이면 끼워 넣기)
    ./argus send <세션> --file inbox/<파일>   여러 줄 메시지는 파일로(보낸 뒤 지운다)
    ./argus remote <세션>            그 세션의 리모트 컨트롤(/remote-control) 켜기
    ./argus quit <세션> [--force]    세션 종료 — Claude Code 를 /exit 로 닫고 터미널 탭도 닫는다(작업 중이면 --force 로 멈추고 종료)
    ./argus restart <세션>           세션 다시 시작 — 끄고 보드 터미널에서 같은 대화로 다시 연다(업데이트 반영 · 새 모델이 /model 에 보이게)
    ./argus import <세션>            보드 터미널로 가져오기 — 다른 터미널(Orca·tmux)에서 도는 세션을 옮기거나 꺼진 세션을 이어 열기
    ./argus archive <세션>           보드에서 숨기기(세션은 그대로) · ./argus unarchive <세션> 다시 보이기
    ./argus new <폴더> [--model opus|sonnet|haiku|fable] [--effort low|medium|high|xhigh|max] [--no-worktree] [첫 메시지…]
                                    새 세션(기본: 새 워크트리·모델·effort 는 설정의 새 세션 기본값). 첫 메시지를 주면 세션이 뜬 뒤 보낸다
    ./argus note add <세션> <할 일…>  넥스트 액션 추가(번호는 ./argus notes 의 #N)
    ./argus note done|undo|rm|queue|unqueue <번호> · note edit <번호> <글>   완료·취소·삭제·「끝나면 보내기」 예약·풀기·고치기
    ./argus inbox move <카드 key> <new|doing|reply|done>   카드 상태 옮기기
    ./argus inbox files <카드 key>   카드 원문(과 스레드)의 첨부 파일·슬랙 주소
    ./argus inbox runs [N] · inbox run   정리 일꾼(자동·「지금 정리」)의 최근 회차 보고 · 지금 한 번 돌리기
    ./argus pin|unpin <세션>         보드 맨 앞에 고정·풀기
    ./argus rename <세션> '<제목>'   이름 바꾸기(터미널의 /rename 도)
    ./argus stop <세션>              작업 중인 Claude 멈추기(Esc)
    ./argus model <세션> [sonnet|opus|haiku|fable] [--effort …]   모델·effort 바꾸기(이 세션만)
    ./argus perm <세션> <auto|default|acceptEdits|plan>   권한 모드 (Codex: auto|default|readonly|bypassPermissions)
    ./argus rewind <세션> <#tN> [conversation|both|code|summarize_from|summarize_upto]   그 요청 앞으로 되돌리기(/rewind)
    ./argus ask <세션> · answer <세션> <질문마다 번호|1,3|기타:글>   묻고 있는 질문·권한 확인 창 보기·답하기(창은 번호 하나)
    ./argus answer <세션> chat       질문에 답하지 않고 대화로 넘기기(터미널의 Chat about this)
    ./argus focus <세션>             그 세션의 터미널 탭을 앞으로
    ./argus switch|attach|close-branch <세션>   갈래 전환·백그라운드 세션 탭으로 열기·갈래 닫기
    ./argus diff <세션> [줄 수]      그 세션 폴더의 커밋 안 한 변경
    ./argus tasks <세션> [번호]      그 세션이 띄운 백그라운드 작업(서브에이전트·명령)과 무엇을 하는지
    ./argus agents [stop|attach|rm <ID>]   백그라운드 세션(claude agents) 목록·멈추기·열기·지우기
    ./argus daily [오늘|어제|YYYY-MM-DD] [--run]   하루 정리(작업 일지) 보기·만들기
    ./argus settings [<키> <값>]     설정 보기·바꾸기(보드 설정 창과 같은 값)
    ./argus open <파일> [--reveal]   파일을 기본 앱으로 열기(이미지·문서 등만) · Finder 에서 보기
    ./argus seen <세션|--all>        새 응답·활동을 본 것으로

보드에서 할 수 있는 일은 모두 여기 있다. 세션을 움직이거나 바꾸는 명령(send·remote·quit·archive·new·pin·rename·stop·model·
perm·rewind·answer·switch·attach·close-branch·agents …·settings·open·note queue)은 사용자가 브레인 대화에서 직접 시켰을 때만
쓴다. 자동 정리·자동 알림 차례에는 서버가 막는다.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = int(os.environ.get("CSL_PORT", "7799"))
BASE = "http://127.0.0.1:%d" % PORT
STATE = {"working": "작업 중", "waiting": "확인 필요", "error": "오류", "done": "완료", "idle": "대기", "ended": "종료"}


def my_sid():
    """이 명령을 실행한 세션의 ID — Claude Code 는 CLAUDE_CODE_SESSION_ID, Codex 는 CODEX_THREAD_ID.
    Codex 스킬로 불렸으면(ARGUS_AGENT=codex) Codex 것을 먼저 — 보드가 띄운 터미널은 Claude 세션의 환경 변수를 물려받을 수 있다."""
    claude, codex = os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CODEX_THREAD_ID")
    return (codex or claude) if os.environ.get("ARGUS_AGENT") == "codex" else (claude or codex)


def api(method, path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Host": "127.0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit("오류 %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
    except OSError as e:
        sys.exit("보드 서버에 연결하지 못했습니다(%s). Argus 서버가 켜져 있는지 확인하세요." % e)


NOTICE = re.compile(r"^\s*<task-notification>")


def asked(prompt):
    """요청 글. 백그라운드 작업이 끝났을 때 Claude Code 가 넣는 알림(<task-notification>)이면 사용자가 보낸 게 아니라고 바꿔 적는다."""
    if not prompt or not NOTICE.match(prompt):
        return prompt
    def tag(name):
        m = re.search(r"<%s>([\s\S]*?)</%s>" % (name, name), prompt)
        return m.group(1).strip() if m else ""
    status = {"completed": "끝남", "failed": "실패", "killed": "중단", "stopped": "중단"}.get(tag("status").lower(), tag("status") or "끝남")
    return "[백그라운드 작업 %s — 사용자 요청 아님] %s" % (status, re.sub(r"^Background command ", "", tag("summary")))


def clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def ago(ts):
    if not ts:
        return ""
    d = time.time() - ts
    if d < 60:
        return "방금"
    if d < 3600:
        return "%d분 전" % (d // 60)
    if d < 86400:
        return "%d시간 전" % (d // 3600)
    return "%d일 전" % (d // 86400)


def board_sessions(state, include_ended=False):
    """보드 칸 순서(고정 먼저, 그다음 최근 활동). 브레인 세션은 뺀다."""
    out = [s for s in state["sessions"] if not s.get("brain")
           and (include_ended or (s["state"] != "ended" and not s["archived"]))]
    pinned = sorted([s for s in out if s.get("pinned")], key=lambda s: s["pinned"])
    rest = sorted([s for s in out if not s.get("pinned")], key=lambda s: -(s["last_activity"] or 0))
    return pinned + rest


def where(s):
    parts = [s.get("project_name") or os.path.basename(s.get("project_root") or "")]
    if s.get("worktree"):
        parts.append("wt:" + os.path.basename(s["worktree"]))
    if s.get("branch"):
        parts.append(s["branch"])
    return " · ".join(p for p in parts if p)


AGENT = {"claude": "Claude Code", "codex": "Codex"}


def until(ts):
    d = (ts or 0) - time.time()
    if d <= 0:
        return ""
    if d < 3600:
        return "%d분 뒤" % max(1, d // 60)
    if d < 86400:
        return "%d시간 뒤" % (d // 3600)
    return "%d일 %d시간 뒤" % (d // 86400, d % 86400 // 3600)


def usage_part(label, pct, reset, window):
    """「주간 62% (권장 70% · 2일 3시간 뒤 초기화)」 — 권장은 지난 시간만큼 고르게 썼을 때의 양(보드 막대의 눈금)."""
    if pct is None:
        return ""
    bits = []
    left = (reset or 0) - time.time()
    if reset and window and 0 < left <= window:
        pace = round((1 - left / window) * 100)
        bits.append("권장 %d%%%s" % (pace, " ⚠ 빠름" if pct > pace + 5 else ""))
    if until(reset):
        bits.append(until(reset) + " 초기화")
    return "%s %d%%%s" % (label, round(pct), " (" + " · ".join(bits) + ")" if bits else "")


def usage_lines(state):
    out = []
    u = state.get("usage") or {}
    claude = [x for x in (usage_part("5시간", u.get("five_hour"), u.get("five_hour_reset"), 5 * 3600),
                          usage_part("주간", u.get("seven_day"), u.get("seven_day_reset"), 7 * 86400)) if x]
    for x in u.get("scoped") or []:   # 모델별 주간(Fable) — Claude Code 가 /usage 때 받아 둔 값
        if x.get("resets_at") and x["resets_at"] < time.time():
            claude.append("%s 주간 모름(받아 둔 값이 초기화 시각을 지남 — /usage 를 열면 새로 받음)" % x["label"])
        else:
            p = usage_part(x["label"] + " 주간", x.get("pct"), x.get("resets_at"), 7 * 86400)
            if p:
                claude.append(p + (" — %s 받은 값" % ago(x["at"]) if x.get("at") and time.time() - x["at"] > 3600 else ""))
    if claude:
        out.append("Claude Code 한도: " + " · ".join(claude))
    cx = state.get("codex_usage") or {}
    codex = [usage_part(x["label"], x.get("pct"), x.get("resets_at"), (x.get("window_minutes") or 0) * 60 or None)
             for x in cx.get("limits") or []]
    if codex:
        out.append("Codex 한도: " + " · ".join(x for x in codex if x) +
                   (" · 한도 도달(%s)" % cx["reached"] if cx.get("reached") else "") +
                   (" — 마지막 Codex 기록 %s 기준" % ago(cx["at"]) if cx.get("at") else ""))
    elif (state.get("codex") or {}).get("available"):
        out.append("Codex 한도: 아직 기록 없음(Codex 세션을 한 번 쓰면 보입니다)")
    return out


def cmd_usage(args):
    if args and args[0] in ("refresh", "새로"):   # Fable 등 모델별 주간 한도를 지금 새로 받기(claude -p "/usage" — 토큰 안 씀)
        api("POST", "/api/usage/refresh", {"from": ME()}, timeout=90)
        print("새로 받았습니다")
    lines = usage_lines(api("GET", "/api/state"))
    print("\n".join(lines) if lines else "사용량 정보가 아직 없습니다")




def model_name(s):
    m = s.get("model") or ""
    return m[len("claude-"):] if m.startswith("claude-") else m


def facts(s):
    """세션 한 줄 정보: 에이전트·모델·effort·권한·턴·컨텍스트·터미널·리모트."""
    out = [AGENT.get(s.get("agent"), s.get("agent") or "?")]
    if model_name(s):
        out.append(model_name(s) + (" (" + s["effort"] + ")" if s.get("effort") else ""))
    if s.get("cx_perm") or s.get("permission_mode"):
        out.append("권한 " + (s.get("cx_perm") or s["permission_mode"]))
    if s.get("turn_count"):
        out.append("턴 %d" % s["turn_count"])
    ctx = s.get("ctx") or {}
    if ctx.get("pct") is not None:
        out.append("컨텍스트 %s%%" % (("%.0f" % ctx["pct"]) if isinstance(ctx["pct"], (int, float)) else ctx["pct"]))
    if s.get("running"):
        out.append("터미널 " + (s.get("term_label") or "열림"))
    elif s["state"] != "ended":
        out.append("열린 터미널 없음")
    if s.get("bg"):
        out.append("백그라운드 세션")
    if s.get("remote"):
        out.append("리모트 켜짐")
    if s.get("pinned"):
        out.append("고정")
    return " · ".join(out)


def cmd_sessions(args):
    """./argus sessions [--all] [--codex|--claude] [--brief]"""
    state = api("GET", "/api/state")
    rows = board_sessions(state, "--all" in args)
    want = "codex" if "--codex" in args else "claude" if "--claude" in args else None
    if want:
        rows = [s for s in rows if s.get("agent") == want]
    brief = "--brief" in args
    num = {x["session_id"]: i for i, x in enumerate(board_sessions(state), 1)}   # #N 은 다른 명령에서도 같은 세션(거르기 전 순서)
    notes = {}
    for n in state.get("notes") or []:
        if not n["done"] and n.get("session_id"):
            notes.setdefault(n["session_id"], []).append(n["text"])
    by_agent, by_state = {}, {}
    for s in rows:
        by_agent[s.get("agent")] = by_agent.get(s.get("agent"), 0) + 1
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1
    print("세션 %d개 (보드 순서%s) — %s | %s" % (
        len(rows), ", " + AGENT[want] + "만" if want else "",
        " · ".join("%s %d" % (AGENT.get(a, a), n) for a, n in sorted(by_agent.items(), key=lambda x: str(x[0]))) or "없음",
        " · ".join("%s %d" % (STATE.get(k, k), by_state[k]) for k in STATE if k in by_state) or "없음"))
    for line in usage_lines(state):
        print("   " + line)
    for s in rows:
        title = s.get("title") or clip(s.get("first_prompt"), 50) or "(요청 없음)"
        st = STATE.get(s["state"], s["state"])
        if s.get("compact_at"):
            st = "컴팩트 중"
        if s.get("unread"):
            st += "·새 응답"
        extra = " · 할 일 %d" % len(notes.get(s["session_id"], [])) if notes.get(s["session_id"]) else ""
        print("\n%s「%s」 [%s] ID %s — %s · %s%s" % ("#%d " % num[s["session_id"]] if s["session_id"] in num else "", title, st, s["session_id"][:8], where(s), ago(s.get("last_activity")), extra))
        print("   " + facts(s))
        if brief:
            continue
        folder = s.get("worktree") or s.get("project_root") or s.get("cwd")
        if folder:
            d = s.get("dirty") or {}
            print("   폴더 " + folder + (" · 커밋 안 한 변경 %d파일(+%d −%d)" % (d["files"], d.get("add") or 0, d.get("del") or 0)
                                          if d.get("files") else ""))
        if s.get("started_at"):
            print("   시작 %s · 마지막 활동 %s" % (ago(s["started_at"]), ago(s.get("last_activity"))))
        if s.get("goal"):
            print("   목표: " + clip(s["goal"] if isinstance(s["goal"], str) else json.dumps(s["goal"], ensure_ascii=False), 160))
        if s["state"] == "waiting" and s.get("state_detail"):
            print("   대기 사유: " + clip(s["state_detail"], 160))
        if s.get("ask"):
            print("   묻는 중: 질문·확인 창이 떠 있음 (./argus ask %s)" % s["session_id"][:8])
        tasks = [t for t in s.get("bg_tasks") or [] if t.get("status") == "running"]
        if tasks:
            print("   백그라운드 작업 %d개 실행 중: %s" % (len(tasks), " / ".join(clip(t.get("desc"), 60) for t in tasks[:3])))
        lp = (s.get("last_prompt") or {}).get("prompt")
        lr = (s.get("last_response") or {}).get("response")
        if lp:
            print("   마지막 요청: " + clip(asked(lp), 160))
        if lr:
            print("   마지막 응답: " + clip(lr, 220))
        if s.get("recap"):
            print("   recap: " + clip(s["recap"], 220))
        for t in notes.get(s["session_id"], [])[:5]:
            print("   할 일: " + clip(t, 120))


def resolve(state, ref):
    rows = board_sessions(state, include_ended=True)
    live = board_sessions(state)
    if ref.startswith("#") and ref[1:].isdigit():
        i = int(ref[1:])
        return live[i - 1] if 0 < i <= len(live) else None
    hit = [s for s in rows if s["session_id"].startswith(ref)]
    if hit:
        return hit[0]
    low = ref.lower()
    hit = [s for s in rows if low in (s.get("title") or "").lower() or low in (s.get("project_name") or "").lower()]
    return hit[0] if hit else None


def cmd_session(args):
    if not args:
        sys.exit("사용법: ./argus session <ID앞자리|제목 일부> [턴수]")
    n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 6
    state = api("GET", "/api/state")
    s = resolve(state, args[0])
    if not s:
        sys.exit("세션을 찾지 못했습니다: " + args[0])
    d = api("GET", "/api/sessions/%s?limit=%d" % (s["session_id"], max(1, min(n, 30))))
    print("%s — %s · %s · id %s" % (s.get("title") or "(제목 없음)", STATE.get(s["state"], s["state"]), where(s),
                                   s["session_id"]))
    print(facts(s))
    if s.get("recap"):
        print("recap: " + clip(s["recap"], 400))
    for t in d["turns"]:
        if t.get("prompt"):
            print("\n▶ 요청 (%s): %s" % (ago(t.get("prompt_at")), clip(asked(t["prompt"]), 800)))
        if t.get("response"):
            print("◀ 응답 (%s): %s" % (ago(t.get("response_at")), clip(t["response"], 1200)))
        elif t.get("status") == "running":
            print("◀ (진행 중)")
    open_notes = [x for x in d["notes"] if not x["done"]]
    if open_notes:
        print("\n넥스트 액션:")
        for x in open_notes:
            print(" - " + clip(x["text"], 200))


def cmd_search(args):
    opts = {"sort": "rank", "limit": "20"}
    words = []
    me = my_sid()
    scoped = os.environ.get("ARGUS_SCOPE") == "project" and me   # 스킬 래퍼가 켠다(브레인은 늘 전체)
    it = iter(args)
    for a in it:
        if a == "--all":
            scoped = False
        elif a == "--project":
            scoped = False
            opts["project"] = next(it, "")
        elif a == "--limit":
            opts["limit"] = next(it, "20")
        elif a in ("--me", "--claude"):
            opts["who"] = a[2:]
        elif a == "--recent":
            opts["sort"] = "recent"
        else:
            words.append(a)
    q = " ".join(words).strip()
    if not q:
        sys.exit("사용법: ./argus search <단어…> [--project 이름|--all] [--me|--claude] [--recent] [--limit N]")
    opts["q"] = q
    if me:   # 지금 이 세션의 대화(방금 한 질문)는 빼고 찾는다
        opts["exclude"] = me

    def find(extra, any_word=False):
        o = dict(opts, **extra)
        if any_word:
            o["any"] = "1"
        return api("GET", "/api/search?" + urllib.parse.urlencode(o))["results"]
    # 모든 단어가 든 턴을 먼저(이 프로젝트 → 전체), 없으면 하나라도 든 턴을 관련도 순으로(이 프로젝트 → 전체)
    tries = [({"same_project": me}, False, "지금 세션의 프로젝트에서만 찾았습니다. 다른 프로젝트까지는 --all")] if scoped else []
    tries.append(({}, False, "이 프로젝트에는 없어 모든 프로젝트에서 찾은 결과입니다" if scoped else ""))
    if len(words) > 1:
        partial = "모든 단어가 든 턴은 없어 일부 단어만 맞는 결과입니다"
        if scoped:
            tries.append(({"same_project": me}, True, partial + "(이 프로젝트)"))
        tries.append(({}, True, partial + ("(모든 프로젝트)" if scoped else "")))
    res = []
    for extra, any_word, note in tries:
        res = find(extra, any_word)
        if res:
            if note:
                print("(%s)\n" % note)
            break
    if not res:
        print("결과 없음 — 다른 표현이나 더 짧은 단어로 다시 찾아 보세요")
        return
    for r in res:
        print("#t%d · %s · %s · %s [%s]\n    %s" % (
            r["turn_id"], ago(r.get("at")), r.get("title") or "(제목 없음)", r.get("project_name") or "",
            "요청" if r.get("in") == "prompt" else "응답", clip(r.get("snippet"), 220)))
    print("\n자세히: timeline #tN (앞뒤 흐름) · turn #tN (전문)")


def turn_id(ref):
    ref = (ref or "").lstrip("#").lstrip("t")
    if not ref.isdigit():
        sys.exit("턴 번호는 검색 결과의 #tN 입니다")
    return int(ref)


def cmd_timeline(args):
    if not args:
        sys.exit("사용법: ./argus timeline <#tN> [앞뒤 턴 수]")
    n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 3
    d = api("GET", "/api/turns/%d?around=%d" % (turn_id(args[0]), max(0, min(n, 20))))
    s = d["session"]
    print("%s · %s · %s · id %s" % (s.get("title") or "(제목 없음)", s.get("project_name") or "",
                                   s.get("branch") or "", s.get("session_id")))
    for t in d["before"] + [d["turn"]] + d["after"]:
        mark = "▶▶" if t["id"] == d["turn"]["id"] else "  "
        print("\n%s #t%d (%s)" % (mark, t["id"], ago(t.get("prompt_at") or t.get("response_at"))))
        if t.get("prompt"):
            print("   요청: " + clip(asked(t["prompt"]), 300))
        if t.get("response"):
            print("   응답: " + clip(t["response"], 500))


def cmd_turn(args):
    if not args:
        sys.exit("사용법: ./argus turn <#tN> [--full]")
    limit = None if "--full" in args else 6000
    d = api("GET", "/api/turns/%d" % turn_id(args[0]))
    s, t = d["session"], d["turn"]

    def body(text):
        text = (text or "").strip()
        if limit and len(text) > limit:
            return text[:limit] + "\n…(%d자 생략, --full 로 전체)" % (len(text) - limit)
        return text
    print("#t%d · %s · %s · %s · 세션 %s" % (t["id"], ago(t.get("prompt_at")), s.get("title") or "(제목 없음)",
                                         s.get("root") or "", s.get("session_id")))
    if t.get("prompt"):
        print("\n▶ 요청\n" + body(asked(t["prompt"])))
    for m in t.get("middle") or []:
        print("\n… 중간 메시지\n" + body(m))
    if t.get("response"):
        print("\n◀ 응답\n" + body(t["response"]))


def cmd_inbox(args):
    sub = args[0] if args else "list"
    if sub == "move":
        return cmd_inbox_move(args[1:])
    if sub == "runs":   # 정리 일꾼 회차와 보고
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 3
        for r in api("GET", "/api/inbox/runs?limit=%d" % n).get("runs", []):
            print("## %s %s · %s%s" % (time.strftime("%m/%d %H:%M", time.localtime(r["started_at"])), "자동" if r["trigger"] == "auto" else "직접",
                                      {"running": "정리 중", "done": "끝남", "error": "실패"}.get(r["status"], r["status"]),
                                      " · 새 카드 %d" % (r["new_cards"] or 0) if r["status"] == "done" else ""))
            print(r.get("summary") or r.get("error") or "")
            print()
        return
    if sub == "run":   # 정리 일꾼을 지금 돌린다(결과는 inbox runs)
        print(api("POST", "/api/inbox/run", {}).get("message"))
        return
    if sub == "files":   # 카드 원문(과 스레드)의 첨부 파일
        if len(args) < 2:
            sys.exit("사용법: ./argus inbox files <카드 key>")
        c = next((x for x in api("GET", "/api/inbox").get("items", []) if x.get("key") == args[1] or str(x.get("id")) == args[1]), None)
        if not c:
            sys.exit("카드를 찾지 못했습니다: " + args[1])
        d = api("GET", "/api/inbox/%s/files" % c["id"], timeout=60)
        print("원문: %s" % (d.get("link") or c.get("permalink") or "(주소 모름)"))
        for f in d.get("files") or []:
            print("📎 %s (%s) — %s · %s" % (f["name"], f.get("type") or "", f.get("who") or "", f.get("link") or ""))
        if not d.get("files"):
            print("첨부 파일 없음")
        return
    if sub == "meta":
        m = api("GET", "/api/inbox/meta")
        at = m.get("checked_at") or 0
        print("지난 확인: %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) + " (unix %d)" % at if at else "없음"))
        print("이미 카드로 만든 키 %d개:" % len(m["keys"]))
        for k in m["keys"]:
            print(k)
    elif sub == "add":
        path = args[1] if len(args) > 1 else None
        if path:
            full = inbox_file(path)
            with open(full, encoding="utf-8") as f:
                raw = f.read().strip()
        else:
            raw = sys.stdin.read().strip()
        try:
            data = json.loads(raw)
        except ValueError as e:
            sys.exit("JSON 이 아닙니다: %s" % e)
        items = data if isinstance(data, list) else [data]
        added = 0
        for it in items:
            added += bool(api("POST", "/api/inbox", it).get("added"))
        print("카드 %d개 중 %d개 추가(나머지는 이미 있음)" % (len(items), added))
        if path:
            os.remove(full)
    elif sub == "raw":
        d = api("GET", "/api/feed")
        for f in d.get("instances") or []:
            if f.get("error"):
                print("! 수집 오류 %s %s: %s" % (f.get("type_name"), f.get("name"), f["error"]))
        items = d["items"]
        print("수집된 항목 %d개 (카드로 만든 뒤 ./argus inbox raw-done 으로 표시)" % len(items))
        for x in items:
            card_key = x["key"].split(":", 1)[1] if ":" in x["key"] else x["key"]
            print("\n- key %s · 출처 %s · 카드 key %s · %s · %s · %s · %s" % (x["key"], x["source"], card_key, x["account"],
                                                                          x["title"], x["kind"], x["author"]))
            print("  시각 %s · %s" % (time.strftime("%m-%d %H:%M", time.localtime(x["ts"])), x["link"]))
            if x.get("text"):
                print("  " + clip(x["text"], 1200))
    elif sub == "raw-done":
        rest = args[1:]
        if not rest:
            sys.exit("사용법: ./argus inbox raw-done <키…> 또는 --all")
        api("POST", "/api/feed/done", {"all": True} if rest == ["--all"] else {"keys": rest})
        print("처리했다고 표시했습니다")
    elif sub == "checked":
        prev = api("POST", "/api/inbox/checked", {}).get("previous") or 0
        print("확인 시각을 기록했습니다(이전: %s)" % (time.strftime("%m-%d %H:%M", time.localtime(prev)) if prev else "없음"))
    elif sub == "thread":
        cmd_thread(args[1:])
    elif sub in ("delegate", "log"):
        if len(args) < 2:
            sys.exit("사용법: ./argus inbox %s <카드 key>%s" % (sub, " [세션]" if sub == "delegate" else ""))
        cards = {x["key"]: x for x in api("GET", "/api/inbox")["items"]}
        card = cards.get(args[1])
        if not card:
            sys.exit("없는 카드: " + args[1])
        if sub == "log":
            for ln in api("GET", "/api/inbox/%d/log" % card["id"])["items"]:
                print("%s %s %s" % (time.strftime("%m/%d %H:%M", time.localtime(ln["at"])), ln["kind"], clip(ln.get("text") or "", 300)))
            return
        if not card.get("prompt"):
            sys.exit("카드에 보낼 프롬프트가 없습니다")
        sid = target(args[2])["session_id"] if len(args) > 2 else card.get("rec_session")
        if not sid:
            sys.exit("맡길 세션을 적어 주세요: ./argus inbox delegate <카드 key> <세션>")
        d = api("POST", "/api/inbox/%d/delegate" % card["id"], {"session_id": sid, "prompt": card["prompt"],
                                                                "from": my_sid()})
        print(d.get("message"))
    elif sub == "close":
        keys = args[1:]
        if not keys:
            sys.exit("사용법: ./argus inbox close <카드 key…>")
        cards = {x["key"]: x for x in api("GET", "/api/inbox")["items"]}
        for k in keys:
            if k not in cards:
                print("없는 카드: " + k)
                continue
            api("PATCH", "/api/inbox/%d" % cards[k]["id"], {"status": "done"})
            print("처리함: %s (%s · %s)" % (k, cards[k]["channel"], cards[k]["author"]))
    else:
        items = [x for x in api("GET", "/api/inbox")["items"] if x["status"] in ("new", "doing")]
        print("열린 카드 %d개 — 할 일·진행 중 (고칠 때는 같은 key 에 \"replace\": true 로 inbox add)" % len(items))
        for x in items:
            print("\n- key %s · %s · %s · %s · %s · 급함 %s · 종류 %s%s" % (
                x["key"], "진행 중" if x["status"] == "doing" else "할 일", x.get("source") or "slack", x["channel"], x["author"],
                x["urgency"], x["rec_type"], " · 스레드 " + x["thread_key"] if x.get("thread_key") else ""))
            print("  요약: " + clip(x["summary"], 400))
            if x.get("todo"):
                print("  할 일: " + clip(x["todo"], 200))
            if x["rec_type"] in ("session", "new") or x.get("rec_session"):
                print("  추천: %s %s %s — %s" % (x["rec_type"], x.get("rec_session") or "", x.get("rec_folder") or "",
                                                 clip(x.get("rec_reason"), 160)))
            if x.get("prompt"):
                print("  프롬프트(전체):\n" + "\n".join("    " + ln for ln in x["prompt"].splitlines()))
            if x.get("reply_draft"):
                print("  답장 초안(전체):\n" + "\n".join("    " + ln for ln in x["reply_draft"].splitlines()))


SLACK_LINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")


def cmd_thread(args):
    """슬랙 메시지 뒤 대화. 카드·수집 항목의 링크에서 스레드(thread_ts)를 알아낸다."""
    if not args:
        sys.exit("사용법: ./argus inbox thread <카드 key|수집 key|슬랙 링크>")
    ref = args[0]
    link = ref if "slack.com/" in ref else ""
    if not link:
        for x in api("GET", "/api/inbox")["items"]:
            if x["key"] == ref:
                link = x.get("permalink") or ""
        for x in api("GET", "/api/feed").get("items") or []:
            if x["key"] == ref:
                link = x.get("link") or ""
    m = SLACK_LINK.search(link)
    if m:
        channel, ts = m.group(1), m.group(2) + "." + m.group(3)
    else:
        parts = ref.split(":")
        if len(parts) < 2 or not re.fullmatch(r"\d+\.\d+", parts[-1]):
            sys.exit("슬랙 메시지를 알아보지 못했습니다(카드 key·수집 key·슬랙 링크 중 하나)")
        channel, ts = parts[-2], parts[-1]
    q = {"channel": channel, "ts": ts}
    th = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("thread_ts") if link else None
    if th and th[0] != ts:
        q["thread_ts"] = th[0]
    d = api("GET", "/api/inbox/followups?" + urllib.parse.urlencode(q))
    msgs = d.get("messages") or []
    print("%s · %s 이후 %d개%s" % (d.get("connector"), time.strftime("%m/%d %H:%M", time.localtime(float(ts))), len(msgs),
                                  " (스레드)" if "thread_ts" in q else ""))
    for x in msgs:
        print("- [%s] %s: %s" % ("나" if x["me"] else x["who"], time.strftime("%m/%d %H:%M", time.localtime(x["ts"])),
                                 clip(x["text"], 400)))
    mine = [x for x in msgs if x["me"]]
    if not msgs:
        print("이어진 대화 없음 — 아직 답하지 않았습니다")
    elif mine:
        last = msgs[-1]
        print("내 답장 %d개 · 마지막 글: %s" % (len(mine), "나" if last["me"] else last["who"] + "(내 답장 뒤에 또 옴)"))
    else:
        print("내 답장 없음")


def inbox_file(path):
    """브레인이 쉘 인용 문제 없이 긴 글을 넘기는 파일 — 브레인 폴더(지금 폴더)나 저장소 brain/ 의 inbox/ 안 파일만."""
    full = os.path.realpath(path)
    boxes = [os.path.realpath(os.path.join(os.getcwd(), "inbox")),
             os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "brain", "inbox"))]
    if not any(full.startswith(b + os.sep) for b in boxes):
        sys.exit("브레인 폴더의 inbox/ 안 파일만 넣을 수 있습니다")
    return full


def target(ref):
    state = api("GET", "/api/state")
    s = resolve(state, ref)
    if not s:
        sys.exit("세션을 찾지 못했습니다: " + ref)
    return s


EV_LABEL = {"response": "새 응답", "waiting": "확인 필요", "question": "질문", "error": "오류",
            "compact": "컴팩트", "start": "시작", "end": "종료"}


def cmd_events(args):
    now = time.time()
    since, kinds, unseen = now - 86400, "response,waiting,question,error", False
    for a in args:
        if a == "--all":
            kinds = ""
        elif a == "--unseen":
            unseen = True
        elif a == "오늘":
            since = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        elif a == "어제":
            since = time.mktime(time.localtime(now - 86400)[:3] + (0, 0, 0, 0, 0, -1))
        elif a.rstrip("시간h").isdigit():
            since = now - int(a.rstrip("시간h")) * 3600
    q = {"since": "%.0f" % since, "limit": "300"}
    if kinds:
        q["kind"] = kinds
    if unseen:
        q["unseen"] = "1"
    items = api("GET", "/api/events?" + urllib.parse.urlencode(q))["items"]
    items = [x for x in items if not x.get("brain")]
    if not items:
        print("활동 없음")
        return
    for x in reversed(items):
        print("%s %s · %s%s · %s%s" % (time.strftime("%m/%d %H:%M", time.localtime(x["at"])), EV_LABEL.get(x["kind"], x["kind"]),
                                     x.get("title") or "(제목 없음)", " (%s)" % x["project_name"] if x.get("project_name") else "",
                                     clip(x.get("text") or "", 200), " · 안 봄" if x.get("unseen") else ""))


def cmd_git(args):
    d = api("GET", "/api/git-report" + ("?refresh=1" if "--refresh" in args else ""))
    print(d["text"])


def cmd_notes(args):
    print(api("GET", "/api/notes-report")["text"])


def cmd_send(args):
    if len(args) < 2:
        sys.exit("사용법: ./argus send <ID앞자리|제목 일부> <메시지…>  또는  --file inbox/<파일>")
    s = target(args[0])
    path = None
    if args[1] == "--file":
        if len(args) < 3:
            sys.exit("--file 다음에 파일 경로를 주세요")
        path = inbox_file(args[2])
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    else:
        text = " ".join(args[1:]).strip()
    if not text:
        sys.exit("보낼 내용이 없습니다")
    d = api("POST", "/api/sessions/%s/send" % s["session_id"],
            {"text": text, "from": my_sid()})
    if path:
        os.remove(path)
    print("%s (ID %s) — %s · 답이 오면 [자동 알림]으로 알려 줍니다(그때 ./argus session %s)" % (
        s.get("title") or "(제목 없음)", s["session_id"][:8], d.get("message") or "보냈습니다", s["session_id"][:8]))


def cmd_remote(args):
    if not args:
        sys.exit("사용법: ./argus remote <ID앞자리|제목 일부>")
    s = target(args[0])
    d = api("POST", "/api/sessions/%s/remote" % s["session_id"], {"from": my_sid()})
    print("%s (%s) — %s" % (s.get("title") or "(제목 없음)", s["session_id"][:8], d.get("message") or "켰습니다"))


def ME():
    """보낸 쪽(브레인) 세션 ID — 서버가 자동 정리 중인지 본다."""
    return my_sid()


def cmd_quit(args):
    if not args:
        sys.exit("사용법: ./argus quit <ID앞자리|제목 일부> [--force]")
    s = target(args[0])
    # 작업 중이면 멈추기·종료·탭 닫기까지 기다린다(길면 30초)
    d = api("POST", "/api/sessions/%s/quit" % s["session_id"], {"force": "--force" in args[1:], "from": ME()}, timeout=60)
    print("%s (%s) — %s" % (s.get("title") or "(제목 없음)", s["session_id"][:8], d.get("message") or "종료했습니다"))


def cmd_restart(args, restart=True):
    """보드 칸 ⋮ 「세션 다시 시작」(restart) · 「보드 터미널로 가져오기」(import) — 같은 서버 기능."""
    name = "restart" if restart else "import"
    _need(args, 1, name + " <세션>")
    s = target(args[0])
    if restart and s.get("state") in ("working", "waiting"):
        sys.exit("작업 중이거나 확인을 기다리는 세션입니다 — 끝난 뒤 다시 시작해 주세요")
    d = _post(s, "import", {"restart": True} if restart else {}, timeout=120)
    msg = d.get("message") or ("다시 시작했습니다" if restart else "보드 터미널로 가져왔습니다")
    if restart:
        msg = re.sub(r"보드 터미널로 가져왔습니다|보드 터미널에서 이어 열었습니다", "세션을 다시 시작했습니다", msg)
    _say(s, msg)


def cmd_archive(args, archived=True):
    if not args:
        sys.exit("사용법: ./argus %s <ID앞자리|제목 일부>" % ("archive" if archived else "unarchive"))
    s = resolve(api("GET", "/api/state?archived=1"), args[0])
    if not s:
        sys.exit("세션을 찾지 못했습니다: " + args[0])
    api("POST", "/api/sessions/%s/archive" % s["session_id"], {"archived": archived, "from": ME()})
    print("%s (%s) — %s" % (s.get("title") or "(제목 없음)", s["session_id"][:8], "보관했습니다(보드에서 숨김)" if archived else "보관을 풀었습니다"))


def cmd_new(args):
    opts, words, i = {"worktree": True}, [], 0
    while i < len(args):
        a = args[i]
        if a in ("--model", "--effort") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]
            i += 2
            continue
        if a == "--no-worktree":
            opts["worktree"] = False
        else:
            words.append(a)
        i += 1
    if not words:
        sys.exit("사용법: ./argus new <폴더> [--model …] [--effort …] [--no-worktree] [첫 메시지…]")
    folder, first = os.path.expanduser(words[0]), " ".join(words[1:]).strip()
    req = {"cwd": folder, "worktree": opts["worktree"], "from": ME()}
    for k in ("model", "effort"):   # 안 주면 서버가 설정의 새 세션 기본값(모델·effort)
        if opts.get(k):
            req[k] = opts[k]
    d = api("POST", "/api/sessions/new", req)
    print("새 세션을 띄웠습니다(%s%s)" % (folder, " · 새 워크트리" if opts["worktree"] else ""))
    if not first:
        return
    # 훅이 세션을 기록하고 Claude 가 입력을 받을 수 있을 때까지 기다렸다가 첫 메시지를 보낸다
    for _ in range(60):
        time.sleep(1)
        s = next((x for x in api("GET", "/api/state").get("sessions", []) if x.get("term_handle") == d.get("handle")), None)
        if s:
            time.sleep(2)
            r = api("POST", "/api/sessions/%s/send" % s["session_id"], {"text": first, "from": ME()})
            print("%s (ID %s) — 첫 메시지: %s" % (s.get("title") or "(제목 없음)", s["session_id"][:8], r.get("message") or "보냈습니다"))
            return
    print("세션이 1분 안에 뜨지 않아 첫 메시지는 보내지 못했습니다 — 터미널을 확인해 주세요")


def cmd_note(args):
    if len(args) >= 3 and args[0] == "add":
        s = target(args[1])
        d = api("POST", "/api/notes", {"session_id": s["session_id"], "text": " ".join(args[2:]).strip()})
        print("%s (%s) 넥스트 액션 #%s 추가" % (s.get("title") or "(제목 없음)", s["session_id"][:8], d.get("id")))
    elif len(args) >= 2 and args[1].lstrip("#").isdigit() and args[0] in ("done", "undo", "rm", "edit", "queue", "unqueue"):
        nid = args[1].lstrip("#")
        if args[0] == "rm":
            api("DELETE", "/api/notes/%s" % nid)
        else:
            body = {"done": {"done": True}, "undo": {"done": False}, "queue": {"queued": True}, "unqueue": {"queued": False},
                    "edit": {"text": " ".join(args[2:]).strip()}}[args[0]]
            if args[0] == "edit" and not body["text"]:
                sys.exit("고칠 글을 주세요")
            body["from"] = ME()
            api("PATCH", "/api/notes/%s" % nid, body)
        print("할 일 #%s — %s" % (nid, {"done": "완료", "undo": "완료 취소", "rm": "삭제", "edit": "고침",
                                        "queue": "응답이 끝나면 그 세션에 보내도록 예약", "unqueue": "예약 풀기"}[args[0]]))
    else:
        sys.exit("사용법: ./argus note add <세션> <할 일…> · note done|undo|rm|queue|unqueue <번호> · note edit <번호> <글>")


# ---------------------------------------------------------------- 보드에서 하는 일을 대화로(사용자가 시켰을 때만)
SHORT_MODELS = {"sonnet": "Sonnet 5.5", "opus": "Opus 5.5", "haiku": "Haiku 5.5", "fable": "Fable 5.2"}


def _say(s, msg):
    print("%s (%s) — %s" % (s.get("title") or "(제목 없음)", s["session_id"][:8], msg))


def _post(s, action, body=None, timeout=60):
    return api("POST", "/api/sessions/%s/%s" % (s["session_id"], action), dict(body or {}, **{"from": ME()}), timeout=timeout)


def _need(args, n, usage):
    if len(args) < n:
        sys.exit("사용법: ./argus " + usage)


def cmd_pin(args, pinned=True):
    _need(args, 1, ("pin" if pinned else "unpin") + " <세션>")
    s = target(args[0])
    _post(s, "pin", {"pinned": pinned})
    _say(s, "고정했습니다(맨 앞)" if pinned else "고정을 풀었습니다")


def cmd_rename(args):
    _need(args, 2, "rename <세션> '<새 제목>'")
    s = target(args[0])
    _say(s, _post(s, "rename", {"title": " ".join(args[1:])}).get("message") or "바꿨습니다")


def cmd_stop(args):
    _need(args, 1, "stop <세션>   (작업 중인 Claude 를 멈춤 — Esc)")
    s = target(args[0])
    _say(s, _post(s, "interrupt").get("message") or "멈췄습니다")


def cmd_model(args):
    _need(args, 2, "model <세션> [sonnet|opus|haiku|fable] [--effort low|medium|high|xhigh|max]")
    s = target(args[0])
    model = effort = None
    rest = args[1:]
    if "--effort" in rest:
        i = rest.index("--effort")
        effort = rest[i + 1] if i + 1 < len(rest) else None
        rest = rest[:i] + rest[i + 2:]
    if rest:
        model = SHORT_MODELS.get(rest[0].lower(), rest[0])
    _say(s, _post(s, "model", {"model": model, "effort": effort}).get("message") or "바꿨습니다")


def cmd_perm(args):
    _need(args, 2, "perm <세션> <auto|default|acceptEdits|plan>   (권한 모드 · Codex: auto|default|readonly|bypassPermissions)")
    s = target(args[0])
    d = _post(s, "permission", {"mode": args[1]})
    _say(s, "권한 모드: %s" % d.get("mode"))


def cmd_rewind(args):
    _need(args, 2, "rewind <세션> <#tN> [conversation|both|code|summarize_from|summarize_upto]")
    s = target(args[0])
    num = re.sub(r"^#?t?", "", args[1])
    if not num.isdigit():
        sys.exit("턴 번호는 #tN 모양으로 — ./argus session <세션> 이나 search 결과의 #tN")
    d = _post(s, "rewind", {"turn_id": int(num), "mode": args[2] if len(args) > 2 else "conversation"})
    _say(s, (d.get("message") or "되돌렸습니다") + (" · 지운 요청: " + d["prompt"][:200] if d.get("prompt") else ""))


def _ask_of(s):
    try:
        return json.loads(s.get("ask") or "null")
    except ValueError:
        return None


def cmd_ask(args):
    _need(args, 1, "ask <세션>   (그 세션이 묻고 있는 질문과 선택지)")
    s = target(args[0])
    ask = _ask_of(s)
    if not ask:
        # 질문 창이 아니면 터미널의 선택 창(권한 확인·계획 승인 …)
        v = api("GET", "/api/sessions/%s/prompt" % s["session_id"]).get("prompt")
        if not v:
            return _say(s, "지금 묻고 있는 질문·선택 창이 없습니다")
        print("\n".join(v["head"]))
        for o in v["options"]:
            print("   %d. %s%s" % (o["n"], o["label"], " — " + o["hint"] if o.get("hint") else ""))
        return print("고르기: ./argus answer %s <번호>" % s["session_id"][:8])
    for qi, q in enumerate(ask["questions"], 1):
        print("Q%d. %s%s" % (qi, q.get("question") or "", " (여러 개 고르기)" if q.get("multiSelect") else ""))
        for oi, o in enumerate(q.get("options") or [], 1):
            print("   %d. %s%s" % (oi, o.get("label") or "", " — " + o["description"] if o.get("description") else ""))
    print("답하기: ./argus answer %s <질문마다 번호 — 여러 개는 1,3 · 직접 입력은 기타:글>" % s["session_id"][:8])


def cmd_answer(args):
    _need(args, 2, "answer <세션> <질문마다: 번호 | 1,3 | 기타:글>")
    s = target(args[0])
    ask = _ask_of(s)
    if not ask:
        v = api("GET", "/api/sessions/%s/prompt" % s["session_id"]).get("prompt")
        if not v or not args[1].isdigit():
            sys.exit("그 세션에 지금 답할 질문·선택 창이 없습니다" if not v else "선택 창은 번호 하나로 고릅니다")
        o = next((x for x in v["options"] if x["n"] == int(args[1])), None) or sys.exit("그런 번호가 없습니다")
        return _say(s, _post(s, "prompt", {"n": o["n"], "label": o["label"]}).get("message") or "골랐습니다")
    if args[1] in ("chat", "대화"):   # 「Chat about this」 — 답하지 않고 대화로
        return _say(s, _post(s, "answer", {"id": ask.get("id"), "chat": True}).get("message") or "넘겼습니다")
    answers = []
    for tok in args[1:]:
        if tok.startswith(("기타:", "other:")):
            answers.append({"choices": [], "other": tok.split(":", 1)[1]})
        else:
            answers.append({"choices": [int(x) for x in tok.split(",") if x.strip().isdigit()], "other": ""})
    _say(s, _post(s, "answer", {"id": ask.get("id"), "answers": answers}).get("message") or "답했습니다")


def cmd_focus(args):
    _need(args, 1, "focus <세션>   (그 세션의 터미널 탭을 앞으로)")
    s = target(args[0])
    _say(s, _post(s, "focus").get("message") or "앞으로 가져왔습니다")


def cmd_branch(args, action):
    _need(args, 1, action + " <세션>")
    s = resolve(api("GET", "/api/state?archived=1"), args[0]) or sys.exit("세션을 찾지 못했습니다: " + args[0])
    _say(s, _post(s, action).get("message") or "했습니다")


def cmd_diff(args):
    _need(args, 1, "diff <세션> [줄 수]")
    s = target(args[0])
    d = api("GET", "/api/sessions/%s/diff" % s["session_id"])
    print("%s · %s%s" % (s.get("title") or "", d.get("dir") or "", " · " + d["branch"] if d.get("branch") else ""))
    print(d.get("status") or "커밋 안 한 변경이 없습니다")
    if d.get("stat"):
        print(d["stat"])
    lines = (d.get("diff") or "").splitlines()
    n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 200
    print("\n".join(lines[:n]) + ("\n… (%d줄 더)" % (len(lines) - n) if len(lines) > n else ""))


def cmd_tasks(args):
    _need(args, 1, "tasks <세션> [번호]   (그 세션이 띄운 백그라운드 작업 · 번호를 주면 무엇을 하는지)")
    s = target(args[0])
    ts = s.get("bg_tasks") or []
    if len(args) < 2:
        if not ts:
            return _say(s, "백그라운드 작업이 없습니다(진행 중이거나 끝난 지 30분 안 된 것)")
        for i, x in enumerate(ts, 1):
            print("%d. [%s] %s — %s" % (i, "서브에이전트" if x["kind"] == "agent" else "명령", x["desc"],
                                        "진행 중" if x["status"] == "running" else x["status"] + (" · " + x["summary"] if x.get("summary") else "")))
        return
    x = ts[int(args[1]) - 1]
    d = api("GET", "/api/sessions/%s/tasks/%s" % (s["session_id"], x["id"]))
    if d.get("text") is not None:
        print(d["text"][-4000:])
    for it in d.get("items", [])[-40:]:
        print(("· %s %s" % (it["name"], it["brief"])) if it["kind"] == "tool" else it["text"][:600])


def cmd_agents(args):
    if args and args[0] in ("stop", "attach", "rm"):
        _need(args, 2, "agents stop|attach|rm <ID>")
        d = api("POST", "/api/agents/%s/%s" % (args[1][:8], args[0]), {"from": ME()}, timeout=60)
        return print(d.get("message") or "했습니다")
    items = api("GET", "/api/agents").get("items", [])
    if not items:
        return print("백그라운드 세션이 없습니다")
    for x in items:
        b = x.get("board") or {}
        print("%s %-24s %s · %s%s" % (x["id"], (x.get("name") or "")[:24], "꺼짐(기록만)" if not x.get("alive") else x.get("state"),
                                      os.path.basename(x.get("cwd") or ""), " · 최근 요청: " + b["lp_prompt"][:80] if b.get("lp_prompt") else ""))


def cmd_daily(args):
    import datetime
    w = args[0] if args and not args[0].startswith("--") else "어제"
    day = {"오늘": datetime.date.today(), "어제": datetime.date.today() - datetime.timedelta(days=1)}.get(w)
    day = day.isoformat() if day else w
    if "--run" in args:
        d = api("POST", "/api/daily", {"day": day, "from": ME()})
        return print("%s 일지 정리를 %s" % (day, "시작했습니다(1분쯤 걸림 — 나중에 ./argus daily %s)" % day if d.get("started") else "이미 하는 중입니다"))
    d = api("GET", "/api/daily?day=" + urllib.parse.quote(day))
    print(d.get("content") or "%s 일지가 아직 없습니다(./argus daily %s --run 으로 만들기)" % (day, day))


def cmd_settings(args):
    items = api("GET", "/api/config").get("items", [])
    if not args:
        for x in items:
            print("%-20s %-12s %s%s" % (x["key"], json.dumps(x["value"], ensure_ascii=False), x["label"],
                                        " (고를 수 있는 값: %s)" % ", ".join(map(str, x["options"])) if x["type"] == "choice" else ""))
        return
    _need(args, 2, "settings <키> <값>")
    x = next((i for i in items if i["key"] == args[0]), None) or sys.exit("모르는 설정입니다: " + args[0])
    raw = " ".join(args[1:])
    val = {"bool": lambda v: v.lower() in ("1", "true", "on", "켜기", "예"), "int": int}.get(x["type"], lambda v: v)(raw)
    if x["type"] == "choice" and isinstance(x["default"], int):
        val = int(raw)
    api("POST", "/api/config", {args[0]: val, "from": ME()})
    print("%s = %s" % (args[0], json.dumps(val, ensure_ascii=False)))


def cmd_open(args):
    _need(args, 1, "open <파일 경로> [--reveal]   (기본 앱으로 열기 · --reveal 은 Finder 에서 보기)")
    d = api("POST", "/api/open", {"path": os.path.expanduser(args[0]), "reveal": "--reveal" in args, "from": ME()})
    print(d.get("message") or d.get("result") or "열었습니다")


def cmd_seen(args):
    _need(args, 1, "seen <세션|--all>   (새 응답·활동을 본 것으로)")
    if args[0] == "--all":
        api("POST", "/api/events/seen-all", {})
        return print("모든 활동을 본 것으로 표시했습니다")
    s = target(args[0])
    api("POST", "/api/sessions/%s/seen" % s["session_id"], {})
    _say(s, "본 것으로 표시했습니다")


def cmd_inbox_move(args):
    _need(args, 2, "inbox move <카드 key> <new|doing|reply|done>")
    items = api("GET", "/api/inbox").get("items", [])
    c = next((x for x in items if x.get("key") == args[0] or str(x.get("id")) == args[0]), None) or sys.exit("카드를 찾지 못했습니다: " + args[0])
    api("PATCH", "/api/inbox/%s" % c["id"], {"status": args[1]})
    print("카드 「%s」 → %s" % ((c.get("todo") or c.get("summary") or "")[:60], args[1]))



def cmd_docs_hook(args):
    """브레인 폴더의 UserPromptSubmit 훅: 명령 도움말·규칙 파일(CLAUDE.md)이 이 세션이 읽은 뒤로 바뀌었으면 알려 준다.

    브레인은 규칙을 세션을 시작할 때만 읽어, 그 뒤에 늘어난 명령을 몰랐다. 세션마다 지문을 inbox/ 에 적어 두고 다르면
    최신 도움말을 그 요청에 곁들인다(Claude Code 훅의 additionalContext). 처음 보는 세션은 막 읽었으니 기준만 잡는다."""
    import hashlib
    if os.environ.get("CSL_SKIP"):   # 보드가 따로 돌리는 claude -p(인박스 정리 일꾼 등) — 대화가 아니다
        return
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    sid = re.sub(r"[^\w-]", "", str(data.get("session_id") or ""))[:36]
    if not sid:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "..", "brain", "CLAUDE.md"), "rb") as f:
            rules_sum = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        rules_sum = ""
    help_sum = hashlib.sha256((__doc__ or "").encode()).hexdigest()
    box = os.path.join(here, "..", "brain", "inbox")
    path = os.path.join(box, ".docs-%s" % sid)
    try:
        with open(path) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = None
    os.makedirs(box, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"help": help_sum, "rules": rules_sum}, f)
    if seen is None or (seen.get("help") == help_sum and seen.get("rules") == rules_sum):
        return
    parts = ["[Argus] 이 세션을 시작한 뒤 보드 명령·규칙이 바뀌었습니다. 아래가 지금의 ./argus 명령 목록입니다 — 이것을 기준으로 하세요."]
    if seen.get("rules") != rules_sum:
        parts.append("규칙 파일 CLAUDE.md 도 바뀌었습니다. 이 요청을 처리하기 전에 Read 로 CLAUDE.md 를 다시 읽고 따르세요.")
    parts.append((__doc__ or "").strip())
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n\n".join(parts)}},
                     ensure_ascii=False))

def cmd_calendar(args):
    import datetime
    today = datetime.datetime.combine(datetime.date.today(), datetime.time())
    w = (args[0] if args else "오늘").strip()
    if w in ("오늘", "today"):
        a, days = today, 1
    elif w in ("내일", "tomorrow"):
        a, days = today + datetime.timedelta(days=1), 1
    elif w in ("이번주", "week"):
        a = today
        days = 7 - today.weekday()
    elif w in ("다음주", "nextweek"):
        a = today + datetime.timedelta(days=7 - today.weekday())
        days = 7
    elif w.isdigit():
        a, days = today, max(1, min(int(w), 62))
    else:
        sys.exit("사용법: ./argus calendar [오늘|내일|이번주|다음주|N]")
    start = a.timestamp()
    d = api("GET", "/api/calendar?" + urllib.parse.urlencode({"start": start, "end": start + days * 86400}))
    if not d["calendars"]:
        print("연결된 캘린더 커넥터가 없습니다(보드 ⋮ → 커넥터 → Google Calendar)")
        return
    for c in d["calendars"]:
        print("%s — %s부터 %d일, 일정 %d개" % (c["name"], a.strftime("%m/%d(%a)"), days, len(c["events"])))
        for e in c["events"]:
            extra = " · ".join(x for x in (e["where"], "주최 " + e["organizer"] if e["organizer"] else "",
                                           "내 응답 " + e["response"] if e["response"] else "",
                                           "%d명" % e["attendees"] if e["attendees"] else "") if x)
            print("- %s  %s%s" % (e["when"], e["title"], ("  (" + extra + ")") if extra else ""))


def main():
    args = sys.argv[1:]
    cmds = {"sessions": cmd_sessions, "usage": cmd_usage, "session": cmd_session, "search": cmd_search, "timeline": cmd_timeline, "turn": cmd_turn, "inbox": cmd_inbox, "calendar": cmd_calendar,
            "send": cmd_send, "remote": cmd_remote, "notes": cmd_notes, "events": cmd_events, "git": cmd_git,
            "quit": cmd_quit, "restart": cmd_restart, "import": lambda a: cmd_restart(a, False), "archive": cmd_archive, "unarchive": lambda a: cmd_archive(a, False), "new": cmd_new, "note": cmd_note,
            "pin": cmd_pin, "unpin": lambda a: cmd_pin(a, False), "rename": cmd_rename, "stop": cmd_stop, "model": cmd_model,
            "perm": cmd_perm, "rewind": cmd_rewind, "ask": cmd_ask, "answer": cmd_answer, "focus": cmd_focus,
            "switch": lambda a: cmd_branch(a, "switch"), "attach": lambda a: cmd_branch(a, "attach"),
            "close-branch": lambda a: cmd_branch(a, "close-branch"), "diff": cmd_diff, "tasks": cmd_tasks, "agents": cmd_agents,
            "daily": cmd_daily, "settings": cmd_settings, "open": cmd_open, "seen": cmd_seen, "_docs-hook": cmd_docs_hook}
    if not args or args[0] not in cmds:
        print(__doc__)
        sys.exit(1 if args else 0)
    cmds[args[0]](args[1:])


if __name__ == "__main__":
    main()
