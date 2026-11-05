"""업데이트 확인·받기: Argus 저장소(이 파일이 든 git 체크아웃)를 원격(origin)과 비교하고, 받을 수 있으면 fast-forward 로 받는다.

- check(fetch): 원격을 가져와(git fetch) 받을 커밋 수·목록, 최신 태그(릴리스), 받을 수 있는지와 못 받는 이유
- apply(): 받을 수 있을 때만 git pull --ff-only, 그다음 install.py 를 다시 돌려 훅을 맞춘다(새 훅이 생겼을 수 있다).
  보드 서버 재시작은 부르는 쪽(server.py)이 응답을 보낸 뒤 한다.
받지 않는 경우: git 저장소가 아님 · 브랜치가 원격 추적 브랜치가 없음 · 커밋 안 한 변경이 있음 · 올리지 않은 로컬 커밋이 있음 —
개발 중인 체크아웃을 덮어쓰지 않게(받기는 늘 fast-forward 만).
"""
import os
import re
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")   # 암호를 묻느라 멈추지 않게
_last = {}


def _git(*args, timeout=30):
    try:
        r = subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True, timeout=timeout, env=_ENV)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _version_key(tag):
    return [int(x) for x in re.findall(r"\d+", tag)] or [0]


def check(fetch=True):
    out = {"repo": REPO, "checked_at": time.time()}
    if not os.path.isdir(os.path.join(REPO, ".git")):
        return dict(out, ok=False, error="git 저장소가 아니라 업데이트를 확인할 수 없습니다(git clone 으로 설치한 경우만 됩니다)")
    _, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    rc, upstream, _ = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0 or not upstream:
        return dict(out, ok=False, branch=branch, error="이 브랜치(%s)에는 따라가는 원격 브랜치가 없습니다" % branch)
    if fetch:
        remote = upstream.split("/", 1)[0]
        rc, _, err = _git("fetch", "--quiet", "--tags", remote, timeout=45)
        if rc != 0:
            return dict(out, ok=False, branch=branch, upstream=upstream,
                        error="원격을 가져오지 못했습니다: " + (err.splitlines() or ["네트워크를 확인해 주세요"])[-1][:200])
    behind = int(_git("rev-list", "--count", "HEAD.." + upstream)[1] or 0)
    ahead = int(_git("rev-list", "--count", upstream + "..HEAD")[1] or 0)
    log = _git("log", "--format=%h%x09%ct%x09%s", "-30", "HEAD.." + upstream)[1]
    commits = [{"hash": h, "at": int(t), "subject": s} for h, t, s in (ln.split("\t", 2) for ln in log.splitlines() if ln.count("\t") >= 2)]
    dirty = len([ln for ln in _git("status", "--porcelain", "--untracked-files=no")[1].splitlines() if ln.strip()])
    tags = sorted(_git("tag", "--list")[1].split(), key=_version_key)
    current = _git("describe", "--tags", "--abbrev=0")[1] if tags else ""
    latest = tags[-1] if tags else ""
    reason = ("커밋하지 않은 변경이 %d개 있습니다" % dirty if dirty
              else "원격에 올리지 않은 로컬 커밋이 %d개 있습니다" % ahead if ahead
              else "" if behind else "이미 최신입니다")
    res = dict(out, ok=True, branch=branch, upstream=upstream, behind=behind, ahead=ahead, dirty=dirty, commits=commits,
               current_tag=current, latest_tag=latest, new_release=bool(latest and latest != current and _version_key(latest) > _version_key(current or "0")),
               head=_git("rev-parse", "--short", "HEAD")[1], can_apply=not reason, reason=reason)
    _last.clear()
    _last.update(res)
    return res


def apply():
    """(성공 여부, 안내, 받은 커밋 수). 받을 수 없으면 그 이유."""
    c = check(fetch=True)
    if not c.get("ok"):
        return False, c.get("error") or "확인하지 못했습니다", 0
    if not c["can_apply"]:
        return False, c["reason"], 0
    rc, _, err = _git("pull", "--ff-only", "--quiet", timeout=90)
    if rc != 0:
        return False, "받지 못했습니다: " + (err.splitlines() or [""])[-1][:200], 0
    # 새 훅이 생겼을 수 있다 — 설치 프로그램을 묻지 않고 다시 돌린다(설정 백업·자동 시작 여부는 그대로)
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
    subprocess.run([py, os.path.join(REPO, "install.py"), "--no-open"], capture_output=True, text=True, timeout=120,
                   stdin=subprocess.DEVNULL, env=_ENV)
    return True, "커밋 %d개를 받았습니다 — 보드를 다시 띄웁니다" % c["behind"], c["behind"]
