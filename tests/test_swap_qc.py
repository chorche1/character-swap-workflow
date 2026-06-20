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
    QCReject,
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


def test_qc_rejects_preserved_for_every_failed_take(monkeypatch, tmp_path):
    """Hugo 2026-06-20: every image QC rejects must be SAVED (snapshotted before
    the next attempt overwrites it), not lost, so the user can see them. One
    reject per retry; the FINAL kept take stays at variant.path (not duplicated
    into qc_rejects)."""
    job, jc, v = _job(tmp_path)
    prompts: list = []
    bad = QCVerdict(False, "wrong person", "fix it")
    _stub(monkeypatch, job, [bad, bad, bad], prompts)
    _run(job, jc, v)
    n_retries = runner.settings.swap_qc_max_retries
    assert len(v.qc_rejects) == n_retries          # intermediates only
    for i, rej in enumerate(v.qc_rejects, start=1):
        assert rej.attempt == i
        assert rej.kind == "swap"
        assert rej.reason == "wrong person"
        assert Path(rej.path).exists()             # actually on disk, not overwritten
    assert Path(v.path).exists()                   # final kept take still there


def test_qc_rejects_accumulate_across_inplace_regeneration(monkeypatch, tmp_path):
    """retry_single_variant / ✎↻ / 🪄 re-run the SAME variant_id with attempt
    reset to 1. A re-roll must NOT overwrite the rejects preserved from the
    previous run — every QC-failed image must survive, and the snapshot files
    must stay distinct (regression for the attempt-based filename collision)."""
    job, jc, v = _job(tmp_path)
    bad = QCVerdict(False, "wrong person", "fix it")
    n = runner.settings.swap_qc_max_retries
    # Run 1: exhaust retries → n preserved rejects.
    _stub(monkeypatch, job, [bad] * (n + 1), [])
    _run(job, jc, v)
    assert len(v.qc_rejects) == n
    # Run 2: in-place regen of the SAME variant → rejects ACCUMULATE on top.
    _stub(monkeypatch, job, [bad] * (n + 1), [])
    _run(job, jc, v)
    paths = [r.path for r in v.qc_rejects]
    assert len(paths) == 2 * n
    assert len(set(paths)) == 2 * n             # no filename collisions
    for p in paths:
        assert Path(p).exists()                 # nothing overwritten/destroyed


def test_qc_rejects_serialized_in_job_dict(tmp_path):
    """The preserved rejects reach the frontend with web-servable /files URLs."""
    from character_swap import api
    from character_swap.config import settings
    job, jc, v = _job(tmp_path)
    rp = settings.output_dir / "j1" / "cA" / "variant_v1.qcreject1.png"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_bytes(b"x")
    v.status = VariantStatus.READY
    v.qc_status = "failed"
    v.qc_reason = "wrong person"
    v.qc_rejects = [QCReject(path=str(rp), reason="wrong person",
                             attempt=1, kind="swap")]
    d = api._job_to_dict(job)
    img = d["characters"]["cA"]["images"][0]
    assert img["qc_status"] == "failed"
    assert len(img["qc_rejects"]) == 1
    rej = img["qc_rejects"][0]
    assert rej["url"].startswith("/files/output/")
    assert rej["reason"] == "wrong person"
    assert rej["attempt"] == 1


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


def _wire_inspect(monkeypatch, call_behavior):
    """Stub the Anthropic layer under inspect_variant. `call_behavior(n)`
    runs per API attempt (1-based) — raise to simulate errors."""
    from character_swap.clients import anthropic_client
    from character_swap.config import settings
    monkeypatch.setattr(settings, "swap_qc_enabled", True, raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "k", raising=False)
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "text", "text": str(p)})
    calls: list = []

    def fake_call(**kw):
        calls.append(kw)
        return call_behavior(len(calls))
    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake_call)
    monkeypatch.setattr(
        anthropic_client, "extract_tool_call",
        lambda resp, name: {"passed": True, "reason": "",
                            "corrective_hint": ""})
    sleeps: list = []
    monkeypatch.setattr(swap_qc.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def _inspect(tmp_path):
    return swap_qc.inspect_variant(scene_image=tmp_path / "s.png",
                                   character_image=tmp_path / "c.png",
                                   result_image=tmp_path / "r.png")


def test_qc_retries_through_rate_limit_burst(monkeypatch, tmp_path):
    """Backlog #4 (audit 2026-06-12): an Anthropic 429 burst disabled QC for
    the whole batch — 21 images/clips shipped unchecked on 06-11. Rate
    limits are transient: the judge call must back off and retry, not skip."""
    def behavior(n):
        if n <= 2:
            raise RuntimeError("Error code: 429 - rate_limit_error")
        return object()
    calls, sleeps = _wire_inspect(monkeypatch, behavior)

    verdict = _inspect(tmp_path)
    assert verdict is not None and verdict.passed
    assert len(calls) == 3
    assert sleeps == [2.0, 8.0]


def test_qc_persistent_rate_limit_skips_after_backoff(monkeypatch, tmp_path):
    def behavior(n):
        raise RuntimeError("overloaded_error (529)")
    calls, sleeps = _wire_inspect(monkeypatch, behavior)

    assert _inspect(tmp_path) is None       # never blocks the pipeline
    assert len(calls) == 4                  # 1 + 3 retries
    assert sleeps == [2.0, 8.0, 20.0]


def test_qc_non_rate_limit_error_skips_immediately(monkeypatch, tmp_path):
    def behavior(n):
        raise ValueError("malformed something")
    calls, sleeps = _wire_inspect(monkeypatch, behavior)

    assert _inspect(tmp_path) is None
    assert len(calls) == 1                  # no pointless retries
    assert sleeps == []


def test_needs_reroll_routes_failure_classes():
    """Backlog #12: geometry/content-base failures re-roll fresh; in-place
    fixable classes keep the minimal-change repair."""
    nr = swap_qc.needs_reroll
    assert nr("WRONG BACKGROUND: the original kitchen was kept")
    assert nr("WRONG FRAMING / ZOOM: clearly wider than the scene")
    assert nr("BROKEN IMAGE: mostly black output")
    assert nr("MISSING/EXTRA PEOPLE: a second person appeared")
    # Repairable in place — must NOT re-roll:
    assert not nr("WRONG BACKGROUND SYMBOL: flag lost its star canton")
    assert not nr("WRONG HEADROOM: empty sky added above the head")
    assert not nr("WRONG GAZE / GESTURE: stares into the camera")
    assert not nr("WRONG PERSON: face is a blend of the two")
    assert not nr("SEVERE ARTIFACTS: six fingers on the left hand")
    assert not nr(None) and not nr("")


def test_reroll_class_skips_repair_mode(monkeypatch, tmp_path):
    """A WRONG BACKGROUND fail on attempt 1 must NOT go through repair (the
    repair contract says 'keep background unchanged' — fighting the fix);
    it re-rolls fresh from the original scene with the hint."""
    job, jc, v = _job(tmp_path)
    prompts: list = []
    _stub(monkeypatch, job,
          [QCVerdict(False, "WRONG BACKGROUND: original environment kept",
                     "Replace the surroundings with the reference."),
           QCVerdict(True, "", "")], prompts)
    _run(job, jc, v)
    assert v.qc_status == "passed"
    assert len(prompts) == 2
    assert prompts[1].startswith("BASE PROMPT")        # re-roll, not repair
    assert "rejected by quality control" in prompts[1]
    assert "almost-correct" not in prompts[1]


def test_repairable_class_still_uses_repair_mode(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)
    prompts: list = []
    _stub(monkeypatch, job,
          [QCVerdict(False, "WRONG HEADROOM: sky added above the head",
                     "Crop away the added headroom."),
           QCVerdict(True, "", "")], prompts)
    _run(job, jc, v)
    assert len(prompts) == 2
    assert prompts[1].startswith("Image 1 is an almost-correct")
    assert "EXCEPT where the fix" in prompts[1]


def test_qc_prompt_covers_gaze_and_prop_precision():
    """Backlog #14+#15 (audit 2026-06-12): originals look down at the task
    but variants stare at camera (passed QC); 3 kiwi halves became a staged
    6-slice flower; a foreground desk vanished. All three classes need
    explicit criteria."""
    text = " ".join(swap_qc.QC_SYSTEM.split())
    assert "WRONG GAZE / GESTURE" in text
    assert "staring into the camera is a FAIL" in text
    assert "prop COUNT" in text
    assert "foreground furniture" in text


def test_qc_prompt_covers_outfit_and_user_intent():
    """Backlog #16+#17 (audit 2026-06-12): no outfit criterion existed (the
    glove-bleed class passed), and the judge never saw the user's own prompt
    so swap-with-modifications jobs were false-failed and 'repaired' back."""
    text = " ".join(swap_qc.QC_SYSTEM.split())
    assert "WRONG OUTFIT" in text
    assert "custom_outfit=" in text
    assert "USER INTENT" in text
    assert "NEVER fail a deviation the user intent clearly requests" in text


def test_qc_passes_outfit_and_intent_to_judge(monkeypatch, tmp_path):
    calls, _ = _wire_inspect(monkeypatch, lambda n: object())
    verdict = swap_qc.inspect_variant(
        scene_image=tmp_path / "s.png", character_image=tmp_path / "c.png",
        result_image=tmp_path / "r.png",
        outfit_text="a red hoodie",
        user_intent="swap the person but give them sunglasses")
    assert verdict is not None
    (call,) = calls
    first_text = call["messages"][0]["content"][0]["text"]
    assert 'custom_outfit="a red hoodie"' in first_text
    assert "USER INTENT" in first_text
    assert "sunglasses" in first_text


def test_qc_runner_derives_outfit_from_job_field(monkeypatch, tmp_path):
    """The outfit flag must come from Job.outfit_mode, not just the legacy
    prompt sniff (backlog #16)."""
    import asyncio
    from character_swap import runner
    from character_swap.models import Job as J

    job, jc, v = _job(tmp_path)
    job.outfit_mode = "character"
    job.prompt = "anything"
    seen: dict = {}

    def fake_inspect(**kw):
        seen.update(kw)
        return QCVerdict(True, "", "")
    prompts: list = []
    _stub(monkeypatch, job, [], prompts)
    monkeypatch.setattr(runner.swap_qc, "inspect_variant", fake_inspect)
    _run(job, jc, v)
    assert seen["outfit_from_character"] is True
    assert seen["user_intent"] == "anything"


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
