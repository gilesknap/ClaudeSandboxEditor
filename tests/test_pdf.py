"""PDF export hardened against the sandboxed agent (SEC-GIT-002, SEC-GIT-003): the agent controls
every file name under ROOT and the HTML posted to /api/pdf. The print page must be rendered outside
ROOT, its image sources resolved so ../ or a symlink cannot reach a file outside ROOT, and the .pdf
written so a symlink planted at <name>.pdf cannot redirect the write out of ROOT. A fake Chrome (an
executable that only writes the --print-to-pdf target) stands in, since headless Chrome's print
subsystem is unavailable here; the writes under test all happen in md-editor, around Chrome."""
import os
import sys
from pathlib import Path

import pytest

from md_collab_editor import server as S

needs_posix = pytest.mark.skipif(os.name != "posix", reason="O_NOFOLLOW / shebang fakes need POSIX")

# writes the fake PDF to the --print-to-pdf target, and (if asked) a copy of the print HTML it was
# handed, so the test can inspect what would have been rendered
FAKE_CHROME = """#!{python}
import sys, os
from urllib.parse import urlparse
from urllib.request import url2pathname
out = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--print-to-pdf=")), None)
if out:
    open(out, "wb").write(b"%PDF-1.4\\nfake\\n%%EOF\\n")
want = os.environ.get("FAKE_CHROME_HTML")
src = next((a for a in sys.argv if a.startswith("file://")), None)
if want and src:
    open(want, "w").write(open(url2pathname(urlparse(src).path)).read())
"""


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = (tmp_path / "root").resolve()
    r.mkdir()
    (r / "notes.md").write_text("# notes\n")
    monkeypatch.setattr(S, "ROOT", r)
    chrome = tmp_path / "fake-chrome"
    chrome.write_text(FAKE_CHROME.format(python=sys.executable))
    chrome.chmod(0o755)
    monkeypatch.setattr(S, "CHROME_BIN", str(chrome))
    return r


def test_pdf_image_src_is_resolved_and_cannot_leave_root(root):
    (root / "pic.png").write_bytes(b"\x89PNG")
    assert S.pdf_image_src(S.RAW_SRC_RE.match('src="/raw/pic.png"')) == f'src="{(root / "pic.png").as_uri()}"'
    assert S.pdf_image_src(S.RAW_SRC_RE.match('src="/raw/pic.png?v=123"')).endswith('pic.png"')
    # ../ traversal in the string is resolved and refused (not turned into file://ROOT/../...)
    assert S.pdf_image_src(S.RAW_SRC_RE.match('src="/raw/../../etc/passwd"')) == 'src=""'


@needs_posix
def test_pdf_image_src_refuses_a_symlink_pointing_outside(root, tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG secret")
    os.symlink(outside, root / "link.png")
    assert S.pdf_image_src(S.RAW_SRC_RE.match('src="/raw/link.png"')) == 'src=""'


@needs_posix
def test_pdf_export_writes_a_regular_file_in_root(root):
    res = S.export_pdf({"path": "notes.md", "html": "<p>hello</p>"})
    assert res["pdf"] == "notes.pdf" and res["bytes"] > 0
    pdf = root / "notes.pdf"
    assert pdf.is_file() and not pdf.is_symlink() and pdf.read_bytes().startswith(b"%PDF")


@needs_posix
def test_pdf_export_refuses_a_symlinked_output_path(root, tmp_path):
    victim = tmp_path / "authorized_keys"
    victim.write_text("# original\n")
    os.symlink(victim, root / "notes.pdf")   # the agent plants <name>.pdf -> a file outside ROOT
    with pytest.raises(RuntimeError):
        S.export_pdf({"path": "notes.md", "html": "<p>ssh-ed25519 AAAA attacker</p>"})
    assert victim.read_text() == "# original\n", "the write did not follow the symlink"
    assert (root / "notes.pdf").is_symlink(), "the planted symlink is left in place, not written through"


@needs_posix
def test_pdf_export_never_writes_the_temp_html_inside_root(root, tmp_path, monkeypatch):
    victim = tmp_path / "bashrc"
    victim.write_text("# original\n")
    os.symlink(victim, root / ".notes.print.html")   # the old temp path, planted as a symlink
    seen = tmp_path / "rendered.html"
    monkeypatch.setenv("FAKE_CHROME_HTML", str(seen))
    res = S.export_pdf({"path": "notes.md", "html": '<p>pwn</p><img src="/raw/../../etc/passwd">'})
    assert res["pdf"] == "notes.pdf"
    assert victim.read_text() == "# original\n", "the temp HTML was not written through the planted symlink"
    assert (root / ".notes.print.html").is_symlink()
    page = seen.read_text()
    # the page Chrome rendered carries a CSP that blocks scripts/frames/plugins, and the ../ image
    # was dropped rather than rewritten to a file:// URL reaching outside ROOT
    assert "default-src 'none'" in page and 'src=""' in page
    assert "/raw/" not in page and "etc/passwd" not in page
