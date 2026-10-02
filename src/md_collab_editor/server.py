#!/usr/bin/env python3
"""MD Collaborative Editor: a local GitHub-style markdown editor with Claude in the loop.

Serves the editor UI, reads and writes .md files under a root folder, pushes
on-disk changes to the browser (so edits made by Claude Code in a terminal show
up live), and answers "ask Claude" requests by running `claude -p` headless,
inside claude-sandbox by default. The side panel also has a terminal running an
interactive Claude Code session (`uvx claude-sandbox@latest` in the open folder
by default) on a PTY, streamed to the browser over a WebSocket.

    md-editor                     # edit the current folder
    md-editor ~/notes             # edit a folder
    md-editor ~/proj/README.md    # edit one file (its folder becomes the root)
    md-editor --agent claude      # terminal runs plain Claude Code (no sandbox)
    md-editor --agent none        # no terminal
    md-editor --ask-agent claude  # Ask Claude runs plain `claude -p` (no sandbox)
"""

import argparse
import atexit
import base64
import collections
import difflib
import hashlib
import json
import itertools
import math
import mimetypes
import os
import posixpath
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

try:  # PTYs need a POSIX system (Linux, macOS)
    import fcntl
    import termios
    HAVE_PTY = hasattr(os, "openpty")
except ImportError:
    fcntl = termios = None
    HAVE_PTY = False

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
SKILLS_DIR = Path.home() / ".claude" / "skills"
MD_EXT = (".md", ".markdown")
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv"}

ROOT: Path = Path.cwd()
CHROME_BIN = next((b for b in (os.environ.get("MDEDIT_CHROME"), "google-chrome", "google-chrome-stable",
                               "chromium", "chromium-browser") if b and shutil.which(b)), None)
DEFAULT_AGENT = "uvx claude-sandbox@latest"
AGENT_CMD = shlex.split(DEFAULT_AGENT)  # argv for the side-panel terminal, or None when turned off
DEFAULT_ASK_AGENT = "uvx claude-sandbox@latest"
# argv for Ask Claude, or None when turned off: `claude` itself (native mode) or a
# claude-sandbox-compatible launcher that runs it in its container (launcher mode)
ASK_CMD = shlex.split(DEFAULT_ASK_AGENT)

PDF_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: https: data:; style-src https: 'unsafe-inline'; font-src https: data:">
<title>{title}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.1/github-markdown-light.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<style>
@page {{ size: A4; margin: 16mm 15mm 18mm; }}
body {{ margin: 0; background: #fff; }}
.markdown-body {{ font-size: 11pt; max-width: none; padding: 0; }}
.markdown-body .math-block {{ margin-bottom: 16px; }}
.markdown-body .mermaid-box {{ text-align: center; margin-bottom: 16px; }}
.markdown-body .mermaid-box svg {{ max-width: 100%; height: auto; }}
.markdown-body .mermaid-src, .markdown-body .anchor {{ display: none; }}
.markdown-body .task-list-item {{ list-style: none; }}
.markdown-body .task-list-item input {{ margin: 0 0.2em 0.25em -1.4em; vertical-align: middle; }}
.markdown-body pre, .markdown-body table, .markdown-body img, .markdown-body .markdown-alert,
.markdown-body .math-block, .markdown-body .mermaid-box {{ break-inside: avoid; }}
.markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4 {{ break-after: avoid; }}
.markdown-body pre code {{ white-space: pre-wrap; word-break: break-word; }}
</style></head>
<body><article class="markdown-body">{body}</article></body></html>
"""

SYSTEM_PROMPT = """You are a writing assistant embedded in a markdown editor. The markdown \
document the user is editing is supplied first, inside <document> tags. Each request \
about it comes in a <request> block and stands on its own. A request names part of the \
document, the <selection>, by quoting it with its line numbers and the text just before \
and after it (<context_before>, <context_after>), or says that the whole document is \
selected. If the document has changed since it was supplied, the request starts with a \
<document_update> holding a unified diff to the current version: work from that version.

Rules:
- Reply with the result ONLY: no preamble, no explanation, no closing remarks, \
no surrounding quotes, no code fence around the whole reply.
- In "replace" mode your reply replaces the selection verbatim, so it must be \
valid markdown that fits seamlessly where the selection was. Keep the existing \
markdown structure (headings, lists, links, emphasis, math, code) unless asked \
to change it. Do not include text from outside the selection.
- In "comment" mode reply with concise feedback in markdown; the document is not changed.
- British English spelling unless the document clearly uses another convention.
- If asked to use a skill, invoke it with the Skill tool before writing."""

# for any file that is not markdown; {lang} is e.g. "Python" or "plain text", {valid} "valid Python"
CODE_SYSTEM_PROMPT = """You are a coding and writing assistant embedded in a text editor. The file \
the user is editing (file type: {lang}) is supplied first, inside <document> tags. Each request about it comes \
in a <request> block and stands on its own. A request names part of the file, the <selection>, by \
quoting it with its line numbers and the text just before and after it (<context_before>, \
<context_after>), or says that the whole file is selected. If the file has changed since it was \
supplied, the request starts with a <document_update> holding a unified diff to the current \
version: work from that version.

Rules:
- Reply with the result ONLY: no preamble, no explanation, no closing remarks, no surrounding \
quotes, and no markdown code fence (```) around the reply.
- In "replace" mode your reply replaces the selection verbatim, so it must be {valid} that fits \
seamlessly where the selection was. Keep the file's indentation (tabs or spaces, and the \
selection's own leading indentation), formatting, naming and comment style, and keep the code \
working. Do not include text from outside the selection.
- In "comment" mode reply with concise feedback in markdown; the file is not changed.
- British English spelling in prose and comments unless the file clearly uses another convention.
- If asked to use a skill, invoke it with the Skill tool before writing."""


# ---------------------------------------------------------------- files

def safe_path(rel: str, follow=True) -> Path:
    """ROOT/rel, refused (ValueError) if it leaves ROOT, including by way of a symlink. With
    follow=False the last component is not resolved, so a symlink stays the link itself (for
    creating, renaming and deleting); the folder holding it must still be inside ROOT."""
    if not isinstance(rel, str) or "\0" in rel:
        raise ValueError("bad path")
    try:
        if follow:
            p = (ROOT / rel).resolve()
            if p != ROOT and ROOT not in p.parents:
                raise ValueError("path escapes the root folder")
            if os.path.islink(p):  # 3.13 hands back a link it could not resolve: a loop
                raise RuntimeError
            return p
        lex = Path(os.path.normpath(ROOT / rel))
        if lex == ROOT:
            return ROOT
        parent = lex.parent.resolve()
    except RuntimeError:  # Python 3.9-3.12 raise this for a symlink loop (3.13 resolves what it can)
        raise ValueError(f"symlink loop: {rel}")
    if parent != ROOT and ROOT not in parent.parents:
        raise ValueError("path escapes the root folder")
    return parent / lex.name


def version_of(p: Path) -> str:
    return str(p.stat().st_mtime_ns) if p.exists() else "0"


def list_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f.lower().endswith(MD_EXT):
                p = Path(dirpath) / f
                out.append({"path": p.relative_to(ROOT).as_posix(), "version": version_of(p)})
    return out


def browse(dirpath: str):
    """Folders and markdown files in `dirpath`, for the in-app file browser."""
    d = Path(dirpath).expanduser().resolve() if dirpath else ROOT
    if not d.is_dir():
        raise ValueError(f"not a folder: {d}")
    entries = []
    try:
        for p in d.iterdir():
            if p.name.startswith(".") or p.name in SKIP_DIRS:
                continue
            try:
                if p.is_dir():
                    entries.append({"name": p.name, "dir": True})
                elif p.suffix.lower() in MD_EXT:
                    entries.append({"name": p.name, "dir": False})
            except OSError:
                pass
    except PermissionError:
        raise ValueError(f"permission denied: {d}")
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"dir": str(d), "parent": str(d.parent) if d.parent != d else None,
            "home": str(Path.home()), "entries": entries}


def set_root(target: str):
    """Point the editor at a folder, or at a file's folder; returns the file to open."""
    global ROOT
    t = Path(target).expanduser().resolve()
    if t.is_file() and t.suffix.lower() in MD_EXT:
        ROOT = t.parent
        return t.name
    if t.is_dir():
        ROOT = t
        return None
    raise ValueError(f"not a markdown file or folder: {t}")


def list_skills():
    skills = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            md = d / "SKILL.md"
            if not md.is_file():
                continue
            desc = ""
            try:
                head = md.read_text(encoding="utf-8").split("---")
                for line in head[1].splitlines() if len(head) > 2 else []:
                    if line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip()
            except OSError:
                pass
            skills.append({"name": d.name, "description": desc})
    return skills


# ---------------------------------------------------------------- workspace
#
# The IDE side: the file tree, any text file, create / rename / delete, quick open and find in
# files. Paths come and go ROOT-relative (posix) and pass through safe_path. Inside a git work
# tree git decides what is ignored, and lists and searches the files; elsewhere a walk that
# skips hidden folders and SKIP_DIRS does.

TEXT_MAX = 5 << 20       # bytes: bigger files are "too_large", and the walk search skips them
SNIFF = 8192             # a NUL in the first 8 KiB makes a file binary (as for git)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico")
ALLFILES_MAX = 50_000
SEARCH_MAX = 2000        # matches
SEARCH_TIMEOUT = 60      # seconds for one search (git grep is killed; the walk gives up)
PREVIEW = 300            # characters of a matching line in a search result
LINE_MAX = 1 << 20       # bytes of a line that git grep found that a search looks at
STATUS_MAX = 5000        # changed files in one /api/git/status answer

LANGS = {
    ".md": "Markdown", ".markdown": "Markdown", ".mdx": "MDX", ".rst": "reStructuredText", ".txt": "plain text",
    ".py": "Python", ".pyi": "Python", ".pyw": "Python", ".ipynb": "JSON", ".js": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JSX", ".ts": "TypeScript", ".mts": "TypeScript",
    ".cts": "TypeScript", ".tsx": "TSX", ".json": "JSON", ".jsonc": "JSON", ".html": "HTML", ".htm": "HTML",
    ".xml": "XML", ".svg": "XML", ".css": "CSS", ".scss": "SCSS", ".sass": "Sass", ".less": "LESS",
    ".vue": "Vue", ".svelte": "Svelte", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI",
    ".cfg": "INI", ".properties": "Properties files", ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".fish": "fish", ".ps1": "PowerShell", ".bat": "Batch", ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++",
    ".cxx": "C++", ".hpp": "C++", ".hh": "C++", ".cs": "C#", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".groovy": "Groovy", ".gradle": "Groovy", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".php": "PHP", ".pl": "Perl", ".pm": "Perl", ".lua": "Lua", ".r": "R", ".jl": "Julia", ".swift": "Swift",
    ".m": "Objective-C", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang",
    ".hs": "Haskell", ".ml": "OCaml", ".clj": "Clojure", ".el": "Emacs Lisp", ".sql": "SQL", ".tex": "LaTeX",
    ".bib": "BibTeX", ".csv": "CSV", ".tsv": "TSV", ".diff": "diff", ".patch": "diff",
    ".proto": "Protocol Buffers", ".graphql": "GraphQL", ".tf": "HCL", ".nix": "Nix", ".cmake": "CMake",
    ".mk": "Makefile", ".dockerfile": "Dockerfile", ".j2": "Jinja2", ".jinja": "Jinja2", ".zig": "Zig",
    ".v": "Verilog", ".vhd": "VHDL", ".f90": "Fortran", ".asm": "Assembly", ".s": "Assembly",
}
LANG_FILES = {
    "Dockerfile": "Dockerfile", "Containerfile": "Dockerfile", "Makefile": "Makefile", "GNUmakefile": "Makefile",
    "CMakeLists.txt": "CMake", "Jenkinsfile": "Groovy", "Gemfile": "Ruby", "Rakefile": "Ruby",
    "Vagrantfile": "Ruby", ".bashrc": "Shell", ".bash_profile": "Shell", ".profile": "Shell", ".zshrc": "Shell",
    ".gitignore": "gitignore", ".dockerignore": "gitignore", ".gitattributes": "gitattributes",
    ".editorconfig": "INI", ".env": "dotenv",
}
SHEBANGS = {"python": "Python", "node": "JavaScript", "deno": "TypeScript", "bash": "Shell", "sh": "Shell",
            "zsh": "Shell", "dash": "Shell", "ksh": "Shell", "fish": "fish", "perl": "Perl", "ruby": "Ruby",
            "php": "PHP", "lua": "Lua", "Rscript": "R"}


class ApiError(Exception):
    """An answer of {"error": message} with this HTTP status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def lex_rel(rel):
    """rel as a ROOT-relative posix path, normalised without resolving symlinks ('' for ROOT)."""
    lex = Path(os.path.normpath(ROOT / rel))
    if lex == ROOT:
        return ""
    try:
        return lex.relative_to(ROOT).as_posix()
    except ValueError:
        raise ValueError("path escapes the root folder")


def no_git_dir(p):
    """Refuse to create, rename, delete or discard anything inside a .git folder."""
    if p != ROOT and ".git" in p.relative_to(ROOT).parts:
        raise ValueError("refusing to change a .git folder")


def decode_text(data):
    """data as text with \\n line ends, or None if it is binary (a NUL in the first 8 KiB, or not
    UTF-8). The line ends match what read_text gives; PUT /api/file puts CRLF back."""
    if b"\0" in data[:SNIFF]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def disk_text(p):
    """File p's text as GET /api/file gives it, "" if there is no file, or None if it is not text
    (binary, not UTF-8, over TEXT_MAX, or not a file)."""
    try:
        if not p.exists():  # as version_of: version "0"
            return ""
        if not p.is_file() or p.stat().st_size > TEXT_MAX:
            return None
        return decode_text(p.read_bytes())
    except FileNotFoundError:  # deleted just now
        return ""
    except OSError:
        return None


def uses_crlf(p):
    """Whether the file's first line ends in CRLF."""
    try:
        with open(p, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    i = head.find(b"\n")
    return i > 0 and head[i - 1:i] == b"\r"


def language_of(path, head=""):
    """The language of a file by its name (CodeMirror's names where it has one), else by a #! line
    at the start of `head`; None when unknown."""
    name = posixpath.basename(path or "")
    lang = LANG_FILES.get(name) or LANGS.get(os.path.splitext(name)[1].lower())
    if lang or not head.startswith("#!"):
        return lang
    words = head[2:].split("\n", 1)[0].split()
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-")]
    return SHEBANGS.get(re.sub(r"[\d.]+$", "", os.path.basename(words[0]))) if words else None


def is_markdown(path):
    return not path or path.lower().endswith(MD_EXT)


def file_info(q):
    """GET /api/file: {path, kind: text|binary|image|too_large, size, version, text (text only),
    mode (language hint, when known)}."""
    rel = q["path"]
    p = safe_path(rel)
    if not p.is_file():
        raise ApiError(404, "not found")
    st = p.stat()  # before reading, so a write in between shows up as a newer version later
    out = {"path": rel, "kind": "text", "size": st.st_size, "version": str(st.st_mtime_ns)}
    head = ""
    if p.suffix.lower() in IMAGE_EXT:
        out["kind"] = "image"
    elif st.st_size > TEXT_MAX:
        out["kind"] = "too_large"
    else:
        text = decode_text(p.read_bytes())
        if text is None:
            out["kind"] = "binary"
        else:
            out["text"], head = text, text[:200]
    lang = language_of(rel, head)
    if lang:
        out["mode"] = lang
    return out


def api_tree(q):
    """GET /api/tree: one folder's entries, folders first; hidden (dotfiles, SKIP_DIRS) and
    git-ignored ones only with all=1, flagged. .git never appears."""
    rel = q.get("dir", "")
    d = safe_path(rel)
    if not d.is_dir():
        raise ApiError(404, "not a folder")
    base, show_all = lex_rel(rel), q.get("all") == "1"
    entries = []
    with os.scandir(d) as it:
        for e in it:
            if e.name == ".git":
                continue
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            size = None
            if not is_dir:
                try:
                    size = e.stat().st_size
                except OSError:  # a dangling symlink
                    pass
            hidden = e.name.startswith(".") or (is_dir and e.name in SKIP_DIRS)
            if hidden and not show_all:
                continue
            entries.append({"name": e.name, "path": f"{base}/{e.name}" if base else e.name, "dir": is_dir,
                            "size": size, "hidden": hidden, "ignored": False})
    # git knows the files where they really are: a folder reached through a symlink is
    # asked about at its target
    real = d.relative_to(ROOT).as_posix() if d != ROOT else ""
    ignored = git_ignored([f"{real}/{e['name']}" if real else e["name"] for e in entries], real)
    for e in entries:
        e["ignored"] = (f"{real}/{e['name']}" if real else e["name"]) in ignored
    if not show_all:
        entries = [e for e in entries if not e["ignored"]]
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower(), e["name"]))
    return {"dir": base, "entries": entries}


def dir_stamp(rel, top=None, memo=None):
    """A short hash of a folder's direct entries (name, is folder), or "0" if it is gone: what
    its rows in the tree show, so saving a file in it does not make the page list it again. A
    .gitignore's mtime is in it too, since that file decides which entries are ignored, and in
    work tree `top` (ws_git()) so are the other files with ignore rules for it (ignore_rules)."""
    try:
        items = []
        d = safe_path(rel)
        with os.scandir(d) as it:
            for e in it:
                if e.name == ".git":
                    continue
                try:
                    items.append((e.name, e.is_dir(), e.stat(follow_symlinks=False).st_mtime_ns
                                  if e.name == ".gitignore" else 0))
                except OSError:
                    items.append((e.name, False, 0))
    except (ValueError, OSError):
        return "0"
    items.sort()
    rules = ignore_rules(d, top, {} if memo is None else memo) if top is not None else []
    return hashlib.sha1(repr((items, rules)).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def ignore_rules(d, top, memo):
    """The mtimes of the files, other than its own .gitignore, whose rules decide which of folder
    d's entries git ignores: the repository's info/exclude and the .gitignore in each folder
    above d up to the work tree's top. memo: path → mtime, shared by one poll's folders."""
    files = [git_exclude_file(top)]
    if top in d.parents:
        for p in d.parents:
            files.append(p / ".gitignore")
            if p == top:
                break
    out = []
    for f in files:
        if f is None:
            continue
        if f not in memo:
            try:
                memo[f] = os.stat(f).st_mtime_ns
            except OSError:
                memo[f] = 0
        out.append(memo[f])
    return out


def api_stat(req):
    """POST /api/stat: the versions of open files and stamps of expanded folders, for polling."""
    paths, dirs = req.get("paths") or [], req.get("dirs") or []
    if not isinstance(paths, list) or not isinstance(dirs, list):
        raise ValueError("paths and dirs must be lists")
    versions = {}
    for rel in paths:
        try:
            p = safe_path(rel)
            versions[rel] = version_of(p) if p.is_file() else "0"
        except (ValueError, OSError):
            versions[rel] = "0"
    top, memo = (ws_git() if dirs else None), {}
    return {"versions": versions, "dirs": {rel: dir_stamp(rel, top, memo) for rel in dirs}}


def api_new(req):
    """POST /api/new: a file (a name with no extension at all gets .md, unless exact is true) or,
    with dir true, a folder; 409 if it exists."""
    rel, is_dir = str(req.get("path") or "").strip(), bool(req.get("dir"))
    if not is_dir:
        rel = rel.rstrip("/")  # "notes/" is not a name: it would become notes/.md
    if not rel:
        raise ValueError("missing path")
    if not is_dir and not req.get("exact") and "." not in posixpath.basename(rel.rstrip("/")):
        rel += ".md"
    p = safe_path(rel, follow=False)
    no_git_dir(p)
    if p == ROOT or os.path.lexists(p):
        raise ApiError(409, "folder already exists" if p.is_dir() else "file already exists")
    out = {"path": lex_rel(rel)}
    if is_dir:
        p.mkdir(parents=True)
        return out
    text = req.get("text")
    if text is None:
        text = f"# {p.stem}\n\n" if is_markdown(p.name) else ""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, "x", encoding="utf-8", newline="") as f:  # never over a file made meanwhile
            f.write(text)
    except FileExistsError:
        raise ApiError(409, "file already exists")
    out["version"] = version_of(p)
    return out


def api_rename(req):
    """POST /api/rename: move a file or folder within ROOT; 409 if the target exists."""
    src, dst = safe_path(req["from"], follow=False), safe_path(req["to"], follow=False)
    if ROOT in (src, dst):
        raise ValueError("cannot rename the root folder")
    no_git_dir(src)
    no_git_dir(dst)
    if not os.path.lexists(src):
        raise ApiError(404, "not found")
    if os.path.lexists(dst):
        # only a change of case on a case-insensitive file system names the same file
        same = src.parent == dst.parent and src.name.lower() == dst.name.lower() and src.name != dst.name
        try:
            same = same and os.path.samefile(src, dst)
        except OSError:  # e.g. one of them is a dangling symlink
            same = False
        if not same:
            raise ApiError(409, "a file or folder of that name already exists")
    if src.is_dir() and not src.is_symlink() and src in dst.parents:
        raise ValueError("cannot move a folder into itself")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    return {"path": lex_rel(req["to"])}


def api_delete(req):
    """POST /api/delete: a file, symlink or folder (recursively, never ROOT). With dry_run true it
    only counts. Answers {ok, files, dirs}: the files and the folders inside a deleted folder
    (or that would be deleted)."""
    p = safe_path(req["path"], follow=False)
    if p == ROOT:
        raise ValueError("refusing to delete the root folder")
    no_git_dir(p)
    if not os.path.lexists(p):
        raise ApiError(404, "not found")
    if p.is_dir() and not p.is_symlink():
        files = dirs = 0
        for _, dnames, fnames in os.walk(p):
            dirs, files = dirs + len(dnames), files + len(fnames)
        if not req.get("dry_run"):
            shutil.rmtree(p)
        return {"ok": True, "files": files, "dirs": dirs}
    if not req.get("dry_run"):
        p.unlink()
    return {"ok": True, "files": 1, "dirs": 0}


def walk_files():
    """ROOT-relative paths of the files under ROOT in a sorted walk that skips hidden folders and
    SKIP_DIRS and does not follow symlinked folders."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        rel = Path(dirpath).relative_to(ROOT).as_posix()
        for f in sorted(filenames):
            yield f if rel == "." else f"{rel}/{f}"


def api_allfiles(q):
    """GET /api/allfiles: every non-ignored file under ROOT, for quick open."""
    files = None
    if ws_git() is not None:
        r = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."], ROOT)
        if r.returncode == 0:
            names = dict.fromkeys(os.fsdecode(n) for n in r.stdout.split(b"\0") if n)  # unmerged: once
            # drops tracked files deleted from the work tree, and submodules
            files = sorted(n for n in names if os.path.isfile(os.path.join(ROOT, n)))
    if files is None:
        files = sorted(itertools.islice(walk_files(), ALLFILES_MAX + 1))
    return {"files": files[:ALLFILES_MAX], "truncated": len(files) > ALLFILES_MAX}


# -- find in files

def split_globs(spec):
    """The include box: globs separated by commas outside {...}."""
    parts, depth, cur = [], 0, ""
    for ch in spec:
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        depth += (ch == "{") - (ch == "}")
        cur += ch
    return parts + [cur]


def expand_braces(g, limit=64):
    """{a,b} alternatives, innermost first; a loop rather than recursion, so a glob of a thousand
    {…} groups is just slow to refuse rather than a RecursionError."""
    out, todo = [], [g]
    while todo:
        s = todo.pop()
        m = re.search(r"\{([^{}]*)\}", s)
        if not m:
            out.append(s)
        else:
            todo += [s[:m.start()] + alt + s[m.end():] for alt in reversed(m.group(1).split(","))]
        if len(out) + len(todo) > limit:
            raise ValueError("too many {…} alternatives in the include globs")
    return out


def glob_parts(spec):
    """The include box as [(glob, exclude)]: globs separated by commas, `!glob` excludes, {a,b}
    alternatives, and a glob without a / matches at any depth (as in .gitignore)."""
    out = []
    for g in split_globs(spec or ""):
        g = g.strip()
        neg = g.startswith("!")
        g = g[1:].strip() if neg else g
        if g.startswith("./"):
            g = g[2:]
        anchored = "/" in g.rstrip("/")
        g = g.strip("/")
        if not g:
            continue
        if ".." in g.split("/"):
            raise ValueError("include globs cannot leave the folder")
        out += [(alt if anchored else "**/" + alt, neg) for alt in expand_braces(g)]
    return out


def glob_re(g):
    """A git-style glob (* and ? within one name, ** across folders, [...]) as a regex over
    ROOT-relative paths; it matches a folder's contents too, as git's pathspecs do."""
    i, n, out = 0, len(g), []
    while i < n:
        c = g[i]
        start = i == 0 or g[i - 1] == "/"
        if start and g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif start and g.startswith("**", i) and i + 2 == n:
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and g[j] in "!^":
                j += 1
            if j < n and g[j] == "]":
                j += 1
            j = g.find("]", j)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body, neg = g[i + 1:j], g[i + 1:i + 2] in ("!", "^")
                if neg:
                    body = body[1:]
                # every character literal (escaped: no nested sets, &&, ~~ or || for re to warn
                # about), except a - between two others, which makes a range
                cls = "".join("-" if c == "-" and 0 < k < len(body) - 1 and body[k - 1] != "-"
                              else c if c.isalnum() or ord(c) > 127 else "\\" + c
                              for k, c in enumerate(body))
                out.append("[" + ("^" if neg else "") + cls + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + "(?:/.*)?", re.S)


def git_pathspecs(globs):
    """git pathspecs for glob_parts() output, relative to ROOT (git's cwd)."""
    specs = []
    for g, neg in globs:
        magic = ":(exclude,glob)" if neg else ":(glob)"
        specs += [magic + g, magic + g + "/**"]
    if not any(not neg for _, neg in globs):
        specs.insert(0, ".")
    return specs


def search_pattern(q, regex, case, word):
    """The Python regex for a search (re.error if q is not a valid regex). Named groups written as
    in JavaScript and PCRE, (?<name>...) and \\k<name>, are translated."""
    if regex:
        pat = re.sub(r"\(\?<(?=[A-Za-z_])", "(?P<", q)
        pat = re.sub(r"\\k<(\w+)>", r"(?P=\1)", pat)
    else:
        pat = re.escape(q)
    if word:
        pat = r"(?<![0-9A-Za-z_])(?:" + pat + r")(?![0-9A-Za-z_])"
    return re.compile(pat, 0 if case else re.IGNORECASE)


def line_spans(pat, line, limit=SEARCH_MAX + 1):
    """Where pat matches in line: its non-empty matches (at most `limit`), else its first empty
    one, else None."""
    if pat is None:
        return None
    spans, empty = [], None
    for m in pat.finditer(line):
        if m.end() > m.start():
            spans.append(m.span())
            if len(spans) >= limit:
                break
        elif empty is None:
            empty = m.span()
    return spans or ([empty] if empty is not None else None)


def js_len(s):
    """Length in UTF-16 code units, which is how JavaScript and CodeMirror count characters."""
    return len(s) if s.isascii() else len(s.encode("utf-16-le")) // 2


def search_hit(path, n, line, s, e):
    """One result. col and len are in the line; a long line is cut to PREVIEW characters around
    the match, and `offset` says where `text` starts in the line."""
    off = 0 if len(line) <= PREVIEW or e <= PREVIEW - 40 else max(0, s - 100)
    return {"path": path, "line": n, "col": js_len(line[:s]), "len": js_len(line[s:e]),
            "text": line[off:off + PREVIEW], "offset": js_len(line[:off])}


def grep_records(stream, line_max=LINE_MAX):
    """(path, line number, column, text) byte strings from `git grep -n --column -z` output, in
    one pass over it (a minified file's single line can be megabytes); text past line_max bytes
    of a line is dropped."""
    head, text, nuls = bytearray(), bytearray(), 0
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            return
        i, n = 0, len(chunk)
        while i < n:
            if nuls < 3:  # path\0line\0column\0
                j = chunk.find(b"\0", i)
                if j < 0:
                    head += chunk[i:]
                    break
                head += chunk[i:j + 1]
                nuls += 1
                i = j + 1
                continue
            j = chunk.find(b"\n", i)
            end = n if j < 0 else j
            if len(text) < line_max:
                text += chunk[i:min(end, i + line_max - len(text))]
            if j < 0:
                break
            path, line, col, _ = bytes(head).split(b"\0")
            yield path, line, col, bytes(text)
            head.clear()
            text.clear()
            nuls = 0
            i = j + 1


class Watchdog(threading.Thread):
    """Calls kill() once the deadline passes (why = "timeout") or the client of `conn`, a request's
    socket, has gone away (why = "gone": a newer search replaced it)."""

    def __init__(self, kill, deadline, conn=None):
        super().__init__(daemon=True)
        self.kill, self.deadline, self.conn = kill, deadline, conn
        self.why = None
        self.done = threading.Event()

    def run(self):
        while not self.done.wait(0.2):
            if time.monotonic() > self.deadline:
                self.why = "timeout"
            elif self.conn is not None and client_gone(self.conn):
                self.why = "gone"
            else:
                continue
            try:
                self.kill()
            except OSError:
                pass
            return

    def stop(self):
        self.done.set()

    def check(self):
        """Raise the ApiError for why the search was stopped, if it was."""
        if self.why == "timeout":
            raise ApiError(504, "the search took too long")
        if self.why == "gone":
            raise ApiError(499, "the search was cancelled")


def client_gone(conn):
    """Whether the other end of this socket has closed it (an aborted fetch)."""
    try:
        if not select.select([conn], [], [], 0)[0]:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except BlockingIOError:
        return False
    except (OSError, ValueError):
        return True


def grep_run(args, deadline, skip_big):
    """Records of one `git grep` run (killed at the deadline or when the client goes away)."""
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(["git"] + GIT_OPTS + args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=err, env=git_env())
    dog = Watchdog(proc.kill, deadline, getattr(REQUEST, "conn", None))
    dog.start()
    try:
        last, skip = None, False
        for rec in grep_records(proc.stdout):
            if rec[0] != last:  # over 5 MB: the editor would not open it anyway, as the walk skips it
                last = rec[0]
                try:
                    skip = skip_big and os.stat(os.path.join(ROOT, os.fsdecode(last))).st_size > TEXT_MAX
                except OSError:
                    skip = False
            if not skip:
                yield rec
        rc = proc.wait()
        dog.check()
        if rc not in (0, 1):
            err.seek(0)
            msg = err.read().decode("utf-8", "replace").strip()
            raise GrepError(re.sub(r"^(fatal|error): ", "", msg.splitlines()[-1] if msg else f"git grep failed ({rc})"))
    finally:
        dog.stop()
        dog.join()  # before the process is reaped and its pid can be reused
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        err.close()


class GrepError(Exception):
    """git grep refused the search (as git words it)."""


def grep_git(query, regex, case, word, globs, pat, deadline, pcre=None):
    """Search with git grep: tracked files (all of them: git grep --untracked would also leave out
    tracked files that match an ignore rule), then untracked files that are not ignored; never
    binary files or ones over 5 MB. Match positions come from `pat`, or from git's column when
    Python's regex flavour disagrees."""
    args = ["grep", "-n", "-I", "--column", "-z", "--no-color"]
    if not case:
        args.append("-i")
    if word:
        args.append("-w")
    if regex and pcre is None:
        pcre = git_pcre()
    args.append(("-P" if pcre else "-E") if regex else "-F")
    specs = git_pathspecs(globs)
    runs = [args + ["-e", query, "--"] + specs]
    r = git(["ls-files", "-z", "--others", "--exclude-standard", "--"] + specs, ROOT)
    untracked = [n for n in r.stdout.split(b"\0") if n and not n.endswith(b"/")] if r.returncode == 0 else []
    for k in range(0, len(untracked), 500):  # literal pathspecs, a command line at a time
        runs.append(args + ["--untracked", "-e", query, "--"]
                    + [":(literal)" + os.fsdecode(n) for n in untracked[k:k + 500]])
    try:
        for run in runs:
            for path_b, line_b, col_b, text_b in grep_run(run, deadline, True):
                path = os.fsdecode(path_b)
                if text_b.endswith(b"\r"):
                    text_b = text_b[:-1]
                line = text_b.decode("utf-8", "replace")
                spans = line_spans(pat, line)
                if spans is None:
                    c = len(text_b[:max(0, int(col_b) - 1)].decode("utf-8", "replace"))
                    spans = [(c, c)]
                for s, e in spans:
                    yield search_hit(path, int(line_b), line, s, e)
    except GrepError as exc:
        raise ApiError(400, f"invalid regular expression: {exc}") if regex else ApiError(500, str(exc))


def grep_walk(pat, globs, regex, deadline):
    """Search by walking ROOT (no hidden folders or SKIP_DIRS, no symlinks, binary or big files).
    A plain-text search first tries each whole file; a regex ($, \\A, lookarounds) might match a
    whole file differently from its lines, so it goes line by line."""
    inc = [glob_re(g) for g, neg in globs if not neg]
    exc = [glob_re(g) for g, neg in globs if neg]
    # in md-editor's own process (a plain-text search) it stops when its client goes away, as
    # search_child's Watchdog stops a regex search
    conn, look = getattr(REQUEST, "conn", None), 0.0
    for rel in walk_files():
        now = time.monotonic()
        if now > deadline:
            raise ApiError(504, "the search took too long")
        if conn is not None and now >= look:
            if client_gone(conn):
                raise ApiError(499, "the search was cancelled")
            look = now + 0.2
        if (inc and not any(r.fullmatch(rel) for r in inc)) or any(r.fullmatch(rel) for r in exc):
            continue
        p = ROOT / rel
        try:
            st = p.lstat()  # regular files only: no symlinks, and a FIFO would block the read
            if not stat.S_ISREG(st.st_mode) or st.st_size > TEXT_MAX:
                continue
            text = decode_text(p.read_bytes())
        except OSError:
            continue
        if text is None or (not regex and not pat.search(text)):
            continue
        for n, line in enumerate(text.split("\n"), 1):
            for s, e in line_spans(pat, line) or ():
                yield search_hit(rel, n, line, s, e)


def search_gen(job, deadline):
    """The hits of a search job ({engine, q, regex, case, word, globs, pcre?}), in this process."""
    try:
        pat = search_pattern(job["q"], job["regex"], job["case"], job["word"])
    except (re.error, OverflowError, RecursionError):
        pat = None  # api_search has already refused this for the walk; git's PCRE may still take it
    if job["engine"] == "git":
        return grep_git(job["q"], job["regex"], job["case"], job["word"], job["globs"], pat, deadline,
                        job.get("pcre"))
    return grep_walk(pat, job["globs"], job["regex"], deadline)


# Python's re has no time limit, and a regex that backtracks (`(a+)+$`) can run for hours while
# holding the GIL, which would stop every other request: so a regular-expression search runs in
# a child process, killed at the time limit or as soon as its client goes away.
SEARCHES = set()           # running search processes, ended when md-editor exits
SEARCH_LOCK = threading.Lock()
SEARCH_CHILD = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from md_collab_editor.server import search_child_main; search_child_main()")


def kill_search(proc):
    if proc.returncode is not None:  # already reaped: its pid could now name another process
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)  # with its git grep
        else:
            proc.kill()
    except OSError:
        pass


def search_child(job, deadline):
    """The hits of a search job, found by a child process (`python -I`: nothing in the folder
    being searched can be imported by it)."""
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen([sys.executable, "-I", "-c", SEARCH_CHILD, str(HERE.parent)], cwd=str(HERE),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                            start_new_session=os.name == "posix")
    with SEARCH_LOCK:
        SEARCHES.add(proc)
    dog = Watchdog(lambda: kill_search(proc), deadline, getattr(REQUEST, "conn", None))
    dog.start()
    try:
        job = dict(job, root=str(ROOT), timeout=max(1.0, deadline - time.monotonic()))
        try:
            proc.stdin.write(json.dumps(job).encode())
            proc.stdin.close()
        except OSError:
            pass
        for line in proc.stdout:
            msg = json.loads(line)
            if "error" in msg:
                raise ApiError(msg.get("status") or 500, msg["error"])
            if msg.get("end"):
                return
            yield msg
        proc.wait()
        dog.check()
        err.seek(0)
        tail = err.read().decode("utf-8", "replace").strip().splitlines()
        raise ApiError(500, "the search failed" + (f": {tail[-1]}" if tail else ""))
    finally:
        dog.stop()
        dog.join()  # before the process is reaped and its pid can be reused
        kill_search(proc)
        proc.wait()
        proc.stdout.close()
        err.close()
        with SEARCH_LOCK:
            SEARCHES.discard(proc)


def search_child_main():
    """The child process of search_child: a job as JSON on stdin; one JSON hit per line on
    stdout, then {"end": true} or {"error", "status"}."""
    global ROOT
    job = json.loads(sys.stdin.buffer.read())
    ROOT = Path(job["root"])
    if hasattr(signal, "alarm"):  # in case md-editor is gone: SIGALRM's default action ends it
        signal.alarm(int(job["timeout"]) + 10)
    out = sys.stdout.buffer
    try:
        for hit in search_gen(job, time.monotonic() + job["timeout"]):
            out.write(json.dumps(hit).encode() + b"\n")
        out.write(b'{"end": true}\n')
    except ApiError as exc:
        out.write(json.dumps({"error": str(exc), "status": exc.status}).encode() + b"\n")
    except BrokenPipeError:
        return
    out.flush()


def end_searches():
    """At exit: kill the searches still running."""
    with SEARCH_LOCK:
        procs = list(SEARCHES)
    for proc in procs:
        kill_search(proc)


def api_search(q):
    """GET /api/search: find in files, git grep in a work tree, else a walk with Python's re."""
    query = q.get("q", "")
    regex, case, word = (q.get(k) == "1" for k in ("regex", "case", "word"))
    if "\n" in query or "\r" in query:
        raise ValueError("search for one line at a time")
    globs = glob_parts(q.get("glob", ""))
    engine = "git" if ws_git() is not None else "walk"
    if not query:
        return {"results": [], "truncated": False, "engine": engine}
    if engine == "walk":
        try:
            for g, _ in globs:
                glob_re(g)
        except re.error as exc:
            raise ApiError(400, f"invalid include glob: {exc}")
    try:
        search_pattern(query, regex, case, word)
    except (re.error, OverflowError, RecursionError) as exc:
        if engine == "walk":
            raise ApiError(400, f"invalid regular expression: {exc}")
        # git's PCRE may still take it
    job = {"engine": engine, "q": query, "regex": regex, "case": case, "word": word, "globs": globs}
    if engine == "git" and regex:
        job["pcre"] = git_pcre()
    deadline = time.monotonic() + env_seconds("MDEDIT_SEARCH_TIMEOUT", SEARCH_TIMEOUT)
    # a plain-text pattern can't backtrack (re.escape, at most with word-boundary checks)
    hits = search_child(job, deadline) if regex else search_gen(job, deadline)
    results, truncated = [], False
    try:
        for h in hits:
            if len(results) == SEARCH_MAX:
                truncated = True
                break
            results.append(h)
    finally:
        hits.close()  # stops git grep, or the child process
    return {"results": results, "truncated": truncated, "engine": engine}


# ---------------------------------------------------------------- git
#
# Read-only apart from /api/git/discard. git never gets an option from the user: paths come
# after `--` (literal pathspecs where they name files), the search pattern after -e, and the
# only revisions are HEAD and refs and SHAs that git itself gave us.

# Repository settings that would change the output parsed here, and ones that name commands for
# git to run: md-editor runs git on this machine, outside the sandbox that the terminal's Claude
# runs in, and that sandbox can write the repository's .git/config and .git/hooks. So no
# fsmonitor hook (git status, ls-files and check-ignore would run it), no hooks (git restore runs
# post-checkout), no submodules; clean/smudge filters: see frozen_git_dir.
GIT_OPTS = ["-c", "color.ui=false", "-c", "core.quotePath=false", "-c", "diff.relative=false",
            "-c", "grep.fullName=false", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
            "-c", "submodule.recurse=false"]
# status/diff run clean (and textconv) drivers, restore/checkout/rm run smudge drivers: a command
# the agent names in .git/config for a path .gitattributes marks. These run against a frozen,
# agent-unwritable copy of the repository's config (frozen_git_dir).
FILTER_CMDS = ("status", "diff", "restore", "checkout", "rm")
# Git LFS's own filter commands (as `git lfs install` writes them) are kept in the frozen config,
# unless the configuration also names commands for git-lfs itself to run: LFS extensions (run on
# every clean and smudge) and custom transfer agents. git-lfs never downloads either
# (GIT_LFS_SKIP_SMUDGE), so no transfer agent, credential helper or ssh command runs for a smudge.
LFS_FILTER = {"clean": ("git-lfs clean -- %f",), "smudge": ("git-lfs smudge -- %f", "git-lfs smudge --skip -- %f"),
              "process": ("git-lfs filter-process", "git-lfs filter-process --skip")}
LFS_COMMAND_KEYS = ("lfs.extension.", "lfs.customtransfer.", "lfs.standalonetransferagent")
# core.* keys that name a command git could run (status runs fsmonitor, restore the hooks): never
# copied into the frozen config, and overridden by GIT_OPTS for good measure.
FROZEN_CORE_SKIP = {"fsmonitor", "fsmonitorhookversion", "hookspath", "sshcommand", "pager", "editor",
                    "askpass", "alternaterefscommand", "gvfs", "commitgraph"}
GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_LITERAL_PATHSPECS",
                "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
                "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_ATTR_SOURCE")
GIT_TTL = 3              # seconds that ROOT's work tree is remembered (git init / rm -rf .git)
GIT_REPOS = {}           # ROOT → (expiry, git_repo() answer)
GIT_FEATURES = {}        # "pcre", "restore" → bool, found out once
GIT_EXCLUDE = {}         # work tree top → its info/exclude file (git_exclude_file)
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def git_env(literal=False):
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    # no index.lock for status while the terminal's Claude runs git; never ask for a password;
    # git-lfs smudges from its local store only (see LFS_FILTER)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    if literal:
        env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def git_plain(args, cwd, env=None, timeout=60):
    """One git subprocess; stdin closed. Used to read the real repo while building the frozen dir."""
    return subprocess.run(["git"] + GIT_OPTS + list(args), cwd=str(cwd), capture_output=True,
                          stdin=subprocess.DEVNULL, env=env if env is not None else git_env(), timeout=timeout)


def _abs_git_path(cwd, which):
    """Absolute path of one of the repo's files (objects, index), by its git-path relative to cwd."""
    r = git_plain(["rev-parse", "--git-path", which], cwd)
    if r.returncode != 0:
        return None
    out = os.fsdecode(r.stdout.rstrip(b"\n"))
    return out if os.path.isabs(out) else os.path.abspath(os.path.join(str(cwd), out))


def write_frozen_config(cwd, path):
    """Write `path`: a git config with only the repo's effective core/extensions/index settings
    (minus any that name a command for git to run) and Git LFS's exact filter commands, read once
    from the real repo. Every other clean/smudge/diff/textconv driver, include, alias and remote is
    left out, so a FILTER_CMD run against this config cannot execute a command the agent slipped
    into .git/config. Reading the config runs nothing, and git later reads only this frozen copy."""
    r = git_plain(["config", "--no-includes", "-z", "--get-regexp", r"^(core|extensions|index|filter|lfs)\."], cwd)
    keep, lfs_vals, lfs_ok = [], {}, True
    for item in r.stdout.split(b"\0") if r.returncode == 0 else ():
        key, _, value = os.fsdecode(item).partition("\n")
        low = key.lower()
        if low.startswith(LFS_COMMAND_KEYS):  # git-lfs would run these itself: do not trust LFS
            lfs_ok = False
            continue
        section, _, var = low.rpartition(".")
        if section in ("core", "extensions", "index"):
            if section == "core" and var in FROZEN_CORE_SKIP:
                continue
            keep.append((key, value))
        elif section == "filter.lfs" and var in ("clean", "smudge", "process", "required"):
            lfs_vals[var] = value.strip()
    # keep Git LFS only when every filter command it names is exactly one `git lfs install` writes
    if lfs_ok and any(v in lfs_vals for v in ("clean", "smudge", "process")) and \
            all(lfs_vals.get(v, next(iter(LFS_FILTER[v]))) in LFS_FILTER[v] for v in ("clean", "smudge", "process")):
        for v in ("clean", "smudge", "process"):
            keep.append((f"filter.lfs.{v}", next(iter(LFS_FILTER[v]))))
        keep.append(("filter.lfs.required", lfs_vals.get("required", "true")))
    open(path, "x").close()  # git config --file writes correct escaping; we only add known keys
    for key, value in keep:
        subprocess.run(["git", "config", "--file", str(path), "--add", key, value],
                       capture_output=True, stdin=subprocess.DEVNULL, env=git_env(), timeout=30)


def frozen_git_dir(cwd):
    """A private, agent-unwritable git directory for running a FILTER_CMD in the work tree at cwd:
    its config is a frozen copy with no agent-supplied drivers (write_frozen_config), its HEAD is
    pinned to the current commit, and its objects and index are the real repo's (shared, so a
    discard still writes the real index). Returns (git_dir, work_tree, index_file) or None when cwd
    is not inside a work tree (nothing there runs a filter). Caller removes git_dir."""
    if git_plain(["rev-parse", "--is-inside-work-tree"], cwd).stdout.strip() != b"true":
        return None
    top = git_plain(["rev-parse", "--show-toplevel"], cwd)
    objects = _abs_git_path(cwd, "objects")
    index = _abs_git_path(cwd, "index")
    if top.returncode != 0 or objects is None or index is None:
        return None
    work_tree = os.fsdecode(top.stdout.rstrip(b"\n"))
    head = git_plain(["rev-parse", "-q", "--verify", "HEAD"], cwd)
    sha = head.stdout.decode().strip() if head.returncode == 0 else ""
    git_dir = tempfile.mkdtemp(prefix="mdedit-git-")
    try:
        os.mkdir(os.path.join(git_dir, "refs"))
        os.makedirs(os.path.join(git_dir, "objects", "info"))
        with open(os.path.join(git_dir, "objects", "info", "alternates"), "w") as f:
            f.write(objects + "\n")
        # a resolved sha (so no ref lookup is needed), or an unborn branch so status reports every
        # file as added, exactly as the real repo would before its first commit
        with open(os.path.join(git_dir, "HEAD"), "w") as f:
            f.write((sha if SHA_RE.fullmatch(sha) else "ref: refs/heads/_mdedit_unborn") + "\n")
        write_frozen_config(cwd, os.path.join(git_dir, "config"))
    except BaseException:  # never leave the private dir behind, and never fall back to unfrozen git
        shutil.rmtree(git_dir, ignore_errors=True)
        raise
    return git_dir, work_tree, index


def git(args, cwd, input=None, literal=False, timeout=60):
    """Run git with bytes in and out; a failing git is the caller's to judge from returncode.
    Raises OSError if git cannot run at all. A FILTER_CMD (which runs clean/smudge/diff drivers)
    is run against a frozen copy of the repository's config that the sandboxed agent cannot change
    under it, so a driver it writes into .git/config never executes on the host."""
    kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    if args and args[0] in FILTER_CMDS:
        frozen = frozen_git_dir(cwd)
        if frozen is not None:
            git_dir, work_tree, index = frozen
            env = git_env(literal)
            env["GIT_INDEX_FILE"] = index
            pre = ["--git-dir=" + git_dir, "--work-tree=" + work_tree]
            try:
                return subprocess.run(["git"] + pre + GIT_OPTS + list(args), cwd=str(cwd),
                                      capture_output=True, env=env, timeout=timeout, **kw)
            finally:
                shutil.rmtree(git_dir, ignore_errors=True)
    return subprocess.run(["git"] + GIT_OPTS + list(args), cwd=str(cwd), capture_output=True,
                          env=git_env(literal), timeout=timeout, **kw)


def git_repo():
    """{"top": the work tree's top (resolved) or None, "root_rel": ROOT relative to it, "ignored":
    whether ROOT itself is git-ignored, "reason": why git cannot be used, if not just "no repo"},
    remembered per ROOT for GIT_TTL seconds."""
    root, now = ROOT, time.monotonic()
    hit = GIT_REPOS.get(root)
    if hit and hit[0] > now:
        return hit[1]
    repo = {"top": None, "root_rel": None, "ignored": False, "reason": ""}
    try:
        r = git(["rev-parse", "--show-toplevel"], root) if root.is_dir() else None
        if r is None:
            pass
        elif r.returncode == 0:
            top = Path(os.fsdecode(r.stdout.rstrip(b"\n"))).resolve()
            if top == root or top in root.parents:
                rr = "" if top == root else root.relative_to(top).as_posix()
                repo.update(top=top, root_rel=rr)
                if rr:
                    repo["ignored"] = git(["check-ignore", "-q", "--", "./" + rr], top).returncode == 0
        else:
            err = r.stderr.decode("utf-8", "replace").strip()
            if err and "not a git repository" not in err:
                repo["reason"] = err.splitlines()[-1][:300]
    except (OSError, subprocess.SubprocessError) as exc:
        repo["reason"] = f"git is not available: {exc}"
    GIT_REPOS[root] = (now + GIT_TTL, repo)
    return repo


def git_exclude_file(top):
    """The info/exclude file of the repository whose work tree is `top` (a linked worktree's is
    in the main one's .git), found out once per work tree; None if git cannot say."""
    if top not in GIT_EXCLUDE:
        try:
            r = git(["rev-parse", "--git-path", "info/exclude"], top)
            out = os.fsdecode(r.stdout.rstrip(b"\n")) if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out:
            GIT_EXCLUDE[top] = top / out
    return GIT_EXCLUDE.get(top)


def ws_git():
    """The work tree's top when git should decide what the tree, quick open and search show: not
    when ROOT is outside any repo, or itself git-ignored (everything in it would be)."""
    repo = git_repo()
    return repo["top"] if repo["top"] is not None and not repo["ignored"] else None


def check_ignore(paths, index):
    """The paths among these that check-ignore reports; None if it fails."""
    # check-ignore takes no literal pathspecs; after ./ a leading : is not pathspec magic
    r = git(["check-ignore", "-z", "--stdin"] + ([] if index else ["--no-index"]), ROOT,
            input=b"".join(b"./" + os.fsencode(p) + b"\0" for p in paths))
    if r.returncode not in (0, 1):  # e.g. a path inside a nested repository
        return None
    return {os.fsdecode(p[2:] if p.startswith(b"./") else p) for p in r.stdout.split(b"\0") if p}


def git_ignored(paths, folder=""):
    """Which of these ROOT-relative paths (the entries of `folder`, a real path: no symlinks on
    the way, which git refuses) git ignores. A tracked file is never ignored, nor is a folder
    with tracked files in it. With the index, check-ignore takes time in proportion to the
    number of paths times the size of the index, so it first asks without the index (quick),
    then sorts out the few it names with the index, or with the tracked files under `folder`."""
    if not paths or ws_git() is None:
        return set()
    try:
        cand = check_ignore(paths, False)
        if not cand:
            return set()
        if len(cand) <= 64:
            return check_ignore(sorted(cand), True) or set()
        r = git(["ls-files", "-z", "--cached", "--", folder or "."], ROOT, literal=True)
        if r.returncode != 0:
            return set()
    except (OSError, subprocess.SubprocessError):
        return set()
    tracked = set()
    for f in r.stdout.split(b"\0"):  # each tracked file, and the folders it is in
        f = os.fsdecode(f)
        while f and f not in tracked:
            tracked.add(f)
            f = posixpath.dirname(f)
    return cand - tracked


def git_pcre():
    """Whether this git's grep has -P (PCRE, close to JavaScript's regexes); else -E is used."""
    if "pcre" not in GIT_FEATURES:
        with tempfile.TemporaryDirectory() as d:
            try:
                rc = git(["grep", "--no-index", "-q", "-P", "-e", "x", "--", "."], d).returncode
            except (OSError, subprocess.SubprocessError):
                rc = 128
        GIT_FEATURES["pcre"] = rc in (0, 1)
    return GIT_FEATURES["pcre"]


def git_has_restore():
    """git restore arrived in git 2.23."""
    if "restore" not in GIT_FEATURES:
        try:
            m = re.search(rb"(\d+)\.(\d+)", git(["version"], tempfile.gettempdir()).stdout)
        except (OSError, subprocess.SubprocessError):
            m = None
        GIT_FEATURES["restore"] = bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 23)
    return GIT_FEATURES["restore"]


def git_commit(top, rev):
    """The SHA of commit `rev` (HEAD or a full ref name), or None."""
    r = git(["rev-parse", "-q", "--verify", rev + "^{commit}"], top)
    sha = r.stdout.decode().strip() if r.returncode == 0 else ""
    return sha if SHA_RE.fullmatch(sha) else None


def default_branch(top):
    """{"name", "ref", "branch"} of the default branch: what origin/HEAD points at, else main,
    else master (local, then origin's); None if there is none."""
    cands = []
    r = git(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], top)
    ref = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    if ref.startswith("refs/remotes/"):
        cands.append(ref)
    cands += ["refs/heads/main", "refs/heads/master", "refs/remotes/origin/main", "refs/remotes/origin/master"]
    r = git(["for-each-ref", "--format=%(refname)"] + cands, top)
    have = set(r.stdout.decode("utf-8", "replace").splitlines()) if r.returncode == 0 else set()
    for ref in cands:
        if ref in have:
            if ref.startswith("refs/heads/"):
                name = branch = ref[len("refs/heads/"):]
            else:
                name = ref[len("refs/remotes/"):]
                branch = name.split("/", 1)[1]
            return {"name": name, "ref": ref, "branch": branch}
    return None


def git_head(top):
    """(branch name or None when detached, HEAD's SHA or None before the first commit)."""
    r = git(["symbolic-ref", "-q", "HEAD"], top)
    ref = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    return (ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None), git_commit(top, "HEAD")


def need_repo():
    repo = git_repo()
    if repo["top"] is None:
        raise ApiError(400, "not a git repository" + (f" ({repo['reason']})" if repo["reason"] else ""))
    return repo


def api_git_info(q):
    """GET /api/git/info."""
    repo = git_repo()
    if repo["top"] is None:
        return {"repo": False, "reason": repo["reason"]} if repo["reason"] else {"repo": False}
    top = repo["top"]
    branch, head = git_head(top)
    dflt = default_branch(top)
    return {"repo": True, "top": str(top), "root_rel": repo["root_rel"], "branch": branch, "head": head,
            "default_branch": dflt["name"] if dflt else None,
            "is_default": bool(dflt and branch is not None and branch == dflt["branch"])}


def git_base(mode):
    """(base, repo) for a base mode: "head" is HEAD; "branch" is HEAD's merge-base with the
    default branch, falling back to HEAD with a `note` when that makes no sense."""
    if mode not in ("head", "branch"):
        raise ValueError("base must be head or branch")
    repo = need_repo()
    top = repo["top"]
    branch, head = git_head(top)
    note = None
    if mode == "branch":
        dflt = default_branch(top)
        if dflt is None:
            note = "There is no default branch (origin/HEAD, main or master), so this shows uncommitted changes."
        elif head is None:
            note = "There are no commits yet, so this shows uncommitted changes."
        elif branch is not None and branch == dflt["branch"]:
            note = f"{branch} is the default branch, so this shows uncommitted changes."
        else:
            r = git(["merge-base", "HEAD", dflt["ref"]], top)
            sha = r.stdout.decode().strip() if r.returncode == 0 else ""
            if SHA_RE.fullmatch(sha):
                return {"mode": "branch", "ref": dflt["ref"], "sha": sha, "label": dflt["name"]}, repo
            note = f"HEAD has no history in common with {dflt['name']}, so this shows uncommitted changes."
    base = {"mode": "head", "ref": "HEAD", "sha": head, "label": "HEAD"}
    if note:
        base["note"] = note
    return base, repo


def parse_status_v2(out):
    """[(status, path, old path or None)] from `git status --porcelain=v2 -z` (top-relative):
    the work tree against HEAD, staged and unstaged changes together."""
    toks, i, entries = out.split(b"\0"), 0, []
    while i < len(toks):
        t = toks[i]
        i += 1
        kind = t[:1]
        if kind == b"1":
            parts = t.split(b" ", 8)
            xy, path = parts[1].decode(), os.fsdecode(parts[8])
            if "A" in xy:
                if "D" not in xy:  # added, then deleted again: nothing against HEAD
                    entries.append(("A", path, None))
            else:
                entries.append(("D" if "D" in xy else "M", path, None))
        elif kind == b"2":
            parts = t.split(b" ", 9)
            xy, path, old = parts[1].decode(), os.fsdecode(parts[9]), os.fsdecode(toks[i])
            i += 1
            if xy[1:] == "D":    # renamed, then deleted: the old file is gone
                entries.append(("D", old, None))
            elif "R" in xy:
                entries.append(("R", path, old))
            else:                # a copy
                entries.append(("A", path, None))
        elif kind == b"u":
            entries.append(("C", os.fsdecode(t.split(b" ", 10)[10]), None))
        elif kind == b"?":
            entries.append(("U", os.fsdecode(t[2:]), None))
    return entries


def parse_name_status(out):
    """[(status, path, old path or None)] from `git diff --name-status -z` (top-relative)."""
    toks, i, entries = out.split(b"\0"), 0, []
    while i < len(toks):
        s = toks[i].decode("ascii", "replace")
        i += 1
        if not s:
            continue
        if s[0] in "RC":
            old, new = os.fsdecode(toks[i]), os.fsdecode(toks[i + 1])
            i += 2
            entries.append(("R", new, old) if s[0] == "R" else ("A", new, None))
        else:
            entries.append(({"A": "A", "D": "D", "U": "C"}.get(s[0], "M"), os.fsdecode(toks[i]), None))
            i += 1
    return entries


def api_git_status(q):
    """GET /api/git/status: changed files under ROOT against the base, and how many are outside."""
    if git_repo()["top"] is None:
        return {"repo": False, "base": None, "files": [], "outside": 0}
    base, repo = git_base(q.get("base") or "head")
    top, rr = repo["top"], repo["root_rel"]
    if base["mode"] == "head":
        r = git(["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=all"], top)
        if r.returncode != 0:
            raise ApiError(500, r.stderr.decode("utf-8", "replace").strip()[-500:] or "git status failed")
        entries = parse_status_v2(r.stdout)
    else:
        r = git(["diff", "--name-status", "-z", "-M", "--no-ext-diff", "--ignore-submodules=all", base["sha"], "--"],
                top)
        u = git(["ls-files", "-z", "--others", "--exclude-standard"], top)
        if r.returncode != 0 or u.returncode != 0:
            raise ApiError(500, (r.stderr + u.stderr).decode("utf-8", "replace").strip()[-500:] or "git diff failed")
        entries = parse_name_status(r.stdout) + [("U", os.fsdecode(p), None) for p in u.stdout.split(b"\0") if p]

    def inside(p):  # top-relative → ROOT-relative, or None when outside ROOT
        if not rr:
            return p
        return p[len(rr) + 1:] if p.startswith(rr + "/") else None

    files, outside, nested = {}, 0, 0

    def add(path, status, old=None):
        prev = files.get(path)
        if prev is not None:  # deleted from the index but back in the work tree as untracked
            if {prev["status"], status} == {"D", "U"}:
                prev["status"] = "M"
            return
        files[path] = {"path": path, "status": status}
        if old is not None:
            files[path]["old_path"] = old

    for status, p, old in entries:
        if p.endswith("/"):  # an untracked folder: a repository of its own (or a worktree) inside this one
            if inside(p.rstrip("/")) is not None:
                nested += 1
            continue
        rp = inside(p)
        if status == "R":
            ro = inside(old)
            if rp is not None and ro is not None:
                add(rp, "R", ro)
            else:  # moved across ROOT's edge: what is inside is new, or gone
                outside += 1
                if rp is not None:
                    add(rp, "A")
                elif ro is not None:
                    add(ro, "D")
        elif rp is None:
            outside += 1
        else:
            add(rp, status)
    listed = sorted(files.values(), key=lambda f: f["path"])
    out = {"repo": True, "base": base, "files": listed[:STATUS_MAX], "outside": outside, "nested": nested}
    if len(listed) > STATUS_MAX:
        out["truncated"] = True
    return out


def top_rel(rel, repo):
    """A ROOT-relative path (inside ROOT) as a path from the top of the work tree."""
    safe_path(rel)
    lr = lex_rel(rel)
    if not lr:
        raise ValueError("expected a file path")
    return f"{repo['root_rel']}/{lr}" if repo["root_rel"] else lr


def api_git_show(q):
    """GET /api/git/show: a file's content in the base commit (old_path for a rename)."""
    base, repo = git_base(q.get("base") or "head")
    path = top_rel(q.get("old_path") or q["path"], repo)
    if not base["sha"]:
        return {"exists": False, "binary": False}
    spec = f"{base['sha']}:{path}"
    r = git(["cat-file", "-s", spec], repo["top"])  # the size first: no need to read a big one
    if r.returncode != 0:
        return {"exists": False, "binary": False}
    if int(r.stdout.strip() or 0) > TEXT_MAX:
        return {"exists": True, "binary": False, "too_large": True}
    r = git(["cat-file", "blob", spec], repo["top"])
    if r.returncode != 0:
        return {"exists": False, "binary": False}
    text = decode_text(r.stdout)
    return {"exists": True, "binary": True} if text is None else {"exists": True, "binary": False, "text": text}


def api_git_discard(req):
    """POST /api/git/discard: make a file match HEAD again, in the index and the work tree (a
    file not in HEAD is deleted). old_path too, for a rename."""
    repo = need_repo()
    top = repo["top"]
    head = git_commit(top, "HEAD")
    todo = []
    for rel in [req["path"]] + ([req["old_path"]] if req.get("old_path") else []):  # check all first
        p = safe_path(rel, follow=False)
        if p == ROOT or (p.is_dir() and not p.is_symlink()):
            raise ValueError("discard works on files, not folders")
        no_git_dir(p)
        todo.append((p, top_rel(rel, repo)))
    for p, path in todo:
        in_head = head is not None and git(["cat-file", "-e", f"{head}:{path}"], top).returncode == 0
        if in_head:
            if git_has_restore():
                r = git(["restore", f"--source={head}", "--staged", "--worktree", "--", path], top, literal=True)
            else:
                r = git(["checkout", "-q", head, "--", path], top, literal=True)
            if r.returncode != 0:
                raise ApiError(500, r.stderr.decode("utf-8", "replace").strip()[-500:])
            continue
        if git(["ls-files", "-z", "--cached", "--", path], top, literal=True).stdout:  # staged, new
            r = git(["rm", "-q", "--cached", "-f", "--", path], top, literal=True)
            if r.returncode != 0:
                raise ApiError(500, r.stderr.decode("utf-8", "replace").strip()[-500:])
        if os.path.lexists(p):
            p.unlink()
    return {"ok": True}


GET_API = {"/api/file": file_info, "/api/tree": api_tree, "/api/allfiles": api_allfiles,
           "/api/search": api_search, "/api/git/info": api_git_info, "/api/git/status": api_git_status,
           "/api/git/show": api_git_show}
POST_API = {"/api/stat": api_stat, "/api/new": api_new, "/api/rename": api_rename, "/api/delete": api_delete,
            "/api/git/discard": api_git_discard}


# ---------------------------------------------------------------- processes

def env_seconds(name, default):
    """A number of seconds from the environment variable `name` (the tests shorten some), or
    `default` when it is unset, unparsable, zero, negative or not finite."""
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if 0 < value < math.inf else default


def parse_command(value):
    """--agent / --ask-agent value → argv list, or None when the feature is turned off."""
    if value is None or value.strip().lower() in ("", "none"):
        return None
    return shlex.split(value) or None


def missing_hint(exe, flag):
    """What to do about a command that is not on the PATH."""
    name = os.path.basename(exe)
    if name in ("uv", "uvx"):
        return f"install uv from https://docs.astral.sh/uv/ or pass {flag} claude"
    if name == "claude":
        return f"install Claude Code (https://docs.claude.com/en/docs/claude-code) or pass {flag} with another command"
    return f"pass {flag} with another command, or {flag} none"


# What a Claude Code session sets for the commands it runs: md-editor started from one must
# not hand them on, or its agents look nested (CLAUDE_CODE_CHILD_SESSION turns off saving
# the terminal's transcript, so /resume can't find it). A PTY has its own size.
CHILD_ENV_DROP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                  "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "COLUMNS", "LINES")


def child_env(**extra):
    env = dict(os.environ)
    for k in CHILD_ENV_DROP:
        env.pop(k, None)
    env.update(extra)
    return env


def set_winsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def wait_fd(fd, write=False, timeout=None):
    """Wait until the (non-blocking) PTY master can be read, or written."""
    try:
        select.select([] if write else [fd], [fd] if write else [], [], timeout)
    except ValueError:  # an fd past FD_SETSIZE: poll instead
        time.sleep(0.01)


def _child_setup():
    """Runs in the child after setsid(): make the PTY (already fd 0) its controlling terminal,
    and undo SIG_IGN inherited from e.g. `nohup md-editor &`, so SIGHUP still ends the session.

    This is a preexec_fn in a threaded server, which the subprocess docs call unsafe: a lock
    another thread held at fork time could deadlock the child before exec. The hazard is
    accepted: CPython re-creates its own locks in the child, glibc makes malloc fork-safe,
    and this makes only ioctl and sigaction calls. No safer route keeps Python 3.9 and
    subprocess's exec-error reporting (the terminal's "Could not start ..." status):
    os.login_tty (3.11+) must also run in the child, posix_spawn cannot set a controlling
    terminal, and a helper process that sets it and then execs the agent would report a
    failed exec as the agent's own output."""
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except (OSError, AttributeError):
        pass
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError):
            pass


def terminate_group(proc):
    """SIGHUP the process group, then SIGKILL whatever is left of it after 3 s; never raises
    if it is already gone. The group is signalled even when the main process has exited,
    since other members (holding the PTY open) may still be running."""
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:  # the whole group is gone
            return
        except OSError:  # EPERM, say, for a member of another user: SIGKILL the rest anyway
            pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            proc.poll()  # reap the main process, so the group can empty
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            except OSError:  # only members we may not signal are left
                break
            time.sleep(0.05)


def spawn_pty(argv, cwd, env, cols=80, rows=24):
    """Start argv as a session leader on a new PTY; returns (Popen, master fd). The master
    is non-blocking, so a write to a child that stops reading can still give up."""
    master = slave = None
    try:
        master, slave = os.openpty()
        set_winsize(master, cols, rows)
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                start_new_session=True, preexec_fn=_child_setup, close_fds=True)
    except BaseException:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        raise
    os.close(slave)
    os.set_inheritable(master, False)
    os.set_blocking(master, False)
    return proc, master


def read_pty(proc, fd, timeout, limit=8 << 20):
    """Everything a PTY child writes (the last `limit` bytes) until every holder of the PTY
    is gone, or until the child has exited and the PTY stayed quiet for a second; then
    close fd and end the child's process group. Past `timeout` seconds, kill the group and
    raise TimeoutExpired."""
    out = bytearray()
    deadline = time.monotonic() + timeout
    quiet = None
    try:
        while True:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                now = time.monotonic()
                if now >= deadline:
                    raise subprocess.TimeoutExpired(proc.args, timeout, output=bytes(out))
                if proc.poll() is not None:  # exited; a straggler may still hold the PTY open
                    if quiet is None:
                        quiet = now
                    elif now - quiet > 1:
                        break
                wait_fd(fd, timeout=min(0.25, deadline - now))
                continue
            except OSError:  # EIO once every slave fd is closed
                break
            if not data:
                break
            quiet = None
            out += data
            if len(out) > limit:
                del out[:len(out) - limit]
    finally:
        os.close(fd)
        terminate_group(proc)
    return bytes(out)


# ---------------------------------------------------------------- claude
#
# Each card forks a "base" session that already holds the document, so the document is
# sent once per version: a card sends only its request (plus a diff when the document has
# changed a little), and Retry / Refine fork the card's own session. If a fork fails, the
# card falls back to one stateless call carrying the whole document.

ASK_TIMEOUT = env_seconds("MDEDIT_ASK_TIMEOUT", 600)  # seconds per claude call
# Bytes of prompt in launcher mode, which passes it to claude as an argument. The binding
# limit is not that argument: claude-sandbox's wrapper hands its whole jailed command line
# (bwrap's arguments, claude's and the prompt, each quoted by bash's printf %q, which can
# make the prompt longer) to `script -c` as ONE string, and Linux caps one at 128 KiB. A
# 97 kB prompt leaves about 28 kB to spare; past the cap the launcher fails with "Argument
# list too long", which run_launcher reports as a request that is too large.
ASK_ARG_MAX = 100_000
ASK_CONTEXT = 300      # characters of context either side of a selection
ASK_BASES_MAX, ASK_CARDS_MAX = 32, 256
ASK_CARDS_CHARS = 4_000_000  # characters of document text the remembered cards may hold
# claude reports a --resume of a session it does not have with this, on stderr
SESSION_MISSING = "No conversation found with session ID:"
# where launcher mode runs claude, in the container: claude-sandbox's wrapper always binds
# ~/.cache into its jail, whatever folder it is set to make writable (`workspace-root`)
LAUNCHER_ASK_DIR = '"$HOME"/.cache/md-editor/ask'
ASK_LOCK = threading.Lock()              # guards the five below
ASK_BASES = collections.OrderedDict()   # key → {"session", "doc"}: a session holding the document
ASK_CARDS = collections.OrderedDict()   # card session id → {"key", "doc"}: the document it was asked about
ASK_KEY_LOCKS = {}                       # key → [Lock, callers], so concurrent cards wait for a single base
ASK_RUNNING = {}                         # call id → [its Popen (None until started), its prompt file or None]
ASK_CLOSING = False                      # md-editor is exiting: start no more claude calls
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\)?|[ -/]*[0-~])")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")  # JSON never holds these raw (it may hold DEL)


def ask_mode():
    return "native" if os.path.basename(ASK_CMD[0]) == "claude" else "launcher"


def ask_info():
    """Whether Ask Claude can run here, for /api/config and /api/ask."""
    info = {"available": False, "cmd": shlex.join(ASK_CMD) if ASK_CMD else None, "mode": None, "reason": ""}
    if not ASK_CMD:
        info["reason"] = "Ask Claude is turned off (--ask-agent none or MDEDIT_ASK_AGENT=none)."
        return info
    info["mode"] = ask_mode()
    if info["mode"] == "launcher" and not HAVE_PTY:
        info["reason"] = ("No PTY support on this platform; Ask Claude through claude-sandbox needs "
                          "Linux or macOS.")
    elif not shutil.which(ASK_CMD[0]):
        info["reason"] = f"{ASK_CMD[0]} not found on the PATH: {missing_hint(ASK_CMD[0], '--ask-agent')}."
    else:
        info["available"] = True
    return info


def file_type(path):
    """How the prompts name a file's type ("Python", "plain text"); None for markdown."""
    return None if is_markdown(path) else language_of(path) or "plain text"


def system_prompt(path=None):
    """The system prompt for a file: the markdown one, or one for code and other text that names
    the file type (from the name alone, so it is the same for every call about the file)."""
    lang = file_type(path)
    if lang is None:
        return SYSTEM_PROMPT
    return CODE_SYSTEM_PROMPT.format(lang=lang, valid="text" if lang == "plain text" else f"valid {lang}")


def claude_args(model, persist=True, resume=None, path=None):
    args = ["-p", "--output-format", "json"]
    if not persist:
        args.append("--no-session-persistence")
    if resume:  # fork, so the parent session never changes
        args += ["--resume", resume, "--fork-session"]
    args += ["--append-system-prompt", system_prompt(path)]
    if model:
        args += ["--model", model]
    return args + ["--tools", "Skill,Read", "--allowedTools", "Skill,Read"]


def parse_result(text):
    """The `--output-format json` result in claude's output: the last line holding a JSON
    object of type "result" (failing that, any with "result" or "is_error")."""
    fallback = None
    for line in reversed(text.splitlines()):
        i = line.find("{")
        if i < 0:
            continue
        try:
            data = json.JSONDecoder().raw_decode(line, i)[0]
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "result":
            return data
        if fallback is None and ("result" in data or "is_error" in data):
            fallback = data
    return fallback


def native_ask_dir():
    """The folder native `claude -p` runs in, ~/.cache/md-editor/ask: always the same, so
    --resume finds the sessions made there (and they stay out of ROOT's /resume list), and
    the user's own, so no one else can plant a CLAUDE.md or .claude/settings.json in it."""
    base = os.environ.get("XDG_CACHE_HOME") or ""
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "md-editor", "ask")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            raise RuntimeError(f"Ask Claude runs claude in {d}, which must be a folder of your own.")
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)
    except OSError as exc:
        raise RuntimeError(f"Ask Claude cannot use {d}: {exc.strerror or exc}")
    return d


def ask_begin(prompt_file=None):
    """Note a claude call about to start, so that end_asks() ends it; returns its id."""
    call = uuid.uuid4().hex
    with ASK_LOCK:
        if ASK_CLOSING:
            raise RuntimeError("md-editor is shutting down")
        ASK_RUNNING[call] = [None, prompt_file]
    return call


def ask_started(call, proc):
    """Note the call's process. False when md-editor has begun to exit, and end_asks() may
    have missed it: the caller ends it."""
    with ASK_LOCK:
        ASK_RUNNING[call][0] = proc
        return not ASK_CLOSING


def ask_done(call):
    with ASK_LOCK:
        ASK_RUNNING.pop(call, None)


def run_native(args, prompt):
    """`claude -p` on pipes, in native_ask_dir(), which holds no project CLAUDE.md, and in a
    process group of its own, which end_asks() ends if md-editor exits first. Returns its
    output."""
    exe = shutil.which(ASK_CMD[0]) or ASK_CMD[0]
    cwd = native_ask_dir()
    call = ask_begin()
    try:
        try:
            proc = subprocess.Popen([exe] + ASK_CMD[1:] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=cwd,
                                    env=child_env(), start_new_session=True)
        except OSError as exc:
            raise RuntimeError(f"Could not run {exe}: {exc}")
        with proc:  # closes the pipes and reaps it
            if not ask_started(call, proc):
                terminate_group(proc)
                raise RuntimeError("md-editor is shutting down")
            try:
                out, err = proc.communicate(prompt, timeout=ASK_TIMEOUT)
            except BaseException:  # the timeout, mostly: end it, as subprocess.run would
                terminate_group(proc)
                raise
    finally:
        ask_done(call)
    if parse_result(out) is None:
        raise RuntimeError((err or out or "no output from claude").strip()[:2000])
    return out


def run_launcher(args, prompt):
    """`claude -p` in the launcher's container (`<launcher> shell -c SCRIPT`, run in ROOT).
    The launcher uses `podman exec -it`, so it needs a terminal: it gets a fresh PTY. The
    prompt goes through a file in ROOT, which the container mounts at the same path. In
    there `claude` is claude-sandbox's wrapper, which runs Claude Code in its jail on a
    PTY of its own; -p ignores a terminal stdin, so the prompt is passed as an argument.
    Returns the output, ANSI-stripped."""
    data = prompt.encode("utf-8")
    if len(data) > ASK_ARG_MAX:
        raise RuntimeError(f"This request ({len(data) // 1000} kB with the document or selected text) is too "
                           f"large to pass to Claude Code in claude-sandbox, which takes about "
                           f"{ASK_ARG_MAX // 1000} kB at most.")
    root = ROOT
    pf = root / f".md-editor-ask-{uuid.uuid4().hex}.txt"
    call = ask_begin(pf)
    try:
        fd = os.open(pf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        ask_done(call)
        raise RuntimeError(f"Ask Claude via claude-sandbox needs to write a temporary file in {root}, "
                           f"but cannot: {exc.strerror or exc}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # the fixed folder keeps these sessions out of ROOT's /resume list and lets --resume find them
        script = (f"mkdir -p {LAUNCHER_ASK_DIR} && cd {LAUNCHER_ASK_DIR} && "
                  f"prompt=$(cat {shlex.quote(str(pf))}) && exec claude "
                  + " ".join(shlex.quote(a) for a in args) + ' -- "$prompt"')
        # the `shell` verb otherwise runs the shell md-editor was started from (zsh, fish, ...)
        env = child_env(TERM="dumb", NO_COLOR="1", CLAUDE_SANDBOX_SHELL="sh")
        env.pop("COLORTERM", None)
        try:
            proc, master = spawn_pty(ASK_CMD + ["shell", "-c", script], str(root), env, 200, 50)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Could not start {shlex.join(ASK_CMD)}: {exc}")
        if not ask_started(call, proc):
            os.close(master)
            terminate_group(proc)
            raise RuntimeError("md-editor is shutting down")
        raw = read_pty(proc, master, ASK_TIMEOUT)
    except OSError as exc:
        raise RuntimeError(f"Ask Claude via claude-sandbox failed: {exc}")
    finally:
        ask_done(call)
        try:
            pf.unlink()
        except OSError:
            pass
    text = CTRL_RE.sub("", ANSI_RE.sub("", raw.decode("utf-8", "replace")).replace("\r", ""))
    if parse_result(text) is None:
        tail = text.strip()[-2000:] or f"no output from {shlex.join(ASK_CMD)}"
        if "Argument list too long" in tail:
            tail = "The request is too large to pass to Claude Code in claude-sandbox.\n\n" + tail
        raise RuntimeError(tail)
    return text


def end_asks():
    """At exit: end the claude calls still running, whose threads die with the server, and
    delete their prompt files (a launcher may outlive the PTY hang-up)."""
    global ASK_CLOSING
    with ASK_LOCK:
        ASK_CLOSING = True
        running = [tuple(v) for v in ASK_RUNNING.values()]
    for proc, pf in running:
        if proc is not None:
            terminate_group(proc)
        if pf is not None:
            try:
                pf.unlink()
            except OSError:
                pass


def run_claude(args, prompt):
    """One `claude -p` call, natively or through the launcher; returns its JSON result."""
    out = run_native(args, prompt) if ask_mode() == "native" else run_launcher(args, prompt)
    data = parse_result(out)
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result") or data)[:2000])
    return data


def doc_tag(path, doc):
    lang = file_type(path)
    kind = f' type="{lang}"' if lang else ""
    return f'<document path="{path}"{kind}>\n{doc}\n</document>'


def doc_diff(old, new, path):
    """Unified diff old → new for a <document_update>: '' when they match, None when it is
    too big to be worth sending instead of the whole document."""
    if old == new:
        return ""
    limit = min(0.3 * len(new), 40000)
    out, n = [], 0
    for line in difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3):
        if not line.endswith("\n"):
            line += "\n"
        n += len(line)
        if n > limit:
            return None
        out.append(line)
    return "".join(out)


def build_request(req, update=""):
    """One card's <request>; `update` is a diff from the document the session holds."""
    doc = req.get("doc", "")
    s, e = int(req.get("start", 0)), int(req.get("end", 0))
    whole = s == 0 and e >= len(doc)
    mode = req.get("mode", "replace")
    parts = []
    if update:
        parts.append("<document_update>The document has changed since the version above. Unified diff "
                     f"from that version to the current one:\n<diff>\n{update}</diff></document_update>")
    if whole:
        parts.append("<selection>The whole document is selected.</selection>")
    else:
        first = doc.count("\n", 0, s) + 1
        last = first + doc.count("\n", s, max(s, e - 1))
        parts += [f'<selection lines="{first}-{last}">\n{doc[s:e]}\n</selection>',
                  f"<context_before>{doc[max(0, s - ASK_CONTEXT):s]}</context_before>",
                  f"<context_after>{doc[e:e + ASK_CONTEXT]}</context_after>"]
    parts += [f"<mode>{mode}</mode>", f"<instruction>\n{req.get('instruction', '').strip()}\n</instruction>"]
    if req.get("previous"):
        parts.append(f"<previous_attempt>\n{req['previous']}\n</previous_attempt>\n"
                     "The user was not satisfied with the previous attempt; the instruction "
                     "above is their feedback on it.")
    if mode != "replace":
        parts.append("Reply with your comments only.")
    elif is_markdown(req.get("path")):
        parts.append("Reply with the replacement text only.")
    else:
        parts.append("Reply with the replacement text only, with no code fence around it.")
    return f'<request path="{req.get("path") or ""}">\n' + "\n\n".join(parts) + "\n</request>"


def ask_key(req):
    return (shlex.join(ASK_CMD), str(ROOT), req.get("path") or "", req.get("model") or "")


def base_session(key, doc):
    """The base session for key: one holding `doc`, or a version close enough to send a
    diff, made if need be. Returns (session id, diff to send)."""
    with ASK_LOCK:  # the key's lock lives while any caller holds or waits for it
        entry = ASK_KEY_LOCKS.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            return _base_session_locked(key, doc)
    finally:
        with ASK_LOCK:
            entry[1] -= 1
            if not entry[1]:
                del ASK_KEY_LOCKS[key]


def _base_session_locked(key, doc):
    path, model = key[2], key[3]
    with ASK_LOCK:
        base = ASK_BASES.get(key)
    if base is not None:
        diff = doc_diff(base["doc"], doc, path)
        if diff is not None:
            with ASK_LOCK:
                if key in ASK_BASES:
                    ASK_BASES.move_to_end(key)
            return base["session"], diff
    t0 = time.time()
    what = "document" if is_markdown(path) else "file"
    msg = (doc_tag(path, doc) + f"\n\nThis is the {what} the user is editing. Each later message "
           "is one independent request about it. Reply with just OK.")
    data = run_claude(claude_args(model, path=path), msg)
    sid = data.get("session_id")
    if not sid:
        raise RuntimeError("claude gave no session id")
    u = data.get("usage") or {}
    print(f"[ask] base session {sid} for {path} ({len(doc)} chars) in {time.time() - t0:.1f}s; input tokens: "
          f"{u.get('input_tokens')} new, {u.get('cache_read_input_tokens')} cached, "
          f"{u.get('cache_creation_input_tokens')} written to the cache", flush=True)
    with ASK_LOCK:
        ASK_BASES[key] = {"session": sid, "doc": doc}
        ASK_BASES.move_to_end(key)
        while len(ASK_BASES) > ASK_BASES_MAX:
            ASK_BASES.popitem(last=False)
    return sid, ""


def forget_session(sid):
    with ASK_LOCK:
        ASK_CARDS.pop(sid, None)
        for k in [k for k, b in ASK_BASES.items() if b["session"] == sid]:
            del ASK_BASES[k]


def ask_forked(req, key):
    """Ask in a fork of the card's last session (Retry / Refine), else of the base session;
    returns claude's JSON result. RuntimeError means the forked route failed."""
    doc, path = req.get("doc", ""), key[2]
    parent = diff = None
    card = req.get("session")
    if card:
        with ASK_LOCK:
            known = ASK_CARDS.get(card)
        if known and known["key"] == key:
            diff = doc_diff(known["doc"], doc, path)
            parent = card if diff is not None else None
    if parent is None:
        parent, diff = base_session(key, doc)
    try:
        data = run_claude(claude_args(key[3], resume=parent, path=path), build_request(req, diff))
    except RuntimeError as exc:
        # forget the parent only when claude no longer has it: a failure of the launcher, a
        # request too large to pass, or a passing API error leaves it good for the next card
        if SESSION_MISSING + " " + parent in " ".join(str(exc).split()):
            forget_session(parent)
        raise
    sid = data.get("session_id")
    if sid:
        remember_card(sid, key, doc)
    return data


def remember_card(sid, key, doc):
    """Note the document version card session `sid` was asked about, for its Retry / Refine.
    The oldest cards are forgotten (their Retry forks the base instead) past ASK_CARDS_MAX
    of them, or past ASK_CARDS_CHARS characters of document text, keeping the newest."""
    with ASK_LOCK:
        base = ASK_BASES.get(key)
        if base is not None and base["doc"] == doc:
            doc = base["doc"]  # the same version: share the base's copy
        ASK_CARDS[sid] = {"key": key, "doc": doc}
        chars = sum(len(c["doc"]) for c in ASK_CARDS.values())
        while len(ASK_CARDS) > 1 and (len(ASK_CARDS) > ASK_CARDS_MAX or chars > ASK_CARDS_CHARS):
            chars -= len(ASK_CARDS.popitem(last=False)[1]["doc"])


def unfence(text: str, original: str, code=False) -> str:
    """The reply without a code fence around it (unless the selection had one). For code
    (code=True) the first line keeps its indentation, and a selection's final newline stays."""
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and not original.lstrip().startswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2:
            t = "\n".join(lines[1:-1])
    elif code:
        t = re.sub(r"\A(?:[ \t]*\n)+", "", text.replace("\r\n", "\n")).rstrip()
    if code and original.endswith("\n") and t and not t.endswith("\n"):
        t += "\n"
    return t


def ask_claude(req):
    t0 = time.time()
    key = ask_key(req)
    doc = req.get("doc", "")
    try:
        data, forked = ask_forked(req, key), True
    except RuntimeError as exc:
        why = " ".join(str(exc).split())[:300]
        print(f"[ask] fork failed, stateless: {why}", flush=True)
        msg = doc_tag(key[2], doc) + "\n\n" + build_request(req)
        data, forked = run_claude(claude_args(key[3], persist=False, path=key[2]), msg), False
    u = data.get("usage") or {}
    print(f"[ask] {'forked' if forked else 'stateless'} reply in {time.time() - t0:.1f}s; input tokens: "
          f"{u.get('input_tokens')} new, {u.get('cache_read_input_tokens')} cached, "
          f"{u.get('cache_creation_input_tokens')} written to the cache", flush=True)
    original = doc[int(req.get("start", 0)):int(req.get("end", 0))]
    return {
        "result": unfence(data.get("result", ""), original, code=not is_markdown(key[2])),
        "seconds": round(time.time() - t0, 1),
        "cost": data.get("total_cost_usd"),
        "session": data.get("session_id") if forked else None,  # Retry / Refine send it back
        "forked": forked,
    }


def prepare_ask(req):
    """Make the base session for a document before its first card needs it."""
    try:
        base_session(ask_key(req), req.get("doc", ""))
    except RuntimeError as exc:
        print(f"[ask] prepare failed: {' '.join(str(exc).split())[:300]}", flush=True)
        raise
    return {"ok": True}


# ---------------------------------------------------------------- pdf

RAW_SRC_RE = re.compile(r'src="/raw/([^"]*)"')


def pdf_image_src(m):
    """Rewrite one <img src="/raw/PATH"> for the print page: Chrome reads the image straight from
    disk (a file:// URL), but only when PATH stays inside ROOT (safe_path resolves it, so neither
    ../ in the string nor a symlink can reach a file outside the folder); otherwise the src is
    dropped. The agent fully controls the HTML posted to /api/pdf, so this is checked here, not
    with a blind string replace."""
    path = unquote(m.group(1).split("?", 1)[0], errors="surrogateescape")
    try:
        p = safe_path(path)
    except (ValueError, OSError):
        return 'src=""'
    return f'src="{p.as_uri()}"'


def export_pdf(req):
    """Print the editor's rendered HTML to <name>.pdf beside the markdown file. The print HTML is
    agent-influenced (whatever is posted as req["html"]), so it is rendered OUTSIDE ROOT, a meta
    Content-Security-Policy in the page blocks scripts, frames and plugins (only images and the
    stylesheets load), image sources are resolved through safe_path, and the .pdf is written with
    O_NOFOLLOW so a symlink the agent planted at <name>.pdf cannot redirect the write out of ROOT."""
    if not CHROME_BIN:
        raise RuntimeError("PDF export needs Google Chrome or Chromium (or set MDEDIT_CHROME)")
    md = safe_path(req["path"])
    html = req["html"]
    if not isinstance(html, str):
        raise ValueError("html must be a string")
    pdf = md.with_suffix(".pdf")
    if os.path.islink(pdf) or (pdf.exists() and not pdf.is_file()):
        raise RuntimeError("refusing to write the PDF: a symlink or non-file is in its place")
    body = RAW_SRC_RE.sub(pdf_image_src, html)
    page = PDF_TEMPLATE.format(title=md.stem.replace("<", "&lt;"), body=body)
    profile = tempfile.mkdtemp(prefix="mdedit-chrome-")  # outside ROOT: the agent cannot touch it
    tmp_html = Path(profile) / "print.html"
    out_pdf = Path(profile) / "out.pdf"
    try:
        tmp_html.write_text(page, encoding="utf-8")
        proc = subprocess.run(
            [CHROME_BIN, "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
             f"--user-data-dir={profile}", "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw",
             f"--print-to-pdf={out_pdf}", tmp_html.as_uri()],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not out_pdf.exists():
            raise RuntimeError(f"Chrome failed to print: {(proc.stderr or proc.stdout).strip()[-800:]}")
        data = out_pdf.read_bytes()
        try:
            fd = os.open(pdf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        except OSError as exc:
            raise RuntimeError(f"refusing to write the PDF: {exc.strerror or exc}")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return {"pdf": pdf.relative_to(ROOT).as_posix(), "bytes": pdf.stat().st_size}


# ---------------------------------------------------------------- term

SCROLLBACK_MAX = 512 * 1024
NUDGE_SECONDS = 0.25  # how long a reattaching browser's PTY is one row short (see resize)
CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
DEC_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]*)([hl])")
# The DEC private modes (CSI ? Pm h / l) that change how xterm.js draws or what it sends, by
# slot. The modes in a slot replace each other, as in xterm.js: there is one screen buffer,
# one mouse protocol and one mouse encoding, and resetting any mode of a slot resets the slot.
# They are listed in the order a replay restores them, the alternate screen first.
DEC_SLOTS = {47: "alt", 1047: "alt", 1049: "alt", 1: "cursor-keys", 7: "wrap", 45: "reverse-wrap",
             25: "cursor", 66: "keypad", 9: "mouse", 1000: "mouse", 1002: "mouse", 1003: "mouse",
             1006: "mouse-encoding", 1016: "mouse-encoding", 1004: "focus", 2004: "paste"}
DEC_DEFAULTS = {"wrap": 7, "cursor": 25}  # the slots a terminal reset leaves set, by this mode


def track_modes(modes, data, end):
    """Apply the DEC private mode changes in data[:end] to `modes`, slot → the mode set in it,
    or 0 once it is reset."""
    for m in DEC_MODE_RE.finditer(data, 0, end):
        for p in m.group(1).split(b";"):
            slot = DEC_SLOTS.get(int(p)) if p.isdigit() else None
            if slot:
                modes[slot] = int(p) if m.group(2) == b"h" else 0


def mode_prefix(modes):
    """The sequences that take a terminal just reset to `modes`, alternate screen first."""
    out = []
    for slot in dict.fromkeys(DEC_SLOTS.values()):
        mode, default = modes.get(slot), DEC_DEFAULTS.get(slot, 0)
        if mode is not None and mode != default:
            out.append(f"\x1b[?{mode}h" if mode else f"\x1b[?{default}l")
    return "".join(out).encode()


def agent_display():
    return shlex.join(AGENT_CMD) if AGENT_CMD else None


def agent_info():
    """Whether the side-panel terminal can run here, for /api/config and /api/term."""
    info = {"available": False, "cmd": agent_display(), "reason": ""}
    if not AGENT_CMD:
        info["reason"] = "The terminal is turned off (--agent none or MDEDIT_AGENT=none)."
    elif not HAVE_PTY:
        info["reason"] = "No PTY support on this platform; the terminal needs Linux or macOS."
    elif not shutil.which(AGENT_CMD[0]):
        info["reason"] = f"{AGENT_CMD[0]} not found on the PATH: {missing_hint(AGENT_CMD[0], '--agent')}."
    else:
        info["available"] = True
    return info


def agent_env():
    return child_env(TERM="xterm-256color", COLORTERM="truecolor")


class _Proc:
    """One spawned agent: the process and its PTY master. Only its reader thread closes `fd`."""

    def __init__(self, proc, fd):
        self.proc, self.fd = proc, fd
        self.wlock = threading.Lock()  # serialises writes; the reader takes it to close fd
        self.closed = False            # fd closed or about to be: stop using it
        self.retired = False           # replaced or killed: drop its output, announce no exit


class TermSession:
    """The single agent session behind /api/term, shared by every attached browser.

    `lock` guards the state, scrollback and client set, and is held while a frame
    is broadcast, so a new client gets status + replay + live output in order and a
    new session's status always reaches clients before its output. `op_lock`
    serialises spawn / restart / first-resize so two of them never race.

    A browser resets its terminal before the replay, which holds only the last 512 KiB
    of output, so an agent's terminal modes, set once at its start, would be lost from a
    long session: `replay_modes` follows the modes in force where the scrollback begins,
    and the replay restores them first. The replay then sets the rest in their order,
    so the browser ends in the modes the agent last set, and draws the alternate screen's
    output there only if that is where it went.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.op_lock = threading.Lock()
        self.clients = set()
        self.cur = None          # _Proc of the live session, if any
        self.next_id = 0
        self.id = None
        self.state = "none"      # none | running | exited | failed
        self.cwd = None
        self.code = None
        self.message = None
        self.size = (80, 24)     # cols, rows
        self.scrollback = bytearray()
        self.replay_modes = {}   # DEC private modes in force where the scrollback begins
        self.closing = False

    # -- clients (call with self.lock held unless noted)

    def status(self):
        return {"type": "status", "id": self.id, "state": self.state, "cmd": agent_display() or "",
                "cwd": self.cwd, "code": self.code, "message": self.message}

    def _broadcast(self, frame):
        for c in [c for c in self.clients if not c.send_frame(frame)]:
            self.clients.discard(c)

    def _broadcast_status(self):
        self._broadcast(ws_frame(OP_TEXT, json.dumps(self.status()).encode()))

    def attach(self, client):
        with self.lock:
            ok = client.send_frame(ws_frame(OP_TEXT, json.dumps(self.status()).encode()))
            # the replay; sent even when empty for a running or ended session, because the
            # client takes the first binary frame after the status to be the replay
            if ok and (self.scrollback or self.state in ("running", "exited")):
                replay = mode_prefix(self.replay_modes) + bytes(self.scrollback)
                ok = client.send_frame(ws_frame(OP_BIN, replay))
            if ok:
                self.clients.add(client)
        return ok

    def detach(self, client):
        with self.lock:
            self.clients.discard(client)

    def _append(self, data):
        sb = self.scrollback
        sb += data
        excess = len(sb) - SCROLLBACK_MAX
        if excess > 0:  # keep the tail, starting just after a newline when one is close
            nl = sb.find(b"\n", excess, excess + 4096)
            cut = nl + 1 if nl >= 0 else excess
            esc = sb.rfind(b"\x1b", max(0, cut - 64), cut)
            m = CSI_RE.match(sb, esc) if esc >= 0 else None
            if m and m.end() > cut:  # never cut a control sequence in two: drop all of it
                cut = m.end()
            track_modes(self.replay_modes, sb, cut)
            del sb[:cut]

    # -- process

    def _spawn_locked(self):
        """Start the agent in the current ROOT (caller holds op_lock and lock, no live session)."""
        self.next_id += 1
        self.id, self.state, self.cwd = self.next_id, "running", str(ROOT)
        self.code = self.message = None
        self.scrollback = bytearray()
        self.replay_modes = {}
        cols, rows = self.size
        try:
            proc, master = spawn_pty(AGENT_CMD, self.cwd, agent_env(), cols, rows)
        except (OSError, subprocess.SubprocessError) as exc:
            self.cur, self.state = None, "failed"
            self.message = f"Could not start {agent_display()}: {exc}"
            print(f"[term] {self.message}", flush=True)
            line = f"\r\n\x1b[31m{self.message}\x1b[0m\r\n".encode()
            self.scrollback += line
            self._broadcast_status()
            self._broadcast(ws_frame(OP_BIN, line))
            return
        # the master is non-blocking, so a write to an agent that stops reading its input can
        # still give up when the session ends or is replaced, instead of hanging that browser
        rec = _Proc(proc, master)
        self.cur = rec
        print(f"[term] spawned {agent_display()} in {self.cwd}", flush=True)
        self._broadcast_status()  # before the reader can forward any output
        threading.Thread(target=self._read_loop, args=(rec,), daemon=True).start()

    def _retire_locked(self):
        rec, self.cur = self.cur, None
        if rec is not None:
            rec.retired = True
        return rec

    def _read_loop(self, rec):
        quiet = None  # since when the agent has been gone and the PTY silent
        held = False  # the agent exited, but something it started still holds the PTY
        while True:
            try:
                data = os.read(rec.fd, 65536)
            except BlockingIOError:
                if rec.proc.poll() is not None:  # as in read_pty: a second of quiet ends it
                    now = time.monotonic()
                    if quiet is None:
                        quiet = now
                    elif now - quiet > 1:
                        held = True
                        break
                wait_fd(rec.fd, timeout=0.25)
                continue
            except OSError:  # EIO once every slave fd is closed
                data = b""
            if not data:
                break
            quiet = None
            if rec.retired:
                continue
            with self.lock:
                if self.cur is rec:
                    self._append(data)
                    self._broadcast(ws_frame(OP_BIN, data))
        rec.closed = True  # a writer waiting for room gives up and releases wlock
        with rec.wlock, self.lock:
            os.close(rec.fd)
        if held and not rec.retired:  # end the stragglers, as Restart would (it does when retired)
            terminate_group(rec.proc)
        code = rec.proc.wait()
        print(f"[term] exited {code}", flush=True)
        with self.lock:
            if self.cur is rec and not rec.retired:
                self.cur, self.state, self.code = None, "exited", code
                self._broadcast_status()

    def write(self, data, alive=lambda: True):
        """Type `data` into the agent. While it is not reading, wait (holding wlock, so other
        writes queue behind) until it does, the session ends or is replaced, or `alive()`
        turns false: the browser that sent it has gone (see WSClient.keepalive)."""
        with self.lock:
            rec = self.cur
        if rec is None:
            return
        with rec.wlock:  # not under self.lock: a blocked write must not stall the output
            view = memoryview(data)
            while view and not rec.closed and not rec.retired and alive():
                try:
                    view = view[os.write(rec.fd, view):]
                except BlockingIOError:  # the agent is not reading: wait for room
                    wait_fd(rec.fd, write=True, timeout=0.5)
                except OSError:
                    return

    def resize(self, cols, rows, nudge=False):
        """Size the PTY (the kernel sends the agent SIGWINCH when that changes it); the first
        resize of all starts the session. `nudge`: this is a newly attached browser's first
        resize. Its terminal has only the replay, so the agent should redraw it, but an
        unchanged size sends no SIGWINCH: the PTY is one row short for NUDGE_SECONDS, long
        enough for the agent to see (Node reads the size when it handles the signal, and
        ignores a SIGWINCH that leaves it unchanged), and then the right size again."""
        cols, rows = max(1, min(1000, cols)), max(1, min(1000, rows))
        with self.lock:
            nudged = nudge and (cols, rows) == self.size
            self.size = (cols, rows)
            rec = self.cur
            if rec is not None and not rec.closed:
                try:
                    if nudged:
                        set_winsize(rec.fd, cols, rows - 1 if rows > 1 else 2)
                    else:
                        set_winsize(rec.fd, cols, rows)
                except OSError:
                    nudged = False
            else:
                nudged = False
            first = self.state == "none" and not self.closing
        if nudged:
            time.sleep(NUDGE_SECONDS)
            with self.lock:
                if self.cur is rec and not rec.closed:
                    try:
                        set_winsize(rec.fd, *self.size)  # the latest, should another browser have resized
                    except OSError:
                        pass
        if first:
            with self.op_lock, self.lock:
                if self.state == "none" and not self.closing:
                    self._spawn_locked()

    def restart(self):
        with self.op_lock:
            with self.lock:
                if self.closing:
                    return
                old = self._retire_locked()
            if old is not None:
                terminate_group(old.proc)
            with self.lock:
                if not self.closing:
                    self._spawn_locked()

    def kill(self):
        """End the session for good (server shutdown); safe to call more than once."""
        with self.lock:
            self.closing = True
            old = self._retire_locked()
        if old is not None:
            terminate_group(old.proc)


TERM = TermSession()


# ---------------------------------------------------------------- websocket

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE = 4 * 1024 * 1024
WS_SEND_TIMEOUT = 10  # seconds; a browser that stops reading for longer is dropped
# seconds between pings; a browser that has not answered one by the next is dropped
WS_PING_INTERVAL = env_seconds("MDEDIT_WS_PING", 30)
WS_DRAIN = 2, 16 << 20  # after a protocol error, read on for up to 2 s or 16 MiB before closing
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def ws_frame(opcode, payload=b""):
    """One unmasked, unfragmented server frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def ws_unmask(payload, key):
    n = len(payload)
    if not n:
        return b""
    mask = int.from_bytes((key * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ mask).to_bytes(n, "big")


class WSClient:
    """A connected browser; frames from the reader thread and the handler never interleave.

    Output is broadcast under TermSession.lock, so a browser that stops reading would stall
    the session for everyone: a frame that cannot be sent within WS_SEND_TIMEOUT drops the
    client and shuts its socket, and the page reconnects and gets the replay. A browser that
    went away without closing the connection (a suspended laptop, a dropped port forward)
    stops answering pings, and keepalive() drops it.
    """

    def __init__(self, sock):
        self.sock = sock
        self.send_lock = threading.Lock()
        self.alive = True
        self.last_seen = time.monotonic()  # when a frame last came from the browser
        self.writing = False               # the handler is in TERM.write, so it reads no frames
        self.sized = False                 # it has sent a resize
        try:  # bounds each blocking send(), not reads, which wait as long as the browser is idle
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", 1, 0))
        except (OSError, AttributeError, struct.error):
            pass

    def send_frame(self, frame):
        with self.send_lock:
            if not self.alive:
                return False
            try:
                # one deadline for the whole frame: a send() that times out after sending part
                # of it would otherwise start the clock again
                view, deadline = memoryview(frame), time.monotonic() + WS_SEND_TIMEOUT
                while view:
                    if time.monotonic() > deadline:
                        raise TimeoutError("send timed out")
                    try:
                        view = view[self.sock.send(view):]
                    except BlockingIOError:  # SO_SNDTIMEO passed with no room: wait on
                        pass
                return True
            except (OSError, ValueError):  # includes TimeoutError
                self._drop_locked()
                return False

    def _drop_locked(self):
        """Stop using the socket; shutting it also ends the handler's blocked read."""
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def keepalive(self, stop):
        """Ping every WS_PING_INTERVAL until `stop` is set; drop the client when nothing (not
        even the pong browsers send by themselves) has come back since the last ping.

        While the handler is blocked in a PTY write (a paste the agent is not reading) the
        pongs wait unread behind it, so the client is pinged but not judged until it is done.
        A browser that closed its end meanwhile makes a ping fail (the first draws a reset,
        the next fails), which drops it, and TERM.write then abandons its paste. One that
        vanished without closing (a suspended laptop) goes unnoticed until TCP gives up on
        it; till then its paste, with this thread and its handler, waits until the agent
        reads its input again or the session is restarted."""
        pinged = None
        while not stop.wait(WS_PING_INTERVAL):
            if self.writing:
                pinged = None
            elif pinged is not None and self.last_seen < pinged:
                with self.send_lock:
                    self._drop_locked()
                return
            else:
                pinged = time.monotonic()
            if not self.send_frame(ws_frame(OP_PING)):
                return

    def close(self, code=None):
        self.send_frame(ws_frame(OP_CLOSE, b"" if code is None else struct.pack("!H", code)))
        with self.send_lock:
            self.alive = False


class WSProtocolError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- http

# Only our own page may use the server, since it reads and writes files and starts
# processes. Every request needs a local Host (any other name means DNS rebinding), and
# an Origin, when there is one, must be that same host: browsers send it with every POST,
# PUT and WebSocket request, so other websites are refused. Any port is fine, so port
# forwarding (8765 → 8766, say) still works.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
REQUEST = threading.local()  # .conn: the socket of the API request this thread is answering
QUIET_POLLS = ("/api/stat", "/api/git/status", "/api/git/info")  # not logged when they succeed
HOST_RE = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[^\[\]:/@\s]+)(?::[0-9]{1,5})?")
# Content types that run script or embed documents when a browser opens them as a page (as
# opposed to an <img> subresource): /raw/ serves these as a download, so a link or placeholder
# in the preview cannot run them in the editor's origin even if the sandbox CSP were bypassed.
RAW_ACTIVE = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
              "text/javascript", "application/javascript", "application/ecmascript", "text/ecmascript",
              "application/pdf"}
# The editor page's own Content-Security-Policy: its code and the pinned CDN libraries, nothing
# else. Kept permissive enough (inline + eval, the two CDNs, ws: for the terminal) not to break
# the no-build front end, while still refusing script, frames or plugins from anywhere else.
APP_CSP = ("default-src 'self'; "
           "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net blob:; "
           "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
           "img-src 'self' data: blob: http: https:; "
           "font-src 'self' data: https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
           "connect-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net ws: wss:; "
           "worker-src 'self' blob:; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def forbidden(self, need_origin):
        """Why the request fails the Host / Origin check, or None if it passes."""
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1:
            return "expected one Host header"
        host = hosts[0]
        m = HOST_RE.fullmatch(host)
        if not m or m.group(1).lower() not in LOCAL_HOSTS:
            return f"Host {host[:100]!r} is not this machine"
        # Browsers say who made a request: another site's page (an <img> or <iframe> pointed at
        # a search that runs for a minute, say) gets nothing from the API or the folder's files.
        # Our own page's requests are same-origin, and a typed address is "none".
        site = self.headers.get("Sec-Fetch-Site")
        if site in ("cross-site", "same-site") and urlparse(self.path).path.startswith(("/api/", "/raw/")):
            return f"a {site} request"
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1:
            return "more than one Origin header"
        if not origins:
            return "missing Origin header" if need_origin else None
        if origins[0] != "http://" + host:
            return f"Origin {origins[0][:100]!r} does not match Host {host!r}"
        return None

    def guard(self, need_origin=False):
        """Refuse (403) a request that fails the Host / Origin check; True if it was refused."""
        why = self.forbidden(need_origin)
        if why is None:
            return False
        print(f"[guard] refused {self.command} {self.path[:100]!r}: {why}", flush=True)
        if self.command == "HEAD":
            self.send_response(403)
            self.end_headers()
        else:
            self.send_json({"error": f"forbidden: {why}"}, 403)
        return True

    def log_message(self, fmt, *args):
        line = str(args[0] if args else "")
        if "/api/events" in line:
            return
        if any(p in line for p in QUIET_POLLS) and len(args) > 1 and str(args[1]) == "200":
            return  # the page polls these every few seconds
        super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        # the editor page (and any HTML the static handler serves) gets the site-wide CSP, unless
        # the response already set its own (the /raw/ sandbox policy), so the two never collide
        buf = b"".join(getattr(self, "_headers_buffer", None) or []).lower()
        if b"text/html" in buf and b"content-security-policy" not in buf:
            self.send_header("Content-Security-Policy", APP_CSP)
        super().end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_raw(self, rel):
        """Serve a file from the document root (images referenced by the markdown). The agent
        controls these files, so the response is locked down: a file opened on its own (an SVG or
        HTML file in the folder, say) must not run scripts in the editor's origin, which can use
        the API. The file is streamed, so opening a huge artefact does not read it all into memory."""
        p = safe_path(rel)
        try:
            st = os.stat(p)
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        if not stat.S_ISREG(st.st_mode):
            return self.send_json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        # the sandbox keeps an opened SVG/HTML file from scripting in our origin; nosniff stops a
        # mislabelled file being run as script; <img> ignores both headers
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self' data:; "
                                                    "style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        # a type that runs script or embeds a document, opened as a page (not fetched as an image),
        # is downloaded rather than rendered; the name is kept out of the header (the agent chooses
        # it, and it may hold CR/LF) to avoid a header injection
        if (ctype in RAW_ACTIVE or ctype.endswith("+xml")) and self.headers.get("Sec-Fetch-Dest") != "image":
            self.send_header("Content-Disposition", "attachment")
        self.end_headers()
        self.stream_file(p, st.st_size)

    def stream_file(self, p, size):
        """Send `size` bytes of `p` in 64 KiB pieces, so a huge file is never read whole."""
        remaining = size
        try:
            with open(p, "rb") as f:
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def answer(self, fn, arg):
        """Reply with fn(arg) as JSON, or {"error": message}: ApiError has its own status, bad
        input is 400 and a failing file operation 500."""
        REQUEST.conn = self.connection  # a long search stops when its client goes away
        try:
            if not isinstance(arg, dict):
                raise ValueError("expected a JSON object")
            res, status = fn(arg), 200
        except ApiError as exc:
            res, status = {"error": str(exc)}, exc.status
        except KeyError as exc:
            res, status = {"error": f"missing {exc.args[0] if exc.args else 'parameter'}"}, 400
        except (ValueError, TypeError) as exc:
            res, status = {"error": str(exc)}, 400
        except subprocess.TimeoutExpired:
            res, status = {"error": "git took too long"}, 504
        except OSError as exc:
            msg = f"{exc.strerror}: {exc.filename}" if exc.strerror and exc.filename else str(exc)
            res, status = {"error": msg}, 404 if isinstance(exc, FileNotFoundError) else 500
        except Exception as exc:  # a bug: still answer, rather than drop the connection
            traceback.print_exc()
            res, status = {"error": f"internal error: {type(exc).__name__}: {exc}"}, 500
        finally:
            REQUEST.conn = None
        try:
            return self.send_json(res, status)
        except (BrokenPipeError, ConnectionResetError):  # e.g. a search the page gave up on
            pass

    def do_HEAD(self):
        if not self.guard():
            super().do_HEAD()

    def do_GET(self):
        u = urlparse(self.path)
        if self.guard(need_origin=u.path == "/api/term"):
            return
        # surrogateescape: a file name that is not UTF-8 comes back as the bytes it is on disk
        q = {k: v[0] for k, v in parse_qs(u.query, errors="surrogateescape").items()}
        try:
            if u.path == "/api/config":
                return self.send_json({"root": str(ROOT), "initial": INITIAL, "skills": list_skills(),
                                       "files": list_files(), "agent": agent_info(), "ask": ask_info()})
            if u.path == "/api/term":
                return self.term_socket()
            if u.path == "/api/download":
                p = safe_path(q["path"])
                if not p.is_file() or p.suffix.lower() != ".pdf":
                    return self.send_json({"error": "not found"}, 404)
                size = p.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                # the agent chooses the name and it may hold CR/LF or quotes: percent-encoded
                # (RFC 5987) it cannot break out of the header
                name = quote(p.name.encode("utf-8", "surrogateescape"), safe="")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{name}")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                self.stream_file(p, size)
                return
            if u.path == "/api/browse":
                return self.send_json(browse(q.get("dir", "")))
            if u.path == "/api/files":
                return self.send_json(list_files())
            if u.path in GET_API:  # /api/file, tree, allfiles, search, git/*
                return self.answer(GET_API[u.path], q)
            if u.path == "/api/events":
                return self.events()
            if u.path.startswith("/raw/"):
                return self.send_raw(unquote(u.path[len("/raw/"):], errors="surrogateescape"))
        except (ValueError, KeyError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        return super().do_GET()

    def do_PUT(self):
        if self.guard(need_origin=True):
            return
        if urlparse(self.path).path != "/api/file":
            return self.send_json({"error": "unknown endpoint"}, 404)
        try:
            req = self.read_json()
            p = safe_path(req["path"])
            base = req.get("base_version")
            version = version_of(p)
            if not req.get("force") and base is not None and version != base:
                # the disk's text as GET /api/file gives it; null when it is not text any more
                # (binary, not UTF-8, over 5 MB), which the page can only overwrite
                return self.send_json({"error": "conflict", "version": version, "text": disk_text(p)}, 409)
            text, mode = req["text"], None
            if p.is_file():  # keep an existing file's permissions (a script stays executable) and CRLFs
                mode = p.stat().st_mode & 0o7777
                if "\r" not in text and uses_crlf(p):
                    text = text.replace("\n", "\r\n")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name("." + p.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, p)
            return self.send_json({"version": version_of(p)})
        except (ValueError, KeyError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except OSError as exc:  # e.g. a read-only file
            return self.send_json({"error": f"could not save: {exc.strerror or exc}"}, 500)

    def do_POST(self):
        if self.guard(need_origin=True):
            return
        u = urlparse(self.path)
        try:
            req = self.read_json()
            if u.path == "/api/pdf":
                return self.send_json(export_pdf(req))
            if u.path == "/api/root":
                initial = set_root(req["path"])
                print(f"[root] {ROOT}", flush=True)
                return self.send_json({"root": str(ROOT), "initial": initial, "files": list_files()})
            if u.path in POST_API:  # /api/stat, new, rename, delete, git/discard
                return self.answer(POST_API[u.path], req)
            if u.path in ("/api/ask", "/api/ask/prepare"):
                info = ask_info()
                if not info["available"]:
                    return self.send_json({"error": info["reason"]}, 503)
                if u.path == "/api/ask/prepare":
                    return self.send_json(prepare_ask(req))
                print(f"[ask] {req.get('mode')}: {req.get('instruction', '')[:80]!r}", flush=True)
                return self.send_json(ask_claude(req))
        except subprocess.TimeoutExpired:
            return self.send_json({"error": "Claude timed out"}, 504)
        except RuntimeError as exc:
            return self.send_json({"error": str(exc)}, 502)
        except (ValueError, KeyError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        return self.send_json({"error": "unknown endpoint"}, 404)

    def events(self):
        """Server-sent events: file list changes and per-file version changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        last = None
        try:
            while True:
                files = list_files()
                if files != last:
                    self.wfile.write(f"data: {json.dumps(files)}\n\n".encode())
                    self.wfile.flush()
                    last = files
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(0.6)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- /api/term: hand-written RFC 6455 WebSocket onto the shared TermSession

    def term_socket(self):
        # browsers apply no CORS to WebSockets: do_GET's guard has already insisted on an
        # Origin that matches our own local Host
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        tokens = {t.strip().lower() for t in (self.headers.get("Connection") or "").split(",")}
        if ((self.headers.get("Upgrade") or "").lower() != "websocket" or "upgrade" not in tokens
                or (self.headers.get("Sec-WebSocket-Version") or "").strip() != "13" or not key):
            return self.send_json({"error": "expected a WebSocket upgrade"}, 400)
        info = agent_info()
        if not info["available"]:
            return self.send_json({"error": info["reason"]}, 503)
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.close_connection = True
        self.log_request(101)
        # written by hand: send_response would say HTTP/1.0, and browsers reject a 1.0 101
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        client = WSClient(self.connection)  # wfile is unbuffered: the 101 has gone
        if not TERM.attach(client):
            return
        stop = threading.Event()
        threading.Thread(target=client.keepalive, args=(stop,), daemon=True).start()
        try:
            self.ws_loop(client)
        except WSProtocolError as exc:
            client.close(exc.code)
            self.ws_drain()
        except (OSError, EOFError, ValueError):
            pass
        finally:
            stop.set()
            TERM.detach(client)
            with client.send_lock:
                client.alive = False

    def ws_drain(self):
        """After a close frame for a protocol error: send FIN, then read and discard what the
        browser is still sending (say the rest of an oversized message) until it closes, for
        up to WS_DRAIN. Closing with unread data would reset the connection, which can lose
        the close frame, so the browser would see 1006 instead of our code."""
        seconds, left = WS_DRAIN
        deadline = time.monotonic() + seconds
        try:
            self.connection.shutdown(socket.SHUT_WR)
            while left > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(wait)
                chunk = self.connection.recv(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:  # includes the timeout
            pass

    def ws_read(self, n, client=None):
        """n bytes from the browser, else EOFError. Each 64 KiB of a long payload counts as
        hearing from the browser (`client.last_seen`): it cannot answer a ping mid-frame."""
        data = bytearray()
        while len(data) < n:
            chunk = self.rfile.read(min(n - len(data), 1 << 16))
            if not chunk:
                raise EOFError
            data += chunk
            if client is not None:
                client.last_seen = time.monotonic()
        return bytes(data)

    def ws_loop(self, client):
        parts, part_op, part_len = None, None, 0
        while True:
            b0, b1 = self.ws_read(2)
            client.last_seen = time.monotonic()
            fin, op, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if b0 & 0x70 or not b1 & 0x80:  # no extensions negotiated; clients must mask
                raise WSProtocolError(1002)
            if n == 126:
                n = struct.unpack("!H", self.ws_read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self.ws_read(8))[0]
            if op >= 0x8:
                if not fin or n > 125:
                    raise WSProtocolError(1002)
            elif (part_len if op == OP_CONT else 0) + n > WS_MAX_MESSAGE:
                raise WSProtocolError(1009)
            key = self.ws_read(4)
            payload = ws_unmask(self.ws_read(n, client), key)
            if op == OP_CLOSE:
                if n == 1:  # a status code takes two bytes
                    raise WSProtocolError(1002)
                client.send_frame(ws_frame(OP_CLOSE, payload[:2]))  # echo the status code
                return
            if op == OP_PING:
                client.send_frame(ws_frame(OP_PONG, payload))
            elif op == OP_PONG:
                pass
            elif op == OP_CONT:
                if parts is None:
                    raise WSProtocolError(1002)
                parts.append(payload)
                part_len += n
                if fin:
                    msg, msg_op = b"".join(parts), part_op
                    parts, part_op, part_len = None, None, 0
                    self.ws_message(client, msg_op, msg)
            elif op in (OP_TEXT, OP_BIN):
                if parts is not None:
                    raise WSProtocolError(1002)
                if fin:
                    self.ws_message(client, op, payload)
                else:
                    parts, part_op, part_len = [payload], op, n
            else:
                raise WSProtocolError(1002)

    def ws_message(self, client, op, payload):
        if op == OP_BIN:
            client.writing = True
            try:
                return TERM.write(payload, lambda: client.alive)
            finally:
                client.last_seen = time.monotonic()  # before keepalive() may judge it again
                client.writing = False
        try:
            msg = json.loads(payload.decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "resize":
            try:
                cols, rows = int(msg["cols"]), int(msg["rows"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return
            nudge, client.sized = not client.sized, True
            TERM.resize(cols, rows, nudge)
        elif msg.get("type") == "restart":
            TERM.restart()


INITIAL = None


STOPPING = False


def _interrupt(signum, frame):
    """Stop the server on the first SIGINT / SIGTERM / SIGHUP. Repeats are ignored: `uv run`
    forwards a SIGTERM its group already got, and a second Ctrl+C must not cut the clean-up
    short and orphan the terminal session."""
    global STOPPING
    if not STOPPING:
        STOPPING = True
        raise KeyboardInterrupt


def main():
    global ROOT, INITIAL, AGENT_CMD, ASK_CMD
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".", help="folder or .md file to edit (default: current folder)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--agent", metavar="CMD", default=os.environ.get("MDEDIT_AGENT", DEFAULT_AGENT),
                    help="command run in the side-panel terminal, in the open folder "
                         f"(default: $MDEDIT_AGENT or '{DEFAULT_AGENT}'; 'none' turns the terminal off)")
    ap.add_argument("--ask-agent", metavar="CMD", default=os.environ.get("MDEDIT_ASK_AGENT", DEFAULT_ASK_AGENT),
                    help="how Ask Claude runs `claude -p`: a claude-sandbox-compatible launcher, or "
                         f"'claude' to run it natively (default: $MDEDIT_ASK_AGENT or '{DEFAULT_ASK_AGENT}'; "
                         "'none' turns Ask Claude off)")
    args = ap.parse_args()
    for flag, value in (("--agent", args.agent), ("--ask-agent", args.ask_agent)):
        try:
            cmd = parse_command(value)
        except ValueError as exc:
            ap.error(f"{flag}: {exc}")
        if flag == "--agent":
            AGENT_CMD = cmd
        else:
            ASK_CMD = cmd

    target = Path(args.path).expanduser().resolve()
    if target.suffix.lower() in MD_EXT:
        ROOT, INITIAL = target.parent, target.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {target.stem}\n\n", encoding="utf-8")
    else:
        ROOT = target
        ROOT.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"MD editor on {url}  (root: {ROOT})", flush=True)
    info = agent_info()
    print(f"Terminal: {info['cmd']}" if info["available"] else f"Terminal unavailable: {info['reason']}",
          flush=True)
    info = ask_info()
    print(f"Ask Claude: {info['cmd']} ({info['mode']})" if info["available"]
          else f"Ask Claude unavailable: {info['reason']}", flush=True)
    # stop cleanly on Ctrl+C, SIGTERM, and SIGHUP unless it is ignored (nohup), so the
    # terminal's and running Ask Claude calls' process groups are killed rather than orphaned
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None and signal.getsignal(sig) in (signal.SIG_DFL, signal.default_int_handler):
            signal.signal(sig, _interrupt)
    atexit.register(TERM.kill)
    atexit.register(end_asks)
    atexit.register(end_searches)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        TERM.kill()
        end_asks()
        end_searches()
        server.server_close()


if __name__ == "__main__":
    main()
