"""The workspace file API: /api/tree, /api/stat, /api/file kinds, /api/new, /api/rename,
/api/delete and /api/allfiles. Every path is ROOT-relative and none may leave the open folder,
including by way of a symlink. One server for the module; each test opens its own folder."""
import os
from urllib.parse import urlencode

import pytest

from gitutil import SERVER_ENV, git, make_repo, open_root, write

from md_collab_editor import server as S


@pytest.fixture(scope="module")
def srv(shared_servers):
    return shared_servers.start(env=SERVER_ENV)


@pytest.fixture
def ws(srv, tmp_path):
    """A fresh, empty folder opened in the server; tests fill it."""
    root = tmp_path / "ws"
    root.mkdir()
    return open_root(srv, root)


def get(srv, endpoint, **params):
    return srv.get(f"{endpoint}?{urlencode(params)}" if params else endpoint)


def tree(srv, d="", all=0):
    status, res = get(srv, "/api/tree", dir=d, all=all)
    assert status == 200, res
    return res


def names(res):
    return [e["name"] for e in res["entries"]]


# ---------------------------------------------------------------- tree

def test_tree_lists_one_folder_folders_first_case_insensitively(srv, ws):
    write(ws, {"c.txt": "ccc", "A.md": "# A\n", "b/x.py": "x = 1\n", "B2/y": "", "a/z": ""})
    res = tree(srv)
    assert res["dir"] == ""
    assert names(res) == ["a", "b", "B2", "A.md", "c.txt"]
    first = res["entries"][0]
    assert first == {"name": "a", "path": "a", "dir": True, "size": None, "hidden": False, "ignored": False}
    assert res["entries"][4] == {"name": "c.txt", "path": "c.txt", "dir": False, "size": 3, "hidden": False,
                                 "ignored": False}
    sub = tree(srv, "b")
    assert sub["dir"] == "b" and sub["entries"] == [
        {"name": "x.py", "path": "b/x.py", "dir": False, "size": 6, "hidden": False, "ignored": False}]
    assert tree(srv, "./b/../b/")["dir"] == "b", "the folder comes back normalised"


def test_tree_hides_dotfiles_and_skip_dirs_unless_asked_and_never_shows_git(srv, ws):
    write(ws, {".env": "X=1", ".cache/c": "", "node_modules/m/i.js": "", "__pycache__/p.pyc": b"\0",
               "venv": "a file, so not hidden", "src/a.py": "", ".git/HEAD": "ref: refs/heads/main\n"})
    assert names(tree(srv)) == ["src", "venv"]
    full = tree(srv, all=1)["entries"]
    assert [(e["name"], e["hidden"]) for e in full] == [
        (".cache", True), ("__pycache__", True), ("node_modules", True), ("src", False), (".env", True),
        ("venv", False)]
    assert not any(e["ignored"] for e in full), "no repo, so nothing is ignored"
    assert names(tree(srv, "node_modules", all=1)) == ["m"], "a hidden folder can be listed"


def test_tree_flags_git_ignored_entries(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {".gitignore": "build/\n*.log\n", "src/a.py": "a\n", "kept.log": "tracked\n"},)
    git(repo, "add", "-f", "kept.log")
    git(repo, "commit", "-q", "-m", "force-add an ignored name")
    write(repo, {"build/out.js": "", "debug.log": "", "src/b.log": "", "new.txt": ""})
    open_root(srv, repo)
    assert names(tree(srv)) == ["src", "kept.log", "new.txt"], "ignored and hidden entries are left out"
    full = {e["name"]: (e["hidden"], e["ignored"]) for e in tree(srv, all=1)["entries"]}
    assert full == {"build": (False, True), "src": (False, False), ".gitignore": (True, False),
                    "debug.log": (False, True), "kept.log": (False, False), "new.txt": (False, False)}
    assert [(e["path"], e["ignored"]) for e in tree(srv, "build", all=1)["entries"]] == [("build/out.js", True)]
    assert [(e["name"], e["ignored"]) for e in tree(srv, "src", all=1)["entries"]] == [("a.py", False),
                                                                                        ("b.log", True)]


def test_tree_in_a_git_ignored_root_shows_everything(srv, tmp_path):
    """Opening a folder that its repository ignores: git would ignore every entry, so it doesn't decide."""
    repo = make_repo(tmp_path / "repo", {".gitignore": "scratch/\n", "a": ""})
    write(repo, {"scratch/notes.md": "", "scratch/x.log": ""})
    open_root(srv, repo / "scratch")
    assert names(tree(srv)) == ["notes.md", "x.log"]


def test_tree_errors_and_symlinks(srv, ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    write(ws, {"f.txt": "", "d/x": ""})
    os.symlink(outside, ws / "out")
    os.symlink(ws / "d", ws / "inlink")
    assert get(srv, "/api/tree", dir="missing")[0] == 404
    assert get(srv, "/api/tree", dir="f.txt")[0] == 404
    for bad in ("..", "../outside", str(outside), "out", "d/../../outside"):
        status, res = get(srv, "/api/tree", dir=bad)
        assert status == 400 and "escapes" in res["error"], bad
    assert {e["name"]: e["dir"] for e in tree(srv)["entries"]} == {"d": True, "inlink": True, "out": True,
                                                                     "f.txt": False}
    assert tree(srv, "inlink")["entries"][0]["path"] == "inlink/x", "paths follow the folder as asked for"


# ---------------------------------------------------------------- stat

def stat(srv, paths=(), dirs=()):
    status, res = srv.post("/api/stat", {"paths": list(paths), "dirs": list(dirs)})
    assert status == 200, res
    return res


def test_symlink_loops_get_an_answer(srv, ws, monkeypatch):
    """Python 3.9-3.12's resolve() raises RuntimeError on a symlink loop, which dropped the
    connection (or failed the whole /api/stat poll with a 502)."""
    os.symlink("loop", ws / "loop")
    os.symlink("b", ws / "a")
    os.symlink("a", ws / "b")
    write(ws, {"ok.md": "ok\n"})
    assert names(tree(srv)) == ["a", "b", "loop", "ok.md"]
    for path in ("loop", "a"):
        assert get(srv, "/api/file", path=path)[0] in (400, 404)
        assert get(srv, "/api/tree", dir=path)[0] in (400, 404)
        assert srv.get(f"/raw/{path}")[0] in (400, 404)
        assert srv.put("/api/file", {"path": path, "text": "x"})[0] in (400, 404, 500)
        assert srv.post("/api/new", {"path": f"{path}/x.md"})[0] in (400, 404, 500)
    status, res = srv.post("/api/stat", {"paths": ["loop", "ok.md"], "dirs": ["loop", ""]})
    assert status == 200 and res["versions"]["loop"] == "0" and res["versions"]["ok.md"] != "0"
    assert res["dirs"]["loop"] == "0"
    # whatever this Python does with the loop itself
    monkeypatch.setattr(S, "ROOT", ws)
    monkeypatch.setattr(S.Path, "resolve", lambda self, strict=False: (_ for _ in ()).throw(RuntimeError("Symlink loop")))
    with pytest.raises(ValueError, match="symlink loop"):
        S.safe_path("loop")
    with pytest.raises(ValueError, match="symlink loop"):
        S.safe_path("loop/x", follow=False)


def test_file_names_that_are_not_utf8(srv, ws):
    """Such a name is listed with lone surrogates (Python's surrogateescape), and the page sends
    those back as the original bytes, %-escaped."""
    with open(os.path.join(os.fsencode(ws), b"lat\xe9.md"), "w") as f:
        f.write("# Latin-1\n")
    os.mkdir(os.path.join(os.fsencode(ws), b"dir\xe9"))
    with open(os.path.join(os.fsencode(ws), b"dir\xe9", b"in.md"), "w") as f:
        f.write("inside\n")
    assert names(tree(srv)) == ["dir\udce9", "lat\udce9.md"]
    status, res = srv.get("/api/file?path=lat%E9.md")
    assert status == 200 and res["text"] == "# Latin-1\n" and res["path"] == "lat\udce9.md"
    status, res = srv.get("/api/tree?dir=dir%E9")
    assert status == 200 and res["entries"][0]["path"] == "dir\udce9/in.md"
    assert srv.get("/raw/dir%E9/in.md") == (200, b"inside\n")
    status, res = srv.put("/api/file", {"path": "lat\udce9.md", "text": "saved\n"})
    assert status == 200
    assert open(os.path.join(os.fsencode(ws), b"lat\xe9.md")).read() == "saved\n"


def test_stat_versions_match_the_file_api(srv, ws, tmp_path):
    write(ws, {"a.md": "# a\n", "d/b.txt": "b"})
    (tmp_path / "outside.txt").write_text("x")
    res = stat(srv, ["a.md", "d/b.txt", "gone.md", "d", "../outside.txt"])
    assert res["versions"]["a.md"] == get(srv, "/api/file", path="a.md")[1]["version"] != "0"
    assert res["versions"]["d/b.txt"] != "0"
    assert res["versions"]["gone.md"] == res["versions"]["d"] == res["versions"]["../outside.txt"] == "0"
    os.utime(ws / "a.md", ns=(1, 1_000_000_000))
    assert stat(srv, ["a.md"])["versions"]["a.md"] == "1000000000"


def test_stat_dir_stamps_change_when_entries_come_and_go(srv, ws):
    write(ws, {"d/x.txt": "x"})
    first = stat(srv, dirs=["", "d", "nope"])["dirs"]
    assert first["nope"] == "0" and first[""] != "0" and first["d"] != "0" and len(first["d"]) == 16
    assert stat(srv, dirs=["", "d"])["dirs"] == {"": first[""], "d": first["d"]}, "steady when nothing changes"
    (ws / "d" / "y.txt").write_text("y")
    second = stat(srv, dirs=["", "d"])["dirs"]
    assert second["d"] != first["d"]
    (ws / "d" / "y.txt").unlink()
    assert stat(srv, dirs=["d"])["dirs"]["d"] == first["d"]
    # a file saved in it does not change the folder's rows, so not its stamp either (the page
    # would list the folder again after every save) ...
    os.utime(ws / "d" / "x.txt", ns=(5, 5))
    (ws / "d" / "x.txt").write_text("changed")
    assert stat(srv, dirs=["d"])["dirs"]["d"] == first["d"]
    # ... except a .gitignore, which decides what is ignored there
    write(ws, {"d/.gitignore": "x.txt\n"})
    third = stat(srv, dirs=["d"])["dirs"]["d"]
    os.utime(ws / "d" / ".gitignore", ns=(5, 5))
    assert stat(srv, dirs=["d"])["dirs"]["d"] not in (first["d"], third)
    top = stat(srv, dirs=[""])["dirs"][""]
    os.mkdir(ws / ".git")  # never listed, so never counted
    assert stat(srv, dirs=[""])["dirs"][""] == top


def test_stat_rejects_bad_input(srv, ws):
    assert srv.post("/api/stat", {"paths": "a.md"})[0] == 400
    assert srv.post("/api/stat", ["a.md"])[0] == 400
    assert stat(srv) == {"versions": {}, "dirs": {}}


# ---------------------------------------------------------------- file kinds

def file(srv, path):
    return get(srv, "/api/file", path=path)


def test_file_kinds(srv, ws):
    write(ws, {"t.py": "print('hi')\r\nx = 1\r\n", "bin.dat": b"abc\0def", "latin.txt": "caf\xe9".encode("latin-1"),
               "pic.png": "not really a png", "logo.svg": "<svg/>", "script": "#!/usr/bin/env python3\nprint(1)\n",
               "notes.md": "# n\n", "plain": "no idea\n"})
    with open(ws / "big.txt", "wb") as f:
        f.truncate(S.TEXT_MAX + 1)
    status, t = file(srv, "t.py")
    assert status == 200
    assert t == {"path": "t.py", "kind": "text", "size": (ws / "t.py").stat().st_size, "version": t["version"],
                 "text": "print('hi')\nx = 1\n", "mode": "Python"}, "CRLF arrives as LF"
    for name, kind in (("bin.dat", "binary"), ("latin.txt", "binary"), ("pic.png", "image"), ("logo.svg", "image"),
                       ("big.txt", "too_large")):
        status, res = file(srv, name)
        assert status == 200 and res["kind"] == kind and "text" not in res, (name, res)
        assert res["size"] == (ws / name).stat().st_size and res["version"] != "0"
    assert file(srv, "script")[1]["mode"] == "Python", "from the #! line"
    assert file(srv, "notes.md")[1]["mode"] == "Markdown"
    assert "mode" not in file(srv, "plain")[1]
    assert file(srv, "missing.txt")[0] == 404
    os.mkdir(ws / "folder")
    assert file(srv, "folder")[0] == 404
    assert file(srv, "../x")[0] == 400
    assert get(srv, "/api/file")[0] == 400


def test_save_keeps_crlf_line_ends_and_permissions(srv, ws):
    write(ws, {"run.sh": "#!/bin/sh\r\necho hi\r\n"})
    os.chmod(ws / "run.sh", 0o750)
    status, f = file(srv, "run.sh")
    assert f["text"] == "#!/bin/sh\necho hi\n"
    status, res = srv.put("/api/file", {"path": "run.sh", "text": f["text"] + "echo bye\n", "base_version": f["version"]})
    assert status == 200, res
    assert (ws / "run.sh").read_bytes() == b"#!/bin/sh\r\necho hi\r\necho bye\r\n"
    assert (ws / "run.sh").stat().st_mode & 0o777 == 0o750
    status, _ = srv.put("/api/file", {"path": "lf.txt", "text": "a\nb\n"})
    assert status == 200 and (ws / "lf.txt").read_bytes() == b"a\nb\n"


# ---------------------------------------------------------------- new

def new(srv, **body):
    return srv.post("/api/new", body)


def test_new_files_and_folders(srv, ws):
    status, res = new(srv, path="notes")
    assert status == 200 and res["path"] == "notes.md" and res["version"] != "0"
    assert (ws / "notes.md").read_text() == "# notes\n\n", "today's + New: a markdown file with a heading"
    status, res = new(srv, path="src/pkg/mod.py")
    assert (status, res["path"]) == (200, "src/pkg/mod.py") and (ws / "src/pkg/mod.py").read_text() == ""
    assert new(srv, path=".gitignore")[1]["path"] == ".gitignore", "a dotfile has a name, not a missing extension"
    assert new(srv, path="Makefile")[1]["path"] == "Makefile.md"
    assert new(srv, path="Makefile", exact=True)[1]["path"] == "Makefile"
    status, res = new(srv, path="docs/guide.md", text="hello\r\n")
    assert res == {"path": "docs/guide.md", "version": res["version"]}
    assert (ws / "docs/guide.md").read_bytes() == b"hello\r\n"
    status, res = new(srv, path="a/b", dir=True)
    assert (status, res) == (200, {"path": "a/b"}) and (ws / "a/b").is_dir()
    assert new(srv, path="./c/../c2.txt")[1]["path"] == "c2.txt"
    assert new(srv, path="later/")[1]["path"] == "later.md", "a trailing / is not an empty name"
    assert new(srv, path="/")[0] == 400


def test_new_refuses_what_exists(srv, ws):
    write(ws, {"a.md": "keep", "d/x": ""})
    status, res = new(srv, path="a.md", text="overwrite")
    assert status == 409 and (ws / "a.md").read_text() == "keep"
    assert new(srv, path="d", dir=True)[0] == 409
    assert new(srv, path="d", exact=True)[0] == 409
    assert new(srv, path="", dir=True)[0] == 400
    os.symlink("nowhere", ws / "dangling.md")
    assert new(srv, path="dangling.md")[0] == 409


def test_new_never_leaves_the_folder(srv, ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, ws / "out")
    for body in ({"path": "../x.md"}, {"path": str(outside / "x.md")}, {"path": "out/x.md"},
                 {"path": "out/sub", "dir": True}, {"path": ".git/hooks/x", "exact": True}, {"path": "a/.git", "dir": True}):
        status, res = new(srv, **body)
        assert status == 400, (body, res)
    assert list(outside.iterdir()) == [] and not (ws / ".git").exists() and not (ws / "a").exists()


# ---------------------------------------------------------------- rename

def rename(srv, src, dst):
    return srv.post("/api/rename", {"from": src, "to": dst})


def test_rename_files_and_folders(srv, ws):
    write(ws, {"a.md": "A", "d/x.txt": "X", "d/e/y.txt": "Y"})
    assert rename(srv, "a.md", "new/place/b.md") == (200, {"path": "new/place/b.md"})
    assert (ws / "new/place/b.md").read_text() == "A" and not (ws / "a.md").exists()
    assert rename(srv, "d", "moved/d2") == (200, {"path": "moved/d2"})
    assert (ws / "moved/d2/e/y.txt").read_text() == "Y" and not (ws / "d").exists()


def test_rename_refusals(srv, ws, tmp_path):
    write(ws, {"a.md": "A", "b.md": "B", "d/x": ""})
    assert rename(srv, "a.md", "b.md")[0] == 409
    assert (ws / "a.md").read_text() == "A" and (ws / "b.md").read_text() == "B"
    assert rename(srv, "a.md", "d")[0] == 409
    assert rename(srv, "missing.md", "c.md")[0] == 404
    assert rename(srv, "d", "d/inner/d")[0] == 400
    for src, dst in (("", "x"), (".", "x"), ("a.md", ""), ("a.md", "../a.md"), ("../ws/a.md", "../a2.md"),
                     ("a.md", ".git/a.md"), ("a.md", str(tmp_path / "a.md"))):
        assert rename(srv, src, dst)[0] == 400, (src, dst)
    assert (ws / "a.md").exists() and not (tmp_path / "a.md").exists()
    assert srv.post("/api/rename", {"from": "a.md"})[0] == 400


def test_rename_moves_a_symlink_not_its_target(srv, ws, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("T")
    os.symlink(target, ws / "link.txt")
    assert rename(srv, "link.txt", "sub/link2.txt")[0] == 200
    assert os.path.islink(ws / "sub/link2.txt") and target.read_text() == "T"


# ---------------------------------------------------------------- delete

def delete(srv, path, **kw):
    return srv.post("/api/delete", dict(path=path, **kw))


def test_delete_files_and_folders_with_counts(srv, ws):
    write(ws, {"a.md": "", "d/x": "", "d/y": "", "d/e/z": "", "d/e/f/w": ""})
    assert delete(srv, "a.md") == (200, {"ok": True, "files": 1, "dirs": 0})
    assert not (ws / "a.md").exists()
    assert delete(srv, "d", dry_run=True) == (200, {"ok": True, "files": 4, "dirs": 2})
    assert (ws / "d/e/f/w").exists(), "a dry run deletes nothing"
    assert delete(srv, "d") == (200, {"ok": True, "files": 4, "dirs": 2})
    assert not (ws / "d").exists()
    assert delete(srv, "d")[0] == 404


def test_delete_refuses_the_root_and_escapes(srv, ws, tmp_path):
    write(ws, {"a.md": "", "sub/b.md": "", ".git/HEAD": ""})
    keep = tmp_path / "keep.txt"
    keep.write_text("K")
    for path in ("", ".", "sub/..", "../keep.txt", str(keep), "..", ".git", ".git/HEAD"):
        status, res = delete(srv, path)
        assert status == 400, (path, res)
    assert keep.exists() and (ws / "a.md").exists() and (ws / ".git/HEAD").exists()


def test_delete_a_symlink_leaves_its_target(srv, ws, tmp_path):
    outside = tmp_path / "outside"
    write(outside, {"precious.txt": "P", "sub/also.txt": "A"})
    os.symlink(outside, ws / "dirlink")
    os.symlink(outside / "precious.txt", ws / "filelink")
    assert delete(srv, "dirlink") == (200, {"ok": True, "files": 1, "dirs": 0})
    assert delete(srv, "filelink")[0] == 200
    assert not os.path.lexists(ws / "dirlink") and not os.path.lexists(ws / "filelink")
    assert (outside / "precious.txt").read_text() == "P" and (outside / "sub/also.txt").exists()


# ---------------------------------------------------------------- allfiles

def test_allfiles_by_walking(srv, ws):
    write(ws, {"b.md": "", "a/z.py": "", ".env": "", ".hidden/x": "", "node_modules/m.js": "", "venv/bin/py": ""})
    status, res = get(srv, "/api/allfiles")
    assert status == 200 and res == {"files": [".env", "a/z.py", "b.md"], "truncated": False}


def test_allfiles_in_git_lists_tracked_and_untracked_not_ignored_or_deleted(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {".gitignore": "*.log\n", "sub/t.md": "", "sub/gone.md": "", "top.txt": "",
                                         ".github/ci.yml": ""})
    write(repo, {"sub/u.py": "", "sub/x.log": "", "sub/.dot/d.txt": ""})
    (repo / "sub/gone.md").unlink()
    open_root(srv, repo)
    assert get(srv, "/api/allfiles")[1] == {
        "files": [".github/ci.yml", ".gitignore", "sub/.dot/d.txt", "sub/t.md", "sub/u.py", "top.txt"], "truncated": False}
    open_root(srv, repo / "sub")
    assert get(srv, "/api/allfiles")[1]["files"] == [".dot/d.txt", "t.md", "u.py"], "ROOT-relative, ROOT only"


def test_allfiles_cap(monkeypatch, tmp_path):
    write(tmp_path, {f"f{i}.txt": "" for i in range(5)})
    monkeypatch.setattr(S, "ROOT", tmp_path.resolve())
    monkeypatch.setattr(S, "ALLFILES_MAX", 3)
    assert S.api_allfiles({}) == {"files": ["f0.txt", "f1.txt", "f2.txt"], "truncated": True}


# ---------------------------------------------------------------- guard

def test_new_endpoints_are_guarded(srv, ws):
    write(ws, {"a.md": ""})
    for path in ("/api/stat", "/api/new", "/api/rename", "/api/delete", "/api/git/discard"):
        status, res = srv.post(path, {"path": "a.md", "from": "a.md", "to": "b.md"}, origin=None)
        assert status == 403, path
        status, res = srv.post(path, {"path": "a.md", "from": "a.md", "to": "b.md"}, origin="http://evil.example")
        assert status == 403, path
    for path in ("/api/tree", "/api/file?path=a.md", "/api/allfiles", "/api/search?q=a", "/api/git/info",
                 "/api/git/status", "/api/git/show?path=a.md"):
        status, _ = srv.get(path, host="evil.example")
        assert status == 403, path
    assert (ws / "a.md").exists()
