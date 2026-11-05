"""Orca 터미널(orca CLI). 세션 기록 키: orca_handle(ORCA_TERMINAL_HANDLE)."""
import json
import os
import re
import subprocess

from csl.terminals import Terminal

ORCA_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"
ORCA_BUNDLE = "com.stablyai.orca"
_HANDLE = re.compile(r"^term_[\w-]+$")


def _run(args, timeout=6):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _json(r):
    try:
        return json.loads(r.stdout) if r else None
    except ValueError:
        return None


def _error(d, r, fallback):
    if d is None:
        return ((r.stderr or r.stdout or "").strip() if r else "Orca CLI 실행 실패")[:200] or fallback
    err = d.get("error")
    return (err.get("message") if isinstance(err, dict) else str(err or fallback))[:200]


def norm_title(t):
    # Orca 는 탭 제목 앞에 상태 기호(✳ 등)를 붙인다
    return re.sub(r"\s+", " ", re.sub(r"^[^\w가-힣]+", "", t or "")).strip().lower()


class Orca(Terminal):
    name = "orca"
    label = "Orca"
    checks_submit = True   # orca terminal send --wait-submit 이 접수를 확인한다
    env_keys = {"orca_handle": "ORCA_TERMINAL_HANDLE"}

    def available(self):
        return os.path.exists(ORCA_BIN)

    def owns(self, term):
        return bool(self.handle_of(term)) or term.get("program") == "Orca" or term.get("bundle") == ORCA_BUNDLE

    def handle_of(self, term):
        h = (term or {}).get("orca_handle")
        return h if self.valid(h) else None

    def remember(self, term, handle):
        return dict(term, program="Orca", backend=self.name, orca_handle=handle)

    def valid(self, handle):
        return bool(handle and _HANDLE.match(handle))

    def _items(self, kind):
        d = _json(_run([ORCA_BIN, kind, "list", "--json"], timeout=8))
        try:
            res = d.get("result") or {}
            items = res.get(kind + "s", []) if isinstance(res, dict) else res
            return [x for x in items if isinstance(x, dict)]
        except AttributeError:
            return []

    def terminals(self):
        return [{"handle": t["handle"], "title": t.get("title") or "", "path": t.get("worktreePath") or "",
                 "agent": t.get("agentIdentity") or "", "orphaned": bool(t.get("orphaned"))}
                for t in self._items("terminal") if t.get("handle")]

    def find(self, title, cwd, project_root):
        if not self.available() or not title:
            return None
        want = norm_title(title)
        hits = [t for t in self.terminals() if norm_title(t["title"]) == want]
        if len(hits) > 1:
            paths = {p for p in (cwd, project_root) if p}
            narrowed = [t for t in hits if t["path"] in paths or (cwd and t["path"] and cwd.startswith(t["path"]))]
            hits = narrowed or hits
        if len(hits) > 1:
            hits = [t for t in hits if t["agent"] == "claude"] or hits
        return hits[0]["handle"] if len(hits) == 1 else None

    def activate(self):
        r = _run(["open", "-b", ORCA_BUNDLE])
        return bool(r and r.returncode == 0)

    def switch(self, handle):
        if not self.valid(handle) or not self.available():
            return False
        d = _json(_run([ORCA_BIN, "terminal", "switch", "--terminal", handle, "--json"]))
        if d and d.get("ok"):
            self.activate()
            return True
        return False

    def info(self, handle):
        if not self.valid(handle):
            return None
        d = _json(_run([ORCA_BIN, "terminal", "show", "--terminal", handle, "--json"]))
        try:
            t = d["result"]["terminal"]
        except (KeyError, TypeError):
            return None
        return {"agent": t.get("agentIdentity") or "", "title": t.get("title") or ""}

    def exited(self, handle):
        # 셸이 끝나도 Orca 는 탭을 빈 채로 남긴다(connected=false, 실시간 목록에서 빠짐)
        if not self.valid(handle):
            return None
        d = _json(_run([ORCA_BIN, "terminal", "show", "--terminal", handle, "--json"]))
        try:
            t = d["result"]["terminal"]
        except (KeyError, TypeError):
            return None
        return t.get("connected") is False

    def screen(self, handle):
        if not self.valid(handle):
            return []
        d = _json(_run([ORCA_BIN, "terminal", "read", "--terminal", handle, "--screen", "--json"]))
        try:
            return d["result"]["terminal"]["tail"]
        except (KeyError, TypeError):
            return []

    def write(self, handle, data):
        # --enter 없이 보내면 PTY 에 그대로 쓴다(제어 문자도 통과)
        if not self.valid(handle):
            return False
        d = _json(_run([ORCA_BIN, "terminal", "send", "--terminal", handle, "--text", data, "--json"], timeout=15))
        return bool(d and d.get("ok"))

    def submit(self, handle, text):
        """--enter 로 보내면 Orca 가 Claude 입력창에 붙여넣기로 넣고, 접수·턴 시작 단계를 3초까지 지켜본다."""
        if not self.valid(handle):
            return False, [], "터미널 핸들이 올바르지 않습니다"
        r = _run([ORCA_BIN, "terminal", "send", "--terminal", handle, "--text", text,
                  "--enter", "--wait-submit", "3", "--json"], timeout=15)
        d = _json(r)
        if not d or not d.get("ok"):
            return False, [], _error(d, r, "전송 실패")
        send = (d.get("result") or {}).get("send") or {}
        return bool(send.get("accepted")), (send.get("prompt") or {}).get("stages") or [], ""

    def projects(self):
        """열린 프로젝트(보관 안 된 작업 공간) — 터미널 수가 많은 순, 같으면 최근 활동 순."""
        counts = {}
        for t in self.terminals():
            if t["path"] and not t["orphaned"]:
                counts[t["path"]] = counts.get(t["path"], 0) + 1
        out = [{"path": w["path"], "name": os.path.basename(w["path"].rstrip("/")),
                "branch": (w.get("branch") or "").replace("refs/heads/", ""), "terminals": counts.get(w["path"], 0),
                "at": w.get("lastActivityAt") or 0}
               for w in self._items("worktree") if w.get("path") and not w.get("isArchived")]
        out.sort(key=lambda w: (-w["terminals"], -w["at"]))
        return out

    def create(self, cwd, title, command):
        """탭은 cwd 를 담은 Orca 작업 공간(가장 가까운 것)에, 없으면 열린 프로젝트 중 첫 번째(터미널 많은 순)에 만든다."""
        spaces = [w["path"] for w in self._items("worktree")
                  if w.get("path") and (cwd == w["path"] or cwd.startswith(w["path"].rstrip("/") + "/"))]
        if spaces:
            where = "path:" + max(spaces, key=len)
        else:
            # Orca 의 "active" 는 명령을 실행한 폴더(보드 서버 = 이 저장소)로 풀려 실패한다
            opened = self.projects()
            if not opened:
                return None, "Orca 에 열린 프로젝트가 없습니다"
            where = "path:" + opened[0]["path"]
        r = _run([ORCA_BIN, "terminal", "create", "--worktree", where, "--title", title,
                  "--command", command, "--json"], timeout=15)   # --focus 없이: 보고 있던 Orca 탭을 뺏지 않는다
        d = _json(r)
        handle = (((d or {}).get("result") or {}).get("terminal") or {}).get("handle")
        if not d or not d.get("ok", True) or not handle:
            return None, _error(d, r, "터미널을 만들지 못했습니다")
        return handle, None

    def close(self, handle):
        if not self.valid(handle):
            return False
        d = _json(_run([ORCA_BIN, "terminal", "close", "--terminal", handle, "--json"]))
        return bool(d and d.get("ok"))


TERMINAL = Orca
