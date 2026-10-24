"""The page and its assets must never be served stale (a cached old app.js broke the UI)."""

from fastapi.testclient import TestClient

from researchai import server


def test_index_versions_assets_and_disables_caching():
    client = TestClient(server.app)
    page = client.get("/")
    assert page.status_code == 200
    assert "/static/app.js?v=" in page.text and "/static/style.css?v=" in page.text
    assert page.headers["cache-control"] == "no-cache"


def test_static_files_must_be_revalidated():
    client = TestClient(server.app)
    js = client.get("/static/app.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert "etag" in js.headers  # unchanged files still come back as cheap 304s
