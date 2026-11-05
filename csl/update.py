"""업데이트: Argus 저장소(이 파일이 든 git 체크아웃 — 한 줄 설치의 ~/.argus 도 git clone 이다)를 GitHub 와 비교하고 받는다.

채널(설정 update_channel)
- stable(기본): 릴리스 태그(v0.2.0 …)가 새로 나왔을 때만 알리고, 그 태그까지만 받는다. 개발자가 main 에 올린 커밋이
  사용자에게 바로 가지 않는다 — 태그를 붙여 내보낼 때만.
- latest: 따라가는 원격 브랜치(main)의 모든 커밋.

방식(설정 update_mode)
- notify(기본): 서버가 몇 시간마다 확인해 두고, 보드가 띠·브라우저 알림으로 알린다. 받는 것은 사용자가 누른다.
- auto: 받을 수 있으면 조용한 때(작업 중·확인 대기 세션이 없고, 보드에서 10분 넘게 아무것도 하지 않았을 때) 받아서
  보드 서버를 다시 띄운다. tmux 안의 세션은 그대로라 하던 작업은 끊기지 않는다. 받은 기록은 설정 update_applied 에.
- off: 확인하지 않는다(⋮ → 업데이트 확인은 그대로 된다).

받기는 늘 fast-forward 만. 커밋 안 한 변경·올리지 않은 커밋이 있으면 받지 않는다(개발 중인 체크아웃을 덮어쓰지 않게).
받은 뒤 install.py 를 다시 돌려 새 훅을 맞추고, 보드 서버 재시작은 부르는 쪽이 한다.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")   # 암호를 묻느라 멈추지 않게
CHECK_EVERY = 6 * 3600     # 서버가 원격을 확인하는 간격
QUIET_UI = 600             # 자동 받기: 보드에서 이만큼 아무것도 하지 않았을 때만
_last = {}
_lock = threading.Lock()
LAST_UI = [time.time()]    # 보드에서 마지막으로 무언가 한 시각(server.py 가 POST 마다 고친다)


def _git(*args, timeout=30):
    try:
        r = subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True, timeout=timeout, env=_ENV)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _version_key(tag):
    return [int(x) for x in re.findall(r"\d+", tag)] or [0]


def _gh_repo():
    """origin 이 GitHub 면 'owner/repo'."""
    url = _git("remote", "get-url", "origin")[1]
    m = re.search(r"github\.com[:/]([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$", url)
    return "%s/%s" % m.groups() if m else ""


def _release_notes(tag):
    """GitHub 릴리스 노트(마크다운)와 주소. 릴리스를 만들지 않은 태그거나 네트워크가 안 되면 ("", 태그 주소)."""
    repo = _gh_repo()
    if not repo or not tag:
        return "", ""
    page = "https://github.com/%s/releases/tag/%s" % (repo, tag)
    try:
        req = urllib.request.Request("https://api.github.com/repos/%s/releases/tags/%s" % (repo, tag),
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "argus-board"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.load(r)
        return (d.get("body") or "")[:20000], d.get("html_url") or page
    except Exception:   # noqa: BLE001 - 릴리스가 없거나(404) 오프라인 — 커밋 목록으로 대신한다
        return "", page


def _settings():
    from csl import config, store
    conn = store.connect()
    try:
        return config.get(conn, "update_channel") or "stable", config.get(conn, "update_mode") or "notify"
    finally:
        conn.close()


def check(fetch=True, channel=None):
    """지금 받을 수 있는 것. channel 을 주지 않으면 설정대로."""
    if channel is None:
        channel = _settings()[0]
    out = {"repo": REPO, "checked_at": time.time(), "channel": channel}
    if not os.path.isdir(os.path.join(REPO, ".git")):
        return dict(out, ok=False, error="git 저장소가 아니라 업데이트를 확인할 수 없습니다(한 줄 설치나 git clone 으로 설치한 경우만 됩니다)")
    _, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    rc, upstream, _ = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0 or not upstream:
        return dict(out, ok=False, branch=branch, error="이 브랜치(%s)에는 따라가는 원격 브랜치가 없습니다" % branch)
    if fetch:
        remote = upstream.split("/", 1)[0]
        rc, _, err = _git("fetch", "--quiet", "--tags", "--force", remote, timeout=45)
        if rc != 0:
            return dict(out, ok=False, branch=branch, upstream=upstream,
                        error="GitHub 에서 가져오지 못했습니다: " + (err.splitlines() or ["네트워크를 확인해 주세요"])[-1][:200])
    head = _git("rev-parse", "HEAD")[1]
    tags = sorted([t for t in _git("tag", "--list", "v*")[1].split() if re.fullmatch(r"v\d+(\.\d+)*", t)], key=_version_key)
    current = _git("describe", "--tags", "--abbrev=0")[1] if tags else ""
    latest_tag = tags[-1] if tags else ""
    if channel == "stable":
        # 새 릴리스 = 지금보다 버전이 높고, 지금 위치에서 fast-forward 로 갈 수 있는 태그
        target = latest_tag if latest_tag and _version_key(latest_tag) > _version_key(current or "v0") else ""
        target_ref = target
        label = target
    else:
        target_ref = upstream
        label = upstream
    target_hash = _git("rev-parse", target_ref + "^{commit}")[1] if target_ref else ""
    behind = int(_git("rev-list", "--count", "HEAD.." + target_hash)[1] or 0) if target_hash else 0
    ahead = int(_git("rev-list", "--count", upstream + "..HEAD")[1] or 0)
    ff = bool(target_hash) and _git("merge-base", "--is-ancestor", "HEAD", target_hash)[0] == 0
    log = _git("log", "--format=%h%x09%ct%x09%s", "-30", "HEAD.." + target_hash)[1] if behind else ""
    commits = [{"hash": h, "at": int(t), "subject": s} for h, t, s in (ln.split("\t", 2) for ln in log.splitlines() if ln.count("\t") >= 2)]
    dirty = len([ln for ln in _git("status", "--porcelain", "--untracked-files=no")[1].splitlines() if ln.strip()])
    reason = ("이미 최신입니다" if not behind
              else "커밋하지 않은 변경이 %d개 있습니다" % dirty if dirty
              else "원격에 올리지 않은 로컬 커밋이 %d개 있습니다" % ahead if ahead and channel != "stable"
              else "지금 위치에서 그대로 받을 수 없습니다(갈라진 기록)" if not ff
              else "")
    notes, notes_url = _release_notes(target) if channel == "stable" and target and behind else ("", "")
    res = dict(out, ok=True, branch=branch, upstream=upstream, behind=behind, ahead=ahead, dirty=dirty, commits=commits,
               current_tag=current, latest_tag=latest_tag, new_release=bool(channel == "stable" and target and behind),
               target=target_hash, target_label=label, head=head[:7], can_apply=not reason, reason=reason,
               notes=notes, notes_url=notes_url)
    with _lock:
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
    before = c["head"]
    if c["channel"] == "stable":
        rc, _, err = _git("merge", "--ff-only", "--quiet", c["target"], timeout=90)
    else:
        rc, _, err = _git("pull", "--ff-only", "--quiet", timeout=90)
    if rc != 0:
        return False, "받지 못했습니다: " + (err.splitlines() or [""])[-1][:200], 0
    # 새 훅이 생겼을 수 있다 — 설치 프로그램을 묻지 않고 다시 돌린다(설정 백업·자동 시작 여부는 그대로)
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
    subprocess.run([py, os.path.join(REPO, "install.py"), "--no-open"], capture_output=True, text=True, timeout=120,
                   stdin=subprocess.DEVNULL, env=_ENV)
    _record_applied(before, c)
    label = c["target_label"] if c["channel"] == "stable" else "최신(%s)" % _git("rev-parse", "--short", "HEAD")[1]
    return True, "%s 로 업데이트했습니다(커밋 %d개) — 보드를 다시 띄웁니다" % (label, c["behind"]), c["behind"]


def _record_applied(before, c):
    from csl import store
    conn = store.connect()
    try:
        with conn:
            store.set_setting(conn, "update_applied", {
                "at": time.time(), "from": before, "to": _git("rev-parse", "--short", "HEAD")[1], "label": c["target_label"],
                "channel": c["channel"], "commits": c["commits"][:30], "notes": c.get("notes") or "", "notes_url": c.get("notes_url") or ""})
    finally:
        conn.close()


def summary(conn):
    """보드 상태(/api/state)에 싣는 짧은 요약 — 띠·알림용. 원격은 가져오지 않는다(마지막 확인 결과)."""
    from csl import config, store
    with _lock:
        c = dict(_last)
    channel, mode = config.get(conn, "update_channel") or "stable", config.get(conn, "update_mode") or "notify"
    out = {"mode": mode, "channel": channel, "applied": store.get_setting(conn, "update_applied", None)}
    if c.get("ok") and c.get("behind") and c.get("channel") == channel:
        out.update(available=True, target=c["target"], label=c["target_label"], behind=c["behind"],
                   can_apply=c["can_apply"], reason=c["reason"], new_release=c["new_release"])
    else:
        out["available"] = False
    return out


def _quiet():
    """자동 받기를 해도 되는 때: 작업 중·확인 대기 세션이 없고, 보드에서 한동안 아무것도 하지 않았다."""
    if time.time() - LAST_UI[0] < QUIET_UI:
        return False
    from csl import store
    conn = store.connect()
    try:
        busy = conn.execute("SELECT COUNT(*) FROM sessions WHERE state IN ('working','waiting') AND archived=0"
                            " AND last_activity > ?", (time.time() - 6 * 3600,)).fetchone()[0]
    finally:
        conn.close()
    return not busy


def start_scheduler(restart):
    """서버가 띄운다: 몇 시간마다 확인하고(off 면 쉰다), auto 면 조용한 때 받은 뒤 restart() 로 서버를 다시 띄운다."""
    def loop():
        time.sleep(60)   # 서버가 막 떴을 때 바로 원격을 두드리지 않게
        last_fetch = 0
        while True:
            try:
                channel, mode = _settings()
                if mode != "off":
                    if time.time() - last_fetch > CHECK_EVERY or _last.get("channel") != channel:
                        check(fetch=True, channel=channel)
                        last_fetch = time.time()
                    if mode == "auto" and _last.get("ok") and _last.get("behind") and _last.get("can_apply") and _quiet():
                        ok, _, _ = apply()
                        if ok:
                            restart()
                            return
            except Exception:   # noqa: BLE001 - 확인이 실패해도 서버는 계속
                pass
            time.sleep(300)
    threading.Thread(target=loop, daemon=True, name="update-scheduler").start()
