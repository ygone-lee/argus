"""tmux. 세션 기록 키: tmux_pane(TMUX_PANE, 예: "%12") · tmux(TMUX — 앞부분이 서버 소켓 경로).

핸들은 창(pane) id. 소켓을 기록해 두므로 `tmux -L 이름` 처럼 기본이 아닌 서버에서 띄운 세션도 다룬다.
Orca·iTerm2 안에서 tmux 를 쓰면 두 터미널의 환경 변수가 함께 보인다 — 바깥 탭에 쓰면 tmux 가 지금 보이는 창으로
보내 버리므로 tmux 가 먼저 잡는다(inner).
"""
import os
import re
import shutil
import subprocess
import threading
import time

from csl.terminals import Terminal
from csl.terminals.orca import norm_title

_PANE = re.compile(r"^%\d+$")
_SOCK = {}   # 창 id → 서버 소켓 경로(기록에서 읽은 것). 없으면 기본 서버


def _bin():
    # 보드 서버는 훅이 띄워 PATH 가 짧을 수 있다(Homebrew 경로가 빠짐)
    return shutil.which("tmux") or next(
        (p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux") if os.path.exists(p)), None)


def _tmux(*args, handle=None, timeout=6, stdin=None, server=None):
    """server: 서버를 고르는 인자(예: ["-L", "argus-web"]). 없으면 그 창의 기록된 소켓, 그것도 없으면 기본 서버."""
    b = _bin()
    if not b:
        return None
    sock = _SOCK.get(handle)
    try:
        r = subprocess.run([b] + (server or (["-S", sock] if sock else [])) + list(args), capture_output=True, text=True, timeout=timeout,
                           input=stdin)
        return r if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _app_of(pid):
    """그 프로세스를 띄운 macOS 앱(…/X.app) 경로 — 부모를 따라 올라간다. 모르면 None."""
    for _ in range(12):
        try:
            r = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        parts = r.stdout.strip().split(None, 1)
        if r.returncode != 0 or len(parts) != 2:
            return None
        m = re.match(r"^(/.+?\.app)/Contents/MacOS/", parts[1])
        if m:
            return m.group(1)
        pid = int(parts[0])
        if pid <= 1:
            return None
    return None


def _claude_fg(tty):
    """그 tty 의 전면 프로세스가 Claude Code 인지(이름이 claude 거나 …/claude/versions/2.x.y 로 실행된 것)."""
    if not re.fullmatch(r"/dev/ttys\d+", tty or ""):
        return False
    try:
        r = subprocess.run(["ps", "-t", tty[len("/dev/"):], "-o", "stat=,comm="], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and "+" in parts[0]:
            comm = parts[1].strip()
            if os.path.basename(comm) == "claude" or "/claude/versions/" in comm:
                return True
    return False


class Tmux(Terminal):
    name = "tmux"
    label = "tmux"
    env_keys = {"tmux_pane": "TMUX_PANE", "tmux": "TMUX"}
    inner = True   # 다른 터미널 안에서 돈다 — 기록할 때 먼저 잡는다

    def _t(self, *args, handle=None, timeout=6, stdin=None):
        """tmux 명령. 하위 클래스(보드 터미널 web.py)는 자기 tmux 서버로 보낸다."""
        return _tmux(*args, handle=handle, timeout=timeout, stdin=stdin)

    def available(self):
        return bool(_bin())

    def owns(self, term):
        return bool(self.handle_of(term))

    def handle_of(self, term):
        term = term or {}
        h = term.get("tmux_pane")
        if not self.valid(h):
            return None
        sock = (term.get("tmux") or "").split(",")[0]
        if sock.startswith("/"):
            _SOCK[h] = sock
        return h

    def remember(self, term, handle):
        return dict(term, backend=self.name, tmux_pane=handle)

    def valid(self, handle):
        return bool(handle and _PANE.match(handle))

    def terminals(self):
        r = self._t("list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}\t#{pane_current_path}")
        rows = [line.split("\t") for line in (r.stdout.splitlines() if r else [])]
        return [{"handle": x[0], "title": x[1], "path": x[2]} for x in rows if len(x) == 3 and self.valid(x[0])]

    def find(self, title, cwd, project_root):
        want = norm_title(title)
        if not want:
            return None
        hits = [t for t in self.terminals() if norm_title(t["title"]) == want]   # Claude Code 는 제목 앞에 ✳ 등을 붙인다
        if len(hits) > 1:
            hits = [t for t in hits if t["path"] in (cwd, project_root)] or hits
        return hits[0]["handle"] if len(hits) == 1 else None

    def _client(self, handle=None):
        """가장 최근에 쓴 클라이언트(tmux 에 붙어 있는 터미널 화면) {tty, pid, session} 또는 None."""
        r = self._t("list-clients", "-F", "#{client_activity}\t#{client_tty}\t#{client_pid}\t#{session_name}", handle=handle)
        rows = [x.split("\t") for x in (r.stdout.splitlines() if r else [])]
        rows = [x for x in rows if len(x) == 4 and x[2].isdigit()]
        if not rows:
            return None
        x = max(rows, key=lambda x: int(x[0]) if x[0].isdigit() else 0)
        return {"tty": x[1], "pid": int(x[2]), "session": x[3]}

    def switch(self, handle):
        if not self.valid(handle):
            return False
        r = self._t("display-message", "-p", "-t", handle, "#{session_name}", handle=handle)
        if not r:
            return False
        self._t("select-window", "-t", handle, handle=handle)
        self._t("select-pane", "-t", handle, handle=handle)
        c = self._client(handle)
        if not c:
            return False   # 어느 터미널에도 붙어 있지 않은(detached) 세션 — 보여 줄 화면이 없다
        if c["session"] != r.stdout.strip():
            self._t("switch-client", "-c", c["tty"], "-t", handle, handle=handle)
        app = _app_of(c["pid"])
        if app:
            subprocess.run(["open", "-a", app], capture_output=True, timeout=5)
        return True

    def activate(self):
        c = self._client()
        app = _app_of(c["pid"]) if c else None
        if not app:
            return False
        r = subprocess.run(["open", "-a", app], capture_output=True, timeout=5)
        return r.returncode == 0

    def info(self, handle):
        r = self.valid(handle) and self._t("display-message", "-p", "-t", handle,
                                         "#{pane_current_command}\t#{pane_title}\t#{pane_tty}", handle=handle)
        if not r:
            return None
        cmd, title, tty = (r.stdout.rstrip("\n").split("\t") + ["", "", ""])[:3]
        # 전면 명령 이름이 claude 가 아니라 버전 번호(2.1.x)로 보일 수 있다 — tty 의 전면 프로세스로 한 번 더 본다
        agent = "claude" if cmd == "claude" or _claude_fg(tty) else ""
        return {"agent": agent, "title": title}

    def exited(self, handle):
        r = self.valid(handle) and self._t("display-message", "-p", "-t", handle, "#{pane_dead}", handle=handle)
        return None if not r else r.stdout.strip() == "1"

    def screen(self, handle):
        r = self.valid(handle) and self._t("capture-pane", "-p", "-t", handle, handle=handle)   # -e 없이: 색 없는 글, 보이는 영역만
        return r.stdout.rstrip("\n").split("\n") if r else []

    def write(self, handle, data):
        # 버퍼에 담아 창에 그대로 붓는다. send-keys 는 글자를 키로 바꿔 다시 인코딩한다 — Claude Code 가 확장 키 모드를 켜면
        # 줄바꿈(LF)이 Ctrl+J 키 코드로 바뀌어 사라졌다. paste-buffer -r: LF 를 CR 로 바꾸지 않음, -p 없이: 붙여넣기 괄호 없음
        if not self.valid(handle) or not data:
            return bool(self.valid(handle))
        buf = "argus-%d-%d" % (os.getpid(), threading.get_ident())
        return bool(self._t("load-buffer", "-b", buf, "-", handle=handle, stdin=data) and
                    self._t("paste-buffer", "-d", "-r", "-b", buf, "-t", handle, handle=handle, timeout=15))

    def submit(self, handle, text):
        if not self.write(handle, text):
            return False, [], "입력 전송 실패"
        time.sleep(0.3)   # 한 번에 보내면 Claude Code 가 붙여넣기로 보고 Enter 를 줄바꿈으로 받는다 — 글을 다 받은 뒤 Enter
        return self.write(handle, "\r"), [], ""

    def create(self, cwd, title, command):
        """새 창(window)에 로그인 셸을 띄우고 명령을 입력한다 — 오류로 끝나도 셸이 남아 메시지를 볼 수 있다."""
        name = re.sub(r"[^\w\-]+", "-", title or "").strip("-")[:40] or "claude"
        fmt = ("-d", "-P", "-F", "#{pane_id}", "-c", cwd)
        r = self._t("new-window", *fmt, "-n", name, timeout=15)
        if not r:   # 돌고 있는 tmux 서버가 없다 — 세션을 새로 연다(붙으려면 tmux attach)
            r = self._t("new-session", *fmt, "-s", "%s-%d" % (name, int(time.time()) % 100000), timeout=15)
        handle = r.stdout.strip() if r else ""
        if not self.valid(handle):
            return None, "tmux 창을 열지 못했습니다"
        if not (self.write(handle, command) and self.write(handle, "\r")):
            return None, "tmux 창에 명령을 입력하지 못했습니다"
        return handle, None

    def projects(self):
        counts = {}
        home = os.path.expanduser("~")
        for t in self.terminals():
            if t["path"] and t["path"] != home:
                counts[t["path"]] = counts.get(t["path"], 0) + 1
        return [{"path": p, "name": os.path.basename(p.rstrip("/")), "branch": "", "terminals": n, "at": 0}
                for p, n in sorted(counts.items(), key=lambda kv: -kv[1])]

    def close(self, handle):
        if not self.valid(handle):
            return False
        if not self._t("display-message", "-p", "-t", handle, "#{pane_id}", handle=handle):
            return True   # 이미 닫혔다(보드가 띄운 창은 Claude 가 끝나면 셸과 함께 닫힌다)
        return bool(self._t("kill-pane", "-t", handle, handle=handle))


TERMINAL = Tmux
