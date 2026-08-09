"""🔁 Repurpose: WHICH characters get a mirrored copy is a choice (Hugo
2026-08-10 — "i den här menyn vill jag kunna välja vilka karaktärer som ska få
repurpose för körningen").

Repurpose rebuilt the WHOLE cast, always. The Swap endpoint already accepted a
`char_ids` filter (built for retry-one-character) but the modal never sent it,
and the Reengineer path had no filter at all.

Hugo's calls: the picker opens with every character TICKED (a per-click filter,
never a remembered setting, so an unticked character can't carry silently into
a later repurpose), and it lives in the Repurpose modal only — Step-6 compile
and "Bygg ihop igen" are untouched.

What this locks:
  1. The filter reaches the Reengineer build — only the picked characters are
     rebuilt, and the unfiltered call still covers the whole cast.
  2. A filtered build MERGES into `repurposed`. Writing the bucket whole would
     delete every other character's earlier mirrored copy from the run card
     while its file sits on disk with nothing pointing at it.
  3. The filter reaches the TELEGRAM send. Because (2) merges, an unfiltered
     send would re-post the untouched copies to their channels on every partial
     repurpose — the defect `send_reengineer_finals` grew a `char_ids` for on
     2026-07-31.
  4. `char_ids` is a per-click filter, never persisted into `repurpose_settings`
     (it would otherwise silently narrow a later repurpose of the same run).
  5. An empty or unknown pick REFUSES LOUDLY on both endpoints instead of
     building nothing and landing `repurposing: False` with no card and no
     error.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from character_swap import api, auto_finalize, runner_reengineer
from character_swap.api import ReAssembleSettingsBody, _repurpose_char_ids
from character_swap.models import (
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)

_ROOT = Path(__file__).resolve().parents[1]


def _job(*char_ids: str) -> Job:
    chars = {}
    for cid in char_ids:
        chars[cid] = JobCharacter(
            char_id=cid, name=cid.upper(), source_image_path=f"/tmp/{cid}.png",
            images=[GeneratedImage(variant_id=f"var_{cid}",
                                   path=f"/tmp/var_{cid}.png", prompt="x",
                                   scene_id="sc1",
                                   status=VariantStatus.READY)],
            approved_variant_ids=[f"var_{cid}"],
            videos=[VideoVariant(video_id=f"vd_{cid}", grok_job_id="g",
                                 status=VideoStatus.DONE,
                                 source_variant_id=f"var_{cid}",
                                 final_video_path=f"/tmp/{cid}.mp4")])
    return Job(job_id="j1", title="t", scene_id="sc1",
               scene_image_path="/tmp/sc1.png", scene_ids=["sc1"],
               scene_image_paths=["/tmp/sc1.png"], characters=chars)


# --- 1. the filter reaches the build ---------------------------------------


def _run_do_repurpose(monkeypatch, state, *, char_ids, tmp_path, copies=None):
    """Run the real `_do_repurpose` with the per-character editor pipeline
    stubbed out, so only the SELECTION and the bucket write are under test.
    Returns (characters actually built, the kwargs handed to `_update`)."""
    job = _job("c1", "c2", "c3")
    monkeypatch.setattr(runner_reengineer, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job,
                                                get_character=lambda cid: None))
    # `_do_repurpose` re-reads the bucket from DISK before merging — that read
    # must see the run's stored state, not the snapshot it was handed.
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: dict(state))
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: tmp_path)
    monkeypatch.setattr(runner_reengineer, "_char_is_uninvolved",
                        lambda st, jc: False)
    monkeypatch.setattr(
        runner_reengineer, "_collect_clips",
        lambda st, jc: ([f"/tmp/{jc.char_id}.mp4"], ["hej"], [], False))
    written: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: written.update(kw))
    built: list[str] = []

    async def fake_pipeline(clips, **kw):
        # Which character we're in is recoverable from the single clip path.
        built.append(Path(clips[0]).stem)
        return SimpleNamespace(final=Path(clips[0]))
    monkeypatch.setattr(runner_reengineer.runner_compile, "run_editor_pipeline",
                        fake_pipeline)
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "_resolve_compile_voice", lambda *a, **k: None)
    # `copies` lets a caller inspect which files the build actually wrote.
    monkeypatch.setattr(
        runner_reengineer.shutil, "copyfile",
        lambda src, dst: None if copies is None
        else copies.append((str(src), str(dst))))

    asyncio.run(runner_reengineer._do_repurpose("re_t", dict(state),
                                                char_ids=char_ids))
    return sorted(built), written


def _state(**extra) -> dict:
    st = {"re_id": "re_t", "job_id": "j1", "status": "done", "scenes": []}
    st.update(extra)
    return st


def test_only_the_picked_characters_are_rebuilt(monkeypatch, tmp_path):
    """THE FEATURE."""
    built, _ = _run_do_repurpose(monkeypatch, _state(), char_ids=["c2"],
                                tmp_path=tmp_path)
    assert built == ["c2"]


def test_no_pick_still_rebuilds_the_whole_cast(monkeypatch, tmp_path):
    """The unfiltered path must be byte-for-byte the old behaviour."""
    built, _ = _run_do_repurpose(monkeypatch, _state(), char_ids=None,
                                tmp_path=tmp_path)
    assert built == ["c1", "c2", "c3"]


def test_empty_list_is_treated_as_no_filter_by_the_runner(monkeypatch,
                                                         tmp_path):
    """Defence in depth: the ENDPOINT refuses an empty pick (below), so if one
    ever reaches the runner it must not silently mean 'nobody'."""
    built, _ = _run_do_repurpose(monkeypatch, _state(), char_ids=[],
                                tmp_path=tmp_path)
    assert built == ["c1", "c2", "c3"]


# --- 2. a filtered build MERGES ---------------------------------------------


def test_filtered_build_keeps_the_other_characters_copies(monkeypatch,
                                                         tmp_path):
    """Without the merge, repurposing c2 alone DELETES c1's and c3's mirrored
    copies from the run card — the files stay on disk, orphaned."""
    prior = {
        "c1": {"status": "done", "final_path": "/tmp/old_c1.mp4"},
        "c3": {"status": "done", "final_path": "/tmp/old_c3.mp4"},
    }
    _, written = _run_do_repurpose(
        monkeypatch, _state(repurposed=dict(prior)), char_ids=["c2"],
        tmp_path=tmp_path)
    bucket = written["repurposed"]
    assert set(bucket) == {"c1", "c2", "c3"}
    assert bucket["c1"] == prior["c1"]        # untouched, verbatim
    assert bucket["c3"] == prior["c3"]
    assert bucket["c2"]["status"] == "done"   # rebuilt


def test_rebuilding_a_character_overwrites_its_own_entry(monkeypatch,
                                                        tmp_path):
    """The merge must let the NEW entry win — a stale 'failed' card surviving a
    successful rebuild would be worse than the bug it fixes."""
    _, written = _run_do_repurpose(
        monkeypatch,
        _state(repurposed={"c2": {"status": "failed", "error": "gammalt"}}),
        char_ids=["c2"], tmp_path=tmp_path)
    assert written["repurposed"]["c2"]["status"] == "done"
    assert "error" not in written["repurposed"]["c2"]


def test_unfiltered_build_still_replaces_the_bucket(monkeypatch, tmp_path):
    """A full repurpose keeps dropping a stale entry for a character no longer
    in the run — unchanged from before the picker."""
    _, written = _run_do_repurpose(
        monkeypatch,
        _state(repurposed={"gone": {"status": "done",
                                    "final_path": "/tmp/gone.mp4"}}),
        char_ids=None, tmp_path=tmp_path)
    assert "gone" not in written["repurposed"]


# --- 2b. the ORIGINAL finals are never touched ------------------------------
#
# Hugo 2026-08-10: "de nya repurpose videorna [ska inte] ersätta några icke
# repurpose videor". A repurpose writes a SEPARATE bucket and a SEPARATE file;
# nothing asserted either, so a future refactor could quietly make the mirrored
# copy overwrite the real final — and the only symptom would be an upside-down
# reel where the original used to be.


def test_repurpose_leaves_the_finals_bucket_alone(monkeypatch, tmp_path):
    finals = {"c1": {"status": "done", "final_path": "/tmp/final_c1.mp4"},
              "c2": {"status": "done", "final_path": "/tmp/final_c2.mp4"}}
    _, written = _run_do_repurpose(
        monkeypatch, _state(finals=dict(finals)), char_ids=["c2"],
        tmp_path=tmp_path)
    # `_update` is given ONLY the repurpose keys — `finals` is never written,
    # so the stored originals cannot be replaced or reordered.
    assert "finals" not in written
    assert set(written) == {"repurposed", "repurposing", "repurposed_at"}


def test_repurpose_writes_a_separate_file_from_the_final(monkeypatch,
                                                         tmp_path):
    """`repurpose_<cid>.mp4` vs `final_<cid>.mp4` in the SAME run dir — one
    shared name would make every mirrored copy destroy its own original."""
    copies: list[tuple[str, str]] = []
    _run_do_repurpose(monkeypatch, _state(), char_ids=["c2"],
                      tmp_path=tmp_path, copies=copies)
    dests = [Path(d).name for _, d in copies]
    assert dests == ["repurpose_c2.mp4"]
    assert not any(d.startswith("final_") for d in dests)


def test_swap_repurpose_slot_writes_its_own_fields_and_file():
    """The Swap side of the same guarantee: the repurpose slot must share NO
    state field and NO output filename with the Step-6 compile slot."""
    from character_swap.runner_compile import _COMPILE_SLOT, _REPURPOSE_SLOT
    for field in ("status_field", "edit_field", "path_field", "error_field",
                  "warning_field", "filename", "event_prefix"):
        assert getattr(_COMPILE_SLOT, field) != getattr(_REPURPOSE_SLOT, field), \
            f"{field} is shared — a repurpose would overwrite the real final"


# --- 3. the filter reaches Telegram -----------------------------------------


def _wire_send(monkeypatch, state, *, rebuilt):
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: dict(state))
    monkeypatch.setattr(runner_reengineer, "_update", lambda re_id, **kw: None)

    async def fake_build(re_id, st, **kw):
        return list(rebuilt)
    monkeypatch.setattr(runner_reengineer, "_do_repurpose", fake_build)

    seen: list = []

    async def fake_send(re_id, *, char_ids=None):
        seen.append(char_ids)
    monkeypatch.setattr(auto_finalize, "send_reengineer_repurposed", fake_send)
    return seen


def test_send_is_narrowed_to_the_rebuilt_characters(monkeypatch):
    """The bucket now holds everyone (merge), so an unfiltered send would
    re-post the copies this click never touched to their channels."""
    seen = _wire_send(monkeypatch, _state(), rebuilt=["c2"])
    asyncio.run(runner_reengineer.repurpose("re_t", char_ids=["c2"]))
    assert seen == [["c2"]]


def test_send_covers_everyone_when_nothing_was_picked(monkeypatch):
    seen = _wire_send(monkeypatch, _state(), rebuilt=["c1", "c2", "c3"])
    asyncio.run(runner_reengineer.repurpose("re_t"))
    assert seen == [["c1", "c2", "c3"]]


def test_send_follows_what_was_BUILT_not_what_was_PICKED(monkeypatch):
    """Hugo 2026-08-10: "bara de nya repurpose videorna skickas". A picked
    character that `_char_is_uninvolved` skips is never rebuilt — if an older
    repurpose left a `done` entry for it in the bucket, sending by the PICK
    would re-post that stale copy to its channel."""
    seen = _wire_send(monkeypatch, _state(), rebuilt=["c2"])
    asyncio.run(runner_reengineer.repurpose("re_t", char_ids=["c2", "skipped"]))
    assert seen == [["c2"]]


def test_nothing_built_sends_NOTHING(monkeypatch):
    """The landmine: `_send_reengineer_bucket` reads an EMPTY char_ids list as
    "no filter at all", so a build that produced nothing must return BEFORE the
    send — handing it [] would re-post the run's whole existing bucket."""
    seen = _wire_send(monkeypatch, _state(), rebuilt=[])
    asyncio.run(runner_reengineer.repurpose("re_t", char_ids=["gone"]))
    assert seen == []


def test_bucket_send_honours_char_ids(monkeypatch, tmp_path):
    """End of the chain: `_send_reengineer_bucket` must actually skip the
    unpicked entries, not just be handed the list."""
    f1 = tmp_path / "c1.mp4"; f1.write_text("x")
    f2 = tmp_path / "c2.mp4"; f2.write_text("x")
    state = _state(repurposed={
        "c1": {"status": "done", "final_path": str(f1)},
        "c2": {"status": "done", "final_path": str(f2)},
    })
    monkeypatch.setattr(auto_finalize.reengineer_mod
                        if hasattr(auto_finalize, "reengineer_mod") else api,
                        "_unused", None, raising=False)
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "load_state", lambda rid: dict(state))
    monkeypatch.setattr(
        auto_finalize, "store",
        lambda: SimpleNamespace(
            get_job=lambda jid: None,
            get_character=lambda cid: SimpleNamespace(
                name=cid, telegram_chat_id="@k")))
    monkeypatch.setattr(auto_finalize, "_persist_reengineer_receipts",
                        lambda *a, **k: None, raising=False)
    sent: list[str] = []

    async def fake_send_char(path, **kw):
        sent.append(Path(path).stem)
        return {"ok": True}
    monkeypatch.setattr(auto_finalize.telegram_delivery,
                        "send_character_final", fake_send_char)

    asyncio.run(auto_finalize.send_reengineer_repurposed(
        "re_t", char_ids=["c2"]))
    assert sent == ["c2"]


# --- 4. never a persisted setting -------------------------------------------


def test_char_ids_never_lands_in_repurpose_settings():
    """A remembered pick would silently narrow a LATER repurpose of the run —
    Hugo chose a per-click filter for exactly that reason."""
    state: dict = {}
    api._store_repurpose_settings(
        state, ReAssembleSettingsBody(char_ids=["c1"], target_wpm=180))
    assert "char_ids" not in state["repurpose_settings"]
    assert state["repurpose_settings"]["target_wpm"] == 180


# --- 5. a pick that builds nothing refuses LOUDLY ---------------------------


def test_no_pick_means_the_whole_cast():
    assert _repurpose_char_ids(None, _job("c1")) is None
    assert _repurpose_char_ids(ReAssembleSettingsBody(), _job("c1")) is None


def test_empty_pick_is_refused():
    with pytest.raises(HTTPException) as e:
        _repurpose_char_ids(ReAssembleSettingsBody(char_ids=[]), _job("c1"))
    assert e.value.status_code == 400
    assert "minst en" in str(e.value.detail)


def test_unknown_character_is_refused_by_name():
    with pytest.raises(HTTPException) as e:
        _repurpose_char_ids(ReAssembleSettingsBody(char_ids=["c1", "ghost"]),
                            _job("c1"))
    assert e.value.status_code == 400
    assert "ghost" in str(e.value.detail)


def test_blank_ids_are_dropped_not_counted():
    """A stray '' from the client must not pass the not-empty check."""
    with pytest.raises(HTTPException) as e:
        _repurpose_char_ids(ReAssembleSettingsBody(char_ids=["", ""]),
                            _job("c1"))
    assert e.value.status_code == 400
    assert _repurpose_char_ids(
        ReAssembleSettingsBody(char_ids=["", "c1"]), _job("c1")) == ["c1"]


# --- 6. the client half ------------------------------------------------------


def test_js_picker_behaviour():
    """Behavioral harness over the real app.js — the picker seeds ALL ticked,
    sends char_ids only for a real subset, and never sends it for an Editor
    reel (whose endpoint has no such field)."""
    proc = subprocess.run(
        ["node", str(_ROOT / "tests" / "js" / "repurpose_char_picker.mjs")],
        capture_output=True, text=True, cwd=_ROOT)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], result.get("failures")
