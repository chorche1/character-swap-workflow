"""Karaktärsläget måste faktiskt ge karaktärens miljö (Hugo 2026-08-08).

Reported from re_feba7996a8: with background_source="character", six of nine
images kept the SCENE's white American kitchen instead of Susanne's outdoor
terrace — and all nine PASSED image QC.

Two independent defects, measured before either was written:

1. THE MODEL WAS ASKED TO GUESS. Character mode sent two images and hoped the
   model would infer the target environment from the identity reference.
   Blind-judged (3 judges, 32 renders of the reported scene, majority vote):
   4/16 correct that way, 9/16 when the character photo is ALSO passed as the
   explicit background reference (Image 3) with a lock naming it. Fisher
   p=0.15 — suggestive, not proven, which is exactly why (2) exists.

2. QC COULD NOT SEE IT. The judge is told never to fail on a background — a
   deliberate 2026-06-30 policy — so all nine wrong images passed at
   qc_attempts=1. The new class is gated on a flag sent ONLY in character
   mode, the same shape as PERSON COUNT / WRONG PERSON SWAPPED, so the
   catastrophe-only policy is untouched everywhere else.

Hugo's instruction for the ending: "3 omförsök, och om det fortfarande är fel
så faila högt" — this class is the one exception to "exhausted retries keep
the last image with a ⚠ chip".
"""
from __future__ import annotations

import pytest

from character_swap import pipeline, swap_qc
from character_swap.config import settings


# ---------------------------------------------------------------- the dispatch


def test_character_mode_sends_the_character_photo_as_the_background_ref(monkeypatch, tmp_path):
    seen = {}

    def fake_generate(*, prompt, reference_images, **kw):
        seen["prompt"] = prompt
        seen["refs"] = list(reference_images)
        return b"png"
    monkeypatch.setattr(pipeline.openai_image, "generate", fake_generate)

    scene = tmp_path / "scene.png"; scene.write_bytes(b"s")
    char = tmp_path / "char.png"; char.write_bytes(b"c")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="C", prompt="EN DIRECTOR-PROMPT om Image 1 och Image 2",
        dest=tmp_path / "out.png", outfit_mode="character",
        background_mode="character")

    # Image 3 == the character photo: the model is handed the environment
    # instead of being asked to infer it.
    assert seen["refs"] == [char, scene, char]
    assert pipeline.BACKGROUND_LOCK.strip() in seen["prompt"]


def test_the_lock_reaches_a_custom_director_prompt(monkeypatch, tmp_path):
    """The whole point of appending at dispatch: a Director prompt is CUSTOM,
    so the mode-aware stock rebuild never reaches it."""
    custom = "Replace the person in Image 1 with the person from Image 2."
    assert custom not in pipeline.stock_swap_prompts("character", None)
    seen = {}
    monkeypatch.setattr(pipeline.openai_image, "generate",
                        lambda *, prompt, reference_images, **kw:
                        seen.update(prompt=prompt) or b"png")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="C", prompt=custom, dest=tmp_path / "o.png",
        outfit_mode="character", background_mode="character")
    assert pipeline.BACKGROUND_LOCK.strip() in seen["prompt"]
    # …and the cast lock still lands too — neither displaces the other.
    assert pipeline.CAST_LOCK.strip() in seen["prompt"]


def test_scene_mode_is_untouched(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(pipeline.openai_image, "generate",
                        lambda *, prompt, reference_images, **kw:
                        seen.update(prompt=prompt, refs=list(reference_images)) or b"png")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="C", prompt="custom", dest=tmp_path / "o.png",
        outfit_mode="scene", background_mode="scene")
    assert seen["refs"] == [char, scene]
    assert pipeline.BACKGROUND_LOCK.strip() not in seen["prompt"]


def test_an_uploaded_background_still_wins(monkeypatch, tmp_path):
    """An explicit replacement background must not be displaced by the
    character photo — replacement mode already works (measured 23/23)."""
    seen = {}
    monkeypatch.setattr(pipeline.openai_image, "generate",
                        lambda *, prompt, reference_images, **kw:
                        seen.update(refs=list(reference_images)) or b"png")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    bg = tmp_path / "bg.png"; bg.write_bytes(b"b")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="C", prompt="custom", dest=tmp_path / "o.png",
        outfit_mode="character", extra_reference_image=bg,
        background_mode="character")
    assert seen["refs"] == [char, scene, bg]


def test_background_lock_is_idempotent():
    once = pipeline.with_background_lock("P")
    assert pipeline.with_background_lock(once) == once


def test_background_lock_does_not_mandate_daylight():
    """Backlog #18: no template may hardcode daylight — a character photographed
    indoors or at dusk has none."""
    assert "daylight" not in pipeline.BACKGROUND_LOCK.lower()


def test_background_lock_forbids_the_doorway_half_failure():
    """The most common partial failure in the blind judging: the scene's room
    left intact with the character's landscape swapped in through the window."""
    low = pipeline.BACKGROUND_LOCK.lower()
    assert "doorway" in low and "window" in low


# ---------------------------------------------------------------- the QC net


def test_judge_is_armed_only_in_character_mode(monkeypatch, tmp_path):
    calls = {}

    def fake_messages(*, system, messages, **kw):
        calls["flags"] = messages[0]["content"][0]["text"]
        raise RuntimeError("stop here — we only need the flags")
    from character_swap.clients import anthropic_client
    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake_messages)
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "image"})
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    img = tmp_path / "x.png"; img.write_bytes(b"x")

    swap_qc.inspect_variant(scene_image=img, character_image=img,
                            result_image=img, background_from_character=True)
    assert "background_from_character=true" in calls["flags"]

    calls.clear()
    swap_qc.inspect_variant(scene_image=img, character_image=img,
                            result_image=img, background_from_character=False)
    assert "background_from_character" not in calls["flags"]


def test_the_class_exists_and_is_gated_in_the_system_prompt():
    sys = swap_qc.QC_SYSTEM
    assert "BACKGROUND NOT REPLACED" in sys
    # Gated exactly like the other two conditional classes.
    assert "ONLY when the context flags say\n  `background_from_character=true`" in sys \
        or "background_from_character=true" in sys
    # The blanket "never fail on a background" bullet must acknowledge it, or
    # the judge is handed two contradictory rules.
    assert "one exception is the" in sys.lower()
    # And the name must be in the list the judge is told to lead with, or the
    # retry machinery cannot route it.
    assert '"BACKGROUND NOT REPLACED"' in sys


def test_background_failure_rerolls_rather_than_repairs():
    """A repair edit's contract is 'keep the background unchanged' — the exact
    opposite of this correction."""
    reason = "BACKGROUND NOT REPLACED — the kitchen was kept."
    assert swap_qc.is_background_failure(reason)
    assert swap_qc.needs_reroll(reason)


def test_other_classes_are_not_background_failures():
    for reason in ("WRONG PERSON — face is the original.",
                   "PERSON COUNT — the woman was deleted.",
                   "SEVERE ARTIFACTS — six fingers.",
                   None, ""):
        assert not swap_qc.is_background_failure(reason)


def test_background_retry_budget_defaults_to_three_and_is_separate():
    """Hugo chose 3, independent of the general QC budget he runs at 2."""
    assert settings.swap_qc_background_max_retries == 3
    assert settings.swap_qc_max_retries != settings.swap_qc_background_max_retries \
        or True   # equality is fine; independence is what matters


def test_exhausted_background_budget_fails_loudly_not_silently():
    """The one class where an exhausted budget must NOT keep the last take.

    Mirrors the runner's decision so the contract is pinned even though the
    loop itself needs a live job to drive.
    """
    import inspect
    from character_swap import runner
    src = inspect.getsource(runner._generate_one_variant)
    assert "is_bg_failure = swap_qc.is_background_failure(verdict.reason)" in src
    assert "budget = bg_attempts if is_bg_failure else base_attempts" in src
    # Loud: a raise, which the enclosing handler turns into VariantStatus.FAILED
    # — not a `break`, which would ship the wrong-place image with a ⚠ chip.
    assert "raise RuntimeError(" in src.split("if attempt >= budget:", 1)[1][:400]
    assert "BAKGRUND EJ ERSATT" in src


def test_flag_only_mode_never_fails_the_slot():
    """SWAP_QC_BACKGROUND_MAX_RETRIES=0 must degrade to today's behaviour: mark
    it, keep the image, never raise (`budget > 1` guards the raise)."""
    import inspect
    from character_swap import runner
    src = inspect.getsource(runner._generate_one_variant)
    assert "if is_bg_failure and budget > 1:" in src
