"""~/.claude/settings.json 에 Argus 훅·상태줄 탭을 넣거나(기본) 뺀다(--uninstall).

    /usr/bin/python3 install.py            # 설치(여러 번 실행해도 한 벌만 유지)
    /usr/bin/python3 install.py --uninstall

다른 훅(Orca 등)과 설정은 그대로 두고 이 도구의 훅 항목만 고친다.
상태줄 탭(csl/statusline_tap.py): 상태줄 입력에서 세션별 컨텍스트 %·사용 한도를 받아 둔다. 상태줄이 없으면 탭을
상태줄 명령으로 두고(화면엔 아무것도 안 나옴), 있으면 원래 명령을 감싼다(같은 입력·출력 그대로). 상태줄 명령이나
그 스크립트에 이미 탭이 들어 있으면 건드리지 않는다. 원래 상태줄 설정은 statusline-original.json 에 두고 제거 때 되돌린다.
바꾸기 전 원본을 ~/.claude/session-log/settings.json.bak-<시각> 으로 남긴다.
스킬(skills/*): ~/.claude/skills/<이름> 에 저장소 폴더를 가리키는 링크를 둔다(모든 세션이 쓴다). 같은 이름의 진짜
폴더가 이미 있으면 건드리지 않는다.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "csl", "hook.py")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
BACKUP_DIR = os.path.expanduser("~/.claude/session-log")
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
TAP = os.path.join(HERE, "csl", "statusline_tap.py")
TAP_MARK = "statusline_tap.py"
ORIGINAL = os.path.join(BACKUP_DIR, "statusline-original.json")
SKILLS_SRC = os.path.join(HERE, "skills")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")
MARK = "/csl/hook.py"   # 저장소 폴더 이름이 바뀌어도(예전 claude-session-log) 이 도구의 훅으로 알아본다

# (이벤트, matcher, async) — 요청 기록은 순서가 중요해 동기, 나머지는 Claude 를 막지 않게 async
HOOKS = [
    ("SessionStart", None, True),
    ("UserPromptSubmit", None, False),
    ("Stop", None, True),
    ("StopFailure", None, True),
    ("Notification", None, True),
    ("PermissionRequest", None, True),
    ("PreToolUse", "AskUserQuestion|ExitPlanMode", True),
    ("PostToolUse", "*", True),
    ("SessionEnd", None, False),
    # 보드가 요청한 모델 전환만 확인 창 없이(출력이 필요해 동기), 전환 결과 기록
    ("PreModelSwitch", None, False),
    ("PostModelSwitch", None, True),
    # 컴팩트 시작·끝(「컴팩트 중」 표시)
    ("PreCompact", None, True),
    ("PostCompact", None, True),
]


def strip_ours(hooks):
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if MARK not in (h.get("command") or "")]
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def _script_has_tap(command):
    """상태줄 명령이 가리키는 스크립트에 이미 탭 줄을 넣어 두었는지(직접 넣은 경우)."""
    try:
        first = os.path.expandvars(os.path.expanduser(shlex.split(command)[0]))
        if os.path.isfile(first) and os.path.getsize(first) < 256 * 1024:
            with open(first, encoding="utf-8", errors="replace") as f:
                return TAP_MARK in f.read()
    except (ValueError, IndexError, OSError):
        pass
    return False


def install_statusline(settings):
    tap = "%s %s" % (PYTHON, shlex.quote(TAP))
    current = settings.get("statusLine")
    command = (current or {}).get("command") or ""
    if TAP_MARK in command and TAP not in command:
        # 저장소를 옮긴 경우: 감싼 명령은 그대로 두고 탭 경로만 새 위치로
        # 따옴표로 감싼 경로(공백 포함)는 따옴표째, 아니면 공백 전까지 — 앞부분만 바꿔 명령이 깨지던 것
        mark = re.escape(TAP_MARK)
        new = re.sub(r"'[^']*%s'|\"[^\"]*%s\"|\S*%s" % (mark, mark, mark), lambda m: shlex.quote(TAP), command, count=1)
        settings["statusLine"] = dict(current, command=new)
        return "상태줄: 탭 경로를 새 위치로 고침"
    if TAP_MARK in command or _script_has_tap(command):
        return "상태줄: 이미 연결됨"
    if not current or current.get("type") != "command" or not command:
        settings["statusLine"] = {"type": "command", "command": tap}
        return "상태줄: 없어서 탭을 상태줄로 지정(표시는 비어 있음)"
    with open(ORIGINAL, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    settings["statusLine"] = dict(current, command="%s --wrap %s" % (tap, shlex.quote(command)))
    return "상태줄: 원래 명령을 감쌈(%s)" % command


def uninstall_statusline(settings):
    command = (settings.get("statusLine") or {}).get("command") or ""
    if TAP_MARK not in command:
        return "상태줄: 그대로(탭이 상태줄 스크립트 안에 있으면 그 줄은 직접 지워 주세요)"
    if " --wrap " in command:
        try:
            with open(ORIGINAL, encoding="utf-8") as f:
                settings["statusLine"] = json.load(f)
            os.remove(ORIGINAL)
        except (OSError, ValueError):
            settings["statusLine"] = dict(settings["statusLine"], command=shlex.split(command)[-1])
        return "상태줄: 원래 명령으로 되돌림"
    del settings["statusLine"]
    return "상태줄: 탭 제거"


def sync_skills(remove=False):
    """저장소의 skills/* 를 ~/.claude/skills 에 링크(제거 때는 이 저장소를 가리키는 링크만 지운다)."""
    done = []
    for name in sorted(os.listdir(SKILLS_SRC)) if os.path.isdir(SKILLS_SRC) else []:
        src, dst = os.path.join(SKILLS_SRC, name), os.path.join(SKILLS_DIR, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            continue
        if os.path.islink(dst):
            if remove and os.path.realpath(dst) != os.path.realpath(src):
                continue   # 제거할 땐 이 저장소를 가리키는 링크만 지운다(같은 이름의 다른 스킬 링크는 그대로)
            os.remove(dst)   # 저장소를 옮겼으면 옛 링크를 새로
        elif os.path.exists(dst):
            done.append(name + "(같은 이름 폴더가 있어 건너뜀)")
            continue
        if not remove:
            os.makedirs(SKILLS_DIR, exist_ok=True)
            os.symlink(src, dst)
        done.append(name)
    return "스킬 %s: %s" % ("링크 제거" if remove else "링크", ", ".join(done) or "없음")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    with open(SETTINGS, encoding="utf-8") as f:
        settings = json.load(f)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, "settings.json.bak-%d" % int(time.time()))
    shutil.copy2(SETTINGS, backup)

    hooks = settings.setdefault("hooks", {})
    strip_ours(hooks)
    if not args.uninstall:
        command = "%s %s" % (shlex.quote(PYTHON), shlex.quote(HOOK))   # 경로에 공백이 있어도 되게
        for event, matcher, is_async in HOOKS:
            entry = {"type": "command", "command": command, "timeout": 5}
            if is_async:
                entry["async"] = True
            group = {"hooks": [entry]}
            if matcher:
                group = {"matcher": matcher, "hooks": [entry]}
            hooks.setdefault(event, []).append(group)
    if not hooks:
        del settings["hooks"]
    status = uninstall_statusline(settings) if args.uninstall else install_statusline(settings)
    status += ". " + sync_skills(remove=args.uninstall)

    tmp = SETTINGS + ".csl-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)
    print("%s 완료. %s. 원본 백업: %s" % ("제거" if args.uninstall else "설치", status, backup))


if __name__ == "__main__":
    main()
