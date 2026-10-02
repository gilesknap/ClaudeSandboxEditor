# MD Collaborative Editor

A local markdown editor that renders exactly like GitHub, with Claude built in. You can highlight any passage and ask Claude to rewrite, tighten, restyle or critique it, then accept the suggestion or keep the original.

## Install

[![PyPI](https://img.shields.io/pypi/v/md-collab-editor)](https://pypi.org/project/md-collab-editor/)

Install it from PyPI as a command with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install md-collab-editor
```

Or run it once without installing:

```bash
uvx --from md-collab-editor md-editor
```

`pipx install md-collab-editor` works too. Upgrade later with `uv tool upgrade md-collab-editor`. For the latest unreleased code, install from GitHub instead: `uv tool install git+https://github.com/garethnisbet/MDCollaborativeEditor`.

## Run

```bash
md-editor                       # edit the .md files in the current folder
md-editor ~/notes               # edit any folder
md-editor ~/proj/README.md      # edit one file (its folder becomes the root)
md-editor --port 9000 --no-browser
md-editor --agent claude        # side-panel terminal runs Claude Code without the sandbox
md-editor --ask-agent claude    # Ask Claude runs claude -p without the sandbox
```

The editor itself needs only Python 3.9+ (standard library, no dependencies). By default *Ask Claude* and the side-panel terminal both run Claude Code inside [claude-sandbox](https://pypi.org/project/claude-sandbox/), which needs [uv](https://docs.astral.sh/uv/), rootless podman (or docker) and `/dev/net/tun` on the host, and Linux or macOS. With `--ask-agent claude` or `--agent claude` they use the `claude` CLI on your PATH instead; uv can't install `claude`, because it isn't a Python package. Without any of these, everything except Claude still works. The page loads its libraries from a CDN, so the browser needs internet access.

The server listens on 127.0.0.1 only and checks the `Host` and `Origin` of every request, so other websites can't use it, even by DNS rebinding; forwarding the port to another local port (8765 to 8766, say) still works.

To work on the editor itself, clone the repo and run `uv run md-editor docs`, which uses the code in the checkout. Run the tests with `uv run pytest`; the browser tests also need `uv run playwright install chromium` (and Python 3.10 or later), and are skipped without it. The tests use stand-ins for claude-sandbox and Claude Code, so they need neither, and GitHub Actions runs them on Python 3.9 and 3.13 for every push and pull request. The terminal tests, the browser tests and the tests that go through the claude-sandbox stand-in need Linux (they use util-linux 2.35+ `script`, GNU tools and `/proc`), so on other systems they are skipped.

## Working with Claude

1. **Highlight** text in the editor or in the rendered preview. In the preview a pop-up opens straight away; in the editor click the small *✦ Ask Claude* pill or press <kbd>Ctrl</kbd>+<kbd>J</kbd>.
2. **Choose** a preset (*My style*, *Improve*, *Tighten*, *Expand*, *Simplify*, *Fix grammar*, *More formal/casual*, *To bullets/prose*, *Critique*) or type your own instruction. An instruction ending in `?` is treated as a question: Claude replies with a comment and leaves the text alone.
3. A **card** appears on the *Suggestions* tab of the Claude panel, and the passage is highlighted in purple while Claude works and in amber when the suggestion is ready. Each card offers:
   - **Changes / Preview / Edit**: a word-level diff, the rendered result, or a text box for tweaking it by hand;
   - **Accept**: replace the passage (Ctrl+Z undoes it);
   - **Keep original**: discard the suggestion;
   - **Retry**: ask for a different version;
   - **Refine…**: give feedback such as "shorter" or "keep the first sentence" and get a revised version.
4. With nothing selected, the request applies to the whole document.

You can run several requests at once, and you can keep editing while Claude works, because each card tracks its passage as the text moves. *My style* uses the `nisbet-writing-style` skill; any skill in `~/.claude/skills` appears as a preset. The model menu in the top bar picks Opus, Sonnet or Haiku.

### Where requests run

Each request runs `claude -p` (headless Claude Code), with its tools limited to reading files and using skills, so it uses your Claude login and needs no API key.

- By default it runs inside claude-sandbox, as `uvx claude-sandbox@latest shell -c …` in the open folder (the requirements are the [terminal's](#claude-code-in-the-side-panel)). The request reaches the container through a short-lived file, `.md-editor-ask-….txt`, in the open folder, so the folder must be writable. Documents larger than about 100 kB can't be passed into the sandbox, so *Ask Claude* reports an error for them. claude-sandbox stops the folder's container when its last session ends, so unless the Terminal tab has a session running in the same folder, every request waits for the container to start again; a terminal session started just as a request finishes can find the container stopping, and **Restart** fixes that.
- `--ask-agent` (or the `MDEDIT_ASK_AGENT` environment variable) picks how requests run: `md-editor --ask-agent claude` runs `claude -p` natively, without the sandbox, and `--ask-agent none` turns *Ask Claude* off (the button is greyed out and says why).
- The document is sent to Claude once per version. When the *✦ Ask Claude* pill or the ask bar appears, a Claude session that holds the document starts in the background, and each card continues a copy of it, sending only the selection and the instruction (and, after small edits, a diff), so later cards are quicker and cheaper. *Retry* and *Refine* continue the card's own conversation. These sessions belong to the folder `~/.cache/md-editor/ask` (in the container, or in your own home folder when run natively), so they stay out of your project's `/resume` list.

## Claude Code in the side panel

The ✦ panel has two tabs: **Suggestions** holds the *Ask Claude* cards, and **Terminal** runs a full interactive Claude Code session in the folder you have open. Ask it to work on your documents and its edits appear in the editor as it saves them. Drag the panel's left edge to widen it.

By default the terminal runs `uvx claude-sandbox@latest`, which runs Claude Code inside a [claude-sandbox](https://pypi.org/project/claude-sandbox/) container for the open folder. That needs [uv](https://docs.astral.sh/uv/), rootless podman (or docker) and `/dev/net/tun` on the host; the first start pulls the container image, so it takes a while.

- The session starts the first time the Terminal tab is shown. It survives page reloads and ends when md-editor quits. After a reload or reconnect the last 512 KiB of output is replayed, the terminal modes Claude Code set (its full screen, mouse reporting, bracketed paste) are restored, and Claude Code redraws its screen.
- There is one session, shared by every browser tab, so it is best open in one tab at a time: it has one size, set by the tab that resized it last, and other tabs draw it wrongly until they resize it.
- **Restart** ends the session and starts a new one in the open folder. When a session ends by itself, press <kbd>Enter</kbd> to start another.
- If you open a different folder, the session stays where it was and a notice offers to restart it in the new folder.
- <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>C</kbd> copies the terminal selection, and <kbd>Ctrl</kbd>+<kbd>V</kbd> pastes. While the terminal has focus the editor's own shortcuts are off, so keys such as <kbd>Ctrl</kbd>+<kbd>O</kbd> and <kbd>Esc</kbd> go to Claude Code.
- `--agent` (or the `MDEDIT_AGENT` environment variable) picks the command: `md-editor --agent claude` runs Claude Code natively, without the sandbox, and `--agent none` turns the terminal off.

The terminal needs Linux or macOS, because it runs on a pseudo-terminal. Only the editor's own page can connect to it, because the server checks the `Host` and `Origin` of every request.

## Working with Claude Code in a terminal

Documents are plain files on disk. When Claude Code, or anything else, edits a file that is open in a tab, the editor updates within about a second and briefly flashes the changed text, whether or not that tab is the one on screen. If you had unsaved edits at that moment, a banner asks which version to keep (and if you undo them, the file on disk is loaded). A file deleted or moved on disk keeps its tab, struck through, until you close it or save it again; it is only written back when you ask (<kbd>Ctrl</kbd>+<kbd>S</kbd>, or *Save to re-create it* in its banner), never by autosave, so Claude's `rm` or `git mv` sticks. Markdown files are saved automatically shortly after you stop typing; other files are saved when you press <kbd>Ctrl</kbd>+<kbd>S</kbd> (see [Editing any file](#editing-any-file)).

## Reviewing changes

The editor doubles as a small IDE for reviewing what Claude, running in the side panel, has done to a project.

### Explorer and activity bar

The thin strip at the far left is the activity bar: **Explorer**, **Source Control** and **Search**. Click an icon to show its panel in the side bar, and click it again (or the ☰ button in the top bar) to hide the side bar. Drag the side bar's right edge to resize it; double-click the edge to reset it. In a narrow window the side bar and the Claude panel open over the editor, one at a time.

The Explorer shows every file in the open folder as a tree, loading each folder as you expand it, folders first. Files that git ignores (when the folder is in a git repository), dotfiles and folders such as `node_modules` are hidden until you turn on the eye toggle; `.git` is never shown. **MD** shows just the markdown files and the folders that hold them, as the old file list did. The tree follows files that appear or disappear on disk, and it highlights the active file, expanding folders to show it.

- The buttons above the tree make a new file or folder, refresh, collapse every folder and reveal the active file; **Open…** opens another folder and **+ New** makes a markdown file (a name without an extension gets `.md`).
- Right-click a file or folder for New file, New folder, Rename, Delete (folders are deleted with everything in them, after a confirmation that says how many files that is), Copy relative path, Copy path and Send to Claude terminal.
- With the tree focused, the arrow keys move and open or close folders, <kbd>Enter</kbd> opens a file, <kbd>Space</kbd> previews it without leaving the tree, <kbd>F2</kbd> renames, <kbd>Delete</kbd> deletes and <kbd>@</kbd> sends the path to the Claude terminal.

### Tabs

Each open file has a tab above the editor. A single click in the Explorer opens a *preview* tab, shown in italics, which the next single click replaces; double-click the file or the tab, start editing or ask Claude about it to keep it open. Middle-click a tab or click its × to close it (<kbd>Alt</kbd>+<kbd>W</kbd> closes the active one, and right-click a tab for more). A tab keeps its own undo history, selection and scroll position, and *Ask Claude* cards stay with the file they were made in, so you can accept a suggestion while another file is on screen. The open tabs are remembered for each folder and come back when you reload or open that folder again.

### Editing any file

Any text file can be edited, with syntax highlighting chosen from its name. Only markdown has the formatting toolbar and the preview; other files use the whole width, with line numbers. Binary files and files over 5 MB open as a placeholder, and images open in a viewer.

- **Saving.** <kbd>Ctrl</kbd>+<kbd>S</kbd> always saves. The status bar's *Autosave* switch applies to the kind of file on screen and is remembered separately for markdown (on unless you turn it off) and for other files (off unless you turn it on). A file with unsaved changes has a ● on its tab and after its name in the top bar.
- Closing a tab with unsaved changes asks whether to save them, as do opening another folder and reloading the page.
- Spell checking is on for markdown and off for other files, where it checks only comments and strings when you turn it on; the setting is remembered for each kind.

### Source control

**Source Control** in the activity bar lists the files that git sees as changed, each with a letter: **M** modified, **A** added, **D** deleted, **R** renamed, **U** untracked and **C** in conflict. The count is shown on its icon, and the Explorer shows the same letters (and a dot on folders with changes inside). The status bar shows the repository and its branch (`⎇ main`); click it to open the panel. The list keeps up with git by itself (every 2 seconds while the panel is open, every 10 seconds otherwise, and straight after you save), so you can watch what Claude is doing.

- **Uncommitted** compares the working tree with HEAD, untracked files included, like VS Code's Changes list. **Branch** compares it with the commit where the branch left the default branch (`origin/HEAD`, else `main`, else `master`, as `git merge-base` finds it), so you can review everything done across several commits, as in a pull request. On the default branch itself, Branch says so and shows the uncommitted changes.
- Only files in the open folder are listed; the panel says how many changes elsewhere in the repository it leaves out, and how many folders inside it hold a repository of their own (a cloned dependency, or a git worktree), which it doesn't list.
- Click a file to open its diff in a preview tab, double-click to keep it open, and right-click for Open diff, Open file, Discard changes, Copy relative path, Reveal in Explorer and Send to Claude terminal. The buttons at the top move to the next or previous changed file and refresh the list; the arrow keys and <kbd>Enter</kbd> work in the list too.
- **Discard changes** (Uncommitted only) asks first, then puts the file back as it is in HEAD, in git's index too; an untracked file is deleted. Unsaved edits to that file in the editor are lost as well. The editor never stages, commits or pushes.
- md-editor runs git itself, on your machine and outside the sandbox that Claude runs in, while the sandbox can write the repository's `.git` folder. So it turns off everything a repository's configuration could make git run: the fsmonitor hook, hooks, submodules, and clean/smudge filters other than Git LFS's. Git LFS's filter is turned off too when the configuration also names commands for git-lfs itself (LFS extensions or custom transfer agents), and git-lfs is never allowed to download, so Discard puts back an LFS file only from objects already on your disk. Files that use another filter (nbstripout, say) may therefore be listed as changed when git itself would not list them. The filters are read from git's configuration just before each command that could run one, so something that kept rewriting `.git/config` could still get one in between.

### Diffs

A diff opens in its own tab, titled `app.py (Working Tree)`, or `app.py (vs main)` in Branch mode, with the base version on the left (read-only) and the file on the right. The right-hand side *is* the file: edit it there and its own tab shows the same text, the same ● and the same undo history, and <kbd>Ctrl</kbd>+<kbd>S</kbd> and the autosave rules apply as usual. The arrows between the two sides put a change back as it is in the base (only the text in the editor changes, so save to keep it). **Inline** shows the changes in one column instead, with ↶ in the gutter to put a change back; the choice of view is remembered. The ↑ and ↓ buttons, or <kbd>Alt</kbd>+<kbd>F5</kbd> and <kbd>Shift</kbd>+<kbd>Alt</kbd>+<kbd>F5</kbd>, move from change to change, and the buttons beside them open the file and step through the changed files. A deleted file has only the left side, a new one only the right, and a binary file is just reported as changed. Right-click in a diff for Revert this change, Open file at this line, Copy and Send to Claude terminal (<kbd>Shift</kbd>+right-click gives the browser's own menu).

In the editor, a file in a git repository has change bars in its gutter, as in VS Code: green for added lines, blue for changed ones and a red triangle where lines were removed, against HEAD and kept up to date as you type. Click a bar to open the diff at that place; <kbd>Alt</kbd>+<kbd>F5</kbd> steps through the changes here too.

### Quick open

Press <kbd>Ctrl</kbd>+<kbd>P</kbd> to go to a file by name. Type some of the letters of its name, in order (`utjs` finds `util.js`; add part of a folder name or a `/` to narrow it down), and the best matches come first, with the matched letters highlighted. With nothing typed, the files you opened most recently come first, and <kbd>Enter</kbd> goes back to the one before the file on screen.

- The arrow keys and <kbd>Enter</kbd> open the file in a preview tab; <kbd>Ctrl</kbd>+<kbd>Enter</kbd> or a double-click keeps it open. End the name with `:42` to go to line 42. <kbd>Esc</kbd> closes the list.
- The list holds every file that git doesn't ignore, or, in a folder that isn't a git repository, every file outside hidden folders.
- <kbd>Ctrl</kbd>+<kbd>P</kbd> no longer prints: use the browser's menu for that.

### Find in files

<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>F</kbd> opens the **Search** panel, filled in with the selected text when there is some. Results appear as you type, grouped by file, each with its line and the match highlighted. The buttons in the search box match case (**Aa**, <kbd>Alt</kbd>+<kbd>C</kbd>), whole words (**ab**, <kbd>Alt</kbd>+<kbd>W</kbd>) and regular expressions (**.\***, <kbd>Alt</kbd>+<kbd>R</kbd>). The box below limits the search to some files: globs separated by commas, such as `*.py, src/`, where `!` excludes (`!*.min.js`) and a name without a `/` matches in any folder.

- Click a result to open its file in a preview tab with the match selected; the focus stays in the results, so the arrow keys move on through them. Double-click a result, or press <kbd>Enter</kbd>, to open it in the editor (a double-click also keeps the tab open). <kbd>Space</kbd> previews, Left and Right fold and unfold a file, and <kbd>Delete</kbd> dismisses a result. Right-click for Copy relative path, Reveal in Explorer and Send to Claude terminal.
- In a git repository the search uses `git grep`, so it covers tracked files (force-added ones that an ignore rule matches too) and untracked files that git doesn't ignore, but not binary files or files over 5 MB; elsewhere it reads every text file under 5 MB outside hidden folders. At most 2,000 matches are shown, and the panel says when there were more.
- A search stops after a minute. A regular expression is matched in a separate process, so one that backtracks endlessly (`(\w+\s?)+$` on a long line, say) only costs that minute, and the search is stopped as soon as you change it.
- The search, its options and the globs are remembered. Click ↻ to search again after files have changed.

### Send to the Claude terminal

**Send to Claude terminal** in the menus of the Explorer, the tabs, the Source Control list and the search results types `@path` into the Claude Code prompt in the side panel. In an editor, <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>L</kbd>, or **Send selection to Claude terminal** in its right-click menu, types `@path#L12-20` for the selected lines (`@path#L12` for one line, and just `@path` with nothing selected), so you can ask Claude about them. Nothing is sent until you press <kbd>Enter</kbd> in the terminal: the text arrives as a single paste, so Claude Code takes it as it is rather than opening its file picker, and the Terminal tab comes to the front with the focus. The path is relative to the folder the terminal runs in, or absolute for a file outside it.

The editor's right-click menu also has Ask Claude, Cut, Copy, Paste and Select all; <kbd>Shift</kbd>+right-click gives the browser's own menu.

### Shortcuts

| Keys | Action |
| --- | --- |
| <kbd>Ctrl</kbd>+<kbd>S</kbd> | Save the active file |
| <kbd>Alt</kbd>+<kbd>W</kbd> | Close the active tab |
| <kbd>Ctrl</kbd>+<kbd>P</kbd> | Go to a file by name (quick open) |
| <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>F</kbd> | Find in files |
| <kbd>Alt</kbd>+<kbd>C</kbd> / <kbd>W</kbd> / <kbd>R</kbd> | Match case / whole words / regular expressions, in the search box |
| <kbd>Ctrl</kbd>+<kbd>F</kbd>, <kbd>Ctrl</kbd>+<kbd>H</kbd> | Search, or replace, in the active file |
| <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>L</kbd> | Send the selected lines (or the file) to the Claude terminal |
| <kbd>Alt</kbd>+<kbd>F5</kbd>, <kbd>Shift</kbd>+<kbd>Alt</kbd>+<kbd>F5</kbd> | Next / previous change, in a diff or in a file with change bars |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> | Open another folder or file |
| <kbd>Ctrl</kbd>+<kbd>J</kbd> | Ask Claude about the selection |
| Arrows, <kbd>Enter</kbd>, <kbd>Space</kbd>, <kbd>F2</kbd>, <kbd>Delete</kbd>, <kbd>@</kbd> | Move, open, preview, rename, delete and send to the Claude terminal in the Explorer |

On a Mac, use <kbd>Cmd</kbd> for <kbd>Ctrl</kbd> and <kbd>Option</kbd> for <kbd>Alt</kbd>; replace is <kbd>Cmd</kbd>+<kbd>Option</kbd>+<kbd>F</kbd>.

## Rendering

Rendering covers GitHub-flavoured markdown: tables, task lists (click the boxes in the preview to tick them), strikethrough, autolinks, `> [!NOTE]`-style alerts, syntax-highlighted code, `$…$` and `$$…$$` maths (KaTeX), ```` ```math ```` blocks, ```` ```mermaid ```` diagrams, heading anchors and inline HTML (sanitised). The preview uses `github-markdown-css` in light or dark (◐ button).

## Editing

For markdown, the toolbar covers headings, bold, italic, strikethrough, quotes, code, links, images, lists, task lists, tables and rules. Shortcuts: <kbd>Ctrl</kbd>+<kbd>B</kbd>/<kbd>I</kbd>/<kbd>K</kbd>, <kbd>Ctrl</kbd>+<kbd>F</kbd> to search, <kbd>Ctrl</kbd>+<kbd>H</kbd> to replace, <kbd>Tab</kbd> to indent, and <kbd>Enter</kbd> to continue lists. Scrolling in the editor and the preview stays in sync, and clicking a preview block moves the cursor to it. Misspelt words get a wavy red underline as you type (British English; code, URLs and HTML are skipped). Right-click one for suggestions, to add it to your dictionary (kept in the browser) or to ignore it, and click **abc✓** in the toolbar to turn checking off or on.

## Opening files elsewhere

Click **📂** in the top bar (or *Open…* in the Explorer, or press <kbd>Ctrl</kbd>+<kbd>O</kbd>) to browse the disk. Click folders to move through them (↑ goes to the parent, ⌂ goes home), or type or paste a path and press Enter. Clicking a `.md` file opens it and makes its folder the working folder, so the Explorer shows its neighbours and live sync keeps working. *Use this folder* switches to the current folder without picking a file. The arrow keys and Enter also work in the list. If any file has unsaved changes you are asked to save them first; the tabs of the folder you leave are remembered for when you come back.

## Exporting to PDF

Click **⬇ PDF** in the top bar. The document is rendered in GitHub's light style (even in dark mode) on A4 pages, with maths and diagrams included and without Claude's highlights. It is saved as `<name>.pdf` beside the markdown file and downloaded by the browser. Relative image links resolve because the page is printed from the markdown file's folder. This uses headless Google Chrome or Chromium; set `MDEDIT_CHROME=/path/to/chrome` if it isn't found on the PATH. For the browser's own print dialog, use Print in the browser's menu (<kbd>Ctrl</kbd>+<kbd>P</kbd> is [quick open](#quick-open) here); the print stylesheet prints only the rendered document.

## Files

All code lives in `src/md_collab_editor/`:

- `server.py`: HTTP server (file API, folder browsing and root switching, change events, the file tree, create / rename / delete, quick open's file list, find in files (`git grep` or a walk), git status, base versions and discard for source control, `/api/ask` → `claude -p` (in claude-sandbox by default), `/api/pdf` → headless Chrome, `/api/term` → WebSocket onto the side-panel terminal's PTY)
- `static/index.html`, `static/app.css`: layout and GitHub-style theme
- `static/render.js`: markdown → HTML, with source offsets on every block so preview selections map back to the source
- `static/app.js`: editor, preview and scroll sync, selection mapping, ask bar, suggestion cards and word diff
- `static/ui.js`: shared helpers (requests, context menus, dialogs, status bar items, the polling of open files and folders)
- `static/activity.js`: the activity bar and the resizable side bar
- `static/tabs.js`: editor tabs and open files (saving, autosave, changes on disk)
- `static/explorer.js`: the Explorer's file tree
- `static/scm.js`: the Source Control panel, diff tabs (CodeMirror's merge addon), discard, and the change bars in the gutter
- `static/linediff.js`, `static/scm-worker.js`: line diffs (diff_match_patch), and a worker that works out the change bars off the main thread
- `static/search.js`: quick open, the Search panel (find in files) and Send to Claude terminal from the editor
- `static/term.js`: the side-panel terminal (xterm.js)
