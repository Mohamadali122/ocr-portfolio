"""The operational UI is served, and does not shadow the API."""

from pathlib import Path


def test_ui_is_served(client):
    res = client.get("/ui/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    body = res.text
    assert "local-doc-ocr console" in body
    assert 'href="css/app.css"' in body
    assert 'src="js/app.js"' in body


def test_ui_js_references_api_paths():
    # API paths live in the external script; keep this canary in sync with the shell.
    js = (Path(__file__).resolve().parents[2] / "app" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "/api/v1/auth/login" in js
    assert "/api/v1/documents" in js
    assert "/api/v1/admin/pipeline" in js
    assert "/api/v1/admin/reprocess-failed" in js


def test_ui_css_is_served(client):
    res = client.get("/ui/css/app.css")
    assert res.status_code == 200
    assert "text/css" in res.headers["content-type"]
    assert "--accent" in res.text


def test_ui_help_page_is_served(client):
    res = client.get("/ui/help.html")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    body = res.text
    assert "Help &amp; reference" in body or "Help & reference" in body
    assert 'href="css/app.css"' in body
    assert 'href="index.html"' in body


def test_ui_mount_does_not_shadow_health(client):
    # Mounting static at /ui must leave the unprefixed health probe reachable.
    assert client.get("/health").status_code == 200
