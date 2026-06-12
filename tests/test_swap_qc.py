"""Vision-QC loop on swap variants — hermetic tests.

The generation call and the QC agent are both stubbed; under test is the
runner's generate → inspect → regenerate-with-hint loop and the never-block
philosophy (QC unavailable → skip; exhausted retries → keep image + ⚠ flag).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import runner, swap_qc
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)
from character_swap.swap_qc import QCVerdict


def _job(tmp_path) -> tuple[Job, JobCharacter, GeneratedImage]:
    dest = tmp_path / "variant_v1.png"
    v = GeneratedImage(variant_id="v1", path=str(dest), prompt="BASE PROMPT",
                       scene_id="s1", status=VariantStatus.GENERATING)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.GENERATING, images=[v])
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/scene.png",
              scene_ids=["s1"], scene_image_paths=["/scene.png"],
              characters={"cA": jc})
    return job, jc, v


def _stub(monkeypatch, job, verdicts: list, gen_prompts: list):
    """Stub generation (records prompts, writes dest) + QC (scripted verdicts)."""
    def fake_gen(**kw):
        gen_prompts.append(kw["prompt"])
        Path(kw["dest"]).write_bytes(b"img")
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_gen)

    it = iter(verdicts)
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: next(it))
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, v: Path("/scene.png"))


def _run(job, jc, v):
    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))


def test_qc_pass_first_try(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)
    prompts: list = []
    _stub(monkeypatch, job, [QCVerdict(True, "", "")], prompts)
    _run(job, jc, v)
    assert v.status == VariantStatus.READY
    assert v.qc_status == "passed"
    assert v.qc_attempts == 1
    assert prompts == ["BASE PROMPT"]


def test_qc_fail_then_pass_regenerates_with_hint(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)
    prompts: list = []
    _stub(monkeypatch, job,
          [QCVerdict(False, "wrong person", "Match the character's face exactly."),
           QCVerdict(True, "", "")], prompts)
    _run(job, jc, v)
    assert v.qc_status == "passed"
    assert v.qc_attempts == 2
    assert len(prompts) == 2
    assert prompts[0] == "BASE PROMPT"
    # First retry = minimal-change REPAIR mode: fix-only-this prompt built
    # from the judge's hint (scene-input swap covered in test_video_qc.py).
    assert "Match the character's face exactly." in prompts[1]
    assert "as little" in prompts[1].lower()
    assert v.prompt == "BASE PROMPT"          # base prompt never mutated


def test_qc_exhausted_keeps_image_with_flag(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)
    prompts: list = []
    bad = QCVerdict(False, "wrong person", "fix it")
    _stub(monkeypatch, job, [bad, bad, bad], prompts)
    _run(job, jc, v)
    # Image KEPT (never destroyed by the judge), flagged for the UI.
    assert v.status == VariantStatus.READY
    assert v.qc_status == "failed"
    assert v.qc_reason == "wrong person"
    assert v.qc_attempts == 1 + runner.settings.swap_qc_max_retries
    assert len(prompts) == v.qc_attempts


def test_qc_unavailable_skips_single_attempt(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)
    prompts: list = []
    _stub(monkeypatch, job, [None], prompts)
    _run(job, jc, v)
    assert v.status == VariantStatus.READY
    assert v.qc_status == "skipped"
    assert len(prompts) == 1


def test_inspect_variant_disabled_returns_none(monkeypatch, tmp_path):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "swap_qc_enabled",
                        property(lambda self: False), raising=False)
    img = tmp_path / "x.png"; img.write_bytes(b"x")
    assert swap_qc.inspect_variant(scene_image=img, character_image=img,
                                   result_image=img) is None


def test_inspect_variant_parses_verdict(monkeypatch, tmp_path):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "swap_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(settings), "anthropic_api_key",
                        property(lambda self: "key"), raising=False)
    from character_swap.clients import anthropic_client
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": str(p)})
    monkeypatch.setattr(anthropic_client, "messages_with_tools",
                        lambda **kw: {"fake": "resp"})
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: {"passed": False, "reason": "wrong person",
                                            "corrective_hint": "use Image 2's face"})
    img = tmp_path / "x.png"; img.write_bytes(b"x")
    verdict = swap_qc.inspect_variant(scene_image=img, character_image=img,
                                      result_image=img)
    assert verdict is not None and verdict.passed is False
    assert verdict.reason == "wrong person"
    assert verdict.corrective_hint == "use Image 2's face"


def test_qc_prompt_covers_prop_and_action_fidelity():
    """Regression guard (Hugo 2026-06-11): wrong-prop images (person holding
    a completely different thing than in the scene) passed QC because the
    judge was never ASKED about props. The system prompt must instruct an
    explicit same-objects/same-action check."""
    text = swap_qc.QC_SYSTEM
    assert "WRONG PROPS" in text
    assert "SAME object" in text
    assert "action" in text.lower()


def test_qc_default_judge_is_sonnet():
    """The fine-grained scene-vs-result comparison needs the stronger vision
    model (wrong-prop images passed the Haiku judge live). Env-overridable
    via SWAP_QC_MODEL."""
    from character_swap.config import settings
    assert "sonnet" in settings.swap_qc_model


def test_qc_prompt_covers_framing_and_zoom():
    """Regression guard (Hugo 2026-06-11): gpt2-id-swap outputs noticeably
    more zoomed-out than the source scene passed QC — the judge was never
    asked about framing. The system prompt must instruct an explicit
    camera-distance/crop/subject-scale comparison."""
    text = swap_qc.QC_SYSTEM
    assert "WRONG FRAMING" in text
    assert "zoomed out" in text
    assert "subject scale" in text


def test_qc_checks_background_symbols():
    """Backlog #3 (audit 2026-06-12): the US flag rendered without its blue
    star canton in 3/6 post-fix scenes and every variant passed QC — the
    judge never inspected background symbols. With a BACKGROUND image the
    judge must fail defaced/incomplete distinctive symbols."""
    text = " ".join(swap_qc.QC_SYSTEM.split())
    assert "WRONG BACKGROUND SYMBOL" in text
    assert "blue star canton" in text


def test_qc_zoom_anchor_survives_replaced_background():
    """Backlog #2 (audit 2026-06-12): clearly wider variants passed QC when
    background_replaced=true — with a new environment the judge had nothing
    to compare the room against and went lenient on zoom. The rule must
    explicitly re-anchor the comparison on the SUBJECT's frame occupancy,
    exactly like the headroom rule already does."""
    low = swap_qc.QC_SYSTEM.lower()
    z = low.index("wrong framing")
    block = low[z:low.index("wrong headroom")]
    assert "background_replaced=true" in block
    assert "subject" in block
    assert "fraction of the frame" in block


def test_qc_prompt_covers_headroom_drift():
    """Regression guard (Hugo 2026-06-12): with a replaced background the
    swap pushed the subject down and added the new background's sky/scenery
    above the head (dead space at top) — and it PASSED QC because the judge
    went lenient on edges when background_replaced=true. The judge must flag
    added headroom even when the background is replaced."""
    text = swap_qc.QC_SYSTEM
    assert "WRONG HEADROOM" in text
    assert "above the" in text.lower()
    # The headroom rule must explicitly survive a replaced background.
    low = text.lower()
    h = low.index("wrong headroom")
    assert "background_replaced=true" in low[h:h + 700]
