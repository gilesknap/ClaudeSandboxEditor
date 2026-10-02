"""The IDE side of the editor in headless Chromium (Playwright): the activity bar and Explorer
tree, editor tabs (preview and pinned), editing any text file with its save rules, on-disk
changes, and the tabs remembered per folder.

Each test opens a fresh git repository through /api/root on one shared server. Skipped like
test_e2e.py when Playwright or its Chromium is missing (MD_EDITOR_E2E=require makes that fail)."""
import os
import re

import pytest

from gitutil import SERVER_ENV, make_repo, open_root

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
    "docs/guide.md": "# Guide\n\nSome text.\n",
    "src/app.py": 'def main():\n    print("hi")\n',
    "src/util.js": "const a = 1;\n",
    "pic.png": bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                             "0000000d49444154789c63f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"),
    "blob.bin": b"\x00\x01\x02binary\x00",
    ".gitignore": "build/\n",
}


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


@pytest.fixture
def repo(srv, tmp_path):
    """A fresh repository (FILES committed, plus an ignored build/ folder) opened in the server."""
    r = make_repo(tmp_path / "ws", FILES)
    (r / "build").mkdir()
    (r / "build" / "out.txt").write_text("built\n", encoding="utf-8")
    return open_root(srv, r)


def open_page(browser, server):
    """A fresh browser profile on the editor; returns (page, errors logged)."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server.base)
    expect(pg.locator("#doc-name")).to_have_text("README.md")
    expect(row(pg, "src")).to_be_visible()
    return pg, errors


@pytest.fixture
def page(browser, srv, repo):
    """The editor on `repo` in a fresh browser profile; it must log no errors."""
    pg, errors = open_page(browser, srv)
    yield pg
    pg.context.close()
    assert not errors, errors


def row(pg, path):
    return pg.locator(f'#tree .tree-row[data-path="{path}"]')


def tab(pg, name):
    return pg.locator(".etab", has=pg.locator(".etab-name", has_text=re.compile(f"^{re.escape(name)}$")))


def tab_names(pg):
    return pg.locator(".etab .etab-name").all_text_contents()


def disk(repo, rel):
    return (repo / rel).read_text(encoding="utf-8")


def type_at_end(pg, text):
    pg.locator(".CodeMirror").click()
    pg.keyboard.press("Control+End")
    pg.keyboard.type(text)


# ---------------------------------------------------------------- activity bar and tree

def test_activity_bar_and_tree(page):
    bar = page.locator("#activity-bar")
    expect(bar.get_by_role("tab", name="Explorer")).to_have_attribute("aria-selected", "true")
    expect(bar.get_by_role("tab", name="Source Control")).to_be_visible()
    expect(bar.get_by_role("tab", name="Search")).to_be_visible()
    # folders first, case-insensitive; ignored and hidden files only on request; never .git
    names = page.locator("#tree .tree-row").evaluate_all("rs => rs.map(r => r.dataset.path)")
    assert names == ["docs", "src", "blob.bin", "pic.png", "README.md"]
    page.locator("#tree-show-all").click()
    expect(row(page, "build")).to_have_class(re.compile(r"\bis-ignored\b"))
    expect(row(page, ".gitignore")).to_have_class(re.compile(r"\bis-hidden\b"))
    assert row(page, ".git").count() == 0
    page.locator("#tree-show-all").click()
    expect(row(page, "build")).to_have_count(0)
    # lazy expand, and the Markdown only view
    row(page, "src").click()
    expect(row(page, "src/app.py")).to_be_visible()
    page.locator("#tree-md-only").click()
    expect(row(page, "docs/guide.md")).to_be_visible()
    expect(row(page, "src")).to_have_count(0)
    page.locator("#tree-md-only").click()
    expect(row(page, "src/app.py")).to_be_visible()
    # the active icon collapses the side bar; the top bar's ☰ brings it back
    bar.get_by_role("tab", name="Explorer").click()
    expect(page.locator("#sidebar")).to_be_hidden()
    page.locator("#toggle-files").click()
    expect(page.locator("#sidebar")).to_be_visible()
    # its width can be dragged, and is remembered
    width = lambda: page.locator("#sidebar").bounding_box()["width"]
    before = width()
    box = page.locator("#sidebar-resize").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 300
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 80, y, steps=5)
    page.mouse.up()
    assert width() == pytest.approx(before + 80, abs=3)
    page.reload()
    expect(row(page, "src")).to_be_visible()
    assert width() == pytest.approx(before + 80, abs=3)


def test_tree_keyboard_and_live_updates(page, repo):
    page.locator("#tree").focus()
    page.keyboard.press("Home")
    page.keyboard.press("ArrowDown")      # src
    page.keyboard.press("ArrowRight")     # expand it
    expect(row(page, "src/app.py")).to_be_visible()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")          # opens src/app.py
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    (repo / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "top.txt").write_text("t\n", encoding="utf-8")
    expect(row(page, "src/new.py")).to_be_visible(timeout=5000)
    expect(row(page, "top.txt")).to_be_visible(timeout=5000)
    (repo / "top.txt").unlink()
    expect(row(page, "top.txt")).to_have_count(0, timeout=5000)


# ---------------------------------------------------------------- tabs

def test_preview_tabs_are_replaced_until_pinned(page):
    row(page, "src").click()
    row(page, "src/app.py").click()
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bpreview\b"))
    row(page, "src/util.js").click()
    expect(tab(page, "util.js")).to_have_class(re.compile(r"\bpreview\b"))
    assert tab_names(page) == ["README.md", "util.js"]   # the preview tab was replaced
    row(page, "src/util.js").dblclick()
    expect(tab(page, "util.js")).not_to_have_class(re.compile(r"\bpreview\b"))
    row(page, "src/app.py").click()
    expect(tab(page, "app.py")).to_be_visible()
    assert tab_names(page) == ["README.md", "util.js", "app.py"]
    # code: full width, no markdown toolbar or preview, highlighted
    expect(page.locator("#preview-pane")).to_be_hidden()
    expect(page.locator("#view-mode button").first).to_be_disabled()
    expect(page.locator("#toolbar [data-cmd=bold]")).to_be_hidden()
    expect(page.locator(".CodeMirror .cm-keyword").first).to_have_text("def")
    expect(page.locator("#stat-mode")).to_have_text("Python")
    # editing a preview tab pins it; middle-click and Alt+W close tabs
    type_at_end(page, "# more\n")
    expect(tab(page, "app.py")).not_to_have_class(re.compile(r"\bpreview\b"))
    tab(page, "README.md").click(button="middle")
    expect(tab(page, "README.md")).to_have_count(0)
    tab(page, "util.js").click()
    page.keyboard.press("Alt+w")
    expect(tab(page, "util.js")).to_have_count(0)
    # back on markdown the preview returns
    row(page, "README.md").click()
    expect(page.locator("#preview")).to_contain_text("Hello world.")
    expect(page.locator("#toolbar [data-cmd=bold]")).to_be_visible()


def test_images_and_binary_files(page):
    row(page, "pic.png").click()
    expect(page.locator(".file-view:not([hidden]) img")).to_be_visible()
    expect(page.locator(".file-view:not([hidden])")).to_contain_text("1 × 1")
    row(page, "blob.bin").click()
    expect(page.locator(".file-view:not([hidden])")).to_contain_text("binary")
    assert tab_names(page) == ["README.md", "blob.bin"]


def test_tabs_are_remembered_per_folder(page, srv):
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    row(page, "src/app.py").click()
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bactive\b"))
    page.reload()
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bactive\b"))
    assert tab_names(page) == ["README.md", "util.js", "app.py"]
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bpreview\b"))
    expect(page.locator("#doc-name")).to_have_text("src/app.py")


def test_reload_keeps_the_tab_on_screen_and_a_url_names_a_file(page):
    row(page, "pic.png").dblclick()
    expect(tab(page, "pic.png")).to_have_class(re.compile(r"\bactive\b"))
    page.reload()
    expect(tab(page, "pic.png")).to_have_class(re.compile(r"\bactive\b"))   # not the last text file
    page.evaluate("location.hash = '#docs/guide.md'")
    page.reload()
    expect(page.locator("#doc-name")).to_have_text("docs/guide.md")
    assert tab_names(page) == ["README.md", "pic.png", "guide.md"]


def test_alt_w_is_left_to_text_boxes(page):
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+o")
    expect(page.locator("#browser-path")).to_be_focused()
    page.keyboard.press("Alt+w")
    page.keyboard.press("Escape")
    expect(page.locator("#browser")).to_be_hidden()
    assert tab_names(page) == ["README.md"]


def test_a_narrow_window_shows_one_overlay_at_a_time(browser, srv, repo):
    ctx = browser.new_context(viewport={"width": 700, "height": 800})
    pg = ctx.new_page()
    pg.goto(srv.base)
    expect(pg.locator("#doc-name")).to_have_text("README.md")
    side, claude = pg.locator("#sidebar"), pg.locator("#claude")
    expect(side).to_be_hidden()
    expect(claude).to_be_hidden()
    pg.locator("#activity-bar").get_by_role("tab", name="Explorer").click()
    expect(side).to_be_visible()
    pg.locator("#toggle-claude").click()          # the Claude panel takes the overlay's place
    expect(claude).to_be_visible()
    expect(side).to_be_hidden()
    pg.locator("#activity-bar").get_by_role("tab", name="Search").click()   # and back
    expect(side).to_be_visible()
    expect(claude).to_be_hidden()
    assert pg.evaluate("[document.documentElement.scrollWidth, innerWidth]") == [700, 700]
    ctx.close()


# ---------------------------------------------------------------- editing and saving

def test_code_files_save_explicitly(page, repo):
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    expect(page.locator("#stat-autosave")).to_have_text("Autosave: off")
    type_at_end(page, "// edited\n")
    expect(tab(page, "util.js")).to_have_class(re.compile(r"\bdirty\b"))
    expect(page.locator("#doc-name")).to_have_class(re.compile(r"\bdirty\b"))
    page.wait_for_timeout(1500)   # longer than the autosave delay
    assert "// edited" not in disk(repo, "src/util.js")
    page.keyboard.press("Control+s")
    expect(tab(page, "util.js")).not_to_have_class(re.compile(r"\bdirty\b"))
    assert "// edited" in disk(repo, "src/util.js")
    # the toggle changes the setting for this kind of file only
    page.locator("#stat-autosave").click()
    expect(page.locator("#stat-autosave")).to_have_text("Autosave: on")
    type_at_end(page, "// auto\n")
    expect(tab(page, "util.js")).not_to_have_class(re.compile(r"\bdirty\b"), timeout=5000)
    assert "// auto" in disk(repo, "src/util.js")


def test_markdown_still_autosaves(page, repo):
    expect(page.locator("#stat-autosave")).to_have_text("Autosave: on")
    type_at_end(page, "More text.")
    expect(page.locator("#save-state")).to_have_text("Saved", timeout=5000)
    assert disk(repo, "README.md").endswith("More text.")


def test_changes_on_disk_reach_every_tab(page, repo):
    row(page, "src").click()
    row(page, "src/app.py").dblclick()
    tab(page, "README.md").click()
    (repo / "README.md").write_text("# Readme\n\nChanged outside.\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("print('outside')\n", encoding="utf-8")
    expect(page.locator(".CodeMirror")).to_contain_text("Changed outside.", timeout=5000)
    tab(page, "app.py").click()
    expect(page.locator(".CodeMirror")).to_contain_text("print('outside')")
    # unsaved edits are kept and a banner asks which version wins
    type_at_end(page, "# mine\n")
    (repo / "src" / "app.py").write_text("print('again')\n", encoding="utf-8")
    banner = page.locator("#banner")
    expect(banner).to_contain_text("changed on disk", timeout=5000)
    banner.get_by_role("button", name="Load disk version").click()
    expect(page.locator(".CodeMirror")).to_contain_text("print('again')")
    expect(tab(page, "app.py")).not_to_have_class(re.compile(r"\bdirty\b"))


def test_closing_a_dirty_tab_asks(page, repo):
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "// unsaved\n")
    t = tab(page, "util.js")
    t.hover()
    t.locator(".etab-close").click()
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text("Do you want to save the changes you made to util.js?")
    dialog.get_by_role("button", name="Cancel").click()
    expect(t).to_have_class(re.compile(r"\bdirty\b"))
    t.hover()
    t.locator(".etab-close").click()
    dialog.get_by_role("button", name="Don't save").click()
    expect(t).to_have_count(0)
    assert "// unsaved" not in disk(repo, "src/util.js")


# ---------------------------------------------------------------- file operations

def test_create_rename_and_delete_from_the_context_menu(page, repo):
    row(page, "docs").click(button="right")
    page.locator(".ctx-menu").get_by_role("menuitem", name="New file…").click()
    expect(page.locator("#tree input.tree-input")).to_be_focused()
    page.keyboard.type("notes.txt")
    page.keyboard.press("Enter")
    expect(tab(page, "notes.txt")).to_be_visible()
    assert (repo / "docs" / "notes.txt").read_text(encoding="utf-8") == ""
    row(page, "docs/notes.txt").click(button="right")
    page.locator(".ctx-menu").get_by_role("menuitem", name="Rename…").click()
    expect(page.locator("#tree input.tree-input")).to_be_focused()
    page.keyboard.press("Control+a")
    page.keyboard.type("notes.md")
    page.keyboard.press("Enter")
    expect(tab(page, "notes.md")).to_be_visible()   # the open tab follows the rename
    expect(page.locator("#preview-pane")).to_be_visible()   # and it is markdown now
    assert (repo / "docs" / "notes.md").exists() and not (repo / "docs" / "notes.txt").exists()
    row(page, "docs").click(button="right")
    page.locator(".ctx-menu").get_by_role("menuitem", name="Delete").click()
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text("2 files")
    dialog.get_by_role("button", name="Delete").click()
    expect(row(page, "docs")).to_have_count(0)
    expect(tab(page, "notes.md")).to_have_count(0)
    assert not (repo / "docs").exists()


def test_a_save_under_way_follows_a_rename(browser, srv, repo):
    page, errors = open_page(browser, srv)
    held = []
    # hold the first write back
    page.route("**/api/file", lambda r: held.append(r) if r.request.method == "PUT" and not held else r.continue_())
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "// typed\n")
    page.keyboard.press("Control+s")
    page.wait_for_function("() => !!Tabs.model('src/util.js').saving")   # (a Promise would be awaited)
    assert len(held) == 1
    row(page, "src/util.js").click(button="right")
    page.locator(".ctx-menu").get_by_role("menuitem", name="Rename…").click()
    page.keyboard.press("Control+a")
    page.keyboard.type("lib.js")
    page.keyboard.press("Enter")
    expect(tab(page, "lib.js")).to_be_visible()
    held[0].continue_()   # the write for the old name: refused, then made again under the new one
    expect(tab(page, "lib.js")).not_to_have_class(re.compile(r"\bdirty\b"))
    expect(page.locator("#banner")).to_be_hidden()
    assert "// typed" in disk(repo, "src/lib.js") and not (repo / "src" / "util.js").exists()
    page.context.close()
    assert [e for e in errors if "409 (Conflict)" not in e] == [], errors   # Chrome logs the refused write


def test_switching_folders_asks_about_unsaved_files(page, repo, tmp_path):
    other = make_repo(tmp_path / "other", {"README.md": "# Other\n"})
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "// unsaved\n")

    def switch():
        page.keyboard.press("Control+o")
        page.locator("#browser-path").fill(str(other))
        page.keyboard.press("Enter")
        expect(page.locator("#browser-path")).to_have_value(str(other))
        page.get_by_role("button", name="Use this folder").click()
        return page.get_by_role("alertdialog")

    switch().get_by_role("button", name="Cancel").click()
    expect(page.locator("#browser")).to_be_visible()   # still here, nothing saved
    page.keyboard.press("Escape")
    assert "// unsaved" not in disk(repo, "src/util.js")
    page.locator(".CodeMirror").click()
    switch().get_by_role("button", name="Save all").click()
    expect(page.locator("#stat-root")).to_have_text(str(other))
    assert tab_names(page) == ["README.md"]
    assert "// unsaved" in disk(repo, "src/util.js")
    # and back: that folder's tabs come back
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+o")
    page.locator("#browser-path").fill(str(repo))
    page.keyboard.press("Enter")
    expect(page.locator("#browser-path")).to_have_value(str(repo))
    page.get_by_role("button", name="Use this folder").click()
    expect(page.locator("#stat-root")).to_have_text(str(repo))
    expect(tab(page, "util.js")).to_have_class(re.compile(r"\bactive\b"))
    assert tab_names(page) == ["README.md", "util.js"]


# ---------------------------------------------------------------- files deleted or moved on disk

def wait_deleted(pg, name):
    expect(pg.locator("#banner")).to_contain_text(f"{name} has been deleted on disk", timeout=5000)


def test_the_deleted_banners_close_asks_about_unsaved_changes(page, repo):
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "// precious\n")
    (repo / "src" / "util.js").unlink()
    wait_deleted(page, "util.js")
    page.locator("#banner").get_by_role("button", name="Close").click()
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text("util.js has been deleted on disk. Do you want to save your changes to it?")
    dialog.get_by_role("button", name="Cancel").click()
    expect(tab(page, "util.js")).to_have_class(re.compile(r"\bdirty\b"))
    page.locator("#banner").get_by_role("button", name="Close").click()
    dialog.get_by_role("button", name="Save", exact=True).click()   # saving makes it again
    expect(tab(page, "util.js")).to_have_count(0)
    assert "// precious" in disk(repo, "src/util.js")


def test_autosave_never_makes_a_deleted_file_again(page, repo):
    """Claude's `git mv` or `rm` must stick: an edit of the open file (markdown autosaves) only
    goes back to disk when asked."""
    (repo / "README.md").rename(repo / "moved.md")
    wait_deleted(page, "README.md")
    type_at_end(page, " typed")
    expect(page.locator("#save-state")).to_have_text("Unsaved changes")
    page.wait_for_timeout(2000)   # well past the autosave delay
    assert not (repo / "README.md").exists()
    expect(tab(page, "README.md")).to_have_class(re.compile(r"\bdeleted\b"))
    page.keyboard.press("Control+s")   # asked: it is made again
    expect(page.locator("#banner")).to_be_hidden()
    assert disk(repo, "README.md").endswith(" typed")


def test_a_save_that_races_a_deletion_says_the_file_was_deleted(browser, srv, repo):
    page, errors = open_page(browser, srv)
    page.route("**/api/stat", lambda r: r.abort())   # the poll never sees the deletion
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "// mine\n")
    (repo / "src" / "util.js").unlink()
    page.keyboard.press("Control+s")
    wait_deleted(page, "util.js")
    expect(page.locator("#banner")).not_to_contain_text("changed on disk")
    expect(tab(page, "util.js")).to_have_class(re.compile(r"\bdirty\b"))
    assert not (repo / "src" / "util.js").exists()
    page.unroute("**/api/stat")
    page.wait_for_timeout(1500)
    wait_deleted(page, "util.js")   # the poll agrees
    page.context.close()
    assert [e for e in errors if "409 (Conflict)" not in e and "ERR_FAILED" not in e] == [], errors


def test_undoing_back_to_the_saved_text_after_a_conflict_loads_the_disk_version(page, repo):
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "Z")
    (repo / "src" / "util.js").write_text("const a = 1;\nclaude = 2;\n", encoding="utf-8")
    expect(page.locator("#banner")).to_contain_text("changed on disk", timeout=5000)
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+z")
    expect(page.locator("#banner")).to_be_hidden(timeout=5000)
    expect(page.locator("#save-state")).to_have_text("Updated from disk")
    assert page.evaluate("() => Tabs.model('src/util.js').doc.getValue()") == "const a = 1;\nclaude = 2;\n"
    expect(tab(page, "util.js")).not_to_have_class(re.compile(r"\bdirty\b"))


def rename_in_tree(pg, path, name):
    row(pg, path).click(button="right")
    pg.locator(".ctx-menu").get_by_role("menuitem", name="Rename…").click()
    expect(pg.locator("#tree input.tree-input")).to_be_focused()
    pg.keyboard.press("Control+a")
    pg.keyboard.type(name)
    pg.keyboard.press("Enter")


def test_renaming_onto_an_open_deleted_file(page, repo):
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "// unsaved\n")
    (repo / "src" / "util.js").unlink()
    wait_deleted(page, "util.js")
    row(page, "src/app.py").click()   # open too, so its tab and model move with the rename
    expect(tab(page, "app.py")).to_have_class(re.compile(r"\bactive\b"))
    # util.js's unsaved changes would be left with no file to save to: refused
    rename_in_tree(page, "src/app.py", "util.js")
    expect(page.locator("#toast")).to_contain_text("src/util.js is open with unsaved changes")
    page.keyboard.press("Escape")
    assert (repo / "src" / "app.py").exists() and not (repo / "src" / "util.js").exists()
    # once they are dropped (the tab stays, clean and deleted), the rename goes ahead and the
    # old tab gives way to the file moved there, which can be saved
    tab(page, "util.js").click()
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+z")
    page.keyboard.press("Control+z")
    expect(tab(page, "util.js")).not_to_have_class(re.compile(r"\bdirty\b"))
    rename_in_tree(page, "src/app.py", "util.js")
    expect(row(page, "src/app.py")).to_have_count(0, timeout=5000)
    assert page.evaluate("() => Tabs.models().map(m => [m.path, m.doc.getValue()])") == [
        ["README.md", "# Readme\n\nHello world.\n"], ["src/util.js", 'def main():\n    print("hi")\n']]
    assert tab_names(page) == ["README.md", "util.js"]
    tab(page, "util.js").click()
    type_at_end(page, "# more\n")
    page.keyboard.press("Control+s")
    expect(tab(page, "util.js")).not_to_have_class(re.compile(r"\bdirty\b"))
    assert disk(repo, "src/util.js") == 'def main():\n    print("hi")\n# more\n'


# ---------------------------------------------------------------- layout and what is remembered

def test_the_url_names_no_file_once_none_is_open(page):
    page.keyboard.press("Alt+w")
    expect(page.locator("#doc-name")).to_have_text("No file open")
    assert page.evaluate("location.hash") == ""
    page.reload()
    expect(row(page, "src")).to_be_visible()
    expect(page.locator("#save-state")).not_to_contain_text("Could not open")


def test_the_side_bar_panel_is_remembered(page):
    page.locator("#activity-bar").get_by_role("tab", name="Source Control").click()
    expect(page.locator(".scm-list")).to_be_visible()
    page.reload()
    expect(page.locator(".scm-list")).to_be_visible()
    expect(page.locator("#activity-bar").get_by_role("tab", name="Source Control")).to_have_attribute("aria-selected", "true")
    expect(page.locator(".scm-empty")).to_have_text("No changes.")   # and it asked git as usual


def test_the_side_bar_and_claude_panel_leave_room_for_the_editor(page):
    def layout():
        return page.evaluate("""() => ({wb: document.getElementById('workbench').getBoundingClientRect().width,
                                        sw: document.documentElement.scrollWidth, w: innerWidth})""")

    def drag(handle, dx):
        box = page.locator(handle).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + 300
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + dx, y, steps=8)
        page.mouse.up()

    expect(page.locator("#claude")).to_be_visible()
    drag("#sidebar-resize", 1200)
    assert layout()["wb"] >= 239 and layout()["sw"] == layout()["w"]
    drag("#panel-resize", -1200)
    assert layout()["wb"] >= 239 and layout()["sw"] == layout()["w"]
    page.reload()
    expect(row(page, "src")).to_be_visible()
    assert layout()["wb"] >= 239 and layout()["sw"] == layout()["w"]
    page.set_viewport_size({"width": 1000, "height": 900})
    page.wait_for_timeout(200)
    assert layout()["wb"] >= 239 and layout()["sw"] == layout()["w"]


# ---------------------------------------------------------------- the Explorer and the terminal

def fake_terminal(pg):
    """Term stands in for a running terminal: what Send to Claude terminal asks it to mention
    (the server works out the text, or sends it over the IDE link) lands in window.sent."""
    pg.evaluate("""() => {
        window.sent = [];
        Object.assign(Term, {available: () => true,
                             mention(path, range) { window.sent.push([path, range]); return Promise.resolve(true); }});
    }""")


def test_explorer_sends_paths_to_the_terminal(page, repo):
    fake_terminal(page)
    page.locator("#tree").focus()
    page.keyboard.press("Home")   # docs; the menu shows @ as the shortcut for sending it
    page.keyboard.press("@")
    page.wait_for_function("() => window.sent.length === 1")
    assert page.evaluate("window.sent") == [["docs", None]]
    page.locator("#tree").click(button="right", position={"x": 60, "y": 400})   # the open folder itself
    page.locator(".ctx-menu").get_by_role("menuitem", name="Send to Claude terminal").click()
    page.wait_for_function("() => window.sent.length === 2")
    assert page.evaluate("window.sent[1]") == ["", None]
    row(page, "src").click()
    row(page, "src/app.py").dblclick()
    expect(page.locator("#doc-name")).to_have_text("src/app.py")
    page.evaluate("() => { App.cm.focus(); App.cm.setSelection({line: 0, ch: 0}, {line: 1, ch: 3}); }")
    page.keyboard.press("Control+Alt+l")
    page.wait_for_function("() => window.sent.length === 3")
    assert page.evaluate("window.sent[2]") == ["src/app.py", {"from": 1, "to": 2}]


def test_send_without_a_terminal_keeps_the_side_bar(browser, srv, repo):
    ctx = browser.new_context(viewport={"width": 700, "height": 800})
    pg = ctx.new_page()
    pg.goto(srv.base)
    expect(pg.locator("#doc-name")).to_have_text("README.md")
    pg.locator("#activity-bar").get_by_role("tab", name="Explorer").click()
    row(pg, "src").click(button="right")
    pg.locator(".ctx-menu").get_by_role("menuitem", name="Send to Claude terminal").click()
    expect(pg.locator("#toast")).to_have_text("The Claude terminal is not available.")
    expect(pg.locator("#sidebar")).to_be_visible()
    expect(pg.locator("#claude")).to_be_hidden()
    ctx.close()


def test_a_file_name_that_is_not_utf8_opens(page, repo, browser, srv):
    with open(os.path.join(os.fsencode(repo), b"lat\xe9.txt"), "w") as f:
        f.write("latin-1 name\n")
    page.wait_for_function("() => [...document.querySelectorAll('#tree .tree-row')].some(r => r.dataset.path.startsWith('lat'))",
                           timeout=5000)
    page.evaluate("() => [...document.querySelectorAll('#tree .tree-row')].find(r => r.dataset.path.startsWith('lat')).click()")
    expect(page.locator("#doc-name")).to_have_text(re.compile(r"^lat.\.txt$"))
    expect(page.locator(".CodeMirror")).to_contain_text("latin-1 name")
    type_at_end(page, "more\n")
    page.keyboard.press("Control+s")
    expect(page.locator("#save-state")).to_have_text("Saved")
    assert open(os.path.join(os.fsencode(repo), b"lat\xe9.txt")).read() == "latin-1 name\nmore\n"
    # a URL names it too: the hash has the name's bytes %-escaped, which decodeURIComponent refuses
    pg, errors = open_page(browser, srv)
    pg.evaluate("location.hash = '#lat%E9.txt'")
    pg.reload()
    expect(pg.locator("#doc-name")).to_have_text(re.compile(r"^lat.\.txt$"))
    assert pg.evaluate(r"""() => ['a b/c%d.md', 'lat\udce9.txt', 'd\u00e9j\u00e0 \ud83d\ude42', '\udce2\udc82A', '%zz', '\udced\udca0\udc80']
                                  .filter(s => UI.decPath(UI.encPath(s)) !== s)""") == []
    pg.context.close()
    assert not errors, errors


def test_a_menu_opened_from_the_keyboard_gives_focus_back(page):
    page.locator("#tree").focus()
    page.keyboard.press("Home")
    menu = page.locator(".ctx-menu")
    for key in ("Shift+F10", "ContextMenu"):
        page.keyboard.press(key)
        expect(menu.get_by_role("menuitem").first).to_be_focused()
        page.keyboard.press("Escape")
        expect(menu).to_have_count(0)
        expect(page.locator("#tree")).to_be_focused()


def test_a_file_that_is_no_longer_text_on_disk_can_be_overwritten(browser, srv, repo):
    page, errors = open_page(browser, srv)
    row(page, "src").click()
    row(page, "src/util.js").dblclick()
    type_at_end(page, "// mine\n")
    banner = page.locator("#banner")
    # a save that finds it changed (before the poll does): a 409 with no text to load
    page.route("**/api/stat", lambda r: r.abort())
    (repo / "src" / "util.js").write_bytes(b"caf\xe9\n")
    page.keyboard.press("Control+s")
    expect(banner).to_contain_text("util.js is no longer a text file on disk (binary).")
    expect(banner.get_by_role("button")).to_have_text(["Keep mine (overwrite)"])
    banner.get_by_role("button", name="Keep mine (overwrite)").click()
    expect(banner).to_be_hidden()
    assert disk(repo, "src/util.js") == "const a = 1;\n// mine\n"
    page.unroute("**/api/stat")
    # the poll finds it
    (repo / "src" / "util.js").write_bytes(b"x\0y")
    expect(banner).to_contain_text("util.js is no longer a text file on disk (binary).", timeout=5000)
    banner.get_by_role("button", name="Keep mine (overwrite)").click()
    expect(banner).to_be_hidden()
    assert disk(repo, "src/util.js") == "const a = 1;\n// mine\n"
    page.context.close()
    assert [e for e in errors if "409 (Conflict)" not in e and "ERR_FAILED" not in e] == [], errors
