"""Find in files: /api/search with git grep inside a work tree and a Python walk elsewhere. Both
engines get the same files and must give the same answers: case, whole word, regex, include
globs, the 2,000-match cap, invalid regexes, binary files and ROOT-relative paths."""
import os
from urllib.parse import urlencode

import pytest

from gitutil import SERVER_ENV, git, make_repo, open_root, write

from md_collab_editor import server as S

FILES = {
    "a.txt": "Foo bar\nfoo baz\nfood\n",
    "notes.md": "x foo foo\n",
    "sub/b.py": "def foo():\n    return 'FOO'\n",
    "sub/deep/c.md": "a.b( literal\nnothing\r\n",
    "bin.dat": b"foo\0binary",
    "u.txt": "é\U0001F642 foo\n",
}


@pytest.fixture(scope="module")
def srv(shared_servers):
    return shared_servers.start(env=SERVER_ENV)


@pytest.fixture(params=["git", "walk"])
def ws(request, srv, tmp_path):
    """FILES in a fresh folder, a git repository (half of it untracked) or a plain one."""
    root = tmp_path / "ws"
    if request.param == "git":
        make_repo(root, {k: v for k, v in FILES.items() if k.startswith(("a", "sub/b"))})
        write(root, FILES)  # the rest stays untracked: git grep --untracked still finds it
    else:
        write(root, FILES)
    open_root(srv, root)
    return request.param, root.resolve()


def search(srv, engine, q, status=200, **flags):
    params = {"q": q}
    params.update({k: (v if isinstance(v, str) else "1" if v else "0") for k, v in flags.items()})
    code, res = srv.get("/api/search?" + urlencode(params))
    assert code == status, res
    if status == 200:
        assert res["engine"] == engine
    return res


def hits(res):
    return sorted((h["path"], h["line"], h["col"], h["len"]) for h in res["results"])


def test_case_insensitive_by_default_and_sensitive_on_request(srv, ws):
    engine, _ = ws
    res = search(srv, engine, "foo")
    assert hits(res) == [("a.txt", 1, 0, 3), ("a.txt", 2, 0, 3), ("a.txt", 3, 0, 3), ("notes.md", 1, 2, 3),
                         ("notes.md", 1, 6, 3), ("sub/b.py", 1, 4, 3), ("sub/b.py", 2, 12, 3), ("u.txt", 1, 4, 3)]
    assert res["truncated"] is False
    first = next(h for h in res["results"] if h["path"] == "sub/b.py" and h["line"] == 2)
    assert first == {"path": "sub/b.py", "line": 2, "col": 12, "len": 3, "text": "    return 'FOO'", "offset": 0}
    assert [h[:2] for h in hits(search(srv, engine, "FOO", case=True))] == [("sub/b.py", 2)]


def test_whole_word(srv, ws):
    engine, _ = ws
    assert ("a.txt", 3, 0, 3) not in hits(search(srv, engine, "foo", word=True))
    assert len(hits(search(srv, engine, "foo", word=True))) == 7


def test_regex_and_fixed_strings(srv, ws):
    engine, _ = ws
    assert hits(search(srv, engine, "fo+d", regex=True)) == [("a.txt", 3, 0, 4)]
    assert hits(search(srv, engine, r"ba[rz]$", regex=True, case=True)) == [("a.txt", 1, 4, 3), ("a.txt", 2, 4, 3)]
    if engine == "walk" or S.git_pcre():  # git's -E has no named groups
        assert hits(search(srv, engine, r"(?<x>o)\k<x>d", regex=True)) == [("a.txt", 3, 1, 3)], "JavaScript's names"
    assert hits(search(srv, engine, "a.b(")) == [("sub/deep/c.md", 1, 0, 4)], "not a regex unless asked"
    res = search(srv, engine, "a.b(", status=400, regex=True)
    assert res["error"].startswith("invalid regular expression")


def test_include_globs(srv, ws):
    engine, _ = ws

    def files(glob):
        return sorted({h["path"] for h in search(srv, engine, "o", glob=glob)["results"]})
    assert files("*.py") == ["sub/b.py"]
    assert files("sub/**") == ["sub/b.py", "sub/deep/c.md"]
    assert files("*.md, !sub/**") == ["notes.md"]
    assert files("*.{py,md}") == ["notes.md", "sub/b.py", "sub/deep/c.md"]
    assert files("deep") == ["sub/deep/c.md"], "a folder's name covers what is in it"
    assert files("!*.txt") == ["notes.md", "sub/b.py", "sub/deep/c.md"]
    assert files("/a.txt") == ["a.txt"]
    for bad in ("../*", "sub/../../x"):
        assert "cannot leave" in search(srv, engine, "o", status=400, glob=bad)["error"]


def test_matches_are_capped(srv, ws):
    engine, root = ws
    write(root, {"many.txt": "x\n" * (S.SEARCH_MAX + 100)})
    res = search(srv, engine, "x", case=True, glob="many.txt")
    assert len(res["results"]) == S.SEARCH_MAX and res["truncated"] is True
    res = search(srv, engine, "x", case=True, glob="many.txt", regex=True)
    assert res["truncated"] is True


def test_columns_count_utf16_and_long_lines_are_cut(srv, ws):
    engine, root = ws
    assert [h for h in hits(search(srv, engine, "foo")) if h[0] == "u.txt"] == [("u.txt", 1, 4, 3)], \
        "é is one unit, the emoji two"
    write(root, {"long.txt": "y" * 800 + "needle" + "z" * 300 + "\n", "short.txt": "ab needle\n"})
    long_hit, short_hit = sorted(search(srv, engine, "needle")["results"], key=lambda h: h["path"])
    assert (long_hit["col"], long_hit["len"], long_hit["offset"], len(long_hit["text"])) == (800, 6, 700, 300)
    s = long_hit["col"] - long_hit["offset"]
    assert long_hit["text"][s:s + long_hit["len"]] == "needle"
    assert (short_hit["text"], short_hit["offset"]) == ("ab needle", 0)


def test_binary_files_are_skipped_and_crlf_is_trimmed(srv, ws):
    engine, _ = ws
    assert "bin.dat" not in {h["path"] for h in search(srv, engine, "foo")["results"]}
    assert [h["text"] for h in search(srv, engine, "nothing")["results"]] == ["nothing"]


def test_empty_and_multiline_queries(srv, ws):
    engine, _ = ws
    assert search(srv, engine, "") == {"results": [], "truncated": False, "engine": engine}
    search(srv, engine, "a\nb", status=400)


# ---------------------------------------------------------------- engine-specific

def test_git_engine_skips_ignored_and_keeps_paths_root_relative(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {".gitignore": "*.log\n", "top.txt": "foo\n", "sub/t.txt": "foo\n"})
    git(repo, "config", "grep.fullName", "true")  # must not make paths top-relative
    write(repo, {"sub/new.txt": "foo\n", "sub/x.log": "foo\n", "sub/.hidden/h.txt": "foo\n"})
    open_root(srv, repo / "sub")
    res = search(srv, "git", "foo")
    assert sorted(h["path"] for h in res["results"]) == [".hidden/h.txt", "new.txt", "t.txt"]


def test_git_engine_falls_back_to_gits_column_for_pcre_only_syntax(srv, tmp_path):
    if not S.git_pcre():
        pytest.skip("this git has no PCRE")
    repo = make_repo(tmp_path / "repo", {"a.txt": "x foo bar\n"})
    open_root(srv, repo)
    res = search(srv, "git", r"foo\h", regex=True)  # \h: horizontal space in PCRE, an error in Python
    assert hits(res) == [("a.txt", 1, 2, 0)]


def test_walk_engine_skips_hidden_skip_dirs_and_big_files(srv, tmp_path):
    root = tmp_path / "plain"
    write(root, {"ok.txt": "foo\n", ".hidden/h.txt": "foo\n", "node_modules/m.js": "foo\n", ".env": "foo\n"})
    with open(root / "big.txt", "wb") as f:
        f.write(b"foo\n")
        f.truncate(S.TEXT_MAX + 1)
    os.symlink(root / "ok.txt", root / "link.txt")
    os.mkfifo(root / "pipe.txt")   # reading it would block the search for ever
    open_root(srv, root)
    assert sorted(h["path"] for h in search(srv, "walk", "foo")["results"]) == [".env", "ok.txt"]
    assert search(srv, "walk", "a(", status=400, regex=True)["error"].startswith("invalid regular expression")


def test_a_git_ignored_root_is_walked(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {".gitignore": "scratch/\n", "a.txt": "foo\n"})
    write(repo, {"scratch/s.txt": "foo\n"})
    open_root(srv, repo / "scratch")
    assert [h["path"] for h in search(srv, "walk", "foo")["results"]] == ["s.txt"]
    assert srv.get("/api/allfiles")[1] == {"files": ["s.txt"], "truncated": False}


# ---------------------------------------------------------------- units

def test_globs():
    assert S.glob_parts("*.py, src/**, !*.min.js,  ,./a/b.txt, *.{c,h}") == [
        ("**/*.py", False), ("src/**", False), ("**/*.min.js", True), ("a/b.txt", False), ("**/*.c", False),
        ("**/*.h", False)]

    def match(glob, path):
        return bool(S.glob_re(glob).fullmatch(path))
    assert match("**/*.py", "x.py") and match("**/*.py", "a/b/x.py") and not match("**/*.py", "x.pyc")
    assert match("src/**", "src/a/b") and not match("src/**", "lib/src/a")
    assert match("a/*/c", "a/b/c") and not match("a/*/c", "a/b/x/c")
    assert match("a/**/c", "a/c") and match("a/**/c", "a/b/x/c")
    assert match("**/deep", "sub/deep/c.md") and match("f?.[ch]", "f1.c") and not match("f?.[!ch]", "f1.c")
    with pytest.raises(ValueError):
        S.glob_parts("{" + ",".join("abcdefghij") + "}{" + ",".join("abcdefghij") + "}")


def test_search_pattern_and_spans():
    assert S.search_pattern(r"(?<n>a)\k<n>", True, True, False).pattern == r"(?P<n>a)(?P=n)"
    assert S.search_pattern(r"(?<=a)b(?<!c)", True, True, False).pattern == r"(?<=a)b(?<!c)"
    assert S.line_spans(S.search_pattern("o", False, False, False), "foo") == [(1, 2), (2, 3)]
    assert S.line_spans(S.search_pattern("^", True, False, False), "abc") == [(0, 0)]
    assert S.line_spans(S.search_pattern("z", False, False, False), "abc") is None
    assert S.js_len("a\U0001F642b") == 4
