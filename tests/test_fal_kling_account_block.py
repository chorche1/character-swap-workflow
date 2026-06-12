"""Backlog #6 (2026-06-12): fal account-error classification + circuit
breaker. calls.jsonl forensics: 47 doomed Kling submits fired in 6 minutes,
every one failing with 'Exhausted balance / User is locked'. One account-
level rejection now (a) raises the distinct FalAccountError with an
actionable billing message, and (b) trips a process-wide block so sibling
submits in the same batch fail FAST instead of re-burning uploads/submits.
Transient errors (5xx etc.) never trip the block.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from character_swap.clients import fal_kling


@pytest.fixture(autouse=True)
def _reset_block():
    fal_kling._account_block.update(until=0.0, reason="")
    yield
    fal_kling._account_block.update(until=0.0, reason="")


def _wire(monkeypatch, upload_error: Exception | None = None,
          submit_error: Exception | None = None):
    uploads: list = []
    submits: list = []

    def upload_file(p):
        uploads.append(p)
        if upload_error:
            raise upload_error
        return "https://fal.example/img.png"

    def submit(endpoint, arguments):
        submits.append(arguments)
        if submit_error:
            raise submit_error
        return SimpleNamespace(request_id="req_1")

    monkeypatch.setattr(fal_kling, "_client", lambda: SimpleNamespace(
        upload_file=upload_file, submit=submit))
    return uploads, submits


def _submit(tmp_path):
    return fal_kling.submit_image_to_video(
        image=tmp_path / "frame.png", prompt="p", duration_secs=5)


def test_balance_error_raises_account_error_and_trips_block(monkeypatch, tmp_path):
    uploads, _ = _wire(monkeypatch, submit_error=RuntimeError(
        "Exhausted balance: User is locked. Top up your balance"))

    with pytest.raises(fal_kling.FalAccountError, match="cannot accept work"):
        _submit(tmp_path)
    assert len(uploads) == 1

    # Sibling submit in the same batch: fails fast, no upload, no submit.
    with pytest.raises(fal_kling.FalAccountError, match="paused"):
        _submit(tmp_path)
    assert len(uploads) == 1                # nothing new hit the API


def test_block_expires_and_submits_resume(monkeypatch, tmp_path):
    uploads, submits = _wire(monkeypatch)
    fal_kling._account_block.update(
        until=fal_kling.time.monotonic() - 1, reason="old")

    assert _submit(tmp_path) == "req_1"     # expired block → normal flow
    assert len(submits) == 1


def test_transient_error_does_not_trip_block(monkeypatch, tmp_path):
    uploads, _ = _wire(monkeypatch, submit_error=RuntimeError(
        "500 internal server error"))

    with pytest.raises(RuntimeError, match="submit failed"):
        _submit(tmp_path)
    # Next attempt still reaches the API — no block was set.
    with pytest.raises(RuntimeError, match="submit failed"):
        _submit(tmp_path)
    assert len(uploads) == 2


def test_account_error_on_upload_also_trips(monkeypatch, tmp_path):
    uploads, _ = _wire(monkeypatch, upload_error=RuntimeError(
        "403: Insufficient credits"))

    with pytest.raises(fal_kling.FalAccountError):
        _submit(tmp_path)
    with pytest.raises(fal_kling.FalAccountError, match="paused"):
        _submit(tmp_path)
    assert len(uploads) == 1
