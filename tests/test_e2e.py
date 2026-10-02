"""The editor in headless Chromium (Playwright): the side-panel terminal, with bash standing in
for Claude Code (test_e2e_session.py has Ask Claude and the IDE link).

Skipped when Playwright or its Chromium is missing (`uv run playwright install chromium`);
MD_EDITOR_E2E=require turns the skip into a failure, as in CI. The page loads its libraries
from CDNs, so these tests need internet access."""
import os
import re
import shlex
import sys

import pytest

from helpers import FAKES, needs_linux

REQUIRE = os.environ.get("MD_EDITOR_E2E") == "require"
try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    if REQUIRE:
        raise
    pytest.skip("Playwright is not installed (it needs Python 3.10+)", allow_module_level=True)

pytestmark = [pytest.mark.e2e, needs_linux]   # the terminal runs bash on a PTY, read through /proc

DOC = "# E2E\n\nFirst paragraph here, long enough to select.\n\nSecond paragraph.\n"


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
def srv(shared_servers):
    (shared_servers.root / "a.md").write_text(DOC, encoding="utf-8")
    return shared_servers.start(agent="bash --norc --noprofile -i")


@pytest.fixture
def page(browser, srv):
    """A fresh browser profile (so no remembered tab, width or theme) on the editor; the page
    must log no errors."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(srv.base + "#a.md")
    expect(pg.locator("#doc-name")).to_have_text("a.md")
    yield pg
    ctx.close()
    assert not errors, errors


def wait_term_line(page, pattern, timeout=10000):
    """Wait until a terminal row matches `pattern` in full."""
    page.wait_for_function(
        """re => [...document.querySelectorAll('#term .xterm-rows > div')]
                 .some(d => new RegExp(re).test(d.textContent.replace(/\\u00a0/g, ' ').trimEnd()))""",
        arg=f"^{pattern}$", timeout=timeout)


def term_text(page):
    """Every terminal row, joined with newlines."""
    return page.evaluate("""() => [...document.querySelectorAll('#term .xterm-rows > div')]
                                  .map(d => d.textContent.replace(/\\u00a0/g, ' ').trimEnd()).join('\\n')""")


def panel_width(page):
    return page.evaluate("() => document.querySelector('#claude').getBoundingClientRect().width")


# ---------------------------------------------------------------- terminal

def test_the_panel_is_the_terminal(page):
    expect(page.locator("#panel-title")).to_have_text("Claude Code")
    expect(page.locator("#term-wrap")).to_be_visible()
    expect(page.locator("#term .xterm")).to_be_visible()
    expect(page.get_by_role("tab", name=re.compile("Suggestions"))).to_have_count(0)
    expect(page.get_by_role("button", name="Restart")).to_be_visible()


def test_terminal_runs_commands_and_replays_after_a_reload(page):
    page.locator("#term").click()
    page.keyboard.type("echo e2e-ok\n")
    wait_term_line(page, "e2e-ok")
    page.reload()
    expect(page.locator("#term .xterm")).to_be_visible()
    wait_term_line(page, "e2e-ok")
    page.locator("#term").click()
    page.keyboard.type("echo after-$((40+2))\n")
    wait_term_line(page, "after-42")


SLOW_SOCKET = """(() => {   // the page's WebSockets take 1.5 s to start connecting
  const Real = window.WebSocket;
  class Slow {
    constructor(url) {
      this.readyState = 0;
      this.binaryType = 'blob';
      setTimeout(() => {
        const r = this._r = new Real(url);
        r.binaryType = this.binaryType;
        r.onopen = e => { this.readyState = 1; window.slowSocketOpened = true; if (this.onopen) this.onopen(e); };
        r.onmessage = e => { if (this.onmessage) this.onmessage(e); };
        r.onerror = e => { if (this.onerror) this.onerror(e); };
        r.onclose = e => { this.readyState = 3; if (this.onclose) this.onclose(e); };
      }, 1500);
    }
    send(d) { if (this.readyState !== 1) throw new Error('not open'); this._r.send(d); }
    close(...a) { if (this._r) this._r.close(...a); else this.readyState = 3; }
  }
  Object.assign(Slow, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  window.WebSocket = Slow;
})()"""


def test_keys_typed_while_connecting_are_kept(browser, srv):
    """Keys typed before the socket opens wait for the session (the replay path when an
    earlier test has started it, else the spawn path) rather than being lost."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    errors = []
    try:
        ctx.add_init_script(SLOW_SOCKET)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(srv.base + "#a.md")
        pg.locator("#term .xterm").click()
        # not a time limit: the page itself can take longer than the shim's delay to load
        assert not pg.evaluate("() => !!window.slowSocketOpened"), "typed before the socket opened"
        pg.keyboard.type("echo early-$((40+3))\n")
        wait_term_line(pg, "early-43")
    finally:
        ctx.close()
    assert not errors, errors


TRACK_SOCKETS = """(() => {   // keep the page's WebSockets where a test can close them
  const Real = window.WebSocket;
  window.__sockets = [];
  window.WebSocket = class extends Real {
    constructor(...a) { super(...a); window.__sockets.push(this); }
  };
})()"""


def test_keys_typed_while_disconnected_are_dropped(browser, srv):
    """Keys typed at a panel whose connection dropped are not sent to whatever session
    answers the reconnect (after a server restart it would be a different one)."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    try:
        ctx.add_init_script(TRACK_SOCKETS)
        pg = ctx.new_page()
        pg.goto(srv.base + "#a.md")
        pg.locator("#term .xterm").click()
        pg.keyboard.type("echo before-$((3+4))\n")
        wait_term_line(pg, "before-7")
        pg.evaluate("() => window.__sockets.at(-1).close()")
        wait_term_line(pg, r"\[disconnected — reconnecting…\]")
        pg.keyboard.type("echo stale-$((5+5))\n")   # the retry waits a second
        pg.wait_for_function("() => window.__sockets.length > 1 && window.__sockets.at(-1).readyState === 1")
        pg.keyboard.type("echo after-$((6+6))\n")
        wait_term_line(pg, "after-12")
        assert "stale-10" not in term_text(pg)   # only output contains it; the echoed command does not
    finally:
        ctx.close()


def test_text_sent_while_connecting_is_kept(browser, srv):
    """Term.sendText (Send to Claude terminal) waits in the same queue as typed keys."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    try:
        ctx.add_init_script(SLOW_SOCKET)
        pg = ctx.new_page()
        pg.goto(srv.base + "#a.md")
        expect(pg.locator("#term .xterm")).to_be_visible()
        assert pg.evaluate("() => !window.slowSocketOpened && Term.sendText('echo sent-$((40+4))\\r')")
        wait_term_line(pg, "sent-44")
    finally:
        ctx.close()


def wait_prompts_after(page, pattern, count=1, timeout=10000):
    """Wait until a terminal row matches `pattern` in full and `count` rows below it are bash
    prompts with nothing typed after them."""
    page.wait_for_function(
        """([re, count]) => {
             const rows = [...document.querySelectorAll('#term .xterm-rows > div')]
               .map(d => d.textContent.replace(/\\u00a0/g, ' ').trimEnd());
             const i = rows.findLastIndex(r => new RegExp(re).test(r));
             return i >= 0 && rows.slice(i + 1).filter(r => /[#$]$/.test(r)).length >= count;
           }""",
        arg=[f"^{pattern}$", count], timeout=timeout)


def test_editor_shortcuts_stay_out_of_the_terminal(page):
    # Each key waits for bash to show it was handled: a ^C that comes while bash is busy (as
    # in a burst of keys sent together) can be lost, and so can keys typed straight after one.
    page.locator("#term").click()
    page.keyboard.type("echo one")
    page.keyboard.press("Control+j")   # newline: runs the line
    wait_prompts_after(page, "one")
    page.keyboard.press("Control+o")   # operate-and-get-next: runs the (empty) line
    wait_prompts_after(page, "one", count=2)
    page.keyboard.type("echo three")
    wait_term_line(page, r".*[#$] echo three")
    page.keyboard.press("Control+c")   # drops the line
    wait_prompts_after(page, r".*[#$] echo three\^C")
    page.keyboard.type("echo keys-$((6*7))\n")
    wait_term_line(page, "keys-42")
    expect(page.locator("#browser")).to_be_hidden()
    expect(page.locator("#askbar")).to_be_hidden()
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+o")
    expect(page.locator("#browser")).to_be_visible()   # while the editor has focus it does open
    expect(page.locator("#browser-path")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#browser")).to_be_hidden()


def test_theme_toggle(page):
    html = page.locator("html")
    viewport = page.locator("#term .xterm-viewport")
    expect(viewport).to_be_visible()
    start = html.get_attribute("data-theme")
    page.get_by_title("Light / dark").click()
    flipped = "dark" if start == "light" else "light"
    expect(html).to_have_attribute("data-theme", flipped)
    dark_bg = "rgb(13, 17, 23)"
    if flipped == "dark":
        expect(viewport).to_have_css("background-color", dark_bg)
    else:
        expect(viewport).not_to_have_css("background-color", dark_bg)
    page.get_by_title("Light / dark").click()
    expect(html).to_have_attribute("data-theme", start)
    page.locator("#term").click()
    page.keyboard.type("echo theme-$((1+1))\n")
    wait_term_line(page, "theme-2")


def test_an_idle_page_answers_pings(browser, shared_servers):
    """The server drops a client that leaves a ping unanswered; the browser answers them by
    itself, so an idle page keeps its one connection."""
    srv = shared_servers.start(agent="bash --norc --noprofile -i", env={"MDEDIT_WS_PING": "0.3"})
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    try:
        pg = ctx.new_page()
        pg.goto(srv.base)
        pg.locator("#term").click()
        pg.keyboard.type("echo ping-$((2+3))\n")
        wait_term_line(pg, "ping-5")
        pg.wait_for_timeout(2000)
        pg.keyboard.type("echo still-$((3+4))\n")
        wait_term_line(pg, "still-7")
        assert srv.logtext().count('"GET /api/term HTTP/1.1" 101') == 1, "never dropped and reconnected"
    finally:
        ctx.close()


def test_a_full_screen_agent_is_restored_after_a_reload(browser, shared_servers):
    """fakes/tui sets its modes at the start, as Claude Code does, then writes more than the
    replay holds. After a reload the terminal is back in those modes: on the alternate screen
    (so nothing to scroll back), reporting focus and the mouse, sending application cursor
    keys and bracketing pastes. And the agent was made to redraw (SIGWINCH)."""
    srv = shared_servers.start(agent=shlex.join([sys.executable, str(FAKES / "tui")]))
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    errors = []
    try:
        pg = ctx.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(srv.base)
        wait_term_line(pg, "READY", 30000)
        pg.reload()
        wait_term_line(pg, "READY")
        wait_term_line(pg, r"WINCH \d+x\d+")
        viewport = pg.locator("#term .xterm-viewport")
        assert viewport.evaluate("v => v.scrollHeight <= v.clientHeight + 1"), "on the alternate screen"
        expect(pg.locator("#term .xterm")).to_have_class(re.compile(r"\benable-mouse-events\b"))
        pg.locator("#term").click()
        pg.wait_for_function("""() => [...document.querySelectorAll('#term .xterm-rows > div')]
                                .some(d => d.textContent.includes('^[[<0;'))""")   # an SGR mouse press
        assert "^[[I" in term_text(pg), "focus reported"
        pg.keyboard.press("ArrowUp")
        wait_term_line(pg, r"GOT \^\[OA")
        pg.locator("#term .xterm-helper-textarea").evaluate("""ta => {
            const dt = new DataTransfer();
            dt.setData('text/plain', 'pasted');
            ta.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        }""")
        wait_term_line(pg, r"GOT \^\[\[200~pasted\^\[\[201~")
    finally:
        ctx.close()
    assert not errors, errors


def test_panel_width_drag_persists(page):
    before = panel_width(page)
    box = page.locator("#panel-resize").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 150, y, steps=10)
    page.mouse.up()
    after = panel_width(page)
    assert after == pytest.approx(before + 150, abs=3)
    assert page.evaluate("() => localStorage.getItem('mdedit.panelWidth')") == str(round(after))
    page.reload()
    expect(page.locator("#doc-name")).to_have_text("a.md")
    assert panel_width(page) == pytest.approx(after, abs=1)
