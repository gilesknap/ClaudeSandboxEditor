"""The editor's side of the one Claude session, in headless Chromium (Playwright): Ask Claude
sends its question to the terminal, the selection reaches Claude Code over md-editor's IDE link,
Send to Claude terminal becomes an at-mention, and Claude's proposed edits open as diff tabs to
accept or reject.

A stand-in `claude` (bash, but named claude, so md-editor runs it natively and opens its IDE
link on a loopback TCP port) takes Claude Code's place in the terminal, and FakeIde plays Claude
Code's side of the link as the recorded CLI frames show it: it reads the lock file md-editor
writes, connects with its token and records what md-editor sends. Skipped like test_e2e.py when
Playwright or its Chromium is missing (MD_EDITOR_E2E=require makes that fail)."""
import json
import os
import re
import signal
import threading
import time

import pytest

from gitutil import SERVER_ENV
from helpers import WS, marked_pids, needs_linux

REQUIRE = os.environ.get("MD_EDITOR_E2E") == "require"
try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    if REQUIRE:
        raise
    pytest.skip("Playwright is not installed (it needs Python 3.10+)", allow_module_level=True)

pytestmark = [pytest.mark.e2e, needs_linux]

DOC = "# Session\n\nFirst paragraph here, long enough to select.\n\nSecond paragraph.\n"
PROMPT = r"bash-[\d.]+[$#]"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(chromium_sandbox=False)
        except PlaywrightError as exc:
            if REQUIRE:
                raise
            pytest.skip(f"Playwright's Chromium is not installed: {exc.message.splitlines()[0]}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    """Claude Code's config folder for the server (CLAUDE_CONFIG_DIR): its ide/ holds the lock."""
    return tmp_path_factory.mktemp("claude-config")


def fake_claude(d):
    """bash, called claude (so md-editor runs it natively, with the IDE link on TCP)."""
    d.mkdir(parents=True, exist_ok=True)
    claude = d / "claude"
    claude.write_text("#!/bin/sh\nexec bash --norc --noprofile -i\n", encoding="utf-8")
    claude.chmod(0o755)
    return str(claude)


def server_env(cfg):
    # bash draws no Claude Code prompt, so an ask in a session just started waits until its
    # output has been quiet for MDEDIT_START_QUIET: short here
    return dict(SERVER_ENV, HISTFILE=os.devnull, CLAUDE_CONFIG_DIR=str(cfg), MDEDIT_IDE_LINK="auto",
                MDEDIT_START_QUIET="0.5")


@pytest.fixture(scope="module")
def srv(shared_servers, tmp_path_factory, cfg):
    return shared_servers.start(agent=fake_claude(tmp_path_factory.mktemp("bin")), env=server_env(cfg))


@pytest.fixture(autouse=True)
def doc(srv):
    (srv.root / "a.md").write_text(DOC, encoding="utf-8")
    return srv.root / "a.md"


def new_page(browser, srv, width=1400, prompt=True):
    """The editor on a.md in a fresh browser profile (and so its context), autosave off (so an
    edit stays unsaved); with `prompt`, once a.md and the terminal's prompt are on screen. pg.sent holds
    the text frames the page sent on its WebSockets, as JSON, pg.errors what it logged as
    errors, and the page's sockets are in window.__sockets."""
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    ctx.add_init_script("""try { localStorage.setItem('mdedit.autosave.md', '0'); } catch {}
        window.__sockets = [];
        const WS = window.WebSocket;
        window.WebSocket = class extends WS { constructor(...a) { super(...a); window.__sockets.push(this); } };""")
    pg = ctx.new_page()
    errors, sent = [], []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("websocket", lambda ws: ws.on("framesent", lambda p: sent.append(json.loads(p)) if isinstance(p, str) else None))
    pg.sent, pg.errors = sent, errors
    pg.goto(srv.base + "#a.md")
    if prompt:
        expect(pg.locator("#doc-name")).to_have_text("a.md")
        wait_term_line(pg, PROMPT + ".*")
    else:   # a proposal waiting for an answer may be on screen instead
        expect(pg.locator(".etab", has=pg.locator(".etab-name", has_text=re.compile(r"^a\.md$")))).to_have_count(1)
    return pg


@pytest.fixture
def page(browser, srv):
    """The editor on a.md, the terminal's prompt on screen (new_page); it must log no errors."""
    pg = new_page(browser, srv)
    yield pg
    pg.context.close()
    assert not pg.errors, pg.errors


@pytest.fixture
def ide(page, cfg):
    """Claude Code's side of the link, connected once the page's session is running."""
    fake = FakeIde(cfg)
    yield fake
    fake.close()


def wait_term_line(pg, pattern, timeout=10000):
    """Wait until a terminal row matches `pattern` in full."""
    pg.wait_for_function(
        """re => [...document.querySelectorAll('#term .xterm-rows > div')]
                 .some(d => new RegExp(re).test(d.textContent.replace(/\\u00a0/g, ' ').trimEnd()))""",
        arg=f"^{pattern}$", timeout=timeout)


def term_text(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('#term .xterm-rows > div')]
                              .map(d => d.textContent.replace(/\\u00a0/g, ' ').trimEnd()).join('\\n')""")


def until(pred, timeout=10, what="condition", pg=None):
    """Poll pred until it is true. With a page, wait through Playwright, which only takes in
    the page's events (page.sent) while one of its calls runs."""
    deadline = time.monotonic() + timeout
    while True:
        r = pred()
        if r:
            return r
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        if pg is not None:
            pg.wait_for_timeout(50)
        else:
            time.sleep(0.05)


def select_lines(pg, a, b):
    """Select lines a..b (0-based, whole lines) in the main editor, as the mouse would."""
    pg.evaluate("([a, b]) => { App.cm.focus(); App.cm.setSelection({line: a, ch: 0}, {line: b + 1, ch: 0}); }", [a, b])


class FakeIde:
    """Claude Code's end of md-editor's IDE link (MCP over a WebSocket), from the frames the real
    CLI sent (initialize, notifications/initialized, ide_connected, tools/list). It answers
    md-editor's pings, unless told to hold them (hold_pings), and records every message."""

    def __init__(self, cfg, timeout=15):
        locks = until(lambda: sorted((cfg / "ide").glob("*.lock")), timeout, "the IDE lock file")
        self.lock = locks[-1]
        info = json.loads(self.lock.read_text(encoding="utf-8"))
        self.token, self.port = info["authToken"], int(self.lock.stem)
        self.ws = WS(self.port, origin=None, path="/", headers={
            "X-Claude-Code-Ide-Authorization": self.token, "Sec-WebSocket-Protocol": "mcp",
            "User-Agent": "claude-code/2.1.280 (cli)"})
        assert self.ws.code == 101, (self.ws.status_line, self.ws.body)
        self.msgs = []          # (time, message) from md-editor, in order
        self.replies = []       # (time, id) of the pings answered
        self.hold_pings = False
        self.held = []
        self.ids = iter(range(10**6))
        self.mu = threading.Lock()
        self.closed = False
        threading.Thread(target=self._pump, daemon=True).start()
        init = self.result(self.call("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {"roots": {"listChanged": True}, "elicitation": {}},
            "clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "2.1.280"}}))
        self.instructions = init.get("instructions", "")
        self.notify("notifications/initialized")
        self.notify("ide_connected", {"pid": os.getpid()})
        self.tools = [t["name"] for t in self.result(self.call("tools/list"))["tools"]]

    def _pump(self):
        seen = 0
        while not self.closed:
            self.ws.wait(lambda: len(self.ws.frames) > seen or self.ws.eof, timeout=0.5)
            frames = self.ws.frames[seen:]
            seen += len(frames)
            for f in frames:
                if f.op != 1:
                    continue
                m = json.loads(f.payload)
                with self.mu:
                    self.msgs.append((time.monotonic(), m))
                if m.get("method") == "ping" and "id" in m:
                    if self.hold_pings:
                        self.held.append(m["id"])
                    else:
                        self.pong(m["id"])
            if self.ws.eof:
                return

    def pong(self, rid):
        self.ws.send_json({"jsonrpc": "2.0", "id": rid, "result": {}})
        with self.mu:
            self.replies.append((time.monotonic(), rid))

    def release_pings(self):
        self.hold_pings = False
        held, self.held = self.held, []
        for rid in held:
            self.pong(rid)

    def notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self.ws.send_json(m)

    def call(self, method, params=None):
        """Send a request; returns its id (see result)."""
        rid = 1000 + next(self.ids)
        m = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            m["params"] = params
        self.ws.send_json(m)
        return rid

    def response(self, rid):
        with self.mu:
            return next((m for _, m in self.msgs if m.get("id") == rid and "method" not in m), None)

    def result(self, rid, timeout=10):
        r = until(lambda: self.response(rid), timeout, f"md-editor's answer to request {rid}")
        assert "result" in r, r
        return r["result"]

    def texts(self, rid, timeout=10):
        return [c["text"] for c in self.result(rid, timeout)["content"]]

    def tool(self, name, args):
        return self.call("tools/call", {"name": name, "arguments": args, "_meta": {"progressToken": 1}})

    def notes(self, method):
        with self.mu:
            return [m["params"] for _, m in self.msgs if m.get("method") == method]

    def last_selection(self):
        s = self.notes("selection_changed")
        return s[-1] if s else None

    def close(self):
        self.closed = True
        self.ws.close()


# ---------------------------------------------------------------- without the link

def test_ask_types_the_reference_and_question_without_the_link(page, doc):
    """Nothing is connected to the link, so the server types "@a.md#L3 question" and Enter into
    the terminal. An unsaved file is saved first, so Claude reads what is on screen."""
    expect(page.locator("#term-ide")).to_have_text("IDE link…")
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+End")
    page.keyboard.type("Unsaved line.")
    expect(page.locator(".etab.active")).to_have_class(re.compile(r"\bdirty\b"))
    page.locator(".CodeMirror-line", has_text="First paragraph").click(click_count=3)
    expect(page.locator("#ask-pill")).to_be_visible()
    page.locator("#ask-pill").click()
    expect(page.locator("#askbar")).to_be_visible()
    page.locator("#ask-input").fill("Why so short?")
    page.keyboard.press("Enter")
    expect(page.locator("#toast")).to_have_text("Saved a.md first, so that Claude reads what you see.")
    assert doc.read_text(encoding="utf-8").endswith("Second paragraph.\nUnsaved line.")
    wait_term_line(page, PROMPT + r" @a\.md#L3 Why so short\?")
    wait_term_line(page, r"bash: @a\.md#L3: command not found")   # Enter ran it
    asks = [m for m in page.sent if m.get("type") == "ask"]
    assert asks == [{"type": "ask", "text": "Why so short?", "path": "a.md",
                     "start": {"line": 2, "character": 0}, "end": {"line": 3, "character": 0}}]
    page.wait_for_function("() => !!document.activeElement.closest('#term')")
    expect(page.locator("#askbar")).to_be_hidden()


def test_a_preset_about_the_whole_file_names_just_the_file(page):
    page.evaluate("() => { App.cm.focus(); App.cm.setCursor({line: 0, ch: 0}); }")
    page.keyboard.press("Control+j")
    expect(page.locator("#ask-input")).to_have_attribute("placeholder", re.compile("whole file"))
    page.locator("#ask-presets").get_by_role("button", name="Critique", exact=True).click()
    wait_term_line(page, PROMPT + r" @a\.md Critique this passage:.*", timeout=10000)
    ask = [m for m in page.sent if m.get("type") == "ask"][-1]
    assert ask["path"] == "a.md" and "start" not in ask and ask["text"].startswith("Critique this passage")


def test_selection_messages_follow_the_editor_and_the_preview(page):
    def sels():
        return [m for m in page.sent if m.get("type") == "selection"]

    select_lines(page, 2, 2)
    until(lambda: sels() and sels()[-1]["start"] == {"line": 2, "character": 0}, what="the selection", pg=page)
    assert sels()[-1] == {"type": "selection", "path": "a.md", "start": {"line": 2, "character": 0},
                          "end": {"line": 3, "character": 0}, "text": "First paragraph here, long enough to select.\n"}
    # debounced: a burst of moves sends one message
    n = len(sels())
    page.evaluate("() => { for (let i = 0; i < 20; i++) App.cm.setCursor({line: 4, ch: i % 5}); }")
    until(lambda: len(sels()) > n and sels()[-1]["start"]["line"] == 4, what="the cursor", pg=page)
    page.wait_for_timeout(400)
    assert len(sels()) == n + 1
    assert sels()[-1]["start"] == sels()[-1]["end"] and sels()[-1]["text"] == ""
    # a selection in the preview is mapped to the source
    page.evaluate("""() => {
        const p = [...document.querySelectorAll('#preview p')].find(e => e.textContent.includes('Second'));
        const r = document.createRange();
        r.selectNodeContents(p);
        getSelection().removeAllRanges();
        getSelection().addRange(r);
        document.querySelector('#preview-pane').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    }""")
    until(lambda: sels()[-1]["start"]["line"] == 4 and sels()[-1]["text"].startswith("Second paragraph"),
          what="the preview's selection", pg=page)
    page.keyboard.press("Escape")   # the ask bar that the preview selection opened


# ---------------------------------------------------------------- over the link

def test_the_selection_reaches_claude_code_over_the_link(page, ide, srv):
    assert "executeCode" not in ide.tools and "openDiff" in ide.tools
    expect(page.locator("#term-ide")).to_have_text("IDE linked")
    expect(page.locator("#term-ide")).to_have_attribute("data-state", "connected")
    select_lines(page, 2, 2)
    path = str(srv.root / "a.md")
    s = until(lambda: (lambda x: x if x and x.get("text", "").startswith("First") else None)(ide.last_selection()),
              what="selection_changed")
    assert s["filePath"] == path
    assert s["selection"]["start"] == {"line": 2, "character": 0} and s["selection"]["end"] == {"line": 3, "character": 0}
    assert s["text"] == "First paragraph here, long enough to select.\n"
    # nothing selected: just the file
    page.evaluate("() => App.cm.setCursor({line: 0, ch: 2})")
    s = until(lambda: (lambda x: x if x and x["text"] == "" else None)(ide.last_selection()), what="the empty selection")
    assert s["filePath"] == path and s["selection"]["start"] == s["selection"]["end"] == {"line": 0, "character": 2}


def test_ask_over_the_link_sends_the_selection_then_types_the_question(page, ide):
    select_lines(page, 2, 2)
    until(lambda: (ide.last_selection() or {}).get("text", "").startswith("First"), what="the selection")
    ide.hold_pings = True
    page.keyboard.press("Control+j")
    page.locator("#ask-input").fill("What does this say?")
    page.keyboard.press("Enter")
    until(lambda: ide.held, what="md-editor's ping")
    page.wait_for_timeout(500)
    assert "What does this say?" not in term_text(page), "typed before the ping came back"
    sel_at = max(t for t, m in ide.msgs if m.get("method") == "selection_changed")
    ping_at = max(t for t, m in ide.msgs if m.get("method") == "ping")
    assert sel_at <= ping_at
    ide.release_pings()
    wait_term_line(page, PROMPT + r" What does this say\?")   # no @reference: the link carried it
    assert ide.last_selection()["text"].startswith("First paragraph")


def test_send_to_claude_terminal_is_an_at_mention_over_the_link(page, ide, srv):
    select_lines(page, 0, 2)
    page.keyboard.press("Control+Alt+l")
    m = until(lambda: ide.notes("at_mentioned"), what="at_mentioned")[-1]
    assert m == {"filePath": str(srv.root / "a.md"), "lineStart": 0, "lineEnd": 2}
    page.wait_for_function("() => !!document.activeElement.closest('#term')")
    page.wait_for_timeout(300)
    assert "@a.md#L1-3" not in term_text(page)   # Claude Code puts it in its prompt itself


# ---------------------------------------------------------------- proposed edits

PROPOSED = DOC.replace("Second paragraph.", "Second paragraph, as Claude would have it.")


def open_diff(ide, path, contents, name="a.md"):
    tab_name = f"✻ [Claude Code] {name} (abc123) ⧉"
    return ide.tool("openDiff", {"old_file_path": str(path), "new_file_path": str(path),
                                 "new_file_contents": contents, "tab_name": tab_name}), tab_name


def proposal_tab(pg):
    return pg.locator(".etab", has=pg.locator(".etab-name", has_text="a.md (proposed)"))


def file_tab(pg):
    return pg.locator(".etab", has=pg.locator(".etab-name", has_text=re.compile(r"^a\.md$")))


def right_side(pg):
    return "document.querySelector('.proposal-tab .CodeMirror-merge-editor .CodeMirror').CodeMirror"


def test_accepting_a_proposed_edit_hands_back_the_edited_text(page, ide, doc):
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(proposal_tab(page)).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(".proposal-tab .CodeMirror-merge")).to_be_visible()
    expect(page.locator(".proposal-tab .diff-stats")).to_have_text("+1 −1")
    # the file's own tab is read-only under a banner while Claude waits
    file_tab(page).click()
    expect(page.locator("#banner")).to_contain_text("Claude has proposed a change to a.md")
    assert page.evaluate("() => App.cm.getOption('readOnly')") is True
    page.locator("#banner").get_by_role("button", name="Show the proposed change").click()
    expect(proposal_tab(page)).to_have_class(re.compile(r"\bactive\b"))
    # the right-hand side can be edited before accepting
    page.evaluate(f"() => {{ const ed = {right_side(page)}; ed.replaceRange('Added in the diff.\\n', {{line: 6, ch: 0}}); ed.focus(); }}")
    page.keyboard.press("Control+Enter")
    assert ide.texts(rid) == ["FILE_SAVED", PROPOSED + "Added in the diff.\n"]
    expect(proposal_tab(page)).to_have_count(0)
    assert doc.read_text(encoding="utf-8") == DOC   # Claude writes the file, not md-editor
    assert ide.texts(ide.tool("close_tab", {"tab_name": tab_name})) == ["TAB_CLOSED"]   # as the CLI does after a decision
    # Claude writes it: the editor follows, and the file can be edited again
    doc.write_text(PROPOSED + "Added in the diff.\n", encoding="utf-8")
    file_tab(page).click()
    page.wait_for_function("() => App.cm.getValue().includes('Added in the diff.')")
    page.wait_for_function("() => !App.cm.getOption('readOnly')")
    expect(page.locator("#banner")).to_be_hidden()


def test_rejecting_or_closing_a_proposed_edit_says_no(page, ide, doc):
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(proposal_tab(page)).to_be_visible()
    page.locator(".proposal-tab").get_by_role("button", name="Reject").click()
    assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
    expect(proposal_tab(page)).to_have_count(0)
    # closing the tab is a rejection too (never TAB_CLOSED, which would accept it)
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(proposal_tab(page)).to_have_class(re.compile(r"\bactive\b"))
    proposal_tab(page).locator(".etab-close").click()
    assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
    # Accept is off while the right side is the file as it is
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(page.locator(".proposal-tab .CodeMirror-merge")).to_be_visible()
    page.evaluate(f"() => {right_side(page)}.setValue(App.cm.getValue())")
    expect(page.locator(".proposal-tab").get_by_role("button", name="Accept")).to_be_disabled()
    page.keyboard.press("Alt+w")
    assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
    assert doc.read_text(encoding="utf-8") == DOC
    assert page.evaluate("() => App.cm.getOption('readOnly')") is False


def test_a_proposal_answered_in_the_terminal_closes_and_one_waiting_survives_a_reload(page, ide, doc):
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(proposal_tab(page)).to_be_visible()
    # the user answered Claude Code's own prompt: it closes the tab
    assert ide.texts(ide.tool("close_tab", {"tab_name": tab_name})) == ["TAB_CLOSED"]
    assert ide.texts(rid) == ["TAB_CLOSED"]
    expect(proposal_tab(page)).to_have_count(0)
    # a waiting proposal is shown again after a reload (the server holds it)
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(proposal_tab(page)).to_be_visible()
    page.reload()
    expect(proposal_tab(page)).to_be_visible(timeout=15000)
    page.locator(".proposal-tab").get_by_role("button", name="Reject").click()
    assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
    # a file outside the terminal's folder is refused without asking
    rid, _ = open_diff(ide, "/etc/hostname", "nope\n", name="hostname")
    r = until(lambda: ide.response(rid), what="the refusal")
    assert "error" in r or r["result"].get("isError") or r["result"]["content"][0]["text"] == "DIFF_REJECTED", r
    page.wait_for_timeout(300)
    expect(page.locator(".proposal-tab")).to_have_count(0)


def test_a_proposal_waits_for_an_answer_before_any_ask(page, ide, doc):
    """Enter would answer Claude Code's own question about the edit ("1. Yes"), so an ask is
    refused while a proposal waits; a new file's proposal says so."""
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(proposal_tab(page)).to_be_visible()
    file_tab(page).click()
    page.evaluate("() => { App.cm.focus(); App.cm.setCursor({line: 0, ch: 0}); }")
    page.keyboard.press("Control+j")
    page.locator("#ask-input").fill("What is two plus two?")
    page.keyboard.press("Enter")
    expect(page.locator("#toast")).to_contain_text("waiting for your answer to its proposed change to a.md")
    page.wait_for_timeout(300)
    assert "two plus two" not in term_text(page)
    assert ide.response(rid) is None
    proposal_tab(page).click()
    page.locator(".proposal-tab").get_by_role("button", name="Reject").click()
    assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
    # a new file
    new = doc.parent / "brand-new.md"
    rid, tab_name = open_diff(ide, new, "# Brand new\n", name="brand-new.md")
    tab = page.locator(".etab", has=page.locator(".etab-name", has_text="brand-new.md (proposed)"))
    expect(tab).to_be_visible()
    expect(page.locator(".proposal-tab .diff-base")).to_have_text("a new file, proposed by Claude")
    tab.locator(".etab-close").click()
    assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
    assert not new.exists()


def test_a_whole_file_ask_over_the_link_names_the_file(page, ide):
    page.evaluate("() => { App.cm.focus(); App.cm.setCursor({line: 0, ch: 0}); }")
    page.keyboard.press("Control+j")
    page.locator("#ask-presets").get_by_role("button", name="Critique", exact=True).click()
    wait_term_line(page, PROMPT + r" @a\.md Critique this passage:.*")
    s = ide.last_selection()
    assert s["selection"]["isEmpty"] and s["filePath"].endswith("/a.md")


def test_a_new_session_is_told_the_selection(page, ide, cfg):
    """After Restart the new session's link starts with no selection: the page tells it, without
    the cursor having to move."""
    select_lines(page, 2, 2)
    until(lambda: (ide.last_selection() or {}).get("text", "").startswith("First"), what="the selection")
    old = ide.lock
    page.once("dialog", lambda d: d.accept())
    page.locator("#term-restart").click()
    until(lambda: not old.exists() and list((cfg / "ide").glob("*.lock")), what="the new session's lock file")
    wait_term_line(page, PROMPT + ".*")
    new = FakeIde(cfg)
    try:
        s = until(lambda: new.last_selection(), what="selection_changed for the new session")
        assert s["text"] == "First paragraph here, long enough to select.\n"
    finally:
        new.close()


def test_a_proposal_does_not_take_the_keys_from_the_editor(browser, srv, ide, doc):
    """A proposal that arrives while the user types in another file opens beside it, and one
    sent again (after a reconnect) does not take over the view."""
    (srv.root / "b.md").write_text("# B\n", encoding="utf-8")
    pg = new_page(browser, srv)
    try:
        pg.evaluate("() => Tabs.open('b.md', { preview: false })")
        expect(pg.locator("#doc-name")).to_have_text("b.md")
        pg.locator(".CodeMirror").click()
        pg.keyboard.press("Control+End")
        pg.keyboard.type("abc")
        rid, tab_name = open_diff(ide, doc, PROPOSED)
        expect(proposal_tab(pg)).to_have_count(1)
        expect(pg.locator("#toast")).to_contain_text("Claude has proposed a change to a.md")
        pg.keyboard.type("def")
        assert pg.evaluate("() => Tabs.active().path") == "b.md"
        assert pg.evaluate("() => Tabs.model('b.md').doc.getValue()") == "# B\nabcdef"
        # the page reconnects, and the server sends the waiting proposal again
        n = pg.evaluate("() => window.__sockets.length")
        pg.evaluate("() => window.__sockets[window.__sockets.length - 1].close()")
        pg.wait_for_function(f"() => window.__sockets.length > {n} && Term.running()", timeout=10000)
        pg.wait_for_timeout(500)
        assert pg.evaluate("() => Tabs.active().path") == "b.md"
        proposal_tab(pg).click()
        pg.locator(".proposal-tab").get_by_role("button", name="Reject").click()
        assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
        assert not pg.errors, pg.errors
    finally:
        pg.context.close()
        (srv.root / "b.md").unlink()


def test_a_narrow_window_shows_a_waiting_proposal(browser, srv, ide, doc):
    """Below 1100 px the panel starts hidden, so the terminal is not made; the page joins the
    session all the same, and a proposal waiting for an answer opens, its file held."""
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    pg = new_page(browser, srv, width=1000, prompt=False)
    try:
        expect(proposal_tab(pg)).to_have_count(1)
        assert pg.evaluate("() => document.body.classList.contains('no-claude')")
        assert pg.evaluate("() => !document.querySelector('#term .xterm')"), "no terminal while hidden"
        file_tab(pg).click()
        expect(pg.locator("#banner")).to_contain_text("Claude has proposed a change to a.md")
        assert pg.evaluate("() => App.cm.getOption('readOnly')") is True
        # its edits through a second editor (a Source Control diff's) are refused too
        assert pg.evaluate("""() => { const m = Tabs.model('a.md'), d = Tabs.linkedDoc(m);
            d.replaceRange('X', {line: 0, ch: 0}); const v = m.doc.getValue(); m.doc.unlinkDoc(d); return v; }""") == DOC
        proposal_tab(pg).click()
        pg.locator(".proposal-tab").get_by_role("button", name="Reject").click()
        assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
        assert pg.evaluate("""() => { const m = Tabs.model('a.md'), d = Tabs.linkedDoc(m);
            d.replaceRange('X', {line: 0, ch: 0}); const v = m.doc.getValue(); d.undo(); m.doc.unlinkDoc(d); return v; }""") == "X" + DOC
        # opening the panel makes the terminal, which replays the session
        pg.locator("#toggle-claude").click()
        wait_term_line(pg, PROMPT + ".*")
        assert not pg.errors, pg.errors
    finally:
        pg.context.close()


def test_a_waiting_proposal_stays_when_another_folder_is_opened(page, ide, doc, srv):
    """The proposals are the session's: opening another folder neither closes nor rejects them
    (and the folders' remembered tabs still come back); back in the session's folder the file is
    held again, and Close saved leaves the proposal too."""
    other = srv.root.parent / "elsewhere"
    other.mkdir(exist_ok=True)
    (other / "o.md").write_text("# O\n", encoding="utf-8")
    (other / "z.md").write_text("# Z\n", encoding="utf-8")
    page.evaluate("""k => localStorage.setItem(k, JSON.stringify({active: 0, tabs: [
        {t: 'file', path: 'o.md', preview: false}, {t: 'file', path: 'z.md', preview: false}]}))""", f"mdedit.tabs:{other}")
    rid, tab_name = open_diff(ide, doc, PROPOSED)
    expect(proposal_tab(page)).to_be_visible()
    file_tab(page).click()

    def open_folder(path):
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        here = page.evaluate("() => UI.root()")
        page.keyboard.press("Control+o")
        expect(page.locator("#browser")).to_be_visible()
        expect(page.locator("#browser-path")).to_have_value(here)   # the browser has opened on it
        page.locator("#browser-path").fill(str(path))
        page.keyboard.press("Enter")
        expect(page.locator("#browser-path")).to_have_value(str(path))
        page.locator("#browser-use-folder").click()
        expect(page.locator("#stat-root")).to_have_text(str(path))

    try:
        open_folder(other)
        expect(proposal_tab(page)).to_have_count(1)
        for name in ("o.md", "z.md"):   # the folder's remembered tabs
            expect(page.locator(".etab", has=page.locator(".etab-name", has_text=name))).to_have_count(1)
        page.wait_for_timeout(300)
        assert ide.response(rid) is None, "not rejected"
    finally:
        open_folder(srv.root)
    expect(file_tab(page)).to_have_count(1)
    file_tab(page).click()
    expect(page.locator("#banner")).to_contain_text("Claude has proposed a change to a.md")
    assert page.evaluate("() => App.cm.getOption('readOnly')") is True
    file_tab(page).click(button="right")
    page.locator(".ctx-menu").get_by_role("menuitem", name="Close saved").click()
    expect(file_tab(page)).to_have_count(0)
    expect(proposal_tab(page)).to_have_count(1)
    assert ide.response(rid) is None
    proposal_tab(page).click()
    page.locator(".proposal-tab").get_by_role("button", name="Reject").click()
    assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]


def test_a_page_that_outlives_md_editor_never_mixes_up_proposals(browser, md_editor, tmp_path, root):
    """md-editor stopped (killed, even) and started again on the same port: the open page joins
    the new one. The old one's proposal is gone, and the new one's, numbered afresh, opens."""
    cfg = tmp_path / "cfg"
    agent = fake_claude(tmp_path / "bin")
    first = md_editor(agent=agent, env=server_env(cfg))
    pg = new_page(browser, first)
    try:
        ide = FakeIde(cfg)
        rid, tab_name = open_diff(ide, root / "a.md", "# a\n\nAnswered.\n")
        expect(proposal_tab(pg)).to_be_visible()
        pg.locator(".proposal-tab").get_by_role("button", name="Reject").click()
        assert ide.texts(rid) == ["DIFF_REJECTED", tab_name]
        open_diff(ide, root / "a.md", "# a\n\nStale.\n")
        expect(proposal_tab(pg)).to_be_visible()
        old = pg.evaluate("() => Proposals.list().map(p => p.id)")
        first.stop(signal.SIGKILL)   # with Claude connected: no chance to withdraw it
        for pid in marked_pids(first.tag):   # the agent, in a session of its own
            os.kill(pid, signal.SIGKILL)
        ide.close()
        md_editor(agent=agent, env=server_env(cfg), args=["--port", str(first.port)])
        expect(proposal_tab(pg)).to_have_count(0, timeout=15000)
        wait_term_line(pg, PROMPT + ".*", timeout=15000)
        ide = FakeIde(cfg)
        rid, tab_name = open_diff(ide, root / "a.md", "# a\n\nFresh.\n")
        expect(proposal_tab(pg)).to_be_visible()
        new = pg.evaluate("() => Proposals.list().map(p => p.id)")
        assert len(new) == 1 and new[0] not in old and new[0].split("-")[0] != old[0].split("-")[0]
        pg.locator(".proposal-tab").get_by_role("button", name="Accept").click()
        assert ide.texts(rid) == ["FILE_SAVED", "# a\n\nFresh.\n"]
        ide.close()
    finally:
        pg.context.close()
