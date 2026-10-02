"""The Host / Origin guard in front of every request: only the editor's own page, on a
loopback name and any port, may use the server."""
import pytest

BASH = "bash --norc --noprofile"


@pytest.fixture(scope="module")
def srv(shared_servers):
    """One server for the module; no test changes its state (nothing is spawned)."""
    (shared_servers.root / "pic.png").write_bytes(b"\x89PNG fake")
    return shared_servers.start(agent=BASH)


@pytest.fixture
def root(srv):
    return srv.root


def forbidden(status, body):
    return status == 403 and isinstance(body, dict) and body.get("error", "").startswith("forbidden: ")


# ---------------------------------------------------------------- Host

@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "LOCALHOST:{port}",
                                  "localhost:8766", "127.0.0.1", "localhost", "[::1]"])
def test_loopback_hosts_on_any_port_pass(srv, host):
    status, conf = srv.get("/api/config", host=host.format(port=srv.port))
    assert status == 200 and conf["root"] == str(srv.root)


@pytest.mark.parametrize("host", ["evil.example:{port}", "evil.example", "127.0.0.1.evil.example:{port}",
                                  "localhost.evil.example", "evil@localhost:{port}", "localhost:{port}@evil",
                                  "[::2]:{port}", "[::1", "0.0.0.0:{port}", "localhost:abc", ""])
def test_other_hosts_are_refused(srv, host):
    assert forbidden(*srv.get("/api/config", host=host.format(port=srv.port)))


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/file?path=a.md", "/raw/pic.png", "/api/files",
                                  "/api/browse?dir=/", "/api/events"])
def test_rebinding_host_cannot_read_anything(srv, path):
    """DNS rebinding: a page on evil.example resolved to 127.0.0.1 sends Host: evil.example."""
    assert forbidden(*srv.get(path, host=f"evil.example:{srv.port}"))
    if path != "/api/events":   # an endless stream
        status, _ = srv.get(path)
        assert status == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_other_sites_pages_cannot_use_the_api(srv, site):
    """Browsers send Sec-Fetch-Site: another site's <img> or <iframe> pointed at the API (a
    search that runs for a minute, say) or at a file is refused; the page itself may be linked to."""
    for path in ("/api/search?q=x", "/api/file?path=a.md", "/raw/pic.png", "/api/config"):
        assert forbidden(*srv.http("GET", path, headers=[("Host", srv.host), ("Sec-Fetch-Site", site)])), path
    assert srv.http("GET", "/", headers=[("Host", srv.host), ("Sec-Fetch-Site", site)])[0] == 200
    for ok in ("same-origin", "none"):   # the page's own requests; an address typed in
        assert srv.http("GET", "/api/config", headers=[("Host", srv.host), ("Sec-Fetch-Site", ok)])[0] == 200


def test_missing_or_repeated_host_is_refused(srv):
    assert forbidden(*srv.http("GET", "/api/config", headers=[]))
    two = [("Host", srv.host), ("Host", srv.host)]
    assert forbidden(*srv.http("GET", "/api/config", headers=two))


def test_head_is_guarded(srv):
    status, _ = srv.http("HEAD", "/", headers=[("Host", "evil.example")])
    assert status == 403
    status, _ = srv.http("HEAD", "/", headers=[("Host", srv.host)])
    assert status == 200


# ---------------------------------------------------------------- Origin

def test_get_with_matching_origin_passes(srv):
    status, _ = srv.get("/api/config", origin=srv.origin)
    assert status == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "https://{host}", "http://127.0.0.1:{other}",
                                    "http://localhost:{port}", "http://{host}/", ""])
def test_get_with_another_origin_is_refused(srv, origin):
    o = origin.format(host=srv.host, port=srv.port, other=srv.port + 1)
    assert forbidden(*srv.get("/api/config", origin=o))


def test_repeated_origin_is_refused(srv):
    h = [("Host", srv.host), ("Origin", srv.origin), ("Origin", srv.origin)]
    assert forbidden(*srv.http("GET", "/api/config", headers=h))


def test_post_needs_a_matching_origin(srv, root):
    assert forbidden(*srv.post("/api/new", {"path": "x.md"}, origin=None))
    assert forbidden(*srv.post("/api/new", {"path": "x.md"}, origin="http://evil.example"))
    assert forbidden(*srv.post("/api/new", {"path": "x.md"}, origin=f"http://localhost:{srv.port}"))
    assert not (root / "x.md").exists()
    status, body = srv.post("/api/new", {"path": "x.md"})
    assert status == 200 and body["path"] == "x.md" and (root / "x.md").exists()


def test_post_to_every_endpoint_is_guarded(srv, root):
    for path in ("/api/root", "/api/new", "/api/pdf", "/api/nonexistent"):
        assert forbidden(*srv.post(path, {"path": str(root)}, origin=None)), path


def test_put_needs_a_matching_origin(srv, root):
    body = {"path": "a.md", "text": "overwritten\n", "force": True}
    assert forbidden(*srv.put("/api/file", body, origin=None))
    assert forbidden(*srv.put("/api/file", body, origin="http://evil.example"))
    assert (root / "a.md").read_text() == "# a\n\nSome text.\n"
    status, res = srv.put("/api/file", body)
    assert status == 200 and "version" in res
    assert (root / "a.md").read_text() == "overwritten\n"


def test_forwarded_port_works_for_post_and_put(srv, root):
    """A devcontainer maps 8766 on the host to the server's port: Host and Origin say 8766."""
    host = "localhost:8766"
    status, _ = srv.post("/api/new", {"path": "fwd.md"}, host=host)
    assert status == 200 and (root / "fwd.md").exists()
    status, _ = srv.put("/api/file", {"path": "fwd.md", "text": "via 8766\n", "force": True}, host=host)
    assert status == 200 and (root / "fwd.md").read_text() == "via 8766\n"
    status, _ = srv.post("/api/new", {"path": "fwd2.md"}, host=host, origin=srv.origin)
    assert status == 403


def test_refusals_are_logged(srv):
    srv.get("/api/config", host="evil.example")
    assert "[guard] refused GET '/api/config'" in srv.logtext()


# ---------------------------------------------------------------- WebSocket

@pytest.mark.parametrize("kw", [dict(origin=None), dict(origin="http://evil.example"), dict(origin="null"),
                                dict(origin="https://{host}"), dict(origin="http://localhost:{port}"),
                                dict(host="evil.example:{port}"), dict(host="evil.example:{port}", origin="http://{host}")],
                         ids=["no-origin", "evil-origin", "null-origin", "https-origin", "other-local-origin",
                              "rebinding-host", "rebinding-host-local-origin"])
def test_websocket_refused(srv, kw):
    kw = {k: v.format(host=srv.host, port=srv.port) if v else v for k, v in kw.items()}
    c = srv.ws(**kw)
    assert c.code == 403, c.status_line
    assert b"forbidden: " in c.body


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "localhost:8766"])
def test_websocket_accepted_from_loopback_on_any_port(srv, host):
    c = srv.ws(host=host.format(port=srv.port))
    try:
        assert c.code == 101, c.status_line
        st = c.wait_status()
        assert st and st["state"] == "none"   # nothing spawned until the first resize
    finally:
        c.close()
