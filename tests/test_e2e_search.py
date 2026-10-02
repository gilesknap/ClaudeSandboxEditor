"""Quick open (Ctrl+P), find in files (Ctrl+Shift+F, the Search panel) and Send to Claude
terminal (Ctrl+Alt+L and the context menus) in headless Chromium (Playwright).

Each test opens a fresh git repository; the terminal test runs bash in place of Claude Code
and reads what the page asked for from its WebSocket frames, and what the server typed from
the screen. Skipped
like test_e2e.py when Playwright or its Chromium is missing (MD_EDITOR_E2E=require makes that
fail)."""
import os
import re
import time

import pytest

from gitutil import SERVER_ENV, make_repo, open_root, write

REQUIRE = os.environ.get("MD_EDITOR_E2E") == "require"
try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    if REQUIRE:
        raise
    pytest.skip("Playwright is not installed (it needs Python 3.10+)", allow_module_level=True)

pytestmark = pytest.mark.e2e

FILES = {
    "README.md": "# Readme\n\nHello world.\n",
    "docs/guide.md": "# Guide\n\nSay hello to the guide.\n",
    "src/app.py": 'def main():\n    print("hello")\n    return "Hello"\n',
    "src/util.js": "const helloWorld = 1;\nexport { helloWorld };\n",
    "src/components/AppHeader.tsx": "export const AppHeader = () => null;\n",
    ".gitignore": "build/\n",
}
PREVIEW = re.compile(r"\bpreview\b")
BASH = "bash --norc --noprofile -i"
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
def srv(shared_servers):
    return shared_servers.start(env=SERVER_ENV)


def new_repo(path):
    """FILES committed, plus a git-ignored build/hello.txt that neither picker may show."""
    r = make_repo(path, FILES)
    write(r, {"build/hello.txt": "hello from the build\n"})
    return r


@pytest.fixture
def repo(srv, tmp_path):
    return open_root(srv, new_repo(tmp_path / "ws"))


def open_page(browser, server, width=1400, on_page=None):
    """A fresh browser profile on the editor; returns (page, errors logged)."""
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    if on_page:
        on_page(pg)
    pg.goto(server.base)
    expect(pg.locator("#doc-name")).to_have_text(re.compile(r"\.md$"))
    pg.wait_for_function("() => window.App && Tabs.active()")
    return pg, errors


ERRORS = {}   # page → console errors it logged


@pytest.fixture
def page(browser, srv, repo):
    """The editor on `repo` in a fresh browser profile; it must log no errors."""
    pg, errors = open_page(browser, srv)
    ERRORS[pg] = errors
    expect(pg.locator("#doc-name")).to_have_text("README.md")
    yield pg
    pg.context.close()
    del ERRORS[pg]
    assert not errors, errors


def row(pg, path):
    return pg.locator(f'#tree .tree-row[data-path="{path}"]')


def tab(pg, name):
    return pg.locator(".etab", has=pg.locator(".etab-name", has_text=re.compile(f"^{re.escape(name)}$")))


def cursor(pg, which="head"):
    return pg.evaluate("w => App.cm.getCursor(w)", which)


def wait_term_line(pg, pattern, timeout=10000):
    """Wait until a terminal row matches `pattern` in full."""
    pg.wait_for_function(
        """re => [...document.querySelectorAll('#term .xterm-rows > div')]
                 .some(d => new RegExp(re).test(d.textContent.replace(/\\u00a0/g, ' ').trimEnd()))""",
        arg=f"^{pattern}$", timeout=timeout)


# ---------------------------------------------------------------- quick open

def test_quick_open_fuzzy_matches_and_opens(page):
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+p")
    box = page.locator(".qo-input")
    expect(box).to_be_focused()
    first = page.locator(".qo-item").first
    expect(first.locator(".qo-name")).to_have_text("README.md")
    expect(first.locator(".qo-tag")).to_have_text("recently opened")

    page.keyboard.type("utjs")
    sel = page.locator(".qo-item.sel")
    expect(sel.locator(".qo-name")).to_have_text("util.js")
    expect(sel.locator(".qo-dir")).to_have_text("src")
    assert sel.locator(".qo-hit").all_text_contents() == ["ut", "js"]
    box.fill("apphead")
    expect(sel.locator(".qo-name")).to_have_text("AppHeader.tsx")
    expect(sel.locator(".qo-dir")).to_have_text("src/components")
    box.fill("hello.txt")   # git-ignored files are left out
    expect(page.locator(".qo-empty")).to_have_text("No matching files.")

    box.fill("gd")
    expect(sel.locator(".qo-name")).to_have_text("guide.md")
    page.keyboard.press("Enter")
    expect(page.locator(".qo-back")).to_have_count(0)
    expect(page.locator("#doc-name")).to_have_text("docs/guide.md")
    expect(tab(page, "guide.md")).to_have_class(PREVIEW)

    # nothing typed: recently opened files first, and Enter goes back to the previous one
    page.keyboard.press("Control+p")
    names = page.locator(".qo-item .qo-name")
    expect(names.nth(0)).to_have_text("guide.md")
    expect(names.nth(1)).to_have_text("README.md")
    expect(page.locator(".qo-item.sel .qo-name")).to_have_text("README.md")
    expect(page.locator(".qo-item").nth(2).locator(".qo-tag")).to_have_text("other files")
    page.keyboard.press("Escape")
    expect(page.locator(".qo-back")).to_have_count(0)
    expect(page.locator(".CodeMirror textarea")).to_be_focused()

    # Ctrl+Enter keeps the tab open; name:line goes to that line
    page.keyboard.press("Control+p")
    page.keyboard.type("app.py:3")
    expect(page.locator(".qo-foot")).to_contain_text("Opens at line 3.")
    page.keyboard.press("Control+Enter")
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    expect(tab(page, "app.py")).not_to_have_class(PREVIEW)
    assert cursor(page)["line"] == 2

    # a click opens a preview tab, a double-click keeps it open
    page.keyboard.press("Control+p")
    page.keyboard.type("readme")
    page.locator(".qo-item").first.click()
    expect(page.locator("#doc-name")).to_have_text("README.md")
    page.keyboard.press("Control+p")
    page.keyboard.type("util")
    page.locator(".qo-item", has_text="util.js").dblclick()
    expect(page.locator("#doc-name")).to_have_text("src/util.js")
    expect(tab(page, "util.js")).not_to_have_class(PREVIEW)
    expect(page.locator(".qo-back")).to_have_count(0)


def test_ctrl_p_is_not_print(page):
    prevented = page.evaluate("""() => {
        const e = new KeyboardEvent('keydown', {key: 'p', code: 'KeyP', ctrlKey: true, bubbles: true, cancelable: true});
        document.querySelector('.CodeMirror textarea').dispatchEvent(e);
        return e.defaultPrevented;
    }""")
    assert prevented
    expect(page.locator(".qo-input")).to_be_focused()
    page.locator(".qo-back").click(position={"x": 5, "y": 5})   # outside the picker
    expect(page.locator(".qo-back")).to_have_count(0)
    # over the folder browser it does nothing (and still doesn't print)
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+o")
    expect(page.locator("#browser-path")).to_be_focused()
    prevented = page.evaluate("""() => {
        const e = new KeyboardEvent('keydown', {key: 'p', code: 'KeyP', ctrlKey: true, bubbles: true, cancelable: true});
        document.querySelector('#browser-path').dispatchEvent(e);
        return e.defaultPrevented;
    }""")
    assert prevented
    expect(page.locator(".qo-back")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.locator("#browser")).to_be_hidden()


def test_replace_moves_to_ctrl_h(page):
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+Shift+F")
    expect(page.locator("#search-q")).to_be_focused()
    expect(page.locator(".CodeMirror-dialog")).to_have_count(0)
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+h")
    expect(page.locator(".CodeMirror-dialog")).to_contain_text("Replace")


# ---------------------------------------------------------------- find in files

def search(page, text):
    q = page.locator("#search-q")
    q.fill(text)
    return q


def test_find_in_files_groups_results_and_opens_the_match(page):
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+Shift+F")
    expect(page.locator("#search-q")).to_be_focused()
    expect(page.locator(".CodeMirror-dialog")).to_have_count(0)   # not CodeMirror's replace
    search(page, "hello")
    msg = page.locator(".search-msg")
    expect(msg).to_have_text("6 results in 4 files")   # build/hello.txt is git-ignored
    files = page.locator(".sr-file")
    assert files.locator(".sr-name").all_text_contents() == ["README.md", "guide.md", "app.py", "util.js"]
    assert files.locator(".sr-dir").all_text_contents() == ["", "docs", "src", "src"]
    assert files.locator(".sr-count").all_text_contents() == ["1", "1", "2", "2"]
    assert page.locator(".sr-match mark").all_text_contents() == ["Hello", "hello", "hello", "Hello", "hello", "hello"]
    match = page.locator(".sr-match", has_text="return")
    expect(match).to_have_attribute("title", "src/app.py:3:13")

    match.click()
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    expect(tab(page, "app.py")).to_have_class(PREVIEW)
    page.wait_for_function("() => App.cm.getSelection() === 'Hello'")
    c = cursor(page, "from")
    assert (c["line"], c["ch"]) == (2, 12)
    expect(page.locator("#search-results")).to_be_focused()   # a click previews; the list keeps the keys

    # keyboard: down to the next match and open it in the editor
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("#doc-name")).to_have_text("src/util.js")
    expect(page.locator(".CodeMirror textarea")).to_be_focused()
    assert page.evaluate("() => App.cm.getSelection()") == "hello"
    assert cursor(page, "from")["line"] == 0

    # a file row folds its matches; a double-click on a match keeps its tab
    page.locator(".sr-file", has_text="util.js").click()
    expect(page.locator(".sr-match")).to_have_count(4)
    expect(page.locator(".sr-file", has_text="util.js")).to_have_attribute("aria-expanded", "false")
    page.locator(".sr-match", has_text="Say hello").dblclick()
    expect(page.locator("#doc-name")).to_have_text("docs/guide.md")
    expect(tab(page, "guide.md")).not_to_have_class(PREVIEW)


def test_find_options_and_include_globs(page):
    page.keyboard.press("Control+Shift+F")
    msg = page.locator(".search-msg")
    q = search(page, "Hello")
    expect(msg).to_have_text("6 results in 4 files")
    case = page.locator('.search-opt[data-opt="case"]')
    case.click()
    expect(case).to_have_attribute("aria-pressed", "true")
    expect(msg).to_have_text("2 results in 2 files")
    q.press("Alt+c")   # the options' keys work in the box
    expect(case).to_have_attribute("aria-pressed", "false")
    expect(msg).to_have_text("6 results in 4 files")
    tabs_before = page.locator(".etab").count()
    q.press("Alt+w")   # whole word (and not Alt+W's close tab)
    expect(page.locator('.search-opt[data-opt="word"]')).to_have_attribute("aria-pressed", "true")
    expect(msg).to_have_text("4 results in 3 files")   # not helloWorld
    assert page.locator(".etab").count() == tabs_before
    page.locator('.search-opt[data-opt="word"]').click()

    regex = page.locator('.search-opt[data-opt="regex"]')
    regex.click()
    search(page, "hel+ow")
    expect(msg).to_have_text("2 results in 1 file")
    assert page.locator(".sr-match mark").all_text_contents() == ["helloW", "helloW"]
    search(page, "(")
    expect(msg).to_contain_text("invalid regular expression")
    expect(msg).to_have_class(re.compile(r"\berr\b"))
    expect(page.locator(".search-box")).to_have_class(re.compile(r"\bbad\b"))
    # Chrome logs every 4xx response, and an invalid regular expression is a 400
    ERRORS[page][:] = [e for e in ERRORS[page] if "status of 400" not in e]
    regex.click()   # literally "("
    expect(msg).to_have_text("3 results in 2 files")
    expect(page.locator(".search-box")).not_to_have_class(re.compile(r"\bbad\b"))

    search(page, "hello")
    expect(msg).to_have_text("6 results in 4 files")
    glob = page.locator("#search-glob")
    names = page.locator(".sr-file .sr-name")
    glob.fill("*.py")
    expect(names).to_have_text(["app.py"])
    glob.fill("src/, !*.py")
    expect(names).to_have_text(["util.js"])
    expect(msg).to_have_text("2 results in 1 file")
    glob.fill("*.rs")
    expect(msg).to_have_text("No results found in the files to include.")

    # the settings survive a reload, and the panel searches again
    page.reload()
    expect(page.locator("#doc-name")).to_have_text("README.md")
    page.keyboard.press("Control+Shift+F")
    expect(page.locator("#search-q")).to_have_value("hello")
    expect(page.locator("#search-glob")).to_have_value("*.rs")
    expect(msg).to_have_text("No results found in the files to include.")


def test_find_seeds_from_the_selection_and_truncates(page, repo):
    write(repo, {"many.txt": "zzz\n" * 2100})
    page.evaluate("() => { App.cm.focus(); App.cm.setSelection({line: 0, ch: 2}, {line: 0, ch: 8}); }")
    page.keyboard.press("Control+Shift+F")
    expect(page.locator("#search-q")).to_have_value("Readme")
    expect(page.locator(".search-msg")).to_have_text("1 result in 1 file")
    search(page, "zzz")
    expect(page.locator(".search-msg")).to_contain_text("2,000 results in 1 file")
    expect(page.locator(".search-warn")).to_contain_text("Results truncated")
    expect(page.locator(".sr-match")).to_have_count(2000)
    page.locator('#search-results').focus()
    page.keyboard.press("Delete")   # dismiss the file
    expect(page.locator(".sr-row")).to_have_count(0)


def test_search_and_quick_open_fit_a_narrow_dark_window(browser, srv, repo):
    page, errors = open_page(browser, srv, width=700)
    page.evaluate("() => setTheme(true)")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.keyboard.press("Control+Shift+F")
    expect(page.locator("#search-q")).to_be_visible()
    search(page, "hello")
    expect(page.locator(".search-msg")).to_have_text("6 results in 4 files")
    side = page.locator("#sidebar").bounding_box()
    for el in (page.locator(".search-box"), page.locator("#search-glob"), page.locator(".sr-match").first):
        b = el.bounding_box()
        assert b["x"] >= side["x"] and b["x"] + b["width"] <= side["x"] + side["width"] + 1, el
    page.keyboard.press("Control+p")
    b = page.locator(".qo").bounding_box()
    assert b["x"] >= 0 and b["x"] + b["width"] <= 700
    assert page.evaluate("() => document.documentElement.scrollWidth") <= 700
    page.context.close()
    assert not errors, errors


# ---------------------------------------------------------------- send to the Claude terminal

def test_send_to_claude_terminal(browser, shared_servers, tmp_path):
    ws_repo = new_repo(tmp_path / "term")
    other = make_repo(tmp_path / "other", {"notes.md": "# Notes\n"})
    # no history file: Ctrl+P below must not bring back an old command line
    srv = shared_servers.start(root=ws_repo, env=dict(SERVER_ENV, HISTFILE=os.devnull), agent=BASH)
    sent = []

    def watch(pg):
        pg.on("websocket", lambda ws: ws.on("framesent", lambda p: sent.append(p if isinstance(p, bytes) else p.encode())))

    def wait_sent(want, timeout=5):
        deadline = time.monotonic() + timeout
        while want not in b"".join(sent):
            assert time.monotonic() < deadline, (want, sent)
            page.wait_for_timeout(50)

    page, errors = open_page(browser, srv, on_page=watch)
    wait_term_line(page, PROMPT)

    # the shortcuts are left to the terminal while it has the focus
    page.locator("#term").click()
    page.keyboard.press("Control+p")
    page.keyboard.press("Control+Shift+F")
    page.wait_for_timeout(300)
    expect(page.locator(".qo-back")).to_have_count(0)
    assert page.evaluate("() => Activity.current()") == "explorer"

    # Ctrl+Alt+L: the selected lines, as one bracketed paste, then the terminal has the focus
    row(page, "src").click()
    row(page, "src/app.py").dblclick()
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    page.evaluate("() => { App.cm.focus(); App.cm.setSelection({line: 0, ch: 0}, {line: 2, ch: 0}); }")
    page.keyboard.press("Control+Alt+l")
    wait_term_line(page, PROMPT + r" @src/app\.py#L1-2")
    # the page asks the server to mention the lines (over the IDE link when it is up; here it
    # types them, as one paste)
    wait_sent(b'{"type":"mention","path":"src/app.py","start":{"line":0,"character":0},"end":{"line":2,"character":0}}')
    page.wait_for_function("() => !!document.activeElement.closest('#term')")

    # the Explorer's menu sends the path
    row(page, "src/util.js").click(button="right")
    page.locator(".ctx-menu").get_by_role("menuitem", name="Send to Claude terminal").click()
    wait_term_line(page, PROMPT + r" @src/app\.py#L1-2 @src/util\.js")
    wait_sent(b'{"type":"mention","path":"src/util.js"}')

    # the editor's own menu sends the selection (Shift+right-click leaves the browser's menu)
    tab(page, "app.py").click()
    page.evaluate("() => { App.cm.focus(); App.cm.setSelection({line: 1, ch: 4}, {line: 1, ch: 9}); }")
    at = page.evaluate("() => App.cm.charCoords({line: 1, ch: 6}, 'window')")
    page.keyboard.down("Shift")
    page.mouse.click(at["left"], (at["top"] + at["bottom"]) / 2, button="right")
    page.keyboard.up("Shift")
    expect(page.locator(".ctx-menu")).to_have_count(0)
    page.mouse.click(at["left"], (at["top"] + at["bottom"]) / 2, button="right")
    item = page.locator(".ctx-menu").get_by_role("menuitem", name="Send selection to Claude terminal")
    expect(item).to_be_visible()
    item.click()
    wait_term_line(page, PROMPT + r" @src/app\.py#L1-2 @src/util\.js @src/app\.py#L2")

    # a file outside the terminal's folder is sent with its absolute path
    status, res = srv.post("/api/root", {"path": str(other)})
    assert status == 200, res
    page.goto(srv.base)   # (a reload would ask for #src/app.py, which this folder doesn't have)
    expect(page.locator("#doc-name")).to_have_text("notes.md")
    tab(page, "notes.md").click(button="right")
    page.locator(".ctx-menu").get_by_role("menuitem", name="Send to Claude terminal").click()
    wait_sent(b'{"type":"mention","path":"notes.md"}')
    page.wait_for_function("""p => [...document.querySelectorAll('#term .xterm-rows > div')]
                                  .map(d => d.textContent.replace(/\\u00a0/g, ' ').trimEnd()).join('').includes(p)""",
                           arg=f"@{other}/notes.md")   # rows joined: the line may wrap
    page.wait_for_function("() => !!document.activeElement.closest('#term')")
    page.context.close()
    assert not errors, errors


def test_send_without_a_terminal_says_so(page):
    page.evaluate("() => { App.cm.focus(); App.cm.setSelection({line: 0, ch: 0}, {line: 0, ch: 3}); }")
    page.keyboard.press("Control+Alt+l")
    expect(page.locator("#toast")).to_have_text("The Claude terminal is not available.")
