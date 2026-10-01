"""Test helpers, stdlib and pytest only: a running md-editor server, raw HTTP requests, and a
WebSocket client that can also send malformed frames."""
import base64
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"
FAKES = TESTS / "fakes"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MARKER = "MDEDIT_TEST_RUN"   # set in each server's environment, so its leftovers can be found


def _linux_tools():
    """Linux with util-linux script(1): fakes/claude runs under it as claude-sandbox's wrapper
    does (BSD script, as on macOS, takes other options), fakes/fake-sandbox uses GNU stat,
    and the terminal tests drive bash and read /proc."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        out = subprocess.run(["script", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "util-linux" in out.stdout + out.stderr


LINUX_TOOLS = _linux_tools()
LINUX_REASON = "needs Linux with util-linux script(1)"
needs_linux = pytest.mark.skipif(not LINUX_TOOLS, reason=LINUX_REASON)


# ---------------------------------------------------------------- processes

def _proc_level():
    """How deep our PID namespace is below the one /proc belongs to: 0, unless a sandbox
    mounted an outer namespace's /proc, whose numbers then aren't our PIDs."""
    try:
        with open("/proc/self/status") as f:
            return next(len(ln.split()) - 2 for ln in f if ln.startswith("NSpid:"))
    except (OSError, StopIteration):
        return 0


PROC_LEVEL = _proc_level()


def _proc_status(entry):
    """(PID as we see it, state letter) of /proc/<entry>; the PID is None for a process
    outside our namespace."""
    pid, state = int(entry), None
    with open(f"/proc/{entry}/status") as f:
        for ln in f:
            if ln.startswith("NSpid:"):
                ids = ln.split()[1:]
                pid = int(ids[PROC_LEVEL]) if len(ids) > PROC_LEVEL else None
            elif ln.startswith("State:"):
                state = ln.split()[1]
    return pid, state


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if PROC_LEVEL == 0:  # a zombie is as good as gone
        try:
            return _proc_status(pid)[1] != "Z"
        except OSError:
            pass
    return True


def gone_within(pids, timeout=5):
    deadline = time.monotonic() + timeout
    while any(alive(p) for p in pids):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def marked_pids(tag):
    """Live processes whose environment carries MARKER=tag (Linux only; [] elsewhere)."""
    needle = f"{MARKER}={tag}".encode()
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/environ", "rb") as f:
                if needle not in f.read().split(b"\0"):
                    continue
            pid, state = _proc_status(name)
        except OSError:
            continue
        if pid is not None and pid != os.getpid() and state != "Z":
            out.append(pid)
    return out


# ---------------------------------------------------------------- server

class Server:
    """`python -m md_collab_editor ROOT --port 0 ...` in its own process group. Fake claude
    state (call logs, flag files) lives in `state`."""

    def __init__(self, root, workdir, args=(), env=None, agent="none", ask_agent="none"):
        self.root = Path(root)
        self.state = Path(workdir) / "fake-state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.logpath = Path(workdir) / "server.log"
        self.tag = os.urandom(8).hex()
        e = {k: v for k, v in os.environ.items() if not k.startswith("MDEDIT_")}
        # started as if from inside a Claude Code session, whose markers must not reach the agents
        e.update({MARKER: self.tag, "FAKE_CLAUDE_STATE": str(self.state), "CLAUDECODE": "1",
                  "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION_ID": "parent-session",
                  "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))})
        e.update(env or {})
        argv = [sys.executable, "-m", "md_collab_editor", str(root), "--no-browser", "--port", "0"]
        if agent is not None:
            argv += ["--agent", agent]
        if ask_agent is not None:
            argv += ["--ask-agent", ask_agent]
        self.log = open(self.logpath, "wb")
        self.proc = subprocess.Popen(argv + list(args), stdout=self.log, stderr=subprocess.STDOUT, env=e,
                                     cwd=str(workdir), start_new_session=True)
        deadline = time.monotonic() + 30
        while True:
            m = re.search(r"MD editor on http://127\.0\.0\.1:(\d+)/", self.logtext())
            if m:
                self.port = int(m.group(1))
                break
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError(f"md-editor did not start:\n{self.logtext()[-3000:]}")
            time.sleep(0.05)
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.base = self.origin + "/"

    def logtext(self):
        if not self.log.closed:
            self.log.flush()
        return self.logpath.read_bytes().decode(errors="replace")

    # -- HTTP

    def http(self, method, path, body=None, headers=None, timeout=60):
        """One request with exactly `headers` (a list of pairs; repeats allowed, no Host added).
        Returns (status, JSON body or raw bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in headers or []:
                conn.putheader(k, v)
            data = json.dumps(body).encode() if body is not None else b""
            if body is not None:
                conn.putheader("Content-Type", "application/json")
            if body is not None or method in ("POST", "PUT"):
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data or None)
            r = conn.getresponse()
            raw = r.read()
        finally:
            conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def request(self, method, path, body=None, host=None, origin="same", timeout=60):
        """A request as the editor's own page makes it: Host, plus a matching Origin on
        POST / PUT (origin='same'), or the given Origin, or none (origin=None)."""
        host = self.host if host is None else host
        headers = [("Host", host)]
        if origin == "same":
            origin = f"http://{host}" if method in ("POST", "PUT") else None
        if origin is not None:
            headers.append(("Origin", origin))
        return self.http(method, path, body, headers, timeout)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body, **kw):
        return self.request("POST", path, body, **kw)

    def put(self, path, body, **kw):
        return self.request("PUT", path, body, **kw)

    def config(self):
        status, conf = self.get("/api/config")
        assert status == 200, conf
        return conf

    def ws(self, **kw):
        return WS(self.port, **kw)

    # -- fake claude

    def flag(self, name, on=True):
        p = self.state / name
        if on:
            p.touch()
        elif p.exists():
            p.unlink()

    def _jsonl(self, name):
        p = self.state / name
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []

    def calls(self):
        """Every fake `claude` call: its digest plus argv, prompt, base, fork, is_error."""
        return self._jsonl("claude.log")

    def bases(self):
        return [c for c in self.calls() if c["base"]]

    def sandbox_calls(self):
        return self._jsonl("sandbox.log")

    # -- shutdown

    def stop(self, sig=signal.SIGTERM):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, sig)
            except OSError:
                pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                self.proc.wait(5)
        if not self.log.closed:
            self.log.close()

    def leftovers(self, timeout=5):
        """Processes the server started that are still alive `timeout` s after it stopped;
        they are killed."""
        deadline = time.monotonic() + timeout
        while True:
            left = marked_pids(self.tag)
            if not left or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return left


# ---------------------------------------------------------------- websocket client

def mask_bytes(data, key):
    n = len(data)
    if not n:
        return b""
    m = int.from_bytes((key * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ m).to_bytes(n, "big")


class Frame:
    def __init__(self, fin, op, payload, masked, lenclass):
        self.fin, self.op, self.payload, self.masked, self.lenclass = fin, op, payload, masked, lenclass

    def json(self):
        return json.loads(self.payload.decode()) if self.op == 1 else None

    def __repr__(self):
        return f"<Frame op={self.op} len={len(self.payload)} {self.payload[:60]!r}>"


class WS:
    """A WebSocket client for /api/term with a reader thread that keeps every frame. It can
    send any frame (unmasked, fragmented, with reserved bits or a false length)."""

    def __init__(self, port, host=None, origin="same", headers=None, pipeline=b"", path="/api/term", pong=True):
        host = host or f"127.0.0.1:{port}"
        self.pong = pong            # answer pings, as browsers do
        if origin == "same":
            origin = f"http://{host}"
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.want_accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        h = {"Host": host, "Upgrade": "websocket", "Connection": "keep-alive, Upgrade",
             "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13", "Origin": origin}
        h.update(headers or {})
        req = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items() if v is not None)
        self.sock.sendall(req.encode() + b"\r\n" + pipeline)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status_line = lines[0]
        parts = self.status_line.split()
        self.code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        self.headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                self.headers[k.strip().lower()] = v.strip()
        self.buf = bytearray(rest)
        self.frames = []
        self.outbuf = bytearray()   # every binary payload, concatenated
        self.cond = threading.Condition()
        self.eof = False
        self.bad = []               # server protocol violations (masked or non-minimal lengths)
        self.send_lock = threading.Lock()
        self.body = b""
        if self.code == 101:
            self.sock.settimeout(None)
            threading.Thread(target=self._run, daemon=True).start()
        else:
            self.sock.settimeout(5)
            body = bytes(self.buf)
            try:
                while True:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        break
                    body += chunk
            except OSError:
                pass
            self.body = body
            self.eof = True
            self.sock.close()

    # -- receiving

    def _parse(self):
        b = self.buf
        if len(b) < 2:
            return None
        b0, b1 = b[0], b[1]
        n, off, cls = b1 & 0x7F, 2, 7
        if n == 126:
            if len(b) < 4:
                return None
            n, off, cls = struct.unpack("!H", bytes(b[2:4]))[0], 4, 16
        elif n == 127:
            if len(b) < 10:
                return None
            n, off, cls = struct.unpack("!Q", bytes(b[2:10]))[0], 10, 64
        masked = bool(b1 & 0x80)
        if masked:
            off += 4
        if len(b) < off + n:
            return None
        payload = bytes(b[off:off + n])
        del b[:off + n]
        if masked:
            self.bad.append("server frame masked")
        if (cls == 16 and n < 126) or (cls == 64 and n < 65536):
            self.bad.append(f"non-minimal length encoding {cls} for {n}")
        return Frame(bool(b0 & 0x80), b0 & 0x0F, payload, masked, cls)

    def _run(self):
        try:
            while True:
                while True:
                    f = self._parse()
                    if f is None:
                        break
                    with self.cond:
                        self.frames.append(f)
                        if f.op == 2:
                            self.outbuf += f.payload
                        self.cond.notify_all()
                    if f.op == 9 and self.pong:
                        try:
                            self.send_frame(10, f.payload)
                        except OSError:
                            pass
                chunk = self.sock.recv(1 << 20)
                if not chunk:
                    break
                self.buf += chunk
        except OSError:
            pass
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def wait(self, pred, timeout=10):
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                r = pred()
                if r:
                    return r
                left = deadline - time.monotonic()
                if left <= 0:
                    return r
                self.cond.wait(min(left, 0.5))

    def statuses(self):
        return [f.json() for f in self.frames if f.op == 1 and f.json().get("type") == "status"]

    def wait_status(self, pred=lambda s: True, timeout=10):
        def find():
            return next((s for s in self.statuses() if pred(s)), None)
        return self.wait(find, timeout)

    def wait_out(self, needle, since=0, timeout=10):
        if isinstance(needle, str):
            needle = needle.encode()
        return self.wait(lambda: needle in self.outbuf[since:], timeout)

    def wait_re(self, pattern, since=0, timeout=10):
        rx = re.compile(pattern)
        return self.wait(lambda: rx.search(bytes(self.outbuf[since:])), timeout)

    def wait_eof(self, timeout=5):
        return self.wait(lambda: self.eof, timeout)

    def close_frames(self):
        return [f for f in self.frames if f.op == 8]

    def mark(self):
        return len(self.outbuf)

    # -- sending

    def send_frame(self, op, payload=b"", fin=True, mask=True, rsv=0, length=None):
        n = len(payload) if length is None else length
        b0 = (0x80 if fin else 0) | rsv | op
        mb = 0x80 if mask else 0
        if n < 126:
            h = struct.pack("!BB", b0, mb | n)
        elif n < 65536:
            h = struct.pack("!BBH", b0, mb | 126, n)
        else:
            h = struct.pack("!BBQ", b0, mb | 127, n)
        if mask:
            key = os.urandom(4)
            h += key
            payload = mask_bytes(payload, key)
        with self.send_lock:
            self.sock.sendall(h + payload)

    def send_bin(self, data):
        self.send_frame(2, data)

    def send_json(self, obj):
        self.send_frame(1, json.dumps(obj).encode())

    def resize(self, cols, rows):
        self.send_json({"type": "resize", "cols": cols, "rows": rows})

    def cmd(self, line):
        self.send_bin((line + "\r").encode())

    def close(self, code=1000):
        if not self.eof:
            try:
                self.send_frame(8, struct.pack("!H", code))
            except OSError:
                pass
            self.wait_eof(3)
        self.abort()

    def abort(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
