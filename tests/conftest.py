"""Fixtures: md-editor servers started from this checkout, each stopped afterwards and checked
for processes left behind."""
import sys

import pytest

from helpers import SRC, Server

sys.path.insert(0, str(SRC))   # the in-process tests import md_collab_editor.server


def make_root(base):
    """A document folder with one markdown file, a.md."""
    r = base / "root"
    r.mkdir()
    (r / "a.md").write_text("# a\n\nSome text.\n", encoding="utf-8")
    return r.resolve()


class Servers:
    """Starts md-editor servers under `base`; stop_all() stops them and lists leftovers."""

    def __init__(self, base, root):
        self.base, self.root, self.servers = base, root, []

    def start(self, root=None, **kw):
        """start(root=..., agent="none", args=(), env=None) -> Server"""
        work = self.base / f"server{len(self.servers)}"
        (work / "tmp").mkdir(parents=True)
        # the IDE link's lock files go to folders of the test's own, never ~/.claude/ide: in
        # native mode (an agent called claude) $CLAUDE_CONFIG_DIR/ide; in launcher mode (any
        # other agent) $CLAUDE_SANDBOX_SHARED_CONFIG/.claude/ide, and as that has no .claude
        # folder, launcher mode has no link (and appends nothing to the agent's arguments)
        # unless a test makes one
        kw["env"] = dict({"TMPDIR": str(work / "tmp"), "XDG_CACHE_HOME": str(work / "cache"),
                          "CLAUDE_CONFIG_DIR": str(work / "claude-config"),
                          "CLAUDE_SANDBOX_SHARED_CONFIG": str(work / "sandbox-config")},
                         **(kw.get("env") or {}))
        srv = Server(root or self.root, work, **kw)
        self.servers.append(srv)
        return srv

    def stop_all(self):
        left = {}
        for srv in self.servers:
            srv.stop()
            pids = srv.leftovers()
            if pids:
                left[srv.port] = pids
        assert not left, f"processes left running after md-editor stopped: {left}"


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


@pytest.fixture
def md_editor(tmp_path, root):
    """Factory: md_editor(root=root, agent="none", args=(), env=None) starts
    `python -m md_collab_editor` on a free port and returns its Server (base URL in .base).
    On teardown every server is stopped (SIGTERM to its group) and nothing it started may
    still be running."""
    servers = Servers(tmp_path, root)
    yield servers.start
    servers.stop_all()


@pytest.fixture(scope="module")
def shared_servers(tmp_path_factory):
    """Like md_editor, for servers that a whole test module shares (`.root`, `.start`)."""
    base = tmp_path_factory.mktemp("shared")
    servers = Servers(base, make_root(base))
    yield servers
    servers.stop_all()
