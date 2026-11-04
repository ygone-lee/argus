"""보드 터미널: 보드가 직접 가진 tmux 서버(-L argus-web)에 띄우고, 브라우저의 xterm.js 로 칸 안에서 보여 준다.

- 사용자의 tmux(기본 서버)와 섞이지 않게 따로 띄운 서버다. 세션 하나 = tmux 세션 하나(창 하나).
- 서버 전체 환경 변수 ARGUS_WEB=1 을 두어, 그 안에서 뜬 Claude Code 를 훅이 이 터미널로 기록한다(tmux 와 같은
  TMUX_PANE 변수를 쓰므로 claims 로 가른다).
- 화면 읽기·입력은 tmux 구현을 그대로 쓴다(서버만 다르다). 브라우저 연결은 server.py 의 /api/term/… 웹소켓이
  `tmux attach` 를 PTY 로 띄워 이어 준다 — 서버를 다시 띄워도 tmux 안의 세션은 살아 있다.
"""
import re
import time

from csl.terminals.tmux import Tmux, _tmux

SERVER = ["-L", "argus-web"]
# 브라우저 화면에 맞춘 tmux 설정: 상태 줄 없음, 마우스(휠 스크롤), 넉넉한 기록, 마지막으로 쓴 화면 크기에 맞춤
OPTIONS = [("status", "off"), ("mouse", "on"), ("history-limit", "50000"), ("window-size", "latest"),
           ("escape-time", "10"), ("default-terminal", "xterm-256color"), ("focus-events", "on")]


class Web(Tmux):
    name = "web"
    label = "보드 터미널"
    env_keys = {"web_pane": "TMUX_PANE", "web": "ARGUS_WEB"}
    inner = True
    auto_default = False   # 새 세션의 기본 터미널로 저절로 고르지 않는다(새 세션 창에서 고른다)

    def _t(self, *args, handle=None, timeout=6, stdin=None):
        return _tmux(*args, timeout=timeout, stdin=stdin, server=SERVER)

    def claims(self, environ):
        return environ.get("ARGUS_WEB") == "1"

    def handle_of(self, term):
        h = (term or {}).get("web_pane")
        return h if self.valid(h) else None

    def remember(self, term, handle):
        return dict(term, backend=self.name, web_pane=handle)

    def switch(self, handle):
        return False   # 바깥 앱이 없다 — 보드가 칸 안의 터미널을 연다

    def activate(self):
        return False

    def _setup(self):
        for k, v in OPTIONS:
            self._t("set-option", "-g", k, v)
        self._t("set-environment", "-g", "ARGUS_WEB", "1")

    def create(self, cwd, title, command):
        name = "%s-%d" % ((re.sub(r"[^\w\-]+", "-", title or "").strip("-")[:40] or "claude"), int(time.time() * 1000) % 10**7)
        # 서버가 없으면 첫 세션이 서버를 띄운다 — 설정·환경 변수는 그 셸이 뜨기 전에 넣어야 해서 -e 로도 준다
        r = self._t("new-session", "-d", "-P", "-F", "#{pane_id}", "-c", cwd, "-s", name, "-x", "160", "-y", "48",
                    "-e", "ARGUS_WEB=1", timeout=15)
        handle = r.stdout.strip() if r else ""
        if not self.valid(handle):
            return None, "보드 터미널(tmux)을 열지 못했습니다"
        self._setup()
        if not (self.write(handle, command) and self.write(handle, "\r")):
            return None, "보드 터미널에 명령을 입력하지 못했습니다"
        return handle, None

    def session_of(self, handle):
        """그 창이 든 tmux 세션 이름(브라우저가 붙을 대상). 없으면 None."""
        r = self.valid(handle) and self._t("display-message", "-p", "-t", handle, "#{session_name}")
        return r.stdout.strip() if r else None

    def close(self, handle):
        name = self.session_of(handle)
        if not name:
            return True
        return bool(self._t("kill-session", "-t", name))


TERMINAL = Web
