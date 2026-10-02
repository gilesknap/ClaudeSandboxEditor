"""Find in files: /api/search with git grep inside a work tree and a Python walk elsewhere. Both
engines get the same files and must give the same answers: case, whole word, regex, include
globs, the 2,000-match cap, invalid regexes, binary files and ROOT-relative paths."""
import io
import os
import socket
import threading
import time
import warnings
from urllib.parse import urlencode

import pytest

from gitutil import SERVER_ENV, git, make_repo, open_root, write
from helpers import marked_pids

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


def test_bad_patterns_and_globs_get_an_answer(srv, ws):
    """Whatever the query, the page gets a JSON answer: re.compile's OverflowError and
    RecursionError, a bad [...] range in a glob and a thousand {…} groups used to drop the
    connection with a traceback."""
    engine, _ = ws
    for q in ("a{4294967296}", "(" * 2000 + "a" + ")" * 2000):
        if engine == "git" and not S.git_pcre() and q.startswith("("):
            continue  # git -E may take it
        assert search(srv, engine, q, status=400, regex=True)["error"].startswith("invalid regular expression")
    if engine == "walk":
        assert search(srv, engine, "foo", status=400, glob="[z-a]")["error"].startswith("invalid include glob")
    else:
        search(srv, engine, "foo", glob="[z-a]")  # git's own globs
    assert search(srv, engine, "foo", glob="{a}" * 1000)["results"] == []


# ---------------------------------------------------------------- engine-specific

def test_git_engine_skips_ignored_and_keeps_paths_root_relative(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {".gitignore": "*.log\n", "top.txt": "foo\n", "sub/t.txt": "foo\n"})
    git(repo, "config", "grep.fullName", "true")  # must not make paths top-relative
    write(repo, {"sub/new.txt": "foo\n", "sub/x.log": "foo\n", "sub/.hidden/h.txt": "foo\n"})
    open_root(srv, repo / "sub")
    res = search(srv, "git", "foo")
    assert sorted(h["path"] for h in res["results"]) == [".hidden/h.txt", "new.txt", "t.txt"]


def test_git_engine_searches_tracked_files_that_match_an_ignore_rule(srv, tmp_path):
    """git grep --untracked leaves out tracked files that an ignore rule matches (force-added
    ones, or ones a global ignore names): tracked and untracked files are searched apart."""
    repo = make_repo(tmp_path / "repo", {".gitignore": "build/\n*.log\n", "a.md": "needle\n"})
    write(repo, {"build/keep.md": "needle\n", "important.log": "needle\n"})
    git(repo, "add", "-f", "build/keep.md", "important.log")
    git(repo, "commit", "-q", "-m", "forced")
    write(repo, {"untracked.md": "needle\n", "x.log": "needle\n", "build/out.md": "needle\n"})
    open_root(srv, repo)
    for regex in (False, True):
        res = search(srv, "git", "needle", regex=regex)
        assert sorted(h["path"] for h in res["results"]) == ["a.md", "build/keep.md", "important.log", "untracked.md"]
    res = search(srv, "git", "needle", glob="*.md")
    assert sorted(h["path"] for h in res["results"]) == ["a.md", "build/keep.md", "untracked.md"]


def test_git_engine_skips_files_over_5_mb(srv, tmp_path):
    repo = make_repo(tmp_path / "repo", {"a.txt": "needle\n"})
    with open(repo / "big.txt", "wb") as f:  # one huge line, as a minified bundle has
        f.write(b"needle " * (S.TEXT_MAX // 7 + 1) + b"\n")
    open_root(srv, repo)
    assert [h["path"] for h in search(srv, "git", "needle")["results"]] == ["a.txt"]


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


# ---------------------------------------------------------------- a regex that never ends

BACKTRACK = r"^\S|(\w+\s?)+$"   # matches at once for git's PCRE; Python's re backtracks for hours


@pytest.fixture(scope="module")
def quick_srv(shared_servers):
    """A server whose searches give up after 3 s."""
    return shared_servers.start(env=dict(SERVER_ENV, MDEDIT_SEARCH_TIMEOUT="3"))


@pytest.mark.parametrize("engine", ["git", "walk"])
def test_a_backtracking_regex_does_not_hold_up_the_server(quick_srv, tmp_path, engine):
    """A regex search runs in a child process: other requests carry on, and the search ends with
    a 504 at the time limit, its process killed (the fixture checks nothing is left running)."""
    root = tmp_path / "ws"
    files = {"prose.md": "word " * 40 + ".\n"}
    make_repo(root, files) if engine == "git" else write(root, files)
    open_root(quick_srv, root)
    out = {}
    t = threading.Thread(target=lambda: out.update(res=quick_srv.get("/api/search?" + urlencode({"q": BACKTRACK, "regex": "1"}))))
    start = time.monotonic()
    t.start()
    time.sleep(0.5)
    for _ in range(3):
        t0 = time.monotonic()
        assert quick_srv.post("/api/stat", {"paths": ["prose.md"]})[0] == 200
        assert time.monotonic() - t0 < 1, "the server is stuck behind the search"
    t.join(20)
    assert out["res"][0] == 504 and out["res"][1]["error"] == "the search took too long"
    assert time.monotonic() - start < 10
    if marked_pids(quick_srv.tag):
        deadline = time.monotonic() + 5
        while len(marked_pids(quick_srv.tag)) > 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert len(marked_pids(quick_srv.tag)) == 1, "only the server itself is left"


def test_a_search_stops_when_the_page_gives_up_on_it(srv, tmp_path):
    """search.js drops a search that a newer one replaces: the server notices the closed
    connection and kills the search's process rather than letting it run for its full minute."""
    root = tmp_path / "ws"
    write(root, {"prose.md": "word " * 40 + ".\n"})
    open_root(srv, root)
    if not marked_pids(srv.tag):
        pytest.skip("needs /proc to see the search's process")
    sock = socket.create_connection(("127.0.0.1", srv.port))
    sock.sendall(f"GET /api/search?{urlencode({'q': BACKTRACK, 'regex': '1'})} HTTP/1.1\r\nHost: {srv.host}\r\n\r\n".encode())
    deadline = time.monotonic() + 10
    while len(marked_pids(srv.tag)) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(marked_pids(srv.tag)) >= 2, "the search's process started"
    sock.close()
    deadline = time.monotonic() + 5
    while len(marked_pids(srv.tag)) > 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(marked_pids(srv.tag)) == 1, "and was killed when the page went away"
    assert srv.get("/api/search?q=word")[0] == 200


def test_a_plain_text_walk_stops_when_the_page_gives_up_on_it(tmp_path, monkeypatch):
    """A plain-text search walks in md-editor's own process: it checks its client as it goes,
    so a search a newer one replaced doesn't read the rest of the folder."""
    write(tmp_path, {f"d{i}/f{j}.txt": "foo\n" for i in range(3) for j in range(3)})
    monkeypatch.setattr(S, "ROOT", tmp_path.resolve())
    pat = S.search_pattern("foo", False, False, False)
    mine, page = socket.socketpair()
    try:
        monkeypatch.setattr(S.REQUEST, "conn", mine, raising=False)
        assert len(list(S.grep_walk(pat, [], False, time.monotonic() + 60))) == 9, "while the page waits"
        page.close()
        with pytest.raises(S.ApiError) as exc:
            list(S.grep_walk(pat, [], False, time.monotonic() + 60))
        assert exc.value.status == 499
    finally:
        mine.close()
        page.close()


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
    assert S.glob_parts("x{a}" * 1000) == [("**/" + "xa" * 1000, False)], "a loop, not a RecursionError"
    assert S.expand_braces("{a,b}{c,d}") == ["ac", "ad", "bc", "bd"]
    with warnings.catch_warnings():  # no "possible nested set" FutureWarnings from re
        warnings.simplefilter("error")
        for g, yes, no in (("[&&]", "&", "a"), ("[a-]", "-", "b"), ("[[:]", ":", "a"), ("[!--]", "a", "-"),
                           ("[\\]", "\\", "a"), ("[~~||]", "|", "a")):
            assert match(g, yes) and not match(g, no), g
    with pytest.raises(S.re.error):
        S.glob_re("[z-a]")


def test_grep_records_reads_long_lines_in_one_pass():
    class Stream:   # a pipe that hands over a few KiB at a time
        def __init__(self, data, n):
            self.f, self.n = io.BytesIO(data), n

        def read1(self, _):
            return self.f.read(self.n)
    out = b"a.txt\x001\x003\x00foo\nsub/b\x0012\x001\x00bar baz\n"
    for n in range(1, 9):   # every way of splitting the records between reads
        assert list(S.grep_records(Stream(out, n))) == [(b"a.txt", b"1", b"3", b"foo"), (b"sub/b", b"12", b"1", b"bar baz")]
    big = b"x\x001\x001\x00" + b"y" * (30 << 20) + b"\nz\x002\x001\x00end\n"
    t = time.monotonic()
    recs = list(S.grep_records(Stream(big, 4096)))
    assert time.monotonic() - t < 5, "linear, not quadratic, in the line's length"
    assert [(r[0], len(r[3])) for r in recs] == [(b"x", S.LINE_MAX), (b"z", 3)]


def test_search_pattern_and_spans():
    assert S.search_pattern(r"(?<n>a)\k<n>", True, True, False).pattern == r"(?P<n>a)(?P=n)"
    assert S.search_pattern(r"(?<=a)b(?<!c)", True, True, False).pattern == r"(?<=a)b(?<!c)"
    assert S.line_spans(S.search_pattern("o", False, False, False), "foo") == [(1, 2), (2, 3)]
    assert S.line_spans(S.search_pattern("^", True, False, False), "abc") == [(0, 0)]
    assert S.line_spans(S.search_pattern("z", False, False, False), "abc") is None
    assert len(S.line_spans(S.search_pattern("a", False, False, False), "a" * 10000)) == S.SEARCH_MAX + 1
    assert S.js_len("a\U0001F642b") == 4
