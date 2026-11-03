"""놓친 git 작업 리포트: 커밋 안 한 변경, 푸시 안 한 커밋, PR 없는 브랜치, 머지 안 된 PR.

브레인(`./argus git`)과 하루 정리가 쓴다. 배포는 보지 않는다(머지되면 다음 배포에 딸려 나간다).

- 대상: 최근 세션이 있었던 저장소(project_root)와 그 저장소의 모든 워크트리(`git worktree list`).
- 원격 정보는 `git fetch` 한 번(저장소마다, 20초 제한)으로 맞춘다. 실패하면 있는 정보로 계속한다.
- PR 은 GitHub CLI(`gh pr list --author @me`)로 저장소마다 한 번 묻는다. 원격 호스트(예: GitHub Enterprise 서버)를
  GH_HOST 로 넘긴다. gh 가 없거나 로그인돼 있지 않으면 PR 부분만 빠진다.
- 읽기만 한다. 커밋·푸시·PR 생성은 하지 않는다.
"""
import json
import os
import re
import shutil
import subprocess
import time

from csl import store

CACHE_SECONDS = 300
STALE_DAYS = 14   # 이보다 오래 손대지 않은 워크트리는 리포트 끝에 한 줄로만
_cache = {"at": 0, "data": None}


def _git(cwd, *args, timeout=10):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _gh():
    return shutil.which("gh") or ("/opt/homebrew/bin/gh" if os.path.exists("/opt/homebrew/bin/gh") else None)


def _repos(conn, days=14):
    """최근 days 일 안에 세션이 있었던 저장소 루트."""
    roots = set()
    for (root,) in conn.execute("SELECT DISTINCT project_root FROM sessions WHERE last_activity >= ? AND project_root IS NOT NULL",
                                (time.time() - days * 86400,)):
        top = _git(root, "rev-parse", "--show-toplevel") if root and os.path.isdir(root) else None
        if top:
            roots.add(top.strip())
    return sorted(roots)


def _worktrees(root):
    out, cur = [], {}
    for line in (_git(root, "worktree", "list", "--porcelain") or "").splitlines() + [""]:
        if not line:
            if cur.get("path") and os.path.isdir(cur["path"]):
                out.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[9:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "detached":
            cur["branch"] = None
    return out


def _default_branch(root):
    ref = (_git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD") or "").strip()
    return ref.replace("refs/remotes/origin/", "") or "main"


def _remote_host_repo(root):
    url = (_git(root, "remote", "get-url", "origin") or "").strip()
    m = re.search(r"(?:https?://|git@)([^/:]+)[/:]([^/]+/[^/]+?)(?:\.git)?$", url)
    return (m.group(1), m.group(2)) if m else (None, None)


def _prs(root, state):
    """PR 목록. 원격이 없으면 [](PR 이 있을 수 없음), gh 를 못 쓰면 None."""
    gh = _gh()
    host, repo = _remote_host_repo(root)
    if not repo:
        return []
    if not gh:
        return None
    env = dict(os.environ, GH_HOST=host, GH_PROMPT_DISABLED="1")
    fields = "number,title,headRefName,url,isDraft,reviewDecision,updatedAt,createdAt"
    try:
        r = subprocess.run([gh, "pr", "list", "--repo", "%s/%s" % (host, repo), "--author", "@me", "--state", state,
                            "--limit", "50", "--json", fields], capture_output=True, text=True, timeout=25, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "[]")
    except ValueError:
        return None


def collect(conn, fetch=True):
    """저장소마다 {root, name, worktrees: [...], open_prs, error}."""
    repos = []
    for root in _repos(conn):
        if fetch:
            _git(root, "fetch", "--quiet", "--prune", "origin", timeout=20)
        default = _default_branch(root)
        base = "origin/" + default
        open_prs = _prs(root, "open")
        merged = {p["headRefName"] for p in (_prs(root, "merged") or [])}
        pr_heads = {p["headRefName"] for p in (open_prs or [])}
        wts = []
        for wt in _worktrees(root):
            path, branch = wt["path"], wt.get("branch")
            status = _git(path, "status", "--porcelain=v1") or ""
            lines = [ln for ln in status.splitlines() if ln.strip()]
            untracked = sum(1 for ln in lines if ln.startswith("??"))
            changed = len(lines) - untracked
            upstream = (_git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "").strip() or None
            ahead = behind = None
            if upstream:
                lr = (_git(path, "rev-list", "--left-right", "--count", "@{u}...HEAD") or "").split()
                if len(lr) == 2:
                    behind, ahead = int(lr[0]), int(lr[1])
            not_in_main = int((_git(path, "rev-list", "--count", base + "..HEAD") or "0").strip() or 0)
            # 마지막으로 손댄 때: HEAD 커밋 시각과 바뀐 파일들의 수정 시각 중 늦은 것
            touched = float((_git(path, "log", "-1", "--format=%ct") or "0").strip() or 0)
            for ln in lines[:200]:
                f = os.path.join(path, ln[3:].split(" -> ")[-1].strip('"'))
                try:
                    touched = max(touched, os.path.getmtime(f))
                except OSError:
                    pass
            item = {"path": path, "branch": branch, "changed": changed, "untracked": untracked, "touched": touched,
                    "upstream": upstream, "ahead": ahead, "behind": behind, "not_in_main": not_in_main,
                    "is_default": branch == default, "has_pr": branch in pr_heads, "merged": branch in merged}
            # 할 일이 있는 것만: 변경이 있거나, 안 올린 커밋이 있거나, main 에 안 들어간 브랜치인데 PR 이 없거나
            item["unpushed"] = (ahead or 0) if upstream else (not_in_main if not item["is_default"] else 0)
            item["no_pr"] = (not item["is_default"] and upstream and not item["has_pr"] and not item["merged"]
                             and not_in_main > 0 and not (ahead or 0))
            if changed or untracked or item["unpushed"] or item["no_pr"]:
                wts.append(item)
        repos.append({"root": root, "name": os.path.basename(root), "default": default, "worktrees": wts,
                      "open_prs": open_prs, "pr_error": open_prs is None})
    return repos


def report(conn, fetch=True, refresh=False):
    """캐시(5분)된 수집 결과."""
    if refresh or not _cache["data"] or time.time() - _cache["at"] > CACHE_SECONDS:
        _cache.update(at=time.time(), data=collect(conn, fetch))
    return {"at": _cache["at"], "repos": _cache["data"]}


def _where(repo, wt):
    """저장소 이름 · 워크트리 폴더 이름 (브랜치) — 저장소 밖 워크트리도 경로 대신 폴더 이름만."""
    path = wt["path"]
    sub = "" if path == repo["root"] else " · " + os.path.basename(path.rstrip("/"))
    return repo["name"] + sub + " (" + (wt["branch"] or "detached") + ")"


def _ago(ts):
    try:
        t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, TypeError):
        return ""
    d = time.time() - t
    return "%d시간 전" % (d // 3600) if d < 86400 else "%d일 전" % (d // 86400)


REVIEW = {"APPROVED": "승인됨", "CHANGES_REQUESTED": "변경 요청", "REVIEW_REQUIRED": "리뷰 대기"}


def text(data):
    """사람이 읽는 리포트(브레인·하루 정리용)."""
    out = {"dirty": [], "unpushed": [], "no_pr": [], "prs": []}
    notes, stale = [], []
    for repo in data["repos"]:
        for wt in sorted(repo["worktrees"], key=lambda w: -w.get("touched", 0)):
            if time.time() - wt.get("touched", 0) > STALE_DAYS * 86400:
                stale.append(_where(repo, wt))
                continue
            if wt["changed"] or wt["untracked"]:
                out["dirty"].append("- %s — 수정 %d · 새 파일 %d" % (_where(repo, wt), wt["changed"], wt["untracked"]))
            if wt["unpushed"]:
                why = "원격 브랜치 없음" if not wt["upstream"] else "원격보다 %d커밋 앞섬" % wt["ahead"]
                out["unpushed"].append("- %s — 푸시 안 한 커밋 %d개(%s)" % (_where(repo, wt), wt["unpushed"], why))
            if wt["no_pr"]:
                out["no_pr"].append("- %s — %s 에 없는 커밋 %d개, PR 없음" % (_where(repo, wt), repo["default"], wt["not_in_main"]))
        for p in repo["open_prs"] or []:
            st = "초안" if p.get("isDraft") else REVIEW.get(p.get("reviewDecision") or "", "리뷰 전")
            out["prs"].append("- %s #%d %s — %s · 갱신 %s · %s" % (repo["name"], p["number"], p["title"], st,
                                                                 _ago(p.get("updatedAt")), p["url"]))
        if repo["pr_error"]:
            notes.append("%s: PR 목록을 못 읽음(gh 로그인 확인)" % repo["name"])
    sec = [("커밋 안 한 변경", out["dirty"]), ("푸시 안 한 커밋", out["unpushed"]),
           ("푸시했지만 PR 없는 브랜치", out["no_pr"]), ("머지 안 된 PR(내가 연 것)", out["prs"])]
    lines = ["# 놓친 git 작업 (%s 기준, 배포는 보지 않음)" % time.strftime("%m/%d %H:%M", time.localtime(data["at"]))]
    for title, rows in sec:
        lines.append("\n## %s (%d)" % (title, len(rows)))
        lines += rows or ["- 없음"]
    if stale:
        lines.append("\n## %d일 넘게 손대지 않은 워크트리 %d곳(변경·커밋이 남아 있음 — 버렸으면 정리)" % (STALE_DAYS, len(stale)))
        lines.append("- " + ", ".join(stale))
    if notes:
        lines.append("\n" + " · ".join(notes))
    return store.mask("\n".join(lines))
