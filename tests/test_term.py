"""The side-panel terminal: /api/term, a hand-written WebSocket onto one shared PTY session.
The agent is mostly bash, so the tests can drive it, and fakes/tui for a full-screen agent
such as Claude Code. Linux only: they read /proc and rely on GNU tools."""
import itertools
import os
import re
import shlex
import signal
import socket
import struct
import sys
import threading
import time

import pytest

from helpers import FAKES, WS, alive, gone_within, needs_linux

from md_collab_editor import server as S

pytestmark = needs_linux

BASH = "bash --norc --noprofile"
SEQ = itertools.count(1)
# a full-screen agent that sets its modes and then writes past the scrollback (fakes/tui)
TUI = shlex.join([sys.executable, str(FAKES / "tui")])
# what its replay starts with: the modes it set, which are no longer in the scrollback
TUI_MODES = b"\x1b[?1049h\x1b[?1h\x1b[?7l\x1b[?25l\x1b[?1003h\x1b[?1006h\x1b[?1004h\x1b[?2004h"
NUDGE_WAIT = S.NUDGE_SECONDS + 0.5


# ---------------------------------------------------------------- helpers

def running(srv, cols=100, rows=30):
    """A client attached to a freshly started session (status none, resize, running)."""
    c = srv.ws()
    assert c.code == 101, c.status_line
    assert c.wait_status()["state"] == "none"
    c.resize(cols, rows)
    assert c.wait_status(lambda s: s["state"] == "running")
    return c


def roundtrip(c, *others, timeout=10):
    """Run an echo whose output differs from what was typed; every client must see it."""
    n = next(SEQ)
    since = [x.mark() for x in (c,) + others]
    c.cmd(f"echo RT{n}-$(({n}*7))")
    want = f"RT{n}-{n * 7}".encode()
    ok = all(x.wait_out(want, s, timeout) for x, s in zip((c,) + others, since))
    return want if ok else None


def shell_value(c, expr, timeout=10):
    """Echo a shell expression (e.g. '$$') and return what it printed."""
    n = next(SEQ)
    since = c.mark()
    c.cmd(f"echo V{n}=$((1+1))-{expr}-X")   # the echo of what was typed doesn't match
    m = c.wait_re(rb"V%d=2-(.*?)-X" % n, since, timeout)
    assert m, bytes(c.outbuf[since:])[-300:]
    return m.group(1).decode()


def stty_size(c):
    cols_rows = shell_value(c, "$(stty size | tr ' ' x)").split("x")
    return int(cols_rows[1]), int(cols_rows[0])


def lines_of(n, fill=b"z"):
    """Exactly n bytes of newline-terminated lines of at most 100 bytes (canonical-mode safe)."""
    full, rem = divmod(n, 100)
    data = (fill * 99 + b"\n") * full
    if rem:
        data += b"q" * (rem - 1) + b"\n"
    return data


def wait_size(path, size, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size == size:
            return True
        time.sleep(0.05)
    return False


def start_cat(c, path):
    """`cat > path` in the shell, waiting until cat is really reading (echo off, so the
    pasted input isn't echoed back)."""
    n = next(SEQ)
    since = c.mark()
    c.cmd(f"stty -echo; echo CAT{n}-$((1+1)); cat > {path}")
    assert c.wait_out(f"CAT{n}-2", since)
    time.sleep(0.3)


def finish_cat(c):
    c.send_bin(b"\x04")
    c.cmd("stty echo")


@pytest.fixture
def srv(md_editor):
    return md_editor(agent=BASH)


# ---------------------------------------------------------------- handshake and errors

def test_handshake_and_first_status(srv):
    c = srv.ws()
    try:
        assert c.status_line == "HTTP/1.1 101 Switching Protocols"
        assert c.headers["sec-websocket-accept"] == c.want_accept
        assert c.headers["upgrade"].lower() == "websocket"
        assert c.headers["connection"].lower() == "upgrade"
        st = c.wait_status()
        assert c.frames[0].op == 1
        assert st == {"type": "status", "id": None, "state": "none", "cmd": BASH, "cwd": None,
                      "code": None, "message": None}
        assert not c.wait(lambda: any(f.op == 2 for f in c.frames), 0.5), "no replay for a 'none' session"
    finally:
        c.close()


def test_bad_upgrades_get_400(srv):
    good = {"Host": srv.host, "Origin": srv.origin, "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
    for change in ({"Upgrade": None}, {"Connection": "keep-alive"}, {"Sec-WebSocket-Version": "8"},
                   {"Sec-WebSocket-Key": None}):
        h = dict(good, **change)
        status, body = srv.http("GET", "/api/term", headers=[(k, v) for k, v in h.items() if v is not None])
        assert status == 400, (change, body)


def test_refused_sockets_spawn_nothing(srv):
    """A 403 never reads the frames pipelined after the request, so a resize can't start anything."""
    resize = struct.pack("!BB", 0x81, 0x80 | 30) + b"\0\0\0\0" + b'{"type":"resize","cols":80,"ro'
    for kw in (dict(origin="http://evil.example"), dict(origin=None), dict(host="evil.example")):
        assert WS(srv.port, pipeline=resize, **kw).code == 403
    c = srv.ws()
    assert c.wait_status()["state"] == "none"
    c.close()


def test_unavailable_agents(md_editor):
    for agent, reason in (("none", "turned off"), ("", "turned off"), ("no-such-agent-xyz", "not found on the PATH")):
        srv = md_editor(agent=agent)
        a = srv.config()["agent"]
        assert a["available"] is False and reason in a["reason"], a
        assert a["cmd"] == (agent if agent not in ("none", "") else None), a
        assert srv.ws().code == 503


# ---------------------------------------------------------------- the session

def test_first_resize_spawns_status_before_output_and_sizes(srv, root):
    c = running(srv, 100, 30)
    try:
        st = c.wait_status(lambda s: s["state"] == "running")
        assert st["id"] == 1 and st["cwd"] == str(root) and st["cmd"] == BASH and st["code"] is None
        c.wait(lambda: any(f.op == 2 for f in c.frames), 5)
        first_running = next(i for i, f in enumerate(c.frames) if f.op == 1 and f.json()["state"] == "running")
        first_output = next(i for i, f in enumerate(c.frames) if f.op == 2)
        assert first_running < first_output
        assert stty_size(c) == (100, 30)
        env = "${CLAUDECODE-u}.${CLAUDE_CODE_CHILD_SESSION-u}.${CLAUDE_CODE_SESSION_ID-u}.$TERM"
        assert shell_value(c, env) == "u.u.u.xterm-256color", "the parent Claude session's markers are dropped"
        c.resize(120, 40)
        time.sleep(0.2)
        assert stty_size(c) == (120, 40)
        c.resize(5000, 0)
        time.sleep(0.2)
        assert stty_size(c) == (1000, 1), "clamped to 1..1000"
        c.resize(100, 30)
        time.sleep(0.2)
        assert len(c.statuses()) == 2, "resizes send no status"
    finally:
        c.close()


def test_utf8_both_ways(srv, root):
    c = running(srv)
    try:
        since = c.mark()
        c.cmd("echo héllo✓ > utf8.txt; cat utf8.txt; echo U-$((2+3))")
        assert c.wait_out("U-5", since)
        assert (root / "utf8.txt").read_bytes() == "héllo✓\n".encode()
        assert "héllo✓".encode() in c.outbuf[since:]
    finally:
        c.close()


def test_fragmented_paste_with_ping_between_fragments(srv, root):
    c = running(srv)
    try:
        start_cat(c, "big.txt")
        data = b"".join(b"%06d" % i + b"y" * 93 + b"\n" for i in range(2000))
        chunks = [data[i:i + 50000] for i in range(0, len(data), 50000)]
        c.send_frame(2, chunks[0], fin=False)
        c.send_frame(9, b"mid-ping")
        c.send_frame(0, chunks[1], fin=False)
        c.send_frame(10, b"unsolicited")   # a pong between fragments is ignored
        c.send_frame(0, chunks[2], fin=False)
        c.send_frame(0, chunks[3], fin=True)
        assert c.wait(lambda: any(f.op == 10 and f.payload == b"mid-ping" for f in c.frames))
        finish_cat(c)
        assert wait_size(root / "big.txt", len(data))
        assert (root / "big.txt").read_bytes() == data
        assert roundtrip(c)
    finally:
        c.close()


def test_payload_length_encodings(srv, root):
    c = running(srv)
    try:
        sizes = (125, 126, 65535, 65536)
        start_cat(c, "lens.txt")
        for n in sizes:
            c.send_bin(lines_of(n))
        finish_cat(c)
        assert wait_size(root / "lens.txt", sum(sizes))
        for n, (cols, rows) in zip(sizes, [(101, 31), (102, 32), (103, 33), (104, 34)]):
            pad = n - len(f'{{"type": "resize", "cols": {cols}, "rows": {rows}, "pad": ""}}')
            msg = f'{{"type": "resize", "cols": {cols}, "rows": {rows}, "pad": "{"p" * pad}"}}'.encode()
            assert len(msg) == n
            c.send_frame(1, msg)
        time.sleep(0.2)
        assert stty_size(c) == (104, 34)
    finally:
        c.close()


PROTOCOL_ERRORS = {
    "unmasked frame": (lambda c: c.send_frame(2, b"hello", mask=False), 1002),
    "message over 4 MiB": (lambda c: c.send_frame(2, b"", length=4 * 1024 * 1024 + 1), 1009),
    "fragments over 4 MiB": (lambda c: (c.send_frame(2, b"a" * (3 << 20), fin=False),
                                        c.send_frame(0, b"", length=2 << 20)), 1009),
    "ping over 125 bytes": (lambda c: c.send_frame(9, b"x" * 126), 1002),
    "fragmented ping": (lambda c: c.send_frame(9, b"x", fin=False), 1002),
    "continuation without a start": (lambda c: c.send_frame(0, b"x"), 1002),
    "data frame inside a fragmented message": (lambda c: (c.send_frame(2, b"a", fin=False),
                                                          c.send_frame(2, b"b")), 1002),
    "RSV1 set": (lambda c: c.send_frame(2, b"x", rsv=0x40), 1002),
    "1-byte close payload": (lambda c: c.send_frame(8, b"\x03"), 1002),
    "reserved opcode": (lambda c: c.send_frame(3, b"x"), 1002),
}


def test_protocol_errors_close_with_the_right_code(srv):
    main = running(srv)
    try:
        for name, (send, code) in PROTOCOL_ERRORS.items():
            c = srv.ws()
            c.wait_status()
            send(c)
            assert c.wait(c.close_frames, 5), name
            assert c.close_frames()[0].payload[:2] == struct.pack("!H", code), name
            assert c.wait_eof(5), name
            c.abort()
        c = srv.ws()
        c.wait_status()
        c.send_frame(9, b"")
        assert c.wait(lambda: any(f.op == 10 and f.payload == b"" for f in c.frames), 5), "empty ping, empty pong"
        c.send_frame(8, struct.pack("!H", 4321) + b"bye")
        assert c.wait(c.close_frames, 5) and c.close_frames()[0].payload == struct.pack("!H", 4321)
        assert c.wait_eof(5)
        c.abort()
        for payload in (b"{not json", b"[1, 2]", b"null", b'{"type":"resize","cols":"abc","rows":null}',
                        b'{"type":"resize","cols":1e400,"rows":5}', b"\xff\xfe"):
            main.send_frame(1, payload)
        assert roundtrip(main), "the session survives"
        assert stty_size(main) == (100, 30), "malformed resizes are ignored"
        assert "Traceback" not in srv.logtext()
    finally:
        main.close()


def test_oversized_message_is_drained_after_the_close(srv):
    """After a 1009 the server reads on (and discards) what the browser is still sending, so
    the connection ends with our close frame and a FIN, not a reset that could lose it."""
    c = srv.ws()
    try:
        c.wait_status()
        c.send_frame(2, b"", length=4 * 1024 * 1024 + 1)   # the header, then part of the payload
        c.sock.sendall(b"p" * (1 << 20))
        assert c.wait(c.close_frames, 5) and c.close_frames()[0].payload == struct.pack("!H", 1009)
        assert c.wait_eof(5)
        time.sleep(0.2)
        c.sock.sendall(b"p" * (256 << 10))   # the server is still reading: no reset
    finally:
        c.abort()


def test_replay_and_broadcast_to_a_second_client(srv):
    c1 = running(srv)
    c2 = None
    try:
        since = c1.mark()
        c1.cmd("head -c 600000 /dev/zero | tr '\\0' x; echo; echo BIG-$((6*7))")
        assert c1.wait_out("BIG-42", since)
        last = roundtrip(c1)
        c2 = srv.ws()
        assert c2.wait(lambda: len(c2.frames) >= 2)
        st, replay = c2.frames[0].json(), c2.frames[1]
        assert st["state"] == "running" and st["id"] == 1
        assert replay.op == 2 and last in replay.payload
        assert 65536 <= len(replay.payload) <= 512 * 1024 + 100, "512 KiB of scrollback (after any modes)"
        assert replay.lenclass == 64
        assert roundtrip(c1, c2) and roundtrip(c2, c1), "both clients see each other's typing"
        assert {f.lenclass for f in c1.frames + c2.frames} >= {7, 64}
        assert not (c1.bad or c2.bad), c1.bad + c2.bad
    finally:
        c1.close()
        if c2:
            c2.close()


def test_reconnect_finds_the_same_session(srv):
    c1 = running(srv)
    bash = int(shell_value(c1, "$$"))
    last = roundtrip(c1)
    c1.close()
    time.sleep(0.3)
    assert alive(bash), "the session outlives its clients"
    c2 = srv.ws()
    try:
        st = c2.wait_status()
        assert st["state"] == "running" and st["id"] == 1
        assert c2.wait(lambda: len(c2.frames) >= 2 and c2.frames[1].op == 2 and last in c2.frames[1].payload, 5)
        assert roundtrip(c2)
    finally:
        c2.close()


def test_reconnect_restores_trimmed_modes_and_nudges_a_redraw(md_editor):
    """The agent set its modes long ago, so the replay starts by restoring them; a browser's
    first resize at the size the PTY already has still makes the agent redraw (SIGWINCH at
    one row fewer, then at the size), and later resizes don't."""
    srv = md_editor(agent=TUI)
    c1 = running(srv, 100, 30)
    c2 = c3 = None
    try:
        assert c1.wait_out("READY", timeout=30)
        assert c1.outbuf.startswith(b"\x1b[?1049h\x1b[?1h"), "the first client saw them live"
        c1.close()
        c2 = srv.ws()
        assert c2.wait(lambda: len(c2.frames) >= 2)
        replay = c2.frames[1].payload
        assert replay.startswith(TUI_MODES + b"x" * 98 + b"\r\n"), replay[:120]
        assert b"\x1b" not in replay[len(TUI_MODES):], "the sequences that set them were trimmed off"
        assert replay.endswith(b"READY\r\n") and len(replay) <= 512 * 1024 + len(TUI_MODES)
        since = c2.mark()
        c2.resize(100, 30)
        assert c2.wait_out("WINCH 100x30", since)
        assert re.fullmatch(rb"WINCH 100x29\r\nWINCH 100x30\r\n", bytes(c2.outbuf[since:])), c2.outbuf[since:]
        since = c2.mark()
        c2.resize(100, 30)
        c2.send_bin(b"hi\x1b[A")
        assert c2.wait_out("GOT hi^[[A\r\n", since)
        assert b"WINCH" not in c2.outbuf[since:], "only a browser's first resize nudges"
        c3 = srv.ws()
        assert c3.wait(lambda: len(c3.frames) >= 2) and c3.frames[1].payload.startswith(TUI_MODES)
        since = c3.mark()
        c3.resize(90, 25)
        assert c3.wait_out("WINCH 90x25", since)
        time.sleep(NUDGE_WAIT)
        assert bytes(c3.outbuf[since:]) == b"WINCH 90x25\r\n", "a new size needs no nudge"
    finally:
        for c in (c1, c2, c3):
            if c:
                c.close()


def test_exit_code_no_respawn_then_restart(srv, root):
    c1, c2 = running(srv), srv.ws()
    try:
        c2.wait(lambda: len(c2.frames) >= 2)
        bash = int(shell_value(c1, "$$"))
        c1.cmd("exit 7")
        for c in (c1, c2):
            st = c.wait_status(lambda s: s["state"] == "exited")
            assert st and st["code"] == 7 and st["id"] == 1, st
        assert gone_within([bash])
        n = len(c1.statuses())
        c1.resize(90, 25)
        time.sleep(0.8)
        assert len(c1.statuses()) == n, "a resize after the exit starts nothing"
        late = srv.ws()
        assert late.wait(lambda: len(late.frames) >= 2)
        st = late.frames[0].json()
        assert st["state"] == "exited" and st["code"] == 7 and late.frames[1].op == 2, "late client gets the replay"
        late.close()
        mark = len(c2.frames)
        c1.send_json({"type": "restart"})
        st = c2.wait_status(lambda s: s["id"] == 2)
        assert st["state"] == "running" and st["cwd"] == str(root)
        status_at = next(i for i, f in enumerate(c2.frames) if f.op == 1 and f.json()["id"] == 2)
        assert all(f.op != 2 for f in c2.frames[mark:status_at]), "no output between restart and the new status"
        assert stty_size(c1) == (90, 25), "the new session gets the last size"
        assert roundtrip(c1, c2)
        c1.cmd("kill -9 $$")
        st = c1.wait_status(lambda s: s["id"] == 2 and s["state"] == "exited")
        assert st["code"] == -9, "killed by a signal: minus the signal number"
    finally:
        c1.close()
        c2.close()


def test_restart_kills_the_old_process_group(srv):
    c = running(srv)
    try:
        bash = int(shell_value(c, "$$"))
        orphan = int(shell_value(c, "$(sleep 1000 >/dev/null 2>&1 & echo $!)"))   # its subshell has exited
        since = c.mark()
        c.cmd("sleep 1001 & echo BG$((1+1))-$!-X")
        bg = int(c.wait_re(rb"BG2-(\d+)-X", since).group(1))
        assert alive(bash) and alive(orphan) and alive(bg)
        c.send_json({"type": "restart"})
        assert c.wait_status(lambda s: s["id"] == 2 and s["state"] == "running")
        assert gone_within([bash, orphan, bg])
        assert not any(s["state"] == "exited" for s in c.statuses()), "a replaced session announces no exit"
        assert roundtrip(c)
    finally:
        c.close()


def test_root_change_then_restart(srv, root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    c = running(srv)
    try:
        n = len(c.statuses())
        status, res = srv.post("/api/root", {"path": str(other)})
        assert status == 200 and res["root"] == str(other)
        time.sleep(0.5)
        assert len(c.statuses()) == n, "changing the root alone restarts nothing"
        c2 = srv.ws()
        assert c2.wait_status()["cwd"] == str(root)
        c2.close()
        c.send_json({"type": "restart"})
        st = c.wait_status(lambda s: s["id"] == 2)
        assert st["state"] == "running" and st["cwd"] == str(other)
        assert shell_value(c, "$(pwd)") == str(other)
    finally:
        c.close()


def test_spawn_failure_then_recovery(srv, root, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    c = running(srv)
    try:
        srv.post("/api/root", {"path": str(gone)})
        gone.rmdir()
        c.send_json({"type": "restart"})
        st = c.wait_status(lambda s: s["id"] == 2)
        assert st["state"] == "failed" and "No such file" in st["message"], st
        assert c.wait_out(b"\x1b[31mCould not start", 0, 5)
        late = srv.ws()
        assert late.wait(lambda: len(late.frames) >= 2)
        assert late.frames[0].json()["state"] == "failed" and b"\x1b[31m" in late.frames[1].payload
        late.close()
        n = len(c.statuses())
        c.resize(100, 30)
        time.sleep(0.8)
        assert len(c.statuses()) == n, "a resize after a failure doesn't retry"
        srv.post("/api/root", {"path": str(root)})
        c.send_json({"type": "restart"})
        st = c.wait_status(lambda s: s["id"] == 3)
        assert st["state"] == "running" and st["cwd"] == str(root)
        assert roundtrip(c)
        assert "Traceback" not in srv.logtext()
    finally:
        c.close()


def test_exec_failure_is_reported(md_editor, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "badagent").write_text("this is not a program\n")
    (bindir / "badagent").chmod(0o755)
    srv = md_editor(agent="badagent --flag", env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
    assert srv.config()["agent"]["available"]
    c = srv.ws()
    try:
        c.wait_status()
        c.resize(80, 24)
        st = c.wait_status(lambda s: s["state"] == "failed")
        assert st["id"] == 1 and "Exec format error" in st["message"], st
        assert srv.proc.poll() is None
    finally:
        c.close()


# ---------------------------------------------------------------- shutdown and slow clients

def test_a_zero_ping_interval_means_the_default(md_editor):
    srv = md_editor(agent=BASH, env={"MDEDIT_WS_PING": "0"})
    c = srv.ws(pong=False)
    try:
        assert c.wait_status()
        time.sleep(1)
        assert not c.eof and not any(f.op == 9 for f in c.frames), "30 s between pings, not none"
    finally:
        c.close()


def test_a_client_that_stops_answering_pings_is_dropped(md_editor):
    srv = md_editor(agent=BASH, env={"MDEDIT_WS_PING": "0.5"})
    gone, live = srv.ws(pong=False), srv.ws()
    try:
        for c in (gone, live):
            c.wait_status()
        live.resize(100, 30)
        assert live.wait_status(lambda s: s["state"] == "running")
        assert gone.wait_eof(5), "dropped after a ping went unanswered"
        assert [f.op for f in gone.frames if f.op in (9, 10)][:1] == [9]
        time.sleep(1.5)
        assert not live.eof and sum(f.op == 9 for f in live.frames) >= 3, "pinged, answered, kept"
        assert roundtrip(live)
    finally:
        gone.abort()
        live.close()


def test_agent_exit_with_a_straggler_holding_the_pty(md_editor, tmp_path):
    """The agent exits, leaving a process (that ignores SIGHUP) with the PTY open: a second
    after the PTY goes quiet the session has exited, and the straggler is killed with it."""
    pidfile = tmp_path / "straggler.pid"
    srv = md_editor(agent=f"sh -c 'trap \"\" HUP; sleep 1000 & echo $! > {pidfile}; echo AGENT-DONE; exit 3'")
    c = srv.ws()
    try:
        c.wait_status()
        t0 = time.monotonic()
        c.resize(80, 24)
        st = c.wait_status(lambda s: s["state"] == "exited", 15)
        assert st and st["id"] == 1 and st["code"] == 3, c.statuses()
        assert time.monotonic() - t0 < 8, "1 s of quiet, then up to 3 s from SIGHUP to SIGKILL"
        assert c.wait_out("AGENT-DONE")
        assert gone_within([int(pidfile.read_text())])
    finally:
        c.close()


def test_restart_kills_a_straggler_that_outlives_the_agent(md_editor, tmp_path):
    """While a straggler keeps writing after the agent has exited the session still runs (its
    output is shown), and Restart kills the old group."""
    pidfile = tmp_path / "straggler.pid"
    srv = md_editor(agent=f"sh -c 'trap \"\" HUP; (while :; do echo TICK; sleep 0.2; done) & "
                          f"echo $! > {pidfile}; exit 0'")
    c = srv.ws()
    try:
        c.wait_status()
        c.resize(80, 24)
        assert c.wait_status(lambda s: s["state"] == "running")
        assert c.wait_out("TICK")
        straggler = int(pidfile.read_text())
        time.sleep(2)
        since = c.mark()
        assert c.wait_out("TICK", since) and alive(straggler)
        assert not any(s["state"] == "exited" for s in c.statuses())
        c.send_json({"type": "restart"})
        assert c.wait_status(lambda s: s["id"] == 2)
        assert gone_within([straggler])
    finally:
        c.close()


def test_sigterm_ends_the_whole_session(srv):
    c = running(srv)
    try:
        assert int(shell_value(c, "$PPID")) == srv.proc.pid, "the agent is md-editor's own child"
        bash = int(shell_value(c, "$$"))
        since = c.mark()
        c.cmd("sleep 1001 & echo BG$((1+1))-$!-X")
        bg = int(c.wait_re(rb"BG2-(\d+)-X", since).group(1))
        since = c.mark()
        c.cmd("sh -c 'echo FG$((1+1))-$$-X; exec sleep 1000'")
        fg = int(c.wait_re(rb"FG2-(\d+)-X", since).group(1))
        os.kill(srv.proc.pid, signal.SIGTERM)
        assert gone_within([bash, bg, fg])
        srv.proc.wait(5)
        assert c.wait_eof(5)
        assert "Traceback" not in srv.logtext()
    finally:
        c.abort()


def stuck_client(port):
    """A WebSocket client that completes the handshake and then never reads again."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.connect(("127.0.0.1", port))
    s.sendall((f"GET /api/term HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(1)
    return s, head.split(b"\r\n")[0].decode()


def test_a_client_that_stops_reading_does_not_stall_the_others(srv):
    c1 = running(srv)
    stuck, line = stuck_client(srv.port)
    c2 = None
    try:
        assert " 101 " in line
        since = c1.mark()
        t0 = time.monotonic()
        c1.cmd("yes 0123456789abcdef | head -c 10000000; echo; echo DONE-$((6*7))")
        assert c1.wait_out("DONE-42", since, 60), f"got {c1.mark() - since} bytes"
        # the stuck client holds everyone up for one WS_SEND_TIMEOUT (10 s), not one per send()
        assert time.monotonic() - t0 < 17
        c2 = srv.ws()
        assert c2.wait(lambda: len(c2.frames) >= 2)
        assert roundtrip(c1, c2)
        stuck.settimeout(20)
        eof = False
        try:
            while True:
                if not stuck.recv(1 << 20):
                    eof = True
                    break
        except OSError:
            pass
        assert eof, "the server dropped the stuck client"
    finally:
        stuck.close()
        c1.close()
        if c2:
            c2.close()


def test_a_client_whose_paste_is_blocked_is_not_dropped(md_editor):
    """While the agent is not reading, a paste blocks the handler that would read the
    client's pongs: keepalive must not take that for a browser that has gone away."""
    srv = md_editor(agent=BASH, env={"MDEDIT_WS_PING": "0.3"})
    c1 = running(srv)
    c2 = None
    try:
        c1.cmd("sleep 1000")
        time.sleep(0.3)
        threading.Thread(target=lambda: c1.send_bin(lines_of(2 << 20)), daemon=True).start()
        time.sleep(3)   # ten ping intervals
        assert not c1.eof, "dropped while its paste was blocked"
        c2 = srv.ws()
        c2.wait_status()
        c2.send_json({"type": "restart"})   # frees the paste
        assert c2.wait_status(lambda s: s["id"] == 2 and s["state"] == "running")
        pings = sum(f.op == 9 for f in c1.frames)
        assert roundtrip(c2, c1)
        assert c1.wait(lambda: sum(f.op == 9 for f in c1.frames) >= pings + 3, 5), "pinged again"
        assert not c1.eof
    finally:
        c1.abort()
        if c2:
            c2.close()


def test_a_blocked_paste_from_a_browser_that_went_away_is_dropped(md_editor, root):
    """A paste the agent is not reading blocks its browser's connection, which then goes away:
    a ping that cannot be sent drops it, and the rest of the paste is never typed."""
    srv = md_editor(agent=BASH, env={"MDEDIT_WS_PING": "0.3"})
    c1 = running(srv)
    c2 = None
    try:
        n = next(SEQ)
        since = c1.mark()
        c1.cmd(f"stty -echo; echo P{n}-$((1+1)); sleep 5; cat > paste.txt")
        assert c1.wait_out(f"P{n}-2", since)

        def paste():
            try:
                c1.send_bin(lines_of(2 << 20))
            except OSError:
                pass
        threading.Thread(target=paste, daemon=True).start()
        time.sleep(1)
        c1.abort()
        c2 = srv.ws()
        c2.wait_status()
        time.sleep(5)   # the sleep ends, and cat reads what was typed before the browser went
        c2.send_bin(b"\n\x04")
        c2.cmd("stty echo")
        assert roundtrip(c2)
        size = (root / "paste.txt").stat().st_size
        assert 0 < size < 1 << 20, f"{size} bytes of the 2 MiB paste were typed"
        assert "Traceback" not in srv.logtext()
    finally:
        c1.abort()
        if c2:
            c2.close()


def test_restart_from_another_client_frees_a_blocked_paste(srv):
    """A paste into an agent that never reads blocks only that connection, until the session
    is replaced."""
    c1 = running(srv)
    c2 = None
    try:
        bash = int(shell_value(c1, "$$"))
        c1.cmd("sleep 1000")
        time.sleep(0.3)
        threading.Thread(target=lambda: c1.send_bin(lines_of(3 << 20)), daemon=True).start()
        time.sleep(1)
        c2 = srv.ws()
        c2.wait_status()
        c2.send_json({"type": "restart"})
        assert c2.wait_status(lambda s: s["id"] == 2 and s["state"] == "running")
        assert gone_within([bash])
        assert roundtrip(c2, c1)
    finally:
        c1.abort()
        if c2:
            c2.close()


# ---------------------------------------------------------------- in-process units

def test_mode_tracking_and_prefix():
    modes = {}
    seqs = (b"\x1b[?1002h\x1b[?1000l\x1b[?9h"     # one mouse protocol: the last set, after a reset
            b"\x1b[?1;;2004h\x1b[?01h"             # several in one, an empty one, a leading zero
            b"\x1b[?2026h\x1b[?12h\x1b[?99999h"    # not restored: synchronised output, blinking, unknown
            b"\x1b[?1016h\x1b[?1006l"              # one mouse encoding
            b"\x1b[?7h\x1b[?25l\x1b[?47h\x1b[?1004h\x1b[?1004l"
            b"\x1b[1;2H\x1b[?5$p\x1b[?h")            # not mode changes
    S.track_modes(modes, seqs, len(seqs))
    assert modes == {"mouse": 9, "cursor-keys": 1, "paste": 2004, "mouse-encoding": 0, "wrap": 7, "cursor": 0,
                     "alt": 47, "focus": 0}
    assert S.mode_prefix(modes) == b"\x1b[?47h\x1b[?1h\x1b[?25l\x1b[?9h\x1b[?2004h", "alternate screen first"
    assert S.mode_prefix({"wrap": 0, "alt": 0, "mouse": 0}) == b"\x1b[?7l"
    assert S.mode_prefix({}) == b""
    modes = {}
    S.track_modes(modes, b"\x1b[?2004h\x1b[?1h", 8)   # only data[:end]
    assert modes == {"paste": 2004}


def test_trimming_the_scrollback_keeps_the_modes(monkeypatch):
    monkeypatch.setattr(S, "SCROLLBACK_MAX", 100)
    t = S.TermSession()
    t._append(b"a" * 10 + b"\x1b[?10")                 # a sequence split across two reads,
    t._append(b"49h\x1b[?2004h" + b"b" * 88)          # which the cut, 14 bytes in, would split
    assert t.scrollback == b"\x1b[?2004h" + b"b" * 88 and t.replay_modes == {"alt": 1049}
    t._append(b"\n" + b"c" * 10)                       # cut just after the newline
    assert t.scrollback == b"c" * 10
    assert S.mode_prefix(t.replay_modes) == b"\x1b[?1049h\x1b[?2004h"
    t._append(b"\x1b[?2004l\x1b[?25l" + b"d" * 200)
    assert t.scrollback == b"d" * 100 and S.mode_prefix(t.replay_modes) == b"\x1b[?1049h\x1b[?25l"
    t._append(b"\x1b[?1049l")                          # a cut that splits nothing
    assert t.scrollback == b"d" * 92 + b"\x1b[?1049l" and t.replay_modes == {"alt": 1049, "paste": 0, "cursor": 0}
