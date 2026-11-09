"""Argus 스킬 관리: 저장소 skills/* 를 Claude Code·Codex 에 설치하거나 뺀다(설치 프로그램과 설정 창 「스킬」).

- Claude Code(~/.claude/skills/<이름>): 저장소 폴더를 가리키는 링크 하나(install.py 와 같은 방식).
- Codex($CODEX_HOME/skills/<이름>, 기본 ~/.codex/skills): 진짜 폴더를 만든다. SKILL.md 는 Codex 용으로 고쳐 쓰고
  (Claude 전용 allowed-tools 줄을 빼고, ~/.claude/skills/<이름> 경로를 Codex 쪽 경로로), 나머지 파일은 저장소 파일로의 링크.
  표시 파일(.argus-skill)이 든 폴더만 Argus 가 만든 것으로 보고 고치거나 지운다 — 같은 이름의 다른 스킬은 건드리지 않는다.
  저장소의 SKILL.md 가 바뀌면 서버가 뜰 때 Codex 쪽 사본을 새로 쓴다(refresh_codex).
- 설치 프로그램은 Codex 에도 스킬을 기본으로 넣는다. 설정 창에서 「제거」한 스킬은 기록해 두고(skills-off.json)
  업데이트 때 다시 넣지 않는다 — 다시 「설치」하면 기록을 지운다.
"""
import json
import os
import re
import shutil

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SRC = os.path.join(REPO, "skills")
MARK = ".argus-skill"
AGENTS = ("claude", "codex")
OFF_PATH = os.path.join(os.path.expanduser(os.environ.get("CSL_DATA_DIR", "~/.claude/session-log")), "skills-off.json")


def opted_out(agent):
    """사용자가 설정 창에서 뺀 스킬 이름들(설치 프로그램이 다시 넣지 않는다)."""
    try:
        with open(OFF_PATH, encoding="utf-8") as f:
            return set(json.load(f).get(agent) or [])
    except (OSError, ValueError, AttributeError):
        return set()


def _set_opt_out(agent, name, off):
    cur = opted_out(agent)
    if (name in cur) == off:
        return
    try:
        with open(OFF_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[agent] = sorted(cur | {name} if off else cur - {name})
    os.makedirs(os.path.dirname(OFF_PATH), exist_ok=True)
    tmp = OFF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, OFF_PATH)


def claude_dir():
    return os.path.expanduser("~/.claude/skills")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def codex_dir():
    return os.path.join(codex_home(), "skills")


def codex_available():
    return os.path.isdir(codex_home()) or bool(shutil.which("codex"))


def _tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def names():
    if not os.path.isdir(SRC):
        return []
    return sorted(n for n in os.listdir(SRC) if os.path.isfile(os.path.join(SRC, n, "SKILL.md")))


def _meta(name):
    """SKILL.md 머리(--- … ---)의 name·description."""
    out = {}
    with open(os.path.join(SRC, name, "SKILL.md"), encoding="utf-8") as f:
        m = re.match(r"---\n(.*?)\n---", f.read(), re.S)
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() in ("name", "description"):
            out[k.strip()] = v.strip()
    return out


def codex_skill_md(name):
    """Codex 에 넣을 SKILL.md — allowed-tools(Claude Code 전용) 줄을 빼고 명령 경로를 Codex 쪽으로."""
    with open(os.path.join(SRC, name, "SKILL.md"), encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"(---\n)(.*?)(\n---)", text, re.S)
    if m:
        head = "\n".join(l for l in m.group(2).splitlines() if not l.startswith("allowed-tools:"))
        text = m.group(1) + head + m.group(3) + text[m.end():]
    return text.replace("~/.claude/skills/%s/" % name, "%s/%s/" % (_tilde(codex_dir()), name))


def _files(name):
    """SKILL.md 말고 링크로 넣을 파일들(숨김 파일 제외)."""
    src = os.path.join(SRC, name)
    return sorted(f for f in os.listdir(src) if f != "SKILL.md" and not f.startswith("."))


def _ours_codex(dst):
    return os.path.isdir(dst) and not os.path.islink(dst) and os.path.isfile(os.path.join(dst, MARK))


def state(name, agent):
    """installed · outdated(Codex 사본이 저장소와 다름) · missing · conflict(같은 이름의 다른 스킬이 있음)."""
    src = os.path.join(SRC, name)
    if agent == "claude":
        dst = os.path.join(claude_dir(), name)
        if os.path.islink(dst):
            return "installed" if os.path.realpath(dst) == os.path.realpath(src) else "conflict"
        return "conflict" if os.path.exists(dst) else "missing"
    dst = os.path.join(codex_dir(), name)
    if not os.path.lexists(dst):
        return "missing"
    if not _ours_codex(dst):
        return "conflict"
    try:
        with open(os.path.join(dst, "SKILL.md"), encoding="utf-8") as f:
            same = f.read() == codex_skill_md(name)
    except OSError:
        same = False
    links_ok = all(os.path.islink(os.path.join(dst, f)) and os.path.realpath(os.path.join(dst, f)) == os.path.realpath(os.path.join(src, f))
                   for f in _files(name))
    return "installed" if same and links_ok else "outdated"


def listing():
    out = []
    for n in names():
        meta = _meta(n)
        out.append({"name": n, "description": meta.get("description", ""), "claude": state(n, "claude"),
                    "codex": state(n, "codex"), "claude_path": _tilde(os.path.join(claude_dir(), n)),
                    "codex_path": _tilde(os.path.join(codex_dir(), n))})
    return out


def install(name, agent):
    """(ok, 메시지)."""
    if name not in names() or agent not in AGENTS:
        return False, "모르는 스킬입니다"
    src = os.path.join(SRC, name)
    st = state(name, agent)
    if st == "conflict":
        return False, "같은 이름의 다른 스킬이 이미 있어 설치하지 않았습니다"
    if agent == "claude":
        dst = os.path.join(claude_dir(), name)
        if st != "installed":
            os.makedirs(claude_dir(), exist_ok=True)
            os.symlink(src, dst)
        return True, "Claude Code 에 설치했습니다 · 새로 여는 세션부터 쓸 수 있습니다"
    _set_opt_out("codex", name, False)
    dst = os.path.join(codex_dir(), name)
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, MARK), "w", encoding="utf-8") as f:
        f.write(src + "\n")
    tmp = os.path.join(dst, ".SKILL.md.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(codex_skill_md(name))
    os.replace(tmp, os.path.join(dst, "SKILL.md"))
    for fname in _files(name):
        link, target = os.path.join(dst, fname), os.path.join(src, fname)
        if os.path.islink(link):
            if os.path.realpath(link) == os.path.realpath(target):
                continue
            os.remove(link)
        elif os.path.exists(link):
            continue   # 사용자가 넣은 진짜 파일은 그대로
        os.symlink(target, link)
    return True, "Codex 에 설치했습니다 · 새로 여는 Codex 세션부터 쓸 수 있습니다"


def remove(name, agent):
    if name not in names() or agent not in AGENTS:
        return False, "모르는 스킬입니다"
    src = os.path.join(SRC, name)
    st = state(name, agent)
    if agent == "codex" and st != "conflict":
        _set_opt_out("codex", name, True)   # 업데이트 때 설치 프로그램이 다시 넣지 않게
    if st == "missing":
        return True, "설치되어 있지 않습니다"
    if st == "conflict":
        return False, "Argus 가 설치한 스킬이 아니라 지우지 않았습니다"
    if agent == "claude":
        os.remove(os.path.join(claude_dir(), name))
        return True, "Claude Code 에서 뺐습니다"
    dst = os.path.join(codex_dir(), name)
    for fname in os.listdir(dst):
        p = os.path.join(dst, fname)
        if fname in ("SKILL.md", MARK) or (os.path.islink(p) and os.path.realpath(p).startswith(os.path.realpath(src) + os.sep)):
            os.remove(p)
    try:
        os.rmdir(dst)
    except OSError:
        return True, "Codex 에서 뺐습니다 · 폴더에 다른 파일이 있어 폴더는 남겼습니다"
    return True, "Codex 에서 뺐습니다"


def refresh_codex():
    """서버가 뜰 때: Codex 에 설치해 둔 스킬의 사본이 저장소와 다르면 새로 쓴다(업데이트를 받은 뒤 등)."""
    if not codex_available():
        return
    for n in names():
        try:
            if state(n, "codex") == "outdated":
                install(n, "codex")
        except OSError:
            pass
