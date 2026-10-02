#!/usr/bin/env python3
"""MD Collaborative Editor: a local GitHub-style markdown editor with Claude in the loop.

Serves the editor UI, reads and writes .md files under a root folder, and pushes
on-disk changes to the browser (so edits made by Claude Code in a terminal show
up live). The side panel has a terminal running an interactive Claude Code
session (`uvx claude-sandbox@latest` in the open folder by default) on a PTY,
streamed to the browser over a WebSocket, and md-editor is that session's IDE:
Claude Code connects back to it, sees the editor's selection, and shows its
proposed edits in md-editor as diffs to accept or reject.

    md-editor                     # edit the current folder
    md-editor ~/notes             # edit a folder
    md-editor ~/proj/README.md    # edit one file (its folder becomes the root)
    md-editor --agent claude      # terminal runs plain Claude Code (no sandbox)
    md-editor --agent none        # no terminal
    md-editor --ide-link off      # the session does not connect back to md-editor
"""

import argparse
import atexit
import base64
import errno
import hashlib
import hmac
import json
import itertools
import math
import mimetypes
import os
import posixpath
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
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
IDE_LINK = "auto"  # --ide-link: auto, off, or native/launcher to choose the link's mode

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
# the IDE link's socket in the session's folder (see IdeBridge): never listed, searched or opened
IDE_SOCK_RE = re.compile(r"\.md-editor-ide-[0-9]+\.sock")


def ide_socket(path):
    """Whether a file name (or the last part of a path) is an IDE link socket's."""
    return IDE_SOCK_RE.fullmatch(posixpath.basename(path)) is not None

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
    git-ignored ones only with all=1, flagged. .git and IDE link sockets never appear."""
    rel = q.get("dir", "")
    d = safe_path(rel)
    if not d.is_dir():
        raise ApiError(404, "not a folder")
    base, show_all = lex_rel(rel), q.get("all") == "1"
    entries = []
    with os.scandir(d) as it:
        for e in it:
            if e.name == ".git" or ide_socket(e.name):
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
                if e.name == ".git" or ide_socket(e.name):  # as api_tree
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
    SKIP_DIRS (and IDE link sockets) and does not follow symlinked folders."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        rel = Path(dirpath).relative_to(ROOT).as_posix()
        for f in sorted(filenames):
            if not ide_socket(f):
                yield f if rel == "." else f"{rel}/{f}"


def api_allfiles(q):
    """GET /api/allfiles: every non-ignored file under ROOT, for quick open."""
    files = None
    if ws_git() is not None:
        r = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."], ROOT)
        if r.returncode == 0:
            names = dict.fromkeys(os.fsdecode(n) for n in r.stdout.split(b"\0") if n)  # unmerged: once
            # drops tracked files deleted from the work tree, and submodules
            files = sorted(n for n in names if os.path.isfile(os.path.join(ROOT, n)) and not ide_socket(n))
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
    untracked = [n for n in r.stdout.split(b"\0")
                 if n and not n.endswith(b"/") and not ide_socket(os.fsdecode(n))] if r.returncode == 0 else []
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
        if status == "U" and ide_socket(p):  # git skips sockets, but should one ever be listed
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
    """--agent value → argv list, or None when the terminal is turned off."""
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
# the terminal's transcript, so /resume can't find it). A PTY has its own size. An IDE's
# terminal (VS Code's) sets CLAUDE_CODE_SSE_PORT, which would connect the session to that
# IDE: the IDE link sets its own.
CHILD_ENV_DROP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                  "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_CODE_SSE_PORT",
                  "COLUMNS", "LINES")


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
        self.started = time.monotonic()
        self.last_out = None           # when it last wrote anything
        self.prompted = False          # it has drawn Claude Code's prompt glyph (so Claude Code is up)


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

    Each spawned agent gets an IdeBridge (`bridge`, None when the IDE link is off), which
    shares `lock`; it goes when the agent exits or is replaced.
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
        self.bridge = None       # the live session's IdeBridge, if it has one
        self.ide_reason = None   # why it has none

    # -- clients (call with self.lock held unless noted)

    def status(self):
        """The session's state, and its IDE link's: `ide` is off, waiting (for Claude Code to
        connect) or connected, `ide_reason` says why it is off (when that is not just that no
        session is running), and `diffs` lists the proposed edits still waiting for the user."""
        b = self.bridge
        return {"type": "status", "id": self.id, "state": self.state, "cmd": agent_display() or "",
                "cwd": self.cwd, "code": self.code, "message": self.message,
                "ide": b.state if b else "off", "ide_reason": None if b else self.ide_reason,
                "diffs": list(b.diffs) if b else []}

    def _broadcast_json(self, obj):
        self._broadcast(ws_frame(OP_TEXT, json.dumps(obj).encode()))

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
            # the proposed edits still waiting for an answer, for a page that was reloaded
            for msg in self.bridge.diff_messages() if ok and self.bridge else ():
                ok = ok and client.send_frame(ws_frame(OP_TEXT, json.dumps(msg).encode()))
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
        # the IDE link listens and writes its lock file before the agent starts: Claude Code
        # looks for it only for 30 s
        args, env, self.bridge, self.ide_reason = [], agent_env(), None, None
        mode = ide_mode() if IDE_LINK != "off" else None
        if IDE_LINK == "off":
            self.ide_reason = "The IDE link is turned off (--ide-link off)."
        elif mode is None:
            self.ide_reason = (f"{agent_display()} is neither Claude Code nor claude-sandbox; "
                               "--ide-link native or launcher links it anyway.")
        else:
            try:
                self.bridge = IdeBridge(self, self.cwd, mode)
                args, env = self.bridge.args, dict(env, **self.bridge.env)
            except IdeUnavailable as exc:
                self.ide_reason = str(exc)
                print(f"[ide] no IDE link: {exc}", flush=True)
        try:
            proc, master = spawn_pty(AGENT_CMD + args, self.cwd, env, cols, rows)
        except (OSError, subprocess.SubprocessError) as exc:
            self._close_bridge()
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
        self._close_bridge()
        return rec

    def _close_bridge(self):
        b, self.bridge = self.bridge, None
        if b is not None:
            b.close()

    def _read_loop(self, rec):
        quiet = None  # since when the agent has been gone and the PTY silent
        held = False  # the agent exited, but something it started still holds the PTY
        while True:
            try:
                data = os.read(rec.fd, 65536)
            except BlockingIOError:
                # exited: a second of quiet ends it (a straggler may still hold the PTY open)
                if rec.proc.poll() is not None:
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
                rec.last_out = time.monotonic()
                if self.cur is rec:
                    self._append(data)
                    # the glyph may be split between two reads
                    if not rec.prompted and PROMPT_GLYPH in self.scrollback[-(len(data) + len(PROMPT_GLYPH) - 1):]:
                        rec.prompted = True
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
                self._close_bridge()
                self._broadcast_status()

    def write(self, data, alive=lambda: True, rec=None):
        """Type `data` into the agent (`rec`, if given, and only while it is the live one).
        While it is not reading, wait (holding wlock, so other writes queue behind) until it
        does, the session ends or is replaced, or `alive()` turns false: the browser that sent
        it has gone (see WSClient.keepalive). True if all of it was written."""
        with self.lock:
            if rec is None:
                rec = self.cur
            elif rec is not self.cur:
                return False
        if rec is None:
            return False
        with rec.wlock:  # not under self.lock: a blocked write must not stall the output
            view = memoryview(data)
            while view and not rec.closed and not rec.retired and alive():
                try:
                    view = view[os.write(rec.fd, view):]
                except BlockingIOError:  # the agent is not reading: wait for room
                    wait_fd(rec.fd, write=True, timeout=0.5)
                except OSError:
                    return False
            return not view

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

    # -- the editor talking to the session (messages from a browser; see IdeBridge)

    def _target(self, msg):
        """The file or folder a browser message names (`path`, ROOT-relative) as (its absolute
        path, its real path), or None."""
        rel = msg.get("path")
        if not isinstance(rel, str) or "\0" in rel:
            return None
        try:
            lex = os.path.normpath(os.path.join(str(ROOT), lex_rel(rel)))
            return lex, os.path.realpath(lex)
        except (ValueError, OSError):
            return None

    def _ide(self):
        """The live session's process and IdeBridge (None when the link is off), or a reason
        why the editor cannot reach the session."""
        with self.lock:
            if self.cur is None or self.state != "running":
                return None, None, "The Claude session is not running."
            return self.cur, self.bridge, None

    def selection(self, msg):
        """{"type": "selection", path, start, end, text}: the editor's selection, for Claude
        Code (selection_changed) when it is in a file inside the session's folder. Lines and
        characters count from 0, characters in UTF-16 code units (CodeMirror's ch). Anywhere
        else (another folder opened since the session started, a link out of it), Claude is
        told to forget the last one, which it would attach to the next prompt."""
        with self.lock:
            bridge = self.bridge
        if bridge is None:
            return
        t = self._target(msg)
        if t is None or not os.path.isfile(t[1]) or not inside(t[1], bridge.cwd):
            bridge.clear_selection()
            return
        start, end = ide_range(msg)
        text = msg.get("text")
        bridge.select(t[1], start, end, text if isinstance(text, str) else "")

    def prompt_state(self):
        """What the agent last drew of Claude Code's prompt: "input" (its input box), "choice" (a
        menu, such as a permission prompt or the folder-trust question, where Enter picks the
        marked answer), or None (nothing yet: starting, or not Claude Code). Claude Code marks
        both with ❯, and the last one it drew is the one on screen."""
        with self.lock:
            sb = self.scrollback
            i = sb.rfind(PROMPT_GLYPH)
            if i < 0:
                return None
            return "choice" if CHOICE_RE.match(sb, i + len(PROMPT_GLYPH)) else "input"

    def _answer_pending(self, bridge):
        """Why an ask must not press Enter now, or None: Claude is waiting for the user to answer
        it, and Enter would answer it (choosing "Yes")."""
        if bridge is not None:
            with self.lock:
                files = [d["file"] for d in bridge.diffs.values()]
            if files:
                return (f"Claude is waiting for your answer to its proposed change to {os.path.basename(files[0])}: "
                        "accept or reject it first.")
        if self.prompt_state() == "choice":
            return "Claude Code is asking you something in the terminal: answer it there first."
        return None

    def _wait_ready(self, rec, alive, waiting):
        """An ask in a session that has only just started waits until Claude Code can take it:
        it keeps text typed before its prompt is drawn, but drops the Enter. Ready once it has
        drawn its prompt (or a menu, which the ask then refuses to answer).

        For Claude Code (the agent is claude or claude-sandbox: ide_mode) nothing else will do:
        claude-sandbox can be silent for long stretches while it makes its container, and a first
        image pull takes minutes. An ask waits for it at most START_WAIT, then fails with nothing
        typed. An agent that is not Claude Code never draws the prompt: it is ready once its
        output has been quiet for START_QUIET, or START_WAIT after it started.

        `waiting()` is called once if it has waited a second. Returns None when ready, else why
        not (also when the session ended or was replaced, or the browser went, meanwhile)."""
        claude = ide_mode() is not None
        told, since = False, time.monotonic()
        while True:
            with self.lock:
                if self.cur is not rec:
                    return "The Claude session ended before it could take the question."
                last, prompted = rec.last_out, rec.prompted
            now = time.monotonic()
            if prompted:
                return None
            if claude:
                if now - since >= START_WAIT:
                    return ("Claude Code has not started yet (its prompt is not up), so the question was not sent: "
                            "ask again once it is.")
            elif now - rec.started >= START_WAIT or (last is not None and now - last >= START_QUIET):
                return None
            if not alive():
                return "The Claude session ended before it could take the question."
            if not told and now - since >= 1:
                told = True
                waiting()
            time.sleep(0.05)

    def ask(self, msg, alive=lambda: True, waiting=lambda: None):
        """{"type": "ask", text, path?, start?, end?}: type the question into the session and
        press Enter. With the IDE link up, Claude is first sent the selection (a file and no
        range: the file alone), then a ping, whose answer means it has handled the selection
        (it answers in order); otherwise the question starts with an @-mention of the lines.
        A question about a whole file starts with an @-mention of it either way. Nothing is
        typed while Claude waits for an answer (Enter would give it), and a session that is
        still starting is waited for (`waiting()` is called then).
        Returns the answer for the browser: {ok, via: "ide" | "typed"} or {ok: false, error}."""
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "There is no question to send."}
        rec, bridge, why = self._ide()
        if why:
            return {"ok": False, "error": why}
        why = self._wait_ready(rec, alive, waiting)
        if why:
            return {"ok": False, "error": why}
        why = self._answer_pending(bridge)
        if why:
            return {"ok": False, "error": why}
        t = self._target(msg) if msg.get("path") else None
        start, end = ide_range(msg)
        via = "typed"
        if bridge is not None and bridge.is_connected():
            ok = t is not None and os.path.isfile(t[1]) and inside(t[1], bridge.cwd)
            sel = bridge.selection_text(t[1], start, end) if ok else None
            if sel is not None:
                bridge.select(t[1], start, end, sel)
                if bridge.ping():
                    time.sleep(IDE_SETTLE)  # Claude stores the selection a moment after handling it
                    via = "ide"
            elif bridge.clear_selection():  # a file elsewhere: Claude must not attach the last selection
                bridge.ping()
        if t is not None and (via == "typed" or start == end):
            # typed, the reference carries the lines; a whole file is named either way, as the
            # empty selection over the link only hints at it
            text = typed_ref(t[0], self.cwd, line_span(start, end)) + " " + text
        if not self.write(paste_bytes(text), alive, rec):
            return {"ok": False, "error": "The Claude session did not take the question."}
        time.sleep(IDE_ENTER_DELAY)  # Enter on its own, after the paste
        why = self._answer_pending(bridge)  # Claude asked something meanwhile
        if why:
            return {"ok": False, "error": "The question was typed but not sent. " + why}
        if not self.write(b"\r", alive, rec):
            return {"ok": False, "error": "The Claude session did not take the question."}
        return {"ok": True, "via": via}

    def mention(self, msg, alive=lambda: True):
        """{"type": "mention", path, start?, end?}: put an @-mention of a file (and lines) or
        folder into Claude's prompt, without pressing Enter. With the IDE link up Claude Code
        inserts it (at_mentioned), with the path relative to its folder; otherwise it is typed."""
        rec, bridge, why = self._ide()
        if why:
            return {"ok": False, "error": why}
        t = self._target(msg)
        if t is None:
            return {"ok": False, "error": "There is no such file."}
        start, end = ide_range(msg)
        span = line_span(start, end)
        # typed: the session's folder itself (Claude Code takes no "@." as a mention), and a path
        # with whitespace, which Claude Code inserts unquoted, so its mention would end there
        if bridge is not None and inside(t[0], bridge.cwd) and t[0] != bridge.cwd \
                and not re.search(r"\s", t[0][len(bridge.cwd):]):
            params = {"filePath": t[0]}
            if span:
                params.update(lineStart=span[0] - 1, lineEnd=span[1] - 1)
            if bridge.notify("at_mentioned", params):
                return {"ok": True, "via": "ide"}
        if not self.write(paste_bytes(typed_ref(t[0], self.cwd, span) + " "), alive, rec):
            return {"ok": False, "error": "The Claude session did not take the text."}
        return {"ok": True, "via": "typed"}

    def diff_decision(self, client, msg):
        """{"type": "diff-decision", id, accept, contents?}: the user's answer to a proposed
        edit. A diff that is no longer waiting is closed in that browser."""
        with self.lock:
            bridge = self.bridge
        did = msg.get("id")
        if bridge is None or not bridge.decide(did, msg.get("accept") is True, msg.get("contents")):
            client.send_frame(ws_frame(OP_TEXT, json.dumps({"type": "diff-close", "id": did}).encode()))


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


class WSReader:
    """The reading side of a server WebSocket, for a request handler (its rfile and
    connection) whose client has upgraded: /api/term's browsers and the IDE link's Claude."""

    def ws_drain(self):
        """After a close frame for a protocol error: send FIN, then read and discard what the
        client is still sending (say the rest of an oversized message) until it closes, for
        up to WS_DRAIN. Closing with unread data would reset the connection, which can lose
        the close frame, so the client would see 1006 instead of our code."""
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
        """n bytes from the client, else EOFError. Each 64 KiB of a long payload counts as
        hearing from the client (`client.last_seen`): it cannot answer a ping mid-frame."""
        data = bytearray()
        while len(data) < n:
            chunk = self.rfile.read(min(n - len(data), 1 << 16))
            if not chunk:
                raise EOFError
            data += chunk
            if client is not None:
                client.last_seen = time.monotonic()
        return bytes(data)

    def ws_serve(self, client, max_message, on_message):
        """Read frames until the client closes: answer its pings, echo its close, and pass each
        message (its fragments joined) to on_message(opcode, payload). Raises WSProtocolError
        (with the close code to send) for a frame the protocol forbids or a message over
        max_message bytes, and EOFError or OSError when the connection drops."""
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
            elif (part_len if op == OP_CONT else 0) + n > max_message:
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
                    on_message(msg_op, msg)
            elif op in (OP_TEXT, OP_BIN):
                if parts is not None:
                    raise WSProtocolError(1002)
                if fin:
                    on_message(op, payload)
                else:
                    parts, part_op, part_len = [payload], op, n
            else:
                raise WSProtocolError(1002)


# ---------------------------------------------------------------- ide link
#
# md-editor is the IDE of the terminal's Claude Code session, as VS Code is with Claude Code's
# extension. Claude Code finds a lock file, `<port>.lock` in its config folder's ide/, holding a
# token; connects to ws://127.0.0.1:<port> with that token; and speaks MCP (JSON-RPC 2.0, one
# message per text frame) to it. md-editor tells Claude the editor's selection
# (`selection_changed`), which is attached to the next prompt, and puts @-mentions into its
# prompt (`at_mentioned`); when Claude would ask in the terminal before an edit, it also shows
# the edit in md-editor (`openDiff`) and the user accepts or rejects it there. No message can
# submit a prompt, so an ask is typed into the terminal.
#
# Each spawned session gets an IdeBridge: a token, a listener and a lock file, gone with it.
# - Native mode (the agent is `claude` itself): a TCP listener on 127.0.0.1, its port in the
#   agent's environment (CLAUDE_CODE_SSE_PORT, which makes Claude connect to that lock's IDE),
#   the lock in ${CLAUDE_CONFIG_DIR:-~/.claude}/ide with md-editor's pid.
# - Launcher mode (any other agent: claude-sandbox): Claude runs in a jail with a loopback of
#   its own and none of md-editor's environment. md-editor listens on a Unix socket in the
#   session's folder, which the jail mounts at the same path, and writes the lock into the
#   jail's ~/.claude/ide (claude-sandbox's shared config folder on this side) with "pid": 1,
#   which is alive in the jail's pid namespace, so Claude's stale-lock sweep keeps it. It
#   appends `--settings` to the agent's arguments, which the launcher hands on to Claude:
#   CLAUDE_CODE_SSE_PORT, and a SessionStart hook that starts socat in the jail to relay its
#   127.0.0.1:<port> to the socket.
#
# Only Claude can use the link: it needs the token, and a browser cannot send the token's
# header. Whoever connects can only show the user diffs and get text back: the link never
# writes a file (on Accept, Claude writes its edit itself), runs anything, or reads a file
# outside the session's folder.

try:
    from . import __version__
except ImportError:  # run as a script
    __version__ = "dev"

IDE_MAX_MESSAGE = 16 << 20   # bytes in one message: openDiff carries whole files
IDE_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")  # MCP versions
IDE_PORTS = (20000, 60000)   # launcher mode's port in the jail, picked at random from these
SUN_PATH_MAX = 107           # bytes in a Unix socket's path
IDE_PING_TIMEOUT = 2         # seconds an ask waits for Claude to answer its ping
IDE_SETTLE = 0.05            # seconds for Claude to store the selection once it has answered
IDE_ENTER_DELAY = 0.1        # seconds between pasting a question and pressing Enter
IDE_LOCK_CHECK = 5           # seconds between checks that the lock file is still there
IDE_RESEND = 0.5             # seconds after ide_connected that the selection is sent again
IDE_DIFFS_MAX = 16           # proposed edits waiting for the user at once
# The page answers a proposal with the whole (edited) file as JSON in one /api/term message,
# which may be at most WS_MAX_MESSAGE (4 MiB): a proposal bigger than this as JSON is not shown,
# so there is room for the user's edits, and an Accept always fits
IDE_DIFF_MAX = 3 << 20
IDE_CONNS_MAX = 8            # connections to the listener at once (Claude needs one)
IDE_HANDSHAKE = env_seconds("MDEDIT_IDE_HANDSHAKE", 10)  # seconds a connection has to send its upgrade
# Proposed edits are numbered `<this process's salt>-<n>`: a page that outlives an md-editor (it
# reconnects to the next one on the same port) must never take a new proposal for an old one.
DIFF_SALT = secrets.token_hex(4)
DIFF_IDS = itertools.count(1)
# An ask that comes while the agent is starting waits until Claude Code can take it (Claude Code
# keeps text typed before its prompt is up, but drops the Enter): until it draws its prompt, for
# at most START_WAIT (then it fails); an agent that is not Claude Code, until its output has been
# quiet for START_QUIET, for at most START_WAIT after it was spawned (_wait_ready).
START_WAIT = env_seconds("MDEDIT_START_WAIT", 20)
START_QUIET = env_seconds("MDEDIT_START_QUIET", 2)
PROMPT_GLYPH = "❯".encode()  # Claude Code's prompt, and the cursor of its menus
# what follows the glyph when it marks a menu's choice ("❯ 1. Yes"); its input box has a no-break
# space after it ("❯\u00a0"), so what the user types there ("1. …") is not taken for a menu
CHOICE_RE = re.compile(rb"(?:\x1b\[[0-?]*[ -/]*[@-~]|[ \t\r\n])*[0-9]+\.\s")
# text typed into the agent as one bracketed paste must not hold ESC (ESC[201~ would end the
# paste early and the rest would run as keystrokes) or other control characters
PASTE_DROP_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# what Claude Code shows the model (as "## ide")
IDE_INSTRUCTIONS = """\
You are attached to md-editor, a local GitHub-style markdown editor (rendered preview, file tree, \
source control) that the user has open beside this terminal. When the user has text selected \
there, the selection is attached to their prompt; questions they send from the editor's ask bar \
arrive as ordinary prompts. Files are plain files on disk: the editor shows your edits as soon \
as they are written, and saves the user's edits to disk.
- When editing markdown, keep its structure (headings, lists, links, emphasis, maths, code, \
tables) unless asked to change it, and keep code valid in the file's language.
- Use British English spelling unless the document clearly uses another convention.
- If asked to use a skill, invoke it with the Skill tool first."""

IDE_TOOLS = [
    {"name": "openDiff",
     "description": "Show a proposed change to a file in md-editor as a diff, and wait for the user to accept or "
                    "reject it",
     "inputSchema": {"type": "object", "properties": {
         "old_file_path": {"type": "string", "description": "The file's absolute path"},
         "new_file_path": {"type": "string", "description": "The same path"},
         "new_file_contents": {"type": "string", "description": "The whole file as proposed"},
         "tab_name": {"type": "string", "description": "The diff's title"}},
         "required": ["old_file_path", "new_file_contents", "tab_name"]}},
    {"name": "close_tab", "description": "Close a diff opened with openDiff",
     "inputSchema": {"type": "object", "properties": {"tab_name": {"type": "string"}}, "required": ["tab_name"]}},
    {"name": "closeAllDiffTabs", "description": "Close every diff opened with openDiff",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "getDiagnostics",
     "description": "Diagnostics for a file URI, or all open files. md-editor reports none, so the result is "
                    "always an empty list.",
     "inputSchema": {"type": "object", "properties": {"uri": {"type": "string"}}}},
]


class IdeUnavailable(Exception):
    """Why a session gets no IDE link."""


IDE_LAUNCHERS = ("claude-sandbox", "claude-container")   # `uvx claude-sandbox@latest`, or the script


def ide_mode():
    """native: the agent is Claude Code itself; launcher: it runs Claude Code in claude-sandbox;
    None: neither, so no link (appending --settings would break, say, `--agent bash`).
    --ide-link native|launcher chooses for an agent md-editor does not recognise."""
    if IDE_LINK in ("native", "launcher"):
        return IDE_LINK
    if os.path.basename(AGENT_CMD[0]) == "claude":
        return "native"
    if any(os.path.basename(a).split("@")[0] in IDE_LAUNCHERS for a in AGENT_CMD):
        return "launcher"
    return None


def ide_lock_dir(mode):
    """Where Claude Code looks for lock files: natively its config folder's ide/; in launcher
    mode the jail's ~/.claude/ide, which claude-sandbox shares from its config folder here."""
    if mode == "native":
        return os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), "ide")
    shared = os.environ.get("CLAUDE_SANDBOX_SHARED_CONFIG") or "~/.config/terminal-config"
    return os.path.join(os.path.expanduser(shared), ".claude", "ide")


# In launcher mode the lock folder is the jail's ~/.claude/ide, which the sandboxed agent can
# write: lock files are made, read and deleted by name in a folder opened without following a
# symlink (open_lock_dir), so a link planted there cannot point md-editor at other files.

def open_lock_dir(d):
    """Make lock folder d (0700) if need be and open it; IdeUnavailable if it is a symlink or
    not a folder."""
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        return os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise IdeUnavailable(f"Cannot use {d} for the IDE link's lock file: {exc.strerror or exc}.")


def write_lock(name, data, dir_fd):
    """Write lock file `name` (mode 0600) whole, and never over an existing one (FileExistsError):
    it is linked into place, so Claude never reads it half written."""
    tmp = f".{name}.{secrets.token_hex(4)}.tmp"  # not *.lock: Claude reads only those
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileExistsError:
            raise
        except OSError:  # a file system without hard links
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
    finally:
        remove_file(tmp, dir_fd)


def remove_file(path, dir_fd=None):
    try:
        os.unlink(path, dir_fd=dir_fd)
    except OSError:
        pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OverflowError, ValueError):  # no process can have it (a lock may say anything)
        return False
    except OSError:  # EPERM: someone else's
        pass
    return True


def read_lock(name, dir_fd=None):
    """A lock file's JSON object, or None. It is read without blocking or following a link (the
    agent could make a lock a FIFO, or a link)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            data = json.loads(os.read(fd, 1 << 16).decode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, ValueError, RecursionError):  # RecursionError: nested too deep to parse
        return None
    return data if isinstance(data, dict) else None


def sweep_ide_locks():
    """At start: delete the lock files that md-editors no longer running left behind (killed
    before they could), and their sockets. Claude Code keeps a launcher mode lock ("pid": 1)
    for ever.

    In launcher mode the lock folder is the sandboxed agents' to write, so a lock may lie about
    its md-editor and its folder (naming another project's live socket): a socket is deleted
    only if nothing answers on it, and its lock is then left alone too."""
    if os.name != "posix":  # os.kill(pid, 0) would end the process on Windows
        return
    for mode in ("native", "launcher"):
        d = ide_lock_dir(mode)
        try:
            dfd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            try:
                names = os.listdir(dfd)
            except OSError:
                names = []
            for name in names:
                m = re.fullmatch(r"([0-9]+)\.lock", name)
                if not m:
                    continue
                try:  # whatever one lock holds, md-editor starts
                    _sweep_lock(d, dfd, name, m.group(1))
                except Exception as exc:
                    print(f"[ide] could not check lock file {os.path.join(d, name)}: {type(exc).__name__}", flush=True)
        finally:
            os.close(dfd)


def _sweep_lock(d, dfd, name, port):
    """sweep_ide_locks for one lock file `name` in folder d (open as dfd)."""
    data = read_lock(name, dfd)
    if data is None or data.get("ideName") != "md-editor":
        return
    pid = data.get("mdEditorHostPid")
    if type(pid) is not int or pid <= 0 or (pid != os.getpid() and pid_alive(pid)):
        return
    folders = data.get("workspaceFolders")
    sock = None
    if isinstance(folders, list) and folders and isinstance(folders[0], str) and os.path.isabs(folders[0]):
        sock = os.path.join(folders[0], f".md-editor-ide-{port}.sock")
        try:
            if not stat.S_ISSOCK(os.lstat(sock).st_mode):
                sock = None
        except (OSError, ValueError):
            sock = None
    if sock and socket_alive(sock):  # someone's link is live there: not stale, whatever it says
        return
    remove_file(name, dfd)
    print(f"[ide] removed a stale lock file: {os.path.join(d, name)}", flush=True)
    try:
        if sock and stat.S_ISSOCK(os.lstat(sock).st_mode):
            os.unlink(sock)
    except OSError:
        pass


def inside(path, folder):
    """Whether path is folder or inside it (both absolute and normalised)."""
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def read_inside(path, folder):
    """The text of file `path` (a real path inside real path `folder`) as disk_text gives it, as
    (text, exists): ("", False) when there is no such file, (None, True) when it is not text.

    The folder is the agent's, which can swap a file that was just checked, or a folder above
    it, for a link to a file elsewhere: the file is opened one name at a time from `folder`, never
    following a link (a link found now is refused), and only a regular file is read."""
    rel = os.path.relpath(path, folder)
    parts = rel.split(os.sep)
    if rel == os.curdir or parts[0] == os.pardir:
        return None, True
    nofollow, odir = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0)
    if not nofollow or os.open not in os.supports_dir_fd:  # no safe way here: by name
        text = disk_text(Path(path))
        return text, text != "" or os.path.lexists(path)
    try:
        fd = os.open(folder, os.O_RDONLY | odir)
    except OSError:
        return None, True
    try:
        for name in parts[:-1]:
            sub = os.open(name, os.O_RDONLY | odir | nofollow, dir_fd=fd)
            os.close(fd)
            fd = sub
        if not stat.S_ISREG(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
            return None, True
        ffd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | nofollow, dir_fd=fd)
    except FileNotFoundError:
        return "", False
    except OSError:  # a link (ELOOP), or a file where a folder should be
        return None, True
    finally:
        os.close(fd)
    with os.fdopen(ffd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > TEXT_MAX:
            return None, True
        data = f.read(TEXT_MAX + 1)
    return (decode_text(data) if len(data) <= TEXT_MAX else None), True


def socket_alive(path):
    """Whether something answers on Unix socket `path` (a socket a crashed process left
    refuses the connection)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except socket.timeout:  # there, but busy
        return True
    except OSError:
        return False
    finally:
        s.close()


def socat_path(path):
    """A path as one parameter of a socat address: its special characters escaped."""
    return re.sub(r"""([\\:,!'"()\[\]{} ])""", r"\\\1", path)


def utf16_index(s, units):
    """The index in s of the character `units` UTF-16 code units in (CodeMirror counts so)."""
    if s.isascii():
        return min(units, len(s))
    n = 0
    for i, ch in enumerate(s):
        if n >= units:
            return i
        n += 2 if ord(ch) > 0xFFFF else 1
    return len(s)


def text_range(text, start, end):
    """The text between two {line, character} positions."""
    lines = text.split("\n")

    def offset(pos):
        line = min(pos["line"], len(lines) - 1)
        return sum(len(x) + 1 for x in lines[:line]) + utf16_index(lines[line], pos["character"])
    return text[offset(start):offset(end)]


def ide_position(p):
    """A {line, character} position from the browser (both from 0), or None."""
    if not isinstance(p, dict):
        return None
    try:
        line, ch = int(p.get("line")), int(p.get("character"))
    except (TypeError, ValueError, OverflowError):
        return None
    if line < 0 or ch < 0:
        return None
    return {"line": min(line, 1 << 30), "character": min(ch, 1 << 30)}


def ide_range(msg):
    """A browser message's (start, end), in order; nothing (or half of it) is the empty range
    at the start of the file."""
    start, end = ide_position(msg.get("start")), ide_position(msg.get("end"))
    if start is None or end is None:
        start = end = {"line": 0, "character": 0}
    if (end["line"], end["character"]) < (start["line"], start["character"]):
        start, end = end, start
    return start, end


def line_span(start, end):
    """The first and last lines (from 1) a range covers, as Claude Code counts them (a range
    that ends at the start of a line does not cover it), or None for an empty range."""
    if start == end:
        return None
    a, b = start["line"] + 1, end["line"] + 1
    if end["character"] == 0 and b > a:
        b -= 1
    return a, b


def typed_ref(path, cwd, span=None):
    """The @-mention Send to Claude terminal types: `@path#La-b`, the path relative to the
    session's folder when it is inside it (absolute otherwise), in quotes if it has spaces."""
    base = (cwd or "").rstrip("/")
    p = path[len(base) + 1:] if base and path.startswith(base + "/") else path
    ref = "@" + (f'"{p}"' if re.search(r"\s", p) else p)
    if span:
        ref += f"#L{span[0]}" if span[0] == span[1] else f"#L{span[0]}-{span[1]}"
    return ref


def paste_bytes(text):
    """text as one bracketed paste, so Claude Code takes it literally: CRs as LFs, and no
    control characters (see PASTE_DROP_RE)."""
    text = PASTE_DROP_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    try:
        data = text.encode("utf-8", "surrogateescape")  # a file name that is not UTF-8 as it is
    except UnicodeEncodeError:
        data = text.encode("utf-8", "replace")
    return b"\x1b[200~" + data + b"\x1b[201~"


def json_size(text):
    """The bytes of str `text` as a JSON string, as a browser's JSON.stringify writes it: UTF-8,
    with quotes, backslashes, control characters and lone surrogates escaped."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8", "backslashreplace"))


def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def tool_text(*texts, error=False):
    out = {"content": [{"type": "text", "text": t} for t in texts]}
    if error:
        out["isError"] = True
    return out


class IdeConn:
    """One connection from Claude Code."""

    def __init__(self, client):
        self.client = client
        self.ready = False  # it has said it is connected (ide_connected)

    def send(self, msg):
        return self.client.send_frame(ws_frame(OP_TEXT, json.dumps(msg).encode()))

    def drop(self):
        with self.client.send_lock:
            self.client._drop_locked()


class IdeBridge:
    """Claude Code's IDE link for one session (see above). Its state is guarded by the session's
    lock, which it holds while it tells the browsers about it; it sends to Claude without it.

    `state`: waiting (for Claude to connect), connected, or off (closed). `diffs`: the proposed
    edits waiting for the user, by id, each from one connection and answering one request."""

    def __init__(self, session, cwd, mode):
        self.session, self.lock = session, session.lock
        self.cwd, self.mode = os.path.realpath(cwd), mode
        self.token = secrets.token_hex(32)
        self.state = "waiting"
        self.conn = None           # the IdeConn Claude is using
        self.selection = None      # the latest selection_changed params, sent again on connecting
        self.diffs = {}
        self.waiters = {}          # our request id → [Event, IdeConn, its reply]
        self.req_ids = itertools.count(1)
        self.args, self.env = [], {}
        self.sock_path = None
        self.file_lock = threading.Lock()  # the lock file: written again, or deleted
        self.next_check = time.monotonic() + IDE_LOCK_CHECK
        self._listen()

    # -- the listener and the lock file

    def _listen(self):
        native = self.mode == "native"
        d = ide_lock_dir(self.mode)
        if not native:
            if not hasattr(socket, "AF_UNIX"):
                raise IdeUnavailable("The IDE link to claude-sandbox needs Unix sockets, which this platform lacks.")
            if not os.path.isdir(os.path.dirname(d)):
                raise IdeUnavailable(f"{os.path.dirname(d)} does not exist: claude-sandbox has not run yet, or keeps "
                                     "its configuration elsewhere (set CLAUDE_SANDBOX_SHARED_CONFIG).")
        dfd = open_lock_dir(d)
        try:
            self._bind(native, d, dfd)
        except BaseException:
            os.close(dfd)
            raise
        self.dir_fd = dfd
        if native:
            self.env = {"CLAUDE_CODE_SSE_PORT": str(self.port)}
        else:
            relay = (f"socat TCP4-LISTEN:{self.port},bind=127.0.0.1,reuseaddr,fork "
                     f"UNIX-CONNECT:{shlex.quote(socat_path(self.sock_path))}")
            settings = {"env": {"CLAUDE_CODE_SSE_PORT": str(self.port)},
                        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command":
                                  f"(setsid {relay} </dev/null >/dev/null 2>&1 &)"}]}]}}
            self.args = ["--settings", json.dumps(settings)]
        self.server.bridge = self
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
        where = self.sock_path or f"127.0.0.1:{self.port}"
        print(f"[ide] waiting for Claude Code on {where} (lock file {self.lock_path})", flush=True)

    def _bind(self, native, d, dfd):
        """Listen, on a port whose lock file name is free in lock folder d (open as dfd), and
        write the lock file."""
        for _ in range(20):
            server = sock = None
            try:
                if native:
                    server = IdeTCPServer(("127.0.0.1", 0), IdeHandler)
                    port = server.server_address[1]
                else:
                    port = IDE_PORTS[0] + secrets.randbelow(IDE_PORTS[1] - IDE_PORTS[0])
                    sock = os.path.join(self.cwd, f".md-editor-ide-{port}.sock")
                    if len(os.fsencode(sock)) > SUN_PATH_MAX:
                        raise IdeUnavailable(f"The IDE link's socket would be {sock}, which is longer than a Unix "
                                             f"socket's path can be ({SUN_PATH_MAX} bytes).")
                    if os.path.lexists(sock):
                        continue
                    server = IdeUnixServer(sock, IdeHandler)
            except OSError as exc:
                if not native and exc.errno == errno.EADDRINUSE:  # made since lexists: another port
                    continue
                raise IdeUnavailable(f"The IDE link cannot listen: {exc.strerror or exc}.")
            name = f"{port}.lock"
            data = {"pid": os.getpid() if native else 1, "workspaceFolders": [self.cwd], "ideName": "md-editor",
                    "transport": "ws", "authToken": self.token, "mdEditorHostPid": os.getpid()}
            try:
                write_lock(name, data, dfd)
            except FileExistsError:  # every session on this machine shares the folder
                server.server_close()
                if sock:
                    remove_file(sock)
                continue
            except OSError as exc:
                server.server_close()
                if sock:
                    remove_file(sock)
                raise IdeUnavailable(f"Cannot write {os.path.join(d, name)}: {exc.strerror or exc}.")
            self.port, self.server, self.sock_path = port, server, sock
            self.lock_name, self.lock_path, self.lock_data = name, os.path.join(d, name), data
            return
        raise IdeUnavailable("The IDE link found no free port.")

    def check_lock(self):
        """Every IDE_LOCK_CHECK seconds (from the listener's thread): write the lock file again
        if something deleted it, such as a Claude Code on this machine whose config folder is
        claude-sandbox's, which takes "pid": 1 for a process that has gone."""
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + IDE_LOCK_CHECK
        with self.file_lock:
            try:
                if self.state == "off" or os.stat(self.lock_name, dir_fd=self.dir_fd, follow_symlinks=False):
                    return
            except FileNotFoundError:
                pass
            except OSError:
                return
            try:
                write_lock(self.lock_name, self.lock_data, self.dir_fd)
                print(f"[ide] the lock file {self.lock_path} had gone: written again", flush=True)
            except OSError:
                pass

    def own_lock(self):
        """Whether the lock file is still this link's (not another IDE's that took the port
        after something deleted ours)."""
        data = read_lock(self.lock_name, self.dir_fd)
        return data is not None and data.get("authToken") == self.token

    def close(self):
        """End the link: the session has ended or is being replaced. Claude's waiting diffs
        are answered as rejected and closed in the browsers, the lock file and the socket are
        deleted, and the listener stops. Safe to call more than once."""
        with self.lock:
            if self.state == "off":
                return
            self.state = "off"
            conn, self.conn = self.conn, None
            diffs = list(self.diffs.values())
            self.diffs.clear()
            for d in diffs:
                self.session._broadcast_json({"type": "diff-close", "id": d["id"]})
            waiters = list(self.waiters.values())
        for w in waiters:
            w[0].set()
        with self.file_lock:
            if self.own_lock():
                remove_file(self.lock_name, self.dir_fd)
            os.close(self.dir_fd)
            if self.sock_path:
                remove_file(self.sock_path)
        print("[ide] closed", flush=True)
        threading.Thread(target=self._stop, args=(conn, diffs), daemon=True).start()

    def _stop(self, conn, diffs):
        if conn is not None:
            for d in diffs:
                if d["conn"] is conn:
                    conn.send(rpc_result(d["rpc"], tool_text("DIFF_REJECTED", d["title"])))
            conn.drop()
        self.server.shutdown()
        self.server.server_close()

    # -- connections (called from the listener's handler threads)

    def attach(self, client):
        """Claude Code has connected (with the token): the newest connection replaces any
        other, so a Claude that reconnects (/ide) is not refused. Returns its IdeConn, or None
        if the link has closed."""
        conn = IdeConn(client)
        with self.lock:
            if self.state == "off":
                return None
            old, self.conn = self.conn, conn
            if old is not None:
                self._forget(old)
                if self.state == "connected":  # until the new one says it is
                    self.state = "waiting"
                    self._status()
        if old is not None:
            old.drop()
            print("[ide] a new connection replaces the old one", flush=True)
        return conn

    def detach(self, conn):
        with self.lock:
            if self.conn is not conn:
                return
            self.conn = None
            self._forget(conn)
            if self.state == "connected":
                self.state = "waiting"
                self._status()
        print("[ide] Claude Code disconnected", flush=True)

    def _forget(self, conn):
        """A connection has gone (call with the lock): its diffs can't be answered any more,
        and nothing waits for its replies."""
        for d in [d for d in self.diffs.values() if d["conn"] is conn]:
            del self.diffs[d["id"]]
            self.session._broadcast_json({"type": "diff-close", "id": d["id"]})
        for w in self.waiters.values():
            if w[1] is conn:
                w[0].set()

    def _status(self):
        if self.session.bridge is self:
            self.session._broadcast_status()

    def live_conn(self):
        """The connection Claude has said it is using, or None."""
        with self.lock:
            return self.conn if self.conn is not None and self.conn.ready else None

    def is_connected(self):
        return self.live_conn() is not None

    # -- MCP from Claude

    def receive(self, conn, op, payload):
        """One message from Claude (on the connection's thread)."""
        if op != OP_TEXT:
            return
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (ValueError, RecursionError):  # RecursionError: nested too deep to parse
            conn.send(rpc_error(None, -32700, "parse error"))
            return
        if not isinstance(msg, dict):
            conn.send(rpc_error(None, -32600, "expected one JSON-RPC message"))
            return
        method, rid = msg.get("method"), msg.get("id")
        if method is None:  # an answer to a request of ours
            with self.lock:
                w = self.waiters.get(rid) if isinstance(rid, int) else None
                if w is not None and w[1] is conn:
                    w[2] = msg
                    w[0].set()
            return
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if "id" not in msg:
            try:
                self._notification(conn, method, params)
            except Exception:  # a bug: the link stays up
                traceback.print_exc()
            return
        try:
            out = self._request(conn, rid, method, params)
        except Exception as exc:  # a bug: still answer
            traceback.print_exc()
            out = rpc_error(rid, -32603, f"internal error: {type(exc).__name__}")
        if out is not None:
            conn.send(out)

    def _notification(self, conn, method, params):
        if method in ("notifications/initialized", "ide_connected"):
            with self.lock:
                if self.conn is not conn:
                    return
                conn.ready = True
                if self.state == "waiting":
                    self.state = "connected"
                    self._status()
                sel = self.selection
            if method == "ide_connected":  # what Claude sends is printed escaped (!r), never raw
                print(f"[ide] Claude Code connected (pid {repr(params.get('pid'))[:60]}, in its own namespace)",
                      flush=True)
            if method == "ide_connected" and sel is not None:
                # Claude listens for selections only a moment after connecting: send it twice
                conn.send(self._notify_msg("selection_changed", sel))
                again = threading.Timer(IDE_RESEND, self._resend, args=(conn, sel))
                again.daemon = True
                again.start()
        elif method == "notifications/cancelled":  # Claude gave up on an openDiff
            rid = params.get("requestId")
            with self.lock:
                for d in [d for d in self.diffs.values() if d["conn"] is conn and d["rpc"] == rid]:
                    del self.diffs[d["id"]]
                    self.session._broadcast_json({"type": "diff-close", "id": d["id"]})
        else:
            print(f"[ide] ignored notification {str(method)[:80]!r}", flush=True)

    def _resend(self, conn, sel):
        with self.lock:
            same = self.conn is conn and self.selection is sel
        if same:
            conn.send(self._notify_msg("selection_changed", sel))

    def _request(self, conn, rid, method, params):
        if method == "initialize":
            want = params.get("protocolVersion")
            return rpc_result(rid, {"protocolVersion": want if want in IDE_PROTOCOLS else IDE_PROTOCOLS[0],
                                    "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "md-editor", "version": __version__},
                                    "instructions": IDE_INSTRUCTIONS})
        if method == "tools/list":
            return rpc_result(rid, {"tools": IDE_TOOLS})
        if method == "ping":
            return rpc_result(rid, {})
        if method == "tools/call":
            args = params.get("arguments")
            return self._call(conn, rid, params.get("name"), args if isinstance(args, dict) else {})
        # a newer Claude Code calling something new shows up here
        print(f"[ide] unknown method {str(method)[:80]!r}", flush=True)
        return rpc_error(rid, -32601, f"method not found: {method}")

    def _call(self, conn, rid, name, args):
        if name == "getDiagnostics":
            return rpc_result(rid, tool_text("[]"))
        if name == "openDiff":
            return self._open_diff(conn, rid, args)
        if name in ("close_tab", "closeAllDiffTabs"):
            title = args.get("tab_name") if name == "close_tab" else None
            with self.lock:
                hit = [d for d in self.diffs.values()
                       if d["conn"] is conn and (name == "closeAllDiffTabs" or d["title"] == title)]
                for d in hit:
                    del self.diffs[d["id"]]
                    self.session._broadcast_json({"type": "diff-close", "id": d["id"]})
            for d in hit:  # Claude closed it itself, so this answer changes nothing
                conn.send(rpc_result(d["rpc"], tool_text("TAB_CLOSED")))
            return rpc_result(rid, tool_text("TAB_CLOSED"))
        print(f"[ide] unknown tool {str(name)[:80]!r}", flush=True)
        return rpc_error(rid, -32602, f"unknown tool: {name}")

    def _open_diff(self, conn, rid, args):
        """openDiff: show the proposed file beside the file on disk in every browser; the answer
        waits for the user (decide). A file md-editor won't show gets an error, so Claude
        falls back to asking in the terminal only."""
        old, new, contents, title = (args.get(k) for k in ("old_file_path", "new_file_path", "new_file_contents",
                                                            "tab_name"))
        if new is None:
            new = old
        if not all(isinstance(v, str) for v in (old, new, contents, title)):
            return rpc_error(rid, -32602, "openDiff needs old_file_path, new_file_path, new_file_contents and tab_name")
        why, real, disk, exists = None, None, None, True
        try:
            real = os.path.realpath(old)
            if os.path.realpath(new) != real:
                why = "md-editor shows changes to one file at a time"
            elif not os.path.isabs(old) or not inside(real, self.cwd) or real == self.cwd:
                why = f"md-editor shows changes to files in {self.cwd} only"
            elif ".git" in Path(os.path.relpath(real, self.cwd)).parts:
                why = "md-editor does not show changes inside .git"
            elif json_size(contents) > IDE_DIFF_MAX:
                why = "the proposed file is too large for md-editor to show"
            else:
                disk, exists = read_inside(real, self.cwd)  # never through a link swapped in since
                if disk is None:
                    why = "md-editor shows changes to text files only"
        except (ValueError, OSError):
            why = "md-editor cannot show a change to that path"
        if why:
            print(f"[ide] openDiff {old[:200]!r} not shown: {why}", flush=True)
            return rpc_result(rid, tool_text(why, error=True))
        lex = os.path.normpath(old)
        rel = next((os.path.relpath(p, str(ROOT)) for p in (lex, real) if inside(p, str(ROOT)) and p != str(ROOT)),
                   None)
        with self.lock:
            if self.conn is not conn:  # it has gone: no one to answer
                return None
            if len(self.diffs) >= IDE_DIFFS_MAX:
                return rpc_result(rid, tool_text("too many changes are waiting in md-editor", error=True))
            did = f"{DIFF_SALT}-{next(DIFF_IDS)}"
            d = self.diffs[did] = {"id": did, "conn": conn, "rpc": rid, "title": title, "file": real, "path": rel,
                                   "old": disk, "new": contents, "exists": exists}
            self.session._broadcast_json(self.diff_message(d))
        print(f"[ide] showing a proposed change to {real!r}", flush=True)
        return None

    def diff_message(self, d):
        """What a browser gets for a waiting diff: `path` is ROOT-relative (null when the file
        is outside ROOT), `file` absolute; `old` is the file on disk ("" for a new file, when
        `exists` is false) and `new` the proposal, both with LF line ends, as the editor holds
        text."""
        new = d["new"].replace("\r\n", "\n").replace("\r", "\n")
        return {"type": "diff", "id": d["id"], "path": d["path"], "file": d["file"], "title": d["title"],
                "old": d["old"], "new": new, "exists": d["exists"]}

    def diff_messages(self):
        """The waiting diffs, for a browser that attaches (call with the lock)."""
        return [self.diff_message(d) for d in self.diffs.values()]

    def decide(self, did, accept, contents=None):
        """The user's answer to diff `did` (the first browser's wins): Accept sends Claude the
        text to write (the proposal as edited in the diff, `contents`; md-editor never writes
        it), Reject denies the edit. False if it is not waiting.

        The text goes back with the proposal's line ends: Claude Code proposes LF text (it reads
        a CRLF file as LF, diffs the answer against that and writes the file's CRLFs back
        itself), so CRLFs here would make every line of the file a change."""
        with self.lock:
            d = self.diffs.pop(did, None) if isinstance(did, str) else None
            if d is None:
                return False
            self.session._broadcast_json({"type": "diff-close", "id": did})
        if accept:
            final = contents if isinstance(contents, str) else d["new"]
            if final == d["new"].replace("\r\n", "\n").replace("\r", "\n"):
                final = d["new"]  # unchanged: exactly as Claude proposed it
            elif "\r\n" in d["new"] and "\r" not in final:  # the editor's LFs back to the proposal's CRLFs
                final = final.replace("\n", "\r\n")
            reply = tool_text("FILE_SAVED", final)
        else:
            reply = tool_text("DIFF_REJECTED", d["title"])
        d["conn"].send(rpc_result(d["rpc"], reply))
        print(f"[ide] {'accepted' if accept else 'rejected'} the change to {d['file']!r}", flush=True)
        return True

    # -- to Claude (from the browsers' handler threads)

    @staticmethod
    def _notify_msg(method, params):
        return {"jsonrpc": "2.0", "method": method, "params": params}

    def notify(self, method, params):
        """Send Claude a notification; False when it is not connected."""
        conn = self.live_conn()
        return conn is not None and conn.send(self._notify_msg(method, params))

    def select(self, path, start, end, text):
        """The editor's selection in file `path` (absolute; inside the session's folder):
        remembered, and sent to Claude when it is connected."""
        params = {"text": text, "filePath": path, "fileUrl": Path(path).as_uri(),
                  "selection": {"start": start, "end": end, "isEmpty": start == end}}
        with self.lock:
            self.selection = params
        self.notify("selection_changed", params)

    def clear_selection(self):
        """The editor's selection is somewhere Claude may not see: Claude forgets the last one
        (an empty range with no file clears it; one with no range at all it would ignore).
        True if Claude was sent that."""
        with self.lock:
            if self.selection is None:  # nothing to forget
                return False
            self.selection = None
        zero = {"line": 0, "character": 0}
        return self.notify("selection_changed", {"text": "", "selection": {"start": zero, "end": zero, "isEmpty": True}})

    def selection_text(self, path, start, end):
        """The text of a range of file `path`: what the editor last said is selected there,
        or else the file's (which the editor saves before an ask); None if it is not text."""
        with self.lock:
            sel = self.selection
        if sel and sel["filePath"] == path and sel["selection"]["start"] == start and sel["selection"]["end"] == end:
            return sel["text"]
        text = read_inside(path, self.cwd)[0]
        return None if text is None else text_range(text, start, end)

    def ping(self, timeout=IDE_PING_TIMEOUT):
        """Ping Claude and wait for the answer: it answers in order, so it has then handled
        everything sent before. False if it is not connected or does not answer in time."""
        with self.lock:
            conn = self.conn if self.conn is not None and self.conn.ready else None
            if conn is None:
                return False
            rid = next(self.req_ids)
            w = self.waiters[rid] = [threading.Event(), conn, None]
        try:
            if not conn.send({"jsonrpc": "2.0", "id": rid, "method": "ping"}):
                return False
            w[0].wait(timeout)
            return w[2] is not None and "result" in w[2]
        finally:
            with self.lock:
                self.waiters.pop(rid, None)


class IdeServer:
    """Mixin for the listener of one IdeBridge (`bridge`): IdeHandler on each connection, in a
    thread of its own. In launcher mode anything in the jail can connect (the socket is in the
    folder it mounts), so at most IDE_CONNS_MAX connections are served at once (the rest are
    closed straight away) and each has IDE_HANDSHAKE to show the token (IdeHandler.timeout):
    idle connections cannot pile up threads and file descriptors in md-editor."""
    daemon_threads = True
    bridge = None

    def __init__(self, *args, **kw):
        self.slots = threading.BoundedSemaphore(IDE_CONNS_MAX)
        super().__init__(*args, **kw)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def service_actions(self):  # each turn of serve_forever
        if self.bridge is not None:
            self.bridge.check_lock()


class IdeTCPServer(IdeServer, socketserver.ThreadingTCPServer):
    pass


if hasattr(socketserver, "ThreadingUnixStreamServer"):
    class IdeUnixServer(IdeServer, socketserver.ThreadingUnixStreamServer):
        def server_bind(self):
            """Bind with mode 0600. Linux makes the socket file with the socket's own mode (less
            the umask); elsewhere it is changed just after, never through a symlink (the
            folder is the agent's)."""
            try:
                os.fchmod(self.socket.fileno(), 0o600)
                made = True
            except OSError:
                made = False
            super().server_bind()
            if not made and os.chmod in os.supports_follow_symlinks:
                os.chmod(self.server_address, 0o600, follow_symlinks=False)


class IdeHandler(WSReader, BaseHTTPRequestHandler):
    """The IDE link's listener: a WebSocket upgrade on / that carries the lock file's token, and
    nothing else (none of Handler's API is here). Claude Code sends no Origin, so none is
    checked; a browser could not send the token's header. The request must come within
    IDE_HANDSHAKE (StreamRequestHandler's timeout, lifted once it has upgraded)."""
    server_version = "md-editor-ide"
    timeout = IDE_HANDSHAKE

    def log_message(self, fmt, *args):  # refusals are logged by refuse()
        pass

    def refuse(self, code, why):
        print(f"[ide] refused a connection: {why}", flush=True)
        body = (why + "\n").encode()
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        bridge = self.server.bridge
        if self.path != "/":  # Claude dials ws://127.0.0.1:<port>
            return self.refuse(404, f"no such path: {self.path[:100]!r}")
        tokens = self.headers.get_all("X-Claude-Code-Ide-Authorization") or []
        given = tokens[0].encode("utf-8", "surrogateescape") if len(tokens) == 1 else b""
        if not hmac.compare_digest(given, bridge.token.encode()):
            return self.refuse(401, "missing or wrong X-Claude-Code-Ide-Authorization")
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        tokens = {t.strip().lower() for t in (self.headers.get("Connection") or "").split(",")}
        if ((self.headers.get("Upgrade") or "").lower() != "websocket" or "upgrade" not in tokens
                or (self.headers.get("Sec-WebSocket-Version") or "").strip() != "13" or not key):
            return self.refuse(400, "expected a WebSocket upgrade")
        protocols = {p.strip() for p in (self.headers.get("Sec-WebSocket-Protocol") or "").split(",")}
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.close_connection = True
        # no Sec-WebSocket-Extensions: permessage-deflate (which Claude offers) is refused
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n"
                          + ("Sec-WebSocket-Protocol: mcp\r\n" if "mcp" in protocols else "") + "\r\n").encode())
        self.connection.settimeout(None)  # Claude may be quiet for as long as it likes now
        client = WSClient(self.connection)
        conn = bridge.attach(client)
        if conn is None:
            client.close(1001)
            return
        try:
            self.ws_serve(client, IDE_MAX_MESSAGE, lambda op, payload: bridge.receive(conn, op, payload))
        except WSProtocolError as exc:
            client.close(exc.code)
            self.ws_drain()
        except (OSError, EOFError, ValueError):
            pass
        finally:
            bridge.detach(conn)
            with client.send_lock:
                client.alive = False

    def not_found(self):
        self.refuse(404, f"{self.command} is not served here")

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_found


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


class Handler(WSReader, SimpleHTTPRequestHandler):
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
                                       "files": list_files(), "agent": agent_info()})
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
        except subprocess.TimeoutExpired:  # Chrome, printing a PDF
            return self.send_json({"error": "timed out"}, 504)
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

    def ws_loop(self, client):
        self.ws_serve(client, WS_MAX_MESSAGE, lambda op, payload: self.ws_message(client, op, payload))

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
        except (ValueError, RecursionError):  # includes UnicodeDecodeError; nested too deep
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "resize":
            try:
                cols, rows = int(msg["cols"]), int(msg["rows"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return
            nudge, client.sized = not client.sized, True
            TERM.resize(cols, rows, nudge)
        elif kind == "restart":
            TERM.restart()
        elif kind == "selection":
            TERM.selection(msg)
        elif kind in ("ask", "mention"):
            client.writing = True  # it may wait for Claude, and for the agent to read its input
            try:
                if kind == "ask":   # a session still starting is waited for: the page says so
                    waiting = {"type": "waiting", "what": kind}
                    if "seq" in msg:
                        waiting["seq"] = msg["seq"]
                    res = TERM.ask(msg, lambda: client.alive,
                                   lambda: client.send_frame(ws_frame(OP_TEXT, json.dumps(waiting).encode())))
                else:
                    res = TERM.mention(msg, lambda: client.alive)
            finally:
                client.last_seen = time.monotonic()
                client.writing = False
            res = dict(res, type="sent", what=kind)
            if "seq" in msg:
                res["seq"] = msg["seq"]
            client.send_frame(ws_frame(OP_TEXT, json.dumps(res).encode()))
        elif kind == "diff-decision":
            TERM.diff_decision(client, msg)


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
    global ROOT, INITIAL, AGENT_CMD, IDE_LINK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".", help="folder or .md file to edit (default: current folder)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--agent", metavar="CMD", default=os.environ.get("MDEDIT_AGENT", DEFAULT_AGENT),
                    help="command run in the side-panel terminal, in the open folder "
                         f"(default: $MDEDIT_AGENT or '{DEFAULT_AGENT}'; 'none' turns the terminal off)")
    ap.add_argument("--ide-link", metavar="auto|off|native|launcher",
                    default=os.environ.get("MDEDIT_IDE_LINK") or "auto",
                    help="whether the terminal's Claude Code session connects back to md-editor, which then "
                         "sends it the editor's selection and shows its proposed edits as diffs. auto links "
                         "claude (native) and claude-sandbox (launcher) agents; native or launcher links "
                         "another agent that runs one of them (default: $MDEDIT_IDE_LINK or 'auto')")
    args = ap.parse_args()
    try:
        AGENT_CMD = parse_command(args.agent)
    except ValueError as exc:
        ap.error(f"--agent: {exc}")
    IDE_LINK = args.ide_link.strip().lower()
    if IDE_LINK not in ("auto", "off", "native", "launcher"):
        ap.error(f"--ide-link: expected auto, off, native or launcher, not {args.ide_link!r}")

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
    if info["available"]:
        mode = ide_mode() if IDE_LINK != "off" else None
        print(f"IDE link: {mode} mode" if mode else "IDE link: off", flush=True)
        if mode:
            sweep_ide_locks()
    # stop cleanly on Ctrl+C, SIGTERM, and SIGHUP unless it is ignored (nohup), so the
    # terminal's process group is killed rather than orphaned, and the IDE link's lock file
    # and socket are deleted
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None and signal.getsignal(sig) in (signal.SIG_DFL, signal.default_int_handler):
            signal.signal(sig, _interrupt)
    atexit.register(TERM.kill)
    atexit.register(end_searches)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        TERM.kill()
        end_searches()
        server.server_close()


if __name__ == "__main__":
    main()
