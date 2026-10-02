"""/raw/ serves the agent's own files, so it is locked down: a sandbox CSP and nosniff keep an
opened HTML/SVG file from scripting in the editor's origin, active types are sent as a download
unless they are an <img> subresource, and files are streamed rather than read whole into memory."""
import http.client

import pytest

from gitutil import SERVER_ENV, open_root


@pytest.fixture(scope="module")
def srv(shared_servers):
    return shared_servers.start(env=SERVER_ENV)


@pytest.fixture
def ws(srv, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    open_root(srv, root)
    return root


def raw(srv, path, dest=None, origin=None):
    """GET /raw/<path>; returns (status, {header: value}, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=30)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", srv.host)
        if dest is not None:
            conn.putheader("Sec-Fetch-Dest", dest)
        if origin is not None:
            conn.putheader("Origin", origin)
        conn.endheaders()
        r = conn.getresponse()
        body = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body
    finally:
        conn.close()


def test_image_is_served_inline_with_a_sandbox_and_nosniff(srv, ws):
    (ws / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
    status, h, body = raw(srv, "/raw/pic.png", dest="image")
    assert status == 200 and h["content-type"] == "image/png"
    assert body == b"\x89PNG\r\n\x1a\n" + b"x" * 40
    assert "sandbox" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in h, "an image renders inline"


def test_active_types_are_downloaded_when_opened_as_a_page(srv, ws):
    (ws / "evil.html").write_text("<script>fetch('/api/config')</script>")
    for dest in ("document", "iframe", None):
        status, h, _ = raw(srv, "/raw/evil.html", dest=dest)
        assert status == 200 and h.get("content-disposition") == "attachment", dest
        assert "sandbox" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"


def test_svg_renders_inline_as_an_image_but_downloads_as_a_page(srv, ws):
    (ws / "d.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
    _, as_img, _ = raw(srv, "/raw/d.svg", dest="image")
    assert "content-disposition" not in as_img, "the image viewer shows an SVG inline"
    _, as_page, _ = raw(srv, "/raw/d.svg", dest="document")
    assert as_page.get("content-disposition") == "attachment", "a navigation to the SVG downloads it"


def test_large_file_is_streamed_whole(srv, ws):
    data = bytes(range(256)) * 9000   # ~2.3 MB, > one read chunk
    (ws / "big.bin").write_bytes(data)
    status, h, body = raw(srv, "/raw/big.bin")
    assert status == 200 and int(h["content-length"]) == len(data) and body == data


def test_raw_rejects_a_null_origin(srv, ws):
    (ws / "pic.png").write_bytes(b"\x89PNG")
    status, _, _ = raw(srv, "/raw/pic.png", origin="null")
    assert status == 403, "a null Origin (a sandboxed document's fetch) is refused"


def test_app_page_has_a_site_wide_csp_but_json_and_raw_do_not_get_it(srv, ws):
    (ws / "pic.png").write_bytes(b"\x89PNG")
    _, index, _ = raw(srv, "/")
    assert "default-src 'self'" in index.get("content-security-policy", "")
    assert "https://cdnjs.cloudflare.com" in index["content-security-policy"]
    _, conf, _ = raw(srv, "/api/config")
    assert "content-security-policy" not in conf, "a JSON response needs no page CSP"
    _, pic, _ = raw(srv, "/raw/pic.png", dest="image")
    assert "sandbox" in pic["content-security-policy"] and "default-src 'self'" not in pic["content-security-policy"], \
        "a raw file keeps its own locked-down policy, not the app page's"


def test_download_keeps_an_agent_chosen_name_out_of_the_headers(srv, ws):
    """A PDF name holding CR/LF or quotes cannot inject a header; the file is streamed whole."""
    from urllib.parse import quote
    name = 'rpt"\r\nX-Injected: 1\r\n.pdf'
    data = b"%PDF-1.4\n" + bytes(range(256)) * 400
    (ws / name).write_bytes(data)
    status, h, body = raw(srv, "/api/download?path=" + quote(name, safe=""))
    assert status == 200 and h["content-type"] == "application/pdf"
    assert "x-injected" not in h
    assert h["content-disposition"] == "attachment; filename*=UTF-8''" + quote(name, safe="")
    assert body == data
