"""Argus 설정: 보드의 설정 창에서 바꾸는 값들. DB settings 표에 두고, 없으면 기본값.

새 값을 추가할 때는 SCHEMA 에 한 줄 넣고 쓰는 곳에서 config.get(conn, "키") 로 읽는다.
설정 창은 이 목록을 그대로 그린다(group 순서·label·help).
"""
import os
import re

from csl import store

_REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MODELS = ["sonnet", "opus", "haiku", "fable"]
# 카드 대화에서 고르는 모델(화면 이름 → Claude Code --model 에 넘기는 정확한 ID)
CARDCHAT_MODELS = {"Sonnet 5.5": "claude-sonnet-5-5", "Opus 5.5": "claude-opus-5-5", "Fable 5.2": "claude-fable-5-2", "Fable 5.1": "claude-fable-5-1",
                   "Haiku 5.5": "claude-haiku-5-5"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
BRAIN_PERMS = ["auto", "manual", "acceptEdits", "plan"]   # claude --permission-mode 값(manual = 명령마다 묻기)

# (키, 묶음, 이름, 종류, 기본값, 옵션, 설명)
#   종류: int(옵션 = (최소, 최대)) · bool · choice(옵션 = 고를 값 목록) · path
SCHEMA = [
    ("board_days", "보드", "최근 며칠의 세션을 보여 줄까", "int", 14, (1, 90),
     "마지막 활동이 이 기간 안인 세션만 칸으로 나옵니다. 고정했거나 할 일이 남은 세션은 기간과 상관없이 나옵니다."),
    ("board_cols", "보드", "처음에 그리는 칸 수", "int", 12, (4, 40),
     "나머지는 「세션 N개 더 보기」로 꺼냅니다. 늘리면 처음 열 때 조금 느려질 수 있습니다."),
    ("new_default_model", "보드", "새 세션 기본 모델", "choice", "", [""] + MODELS,
     "「＋ 새 세션」 창에서 처음 골라져 있는 모델. 비우면 Claude Code 설정대로."),
    ("new_default_effort", "보드", "새 세션 기본 effort", "choice", "high", EFFORTS,
     "「＋ 새 세션」 창에서 처음 골라져 있는 effort(claude --effort). 브레인이 effort 없이 새 세션을 열 때도 이 값."),
    ("clear_resets_model", "보드", "/clear 하면 기본 모델로", "bool", True, None,
     "Claude 세션에서 /clear 로 대화를 비우면 「새 세션 기본 모델·effort」로 바꿔 둡니다(브레인은 브레인 모델). "
     "끄면 비우기 전 모델을 그대로 씁니다. 그 터미널에 쳐 둔 글이 있으면 건드리지 않습니다."),
    ("board_ai_model", "보드", "마크다운 보드 AI 모델", "choice", "sonnet", MODELS,
     "마크다운 칸의 AI 대화창에서 문서를 고칠 때 쓰는 모델. 보낼 때마다 claude -p 를 한 번 돌리고, 그때마다 사용량이 듭니다."),
    ("auto_remote", "보드", "보낸 세션에 리모트 컨트롤 자동으로 켜기", "bool", False, None,
     "보드에서 대화를 보낸 세션의 리모트 컨트롤(claude.ai/code·모바일 앱)이 꺼져 있으면 켭니다."),
    ("codex_enabled", "보드", "Codex 세션도 모으기", "bool", True, None,
     "OpenAI Codex(CLI·데스크톱 앱)의 대화를 Claude Code 세션처럼 칸으로 보여 줍니다. ~/.codex 의 기록을 읽기만 합니다. "
     "끄면 새로 모으지 않습니다(이미 모은 칸은 그대로 — 보관하면 사라집니다)."),
    ("auto_daily", "하루 정리", "하루 정리 자동으로 만들기", "bool", True, None,
     "정한 시각이 지나면 전날 작업 일지를 자동으로 정리합니다. 그때 Mac 이 잠들어 있었으면 깨어난 뒤에."),
    ("daily_hour", "하루 정리", "자동 정리 시각(시)", "int", 6, (0, 23), "이 시각이 지나면 전날 일지를 만듭니다."),
    ("daily_model", "하루 정리", "정리에 쓸 모델", "choice", os.environ.get("CSL_DAILY_MODEL", "sonnet"), MODELS, ""),
    ("daily_timeout", "하루 정리", "정리 제한 시간(초)", "int", 300, (60, 1200), "이 시간 안에 끝나지 않으면 실패로 남깁니다."),
    ("brain_dir", "브레인", "브레인 폴더", "path", os.path.join(_REPO, "brain"), None,
     "브레인 세션을 띄우는 폴더. CLAUDE.md·.claude/settings.json·argus·inbox/ 가 있어야 합니다(저장소 brain/ 을 복사해 고쳐 쓰면 됩니다)."),
    ("brain_model", "브레인", "브레인 모델", "choice", "sonnet", [""] + MODELS, "「브레인 시작」 때 처음 골라져 있는 모델."),
    ("brain_permission_mode", "브레인", "브레인 권한 모드", "choice", "auto", BRAIN_PERMS,
     "「브레인 시작」 때 처음 골라져 있는 권한 모드. auto 는 Claude 가 판단해 진행하고 위험해 보이는 것만 묻습니다 · manual 은 명령마다 묻습니다. 켜져 있는 브레인은 패널 머리의 권한 칩으로 바꿉니다."),
    ("brain_slack_every", "브레인", "인박스 자동 정리 간격(분)", "choice", 0, [0, 10, 15, 30, 60],
     "커넥터가 모은 새 항목이 있고 브레인이 쉬고 있을 때, 브레인이 카드로 정리합니다. 0 이면 끔. 정리할 때마다 Claude 사용량이 듭니다(수집 자체는 커넥터 창의 간격으로 늘 돌고 사용량이 들지 않습니다)."),
    ("brain_hour_from", "브레인", "자동 정리 시작 시각(시)", "int", 9, (0, 23), ""),
    ("brain_hour_to", "브레인", "자동 정리 끝 시각(시)", "int", 20, (1, 24), "이 시각 전까지만 정리합니다."),
    ("brain_weekdays_only", "브레인", "평일에만 자동 정리", "bool", True, None, ""),
    ("done_cards_days", "브레인", "완료한 인박스 카드 보관 기간(일)", "int", 1, (1, 30),
     "인박스 칸반의 「완료」 열에 남겨 두는 기간. 지나면 목록에서 사라집니다(카드는 「끝난 인박스 카드 보관 기간」이 지나면 지워짐)."),
    ("slack_team", "브레인", "슬랙 워크스페이스 ID", "text", "", None,
     "넣어 두면 인박스 카드의 「원문 ↗」을 브라우저 탭 없이 슬랙 앱에서 바로 엽니다(⌥ 누르고 누르면 브라우저로). "
     "슬랙을 브라우저로 열었을 때 주소 app.slack.com/client/<ID>/… 의 그 값 — Enterprise Grid 면 조직 ID(E…) 하나로 모든 워크스페이스가 열립니다. "
     "비우면 지금처럼 브라우저 탭으로 엽니다."),
    ("cardchat_model", "브레인", "카드 대화 모델", "choice", "Sonnet 5.5", list(CARDCHAT_MODELS),
     "인박스 카드의 「💬 대화」에 쓰는 모델(정확한 버전). 카드마다 따로 대화하므로 브레인 대화 맥락을 쓰지 않습니다."),
    ("cardchat_effort", "브레인", "카드 대화 effort", "choice", "", [""] + EFFORTS,
     "비우면 모델 기본값. 대화 영역에서 카드마다 바꿀 수도 있습니다."),
    ("update_mode", "업데이트", "업데이트 방식", "choice", "notify", ["notify", "auto", "off"],
     "알림만: 새 버전이 나오면 보드 위 띠와 브라우저 알림으로 알리고, 받기는 직접 누릅니다 · 자동: 작업 중인 세션이 없고 보드를 한동안 쓰지 않을 때 받아서 보드를 다시 띄웁니다(세션은 그대로) · 끔: 확인하지 않습니다."),
    ("update_channel", "업데이트", "업데이트 채널", "choice", "stable", ["stable", "latest"],
     "안정판: 릴리스(v0.2.0 같은 태그)가 새로 나왔을 때만 받습니다 · 최신판: GitHub main 에 올라온 커밋을 모두 받습니다(개발 중인 변경도 바로 옴)."),
    ("retention_days", "기록 보관", "세션 기록 보관 기간(일)", "int", int(os.environ.get("CSL_RETENTION_DAYS", "90")), (7, 3650),
     "마지막 활동이 이보다 오래된 세션과 대화를 하루 한 번 지웁니다. 고정했거나 할 일이 남은 세션은 둡니다. Claude Code 의 기록 파일은 건드리지 않습니다."),
    ("done_notes_days", "기록 보관", "완료한 할 일 보관 기간(일)", "int", 30, (1, 365), ""),
    ("events_days", "기록 보관", "활동 기록 보관 기간(일)", "int", 30, (1, 365), "활동 화면(새 응답·확인 필요·오류…)에 남기는 기간."),
    ("uploads_days", "기록 보관", "붙인 이미지 보관 기간(일)", "int", 30, (1, 365), "넥스트 액션이 가리키지 않는 이미지만 지웁니다."),
    ("inbox_days", "기록 보관", "끝난 인박스 카드 보관 기간(일)", "int", 7, (1, 365),
     "완료·무시한 카드와 그 기록·카드 대화를 마지막으로 바뀐 지 이만큼 지나면 지웁니다. 할 일·진행 중·답장만 남은 카드는 둡니다. "
     "슬랙·메일 요약이 들어 있어 오래 두지 않습니다."),
]
_BY_KEY = {row[0]: row for row in SCHEMA}


def get(conn, key):
    row = _BY_KEY[key]
    v = store.get_setting(conn, key, None)
    if v is not None and row[3] == "choice" and v not in row[5]:
        return row[4]   # 고를 수 있는 값이 바뀐 뒤 남은 옛 값(예: 별칭 sonnet)은 기본값으로
    return row[4] if v is None else v


def get_free(key):
    """연결이 없는 곳(서버 밖 스레드 등)에서 한 값만 읽을 때."""
    conn = store.connect()
    try:
        return get(conn, key)
    finally:
        conn.close()


def validate(key, value):
    """(바꿀 값, 오류 문구). 틀리면 (None, 문구)."""
    row = _BY_KEY.get(key)
    if not row:
        return None, "모르는 설정입니다: %s" % key
    kind, opt = row[3], row[5]
    if kind == "int":
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, "「%s」는 숫자여야 합니다" % row[2]
        if not opt[0] <= v <= opt[1]:
            return None, "「%s」는 %d~%d 사이여야 합니다" % (row[2], opt[0], opt[1])
        return v, None
    if kind == "bool":
        return bool(value), None
    if kind == "choice":
        for c in opt:
            if str(c) == str(value):
                return c, None
        return None, "「%s」에 고를 수 없는 값입니다" % row[2]
    if kind == "text":
        v = str(value or "").strip()[:2000]
        if key == "slack_team" and v and not re.fullmatch(r"[TE][A-Z0-9]{6,15}", v):
            return None, "「%s」는 T 나 E 로 시작하는 ID 여야 합니다(예: E03ABCDEF)" % row[2]
        return v, None
    if kind == "path":
        p = os.path.realpath(os.path.expanduser(str(value or "").strip()))
        if not os.path.isdir(p):
            return None, "폴더가 없습니다: %s" % p
        return p, None
    return None, "알 수 없는 종류"


def describe(conn):
    """설정 창이 그릴 목록."""
    return [{"key": k, "group": g, "label": lab, "type": kind, "default": d,
             "options": list(opt) if opt is not None else None, "help": h, "value": get(conn, k)}
            for k, g, lab, kind, d, opt, h in SCHEMA]
