"""The IDE link: md-editor speaking Claude Code's IDE protocol (MCP over a WebSocket) to the
terminal's session. A fake agent stands in for Claude Code's process, and a WebSocket client
replays the frames Claude Code 2.1.280 was recorded sending (initialize, ide_connected,
tools/list, openDiff, close_tab, closeAllDiffTabs, getDiagnostics) against md-editor's bridge,
in native mode (TCP, the agent is `claude`) and in launcher mode (a Unix socket, the agent is
claude-sandbox). Linux only, as the terminal tests."""
import json
import os
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from helpers import WS, needs_linux

from md_collab_editor import server as S

pytestmark = needs_linux

# Stands in for the agent: reports its arguments and CLAUDE_CODE_SSE_PORT, then appends every
# byte typed into it (raw) to $FAKE_AGENT_LOG, except SAY + text, which it draws (as Claude Code
# draws its prompt or a menu).
FAKE_AGENT = """#!{python}
import json, os, sys, tty
tty.setraw(0)
info = {{"argv": sys.argv[1:], "port": os.environ.get("CLAUDE_CODE_SSE_PORT")}}
os.write(1, ("READY " + json.dumps(info) + "\\r\\n").encode())
while True:
    data = os.read(0, 65536)
    if not data:
        break
    if data.startswith(b"\\x01SAY "):
        os.write(1, data[5:])
        continue
    with open(os.environ["FAKE_AGENT_LOG"], "ab") as f:
        f.write(data)
"""
INPUT_BOX = "\u276f\u00a0\x1b[2mTry something\x1b[22m\r\n"     # Claude Code's prompt, ready for input
MENU = "\u276f\x1b[4G\x1b[38;2;153;153;153m1. \x1b[38;2;177;185;249mYes\r\n   2. No\r\n"   # a permission prompt

TAB = "✻ [Claude Code] target.md (6d7411) ⧉"
DOC = "line one\nhello world\nline three\n"
# as Claude Code 2.1.280 sent them (protocol notes, t1.log to t4.log)
INITIALIZE = {"method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {"roots": {"listChanged": True}, "elicitation": {}},
    "clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "2.1.280",
                   "description": "Anthropic's agentic coding tool", "websiteUrl": "https://claude.com/claude-code"}},
    "jsonrpc": "2.0", "id": 0}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
IDE_CONNECTED = {"jsonrpc": "2.0", "method": "ide_connected", "params": {"pid": 1230101}}
TOOLS_LIST = {"method": "tools/list", "jsonrpc": "2.0", "id": 1}
CLAUDE_HEADERS = {"Sec-WebSocket-Protocol": "mcp", "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
                  "User-Agent": "claude-code/2.1.280 (cli)"}


def tool_call(rid, name, arguments):
    return {"method": "tools/call", "params": {"name": name, "arguments": arguments, "_meta": {"progressToken": rid}},
            "jsonrpc": "2.0", "id": rid}


def open_diff(rid, path, contents, tab=TAB):
    return tool_call(rid, "openDiff", {"old_file_path": str(path), "new_file_path": str(path),
                                       "new_file_contents": contents, "tab_name": tab})


def texts(reply):
    return [c["text"] for c in reply["result"]["content"]]


class Claude:
    """Claude Code's side of the link: connects as it does (its headers and the lock's token)
    and answers md-editor's pings, as the MCP SDK does, after `ping_delay` seconds."""

    def __init__(self, lock, unix=None, token=None, headers=None):
        self.lock_path = Path(lock)
        self.lock = json.loads(self.lock_path.read_text())
        self.port = int(self.lock_path.stem)
        h = dict(CLAUDE_HEADERS, **{"X-Claude-Code-Ide-Authorization": self.lock["authToken"] if token is None
                                    else token})
        h.update(headers or {})
        self.ws = WS(self.port, origin=None, path="/", headers=h, unix=unix)
        self.code = self.ws.code
        self.ping_delay, self.pinged = 0, []   # when md-editor's pings came, and were answered
        if self.code == 101:
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        seen = 0
        while True:
            self.ws.wait(lambda: len(self.ws.frames) > seen or self.ws.eof, 1)
            frames = self.ws.frames[seen:]
            seen += len(frames)
            for f in frames:
                m = f.json()
                if m and m.get("method") == "ping" and "id" in m:
                    time.sleep(self.ping_delay)
                    self.pinged.append(time.monotonic())
                    self.send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
            if self.ws.eof and seen == len(self.ws.frames):
                return

    def send(self, msg):
        self.ws.send_json(msg)

    def msgs(self):
        return [f.json() for f in self.ws.frames if f.op == 1]

    def wait(self, pred, timeout=10):
        return self.ws.wait(lambda: next((m for m in self.msgs() if pred(m)), None), timeout)

    def call(self, msg, timeout=10):
        """Send a request and wait for md-editor's answer."""
        self.send(msg)
        return self.reply(msg["id"], timeout)

    def reply(self, rid, timeout=10):
        return self.wait(lambda m: m.get("id") == rid and "method" not in m, timeout)

    def notes(self, method):
        return [m["params"] for m in self.msgs() if m.get("method") == method]

    def handshake(self):
        init = self.call(INITIALIZE)
        self.send(INITIALIZED)
        self.send(IDE_CONNECTED)
        return init, self.call(TOOLS_LIST)

    def close(self):
        self.ws.close()


# ---------------------------------------------------------------- set-up

@pytest.fixture
def agent_log(tmp_path):
    return tmp_path / "typed.log"


def fake_agent(tmp_path, name):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(FAKE_AGENT.format(python=sys.executable))
    p.chmod(0o755)
    return str(p)


def start(md_editor, tmp_path, agent_log, mode="native", env=None, **kw):
    """md-editor with the fake agent: native (called claude, its lock in tmp/cfg/ide) or
    launcher (its lock in tmp/shared/.claude/ide)."""
    e = {"FAKE_AGENT_LOG": str(agent_log), "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"),
         "CLAUDE_SANDBOX_SHARED_CONFIG": str(tmp_path / "shared")}
    if mode == "launcher":
        (tmp_path / "shared" / ".claude").mkdir(parents=True, exist_ok=True)
    e.update(env or {})
    agent = fake_agent(tmp_path, "claude" if mode == "native" else "claude-sandbox")
    return md_editor(agent=agent, env=e, **kw)


def lock_dir(tmp_path, mode="native"):
    return tmp_path / "cfg" / "ide" if mode == "native" else tmp_path / "shared" / ".claude" / "ide"


def say(c, text):
    """Make the fake agent draw `text` (as Claude Code draws its prompt, or a menu)."""
    mark = c.mark()
    c.send_bin(b"\x01SAY " + text.encode())
    assert c.wait_out(text.encode(), since=mark), bytes(c.outbuf)[-300:]


def session(srv, prompt=True):
    """A browser on /api/term with the session started; returns (client, status, agent's report).
    With `prompt`, the agent then draws Claude Code's input box, so asks need not wait for it."""
    c = srv.ws()
    assert c.code == 101, c.status_line
    c.wait_status()
    c.resize(100, 30)
    st = c.wait_status(lambda s: s["state"] == "running")
    m = c.wait_re(rb"READY (\{.*?\})\r\n")
    assert m, bytes(c.outbuf)[-500:]
    if prompt:
        say(c, INPUT_BOX)
    return c, st, json.loads(m.group(1))


def the_lock(d):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        locks = sorted(d.glob("*.lock")) if d.is_dir() else []
        if locks:
            assert len(locks) == 1, locks
            return locks[0]
        time.sleep(0.05)
    raise AssertionError(f"no lock file in {d}")


def connected(srv, tmp_path, agent_log, mode="native", **kw):
    """md-editor, a browser on its terminal, and Claude connected and past the handshake."""
    srv = start(srv, tmp_path, agent_log, mode, **kw)
    term, st, report = session(srv)
    lock = the_lock(lock_dir(tmp_path, mode))
    unix = str(srv.root / f".md-editor-ide-{lock.stem}.sock") if mode == "launcher" else None
    claude = Claude(lock, unix=unix)
    assert claude.code == 101, claude.ws.status_line
    claude.handshake()
    assert term.wait_status(lambda s: s["ide"] == "connected")
    return srv, term, claude


def browser_msgs(c, kind):
    return [f.json() for f in c.frames if f.op == 1 and f.json().get("type") == kind]


def wait_browser(c, kind, pred=lambda m: True, timeout=10):
    return c.wait(lambda: next((m for m in browser_msgs(c, kind) if pred(m)), None), timeout)


def typed(agent_log, want, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = agent_log.read_bytes() if agent_log.exists() else b""
        if got == want:
            return got
        time.sleep(0.05)
    return agent_log.read_bytes() if agent_log.exists() else b""


# ---------------------------------------------------------------- native mode: handshake and lock

def test_native_lock_handshake_and_tools(md_editor, tmp_path, agent_log, root):
    srv = start(md_editor, tmp_path, agent_log)
    term, st, report = session(srv)
    try:
        assert st["ide"] == "waiting" and st["ide_reason"] is None and st["diffs"] == []
        lock = the_lock(lock_dir(tmp_path))
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        data = json.loads(lock.read_text())
        token = data.pop("authToken")
        assert re.fullmatch(r"[0-9a-f]{64}", token)
        assert data == {"pid": srv.proc.pid, "workspaceFolders": [str(root)], "ideName": "md-editor",
                        "transport": "ws", "mdEditorHostPid": srv.proc.pid}
        assert report == {"argv": [], "port": lock.stem}, "natively the port is in the agent's environment"
        assert token not in srv.logtext()

        claude = Claude(lock)
        assert claude.code == 101
        assert claude.ws.headers.get("sec-websocket-protocol") == "mcp"
        assert "sec-websocket-extensions" not in claude.ws.headers, "permessage-deflate is refused"
        init, tools = claude.handshake()
        r = init["result"]
        assert r["protocolVersion"] == "2025-11-25" and r["serverInfo"]["name"] == "md-editor"
        assert "md-editor" in r["instructions"] and "British English" in r["instructions"]
        names = [t["name"] for t in tools["result"]["tools"]]
        assert sorted(names) == ["closeAllDiffTabs", "close_tab", "getDiagnostics", "openDiff"]
        assert "executeCode" not in json.dumps(tools)
        assert term.wait_status(lambda s: s["ide"] == "connected")

        assert texts(claude.call(tool_call(2, "closeAllDiffTabs", {}))) == ["TAB_CLOSED"]
        uri = (root / "a.md").as_uri()
        assert texts(claude.call(tool_call(3, "getDiagnostics", {"uri": uri}))) == ["[]"]
        assert texts(claude.call(tool_call(4, "getDiagnostics", {}))) == ["[]"]
        assert claude.call({"jsonrpc": "2.0", "id": 5, "method": "ping"})["result"] == {}
        err = claude.call({"jsonrpc": "2.0", "id": 6, "method": "getOpenEditors", "params": {}})["error"]
        assert err["code"] == -32601
        assert claude.call(tool_call(7, "executeCode", {"code": "1"}))["error"]["code"] == -32602
        claude.send({"jsonrpc": "2.0", "method": "log_event", "params": {}})   # ignored
        assert claude.call({"jsonrpc": "2.0", "id": 8, "method": "ping"})["result"] == {}
        assert "unknown method 'getOpenEditors'" in srv.logtext()

        claude.close()
        assert term.wait_status(lambda s: s["ide"] == "waiting"), "it may connect again (/ide)"
        again = Claude(lock)
        again.handshake()
        assert term.wait_status(lambda s: s["ide"] == "connected", 5)
        again.close()
    finally:
        term.close()
    srv.stop()
    assert not lock.exists(), "the lock file goes when md-editor stops"


def test_the_token_is_required_and_nothing_else_is_served(md_editor, tmp_path, agent_log):
    srv = start(md_editor, tmp_path, agent_log)
    term, _, _ = session(srv)
    lock = the_lock(lock_dir(tmp_path))
    token = json.loads(lock.read_text())["authToken"]
    port = int(lock.stem)
    for bad in ("", "x" * 64, token[:-1], token + "0", token.upper()):
        c = Claude(lock, token=bad)
        assert c.code == 401, (bad, c.ws.status_line)
    # the token twice (one header, then another with the same name) is refused
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
               f"X-Claude-Code-Ide-Authorization: {token}\r\nX-Claude-Code-Ide-Authorization: {token}\r\n\r\n").encode())
    assert s.recv(100).startswith(b"HTTP/1.0 401")
    s.close()
    # an Origin changes nothing: a browser could not send the token's header anyway
    c = Claude(lock, headers={"Origin": "http://evil.example"})
    assert c.code == 101
    c.close()
    assert Claude(lock, token="nope", headers={"Origin": f"http://127.0.0.1:{port}"}).code == 401
    # none of md-editor's API, or any other path or method, on the IDE port
    for method, path in (("GET", "/api/config"), ("GET", "/api/term"), ("POST", "/api/root"), ("GET", "/x"),
                         ("PUT", "/"), ("POST", "/")):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall((f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 0\r\n"
                   f"X-Claude-Code-Ide-Authorization: {token}\r\n\r\n").encode())
        head = s.recv(200)
        s.close()
        assert re.match(rb"HTTP/1\.[01] 404", head), (method, path, head)
    # the token, but no upgrade
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET / HTTP/1.1\r\nHost: x\r\nX-Claude-Code-Ide-Authorization: {token}\r\n\r\n".encode())
    assert s.recv(100).startswith(b"HTTP/1.0 400")
    s.close()
    assert "refused a connection" in srv.logtext() and token not in srv.logtext()
    term.close()


def test_messages_over_16_mib_and_bad_json(md_editor, tmp_path, agent_log):
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    claude.ws.send_frame(1, b"{not json")
    assert claude.wait(lambda m: m.get("error", {}).get("code") == -32700)
    claude.ws.send_frame(1, b"[1, 2]")
    assert claude.wait(lambda m: m.get("error", {}).get("code") == -32600)
    big = S.IDE_MAX_MESSAGE + 1
    claude.ws.send_frame(1, b"", length=big)   # a header claiming more than the cap: no payload is read
    assert claude.ws.wait(lambda: claude.ws.close_frames(), 5)
    assert struct.unpack("!H", claude.ws.close_frames()[0].payload[:2])[0] == 1009
    assert term.wait_status(lambda s: s["ide"] == "waiting")
    term.close()


# ---------------------------------------------------------------- openDiff

def test_open_diff_accept_with_edits_is_sent_back_and_not_written(md_editor, tmp_path, agent_log, root):
    (root / "target.md").write_text(DOC)
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    claude.call(tool_call(2, "closeAllDiffTabs", {}))
    proposal = "line one\ngoodbye world\nline three\n"
    claude.send(open_diff(3, root / "target.md", proposal))
    d = wait_browser(term, "diff")
    assert d == {"type": "diff", "id": d["id"], "path": "target.md", "file": str(root / "target.md"), "title": TAB,
                 "old": DOC, "new": proposal, "exists": True}
    # numbered per md-editor process: a page that outlives one never takes a new proposal for an old one
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9]+", d["id"]), d["id"]
    assert claude.reply(3, 0.5) is None, "Claude waits for the user"
    # a page that is reloaded (or another browser) gets it again, and the status lists it
    other = srv.ws()
    assert other.wait_status()["diffs"] == [d["id"]]
    assert wait_browser(other, "diff") == d
    # the first answer wins; md-editor never writes the file (Claude does)
    edited = proposal + "EDITED-IN-IDE\n"
    term.send_json({"type": "diff-decision", "id": d["id"], "accept": True, "contents": edited})
    assert texts(claude.reply(3)) == ["FILE_SAVED", edited]
    assert wait_browser(term, "diff-close", lambda m: m["id"] == d["id"])
    assert wait_browser(other, "diff-close", lambda m: m["id"] == d["id"])
    other.send_json({"type": "diff-decision", "id": d["id"], "accept": False})
    time.sleep(0.3)
    assert len([m for m in claude.msgs() if m.get("id") == 3]) == 1
    assert (root / "target.md").read_text() == DOC
    # Claude then closes the tab twice and asks for diagnostics, as 2.1.280 does
    assert texts(claude.call(tool_call(4, "close_tab", {"tab_name": TAB}))) == ["TAB_CLOSED"]
    assert texts(claude.call(tool_call(5, "close_tab", {"tab_name": TAB}))) == ["TAB_CLOSED"]
    assert texts(claude.call(tool_call(6, "getDiagnostics", {"uri": (root / "target.md").as_uri()}))) == ["[]"]
    # a CRLF file: Claude Code 2.1.280 reads it as LF and proposes LF text, and an edited
    # proposal must go back as LF too (it diffs the answer against its LF copy, and puts the
    # file's CRLFs back when it writes): CRLFs would make every line a change
    (root / "dos.md").write_bytes(b"a\r\nx\r\n")
    claude.send(open_diff(7, root / "dos.md", "a\nb\n", "dos"))
    d2 = wait_browser(term, "diff", lambda m: m["title"] == "dos")
    assert d2["old"] == "a\nx\n" and d2["new"] == "a\nb\n", "the editor's LF text"
    term.send_json({"type": "diff-decision", "id": d2["id"], "accept": True, "contents": "a\nc\nd\n"})
    assert texts(claude.reply(7)) == ["FILE_SAVED", "a\nc\nd\n"], "edited: the proposal's LFs"
    # a proposal with CRLFs: accepted unchanged, exactly as proposed; edited, with its CRLFs
    crlf = "a\r\nb\r\n"
    claude.send(open_diff(8, root / "dos.md", crlf, "dos2"))
    d3 = wait_browser(term, "diff", lambda m: m["title"] == "dos2")
    term.send_json({"type": "diff-decision", "id": d3["id"], "accept": True, "contents": "a\nb\n"})
    assert texts(claude.reply(8)) == ["FILE_SAVED", crlf]
    claude.send(open_diff(9, root / "dos.md", crlf, "dos3"))
    d3 = wait_browser(term, "diff", lambda m: m["title"] == "dos3")
    term.send_json({"type": "diff-decision", "id": d3["id"], "accept": True, "contents": "a\nc\n"})
    assert texts(claude.reply(9)) == ["FILE_SAVED", "a\r\nc\r\n"], "edited: back to the proposal's CRLFs"
    # a new file (and one in a new folder): said to be new
    claude.send(open_diff(10, root / "new.md", "# New\n", "new"))
    d4 = wait_browser(term, "diff", lambda m: m["title"] == "new")
    assert d4["old"] == "" and d4["path"] == "new.md" and d4["exists"] is False
    term.send_json({"type": "diff-decision", "id": d4["id"], "accept": True})
    assert texts(claude.reply(10)) == ["FILE_SAVED", "# New\n"]
    assert not (root / "new.md").exists()
    claude.send(open_diff(11, root / "sub" / "new.md", "# New\n", "new2"))
    d5 = wait_browser(term, "diff", lambda m: m["title"] == "new2")
    assert d5["old"] == "" and d5["exists"] is False
    term.send_json({"type": "diff-decision", "id": d5["id"], "accept": False})
    assert texts(claude.reply(11)) == ["DIFF_REJECTED", "new2"]
    other.close()
    term.close()


def test_open_diff_reject_and_close_tab_while_waiting(md_editor, tmp_path, agent_log, root):
    (root / "target.md").write_text(DOC)
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    claude.send(open_diff(3, root / "target.md", DOC + "EDITED-IN-IDE\n"))
    d = wait_browser(term, "diff")
    term.send_json({"type": "diff-decision", "id": d["id"], "accept": False})
    assert texts(claude.reply(3)) == ["DIFF_REJECTED", TAB]
    assert wait_browser(term, "diff-close", lambda m: m["id"] == d["id"])
    # answered in the terminal: Claude closes the tab, which answers the open diff too
    claude.send(open_diff(4, root / "target.md", "x\n", "second"))
    d = wait_browser(term, "diff", lambda m: m["title"] == "second")
    assert texts(claude.call(tool_call(5, "close_tab", {"tab_name": "second"}))) == ["TAB_CLOSED"]
    assert texts(claude.reply(4)) == ["TAB_CLOSED"]
    assert wait_browser(term, "diff-close", lambda m: m["id"] == d["id"])
    claude.send(open_diff(6, root / "target.md", "y\n", "third"))
    d = wait_browser(term, "diff", lambda m: m["title"] == "third")
    assert texts(claude.call(tool_call(7, "closeAllDiffTabs", {}))) == ["TAB_CLOSED"]
    assert texts(claude.reply(6)) == ["TAB_CLOSED"]
    assert wait_browser(term, "diff-close", lambda m: m["id"] == d["id"])
    # Claude gives up on one (notifications/cancelled)
    claude.send(open_diff(8, root / "target.md", "z\n", "fourth"))
    d = wait_browser(term, "diff", lambda m: m["title"] == "fourth")
    claude.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 8}})
    assert wait_browser(term, "diff-close", lambda m: m["id"] == d["id"])
    # a decision about a diff that is not waiting closes it in that browser
    term.send_json({"type": "diff-decision", "id": 999999, "accept": True})
    assert wait_browser(term, "diff-close", lambda m: m["id"] == 999999)
    assert (root / "target.md").read_text() == DOC
    term.close()


def test_open_diff_only_shows_text_files_in_the_session_folder(md_editor, tmp_path, agent_log, root):
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    (root / "link.md").symlink_to(outside)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "bin.dat").write_bytes(b"\0\1\2")
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    for rid, path in enumerate((outside, root / "link.md", root / ".git" / "config", root / "bin.dat",
                                root / "sub" / ".." / ".." / "outside.md", "relative.md", root, "/etc/passwd"), 10):
        reply = claude.call(open_diff(rid, path, "new\n"))
        assert reply["result"]["isError"] is True, (path, reply)
        assert texts(reply)[0] not in ("FILE_SAVED", "DIFF_REJECTED", "TAB_CLOSED"), "Claude asks in the terminal"
    assert not browser_msgs(term, "diff")
    assert claude.call(tool_call(30, "openDiff", {"old_file_path": 1}))["error"]["code"] == -32602
    term.close()


def test_the_session_ending_rejects_waiting_diffs(md_editor, tmp_path, agent_log, root):
    (root / "target.md").write_text(DOC)
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    lock = the_lock(lock_dir(tmp_path))
    claude.send(open_diff(3, root / "target.md", "x\n"))
    d = wait_browser(term, "diff")
    term.send_json({"type": "restart"})
    assert texts(claude.reply(3)) == ["DIFF_REJECTED", TAB]
    assert wait_browser(term, "diff-close", lambda m: m["id"] == d["id"])
    assert claude.ws.wait_eof(5), "the old session's link is closed"
    st = term.wait_status(lambda s: s["id"] == 2)
    assert st["ide"] == "waiting" and st["diffs"] == []
    assert not lock.exists(), "the old session's lock went with it"
    new = the_lock(lock_dir(tmp_path))
    assert new != lock and json.loads(new.read_text())["authToken"] != claude.lock["authToken"]
    term.close()


def test_a_new_connection_replaces_the_old_one(md_editor, tmp_path, agent_log, root):
    (root / "target.md").write_text(DOC)
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    claude.send(open_diff(3, root / "target.md", "x\n"))
    d = wait_browser(term, "diff")
    second = Claude(claude.lock_path)
    second.handshake()
    assert claude.ws.wait_eof(5)
    assert wait_browser(term, "diff-close", lambda m: m["id"] == d["id"]), "the old connection's diffs go"
    assert term.wait_status(lambda s: s["ide"] == "connected")
    term.send_json({"type": "selection", "path": "target.md", "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 4}, "text": "line"})
    assert second.wait(lambda m: m.get("method") == "selection_changed")
    second.close()
    term.close()


# ---------------------------------------------------------------- selection, ask and mention

def test_selection_goes_to_claude_for_files_in_the_session_folder(md_editor, tmp_path, agent_log, root):
    (root / "target.md").write_text(DOC)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    (root / "link.md").symlink_to(outside)
    srv = start(md_editor, tmp_path, agent_log)
    term, _, _ = session(srv)
    # sent before Claude connects: it gets it once connected (twice: it listens a moment later)
    term.send_json({"type": "selection", "path": "target.md", "start": {"line": 1, "character": 0},
                    "end": {"line": 2, "character": 4}, "text": "hello world\nline"})
    time.sleep(0.3)
    claude = Claude(the_lock(lock_dir(tmp_path)))
    claude.handshake()
    want = {"text": "hello world\nline", "filePath": str(root / "target.md"), "fileUrl": (root / "target.md").as_uri(),
            "selection": {"start": {"line": 1, "character": 0}, "end": {"line": 2, "character": 4}, "isEmpty": False}}
    assert claude.ws.wait(lambda: len(claude.notes("selection_changed")) == 2, 5)
    assert claude.notes("selection_changed") == [want, want]
    # no selection: an empty range where the cursor is (Claude shows "In target.md")
    term.send_json({"type": "selection", "path": "target.md", "start": {"line": 2, "character": 3},
                    "end": {"line": 2, "character": 3}, "text": ""})
    assert claude.wait(lambda m: m.get("method") == "selection_changed" and m["params"]["selection"]["isEmpty"])
    # backwards ranges are put in order
    term.send_json({"type": "selection", "path": "target.md", "start": {"line": 2, "character": 1},
                    "end": {"line": 0, "character": 2}, "text": "ne one"})
    sel = claude.wait(lambda m: m.get("method") == "selection_changed" and m["params"]["text"] == "ne one")
    assert sel["params"]["selection"]["start"] == {"line": 0, "character": 2}
    n = len(claude.notes("selection_changed"))
    # nothing outside the session's folder, or that is not a file: Claude is told to forget the
    # last selection (an empty range, no file), once, rather than attach it to the next prompt
    for path in ("link.md", "../outside.md", str(outside), "", "nope.md", None):
        term.send_json({"type": "selection", "path": path, "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 3}, "text": "sec"})
    srv.post("/api/root", {"path": str(tmp_path)})   # the session stays in root
    term.send_json({"type": "selection", "path": "outside.md", "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 3}, "text": "sec"})
    term.send_json({"type": "selection", "path": "root/target.md", "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 4}, "text": "line"})
    last = claude.wait(lambda m: m.get("method") == "selection_changed" and m["params"]["text"] == "line")
    assert last["params"]["filePath"] == str(root / "target.md"), "ROOT-relative paths, ROOT changed"
    zero = {"line": 0, "character": 0}
    assert claude.notes("selection_changed")[n:] == [
        {"text": "", "selection": {"start": zero, "end": zero, "isEmpty": True}}, last["params"]]
    assert "secret" not in json.dumps(claude.msgs())
    claude.close()
    term.close()


def test_ask_over_the_link_sends_the_selection_then_pings_then_types(md_editor, tmp_path, agent_log, root):
    (root / "target.md").write_text(DOC)
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    claude.ping_delay = 0.5   # nothing may be typed before Claude has answered
    term.send_json({"type": "ask", "text": "Why \x1b[201~this?\r\nReally?", "path": "target.md",
                    "start": {"line": 1, "character": 0}, "end": {"line": 3, "character": 0}, "seq": 7})
    deadline = time.monotonic() + 10
    while not (agent_log.exists() and agent_log.read_bytes()) and time.monotonic() < deadline:
        time.sleep(0.01)
    # (Claude notes the time before it answers, and md-editor types only once it has the answer)
    assert claude.pinged, "the question was typed only after Claude answered the ping"
    sent = wait_browser(term, "sent")
    assert sent == {"type": "sent", "what": "ask", "ok": True, "via": "ide", "seq": 7}
    msgs = [m for m in claude.msgs() if m.get("method") in ("selection_changed", "ping")]
    assert [m["method"] for m in msgs[-2:]] == ["selection_changed", "ping"]
    assert msgs[-2]["params"]["text"] == "hello world\nline three\n", "the lines, read from the saved file"
    assert typed(agent_log, b"\x1b[200~Why [201~this?\nReally?\x1b[201~\r") == \
        b"\x1b[200~Why [201~this?\nReally?\x1b[201~\r", "one paste, no escapes, then Enter"
    # the whole file (no range): an empty selection, and the question names the file (the empty
    # selection only hints at it, so Claude could take "this" for something else)
    agent_log.write_bytes(b"")
    claude.ping_delay = 0
    term.send_json({"type": "ask", "text": "Summarise", "path": "target.md"})
    assert wait_browser(term, "sent", lambda m: m.get("via") == "ide" and "seq" not in m)
    sel = claude.notes("selection_changed")[-1]
    assert sel["selection"] == {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0},
                                "isEmpty": True} and sel["text"] == ""
    assert typed(agent_log, b"\x1b[200~@target.md Summarise\x1b[201~\r") == b"\x1b[200~@target.md Summarise\x1b[201~\r"
    # a mention: Claude Code inserts it (0-based lines; the end at a line's start is not in it)
    term.send_json({"type": "mention", "path": "target.md", "start": {"line": 1, "character": 0},
                    "end": {"line": 3, "character": 0}})
    assert wait_browser(term, "sent", lambda m: m["what"] == "mention") == \
        {"type": "sent", "what": "mention", "ok": True, "via": "ide"}
    assert claude.notes("at_mentioned")[-1] == {"filePath": str(root / "target.md"), "lineStart": 1, "lineEnd": 2}
    term.send_json({"type": "mention", "path": "target.md"})
    assert claude.wait(lambda m: m.get("method") == "at_mentioned" and "lineStart" not in m["params"])
    # the session's own folder is typed (Claude Code takes no "@.")
    agent_log.write_bytes(b"")
    term.send_json({"type": "mention", "path": ""})
    assert typed(agent_log, f"\x1b[200~@{root} \x1b[201~".encode()) == f"\x1b[200~@{root} \x1b[201~".encode()
    # so is a path with whitespace: Claude Code would insert it unquoted, and its mention end there
    n = len(claude.notes("at_mentioned"))
    (root / "my notes").mkdir()
    (root / "my notes" / "n.md").write_text("n\n")
    agent_log.write_bytes(b"")
    term.send_json({"type": "mention", "path": "my notes/n.md", "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 0}})
    want = b'\x1b[200~@"my notes/n.md"#L1 \x1b[201~'
    assert typed(agent_log, want) == want
    assert len(claude.notes("at_mentioned")) == n
    # Claude does not answer the ping: the question goes typed, with the reference
    agent_log.write_bytes(b"")
    claude.ping_delay = 5
    term.send_json({"type": "ask", "text": "Still there?", "path": "target.md",
                    "start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}})
    assert wait_browser(term, "sent", lambda m: m.get("via") == "typed", 10)
    assert typed(agent_log, b"\x1b[200~@target.md#L1 Still there?\x1b[201~\r") == \
        b"\x1b[200~@target.md#L1 Still there?\x1b[201~\r"
    term.close()


def test_an_ask_about_a_file_elsewhere_clears_claudes_selection(md_editor, tmp_path, agent_log, root):
    """Another folder opened since the session started: an ask about a file there is typed with
    its absolute path, and Claude is first told to forget the selection it holds (from the
    session's folder), which it would otherwise attach to the question."""
    (root / "t.md").write_text("hello world\n")
    other = tmp_path / "other"
    other.mkdir()
    (other / "o.md").write_text("other\n")
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    term.send_json({"type": "selection", "path": "t.md", "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5}, "text": "hello"})
    assert claude.wait(lambda m: m.get("method") == "selection_changed" and m["params"]["text"] == "hello")
    assert srv.post("/api/root", {"path": str(other)})[0] == 200
    agent_log.write_bytes(b"")
    term.send_json({"type": "ask", "text": "Explain", "path": "o.md", "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5}, "seq": 1})
    assert wait_browser(term, "sent", lambda m: m.get("seq") == 1)["via"] == "typed"
    want = f"\x1b[200~@{other / 'o.md'}#L1 Explain\x1b[201~\r".encode()
    assert typed(agent_log, want) == want
    zero = {"line": 0, "character": 0}
    msgs = [m for m in claude.msgs() if m.get("method") in ("selection_changed", "ping")]
    assert msgs[-2:] == [{"jsonrpc": "2.0", "method": "selection_changed",
                          "params": {"text": "", "selection": {"start": zero, "end": zero, "isEmpty": True}}},
                         {"jsonrpc": "2.0", "id": msgs[-1]["id"], "method": "ping"}]
    term.close()


def test_without_the_link_asks_and_mentions_are_typed(md_editor, tmp_path, agent_log, root):
    (root / "target.md").write_text(DOC)
    (root / "my notes").mkdir()
    (root / "my notes" / "n.md").write_text("n\n")
    srv = start(md_editor, tmp_path, agent_log, args=["--ide-link", "off"])
    term, st, report = session(srv)
    assert st["ide"] == "off" and "--ide-link off" in st["ide_reason"]
    assert report == {"argv": [], "port": None}
    assert not lock_dir(tmp_path).exists()
    cases = [
        ({"type": "ask", "text": "Why?", "path": "target.md", "start": {"line": 1, "character": 0},
          "end": {"line": 3, "character": 0}}, b"@target.md#L2-3 Why?", b"\r"),
        ({"type": "ask", "text": "Why?", "path": "target.md", "start": {"line": 1, "character": 2},
          "end": {"line": 1, "character": 5}}, b"@target.md#L2 Why?", b"\r"),
        ({"type": "ask", "text": "Whole?", "path": "target.md"}, b"@target.md Whole?", b"\r"),
        ({"type": "ask", "text": "Plain"}, b"Plain", b"\r"),
        ({"type": "ask", "text": "Spaced", "path": "my notes/n.md"}, b'@"my notes/n.md" Spaced', b"\r"),
        ({"type": "mention", "path": "target.md", "start": {"line": 0, "character": 0},
          "end": {"line": 2, "character": 0}}, b"@target.md#L1-2 ", b""),
        ({"type": "mention", "path": "my notes"}, b'@"my notes" ', b""),
    ]
    for i, (msg, text, enter) in enumerate(cases):
        agent_log.write_bytes(b"")
        term.send_json(msg)
        want = b"\x1b[200~" + text + b"\x1b[201~" + enter
        assert typed(agent_log, want) == want, msg
        assert term.wait(lambda: len(browser_msgs(term, "sent")) == i + 1)
        assert browser_msgs(term, "sent")[-1]["via"] == "typed"
    srv.post("/api/root", {"path": str(tmp_path)})   # a file outside the session's folder: absolute
    agent_log.write_bytes(b"")
    (tmp_path / "out.md").write_text("o\n")
    term.send_json({"type": "mention", "path": "out.md"})
    want = f"\x1b[200~@{tmp_path / 'out.md'} \x1b[201~".encode()
    assert typed(agent_log, want) == want
    assert [m["ok"] for m in browser_msgs(term, "sent")] == [True] * (len(cases) + 1)
    term.send_json({"type": "ask", "text": "  "})
    assert wait_browser(term, "sent", lambda m: m["ok"] is False)
    # Claude Code shows a menu (a permission prompt): Enter would answer it, so nothing is typed
    agent_log.write_bytes(b"")
    say(term, MENU)
    term.send_json({"type": "ask", "text": "Hello?", "seq": 5})
    m = wait_browser(term, "sent", lambda m: m.get("seq") == 5)
    assert m["ok"] is False and "answer it there first" in m["error"]
    time.sleep(0.2)
    assert agent_log.read_bytes() == b""
    term.close()


def test_no_ask_while_claude_waits_for_an_answer(md_editor, tmp_path, agent_log, root):
    """Enter answers whatever Claude Code is asking (its default is "1. Yes"): no question is
    typed while a proposed edit waits, or while Claude Code shows a menu."""
    (root / "target.md").write_text(DOC)
    srv, term, claude = connected(md_editor, tmp_path, agent_log)
    claude.send(open_diff(3, root / "target.md", "x\n"))
    d = wait_browser(term, "diff")
    agent_log.write_bytes(b"")
    pings = len([m for m in claude.msgs() if m.get("method") == "ping"])
    term.send_json({"type": "ask", "text": "What is two plus two?", "path": "target.md", "seq": 1})
    m = wait_browser(term, "sent", lambda m: m.get("seq") == 1)
    assert m["ok"] is False and "proposed change to target.md" in m["error"], m
    time.sleep(0.3)
    assert agent_log.read_bytes() == b"" and claude.reply(3, 0.1) is None, "nothing typed; still waiting"
    assert len([m for m in claude.msgs() if m.get("method") == "ping"]) == pings
    term.send_json({"type": "diff-decision", "id": d["id"], "accept": False})
    assert texts(claude.reply(3)) == ["DIFF_REJECTED", TAB]
    # a menu: a permission prompt for a command, or the folder-trust question (Claude Code marks
    # the choice with its prompt's glyph), whatever the link knows
    for seq, menu in enumerate((MENU, "\u276f 2. Yes, and don't ask again\r\n"), 20):
        say(term, menu)
        term.send_json({"type": "ask", "text": "Hello?", "seq": seq})
        m = wait_browser(term, "sent", lambda m: m.get("seq") == seq)
        assert m["ok"] is False and "answer it there first" in m["error"], m
    time.sleep(0.2)
    assert agent_log.read_bytes() == b""
    # its input box again (even holding a numbered line): the question goes
    say(term, INPUT_BOX)
    say(term, "\u276f\u00a01. Fix this\r\n")
    term.send_json({"type": "ask", "text": "Hello?", "seq": 3})
    assert wait_browser(term, "sent", lambda m: m.get("seq") == 3)["ok"] is True
    assert typed(agent_log, b"\x1b[200~Hello?\x1b[201~\r") == b"\x1b[200~Hello?\x1b[201~\r"
    term.close()


def test_an_ask_waits_for_claude_code_to_start(md_editor, tmp_path, agent_log, root):
    """Claude Code keeps text typed before its prompt is up but drops the Enter, so an ask that
    comes as the session starts (the first one in a narrow window, or just after Restart) waits
    until Claude Code has drawn its prompt, and the page is told; an agent that never draws it
    is waited for until its output has been quiet for a moment."""
    (root / "target.md").write_text(DOC)
    srv = start(md_editor, tmp_path, agent_log, args=["--ide-link", "off"], env={"MDEDIT_START_QUIET": "30"})
    term, _, _ = session(srv, prompt=False)
    other = srv.ws()   # draws while the first one's ask waits
    other.wait_status()
    term.send_json({"type": "ask", "text": "Early?", "path": "target.md", "seq": 1})
    assert wait_browser(term, "waiting", lambda m: m.get("seq") == 1, 5)
    time.sleep(0.5)
    assert not agent_log.exists() or agent_log.read_bytes() == b"", "typed before Claude Code was ready"
    say(other, INPUT_BOX)
    want = b"\x1b[200~@target.md Early?\x1b[201~\r"
    assert typed(agent_log, want, 5) == want
    assert wait_browser(term, "sent", lambda m: m.get("seq") == 1)["ok"] is True
    # Restart: the new session is waited for too
    agent_log.write_bytes(b"")
    term.send_json({"type": "restart"})
    term.wait_status(lambda s: s["id"] == 2)
    term.send_json({"type": "ask", "text": "Again?", "seq": 2})
    assert wait_browser(term, "waiting", lambda m: m.get("seq") == 2, 5)
    say(other, INPUT_BOX)
    assert typed(agent_log, b"\x1b[200~Again?\x1b[201~\r", 5) == b"\x1b[200~Again?\x1b[201~\r"
    other.close()
    term.close()
    # an agent that is not Claude Code: once its output has been quiet for START_QUIET
    srv = start(md_editor, tmp_path, agent_log, args=["--ide-link", "off"], env={"MDEDIT_START_QUIET": "1"})
    term, _, _ = session(srv, prompt=False)
    agent_log.write_bytes(b"")
    t0 = time.monotonic()
    term.send_json({"type": "ask", "text": "Quiet?", "seq": 3})
    assert typed(agent_log, b"\x1b[200~Quiet?\x1b[201~\r", 10) == b"\x1b[200~Quiet?\x1b[201~\r"
    assert wait_browser(term, "sent", lambda m: m.get("seq") == 3)["ok"] is True
    assert time.monotonic() - t0 >= 0.5
    term.close()


def test_asks_need_a_running_session(md_editor, tmp_path, agent_log):
    srv = start(md_editor, tmp_path, agent_log)
    c = srv.ws()
    c.wait_status()
    c.send_json({"type": "ask", "text": "Hello?"})
    m = wait_browser(c, "sent")
    assert m["ok"] is False and "not running" in m["error"]
    c.send_json({"type": "mention", "path": "a.md"})
    assert wait_browser(c, "sent", lambda m: m["what"] == "mention")["ok"] is False
    c.close()


# ---------------------------------------------------------------- launcher mode (claude-sandbox)

def test_launcher_mode_socket_lock_and_settings(md_editor, tmp_path, agent_log, root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    srv = start(md_editor, tmp_path, agent_log, mode="launcher")
    term, st, report = session(srv)
    assert st["ide"] == "waiting"
    lock = the_lock(lock_dir(tmp_path, "launcher"))
    port = int(lock.stem)
    assert S.IDE_PORTS[0] <= port < S.IDE_PORTS[1]
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    data = json.loads(lock.read_text())
    assert data["pid"] == 1, "alive in the jail's pid namespace"
    assert data["mdEditorHostPid"] == srv.proc.pid and data["workspaceFolders"] == [str(root)]
    sock = root / f".md-editor-ide-{port}.sock"
    st_sock = os.lstat(sock)
    assert stat.S_ISSOCK(st_sock.st_mode) and stat.S_IMODE(st_sock.st_mode) == 0o600
    # the launcher gets the settings as arguments, and no environment
    assert report["port"] is None
    assert report["argv"][0] == "--settings" and len(report["argv"]) == 2
    settings = json.loads(report["argv"][1])
    assert settings["env"] == {"CLAUDE_CODE_SSE_PORT": str(port)}
    hook = settings["hooks"]["SessionStart"][0]["hooks"][0]
    assert hook["type"] == "command"
    relay = f"socat TCP4-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:{sock}"
    assert hook["command"] == f"(setsid {relay} </dev/null >/dev/null 2>&1 &)"
    assert data["authToken"] not in json.dumps(settings)
    # the socket is md-editor's own: never listed, searched or opened
    tree = srv.get("/api/tree?all=1")[1]
    assert [e["name"] for e in tree["entries"]] == ["a.md"], tree
    assert srv.get("/api/allfiles")[1]["files"] == ["a.md"]
    assert srv.get("/api/git/status")[1]["files"] == [{"path": "a.md", "status": "U"}]
    # Claude connects through the socket
    claude = Claude(lock, unix=str(sock))
    assert claude.code == 101
    claude.handshake()
    assert term.wait_status(lambda s: s["ide"] == "connected")
    claude.close()
    # restarting gives a new port, socket and lock, and removes the old ones
    term.send_json({"type": "restart"})
    term.wait_status(lambda s: s["id"] == 2)
    deadline = time.monotonic() + 5
    while (lock.exists() or sock.exists()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not lock.exists() and not os.path.lexists(sock)
    lock2 = the_lock(lock_dir(tmp_path, "launcher"))
    sock2 = root / f".md-editor-ide-{lock2.stem}.sock"
    assert os.path.lexists(sock2)
    term.close()
    srv.stop()
    assert not lock2.exists() and not os.path.lexists(sock2), "both go when md-editor stops"


@pytest.mark.skipif(not shutil.which("socat"), reason="needs socat")
def test_launcher_mode_hook_relays_tcp_to_the_socket(md_editor, tmp_path, agent_log):
    """The SessionStart hook's relay, run here (Claude would run it in the jail): Claude's
    connection to 127.0.0.1:<port> reaches md-editor's socket. The folder's name has the
    characters socat's addresses treat specially."""
    weird = tmp_path / "a,b:c d"
    weird.mkdir()
    (weird / "a.md").write_text("# a\n")
    srv = start(md_editor, tmp_path, agent_log, mode="launcher", root=weird)
    term, _, report = session(srv)
    lock = the_lock(lock_dir(tmp_path, "launcher"))
    hook = json.loads(report["argv"][1])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    m = re.fullmatch(r"\(setsid (socat .*) </dev/null >/dev/null 2>&1 &\)", hook)
    assert m
    relay = subprocess.Popen(["sh", "-c", "exec " + m.group(1)], stderr=subprocess.PIPE)
    try:
        port = int(lock.stem)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if relay.poll() is not None:
                pytest.skip(f"socat could not listen on {port}: {relay.stderr.read().decode()[-300:]}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)
        claude = Claude(lock)   # over TCP, as Claude in the jail
        assert claude.code == 101, claude.ws.status_line
        init, _ = claude.handshake()
        assert init["result"]["serverInfo"]["name"] == "md-editor"
        assert term.wait_status(lambda s: s["ide"] == "connected")
        claude.close()
    finally:
        relay.kill()
        relay.wait()
    term.close()


def test_an_unrecognised_agent_gets_no_link_unless_asked(md_editor, tmp_path, agent_log):
    """Only claude and claude-sandbox are linked by default: another agent (bash, say) would not
    understand the --settings the launcher link appends. --ide-link launcher links it anyway."""
    (tmp_path / "shared" / ".claude").mkdir(parents=True)
    env = {"FAKE_AGENT_LOG": str(agent_log), "CLAUDE_SANDBOX_SHARED_CONFIG": str(tmp_path / "shared")}
    srv = md_editor(agent=fake_agent(tmp_path, "my-agent"), env=env)
    term, st, report = session(srv)
    assert st["ide"] == "off" and "neither Claude Code nor claude-sandbox" in st["ide_reason"]
    assert report["argv"] == []
    term.close()
    srv = md_editor(agent=fake_agent(tmp_path, "my-agent"), env=env, args=["--ide-link", "launcher"])
    term, st, report = session(srv)
    assert st["ide"] == "waiting" and report["argv"][0] == "--settings"
    term.close()


def test_launcher_mode_without_a_link(md_editor, tmp_path, agent_log):
    # no claude-sandbox config folder (conftest's default): no link, and nothing appended
    srv = md_editor(agent=fake_agent(tmp_path, "claude-sandbox"), env={"FAKE_AGENT_LOG": str(agent_log)})
    term, st, report = session(srv)
    assert st["ide"] == "off" and "does not exist" in st["ide_reason"] and report["argv"] == []
    term.close()
    # a session folder whose socket path would be too long for a Unix socket
    deep = tmp_path / ("d" * 100)
    deep.mkdir()
    srv = start(md_editor, tmp_path, agent_log, mode="launcher", root=deep)
    term, st, report = session(srv)
    assert st["ide"] == "off" and "longer than" in st["ide_reason"] and report["argv"] == []
    assert "[ide] no IDE link" in srv.logtext()
    assert not list(lock_dir(tmp_path, "launcher").glob("*.lock"))
    term.close()
    # the jail's ~/.claude/ide is the sandboxed agent's to write: a symlink there is not followed
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    lock_dir(tmp_path, "launcher").rmdir()
    lock_dir(tmp_path, "launcher").symlink_to(elsewhere)
    srv = start(md_editor, tmp_path, agent_log, mode="launcher")
    term, st, report = session(srv)
    assert st["ide"] == "off" and "Cannot use" in st["ide_reason"] and report["argv"] == []
    assert not list(elsewhere.iterdir()) and not list(srv.root.glob(".md-editor-ide-*"))
    term.close()


# ---------------------------------------------------------------- turning it off; stale locks

def test_ide_link_off(md_editor, tmp_path, agent_log):
    srv = start(md_editor, tmp_path, agent_log, env={"MDEDIT_IDE_LINK": "OFF"})
    term, st, report = session(srv)
    assert st["ide"] == "off" and report == {"argv": [], "port": None}
    assert "IDE link: off" in srv.logtext()
    term.send_json({"type": "selection", "path": "a.md", "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1}, "text": "#"})
    term.close()
    assert not lock_dir(tmp_path).exists()
    with pytest.raises(RuntimeError, match="did not start"):
        start(md_editor, tmp_path, agent_log, args=["--ide-link", "sometimes"])


def test_what_claude_sends_is_logged_escaped(md_editor, tmp_path, agent_log, root):
    """Whoever holds the token (in launcher mode, anything in the jail) chooses the pid it
    reports and the file names it proposes changes to: md-editor's console gets them escaped,
    never as terminal control sequences."""
    srv = start(md_editor, tmp_path, agent_log)
    term, _, _ = session(srv)
    claude = Claude(the_lock(lock_dir(tmp_path)))
    claude.call(INITIALIZE)
    claude.send(INITIALIZED)
    claude.send({"jsonrpc": "2.0", "method": "ide_connected", "params": {"pid": "\x1b]0;PWNED\x07\x1b[2K\x1b[1A"}})
    assert term.wait_status(lambda s: s["ide"] == "connected")
    claude.send(open_diff(5, root / "evil\x1b]2;NAME\x07.md", "x\n", "evil"))
    d = wait_browser(term, "diff")
    term.send_json({"type": "diff-decision", "id": d["id"], "accept": False})
    assert texts(claude.reply(5)) == ["DIFF_REJECTED", "evil"]
    deadline = time.monotonic() + 5
    while "rejected the change" not in srv.logtext() and time.monotonic() < deadline:
        time.sleep(0.05)
    log = srv.logtext()
    assert "PWNED" in log and "showing a proposed change" in log and "rejected the change" in log
    assert "\x1b" not in log and "\x07" not in log, log[-800:]
    term.close()


def test_idle_connections_to_the_link_are_dropped_and_capped(md_editor, tmp_path, agent_log):
    """Connections that never send their upgrade are closed after MDEDIT_IDE_HANDSHAKE seconds,
    and there are at most IDE_CONNS_MAX at once, so they cannot pile up threads and file
    descriptors in md-editor; Claude, once connected, may be quiet for as long as it likes."""
    srv = start(md_editor, tmp_path, agent_log, env={"MDEDIT_IDE_HANDSHAKE": "3"})
    term, _, _ = session(srv)
    lock = the_lock(lock_dir(tmp_path))
    port = int(lock.stem)
    t0 = time.monotonic()
    idle = []
    for _ in range(S.IDE_CONNS_MAX):   # one at a time: the listen backlog is short
        idle.append(socket.create_connection(("127.0.0.1", port), timeout=10))
        time.sleep(0.05)
    extra = socket.create_connection(("127.0.0.1", port), timeout=10)
    assert extra.recv(10) == b"" and time.monotonic() - t0 < 2.5, "over the cap: closed at once"
    for s in idle:
        assert s.recv(10) == b"", "closed when its time to upgrade ran out"
        s.close()
    assert time.monotonic() - t0 >= 2.5
    extra.close()
    claude = Claude(lock)
    assert claude.code == 101
    claude.handshake()
    assert term.wait_status(lambda s: s["ide"] == "connected")
    time.sleep(1.5)
    assert claude.call({"jsonrpc": "2.0", "id": 9, "method": "ping"})["result"] == {}
    claude.close()
    term.close()


def test_stale_lock_files_are_swept_at_start(md_editor, tmp_path, agent_log, root):
    d = lock_dir(tmp_path)
    d.mkdir(parents=True)
    gone = subprocess.Popen(["true"])
    gone.wait()
    mine = {"ideName": "md-editor", "workspaceFolders": [str(root)], "transport": "ws", "authToken": "t"}
    (d / "21000.lock").write_text(json.dumps(dict(mine, pid=1, mdEditorHostPid=gone.pid)))
    stale_sock = socket.socket(socket.AF_UNIX)
    stale_sock.bind(str(root / ".md-editor-ide-21000.sock"))
    stale_sock.close()
    (d / "21001.lock").write_text(json.dumps(dict(mine, pid=1, mdEditorHostPid=os.getpid())))   # alive
    (d / "21002.lock").write_text(json.dumps({"ideName": "VS Code", "pid": gone.pid, "workspaceFolders": []}))
    (d / "21003.lock").write_text("not json")
    os.mkfifo(d / "21004.lock")   # never read (it would block)
    shared = lock_dir(tmp_path, "launcher")
    shared.mkdir(parents=True)
    (shared / "21005.lock").write_text(json.dumps(dict(mine, pid=1, mdEditorHostPid=gone.pid)))
    # a sandboxed agent can write the shared folder: a lock that claims a dead md-editor but
    # names a live link (another project's) is left alone, and so is its socket
    live = socket.socket(socket.AF_UNIX)
    live.bind(str(root / ".md-editor-ide-21006.sock"))
    live.listen(4)
    (shared / "21006.lock").write_text(json.dumps(dict(mine, pid=1, mdEditorHostPid=gone.pid)))
    try:
        srv = start(md_editor, tmp_path, agent_log)
        assert sorted(p.name for p in d.iterdir()) == ["21001.lock", "21002.lock", "21003.lock", "21004.lock"]
        assert not os.path.lexists(root / ".md-editor-ide-21000.sock")
        assert not (shared / "21005.lock").exists(), "both folders are swept"
        assert (shared / "21006.lock").exists() and os.path.lexists(root / ".md-editor-ide-21006.sock")
        assert "removed a stale lock file" in srv.logtext()
    finally:
        live.close()


def test_lock_file_deleted_is_written_again(md_editor, tmp_path, agent_log):
    srv = start(md_editor, tmp_path, agent_log)
    term, _, _ = session(srv)
    lock = the_lock(lock_dir(tmp_path))
    body = lock.read_text()
    lock.unlink()
    deadline = time.monotonic() + S.IDE_LOCK_CHECK + 3
    while not lock.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert lock.read_text() == body
    term.close()


# ---------------------------------------------------------------- in-process units

def test_read_inside_never_follows_a_link(tmp_path):
    """openDiff and ask read a file whose real path they checked: the agent could have put a link
    in its place (or a folder's) since, so the file is opened one name at a time from the
    session's folder, and a link is refused."""
    folder = tmp_path / "f"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.md").write_bytes(b"a\r\nb\n")
    (folder / "sub" / "b.md").write_text("b\n")
    (tmp_path / "secret.md").write_text("secret\n")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x.md").write_text("secret\n")
    (folder / "link.md").symlink_to(tmp_path / "secret.md")
    (folder / "dirlink").symlink_to(tmp_path / "out")
    os.mkfifo(folder / "fifo.md")
    (folder / "bin.dat").write_bytes(b"\0\1")
    f = str(folder)
    assert S.read_inside(f + "/a.md", f) == ("a\nb\n", True)
    assert S.read_inside(f + "/sub/b.md", f) == ("b\n", True)
    for p in ("/link.md", "/dirlink/x.md", "/fifo.md", "/bin.dat", "/sub", ""):
        assert S.read_inside(f + p, f) == (None, True), p
    assert S.read_inside(str(tmp_path / "secret.md"), f) == (None, True)
    assert S.read_inside(f + "/new.md", f) == ("", False)
    assert S.read_inside(f + "/new/deeper/x.md", f) == ("", False)


def test_text_helpers():
    assert S.line_span({"line": 1, "character": 0}, {"line": 3, "character": 0}) == (2, 3)
    assert S.line_span({"line": 1, "character": 2}, {"line": 1, "character": 5}) == (2, 2)
    assert S.line_span({"line": 1, "character": 0}, {"line": 2, "character": 1}) == (2, 3)
    assert S.line_span({"line": 4, "character": 0}, {"line": 4, "character": 0}) is None
    assert S.typed_ref("/w/p/a.md", "/w/p", (2, 3)) == "@a.md#L2-3"
    assert S.typed_ref("/w/p/a.md", "/w/p/", (2, 2)) == "@a.md#L2"
    assert S.typed_ref("/w/q/a b.md", "/w/p") == '@"/w/q/a b.md"'
    assert S.typed_ref("/w/p", "/w/p") == "@/w/p"
    assert S.paste_bytes("a\x1b[201~b\r\nc\td\x9b\x07") == b"\x1b[200~a[201~b\nc\td\x1b[201~"
    text = "a😀b\nxyz\n"
    assert S.text_range(text, {"line": 0, "character": 1}, {"line": 0, "character": 3}) == "😀"
    assert S.text_range(text, {"line": 0, "character": 3}, {"line": 1, "character": 2}) == "b\nxy"
    assert S.text_range(text, {"line": 9, "character": 0}, {"line": 9, "character": 0}) == ""
    assert S.ide_range({"start": {"line": 2, "character": 0}, "end": {"line": 1, "character": 5}}) == \
        ({"line": 1, "character": 5}, {"line": 2, "character": 0})
    assert S.ide_range({"start": {"line": -1, "character": 0}}) == ({"line": 0, "character": 0},) * 2
    assert S.socat_path("/a,b:c d/x.sock") == r"/a\,b\:c\ d/x.sock"
    assert S.socat_path("/plain/x.sock") == "/plain/x.sock"
    assert shlex.split(f"x {shlex.quote(S.socat_path(chr(39) + 'q'))}")[1] == "\\'q"
    assert all(S.ide_socket(n) for n in (".md-editor-ide-1.sock", "a/b/.md-editor-ide-54321.sock"))
    assert not any(S.ide_socket(n) for n in ("md-editor-ide-1.sock", ".md-editor-ide-x.sock", ".md-editor-ide-1.sock.x"))
