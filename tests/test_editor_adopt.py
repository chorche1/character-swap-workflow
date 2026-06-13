"""GET /api/editor/edit/{edit_id} — adopt an existing edit into the Editor
tab (Hugo 2026-06-13). Reengineer finals and Step-6 compiles are Editor
edits; adopting one enables caption re-render / word editing / timeline /
speed without re-billing. The NEWEST render wins (a later rerender must not
be shadowed by the original 04-final)."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi import HTTPException

from character_swap import api
from character_swap.config import settings


def _mk_edit(edit_id: str) -> "Path":
    d = settings.output_dir / "editor" / edit_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_adopt_returns_latest_render_and_words():
    d = _mk_edit("ed_adopt1")
    final = d / "04-final.mp4"
    final.write_bytes(b"old")
    rerender = d / "rerender-01.mp4"
    rerender.write_bytes(b"new")
    words = [{"text": "hej", "start": 0.0, "end": 0.4}]
    (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
    os.utime(final, (1_000_000_000, 1_000_000_000))
    os.utime(rerender, (2_000_000_000, 2_000_000_000))

    out = asyncio.run(api.get_editor_edit("ed_adopt1"))
    assert out["edit_id"] == "ed_adopt1"
    assert out["kind"] == "adopted"
    assert out["output_url"].endswith("rerender-01.mp4")   # newest wins
    assert out["words"] == words
    assert out["n_words"] == 1


def test_adopt_without_words_still_works():
    d = _mk_edit("ed_adopt2")
    (d / "04-final.mp4").write_bytes(b"v")
    out = asyncio.run(api.get_editor_edit("ed_adopt2"))
    assert out["output_url"].endswith("04-final.mp4")
    assert out["words"] == [] and out["n_words"] == 0


def test_adopt_unknown_edit_404():
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.get_editor_edit("ed_nope"))
    assert e.value.status_code == 404


def test_adopt_empty_dir_404():
    _mk_edit("ed_adopt3")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.get_editor_edit("ed_adopt3"))
    assert e.value.status_code == 404
