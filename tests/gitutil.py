"""Test helpers for the workspace API: temporary folders and git repositories (made with the
user's and the system's git config ignored), and opening a folder in a running md-editor."""
import os
import subprocess
from pathlib import Path

# what the md-editor under test gets, so the user's git config can't change the answers
SERVER_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(SERVER_ENV, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")


def git(cwd, *args, check=True):
    """Run git in cwd; returns its stdout."""
    r = subprocess.run(["git", *args], cwd=str(cwd), env=GIT_ENV, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def write(root, files):
    """Write {relative path: text or bytes} under root, making folders."""
    for rel, data in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            with open(p, "w", encoding="utf-8", newline="") as f:  # Path.write_text has newline= from 3.10
                f.write(data)


def make_repo(path, files=None, branch="main", message="initial"):
    """A git repository at path on `branch`, with `files` committed (if any)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    if files:
        write(path, files)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", message)
    return path.resolve()


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def open_root(srv, path):
    """Point the server at a folder, as the in-app file browser does."""
    status, res = srv.post("/api/root", {"path": str(path)})
    assert status == 200, res
    return Path(res["root"])
