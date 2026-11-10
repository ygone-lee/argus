"""터미널 계층: 보드가 Claude Code 세션의 터미널을 다루는 공통 인터페이스와 구현체 목록.

보드·브레인·훅은 특정 터미널 앱(Orca 등)을 직접 부르지 않고 이 인터페이스만 쓴다. Claude Code 화면을 해석하는 일
(입력칸·선택 창·모델 창·질문 창)은 csl/focus.py 가 이 인터페이스의 screen·write·submit 위에서 한다.

새 터미널을 붙이려면:
  1. csl/terminals/<이름>.py 에 Terminal 을 상속한 클래스를 만들고 메서드를 구현한다(못 하는 건 기본값 그대로 두면
     「지원하지 않음」으로 처리된다).
  2. 그 모듈에 `TERMINAL = 클래스` 를 둔다. registry() 가 자동으로 찾는다(앞에 있는 것이 기본 — ORDER).
  3. env_keys 에 그 터미널이 Claude Code 프로세스에 넣어 주는 환경 변수(탭·창 식별자)를 적는다. 훅이 세션마다 기록한다.
"""
import importlib
import pkgutil
from collections import namedtuple

# 보드가 다루는 한 터미널: 어느 구현체의 어느 핸들인지
Target = namedtuple("Target", "terminal handle")


class Terminal:
    name = ""          # 내부 이름(세션 기록의 "backend" 값)
    label = ""         # 화면에 보일 이름
    checks_submit = False   # submit 이 입력창에 들어갔는지 스스로 확인한다(Orca) — 아니면 보드가 Enter 전에 화면을 다시 본다
    env_keys = {}      # 훅이 기록할 {터미널 정보 키: 환경 변수}

    # --- 있는지·누구 것인지
    def available(self):
        """이 컴퓨터에서 쓸 수 있는지(앱·CLI 설치 여부)."""
        return False

    def owns(self, term):
        """훅이 기록한 터미널 정보(dict)가 이 터미널의 것인지."""
        return bool(self.handle_of(term))

    def claims(self, environ):
        """env_keys 가 보였을 때 정말 이 터미널인지(같은 변수를 쓰는 터미널끼리 가른다). 기본은 그렇다."""
        return True

    def handle_of(self, term):
        """기록된 터미널 정보에서 핸들. 없으면 None."""
        return None

    def remember(self, term, handle):
        """찾은 핸들을 터미널 정보에 넣은 새 dict(세션에 다시 저장한다)."""
        return dict(term, backend=self.name)

    def valid(self, handle):
        """핸들 형식 검사(명령 인자로 넘기기 전에)."""
        return False

    # --- 찾기·이동
    def find(self, title, cwd, project_root):
        """핸들을 모를 때 탭 제목·작업 경로로 찾기. 하나로 좁혀지지 않으면 None."""
        return None

    def switch(self, handle):
        """그 탭을 앞으로(앱 활성화 포함). 성공 여부."""
        return False

    def activate(self):
        """앱만 앞으로."""
        return False

    def info(self, handle):
        """{"agent": 돌고 있는 에이전트 이름 또는 "", "title": 탭 제목} 또는 None(탭 없음)."""
        return None

    def terminals(self):
        """열린 터미널 목록 [{"handle", "title", "path"}]."""
        return []

    def exited(self, handle):
        """탭은 남아 있는데 그 안의 셸(프로세스)이 끝났으면 True, 돌고 있으면 False, 탭이 없거나 모르면 None."""
        return None

    # --- 입출력
    def screen(self, handle):
        """지금 화면에 보이는 줄들(색 없는 글)."""
        return []

    def screen_ansi(self, handle):
        """색·글꼴 속성(SGR)을 담은 화면 줄들. 못 읽는 터미널이면 None."""
        return None

    def write(self, handle, data):
        """PTY 에 그대로 쓰기(제어 문자·화살표 포함). 성공 여부."""
        return False

    def submit(self, handle, text):
        """Claude Code 입력창에 한 줄 요청을 넣고 Enter. (성공 여부, 단계 목록, 오류 문구)

        기본 구현: 글을 쓰고 Enter. 터미널이 요청 접수·턴 시작을 알려 주면 단계 목록에 담는다.
        """
        if not self.write(handle, text):
            return False, [], "입력 전송 실패"
        return self.write(handle, "\r"), [], ""

    # --- 새 터미널
    def create(self, cwd, title, command):
        """cwd 에서 command 를 실행하는 새 터미널 탭. (핸들, 오류 문구)"""
        return None, "%s 은(는) 새 터미널 만들기를 지원하지 않습니다" % (self.label or self.name)

    def projects(self):
        """새 세션 창에 보여 줄 열린 프로젝트 [{"path", "name", "branch", "terminals", "at"}]."""
        return []

    def close(self, handle):
        return False


ORDER = ["orca", "web", "tmux"]   # 기본 터미널 우선순위(앞이 기본). 목록에 없는 구현체는 뒤에. web 은 tmux 보다 먼저 봐야 한다(같은 변수)
_registry = None


# 세션 하나에 딸린 환경 변수: 보드 서버를 Claude 세션(훅·그 세션의 셸)이 띄우면 이것들을 물려받는다. 그대로 두면 보드가
# 띄우는 터미널·세션이 남의 세션 ID·원격(Remote Control) 세션·메시지 토큰·터미널 식별자를 갖고 시작한다
# (보드 터미널의 세션들이 모두 띄운 쪽 세션의 원격에 연결된 것으로 기록되던 것). 사용자 설정 값(CLAUDE_CONFIG_DIR 등)은 둔다
SESSION_ENV = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "TMUX", "TMUX_PANE", "ITERM_SESSION_ID", "TERM_SESSION_ID",
               "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "__CFBundleIdentifier", "ARGUS_WEB"}
SESSION_ENV_PREFIXES = ("CLAUDE_CODE_", "ORCA_")


def session_env(name):
    return name in SESSION_ENV or name.startswith(SESSION_ENV_PREFIXES)


def registry():
    """설치된 구현체 {이름: 인스턴스} — csl/terminals/*.py 의 TERMINAL."""
    global _registry
    if _registry is None:
        found = {}
        for m in pkgutil.iter_modules(__path__):
            mod = importlib.import_module(__name__ + "." + m.name)
            cls = getattr(mod, "TERMINAL", None)
            if cls and cls.name:
                found[cls.name] = cls()
        names = sorted(found, key=lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n))
        _registry = {n: found[n] for n in names}
    return _registry


def default():
    """새 세션을 띄울 터미널: 쓸 수 있는 것 중 첫 번째. 없으면 None."""
    return next((t for t in registry().values() if t.available() and getattr(t, "auto_default", True)), None)


def of_record(term):
    """훅이 기록한 터미널 정보의 구현체. 모르면 None."""
    term = term or {}
    if term.get("backend") in registry():
        return registry()[term["backend"]]
    return next((t for t in registry().values() if t.owns(term)), None)


def env_info(environ):
    """훅: 환경 변수에서 구현체별 터미널 정보. {"backend": 이름, 키: 값…} 또는 {}.

    다른 터미널 안에서 도는 것(inner — Orca 탭 안의 tmux)을 먼저 본다: 바깥 탭에 쓰면 엉뚱한 창으로 간다."""
    for t in sorted(registry().values(), key=lambda t: not getattr(t, "inner", False)):
        got = {k: environ[v] for k, v in t.env_keys.items() if environ.get(v)}
        if got and t.claims(environ):
            return dict(got, backend=t.name)
    return {}
