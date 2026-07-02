"""Fake providers for the stubbed-provider e2e harness.

`apply_fakes(monkeypatch)` swaps every PAID seam for a deterministic local
fake and returns a `FakeLedger` that records what the app asked the
"providers" to do:

- ``pipeline.generate_variant`` / ``generate_image`` / ``edit_image`` →
  writes a tiny real PNG to ``dest`` (the runner and the UI only need the
  file to exist).
- ``pipeline.submit_video`` → returns a fake provider job id;
  ``pipeline.wait_for_video`` → writes a tiny REAL mp4 (colour + 440 Hz
  tone via ffmpeg lavfi — the `_clip` pattern from
  tests/test_leading_silence_trim.py) so every downstream ffmpeg step
  (onset trim, assemble_clips, concat, probe) works on it.
- ``swap_qc.inspect_variant`` / ``video_qc.inspect_clip`` → auto-PASS
  verdicts (QC stays ON so the QC integration seam is exercised);
  ``inspect_consistency`` → no warnings.
- ``video_edit.transcribe_words`` → canned word list (no Whisper call).
- ``prompt_director.*`` / ``prompt_enrich.enrich_prompt`` → None (the
  documented "provider unavailable → fall back" contract).
- ElevenLabs is disabled via an empty key AND its client functions raise —
  the flows under test never voice-swap.
- BILLING GUARDS: the low-level OpenAI-image and Grok client functions
  raise AssertionError, so any code path that slips past the pipeline-level
  fakes fails the test loudly instead of billing.

Everything patches MODULE ATTRIBUTES on the public modules (``pipeline``,
``swap_qc``, ``video_qc``, ``video_edit``) — the runners call through those
attributes, so the fakes survive internal refactors of runner.py/api.py.
"""
from __future__ import annotations

import io
import itertools
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from character_swap import (
    pipeline,
    prompt_director,
    prompt_enrich,
    runner_reengineer,
    swap_qc,
    video_edit,
    video_qc,
)
from character_swap.clients import elevenlabs, grok, openai_image
from character_swap.config import settings
from character_swap.swap_qc import QCVerdict
from character_swap.video_qc import ClipVerdict
from character_swap.video_edit import Word

# ------------------------------------------------------------- tiny assets


def tiny_png(color: tuple[int, int, int] = (200, 40, 40),
             size: tuple[int, int] = (64, 64)) -> bytes:
    """A small valid PNG. Distinct `color` → distinct bytes → distinct
    content-addressed scene/character ids."""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def write_png(dest: Path, color: tuple[int, int, int] = (40, 160, 40)) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(tiny_png(color))
    return dest


def make_clip(dest: Path, secs: float = 2.0) -> Path:
    """Tiny REAL mp4: solid colour + a 440 Hz tone for the full duration
    (no leading/interior silence, so trim steps are no-ops). Reuses the
    lavfi pattern from tests/test_leading_silence_trim.py::_clip."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"color=c=red:s=160x284:d={secs}:r=12",
         "-f", "lavfi", "-i",
         f"sine=frequency=440:sample_rate=44100:duration={secs}",
         "-map", "0:v", "-map", "1:a",
         "-pix_fmt", "yuv420p", "-shortest", str(dest)],
        check=True, capture_output=True,
    )
    return dest


# Canned Whisper words — short enough to fit inside any fake clip.
CANNED_WORDS = [Word(text="hej", start=0.10, end=0.40),
                Word(text="hopp", start=0.50, end=0.90)]


# ------------------------------------------------------------------ ledger


@dataclass
class FakeLedger:
    """What the app asked the fake providers to do. Appends happen from
    worker threads (`asyncio.to_thread`), hence the lock."""
    image_calls: list[dict] = field(default_factory=list)
    edit_calls: list[dict] = field(default_factory=list)
    video_submits: list[dict] = field(default_factory=list)
    video_waits: list[dict] = field(default_factory=list)
    qc_images: list[dict] = field(default_factory=list)
    qc_clips: list[dict] = field(default_factory=list)
    transcribes: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, bucket: str, **info) -> None:
        with self._lock:
            getattr(self, bucket).append(info)


# ------------------------------------------------------------- the patcher


def _billing_guard(name: str):
    def _blocked(*a, **kw):  # pragma: no cover — reaching this IS the failure
        raise AssertionError(
            f"e2e billing guard: real provider call attempted via {name} "
            f"(args={a!r:.200}, kwargs keys={sorted(kw)})")
    return _blocked


def apply_fakes(monkeypatch, ledger: FakeLedger | None = None,
                clip_secs: float = 2.0) -> FakeLedger:
    """Install every fake + speed knob. Returns the ledger."""
    led = ledger or FakeLedger()

    # --- deterministic settings: keys present for the providers the flows
    # validate upfront (openai for jobs/compile, xai for grok-imagine, fal
    # for kling-v3), ABSENT for elevenlabs/anthropic so voice swap and the
    # Director stay off regardless of the host machine's .env.
    monkeypatch.setattr(settings, "openai_api_key", "e2e-test-key")
    monkeypatch.setattr(settings, "xai_api_key", "e2e-test-key")
    monkeypatch.setattr(settings, "fal_api_key", "e2e-test-key")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    # QC ON (the fakes auto-pass) so the QC seam is part of the flow,
    # independent of the host's SWAP_QC / VIDEO_QC env.
    monkeypatch.setattr(settings, "swap_qc_enabled", True)
    monkeypatch.setattr(settings, "video_qc_enabled", True)

    # --- speed: the Reengineer phase watchers poll on a module constant.
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.05)
    monkeypatch.setattr(runner_reengineer, "_ASSEMBLE_COVERAGE_POLL_SECS", 0.1)

    # --- image generation ------------------------------------------------
    def fake_generate_variant(**kw):
        dest = Path(kw["dest"])
        led.record("image_calls",
                   model=kw.get("model"),
                   scene=str(kw.get("scene_image")),
                   character=str(kw.get("character_image")),
                   prompt=kw.get("prompt"),
                   job_id=kw.get("job_id"),
                   background_mode=kw.get("background_mode"))
        return write_png(dest)

    def fake_generate_image(**kw):
        dest = Path(kw["dest"])
        led.record("image_calls", model="gpt-image(generate_image)",
                   scene=str(kw.get("scene_image")),
                   prompt=kw.get("prompt"), job_id=kw.get("job_id"))
        return write_png(dest)

    def fake_edit_image(**kw):
        dest = Path(kw["dest"])
        led.record("edit_calls", prompt=kw.get("custom_prompt"),
                   source=str(kw.get("source_image")), job_id=kw.get("job_id"))
        return write_png(dest, color=(160, 40, 160))

    monkeypatch.setattr(pipeline, "generate_variant", fake_generate_variant)
    monkeypatch.setattr(pipeline, "generate_image", fake_generate_image)
    monkeypatch.setattr(pipeline, "edit_image", fake_edit_image)

    # --- video generation ------------------------------------------------
    counter = itertools.count(1)

    def fake_submit_video(**kw):
        provider_id = f"fake-vid-{next(counter)}"
        led.record("video_submits",
                   provider_id=provider_id,
                   model=kw.get("model"),
                   prompt=kw.get("movement_prompt"),
                   image=str(kw.get("image")),
                   duration_secs=kw.get("duration_secs"),
                   end_image=(str(kw["end_image"])
                              if kw.get("end_image") else None),
                   generate_audio=kw.get("generate_audio"),
                   job_id=kw.get("job_id"))
        return provider_id

    def fake_wait_for_video(**kw):
        dest = Path(kw["dest"])
        led.record("video_waits", provider_id=kw.get("job_id"),
                   model=kw.get("model"), dest=str(dest))
        return make_clip(dest, secs=clip_secs)

    monkeypatch.setattr(pipeline, "submit_video", fake_submit_video)
    monkeypatch.setattr(pipeline, "wait_for_video", fake_wait_for_video)

    # --- QC: judged, always PASS ------------------------------------------
    def fake_inspect_variant(**kw):
        led.record("qc_images", result=str(kw.get("result_image")),
                   scene=str(kw.get("scene_image")),
                   background_replaced=kw.get("background_replaced"),
                   camera_gaze=kw.get("camera_gaze"))
        return QCVerdict(passed=True, reason="", corrective_hint="")

    def fake_inspect_clip(video, **kw):
        led.record("qc_clips", video=str(video),
                   prompt=kw.get("movement_prompt"))
        return ClipVerdict(passed=True, reason="", corrective_hint="")

    monkeypatch.setattr(swap_qc, "inspect_variant", fake_inspect_variant)
    monkeypatch.setattr(swap_qc, "inspect_consistency",
                        lambda **kw: [], raising=False)
    monkeypatch.setattr(video_qc, "inspect_clip", fake_inspect_clip)

    # --- Whisper: canned words --------------------------------------------
    def fake_transcribe_words(video_path, *, job_id=None, script_hint=None):
        led.record("transcribes", video=str(video_path), job_id=job_id,
                   script_hint=script_hint)
        return list(CANNED_WORDS)

    monkeypatch.setattr(video_edit, "transcribe_words", fake_transcribe_words)

    # --- Director / enrich: unavailable → documented fallback --------------
    for name in ("direct_swap", "direct_movement", "direct_reengineer_swap",
                 "direct_scene_prompt_rewrite", "direct_moderation_rewrite"):
        monkeypatch.setattr(prompt_director, name,
                            lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(prompt_enrich, "enrich_prompt",
                        lambda *a, **kw: None, raising=False)

    # --- guards: anything below the pipeline seams must never be reached ---
    monkeypatch.setattr(openai_image, "generate",
                        _billing_guard("openai_image.generate"))
    for fn in ("submit", "status", "download_video", "generate_image"):
        monkeypatch.setattr(grok, fn, _billing_guard(f"grok.{fn}"),
                            raising=False)
    for fn in ("voice_changer", "text_to_speech"):
        monkeypatch.setattr(elevenlabs, fn,
                            _billing_guard(f"elevenlabs.{fn}"), raising=False)
    # The e2e flows run captions OFF — an accidental caption render would try
    # to download fonts / spawn Remotion. Fail loudly instead.
    monkeypatch.setattr(video_edit, "render_captions",
                        _billing_guard("video_edit.render_captions"))

    return led
