"""보드 터미널의 브라우저 연결: 웹소켓 하나 = `tmux attach` 하나를 PTY 로 띄워 바이트를 그대로 잇는다.

보안(로컬 셸을 브라우저로 다루는 기능이라 겹겹이 막는다):
- 서버는 127.0.0.1 에만 열려 있고, Host 가 127.0.0.1·localhost 가 아니면 거절한다(server._host_ok — DNS 리바인딩).
- 웹소켓은 같은 출처만: Origin 이 http://127.0.0.1:포트 · http://localhost:포트 가 아니면 거절한다(다른 사이트의 접속).
- 서버를 띄울 때마다 새로 만드는 토큰(TOKEN)이 맞아야 한다. 보드 화면이 같은 출처 API 로 받아 간다.
- 붙는 대상은 보드 터미널(tmux -L argus-web)의 그 세션 창뿐이다 — 아무 명령이나 실행하는 길은 없다.
메시지: 브라우저 → 서버  바이너리 = 키 입력 그대로, 글자 {"resize": [열, 줄]}
        서버 → 브라우저  바이너리 = 화면 출력 그대로
"""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import select
import signal
import struct
import termios
import threading

TOKEN = secrets.token_urlsafe(24)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 1 << 20


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def token_ok(got):
    return bool(got) and secrets.compare_digest(str(got), TOKEN)


class WebSocket:
    """서버 쪽 웹소켓(RFC 6455) — 보드 터미널에 필요한 만큼만: 마스크 풀기·조각 잇기·ping/pong·close."""

    def __init__(self, rfile, wfile):
        self.rfile, self.wfile, self.lock, self.closed = rfile, wfile, threading.Lock(), False

    def _read(self, n):
        buf = self.rfile.read(n)
        if not buf or len(buf) < n:
            raise EOFError
        return buf

    def send(self, data, opcode=2):
        if self.closed:
            return
        n = len(data)
        head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                                         else bytes([127]) + struct.pack(">Q", n))
        with self.lock:
            try:
                self.wfile.write(head + data)
                self.wfile.flush()
            except OSError:
                self.closed = True

    def close(self):
        if not self.closed:
            self.send(b"", 8)
            self.closed = True

    def recv(self):
        """(opcode, 데이터) — 1 글자 · 2 바이너리. 끊기면 None."""
        parts, first = [], None
        try:
            while True:
                b0, b1 = self._read(2)
                op, n = b0 & 0x0F, b1 & 0x7F
                if not b1 & 0x80:
                    return None   # 브라우저가 보내는 프레임은 꼭 가려져(mask) 있다
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                if n > MAX_FRAME:
                    return None
                mask = self._read(4)
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(self._read(n))) if n else b""
                if op == 8:
                    self.close()
                    return None
                if op == 9:
                    self.send(data, 10)
                    continue
                if op == 10:
                    continue
                if op in (1, 2):
                    first, parts = op, [data]
                elif op == 0 and first:
                    parts.append(data)
                if b0 & 0x80 and first:
                    return first, b"".join(parts)
        except (EOFError, OSError, ValueError):
            return None


def _winsize(fd, cols, rows):
    cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 300))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def relay(ws, argv, cols=120, rows=36):
    """argv(tmux attach …)를 PTY 에서 띄우고 웹소켓과 잇는다. 어느 쪽이든 끝나면 다른 쪽도 닫는다."""
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", LANG=os.environ.get("LANG") or "en_US.UTF-8")
    env.pop("TMUX", None)   # 보드 서버가 tmux 안에서 떴어도 「안에서 또 attach」로 막히지 않게
    pid, fd = os.forkpty()
    if pid == 0:   # 자식: 바로 exec(포크한 뒤 다른 일을 하지 않는다)
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    try:
        _winsize(fd, cols, rows)
    except (OSError, ValueError):
        pass

    def pump():   # 화면 출력 → 브라우저
        try:
            while not ws.closed:
                r, _, _ = select.select([fd], [], [], 1.0)
                if not r:
                    continue
                data = os.read(fd, 65536)
                if not data:
                    break
                ws.send(data)
        except OSError:
            pass
        ws.close()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        while True:
            msg = ws.recv()
            if msg is None:
                break
            op, data = msg
            if op == 2:
                os.write(fd, data)
            else:
                try:
                    m = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(m, dict) and isinstance(m.get("resize"), list) and len(m["resize"]) == 2:
                    try:
                        _winsize(fd, *m["resize"])
                    except (OSError, ValueError, TypeError):
                        pass
    except OSError:
        pass
    finally:
        ws.close()
        try:
            os.kill(pid, signal.SIGHUP)   # attach 만 끝낸다 — tmux 안의 세션(Claude Code)은 그대로 산다
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        t.join(timeout=2)   # 출력 쪽이 끝난 뒤에 닫는다(읽는 중에 닫으면 번호가 다른 파일로 다시 쓰일 수 있다)
        os.close(fd)
