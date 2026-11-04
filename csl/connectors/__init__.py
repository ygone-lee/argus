"""Argus 커넥터: 바깥 서비스(슬랙·Gmail …)에서 나에게 온 것들을 모아 브레인 인박스로 넘기는 플러그인 층.

플러그인 넣기: 이 폴더에 파일 하나(예: jira.py)를 두고 Connector 를 상속한 클래스를 CONNECTOR 로 내보내면
「커넥터 추가」 목록에 나온다. 필요한 것:

    class Jira(Connector):
        type = "jira"; name = "Jira"; icon = "🧩"; description = "나에게 배정된 이슈"
        fields = [Field("token", "API 토큰", "secret"), Field("jql", "JQL", "text", default="assignee = currentUser()")]
        def poll(self, ctx): ...   # ctx.since 이후 새 항목을 Item 목록으로. ctx.secret("token")·ctx.state(dict, 저장됨)
        def test(self, ctx): ...   # 연결 확인 — 계정 이름 같은 짧은 문구를 돌려준다(틀리면 예외)
    CONNECTOR = Jira

- 같은 커넥터를 여러 개 둘 수 있다(슬랙 워크스페이스마다 하나씩). 설정은 settings 의 connectors 목록에 둔다.
- secret 필드는 macOS 키체인에만 넣는다(항목 이름 argus-connector-<인스턴스>). 이미 넣어 둔 키체인 항목 이름을 대신 적어도 된다.
- 모은 항목은 feed_items 표에 잠깐 두고, 브레인이 카드로 만들면 done 으로 바꾸고 본문을 지운다.
"""
import importlib
import pkgutil
import shutil
import re
import secrets as _secrets
import subprocess
import time

from csl import store

KEEP = 7 * 86400   # 처리한 항목의 키(중복 방지)·오래된 행 보관


def gws_json(args, lists=False):
    """Google Workspace CLI(gws)를 실행해 JSON 결과를 돌려준다(Gmail·캘린더 커넥터 공용). 앞에 붙는 안내 글은 건너뛴다.
    lists=True 면 결과가 목록([…])이어도 받는다."""
    import json
    r = subprocess.run([shutil.which("gws") or "/opt/homebrew/bin/gws"] + args, capture_output=True, text=True, timeout=60)
    out = r.stdout
    start = out.find("{") if "{" in out or not lists else out.find("[")
    if r.returncode != 0 or start < 0:
        raise RuntimeError("gws 실패: " + (r.stderr or out).strip()[:200] + " — `gws auth login` 이 필요할 수 있습니다")
    return json.loads(out[start:])


class Field:
    """설정 창에 그릴 칸. kind: text · secret · int · bool · textarea"""
    def __init__(self, key, label, kind="text", default="", help="", required=True, prefix=""):
        self.key, self.label, self.kind, self.default, self.help, self.required = key, label, kind, default, help, required
        self.prefix = prefix   # secret: 맞는 값의 앞머리(예: xoxp-). 붙여 넣으면 화면이 바로 저장·연결 확인한다

    def describe(self):
        return {"key": self.key, "label": self.label, "kind": self.kind, "default": self.default, "help": self.help,
                "required": self.required, "prefix": self.prefix}


class Item:
    """인박스로 넘길 한 건. key 는 같은 것을 두 번 넣지 않게 하는 고유값(서비스 안에서)."""
    def __init__(self, key, ts, title, author="", text="", link="", kind="", account=""):
        self.key, self.ts, self.title, self.author, self.text, self.link, self.kind, self.account = \
            key, ts, title, author, text, link, kind, account


class Connector:
    type = ""
    name = ""
    icon = "🔌"
    description = ""
    fields = []
    default_every = 2   # 분
    role = "inbox"      # inbox: 모아서 브레인 인박스로 · info: 모으지 않고 브레인이 필요할 때 query 로 조회만
    # 추가 창 위에 그릴 빠른 연결 안내: {"button", "url"(키를 받으러 갈 곳), "steps": [...], "links": [[글, 주소], ...]}
    setup = None

    def poll(self, ctx):
        raise NotImplementedError

    def test(self, ctx):
        return "ok"


class Ctx:
    """커넥터 한 인스턴스가 돌 때 쓰는 것들."""
    def __init__(self, inst, state):
        self.inst, self.config, self._state = inst, inst.get("config") or {}, state

    @property
    def state(self):
        return self._state

    def get(self, key, default=None):
        v = self.config.get(key)
        return default if v in (None, "") else v

    def secret(self, key):
        service = (self.inst.get("secrets") or {}).get(key)
        return keychain_get(service) if service else ""

    @property
    def since(self):
        return float(self._state.get("last") or 0)


# ---------------------------------------------------------------- 키체인
_SVC = re.compile(r"^[\w.\-]{1,120}$")


def keychain_get(service):
    if not service or not _SVC.match(service):
        return ""
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""


def keychain_set(service, value):
    """값은 명령줄이 아니라 표준 입력(security -i)으로 넘겨 프로세스 목록에 드러나지 않게 한다."""
    if not _SVC.match(service) or not value or any(c in value for c in '"\\\n\r'):
        raise ValueError("넣을 수 없는 값입니다")
    r = subprocess.run(["security", "-i"], input='add-generic-password -U -a argus -s "%s" -w "%s"\n' % (service, value),
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError("키체인에 넣지 못했습니다")


def keychain_delete(service):
    if service and _SVC.match(service):
        subprocess.run(["security", "delete-generic-password", "-s", service], capture_output=True, timeout=10)


# ---------------------------------------------------------------- 등록
def registry():
    """이 폴더의 플러그인들 {type: 클래스}."""
    out = {}
    for m in pkgutil.iter_modules(__path__):
        try:
            mod = importlib.import_module(__name__ + "." + m.name)
        except Exception:  # noqa: BLE001 - 깨진 플러그인 하나가 나머지를 막지 않게
            continue
        cls = getattr(mod, "CONNECTOR", None)
        if cls and cls.type:
            out[cls.type] = cls
    return out


def instances(conn):
    return store.get_setting(conn, "connectors", []) or []


def save_instances(conn, items):
    store.set_setting(conn, "connectors", items)


def _states(conn):
    return store.get_setting(conn, "connector_state", {}) or {}


def new_id(kind):
    return "%s-%s" % (kind, _secrets.token_hex(3))


def upsert(conn, data):
    """인스턴스 추가·수정. secret 필드는 키체인으로 보내고 설정에는 항목 이름만 남긴다."""
    reg = registry()
    cls = reg.get(data.get("type"))
    if not cls:
        raise ValueError("모르는 커넥터입니다")
    items = instances(conn)
    inst = next((i for i in items if i["id"] == data.get("id")), None)
    if inst is None:
        inst = {"id": new_id(cls.type), "type": cls.type, "enabled": True, "config": {}, "secrets": {},
                "every": cls.default_every, "created_at": time.time()}
        items.append(inst)
    inst["name"] = (data.get("name") or inst.get("name") or cls.name).strip()[:60]
    if "enabled" in data:
        inst["enabled"] = bool(data["enabled"])
    if "every" in data:
        inst["every"] = max(0, min(int(data["every"] or 0), 1440))
    cfg = data.get("config") or {}
    for f in cls.fields:
        if f.key not in cfg:
            continue
        v = cfg[f.key]
        if f.kind == "secret":
            v = str(v or "").strip()
            if not v:
                continue   # 비워 두면 그대로
            if v.startswith("keychain:"):   # 이미 넣어 둔 키체인 항목을 쓴다
                inst["secrets"][f.key] = v[len("keychain:"):].strip()
            else:
                service = "argus-connector-%s-%s" % (inst["id"], f.key)
                keychain_set(service, v)
                inst["secrets"][f.key] = service
        elif f.kind == "int":
            inst["config"][f.key] = int(v or 0)
        elif f.kind == "bool":
            inst["config"][f.key] = bool(v)
        else:
            inst["config"][f.key] = str(v or "")[:4000]
    missing = [f.label for f in cls.fields if f.required and
               (not inst["secrets"].get(f.key) if f.kind == "secret" else inst["config"].get(f.key) in (None, ""))]
    save_instances(conn, items)
    return inst, missing


def remove(conn, iid):
    items = instances(conn)
    inst = next((i for i in items if i["id"] == iid), None)
    if not inst:
        return False
    for svc in (inst.get("secrets") or {}).values():
        if svc.startswith("argus-connector-"):   # Argus 가 만든 키체인 항목만 지운다(직접 넣은 항목은 둔다)
            keychain_delete(svc)
    save_instances(conn, [i for i in items if i["id"] != iid])
    states = _states(conn)
    states.pop(iid, None)
    store.set_setting(conn, "connector_state", states)
    return True


# ---------------------------------------------------------------- 수집
def _poll_one(conn, inst, cls):
    states = _states(conn)
    st = states.get(inst["id"]) or {}
    ctx = Ctx(inst, st)
    items = cls().poll(ctx) or []
    rows, newest = [], max(ctx.since, float(st.get("seen") or 0))   # seen: 걸러 낸 것까지 포함해 커넥터가 본 마지막 시각
    for it in items:
        newest = max(newest, float(it.ts or 0))
        rows.append((inst["id"] + ":" + it.key, inst["id"], cls.type, it.account or st.get("account") or "", it.kind or "",
                     (it.title or "")[:300], (it.author or "")[:200], store.mask(it.text or "")[:6000], (it.link or "")[:800],
                     float(it.ts or time.time()), time.time()))
    with conn:
        conn.executemany("INSERT OR IGNORE INTO feed_items (key, instance, source, account, kind, title, author, text, link, ts,"
                         " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        st.update(last=newest, polled_at=time.time(), error=None, added=len(rows))
        states = _states(conn)
        states[inst["id"]] = st
        store.set_setting(conn, "connector_state", states)
    return len(rows)


def poll_now(conn, iid):
    inst = next((i for i in instances(conn) if i["id"] == iid), None)
    cls = inst and registry().get(inst["type"])
    if not cls:
        raise ValueError("없는 커넥터입니다")
    return _poll_one(conn, inst, cls)


def test(conn, iid):
    inst = next((i for i in instances(conn) if i["id"] == iid), None)
    cls = inst and registry().get(inst["type"])
    if not cls:
        raise ValueError("없는 커넥터입니다")
    st = _states(conn).get(iid) or {}
    msg = cls().test(Ctx(inst, st))
    with conn:
        states = _states(conn)
        states.setdefault(iid, {}).update(st, account=msg, error=None)
        store.set_setting(conn, "connector_state", states)
        if st.get("team") and (inst.get("name") or cls.name) == cls.name:   # 이름을 안 바꿨으면 워크스페이스 이름으로
            items = instances(conn)
            for i in items:
                if i["id"] == iid:
                    i["name"] = str(st["team"])[:60]
            save_instances(conn, items)
    return msg


def tick(force=False):
    """서버 스케줄러가 1분마다 부른다. 인스턴스마다 정한 간격이 지났을 때만 모은다."""
    conn = store.connect()
    try:
        with conn:
            conn.execute("DELETE FROM feed_items WHERE created_at < ?", (time.time() - KEEP,))
        reg = registry()
        polled = {k: (v or {}).get("polled_at") for k, v in _states(conn).items()}   # 한 번만 읽는다(각자 자기 것만 바꾼다)
        for inst in instances(conn):
            cls = reg.get(inst["type"])
            every = int(inst.get("every") or 0)
            if not cls or cls.role != "inbox" or not inst.get("enabled", True) or (not every and not force):
                continue
            last = float(polled.get(inst["id"]) or 0)
            if not force and time.time() - last < every * 60:
                continue
            try:
                _poll_one(conn, inst, cls)
            except Exception as e:  # noqa: BLE001 - 한 커넥터가 실패해도 나머지는 모은다
                with conn:
                    states = _states(conn)
                    states.setdefault(inst["id"], {}).update(error=str(e)[:300], polled_at=time.time())
                    store.set_setting(conn, "connector_state", states)
    finally:
        conn.close()


def query(conn, kind, *args):
    """정보 전용 커넥터(켜 둔 것 전부)에 묻는다. [(인스턴스 이름, 결과 목록)]"""
    reg, out, states = registry(), [], _states(conn)
    for inst in instances(conn):
        cls = reg.get(inst["type"])
        if cls and cls.type == kind and cls.role == "info" and inst.get("enabled", True):
            st = states.get(inst["id"]) or {}
            out.append((inst.get("name"), cls().query(Ctx(inst, st), *args)))
    return out


def followups(conn, channel, ts, thread_ts=None, include_self=False):
    """슬랙 메시지 뒤에 이어진 대화. 켜 둔 슬랙 커넥터를 차례로 물어 그 대화가 있는 워크스페이스의 결과.

    (인스턴스 이름, 메시지 목록) 또는 (None, 오류 문구).
    """
    reg, last_err, states = registry(), "슬랙 커넥터가 없습니다", _states(conn)
    for inst in instances(conn):
        cls = reg.get(inst["type"])
        if not cls or not hasattr(cls, "followups") or not inst.get("enabled", True):
            continue
        st = states.get(inst["id"]) or {}
        try:
            return inst.get("name"), cls().followups(Ctx(inst, st), channel, ts, thread_ts, include_self=include_self)
        except Exception as e:  # noqa: BLE001 - 다른 워크스페이스의 대화면 channel_not_found — 다음 커넥터로
            last_err = str(e)[:200]
    return None, last_err


def describe(conn):
    """커넥터 창이 그릴 것: 넣을 수 있는 종류(필드)와 지금 인스턴스(상태 포함). 비밀값은 내보내지 않는다."""
    reg, states = registry(), _states(conn)
    pend = {r[0]: r[1] for r in conn.execute("SELECT instance, COUNT(*) FROM feed_items WHERE done=0 GROUP BY instance")}
    types = [{"type": c.type, "name": c.name, "icon": c.icon, "description": c.description, "every": c.default_every,
              "role": c.role, "setup": c.setup, "fields": [f.describe() for f in c.fields]} for c in reg.values()]
    out = []
    for i in instances(conn):
        cls, st = reg.get(i["type"]), states.get(i["id"]) or {}
        out.append({"id": i["id"], "type": i["type"], "name": i.get("name"), "enabled": i.get("enabled", True),
                    "every": i.get("every"), "config": i.get("config") or {},
                    "secrets": {k: ("keychain:" + v if not v.startswith("argus-connector-") else "저장됨")
                                for k, v in (i.get("secrets") or {}).items()},
                    "icon": cls.icon if cls else "❔", "type_name": cls.name if cls else i["type"], "missing": not cls,
                    "role": cls.role if cls else "inbox",
                    "account": st.get("account") or st.get("team"), "polled_at": st.get("polled_at"), "last": st.get("last"),
                    "error": st.get("error"), "pending": pend.get(i["id"], 0)})
    return {"types": types, "instances": out}
