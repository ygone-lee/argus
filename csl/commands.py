"""보드 입력창의 슬래시 명령 자동완성 목록.

- 내장 명령: 설치된 Claude Code 실행 파일에서 명령 정의(type/name/description)를 읽는다.
  버전마다 달라지므로 실행 파일 경로·크기·수정 시각을 열쇠로 결과를 캐시한다.
- 사용자·프로젝트 명령과 스킬, 켜 둔 플러그인의 명령·스킬: 각 폴더의 .md 머리말(description)을 읽는다.
Claude Code 내부 형식을 읽는 것이라 버전에 따라 못 읽을 수 있다. 그때는 빈 목록.
"""
import glob
import json
import mmap
import os
import re
import shutil
import time

from csl import store

CACHE_PATH = os.path.join(store.DATA_DIR, "commands-cache.json")
_HEAD = re.compile(rb'type:"(local|local-jsx|prompt)",name:"([a-z0-9][a-z0-9:_-]*)",')
_DESC = re.compile(rb'(?:get description\(\)\{return"([^"]*)"\}|description:"([^"]*)")')
# 설명이 이름보다 앞에 오는 정의({description:"…",name:"rewind",…,type:"local"})
_HEAD2 = re.compile(rb'\{(?:get )?description(?:\(\)\{return|:)"([^"]*)"\}?,name:"([a-z0-9][a-z0-9:_-]*)",(.{0,400}?)'
                    rb'type:"(local|local-jsx|prompt)"', re.S)
_HINT = re.compile(rb'(?:get argumentHint\(\)\{return"([^"]*)"\}|argumentHint:"([^"]*)")')
_custom_cache = {}
# 설명이 실행 시점에 정해져 실행 파일에서 못 읽거나 잘못 읽히는 명령
_DESC_FIX = {
    "init": "Initialize a new CLAUDE.md file with codebase documentation",
    "remote-control": "Connect this session to claude.ai/code (Remote Control)",
    "login": "Sign in with your Anthropic account",
    "exit": "Exit Claude Code",
    "fast": "Toggle fast mode",
}


def _claude_binary():
    path = shutil.which("claude")
    if path:
        real = os.path.realpath(path)
        if os.path.getsize(real) > 10 * 1024 * 1024:   # 실행 파일 자체(작은 스크립트 래퍼가 아님)
            return real
    versions = sorted(glob.glob(os.path.expanduser("~/.local/share/claude/versions/*")), key=os.path.getmtime)
    return versions[-1] if versions else None


_MEM = {}


def builtin_commands():
    binary = _claude_binary()
    if not binary:
        return []
    st = os.stat(binary)
    key = "%s|%d|%d" % (binary, st.st_size, int(st.st_mtime))
    if _MEM.get("key") == key:   # 같은 실행 파일이면 캐시 파일도 다시 읽지 않는다(입력창에서 / 를 칠 때마다 불린다)
        return _MEM["commands"]
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("key") == key:
            _MEM.update(key=key, commands=cached["commands"])
            return cached["commands"]
    except (OSError, ValueError):
        pass
    cmds = {}
    with open(binary, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        heads = list(_HEAD.finditer(m))
        for i, h in enumerate(heads):
            # 설명·인자 힌트는 다음 명령 정의가 시작되기 전까지에서만 찾는다(옆 명령 것을 가져오지 않게)
            end = min(h.end() + 800, heads[i + 1].start() if i + 1 < len(heads) else len(m))
            seg = m[h.end():end]
            name = h.group(2).decode()
            if name in cmds or b"isHidden:!0" in seg:
                continue
            d, a = _DESC.search(seg), _HINT.search(seg)
            desc = (d.group(1) or d.group(2)).decode("utf-8", "replace") if d else ""
            if desc.startswith(("(removed)", "Renamed to")):
                continue
            cmds[name] = {
                "name": name,
                "desc": _DESC_FIX.get(name, desc),
                "hint": (a.group(1) or a.group(2)).decode("utf-8", "replace") if a else "",
                # local-jsx 는 터미널에 선택 창 등을 띄우는 명령, prompt 는 Claude 에게 요청으로 가는 명령
                "kind": {"local-jsx": "interactive", "prompt": "prompt"}.get(h.group(1).decode(), "builtin"),
            }
        for h in _HEAD2.finditer(m):
            name, seg = h.group(2).decode(), h.group(3)
            if name in cmds or b"isHidden:!0" in seg:
                continue
            a = _HINT.search(seg)
            cmds[name] = {
                "name": name,
                "desc": _DESC_FIX.get(name, h.group(1).decode("utf-8", "replace")),
                "hint": (a.group(1) or a.group(2)).decode("utf-8", "replace") if a else "",
                "kind": {"local-jsx": "interactive", "prompt": "prompt"}.get(h.group(4).decode(), "builtin"),
            }
    out = sorted(cmds.values(), key=lambda c: c["name"])
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"key": key, "commands": out}, f, ensure_ascii=False)
    except OSError:
        pass
    _MEM.update(key=key, commands=out)
    return out


def _front(path):
    """마크다운 머리말에서 description·argument-hint."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(4000).splitlines()
    except OSError:
        return out
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("description", "argument-hint", "name"):
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _scan(base, prefix, kind):
    found = []
    for path in sorted(glob.glob(os.path.join(base, "commands", "**", "*.md"), recursive=True)):
        rel = os.path.relpath(path, os.path.join(base, "commands"))[:-3].replace(os.sep, ":")
        fm = _front(path)
        found.append({"name": prefix + rel, "desc": fm.get("description", ""), "hint": fm.get("argument-hint", ""), "kind": kind})
    for path in sorted(glob.glob(os.path.join(base, "skills", "*", "SKILL.md"))):
        fm = _front(path)
        name = prefix + os.path.basename(os.path.dirname(path))
        found.append({"name": name, "desc": fm.get("description", ""), "hint": fm.get("argument-hint", ""), "kind": "skill"})
    return found


def _enabled_plugins():
    try:
        with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as f:
            enabled = {k for k, v in (json.load(f).get("enabledPlugins") or {}).items() if v}
        with open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"), encoding="utf-8") as f:
            installed = json.load(f).get("plugins") or {}
    except (OSError, ValueError):
        return []
    out = []
    for key, entries in installed.items():
        entry = entries[0] if isinstance(entries, list) and entries else entries
        if key in enabled and isinstance(entry, dict) and entry.get("installPath"):
            out.append((key.split("@", 1)[0], entry["installPath"]))
    return out


def custom_commands(project_root):
    key = project_root or ""
    hit = _custom_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    found = _scan(os.path.expanduser("~/.claude"), "", "command")
    if project_root:
        found += _scan(os.path.join(project_root, ".claude"), "", "command")
    for plugin, path in _enabled_plugins():
        found += _scan(path, plugin + ":", "plugin")
    seen, out = set(), []
    for c in found:
        if c["name"] not in seen:
            seen.add(c["name"])
            out.append(c)
    _custom_cache[key] = (time.time(), out)
    return out


# 실행 파일에서 정의를 못 읽는 내장 명령(목록에 없을 때만 덧붙인다)
_EXTRA = [
    {"name": "btw", "desc": "곁질문 — 지금 맥락으로 묻되 대화에는 넣지 않음. 답은 보드 칸에 따로 보입니다", "hint": "<질문>",
     "kind": "builtin"},
]


def all_commands(project_root):
    names, out = set(), []
    for c in custom_commands(project_root) + builtin_commands() + _EXTRA:
        if c["name"] not in names:
            names.add(c["name"])
            out.append(c)
    return out
