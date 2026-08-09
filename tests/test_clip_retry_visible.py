"""A retrying clip has to LOOK like it is retrying (Hugo 2026-08-10).

"Hur vet jag att något retryar, står det fortfarande så här rött?" — asked with
a screenshot of a run where one clip showed the red content-policy panel and
every other clip showed an ordinary spinner. Nothing on screen distinguished
"this clip is on take 3 of 5" from "this clip is rendering", because the retry
loop puts a refused clip straight back into PROCESSING: same status, same
spinner, no counter. The only evidence lived in the server log.

Three things are locked here:

  1. the runner COUNTS every refused take, reroute legs included, and persists
     it BEFORE the next take starts — a counter written only on the way out is
     missing for the whole minute the user is actually watching;
  2. the API SERIALIZES it, or the client has nothing to render;
  3. app.js turns it into an in-flight chip and a done-note, and stays silent
     for a clip nobody refused (`tests/js/clip_retry_note.mjs`).

The failure panel keeps its monopoly on red: a FAILED clip shows no retry chip,
because the panel already explains the refusal and two red messages about one
clip is noise.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from character_swap import runner, runner_media
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus, VideoStatus,
    VideoVariant,
)

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = Path(__file__).parent / "js" / "clip_retry_note.mjs"

_MODERATION = "fal submit failed: 400 rejected — flagged as nsfw content"
_TIMEOUT = "Video job req-1 timed out after 600s"


@pytest.fixture
def _chain(monkeypatch):
    """One unchanged re-submit, reroute on — a short, fully walked chain."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "video_refusal_retries",
                        property(lambda self: 1), raising=False)
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: True), raising=False)


def _job_one_clip(tmp_path, *, video_model="kling-v3"):
    v_img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                           prompt="BASE", scene_id="s1",
                           status=VariantStatus.READY)
    Path(v_img.path).write_bytes(b"img")
    jc = JobCharacter(char_id="cA", name="A",
                      source_image_path=str(tmp_path / "char.png"),
                      status=CharStatus.APPROVED, images=[v_img],
                      approved_variant_ids=["v1"], approved_variant_id="v1")
    (tmp_path / "char.png").write_bytes(b"char")
    scene = tmp_path / "scene.png"; scene.write_bytes(b"scene")
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path=str(scene), scene_ids=["s1"],
              scene_image_paths=[str(scene)], video_model=video_model,
              characters={"cA": jc})
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    return job, jc, video


def _stub(monkeypatch, tmp_path, *, seen=None):
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir", lambda job_id, char_id: tmp_path)

    def _replace(job, jc, video):
        # Snapshot what a poll would have seen at this instant — the whole
        # point is that the counter is visible DURING the wait, not after.
        if seen is not None:
            seen.append((video.status, video.refusal_takes))
    monkeypatch.setattr(runner, "_replace_video", _replace)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(type(runner.settings), "video_qc_enabled",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(runner.video_qc, "inspect_clip", lambda *a, **k: None)


def _run(coro):
    return asyncio.run(coro)


# --- 1. the runner counts, and counts in time -------------------------------

def test_refusal_is_visible_while_the_clip_is_still_running(
        monkeypatch, tmp_path, _chain):
    """THE REPORTED CASE. After the first refusal the clip goes back to
    PROCESSING — and a poll landing there must already see refusal_takes=1,
    or the UI spends the next ~60 s showing a plain spinner."""
    job, jc, video = _job_one_clip(tmp_path)
    seen: list[tuple] = []
    _stub(monkeypatch, tmp_path, seen=seen)
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        if len(calls) == 1:
            raise RuntimeError(_MODERATION)
        return "req-2"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert video.status == VideoStatus.DONE
    assert video.refusal_takes == 1
    assert (VideoStatus.PROCESSING, 1) in seen, (
        "a poll during the retry must see the count, not a bare spinner")


def test_every_leg_of_the_chain_is_counted(monkeypatch, tmp_path, _chain):
    """Reroute legs count too: the user watching a clip walk Kling → Grok is
    watching retries, whatever the code calls them internally."""
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        raise RuntimeError(_MODERATION)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert calls == ["kling-v3", "kling-v3", "grok-imagine-1.5"]
    assert video.refusal_takes == 3
    assert video.status == VideoStatus.ERROR


def test_non_refusals_are_not_counted_as_refusals(monkeypatch, tmp_path, _chain):
    """A timeout is not a content block. Counting it would put "nekad ×1" on a
    clip nothing refused — the chip has to mean what it says."""
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner.pipeline, "submit_video",
        lambda **kw: (_ for _ in ()).throw(RuntimeError(_TIMEOUT)))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert video.refusal_takes == 0
    assert video.status == VideoStatus.ERROR


def test_clean_clip_keeps_the_counter_at_zero(monkeypatch, tmp_path, _chain):
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(runner.pipeline, "submit_video", lambda **kw: "req-1")
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert video.status == VideoStatus.DONE
    assert video.refusal_takes == 0


# --- 2. it survives the trip to the client ----------------------------------

def test_counter_is_serialized_to_the_client(tmp_path):
    """A counter the API drops is a counter the UI cannot render."""
    from character_swap import api
    job, jc, video = _job_one_clip(tmp_path)
    video.refusal_takes = 2
    video.status = VideoStatus.PROCESSING
    payload = api._job_to_dict(job)
    clip = payload["characters"]["cA"]["videos"][0]
    assert clip["refusal_takes"] == 2


def test_counter_round_trips_through_the_model(tmp_path):
    """Persisted, not derived: after a restart the run card must still be able
    to say why a clip is on its fourth take."""
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PROCESSING, refusal_takes=3)
    assert VideoVariant.model_validate(
        json.loads(video.model_dump_json())).refusal_takes == 3
    # Old rows carry no such field and must load as "never refused".
    assert VideoVariant.model_validate(
        {"video_id": "vd2", "grok_job_id": ""}).refusal_takes == 0


# --- 3. the client turns it into something visible --------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_clip_retry_note_behavior():
    proc = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True, text=True, timeout=60, cwd=_ROOT,
    )
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "clip retry note regressions:\n  - " + "\n  - ".join(
        result.get("failures", []))


def test_the_markup_actually_renders_the_note():
    """Node-free backstop: helpers nothing calls are invisible, which is the
    exact bug this file exists for. Both clip strips and the compact per-scene
    row must reference them."""
    html = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert html.count("clipRetryNote(") >= 3, (
        "the retry chip must appear in the Swap strip, the Reengineer strip "
        "and the compact per-scene clip row")
    assert "clipTakesNote(" in html


# --- 4. the counter and the failure message agree ---------------------------

def test_failure_message_names_the_whole_walked_chain(
        monkeypatch, tmp_path, _chain):
    """What the red panel says when the chip finally gives up: every model,
    with its take count — the signal to change the START FRAME rather than
    click ↻ again."""
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner.pipeline, "submit_video",
        lambda **kw: (_ for _ in ()).throw(RuntimeError(_MODERATION)))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    err = video.error or ""
    assert "kling-v3 ×2" in err, "repeats must collapse into a count"
    assert "grok-imagine-1.5" in err
    assert runner._chain_summary(["a", "a", "b"], 2) == "a ×2 → b"
    # Only the legs that RAN are named — a clip that died on leg 1 must not
    # claim models it never reached.
    assert runner._chain_summary(["a", "a", "b"], 0) == "a"


def test_summary_covers_the_real_chain(monkeypatch):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "video_refusal_retries",
                        property(lambda self: 4), raising=False)
    models = runner_media.video_attempt_models("veo-3.1-fast", language="es")
    assert runner._chain_summary(models, len(models) - 1) == (
        "veo-3.1-fast ×5 → kling-v3 → grok-imagine-1.5")
