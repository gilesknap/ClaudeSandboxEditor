"""Source control in headless Chromium (Playwright): the Source Control panel and its badge,
diff tabs (side by side and inline) whose working side is the file itself, hunk revert, discard,
the Branch base, change bars in the editor's gutter, and the branch in the status bar.

Each test opens a fresh git repository through /api/root on one shared server: FILES committed
on main, then src/app.py modified (two hunks), src/old.py deleted and new.txt left untracked.
Skipped like test_e2e.py when Playwright or its Chromium is missing (MD_EDITOR_E2E=require makes
that fail)."""
import os
import re

import pytest

from gitutil import SERVER_ENV, git, make_repo, open_root

REQUIRE = os.environ.get("MD_EDITOR_E2E") == "require"
try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    if REQUIRE:
        raise
    pytest.skip("Playwright is not installed (it needs Python 3.10+)", allow_module_level=True)

pytestmark = pytest.mark.e2e

APP = ('def main():\n    print("hi")\n    return 1\n\n\ndef helper(x):\n    y = x * 2\n    return y\n\n\n'
       'def other():\n    return 2\n')
APP_NOW = APP.replace('print("hi")', 'print("hello")').replace("return 2", "return 3")
FILES = {
    "README.md": "# Readme\n\nHello world.\n",
    "docs/guide.md": "# Guide\n\nSome text.\n",
    "src/app.py": APP,
    "src/old.py": "old = 1\n",
}
CHANGED = ["new.txt U", "src/app.py M", "src/old.py D"]


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


def make_changes(r):
    (r / "src" / "app.py").write_text(APP_NOW, encoding="utf-8")
    (r / "src" / "old.py").unlink()
    (r / "new.txt").write_text("brand new\n", encoding="utf-8")


@pytest.fixture
def repo(srv, tmp_path):
    """A fresh repository with uncommitted changes, opened in the server."""
    r = make_repo(tmp_path / "ws", FILES)
    make_changes(r)
    return open_root(srv, r)


def open_page(browser, server, width=1400):
    """A fresh browser profile on the editor; returns (page, errors logged)."""
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server.base)
    expect(pg.locator("#doc-name")).to_have_text("README.md")
    return pg, errors


@pytest.fixture
def page(browser, srv, repo):
    """The editor on `repo` in a fresh browser profile; it must log no errors."""
    pg, errors = open_page(browser, srv)
    yield pg
    pg.context.close()
    assert not errors, errors


def disk(repo, rel):
    return (repo / rel).read_text(encoding="utf-8")


def tab(pg, name):
    return pg.locator(".etab", has=pg.locator(".etab-name", has_text=re.compile(f"^{re.escape(name)}$")))


def badge(pg):
    return pg.locator('#activity-bar .activity-btn[data-id="scm"] .activity-badge')


def scm_row(pg, path):
    return pg.locator(f'.scm-row[data-path="{path}"]')


def open_scm(pg):
    pg.locator('#activity-bar .activity-btn[data-id="scm"]').click()
    expect(pg.locator(".scm-list")).to_be_visible()


def expect_rows(pg, rows, timeout=5000):
    """The Source Control list shows exactly these "path letter" rows."""
    pg.wait_for_function(
        """want => JSON.stringify([...document.querySelectorAll('.scm-row')]
             .map(r => r.dataset.path + ' ' + r.querySelector('.st').textContent)) === JSON.stringify(want)""",
        arg=rows, timeout=timeout)


VIEW = ".custom-tab-view:not([hidden])"


def merge_values(pg):
    """(base, working) text of the diff tab on screen."""
    return pg.evaluate(f"""() => {{
        const cm = s => document.querySelector('{VIEW} ' + s + ' .CodeMirror').CodeMirror;
        return [cm('.CodeMirror-merge-left').getValue(), cm('.CodeMirror-merge-editor').getValue()];
    }}""")


def edit_cursor_line(pg):
    return pg.evaluate(f"() => document.querySelector('{VIEW} .CodeMirror-merge-editor .CodeMirror').CodeMirror.getCursor().line")


# ---------------------------------------------------------------- the panel

def test_panel_lists_changes_with_a_badge(page, repo):
    expect(badge(page)).to_have_text("3")
    expect(page.locator("#stat-branch")).to_have_text(re.compile(r"ws\s+⎇ main"))
    open_scm(page)
    expect_rows(page, CHANGED)
    expect(page.locator(".scm-desc")).to_contain_text("against HEAD")
    expect(page.locator(".scm-group .count")).to_have_text("3")
    # the Explorer shows the status letters too
    page.locator('#activity-bar .activity-btn[data-id="explorer"]').click()
    src = page.locator('#tree .tree-row[data-path="src"]')
    expect(src).to_have_class(re.compile(r"\bscm-dir\b"))
    src.click()
    expect(page.locator('#tree .tree-row[data-path="src/app.py"] .deco')).to_have_text("M")
    expect(page.locator('#tree .tree-row[data-path="new.txt"] .deco')).to_have_text("U")
    # new files show up by polling, and the status bar opens the panel
    (repo / "more.txt").write_text("more\n", encoding="utf-8")
    page.locator("#stat-branch").click()
    expect(page.locator(".scm-list")).to_be_visible()
    expect_rows(page, ["more.txt U"] + CHANGED)
    expect(badge(page)).to_have_text("4")


def test_not_a_repository(browser, srv, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("# Readme\n", encoding="utf-8")
    open_root(srv, plain)
    page, errors = open_page(browser, srv)
    open_scm(page)
    expect(page.locator(".scm-empty")).to_contain_text("not in a git repository")
    expect(page.locator(".scm-base")).to_be_hidden()
    expect(badge(page)).to_be_hidden()
    expect(page.locator("#stat-branch")).to_be_hidden()
    page.context.close()
    assert not errors, errors


# ---------------------------------------------------------------- diff tabs

def test_diff_tab_edits_the_file_itself(page, repo):
    open_scm(page)
    scm_row(page, "src/app.py").click()
    diff = tab(page, "app.py (Working Tree)")
    expect(diff).to_have_class(re.compile(r"\bactive\b"))
    expect(diff).to_have_class(re.compile(r"\bpreview\b"))
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    base, work = merge_values(page)
    assert base == APP and work == APP_NOW
    expect(page.locator(f"{VIEW} .diff-stats")).to_have_text("+2 −2")
    # editing the working side edits the file: dirty, pinned, saved with Ctrl+S
    page.locator(f"{VIEW} .CodeMirror-merge-editor .CodeMirror").click()
    page.keyboard.press("Control+End")
    page.keyboard.type("# reviewed\n")
    expect(diff).to_have_class(re.compile(r"\bdirty\b"))
    expect(diff).not_to_have_class(re.compile(r"\bpreview\b"))
    page.keyboard.press("Control+s")
    expect(diff).not_to_have_class(re.compile(r"\bdirty\b"))
    assert disk(repo, "src/app.py") == APP_NOW + "# reviewed\n"
    # the file's own tab shares the document both ways
    page.locator(f"{VIEW} .diff-head [data-act=open]").click()
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    expect(page.locator("#editor .CodeMirror")).to_contain_text("# reviewed")
    page.locator("#editor .CodeMirror").click()
    page.keyboard.press("Control+End")
    page.keyboard.type("x = 1\n")
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bdirty\b"))
    diff.click()
    expect(diff).to_have_class(re.compile(r"\bdirty\b"))
    assert merge_values(page)[1].endswith("# reviewed\nx = 1\n")
    page.keyboard.press("Control+s")
    expect(diff).not_to_have_class(re.compile(r"\bdirty\b"))
    expect(tab(page, "app.py")).not_to_have_class(re.compile(r"\bdirty\b"))
    assert disk(repo, "src/app.py").endswith("# reviewed\nx = 1\n")
    # changes on disk reach the diff too
    (repo / "src" / "app.py").write_text(APP_NOW + "# from disk\n", encoding="utf-8")
    page.wait_for_function(f"""() => document.querySelector('{VIEW} .CodeMirror-merge-editor .CodeMirror')
                                .CodeMirror.getValue().endsWith('# from disk\\n')""", timeout=5000)


def test_hunk_revert(page, repo):
    open_scm(page)
    scm_row(page, "src/app.py").dblclick()
    diff = tab(page, "app.py (Working Tree)")
    expect(diff).not_to_have_class(re.compile(r"\bpreview\b"))
    arrows = page.locator(f"{VIEW} .CodeMirror-merge-copy")
    expect(arrows).to_have_count(2)
    arrows.first.click()
    expect(diff).to_have_class(re.compile(r"\bdirty\b"))
    assert merge_values(page)[1] == APP.replace("return 2", "return 3")
    expect(arrows).to_have_count(1)
    arrows.first.click()
    assert merge_values(page)[1] == APP
    page.keyboard.press("Control+s")
    expect(diff).not_to_have_class(re.compile(r"\bdirty\b"))
    assert disk(repo, "src/app.py") == APP
    # no longer changed: gone from the list
    expect_rows(page, ["new.txt U", "src/old.py D"])
    expect(badge(page)).to_have_text("2")


def test_inline_view_navigation_and_revert(page, repo):
    open_scm(page)
    scm_row(page, "src/app.py").click()
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    # change navigation: the diff opens on the first change; Alt+F5 and the buttons move on
    assert edit_cursor_line(page) == 1
    page.locator(f"{VIEW} .diff-head [data-act=next]").click()
    assert edit_cursor_line(page) == 11
    page.keyboard.press("Alt+F5")   # wraps around
    assert edit_cursor_line(page) == 1
    page.keyboard.press("Shift+Alt+F5")
    assert edit_cursor_line(page) == 11
    # the inline view: removed lines between the others, kept at the same place
    page.locator(f"{VIEW} .diff-head [data-view=inline]").click()
    inline = page.locator(f"{VIEW} .diff-inline")
    expect(inline).to_be_visible()
    expect(page.locator(f"{VIEW} .diff-split")).to_be_hidden()
    expect(inline.locator(".diff-del-line")).to_have_count(2)
    expect(inline.locator(".diff-add-line")).to_have_count(2)
    rows = page.evaluate(f"() => document.querySelector('{VIEW} .diff-inline .CodeMirror').CodeMirror.getValue().split('\\n')")
    assert rows[:3] == ["def main():", '    print("hi")', '    print("hello")']
    # its revert buttons put the base's lines back
    inline.locator(".diff-revert-btn").first.click()
    diff = tab(page, "app.py (Working Tree)")
    expect(diff).to_have_class(re.compile(r"\bdirty\b"))
    expect(inline.locator(".diff-del-line")).to_have_count(1)
    assert page.evaluate("() => Tabs.model('src/app.py').doc.getValue()") == APP.replace("return 2", "return 3")
    page.keyboard.press("Control+s")
    expect(diff).not_to_have_class(re.compile(r"\bdirty\b"))
    # the tab and the choice of view are remembered
    page.reload()
    expect(diff).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(f"{VIEW} .diff-inline .CodeMirror")).to_be_visible()
    expect(page.locator(f"{VIEW} .diff-head [data-view=inline]")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator(f"{VIEW} .diff-inline .diff-del-line")).to_have_count(1)


def test_diff_editors_report_the_working_files_selection(page):
    """What is selected in a diff is what Claude is told about (Term.select, which the IDE link
    passes on): lines of the working file, from either view."""
    page.evaluate("() => { window.sels = []; Term.select = s => window.sels.push(s); }")
    open_scm(page)
    scm_row(page, "src/app.py").click()
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    page.evaluate(f"""() => document.querySelector('{VIEW} .CodeMirror-merge-editor .CodeMirror').CodeMirror
                          .setSelection({{line: 1, ch: 4}}, {{line: 2, ch: 0}})""")
    page.wait_for_function("() => window.sels.length && window.sels.at(-1).start.line === 1")
    assert page.evaluate("window.sels.at(-1)") == {"path": "src/app.py", "start": {"line": 1, "character": 4},
                                                   "end": {"line": 2, "character": 0}, "text": 'print("hello")\n'}
    # the inline view's rows are mapped back to the file's lines, the removed ones left out
    page.locator(f"{VIEW} .diff-head [data-view=inline]").click()
    expect(page.locator(f"{VIEW} .diff-inline .CodeMirror")).to_be_visible()
    page.evaluate(f"""() => document.querySelector('{VIEW} .diff-inline .CodeMirror').CodeMirror
                          .setSelection({{line: 1, ch: 0}}, {{line: 3, ch: 5}})""")
    page.wait_for_function("() => window.sels.at(-1).end.line === 2")
    assert page.evaluate("window.sels.at(-1)") == {"path": "src/app.py", "start": {"line": 1, "character": 0},
                                                   "end": {"line": 2, "character": 12}, "text": '    print("hello")\n    return 1'}


def test_deleted_and_untracked_files(page):
    open_scm(page)
    scm_row(page, "src/old.py").click()
    expect(tab(page, "old.py (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    assert merge_values(page) == ["old = 1\n", ""]
    assert page.evaluate(f"() => document.querySelector('{VIEW} .CodeMirror-merge-editor .CodeMirror').CodeMirror.getOption('readOnly')")
    expect(page.locator(f"{VIEW} .CodeMirror-merge-copy")).to_have_count(0)
    expect(page.locator(f"{VIEW} .diff-head [data-act=open]")).to_be_disabled()
    scm_row(page, "new.txt").click()   # replaces the preview tab
    expect(tab(page, "new.txt (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    expect(tab(page, "old.py (Working Tree)")).to_have_count(0)
    assert merge_values(page) == ["", "brand new\n"]


def test_next_and_previous_changed_file(page):
    open_scm(page)
    nxt = page.locator('.scm .side-actions [data-act="next-file"]')
    nxt.click()
    expect(tab(page, "new.txt (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    nxt.click()
    expect(tab(page, "app.py (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    expect(scm_row(page, "src/app.py")).to_have_class(re.compile(r"\bactive\b"))
    page.locator(f"{VIEW} .diff-head [data-act=next-file]").click()
    expect(tab(page, "old.py (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    page.locator('.scm .side-actions [data-act="prev-file"]').click()
    expect(tab(page, "app.py (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    names = page.locator(".etab .etab-name").all_text_contents()
    assert names == ["README.md", "app.py (Working Tree)"]   # one preview tab, replaced each time
    # pinned diff tabs come back after a reload
    tab(page, "app.py (Working Tree)").dblclick()
    page.reload()
    expect(tab(page, "app.py (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    assert merge_values(page) == [APP, APP_NOW]


# ---------------------------------------------------------------- discard

def test_discard_asks_first(page, repo):
    open_scm(page)
    # an unsaved edit in the open file goes too
    page.evaluate("() => Tabs.open('src/app.py', {preview: false})")
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    page.locator("#editor .CodeMirror").click()
    page.keyboard.press("Control+End")
    page.keyboard.type("# unsaved\n")
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bdirty\b"))

    def discard(path, item):
        scm_row(page, path).click(button="right")
        page.locator(".ctx-menu").get_by_role("menuitem", name=item).click()
        return page.get_by_role("alertdialog")

    dialog = discard("src/app.py", "Discard changes…")
    expect(dialog).to_contain_text("unsaved changes in the editor will be lost")
    dialog.get_by_role("button", name="Cancel").click()
    assert disk(repo, "src/app.py") == APP_NOW
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bdirty\b"))
    discard("src/app.py", "Discard changes…").get_by_role("button", name="Discard").click()
    expect_rows(page, ["new.txt U", "src/old.py D"])
    assert disk(repo, "src/app.py") == APP
    expect(tab(page, "app.py")).not_to_have_class(re.compile(r"\bdirty\b"))
    assert page.evaluate("() => Tabs.cm().getValue()") == APP
    # untracked: deleted; deleted: restored
    discard("new.txt", "Delete untracked file…").get_by_role("button", name="Delete").click()
    expect_rows(page, ["src/old.py D"])
    assert not (repo / "new.txt").exists()
    discard("src/old.py", "Discard changes…").get_by_role("button", name="Restore").click()
    expect_rows(page, [])
    assert disk(repo, "src/old.py") == "old = 1\n"
    expect(page.locator(".scm-empty")).to_have_text("No changes.")
    expect(badge(page)).to_be_hidden()


# ---------------------------------------------------------------- the Branch base

def test_branch_mode(browser, srv, tmp_path):
    r = make_repo(tmp_path / "ws", FILES)
    git(r, "checkout", "-q", "-b", "feature")
    (r / "README.md").write_text("# Readme\n\nHello branch.\n", encoding="utf-8")
    (r / "docs" / "feature.md").write_text("# Feature\n", encoding="utf-8")
    git(r, "add", "README.md", "docs/feature.md")
    git(r, "commit", "-q", "-m", "feature work")
    make_changes(r)
    open_root(srv, r)
    page, errors = open_page(browser, srv)
    expect(page.locator("#stat-branch")).to_contain_text("⎇ feature")
    open_scm(page)
    expect_rows(page, CHANGED)
    page.locator('.scm-base [data-mode="branch"]').click()
    expect_rows(page, ["README.md M", "docs/feature.md A"] + CHANGED)
    expect(page.locator(".scm-desc")).to_contain_text("against main")
    expect(badge(page)).to_have_text("5")
    scm_row(page, "README.md").click()
    expect(tab(page, "README.md (vs main)")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    assert merge_values(page) == [FILES["README.md"], "# Readme\n\nHello branch.\n"]
    # discarding is for uncommitted changes only
    scm_row(page, "README.md").click(button="right")
    expect(page.locator(".ctx-menu").get_by_role("menuitem", name="Discard changes (Uncommitted only)")).to_be_disabled()
    page.keyboard.press("Escape")
    # back on main, Branch falls back to uncommitted changes and says why
    page.locator('.scm-base [data-mode="head"]').click()
    expect_rows(page, CHANGED)
    git(r, "stash", "-u")
    git(r, "checkout", "-q", "main")
    git(r, "stash", "pop")
    page.locator('.scm-base [data-mode="branch"]').click()
    expect(page.locator(".scm-note")).to_contain_text("main is the default branch")
    expect_rows(page, CHANGED)
    expect(page.locator("#stat-branch")).to_contain_text("⎇ main", timeout=5000)
    page.context.close()
    assert not errors, errors


# ---------------------------------------------------------------- gutter change bars

def test_gutter_change_bars(page):
    page.evaluate("() => Tabs.open('src/app.py', {preview: false})")
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    bars = page.locator("#editor .CodeMirror-gutter-background")
    expect(bars).to_have_count(2)
    expect(page.locator("#editor .CodeMirror-gutter-background.scm-mod")).to_have_count(2)
    # typing a new line between unchanged ones adds a green bar, removing one a red triangle
    # (recomputed as you type)
    page.locator("#editor .CodeMirror").click()
    page.evaluate("() => Tabs.cm().setCursor({line: 7, ch: 0})")
    page.keyboard.type("    z = 0\n")
    expect(page.locator("#editor .CodeMirror-gutter-background.scm-add")).to_have_count(1)
    page.evaluate("() => Tabs.cm().replaceRange('', {line: 3, ch: 0}, {line: 4, ch: 0})")
    expect(page.locator("#editor .CodeMirror-gutter-background.scm-del")).to_have_count(1)
    expect(page.locator("#editor .CodeMirror-gutter-background.scm-mod")).to_have_count(2)
    # Alt+F5 steps through the changes; clicking a bar opens the diff there
    page.locator("#editor .CodeMirror").click()
    page.keyboard.press("Control+Home")
    page.keyboard.press("Alt+F5")
    assert page.evaluate("() => Tabs.cm().getCursor().line") == 1
    bar = page.locator("#editor .CodeMirror-gutter-background.scm-mod").first.bounding_box()
    page.mouse.click(bar["x"] + bar["width"] - 4, bar["y"] + bar["height"] / 2)
    expect(tab(page, "app.py (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    assert merge_values(page)[1] == page.evaluate("() => Tabs.model('src/app.py').doc.getValue()")


def test_gutter_change_bars_for_a_renamed_file(page, repo):
    """A file renamed since HEAD (git mv) is compared with HEAD's file of the old name."""
    git(repo, "mv", "docs/guide.md", "docs/howto.md")
    (repo / "docs" / "howto.md").write_text("# Guide\n\nOther text.\n", encoding="utf-8")
    page.evaluate("() => SCM.refresh()")
    page.wait_for_function("() => SCM.status('docs/howto.md')?.status === 'R'")
    page.evaluate("() => Tabs.open('docs/howto.md', {preview: false})")
    expect(page.locator("#doc-name")).to_have_text("docs/howto.md")
    expect(page.locator("#editor .CodeMirror-gutter-background.scm-mod")).to_have_count(1)
    expect(page.locator("#editor .CodeMirror-gutter-background")).to_have_count(1)


# ---------------------------------------------------------------- review fixes

LINES = "".join(f"l{i}\n" for i in range(1, 9))


def test_a_double_click_on_an_inline_revert_reverts_one_change(browser, srv, tmp_path):
    """The inline view's rows are rebuilt 300 ms after an edit: a second click before then (a
    double-click) used the old rows and deleted, or duplicated, lines next to the change."""
    r = make_repo(tmp_path / "ws", {"README.md": "# Readme\n", "notes.md": LINES, "g.txt": LINES})
    (r / "notes.md").write_text(LINES.replace("l2\n", "l2\nADD1\nADD2\n"), encoding="utf-8")
    (r / "g.txt").write_text(LINES.replace("l3\nl4\n", ""), encoding="utf-8")
    open_root(srv, r)
    page, errors = open_page(browser, srv)
    open_scm(page)
    for path in ("notes.md", "g.txt"):
        scm_row(page, path).dblclick()
        expect(tab(page, f"{path} (Working Tree)")).to_have_class(re.compile(r"\bactive\b"))
        page.locator(f"{VIEW} .diff-head [data-view=inline]").click()
        btn = page.locator(f"{VIEW} .diff-inline .diff-revert-btn")
        expect(btn).to_have_count(1)
        btn.dblclick()
        page.wait_for_timeout(600)
        assert page.evaluate(f"() => Tabs.model('{path}').doc.getValue()") == LINES, path
    page.wait_for_timeout(1500)   # notes.md autosaves
    assert disk(r, "notes.md") == LINES
    page.context.close()
    assert not errors, errors


def test_a_deleted_files_diff_counts_its_lines(page, repo):
    (repo / "README.md").unlink()   # three lines, the last one ending in a newline
    open_scm(page)
    expect_rows(page, ["README.md D"] + CHANGED)
    scm_row(page, "README.md").click()
    stats = page.locator(f"{VIEW} .diff-stats")
    expect(stats).to_have_text("+0 −3")
    page.locator(f"{VIEW} .diff-head [data-view=inline]").click()
    expect(page.locator(f"{VIEW} .diff-inline .diff-del-line")).to_have_count(3)
    expect(stats).to_have_text("+0 −3")


def test_a_deleted_files_diff_tab_follows_the_file_back(page, repo):
    open_scm(page)
    scm_row(page, "src/old.py").dblclick()
    diff = tab(page, "old.py (Working Tree)")
    expect(diff).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(f"{VIEW} .diff-head [data-act=open]")).to_be_disabled()
    git(repo, "checkout", "--", "src/old.py")   # as Claude might, in the terminal
    expect_rows(page, ["new.txt U", "src/app.py M"])
    tab(page, "README.md").click()
    diff.click()
    expect(page.locator(f"{VIEW} .diff-head [data-act=open]")).to_be_enabled(timeout=5000)
    assert merge_values(page) == ["old = 1\n", "old = 1\n"]
    expect(page.locator(f"{VIEW} .diff-stats")).to_have_text("No changes")


def test_nested_repositories_are_mentioned_not_listed(page, repo):
    make_repo(repo / "vendor" / "lib", {"x.txt": "x\n"})
    open_scm(page)
    expect(page.locator(".scm-foot")).to_contain_text("1 folder with a git repository of its own", timeout=5000)
    expect_rows(page, CHANGED)


def test_hidden_diffs_and_the_gutter_leave_typing_alone(browser, srv, tmp_path):
    """A big file with thousands of changed lines: its change bars are worked out off the main
    thread, and its diff tab, while hidden, does not diff it again after every edit."""
    base = "".join(f"line {i} = {i}\n" for i in range(10000))
    now = "".join(f"line {i} = {i}\n" if i % 2 else f"line {i} = changed\n" for i in range(10000))
    r = make_repo(tmp_path / "ws", {"README.md": "# Readme\n", "heavy.py": base})
    (r / "heavy.py").write_text(now, encoding="utf-8")
    open_root(srv, r)
    page, errors = open_page(browser, srv)
    open_scm(page)
    scm_row(page, "heavy.py").dblclick()
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible(timeout=10000)
    page.locator(f"{VIEW} .diff-head [data-act=open]").click()   # the file's own tab, the diff hidden
    expect(page.locator("#doc-name")).to_have_text("heavy.py")
    expect(page.locator("#editor .CodeMirror-gutter-background.scm-mod").first).to_be_attached(timeout=10000)
    page.wait_for_timeout(1500)
    page.evaluate("""() => {
        window.longTasks = [];
        new PerformanceObserver(l => { for (const e of l.getEntries()) window.longTasks.push(Math.round(e.duration)); })
            .observe({type: 'longtask'});
    }""")
    page.locator("#editor .CodeMirror").click()
    for ch in "abc":
        page.keyboard.type(ch)
        page.wait_for_timeout(500)
    page.wait_for_timeout(1500)
    slow = [d for d in page.evaluate("window.longTasks") if d > 300]
    assert slow == [], slow
    expect(page.locator("#editor .CodeMirror-gutter-background.scm-mod").first).to_be_attached()
    diff = tab(page, "heavy.py (Working Tree)")
    diff.click()   # shown again: up to date
    expect(page.locator(f"{VIEW} .CodeMirror-merge")).to_be_visible()
    page.wait_for_function(f"() => document.querySelector('{VIEW} .CodeMirror-merge-editor .CodeMirror').CodeMirror.getValue() === Tabs.model('heavy.py').doc.getValue()")
    page.context.close()
    assert not errors, errors
