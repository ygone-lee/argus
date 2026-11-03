"""Finder 에서 입력창으로 끌어 놓은 파일의 원래 경로 찾기.

브라우저는 보안상 끌어 놓은 파일의 경로를 주지 않는다 — 이름·크기·수정 시각만 안다. 그래서 같은 이름이면서 크기와
수정 시각(2초 안)까지 같은 파일을 찾는다(폴더는 이름과 수정 시각). 찾는 순서 — 찾으면 바로 멈춘다:
  1. 그 세션의 프로젝트 폴더(워크트리 포함) — 끌어 오는 파일은 대개 작업 중인 저장소의 것
  2. Spotlight(mdfind) — 빠르지만 색인에서 빠진 폴더(개발 폴더 등)는 못 찾는다
  3. 보드에 기록된 다른 최근 프로젝트 폴더, 데스크탑·다운로드·문서, 그다음 나머지 홈 폴더(시간 한도 안에서)
숨김 폴더(~/.ssh 등)·Library·node_modules·.git·자격증명 같은 이름의 폴더는 건너뛴다. 경로만 돌려주고 파일 내용은 읽지 않는다.
클라우드 동기화 폴더(Dropbox 등)는 목록을 읽을 때 원격에서 받아 오느라 수십 초씩 막혀(실측) 직접 뒤지지 않고 Spotlight 에 맡긴다.
그래도 어딘가에서 막힐 수 있어 홈 폴더 탐색은 따로 도는 스레드에서 — 시간 한도가 지나면 기다리지 않고 찾은 만큼만 돌려준다.
"""
import os
import re
import subprocess
import threading
import time

SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "Library", "Applications", "dist", "build", ".next",
        "site-packages", "Pictures", "Music", "Movies", "anaconda3", "miniconda3", "google-cloud-sdk", "go", "sdk"}
SKIP_RE = re.compile(r"^(dropbox|google ?drive|onedrive|icloud|box)\b|^(keys?|secrets?|credentials?|\.?ssh|\.?aws|\.?gnupg)$", re.I)
HOME = os.path.expanduser("~")
FIRST = ["Desktop", "Downloads", "Documents"]
BUDGET = 4.0   # 홈 폴더를 뒤지는 시간 한도(초)


def _match(path, want):
    try:
        st = os.stat(path)
    except OSError:
        return False
    if want.get("dir"):
        return os.path.isdir(path)
    if not os.path.isfile(path) or st.st_size != int(want.get("size") or 0):
        return False
    m = want.get("mtime")
    return m is None or abs(st.st_mtime * 1000 - float(m)) < 2000


def _mtime_ok(path, want):
    m = want.get("mtime")
    try:
        return m is None or abs(os.stat(path).st_mtime * 1000 - float(m)) < 2000
    except OSError:
        return False


def _walk(root, names, found, deadline):
    """root 아래에서 이름이 같은 파일·폴더를 모은다(found: 이름 → [경로]). 시간이 다 되면 멈춘다."""
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith(".") and d not in SKIP and not SKIP_RE.match(d)]
        for n in names:
            if n in fns or n in dns:
                found.setdefault(n, []).append(os.path.join(dp, n))
        if time.time() > deadline:
            return False
    return True


def _spotlight(name):
    q = 'kMDItemFSName == "%s"' % name.replace("\\", "\\\\").replace('"', '\\"')
    try:
        r = subprocess.run(["mdfind", q], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in r.stdout.splitlines()[:500] if os.path.basename(p) == name]


def _hidden(path):
    """홈 아래 숨김 폴더(~/.ssh·~/.aws 등) 안의 경로는 돌려주지 않는다."""
    rel = os.path.relpath(path, HOME)
    return not rel.startswith("..") and any(part.startswith(".") for part in rel.split(os.sep)[:-1])


def _under(p, roots):
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def locate(files, roots=(), extra=()):
    """files: [{name, size, mtime(ms), dir}] → [{name, path, others}] (path 는 못 찾으면 None, others 는 같은 파일의 다른 위치 수).
    roots: 그 세션의 폴더(먼저 찾고, 같은 파일이 여럿이면 이 안의 것), extra: 다른 최근 프로젝트 폴더."""
    want = [f for f in files if f.get("name") and os.sep not in f["name"]][:20]
    roots = [os.path.realpath(r) for r in roots if r and os.path.isdir(r)]
    more = []
    for r in (os.path.realpath(x) for x in extra if x and os.path.isdir(x)):
        if not _under(r, roots + more):
            more = [m for m in more if not m.startswith(r + os.sep)] + [r]
    hits = {i: [] for i in range(len(want))}

    def take(i, paths):
        for p in paths:
            p = os.path.realpath(p)
            if p not in hits[i] and not _hidden(p) and _match(p, want[i]):
                hits[i].append(p)

    def todo():
        return [i for i in hits if not hits[i]]

    names = {f["name"] for f in want}
    first = time.time() + 2
    for r in roots:   # 1. 세션 폴더
        found = {}
        _walk(r, names, found, first)
        for i in todo():
            take(i, found.get(want[i]["name"], []))
    for i in todo():   # 2. Spotlight
        take(i, _spotlight(want[i]["name"]))
    deadline = time.time() + BUDGET
    tops = more + [os.path.join(HOME, d) for d in FIRST] + sorted(
        os.path.join(HOME, d) for d in os.listdir(HOME)
        if not d.startswith(".") and d not in SKIP and d not in FIRST and not SKIP_RE.match(d))
    found = {}

    def home():   # 3. 홈 폴더
        names3 = {want[i]["name"] for i in todo()}
        for top in tops:
            if not names3 or time.time() > deadline:
                return
            real = os.path.realpath(top)
            if not os.path.isdir(top) or "/Library/CloudStorage" in real or _under(real, roots) or (top not in more and _under(real, more)):
                continue
            part = {}
            _walk(top, names3, part, deadline)
            for n, ps in part.items():
                found.setdefault(n, []).extend(ps)
    if todo():
        th = threading.Thread(target=home, daemon=True)
        th.start()
        th.join(BUDGET + 0.5)
        for i in todo():
            take(i, list(found.get(want[i]["name"], [])))
    out = []
    for i, f in enumerate(want):
        ps = hits[i]
        if f.get("dir") and len(ps) > 1:   # 폴더는 크기가 없어 이름이 같은 게 여럿일 수 있다 — 수정 시각이 맞는 것 먼저
            ps = sorted(ps, key=lambda p: not _mtime_ok(p, f))
        # 같은 파일이 여럿이면(워크트리 사본 등) 세션 폴더 안의 것
        ps = sorted(ps, key=lambda p: not any(p.startswith(r + os.sep) for r in roots))
        out.append({"name": f["name"], "path": ps[0] if ps else None, "others": max(0, len(ps) - 1)})
    return out
