"""Ask Claude: `claude -p` through a claude-sandbox-compatible launcher (on a PTY, prompt in a
file in ROOT) or natively (pipes), with base sessions that cards fork. fakes/fake-sandbox
stands in for `uvx claude-sandbox@latest` and fakes/claude for `claude`; the fake's call log
shows what each call received."""
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from helpers import FAKES, gone_within

from md_collab_editor import server as S

LAUNCHER = str(FAKES / "fake-sandbox")
NATIVE = str(FAKES / "claude")
DOC = ("# Title\n\nFirst paragraph here, which is long enough to select.\n\nSecond paragraph.\n\n"
       + "".join(f"Line {i} of filler text so that small edits stay under the diff limit.\n" for i in range(60)))
SELECTED = "First paragraph here, which is long enough to select."


def ask(srv, doc=DOC, path="a.md", start=None, end=None, **kw):
    """POST /api/ask about `doc` (by default the first paragraph, replace mode, haiku)."""
    if start is None:
        start = doc.index(SELECTED)
        end = start + len(SELECTED)
    body = dict(path=path, doc=doc, start=start, end=end, mode="replace", model="haiku", instruction="Tighten")
    body.update(kw)
    return srv.post("/api/ask", body)


def echo(res):
    """The fake's digest of what a card's call received, from an /api/ask reply."""
    r = res.get("result", "")
    assert r.startswith("ECHO:"), res
    return json.loads(r[len("ECHO:"):])


def prompt_files(root):
    return [f for f in os.listdir(root) if f.startswith(".md-editor-ask-")]


def session_file(srv, sid):
    return next(Path(d) / f for d, _, fs in os.walk(srv.state / "sessions") for f in fs if f == sid + ".json")


@pytest.fixture
def launcher(md_editor):
    return md_editor(ask_agent=LAUNCHER)


@pytest.fixture
def native(md_editor):
    return md_editor(ask_agent=NATIVE)


# ---------------------------------------------------------------- configuration

def test_config_launcher(launcher):
    assert launcher.config()["ask"] == {"available": True, "cmd": LAUNCHER, "mode": "launcher", "reason": ""}
    assert f"Ask Claude: {LAUNCHER} (launcher)" in launcher.logtext()


def test_config_native_for_any_path_to_claude(md_editor):
    srv = md_editor(ask_agent=f"{NATIVE} --verbose")
    a = srv.config()["ask"]
    assert a["available"] and a["mode"] == "native" and a["cmd"] == f"{NATIVE} --verbose"


def test_ask_agent_none(md_editor):
    for value in ("none", "NONE", ""):
        srv = md_editor(ask_agent=value)
        assert srv.config()["ask"] == {"available": False, "cmd": None, "mode": None,
                                       "reason": "Ask Claude is turned off (--ask-agent none or MDEDIT_ASK_AGENT=none)."}
    status, res = ask(srv)
    assert status == 503 and "turned off" in res["error"]
    status, res = srv.post("/api/ask/prepare", {"path": "a.md", "doc": DOC})
    assert status == 503


@pytest.mark.parametrize("cmd, mode, hint", [
    ("no-such-launcher-xyz shell", "launcher", "pass --ask-agent with another command, or --ask-agent none"),
    ("/nonexistent/uvx claude-sandbox@latest", "launcher", "docs.astral.sh/uv/ or pass --ask-agent claude"),
    ("/nonexistent/claude", "native", "install Claude Code"),
])
def test_unknown_binary_gives_a_reason(md_editor, cmd, mode, hint):
    srv = md_editor(ask_agent=cmd)
    a = srv.config()["ask"]
    exe = cmd.split()[0]
    assert not a["available"] and a["mode"] == mode and a["cmd"] == cmd
    assert a["reason"].startswith(f"{exe} not found on the PATH: ") and hint in a["reason"], a
    status, res = ask(srv)
    assert status == 503 and res["error"] == a["reason"]
    assert f"Ask Claude unavailable: {a['reason']}" in srv.logtext()


def test_environment_variable_and_flag(md_editor):
    assert md_editor(ask_agent=None, env={"MDEDIT_ASK_AGENT": "none"}).config()["ask"]["available"] is False
    assert md_editor(ask_agent=None, env={"MDEDIT_ASK_AGENT": NATIVE}).config()["ask"]["mode"] == "native"
    assert md_editor(ask_agent=LAUNCHER, env={"MDEDIT_ASK_AGENT": "none"}).config()["ask"]["available"] is True


def test_default_is_claude_sandbox(md_editor):
    a = md_editor(ask_agent=None).config()["ask"]
    assert a["cmd"] == "uvx claude-sandbox@latest" and a["mode"] == "launcher"
    assert a["available"] == bool(shutil.which("uvx"))


# ---------------------------------------------------------------- launcher mode

def test_first_card_makes_a_base_then_forks_it(launcher, root):
    status, res = ask(launcher)
    assert status == 200, res
    assert res["forked"] is True and res["session"]
    bases = launcher.bases()
    assert len(bases) == 1
    base = bases[0]
    assert base["persisted"] and not base["parent"] and base["last_doc"]
    assert base["prompt"] == (f'<document path="a.md">\n{DOC}\n</document>\n\nThis is the document the user is '
                              "editing. Each later message is one independent request about it. Reply with just OK.")
    d = echo(res)
    assert d["parent"] == base["session"] and launcher.calls()[-1]["fork"], "card = --resume BASE --fork-session"
    assert d["n"] == 2 and d["doc_msgs"] == 1, "the card's history: the base's document, then its request"
    assert d["last_request"] and not d["last_doc"], "the card's own message holds no document"
    assert res["session"] == d["session"] != base["session"]
    assert d["tools"] == ["Skill,Read"] and d["allowed"] == ["Skill,Read"] and d["model"] == "haiku"
    # inside the "container": claude's stdin is a terminal, in the fixed folder
    assert d["tty"] is True and d["cwd"] == "/tmp/md-editor-ask"


def test_launcher_runs_on_a_pty_with_the_prompt_in_a_private_file(launcher, root):
    status, res = ask(launcher)
    assert status == 200, res
    runs = launcher.sandbox_calls()
    assert len(runs) == 2   # base + card
    for run in runs:
        assert run["tty"] is True, "podman exec -it needs a TTY"
        assert run["term"] == "dumb" and run["no_color"] == "1"
        assert run["claudecode"] is None and run["child_session"] is None, "not nested in md-editor's parent"
        assert run["shell"] == "sh", "the `shell` verb runs SCRIPT in sh, not the user's own shell"
        assert run["cwd"] == str(root)
        assert re.fullmatch(re.escape(str(root)) + r"/\.md-editor-ask-[0-9a-f]{32}\.txt", run["prompt_file"])
        assert run["mode"] == "600"
        assert run["script"].startswith("mkdir -p /tmp/md-editor-ask && cd /tmp/md-editor-ask && prompt=$(cat ")
        assert run["script"].endswith(' -- "$prompt"')
    assert len({r["prompt_file"] for r in runs}) == 2, "a new file per call"
    assert prompt_files(root) == []


def test_launcher_noise_crlf_and_escapes_are_tolerated(launcher):
    launcher.flag("noise")
    status, res = ask(launcher)
    assert status == 200 and res["forked"], res
    assert "\x1b" not in res["result"] and "\r" not in res["result"]
    assert echo(res)["session"] == res["session"], "the result line, not the noise, gives the session"


def test_request_format(launcher):
    ask(launcher)
    card = launcher.calls()[-1]["prompt"]
    line = DOC[:DOC.index(SELECTED)].count("\n") + 1
    assert card == "\n\n".join([
        '<request path="a.md">\n'
        f'<selection lines="{line}-{line}">\n{SELECTED}\n</selection>',
        "<context_before># Title\n\n</context_before>",
        f"<context_after>{DOC[DOC.index(SELECTED) + len(SELECTED):][:300]}</context_after>",
        "<mode>replace</mode>",
        "<instruction>\nTighten\n</instruction>",
        "Reply with the replacement text only.\n</request>"])
    ask(launcher, start=0, end=len(DOC), mode="comment", instruction="Critique?")
    card = launcher.calls()[-1]["prompt"]
    assert "<selection>The whole document is selected.</selection>" in card and "<context_before>" not in card
    assert "<mode>comment</mode>" in card and card.endswith("Reply with your comments only.\n</request>")
    ask(launcher, previous="An earlier attempt.", instruction="Shorter")
    card = launcher.calls()[-1]["prompt"]
    assert ("<previous_attempt>\nAn earlier attempt.\n</previous_attempt>\nThe user was not satisfied with the "
            "previous attempt; the instruction above is their feedback on it.") in card


def test_second_card_on_the_same_document_reuses_the_base(launcher):
    ask(launcher)
    status, res = ask(launcher, instruction="Simplify")
    assert status == 200 and res["forked"]
    assert len(launcher.bases()) == 1
    card = launcher.calls()[-1]
    assert card["parent"] == launcher.bases()[0]["session"]
    assert "<document" not in card["prompt"] and "Line 42 of filler" not in card["prompt"]
    assert len(card["prompt"]) < len(DOC) / 2


def test_concurrent_cards_wait_for_one_base(launcher):
    out = []
    threads = [threading.Thread(target=lambda: out.append(ask(launcher, path="c.md"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 4 and all(s == 200 and r["forked"] for s, r in out), out
    bases = launcher.bases()
    assert len(bases) == 1
    assert {echo(r)["parent"] for _, r in out} == {bases[0]["session"]}
    assert len({r["session"] for _, r in out}) == 4


def test_small_edit_sends_a_diff(launcher):
    ask(launcher)
    edited = DOC.replace("Line 30 of filler", "Line 30 (edited) of filler")
    status, res = ask(launcher, doc=edited)
    assert status == 200 and res["forked"]
    assert len(launcher.bases()) == 1
    card = launcher.calls()[-1]["prompt"]
    assert card.startswith('<request path="a.md">\n<document_update>The document has changed since the version '
                           "above. Unified diff from that version to the current one:\n<diff>\n--- a/a.md\n+++ b/a.md\n@@")
    assert "-Line 30 of filler" in card and "+Line 30 (edited) of filler" in card
    assert card.index("</document_update>") < card.index("<selection")
    assert "Line 5 of filler" not in card, "only the changed hunk, not the whole document"


def test_large_edit_makes_a_new_base(launcher):
    ask(launcher)
    rewritten = f"# Rewritten\n\n{SELECTED}\n\n" + "Different text.\n" * 40
    status, res = ask(launcher, doc=rewritten)
    assert status == 200 and res["forked"]
    bases = launcher.bases()
    assert len(bases) == 2 and "Different text." in bases[1]["prompt"]
    d = echo(res)
    assert d["parent"] == bases[1]["session"] and not d["last_update"]


def test_retry_and_refine_fork_the_cards_own_session(launcher):
    status, res = ask(launcher)
    card = res["session"]
    before = session_file(launcher, card).read_text()
    status, res2 = ask(launcher, session=card, previous="Attempt one.", instruction="Shorter")
    d = echo(res2)
    assert status == 200 and res2["forked"]
    assert d["parent"] == card and launcher.calls()[-1]["fork"], "--resume CARD --fork-session"
    assert d["n"] == 3 and d["doc_msgs"] == 1, "document, first request, this one"
    assert res2["session"] not in (None, card)
    assert session_file(launcher, card).read_text() == before, "a retry never changes the card's session"
    status, res3 = ask(launcher, session=res2["session"], previous="Attempt two.", instruction="Even shorter")
    assert echo(res3)["parent"] == res2["session"] and echo(res3)["n"] == 4
    edited = DOC.replace("Line 10 of filler", "Line 10 (tweaked) of filler")
    status, res4 = ask(launcher, doc=edited, session=res3["session"], previous="Attempt three.", instruction="Again")
    d4 = echo(res4)
    assert d4["parent"] == res3["session"] and d4["last_update"], "after a small edit: the card's session + a diff"
    assert len(launcher.bases()) == 1


def test_unknown_session_forks_the_base(launcher):
    """A session the server doesn't know (e.g. from before a restart) isn't resumed: the request
    goes to a fork of the document's base session, with the previous attempt in it."""
    status, res = ask(launcher, session="00000000-0000-0000-0000-000000000000", previous="x", instruction="y")
    assert status == 200 and res["forked"]
    d = echo(res)
    assert d["parent"] == launcher.bases()[0]["session"]
    assert "<previous_attempt>\nx\n</previous_attempt>" in launcher.calls()[-1]["prompt"]


def test_failed_card_session_falls_back_to_one_stateless_call(launcher):
    status, res = ask(launcher)
    session_file(launcher, res["session"]).unlink()   # claude no longer has it
    n = len(launcher.calls())
    status, res2 = ask(launcher, session=res["session"], previous="x", instruction="y")
    assert status == 200 and res2["forked"] is False and res2["session"] is None
    calls = launcher.calls()[n:]
    assert len(calls) == 1, "the failed resume never reached the log; then one stateless call"
    last = calls[0]
    assert "--no-session-persistence" in last["argv"] and "--resume" not in last["argv"]
    assert last["prompt"].startswith(f'<document path="a.md">\n{DOC}\n</document>\n\n<request path="a.md">\n')
    assert echo(res2)["n"] == 1
    assert re.search(r"\[ask\] fork failed, stateless: .*No conversation found", launcher.logtext())


def test_failed_base_fork_is_dropped(launcher):
    ask(launcher)
    launcher.flag("fail-resume")
    status, res = ask(launcher)
    assert status == 200 and res["forked"] is False and echo(res)["last_doc"]
    launcher.flag("fail-resume", False)
    status, res = ask(launcher)
    assert res["forked"] and len(launcher.bases()) == 2, "the next card makes a new base"


def test_system_prompt_is_the_same_for_every_call(launcher):
    ask(launcher)
    launcher.flag("fail-resume")
    ask(launcher)
    calls = launcher.calls()
    assert {("base" if c["base"] else "fork" if c["fork"] else "stateless") for c in calls} == \
        {"base", "fork", "stateless"}
    want = S.claude_args("haiku")[S.claude_args("haiku").index("--append-system-prompt") + 1]
    assert {c["sys"] for c in calls} == {hashlib.sha256(want.encode()).hexdigest()[:10]}


def test_prepare_makes_the_base_early(launcher):
    status, res = launcher.post("/api/ask/prepare", {"path": "a.md", "doc": DOC, "model": "haiku"})
    assert status == 200 and res == {"ok": True}
    assert len(launcher.bases()) == 1 and len(launcher.calls()) == 1
    status, res = launcher.post("/api/ask/prepare", {"path": "a.md", "doc": DOC, "model": "haiku"})
    assert status == 200 and len(launcher.calls()) == 1, "nothing to do for the same document"
    status, res = ask(launcher)
    assert res["forked"] and echo(res)["parent"] == launcher.bases()[0]["session"]
    assert len(launcher.bases()) == 1
    status, res = launcher.post("/api/ask/prepare", {"path": "a.md", "doc": DOC, "model": "sonnet"})
    assert len(launcher.bases()) == 2, "one base per model"


def test_claude_error_is_502(launcher):
    launcher.flag("is-error")
    status, res = ask(launcher)
    assert status == 502 and "API Error: fake failure" in res["error"]


def test_launcher_failure_is_502_with_its_output(launcher, root):
    launcher.flag("broken-launcher")
    status, res = ask(launcher)
    assert status == 502
    assert 'no container with name or ID "md-editor-fake" found' in res["error"]
    assert "\x1b" not in res["error"] and "\r" not in res["error"]
    assert prompt_files(root) == []


def test_too_large_for_the_sandbox(launcher, root):
    status, res = ask(launcher, doc="word " * 30000, start=0, end=4, path="big.md")
    assert status == 502 and "too large" in res["error"]
    assert launcher.calls() == [] and prompt_files(root) == []


def test_unwritable_root(launcher, root):
    shutil.rmtree(root)
    status, res = ask(launcher)
    assert status == 502 and "needs to write a temporary file in" in res["error"], res


def test_ask_needs_a_matching_origin(launcher):
    status, res = launcher.post("/api/ask", {"path": "a.md", "doc": DOC, "start": 0, "end": 5}, origin=None)
    assert status == 403 and res["error"].startswith("forbidden")
    status, res = launcher.post("/api/ask/prepare", {"path": "a.md", "doc": DOC}, origin="http://evil.example")
    assert status == 403
    assert launcher.calls() == []


@pytest.mark.parametrize("agent", [LAUNCHER, NATIVE], ids=["launcher", "native"])
def test_timeout_is_504_and_kills_claude(md_editor, root, agent):
    srv = md_editor(ask_agent=agent, env={"MDEDIT_ASK_TIMEOUT": "2"})
    srv.flag("hang")
    t0 = time.monotonic()
    status, res = ask(srv)
    assert status == 504 and res["error"] == "Claude timed out"
    assert time.monotonic() - t0 < 10
    assert prompt_files(root) == []
    # the md_editor fixture checks that the hung fake is gone once the server stops


def test_stopping_md_editor_ends_a_running_call(md_editor, root):
    """The request's thread dies with the server, so the server itself must end the launcher
    (which here ignores the PTY hang-up) and delete the prompt file, and must not start the
    stateless fallback on the way out."""
    srv = md_editor(ask_agent=LAUNCHER)
    srv.flag("hang")

    def call():
        try:
            ask(srv)
        except OSError:   # the server went away mid-request
            pass
    threading.Thread(target=call, daemon=True).start()
    deadline = time.monotonic() + 10
    while not (srv.state / "hanging").exists():
        assert time.monotonic() < deadline, "the call never started"
        time.sleep(0.05)
    srv.stop()
    assert prompt_files(root) == []
    assert len(srv.sandbox_calls()) == 1, "no new call after shutdown began"
    assert srv.leftovers(1) == []


# ---------------------------------------------------------------- native mode

def test_native_mode_uses_pipes_in_a_fixed_folder(native, root):
    assert native.config()["ask"] == {"available": True, "cmd": NATIVE, "mode": "native", "reason": ""}
    status, res = ask(native)
    assert status == 200 and res["forked"], res
    d = echo(res)
    assert d["n"] == 2 and d["tty"] is False
    tmp = native.logpath.parent / "tmp"   # the server's TMPDIR
    assert os.path.realpath(d["cwd"]) == os.path.realpath(tmp / "md-editor-ask")
    assert "--" not in native.calls()[-1]["argv"], "the prompt goes on stdin"
    assert native.sandbox_calls() == [] and prompt_files(root) == []
    status, res2 = ask(native, session=res["session"], previous="x", instruction="Shorter")
    assert echo(res2)["parent"] == res["session"]


# ---------------------------------------------------------------- in-process units

def test_doc_diff():
    assert S.doc_diff(DOC, DOC, "a.md") == ""
    d = S.doc_diff(DOC, DOC.replace("Line 5 ", "Line five "), "a.md")
    assert d.startswith("--- a/a.md\n+++ b/a.md\n@@") and "+Line five " in d
    assert S.doc_diff(DOC, "x" + DOC[len(DOC) // 2:], "a.md") is None, "over 30% of the document"
    lines = [f"line {i:05d} {'x' * 50}\n" for i in range(3000)]
    changed = lines[:1000] + [ln.upper() for ln in lines[1000:1400]] + lines[1400:]
    assert S.doc_diff("".join(lines), "".join(changed), "a.md") is None, "under 30% but over 40k characters"
    filler = "".join(f"filler line {i}\n" for i in range(50))
    assert S.doc_diff(filler + "a\nb", filler + "a\nc", "x").endswith("-b\n+c\n")


def test_build_request_line_numbers_and_context():
    doc = "one\ntwo\nthree\nfour\n"
    r = S.build_request({"doc": doc, "start": doc.index("two"), "end": doc.index("four"), "path": "p.md"})
    assert '<selection lines="2-3">\ntwo\nthree\n\n</selection>' in r
    r = S.build_request({"doc": doc, "start": 0, "end": 3, "path": "p.md"})
    assert '<selection lines="1-1">' in r and "<context_before></context_before>" in r
    big = "x" * 1000 + "SEL" + "y" * 1000
    r = S.build_request({"doc": big, "start": 1000, "end": 1003})
    assert f"<context_before>{'x' * 300}</context_before>" in r and f"<context_after>{'y' * 300}</context_after>" in r


def test_parse_result_and_escape_stripping():
    raw = ("\x1b]0;title\x07\x1b[?25l\x1b[1;34mnoise\x1b[0m\r\n"
           '{"type":"result","is_error":false,"result":"hi","session_id":"s"}\x1b[?25h\r\n\x1b(B')
    text = S.CTRL_RE.sub("", S.ANSI_RE.sub("", raw).replace("\r", ""))
    assert "\x1b" not in text and "noise" in text and "title" not in text
    assert S.parse_result(text)["result"] == "hi"
    assert S.parse_result('x\n{"is_error": true, "result": "bad"}\n')["result"] == "bad"
    assert S.parse_result('{"type":"result","result":"a"}\n{"result":"b"}\n')["result"] == "a"
    assert S.parse_result("Error: podman not found\n{not json\n") is None
    assert S.parse_result('{"type":"result","result":"z"}junk')["result"] == "z"


def test_base_sessions_one_per_key_and_at_most_32(monkeypatch):
    monkeypatch.setattr(S, "ASK_BASES", collections.OrderedDict())
    monkeypatch.setattr(S, "ASK_KEY_LOCKS", {})
    calls = []

    def fake_run(args, prompt):
        calls.append((args, prompt))
        time.sleep(0.2 if len(calls) == 1 else 0)
        return {"session_id": f"s{len(calls)}", "result": "OK", "usage": {}}
    monkeypatch.setattr(S, "run_claude", fake_run)
    key = ("cmd", "/r", "p.md", "")
    out = []
    threads = [threading.Thread(target=lambda: out.append(S.base_session(key, DOC))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and out == [("s1", "")] * 6
    base_args = calls[0][0]
    assert "--no-session-persistence" not in base_args and "--resume" not in base_args
    assert base_args[base_args.index("--append-system-prompt") + 1] == S.SYSTEM_PROMPT
    for i in range(40):
        S.base_session(("cmd", "/r", f"f{i}.md", ""), DOC)
    assert len(S.ASK_BASES) == 32
    assert ("cmd", "/r", "f0.md", "") not in S.ASK_BASES and ("cmd", "/r", "f39.md", "") in S.ASK_BASES
    assert len(S.ASK_KEY_LOCKS) <= 33
    S.forget_session(S.ASK_BASES[("cmd", "/r", "f39.md", "")]["session"])
    assert ("cmd", "/r", "f39.md", "") not in S.ASK_BASES


def test_read_pty_timeout_and_stragglers(tmp_path):
    proc, fd = S.spawn_pty(["sh", "-c", "echo started; sleep 30"], str(tmp_path), S.child_env(), 80, 24)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        S.read_pty(proc, fd, 1)
    assert time.monotonic() - t0 < 6 and b"started" in exc.value.output
    assert proc.poll() is not None
    with pytest.raises(OSError):
        os.killpg(proc.pid, 0)
    pidfile = tmp_path / "straggler.pid"
    proc, fd = S.spawn_pty(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; echo done"], str(tmp_path),
                           S.child_env(), 80, 24)
    t0 = time.monotonic()
    out = S.read_pty(proc, fd, 20)
    assert b"done" in out and time.monotonic() - t0 < 5, "returns ~1 s after the child exits"
    assert gone_within([int(pidfile.read_text())]), "the straggler holding the PTY is killed with the group"


def test_launcher_needs_a_pty(monkeypatch):
    monkeypatch.setattr(S, "ASK_CMD", ["uvx", "claude-sandbox@latest"])
    monkeypatch.setattr(S, "HAVE_PTY", False)
    a = S.ask_info()
    assert not a["available"] and "No PTY support" in a["reason"]
    monkeypatch.setattr(S, "ASK_CMD", ["claude"])
    assert S.ask_info()["mode"] == "native" and "PTY" not in S.ask_info()["reason"]
