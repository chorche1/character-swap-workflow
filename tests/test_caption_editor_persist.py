"""Visual caption editor persistence — POST /api/editor/rerender + words_json.

2026-07-01 audit gap #9: this path overwrites the edit's canonical words.json
(with a one-time words.original.json backup) so ALL future rerenders inherit
the user's manual per-word caption edits. Zero tests referenced words_json
before — a regression here silently reverts hours of timing edits on the next
rerender, or destroys the original transcript backup.

Locked here:
  - words.json is overwritten with the posted edits;
  - words.original.json is created ONCE with the ORIGINAL transcript and a
    second edit does NOT clobber it;
  - the render call receives the EDITED words (not the stale cache);
  - a follow-up rerender WITHOUT words_json inherits the persisted edits;
  - malformed words_json → 400, words.json untouched;
  - empty-list words_json → 422, words.json untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from character_swap import api, video_edit
from character_swap.config import settings

client = TestClient(api.app)

_ORIG_WORDS = [
    {"text": "hello", "start": 0.0, "end": 0.4},
    {"text": "world", "start": 0.4, "end": 0.9},
]
_EDIT_1 = [
    {"text": "HELLO", "start": 0.0, "end": 0.5},
    {"text": "world", "start": 0.5, "end": 1.1},
]
_EDIT_2 = [
    {"text": "goodbye", "start": 0.2, "end": 0.8},
]


@pytest.fixture
def edit_dir(tmp_path: Path) -> tuple[str, Path]:
    """A fake finished auto-edit under the ISOLATED output dir: cached
    transcript + pre-caption video pointer, exactly what rerender expects."""
    edit_id = f"ed_captest_{tmp_path.name[-8:]}"
    d = settings.output_dir / "editor" / edit_id
    d.mkdir(parents=True, exist_ok=False)
    (d / "words.json").write_text(json.dumps(_ORIG_WORDS), encoding="utf-8")
    pre = d / "02-pre-caption.mp4"
    pre.write_bytes(b"\x00fake-video-bytes")
    (d / "pre_caption.txt").write_text(str(pre), encoding="utf-8")
    return edit_id, d


@pytest.fixture
def render_spy(monkeypatch) -> list[list[dict]]:
    """Stub the actual caption render; record the words each call received."""
    calls: list[list[dict]] = []

    def fake_render(input_video, output_video, *, words, style, job_id=None):
        calls.append([{"text": w.text, "start": w.start, "end": w.end}
                      for w in words])
        Path(output_video).write_bytes(b"rendered")
        return {"engine": "stub"}

    monkeypatch.setattr(video_edit, "render_captions", fake_render)
    return calls


def _post(edit_id: str, words_json: str | None = None, **extra) -> object:
    data = {"edit_id": edit_id, "template": "minimal", **extra}
    if words_json is not None:
        data["words_json"] = words_json
    return client.post("/api/editor/rerender", data=data)


# --- persistence ---------------------------------------------------------------------


def test_words_json_overwrites_cache_and_backs_up_original(edit_dir, render_spy):
    edit_id, d = edit_dir
    r = _post(edit_id, json.dumps(_EDIT_1))
    assert r.status_code == 200, r.text

    # words.json now holds the EDIT.
    assert json.loads((d / "words.json").read_text()) == _EDIT_1
    # The one-time backup holds the ORIGINAL transcript.
    backup = d / "words.original.json"
    assert backup.exists()
    assert json.loads(backup.read_text()) == _ORIG_WORDS
    # The render received the edited words, not the stale cache.
    assert render_spy[-1] == _EDIT_1
    assert r.json()["n_words"] == len(_EDIT_1)


def test_second_edit_does_not_clobber_original_backup(edit_dir, render_spy):
    edit_id, d = edit_dir
    assert _post(edit_id, json.dumps(_EDIT_1)).status_code == 200
    assert _post(edit_id, json.dumps(_EDIT_2)).status_code == 200

    # Cache follows the LATEST edit...
    assert json.loads((d / "words.json").read_text()) == _EDIT_2
    # ...but the backup still holds the ORIGINAL words — not edit 1.
    assert json.loads((d / "words.original.json").read_text()) == _ORIG_WORDS
    assert render_spy[-1] == _EDIT_2


def test_rerender_without_words_json_inherits_persisted_edits(edit_dir, render_spy):
    """The whole point of persisting: a later plain rerender (template swap,
    no words_json) must render with the user's edits, not the pre-edit cache."""
    edit_id, d = edit_dir
    assert _post(edit_id, json.dumps(_EDIT_1)).status_code == 200

    r = _post(edit_id)                              # no words_json
    assert r.status_code == 200, r.text
    assert render_spy[-1] == _EDIT_1


# --- validation ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "not json at all",
    '{"text": "not-a-list"}',
    "[1, 2, 3]",
])
def test_malformed_words_json_400s_and_leaves_cache_untouched(
        edit_dir, render_spy, bad):
    edit_id, d = edit_dir
    r = _post(edit_id, bad)
    assert r.status_code == 400
    assert json.loads((d / "words.json").read_text()) == _ORIG_WORDS
    assert not (d / "words.original.json").exists()
    assert render_spy == []                         # never reached the render


def test_empty_words_json_422s_and_leaves_cache_untouched(edit_dir, render_spy):
    edit_id, d = edit_dir
    r = _post(edit_id, "[]")
    assert r.status_code == 422
    assert json.loads((d / "words.json").read_text()) == _ORIG_WORDS
    assert not (d / "words.original.json").exists()
    assert render_spy == []


def test_missing_edit_dir_404s(render_spy):
    r = _post("ed_never_existed", json.dumps(_EDIT_1))
    assert r.status_code == 404


def test_rerender_words_json_missing_key_is_400_not_500(edit_dir, render_spy):
    """A valid-JSON list whose dicts lack a required key (e.g. no "text")
    raised KeyError past the except tuple → 500. Must be the same 400 as
    other malformed payloads, leaving the words.json cache untouched."""
    edit_id, d = edit_dir
    before = (d / "words.json").read_text(encoding="utf-8")
    r = _post(edit_id, words_json=json.dumps([{"start": 0.0, "end": 1.0}]))
    assert r.status_code == 400
    assert (d / "words.json").read_text(encoding="utf-8") == before
    assert render_spy == []                     # nothing rendered
