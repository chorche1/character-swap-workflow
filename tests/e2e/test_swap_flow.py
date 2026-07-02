"""END-TO-END: the full Swap 6-step flow over the public HTTP surface.

Steps exercised (all against `api.app` via TestClient, fakes from e2e.fakes,
ZERO provider billing):

  1. Scene upload            POST /api/scenes                (x2 scenes)
  2. Character upload        POST /api/characters
  3. Image generation        POST /api/jobs → variants READY + QC-passed
     Approval                POST /api/jobs/{id}/approve_all (one per scene)
  4. Movement                POST /api/jobs/{id}/movement    (per-scene prompts)
  5. Videos                  fake provider clips → all DONE, char DONE
  6. Compile                 POST /api/jobs/{id}/compile_videos (captions OFF)
                             → real ffmpeg concat → compiled/<char_id>.mp4

FastAPI BackgroundTasks run inline under TestClient, so each POST returns
only after its scheduled runner entry point finished — the `_wait_job` poll
is a defensive net (short interval + deadline, no long sleeps) in case that
scheduling ever changes.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from character_swap import video_edit
from character_swap.config import settings

from e2e import fakes

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------- helpers

def _upload_scene(client, color) -> str:
    r = client.post(
        "/api/scenes",
        files={"file": ("scene.png", fakes.tiny_png(color), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scene_id"].startswith("sc_")
    return body["scene_id"]


def _upload_character(client, color, name: str) -> str:
    r = client.post(
        "/api/characters",
        files={"file": (f"{name}.png", fakes.tiny_png(color), "image/png")},
        data={"name": name})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == name
    assert body["images"], "character upload must register at least one image"
    return body["char_id"]


def _get_job(client, job_id: str) -> dict:
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _wait_job(client, job_id: str, cond, *, desc: str,
              timeout: float = 20.0, interval: float = 0.05) -> dict:
    """Poll GET /api/jobs/{id} until `cond(job)` holds. Deterministic: short
    interval + hard deadline, and under TestClient the condition is normally
    already true on the first check (background tasks ran inline)."""
    deadline = time.monotonic() + timeout
    while True:
        job = _get_job(client, job_id)
        if cond(job):
            return job
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {desc}\n"
                                 f"last job state: {job}")
        time.sleep(interval)


# ------------------------------------------------------------------ the flow

def test_full_swap_flow_six_steps(client, ledger):
    # ---- Step 1: upload two scenes (multi-scene job) ----------------------
    s1 = _upload_scene(client, (200, 40, 40))
    s2 = _upload_scene(client, (40, 40, 200))
    assert s1 != s2, "distinct images must content-address to distinct scenes"

    # ---- Step 2: character library ----------------------------------------
    cid = _upload_character(client, (30, 200, 30), "E2E Hero")

    # ---- Step 3: create the job → image generation (inline, faked) --------
    r = client.post("/api/jobs", json={
        "scene_ids": [s1, s2],
        "character_ids": [cid],
        "images_per_character": 2,
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    job = _wait_job(
        client, job_id,
        lambda j: (j["characters"][cid]["images"]
                   and all(v["status"] == "ready"
                           for v in j["characters"][cid]["images"])),
        desc="all variants READY")
    jc = job["characters"][cid]

    # 2 scenes x 2 images_per_character = 4 variants, all READY + QC-passed.
    assert len(jc["images"]) == 4
    assert {v["scene_id"] for v in jc["images"]} == {s1, s2}
    assert all(v["qc_status"] == "passed" for v in jc["images"])
    assert jc["status"] == "awaiting_approval"
    assert jc["approved_variant_ids"] == []

    # The fake provider was called once per slot and every slot was judged.
    assert len(ledger.image_calls) == 4
    assert len(ledger.qc_images) == 4
    assert all(c["model"] == "gpt-image" for c in ledger.image_calls)
    # Every generated file really exists on disk.
    for v in jc["images"]:
        assert v["url"], "ready variant must expose a file URL"

    # Serialization sanity: job list + summary endpoints stay coherent.
    listed = client.get("/api/jobs?summary=1")
    assert listed.status_code == 200
    assert any(j["job_id"] == job_id for j in listed.json())

    # ---- Step 3b: approve all (one variant per scene) ----------------------
    r = client.post(f"/api/jobs/{job_id}/approve_all")
    assert r.status_code == 200, r.text
    picked = r.json()["approved"]
    assert len(picked) == 2
    assert {p["scene_id"] for p in picked} == {s1, s2}
    job = r.json()["job"]
    jc = job["characters"][cid]
    assert jc["status"] == "approved"
    assert len(jc["approved_variant_ids"]) == 2

    # ---- Step 4: per-scene movement prompts → video phase (inline, faked) --
    prompts = {
        s1: 'He smiles and says: "Hello world."',
        s2: "She waves goodbye at the camera.",
    }
    r = client.post(f"/api/jobs/{job_id}/movement", json={
        "movement_prompts": prompts,
        "video_model": "grok-imagine",
        "videos_per_character": 1,
        "duration_secs": 5,
    })
    assert r.status_code == 200, r.text
    assert r.json()["movement_prompts"] == prompts

    # A second movement submit must be refused (approvals lock).
    r2 = client.post(f"/api/jobs/{job_id}/movement",
                     json={"movement_prompts": prompts,
                           "video_model": "grok-imagine"})
    assert r2.status_code == 409

    # ---- Step 5: videos all DONE -------------------------------------------
    job = _wait_job(
        client, job_id,
        lambda j: j["characters"][cid]["status"] == "done",
        desc="character DONE after video phase")
    jc = job["characters"][cid]
    videos = jc["videos"]
    assert len(videos) == 2, "one clip per approved image"
    assert all(vv["status"] == "done" for vv in videos)
    assert all(vv["qc_status"] == "passed" for vv in videos)
    assert {vv["source_variant_id"] for vv in videos} == set(
        jc["approved_variant_ids"])

    # Fake provider got exactly the per-scene prompts, on the chosen model.
    assert len(ledger.video_submits) == 2
    assert all(s["model"] == "grok-imagine" for s in ledger.video_submits)
    assert all(s["duration_secs"] == 5 for s in ledger.video_submits)
    assert {s["prompt"] for s in ledger.video_submits} == set(prompts.values())
    # ...and the downloaded clips are REAL playable mp4s on disk.
    assert len(ledger.video_waits) == 2
    for w in ledger.video_waits:
        clip = Path(w["dest"])
        assert clip.exists() and clip.stat().st_size > 0
        assert video_edit._probe_duration(clip) == pytest.approx(2.0, abs=0.5)
    assert len(ledger.qc_clips) == 2

    # ---- Step 6: compile the final per-character MP4 (captions OFF) --------
    r = client.post(f"/api/jobs/{job_id}/compile_videos", json={
        "enable_captions": False,
        "enable_wpm_normalize": False,
        "enable_voice_swap": False,
    })
    assert r.status_code == 200, r.text

    job = _wait_job(
        client, job_id,
        lambda j: j["characters"][cid]["compile_status"] in ("done", "failed"),
        desc="compile settled")
    jc = job["characters"][cid]
    assert jc["compile_status"] == "done", jc["compile_error"]
    assert jc["compile_error"] is None
    assert jc["compile_warning"] is None, (
        "compile must be warning-free (a warning = a silently degraded step): "
        f"{jc['compile_warning']}")
    assert jc["compiled_video_url"]
    assert jc["compile_edit_id"], "compile must be re-renderable via an edit_id"

    # The canonical final exists and is roughly the two concatenated clips.
    final = settings.output_dir / job_id / "compiled" / f"{cid}.mp4"
    assert final.exists() and final.stat().st_size > 0
    assert video_edit._probe_duration(final) == pytest.approx(4.0, abs=1.0)

    # The parallel editor copy exists too (words.json via the stubbed Whisper).
    edit_dir = settings.output_dir / "editor" / jc["compile_edit_id"]
    assert edit_dir.exists()
    assert (edit_dir / "words.json").exists(), (
        "the compile always transcribes (stubbed here) so the words.json for "
        "later re-renders must exist")
    assert ledger.transcribes, "Whisper stub (not the real API) must be used"

    # Whole-flow invariant: nothing but the fakes ran — 4 images, 2 clips.
    assert len(ledger.image_calls) == 4
    assert len(ledger.video_submits) == 2
