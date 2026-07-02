"""END-TO-END: the Reengineer from_images flow (the Swap tab's
"animera dina referensbilder" path) over the public HTTP surface.

Chain exercised (fakes from e2e.fakes, ZERO provider billing):

  upload character → POST /api/reengineer/from_images (2 image rows, 1 char)
  → swap phase (fake gpt2-id-swap images, QC auto-pass, consistency no-op)
  → gate: status "awaiting_approval"
  → approve both variants via the underlying job's /approve endpoint
  → POST .../animate (captions OFF persisted for the final build)
  → video phase (fake kling-v3 clips with per-scene durations)
  → gate: status "awaiting_assembly"
  → POST .../assemble → real ffmpeg concat through the Editor pipeline
  → status "done", per-char final_<cid>.mp4 exists, nobody excluded.

BackgroundTasks run inline under TestClient; the reengineer phase watchers
poll a module constant that e2e.fakes pins to 0.05 s, so the whole flow is
deterministic and fast.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from character_swap import video_edit
from character_swap.config import settings

from e2e import fakes

pytestmark = pytest.mark.e2e


MOTION_1 = 'He nods slowly and says: "Hi there, welcome back."'
MOTION_2 = "She raises the cup and takes a sip."


def _upload_character(client, color, name: str) -> str:
    r = client.post(
        "/api/characters",
        files={"file": (f"{name}.png", fakes.tiny_png(color), "image/png")},
        data={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["char_id"]


def _get_run(client, re_id: str) -> dict:
    r = client.get(f"/api/reengineer/{re_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _wait_run(client, re_id: str, cond, *, desc: str,
              timeout: float = 25.0, interval: float = 0.05) -> dict:
    """Poll the run state until `cond(state)` holds — normally true on the
    first check (background tasks ran inline)."""
    deadline = time.monotonic() + timeout
    while True:
        st = _get_run(client, re_id)
        if cond(st):
            return st
        if st.get("status") == "failed" and not cond(st):
            raise AssertionError(
                f"run failed while waiting for {desc}: {st.get('error')}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {desc}\n"
                                 f"last state: {st.get('status')} / "
                                 f"{st.get('error')}")
        time.sleep(interval)


def test_reengineer_from_images_full_flow(client, ledger):
    cid = _upload_character(client, (10, 90, 220), "E2E Reel Star")

    # ---- create the run: 2 image rows, per-row prompt + clip length --------
    r = client.post(
        "/api/reengineer/from_images",
        files=[
            ("files", ("row1.png", fakes.tiny_png((230, 90, 20)), "image/png")),
            ("files", ("row2.png", fakes.tiny_png((20, 90, 230)), "image/png")),
        ],
        data={
            "motion_prompts": json.dumps([MOTION_1, MOTION_2]),
            "lengths": json.dumps([3.0, 4.0]),
            "character_ids": json.dumps([cid]),
            "use_director": "false",
            "skip_qc": "false",
        })
    assert r.status_code == 200, r.text
    re_id = r.json()["re_id"]
    assert re_id.startswith("re_")

    # ---- swap phase finished inline → the manual approval gate -------------
    st = _wait_run(client, re_id,
                   lambda s: s.get("status") == "awaiting_approval",
                   desc="swap phase → awaiting_approval")
    assert st["job_id"], "the underlying swap job must be attached to the run"
    job_id = st["job_id"]
    assert len(st["scenes"]) == 2
    assert st["scenes"][0]["kling_secs"] == 3
    assert st["scenes"][1]["kling_secs"] == 4
    # Dialogue extraction: row 1 has a says-clause, row 2 is action-only.
    assert "Hi there" in st["scenes"][0]["speech"]
    assert st["scenes"][1]["speech"] == ""

    jc = st["job"]["characters"][cid]
    variants = jc["images"]
    assert len(variants) == 2, "1 char x 2 scenes x 1 image"
    assert all(v["status"] == "ready" for v in variants)
    assert all(v["qc_status"] == "passed" for v in variants)
    assert {v["scene_id"] for v in variants} == {
        e["scene_id"] for e in st["scenes"]}

    # Fake image engine got the run's default model, once per (char, scene).
    assert len(ledger.image_calls) == 2
    assert all(c["model"] == "gpt2-id-swap" for c in ledger.image_calls)
    # Standard background source (Hugo 2026-06-21) rode through to the engine.
    assert all(c["background_mode"] == "character"
               for c in ledger.image_calls)

    # The run shows up in the list view (serialization sanity).
    listing = client.get("/api/reengineer")
    assert listing.status_code == 200
    row = next((x for x in listing.json() if x["re_id"] == re_id), None)
    assert row is not None
    assert row["char_names"].get(cid) == "E2E Reel Star"

    # ---- approve both images at the gate ------------------------------------
    for v in variants:
        r = client.post(f"/api/jobs/{job_id}/approve", json={
            "char_id": cid, "action": "approve", "variant_id": v["variant_id"],
        })
        assert r.status_code == 200, r.text
    jc = client.get(f"/api/jobs/{job_id}").json()["characters"][cid]
    assert jc["status"] == "approved"
    assert len(jc["approved_variant_ids"]) == 2

    # ---- animate (settings for the FINAL build persisted now) ---------------
    r = client.post(f"/api/reengineer/{re_id}/animate", json={
        "enable_captions": False,       # no font/remotion work in tests
        "playback_speed": 1.0,          # keep the ffmpeg chain minimal
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    st = _wait_run(client, re_id,
                   lambda s: s.get("status") == "awaiting_assembly",
                   desc="video phase → awaiting_assembly (clip-review gate)")

    # Per-scene fake Kling submits: locked model, per-row durations, audio on,
    # and the gate-approved motion prompt text reached the provider verbatim.
    assert len(ledger.video_submits) == 2
    assert all(s["model"] == "kling-v3" for s in ledger.video_submits)
    assert {s["duration_secs"] for s in ledger.video_submits} == {3, 4}
    assert all(s["generate_audio"] is True for s in ledger.video_submits)
    sub_prompts = " || ".join(s["prompt"] for s in ledger.video_submits)
    assert "Hi there, welcome back." in sub_prompts
    assert "raises the cup" in sub_prompts

    # All clips DONE on the underlying job, QC-passed, real files on disk.
    jc = client.get(f"/api/jobs/{job_id}").json()["characters"][cid]
    assert len(jc["videos"]) == 2
    assert all(vv["status"] == "done" for vv in jc["videos"])
    assert all(vv["qc_status"] == "passed" for vv in jc["videos"])
    for w in ledger.video_waits:
        assert Path(w["dest"]).exists()

    # ---- assemble → per-character final --------------------------------------
    r = client.post(f"/api/reengineer/{re_id}/assemble")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["stale_scenes"] == []
    assert body["excluded"] == [], (
        "the (approved) character must never land in the excluded bucket")

    st = _wait_run(client, re_id, lambda s: s.get("status") == "done",
                   desc="assemble → done")
    assert st["status"] == "done"
    assert st.get("error") is None
    assert st.get("finals_stale") is False

    finals = st["finals"]
    assert set(finals.keys()) == {cid}, "exactly one final, nobody excluded"
    f = finals[cid]
    assert f["status"] == "done"
    assert f["n_clips"] == 2
    assert f["edit_id"].startswith("ed_"), "final must be Editor-re-renderable"
    assert "warning" not in f, (
        f"assemble must be warning-free (a warning = a degraded step): {f}")

    final = Path(f["final_path"])
    assert final == settings.output_dir / "reengineer" / re_id / f"final_{cid}.mp4"
    assert final.exists() and final.stat().st_size > 0
    # Two 2 s fake clips, no lead silence, captions off, speed 1.0 → ~4 s.
    assert video_edit._probe_duration(final) == pytest.approx(4.0, abs=1.0)

    # Whole-flow invariants: only the fakes ran.
    assert len(ledger.image_calls) == 2
    assert len(ledger.video_submits) == 2
    assert ledger.transcribes, "assemble transcribes via the Whisper stub"
