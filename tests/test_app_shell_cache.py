"""Regression: app-shell files must not be heuristically cached stale.

2026-06-27 bug — `/` (index.html) and `/app.js` were served via plain
`FileResponse` with NO `Cache-Control` header. Browsers then apply HEURISTIC
caching and may serve a stale `app.js` WITHOUT revalidating. After a release a
freshly-reloaded index.html (new `🔁 Repurpose` button) paired with an old
cached app.js (missing `openRepurposeModal`), so clicking the button silently
did nothing. The fix sets `Cache-Control: no-cache` on the app-shell routes so
the browser always revalidates against the ETag (cheap 304 when unchanged).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from character_swap import api

client = TestClient(api.app)


def _cache_control(path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.headers.get("cache-control", "")


def test_index_html_must_revalidate():
    assert "no-cache" in _cache_control("/")


def test_app_js_must_revalidate():
    assert "no-cache" in _cache_control("/app.js")


def test_spa_deeplink_must_revalidate():
    # Reload on /j/<job_id> serves index.html — same shell, same rule.
    assert "no-cache" in _cache_control("/j/whatever")


def test_app_js_still_has_etag():
    # no-cache forces revalidation; the ETag is still emitted so any future
    # conditional-request support (or a proxy) can short-circuit a re-fetch.
    r = client.get("/app.js")
    assert r.headers.get("etag"), "FileResponse should still emit an ETag"
