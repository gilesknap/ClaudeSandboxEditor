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
import mimetypes
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
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


# ---------------------------------------------------------------- files

def safe_path(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError("path escapes the root folder")
    return p


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


# ---------------------------------------------------------------- processes

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

try:  # seconds per claude call; the tests shorten it
    ASK_TIMEOUT = float(os.environ.get("MDEDIT_ASK_TIMEOUT") or 600)
except ValueError:
    ASK_TIMEOUT = 600
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
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # JSON never holds these raw


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


def claude_args(model, persist=True, resume=None):
    args = ["-p", "--output-format", "json"]
    if not persist:
        args.append("--no-session-persistence")
    if resume:  # fork, so the parent session never changes
        args += ["--resume", resume, "--fork-session"]
    args += ["--append-system-prompt", SYSTEM_PROMPT]
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
    return f'<document path="{path}">\n{doc}\n</document>'


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
    parts.append("Reply with the replacement text only." if mode == "replace"
                 else "Reply with your comments only.")
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
    msg = (doc_tag(path, doc) + "\n\nThis is the document the user is editing. Each later message "
           "is one independent request about it. Reply with just OK.")
    data = run_claude(claude_args(model), msg)
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
        data = run_claude(claude_args(key[3], resume=parent), build_request(req, diff))
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


def unfence(text: str, original: str) -> str:
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and not original.lstrip().startswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2:
            t = "\n".join(lines[1:-1])
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
        data, forked = run_claude(claude_args(key[3], persist=False), msg), False
    u = data.get("usage") or {}
    print(f"[ask] {'forked' if forked else 'stateless'} reply in {time.time() - t0:.1f}s; input tokens: "
          f"{u.get('input_tokens')} new, {u.get('cache_read_input_tokens')} cached, "
          f"{u.get('cache_creation_input_tokens')} written to the cache", flush=True)
    original = doc[int(req.get("start", 0)):int(req.get("end", 0))]
    return {
        "result": unfence(data.get("result", ""), original),
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

def export_pdf(req):
    """Print the editor's rendered HTML to <name>.pdf beside the markdown file."""
    if not CHROME_BIN:
        raise RuntimeError("PDF export needs Google Chrome or Chromium (or set MDEDIT_CHROME)")
    md = safe_path(req["path"])
    pdf = md.with_suffix(".pdf")
    # images point at the /raw/ route; Chrome reads them straight from disk instead
    body = req["html"].replace('src="/raw/', f'src="{ROOT.as_uri()}/')
    page = PDF_TEMPLATE.format(title=md.stem.replace("<", "&lt;"), body=body)
    # the page sits beside the .md so relative image links resolve
    tmp_html = md.with_name(f".{md.stem}.print.html")
    tmp_html.write_text(page, encoding="utf-8")
    profile = tempfile.mkdtemp(prefix="mdedit-chrome-")
    try:
        proc = subprocess.run(
            [CHROME_BIN, "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
             f"--user-data-dir={profile}", "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw",
             f"--print-to-pdf={pdf}", tmp_html.as_uri()],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not pdf.exists():
            raise RuntimeError(f"Chrome failed to print: {(proc.stderr or proc.stdout).strip()[-800:]}")
    finally:
        tmp_html.unlink(missing_ok=True)
        shutil.rmtree(profile, ignore_errors=True)
    return {"pdf": pdf.relative_to(ROOT).as_posix(), "bytes": pdf.stat().st_size}


# ---------------------------------------------------------------- term

SCROLLBACK_MAX = 512 * 1024


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
                ok = client.send_frame(ws_frame(OP_BIN, bytes(self.scrollback)))
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
            del sb[:nl + 1 if nl >= 0 else excess]

    # -- process

    def _spawn_locked(self):
        """Start the agent in the current ROOT (caller holds op_lock and lock, no live session)."""
        self.next_id += 1
        self.id, self.state, self.cwd = self.next_id, "running", str(ROOT)
        self.code = self.message = None
        self.scrollback = bytearray()
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

    def write(self, data):
        with self.lock:
            rec = self.cur
        if rec is None:
            return
        with rec.wlock:  # not under self.lock: a blocked write must not stall the output
            view = memoryview(data)
            while view and not rec.closed and not rec.retired:
                try:
                    view = view[os.write(rec.fd, view):]
                except BlockingIOError:  # the agent is not reading: wait for room
                    wait_fd(rec.fd, write=True, timeout=0.5)
                except OSError:
                    return

    def resize(self, cols, rows):
        cols, rows = max(1, min(1000, cols)), max(1, min(1000, rows))
        with self.lock:
            self.size = (cols, rows)
            rec = self.cur
            if rec is not None and not rec.closed:
                try:
                    set_winsize(rec.fd, cols, rows)  # the kernel sends SIGWINCH
                except OSError:
                    pass
            first = self.state == "none" and not self.closing
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
try:  # seconds between pings; a browser that has not answered one by the next is dropped
    WS_PING_INTERVAL = float(os.environ.get("MDEDIT_WS_PING") or 30)  # the tests shorten it
except ValueError:
    WS_PING_INTERVAL = 30
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
        even the pong browsers send by themselves) has come back since the last ping. While
        the handler is blocked in a PTY write (a paste the agent is not reading) the pongs
        wait unread behind it, so the client is neither pinged nor judged until it is done."""
        pinged = None
        while not stop.wait(WS_PING_INTERVAL):
            if self.writing:
                pinged = None
                continue
            if pinged is not None and self.last_seen < pinged:
                with self.send_lock:
                    self._drop_locked()
                return
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
HOST_RE = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[^\[\]:/@\s]+)(?::[0-9]{1,5})?")


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
        if "/api/events" not in str(args[0] if args else ""):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_raw(self, rel):
        """Serve a file from the document root (images referenced by the markdown)."""
        p = safe_path(rel)
        if not p.is_file():
            return self.send_json({"error": "not found"}, 404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_HEAD(self):
        if not self.guard():
            super().do_HEAD()

    def do_GET(self):
        u = urlparse(self.path)
        if self.guard(need_origin=u.path == "/api/term"):
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
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
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/browse":
                return self.send_json(browse(q.get("dir", "")))
            if u.path == "/api/files":
                return self.send_json(list_files())
            if u.path == "/api/file":
                p = safe_path(q["path"])
                if not p.is_file():
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json({"path": q["path"], "text": p.read_text(encoding="utf-8"),
                                       "version": version_of(p)})
            if u.path == "/api/events":
                return self.events()
            if u.path.startswith("/raw/"):
                return self.send_raw(unquote(u.path[len("/raw/"):]))
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
            if not req.get("force") and base is not None and version_of(p) != base:
                return self.send_json({"error": "conflict", "version": version_of(p),
                                       "text": p.read_text(encoding="utf-8") if p.exists() else ""}, 409)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name("." + p.name + ".tmp")
            tmp.write_text(req["text"], encoding="utf-8")
            os.replace(tmp, p)
            return self.send_json({"version": version_of(p)})
        except (ValueError, KeyError) as exc:
            return self.send_json({"error": str(exc)}, 400)

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
            if u.path == "/api/new":
                rel = req["path"].strip()
                if not rel.lower().endswith(MD_EXT):
                    rel += ".md"
                p = safe_path(rel)
                if p.exists():
                    return self.send_json({"error": "file already exists"}, 409)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(req.get("text", f"# {p.stem}\n\n"), encoding="utf-8")
                return self.send_json({"path": rel, "version": version_of(p)})
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
                return TERM.write(payload)
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
            TERM.resize(cols, rows)
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
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        TERM.kill()
        end_asks()
        server.server_close()


if __name__ == "__main__":
    main()
