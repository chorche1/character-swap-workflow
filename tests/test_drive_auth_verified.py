"""Drive write auth is VERIFIED, not merely present (2026-07-26).

Regression for the dead-end Hugo hit: the drive.file refresh token was revoked
by Google (`invalid_grant: Token has been expired or revoked`), but
`write_status()["ready"]` only checked that the token FILE existed, so
/api/health reported `drive_write_ready: true`. The Editor hides its "click
here to authorize" link on ready=true and the export modal had no 409→bootstrap
self-heal, so the upload failed with a raw error toast and no way back.

Two locks here:
  1. `ready` reflects credentials that actually load/refresh (and is cached, so
     /api/health — which init() awaits — never turns into a per-call network
     round-trip).
  2. The Editor's export modal retries once after the one-time OAuth, the same
     self-heal `drivePush` / `drivePushAll` already had.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from character_swap.clients import google_drive


APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


@pytest.fixture
def drive_files(tmp_path, monkeypatch):
    """Both on-disk artifacts present — the exact state that used to report
    ready=True regardless of whether the token still worked."""
    creds = tmp_path / "credentials.json"
    token = tmp_path / "drive_write_token.json"
    creds.write_text("{}")
    token.write_text("{}")
    monkeypatch.setattr(google_drive, "_credentials_path", lambda: creds)
    monkeypatch.setattr(google_drive, "_write_token_path", lambda: token)
    google_drive.invalidate_write_auth_cache()
    yield creds, token
    google_drive.invalidate_write_auth_cache()


def _patch_loader(monkeypatch, result, counter=None):
    """Stand in for _load_credentials; `result` may be a value or an exception
    instance to raise."""
    def _fake(**kw):
        if counter is not None:
            counter.append(kw)
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(google_drive, "_load_credentials", _fake)


# ---------------------------------------------------- verified `ready` flag

def test_ready_false_when_refresh_token_revoked(drive_files, monkeypatch):
    """The live failure: files on disk, but the token can't be refreshed.
    `_load_credentials(interactive=False)` swallows the RefreshError and
    returns None — `ready` must follow it, not the file listing."""
    _patch_loader(monkeypatch, None)
    st = google_drive.write_status()
    assert st["credentials_present"] is True
    assert st["token_present"] is True, "the file IS there — that's the trap"
    assert st["ready"] is False


def test_ready_true_when_credentials_load(drive_files, monkeypatch):
    _patch_loader(monkeypatch, object())
    assert google_drive.write_status()["ready"] is True


def test_ready_false_when_loader_raises(drive_files, monkeypatch):
    """Never let a Google/network exception escape into /api/health — an
    unusable token and a broken check are the same answer for the caller."""
    _patch_loader(monkeypatch, RuntimeError("network down"))
    assert google_drive.write_status()["ready"] is False


def test_ready_false_when_token_file_missing(drive_files, monkeypatch):
    """No token at all → don't even attempt a load."""
    creds, token = drive_files
    token.unlink()
    calls: list = []
    _patch_loader(monkeypatch, object(), counter=calls)
    assert google_drive.write_status()["ready"] is False
    assert calls == [], "missing token must short-circuit before any load"


def test_write_auth_uses_write_scope_and_never_prompts(drive_files, monkeypatch):
    """A request thread must never block inside run_local_server waiting for a
    browser consent on the server machine."""
    calls: list = []
    _patch_loader(monkeypatch, object(), counter=calls)
    google_drive.write_auth_usable()
    assert calls, "expected a credential load"
    assert calls[0]["interactive"] is False
    assert calls[0]["scopes"] == google_drive.DRIVE_FILE_SCOPE


# ------------------------------------------------------------------ caching

def test_verdict_is_cached_across_calls(drive_files, monkeypatch):
    """init() awaits /api/health — repeated checks must not each hit Google."""
    calls: list = []
    _patch_loader(monkeypatch, None, counter=calls)
    google_drive.write_status()
    google_drive.write_status()
    google_drive.write_status()
    assert len(calls) == 1


def test_cache_expires_after_ttl(drive_files, monkeypatch):
    calls: list = []
    _patch_loader(monkeypatch, None, counter=calls)
    assert google_drive.write_auth_usable() is False
    # Zero TTL forces a re-check — the cached verdict must not be permanent,
    # or a re-auth outside the app would never be picked up.
    assert google_drive.write_auth_usable(max_age_secs=0) is False
    assert len(calls) == 2


def test_bootstrap_invalidates_a_stale_negative(drive_files, monkeypatch):
    """After the user clicks through consent, health must flip to ready
    immediately — not after the TTL drains."""
    _patch_loader(monkeypatch, None)
    assert google_drive.write_status()["ready"] is False   # caches False

    monkeypatch.setattr(google_drive, "_write_service", lambda: object())
    _patch_loader(monkeypatch, object())
    result = google_drive.bootstrap_write_oauth()
    assert result["ok"] is True
    assert result["ready"] is True


def test_bootstrap_reports_failure_when_flow_declined(drive_files, monkeypatch):
    monkeypatch.setattr(google_drive, "_write_service", lambda: None)
    _patch_loader(monkeypatch, None)
    result = google_drive.bootstrap_write_oauth()
    assert result["ok"] is False
    assert result["ready"] is False


# ------------------------------------------- frontend self-heal (JS mirror)

def _js_function(name: str) -> str:
    """Source of an Alpine component method, brace-matched from its header."""
    src = APP_JS.read_text()
    start = src.index(f"async {name}(")
    # Skip the parameter list before hunting for the body's opening brace —
    # a default value like `payload = {}` would otherwise be read as the body.
    depth, k = 0, src.index("(", start)
    for j in range(k, len(src)):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                k = j
                break
    depth, i = 0, src.index("{", k)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


@pytest.mark.parametrize("fn", ["confirmDriveExport", "drivePush", "drivePushAll"])
def test_drive_paths_bootstrap_and_retry_on_409(fn):
    """Every Drive upload entry point self-heals a revoked token. Before this
    fix only the two push helpers did; the Editor's export modal dumped the raw
    409 body into a toast and stopped."""
    body = _js_function(fn)
    assert "409" in body, f"{fn} must branch on the auth 409"
    assert "/api/editor/drive_export/bootstrap" in body, \
        f"{fn} must run the one-time OAuth"
    # A bootstrap that never retries leaves the user clicking twice.
    assert len(re.findall(r"await post\(\)", body)) >= 2, \
        f"{fn} must retry the request after authorizing"


@pytest.mark.parametrize("fn", ["confirmDriveExport", "drivePush", "drivePushAll"])
def test_drive_paths_refresh_health_after_bootstrap(fn):
    """health.drive_write_ready gates the export modal's button and its
    authorize nag; it's loaded once at init, so a re-auth must refresh it or
    the UI stays stale until a page reload."""
    assert "loadHealth" in _js_function(fn)


def test_confirm_drive_export_surfaces_detail_not_raw_body():
    """The old toast read `Drive upload failed: {"detail":"..."}` — JSON at the
    user. Errors go through the shared detail extractor now."""
    body = _js_function("confirmDriveExport")
    assert "j.detail" in body
    assert "await r.text()" not in body
