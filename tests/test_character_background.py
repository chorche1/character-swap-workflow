"""Character-background swap default (Hugo 2026-06-21).

The swap phase (Swap + Reengineer) now defaults to taking the OUTPUT background
from the CHARACTER reference image instead of preserving the scene's. The scene
still supplies pose/framing/props; the person is relit to the character's own
environment. A per-job opt-out (`background_source="scene"`) restores the old
"preserve the scene background" behavior, and an uploaded replacement
("Image 3" / extra_reference_path) still wins over both.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from character_swap import api, pipeline, prompt_director, runner
from character_swap.models import CharStatus, Job, JobCharacter


def _flat(s: str) -> str:
    return " ".join(s.split()).lower()


# --- static builders --------------------------------------------------------

def test_edit_prompt_character_bg_pulls_from_image2():
    p = _flat(pipeline.build_edit_swap_prompt("scene", None,
                                              background_mode="character"))
    # Background source flips from the scene (Image 1) to the character (Image 2).
    assert "source of the background" in p
    assert "environment visible in image 2" in p
    assert "do not keep image 1's background" in p
    # Relight + framing-from-scene anchors.
    assert "match image 2's white balance" in p
    assert "keep image 1's framing" in p
    # The contradictory "do not carry background over from Image 2" must be gone
    # (in this mode the background DOES come from Image 2).
    assert "background or objects over from image 2" not in p
    assert "clothing, background or objects over from image 2" not in p


def test_edit_prompt_scene_bg_unchanged():
    """Opt-out byte-compatibility: scene mode == the pre-2026-06-21 default."""
    assert pipeline.build_edit_swap_prompt("scene") == pipeline.EDIT_SWAP_PROMPT
    p = _flat(pipeline.build_edit_swap_prompt("scene"))
    assert "recreate image 1 exactly" in p
    assert "background element" in p


def test_gpt_id_prompt_character_bg_pulls_from_image1():
    # gpt2-id-swap is identity-FIRST: Image 1 = character, Image 2 = scene.
    p = _flat(pipeline.build_gpt_id_swap_prompt("scene", None,
                                                background_mode="character"))
    assert "image 1 is also the source of the background" in p
    assert "replace the surroundings with image 1's environment" in p
    # bg_part relights to Image 1 → the scene-light integration line is dropped.
    assert "match the scene's own existing light" not in p
    # scene_keep no longer re-anchors the background to Image 2.
    assert "subject scale, pose and every object the person touches" in p


def test_gpt_id_prompt_scene_bg_keeps_scene_light():
    p = _flat(pipeline.build_gpt_id_swap_prompt())               # scene default
    assert "match the scene's own existing light" in p
    assert "pose, background and" in p                            # scene_keep


def test_all_modes_keep_camera_gaze():
    for mode in pipeline.SWAP_BACKGROUND_MODES:
        assert "directly into the camera" in pipeline.build_gpt_id_swap_prompt(
            "scene", None, background_mode=mode)
        assert "directly into the camera" in pipeline.build_edit_swap_prompt(
            "scene", None, background_mode=mode)


def test_stock_swap_prompts_covers_every_mode():
    stock = pipeline.stock_swap_prompts("scene", None)
    assert pipeline.GENERATION_PROMPT in stock
    assert pipeline.EDIT_SWAP_PROMPT in stock
    for mode in pipeline.SWAP_BACKGROUND_MODES:
        assert pipeline.build_edit_swap_prompt("scene", None,
                                               background_mode=mode) in stock


def test_unknown_background_mode_raises():
    import pytest
    with pytest.raises(ValueError):
        pipeline.build_edit_swap_prompt("scene", None, background_mode="nope")


# --- runner resolution ------------------------------------------------------

def _job(**kw) -> Job:
    base = dict(job_id="j1", scene_id="s1", scene_image_path="/tmp/s.png",
                scene_ids=["s1"], scene_image_paths=["/tmp/s.png"],
                image_model="gpt2-id-swap",
                characters={"c1": JobCharacter(char_id="c1", name="A",
                                               source_image_path="/tmp/c.png",
                                               status=CharStatus.QUEUED)})
    base.update(kw)
    return Job(**base)


def test_background_mode_default_is_character():
    assert runner._swap_background_mode(_job()) == "character"


def test_background_mode_opt_out_scene():
    assert runner._swap_background_mode(_job(background_source="scene")) == "scene"


def test_background_mode_uploaded_replacement_wins(tmp_path):
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"x")
    # Even with background_source="scene", an uploaded Image 3 takes over.
    j = _job(background_source="scene", extra_reference_path=str(bg))
    assert runner._swap_background_mode(j) == "replacement"


def test_engine_effective_prompt_is_character_bg_by_default():
    """A default (stock-prompt) gpt2-id-swap job runs the character-bg prompt."""
    j = _job(prompt=None)
    eff = runner.engine_effective_swap_prompt(j, pipeline.GENERATION_PROMPT)
    # engine_effective returns the scene-first (storage) orientation; dispatch
    # re-flips. Flip it to the engine's run-view to compare with the builder.
    run_view = pipeline._flip_image_roles(eff)
    assert run_view == pipeline.build_gpt_id_swap_prompt(
        "scene", None, background_mode="character")


def test_engine_effective_prompt_scene_opt_out():
    j = _job(prompt=None, background_source="scene")
    eff = runner.engine_effective_swap_prompt(j, pipeline.GENERATION_PROMPT)
    run_view = pipeline._flip_image_roles(eff)
    assert run_view == pipeline.build_gpt_id_swap_prompt(
        "scene", None, background_mode="scene")


# --- API defaults (WYSIWYG) -------------------------------------------------

def test_swap_defaults_default_to_character_bg():
    data = asyncio.run(api.get_swap_defaults(image_model="gpt2-id-swap"))
    assert "Image 1 is ALSO the source of the BACKGROUND" in data["prompt"]


def test_swap_defaults_scene_opt_out():
    data = asyncio.run(api.get_swap_defaults(image_model="gpt2-id-swap",
                                             background_source="scene"))
    assert "Image 1 is ALSO the source of the BACKGROUND" not in data["prompt"]
    assert "Match the scene's own existing light" in data["prompt"]


# --- Director (opt-in) is mode-aware ----------------------------------------

def test_director_swap_system_uses_character_rule(monkeypatch, tmp_path):
    from character_swap.clients import anthropic_client
    captured = {}

    def fake_msgs(**kw):
        captured["system"] = kw.get("system")
        return object()

    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake_msgs)
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: None)        # → returns None, fine
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "text", "text": str(p)})

    prompt_director.direct_swap(
        user_prompt="x", characters=[("c1", "A", tmp_path / "c.png")],
        scenes=[("s1", tmp_path / "s.png")], images_per_character=1,
        background_mode="character")
    assert "USE THE CHARACTER'S OWN ENVIRONMENT" in captured["system"]
    assert "{background_rule}" not in captured["system"]   # placeholder filled


# --- review fixes (2026-06-21) ------------------------------------------------

def test_style_light_clause_not_scene_in_character_mode():
    """The trailing Style sentence must not re-anchor ambient light to the
    scene once the Integration block relit to the character's environment."""
    for build in (pipeline.build_edit_swap_prompt, pipeline.build_gpt_id_swap_prompt):
        scene = _flat(build("scene", None, background_mode="scene"))
        char = _flat(build("scene", None, background_mode="character"))
        assert "the scene's own mundane ambient light" in scene
        assert "the scene's own mundane ambient light" not in char
        assert "new environment's own ordinary ambient light" in char


def test_variant_prompt_display_is_engine_effective(tmp_path):
    """The ✎↻ modal prefill (prompt_display) must show the CHARACTER-bg prompt
    the engine actually ran, not the raw stored stock GENERATION_PROMPT."""
    from character_swap.models import GeneratedImage, VariantStatus
    jc = JobCharacter(char_id="c1", name="A", source_image_path="/tmp/c.png",
                      status=CharStatus.AWAITING_APPROVAL)
    jc.images = [GeneratedImage(variant_id="v1", scene_id="s1",
                                path="/tmp/v1.png", prompt=pipeline.GENERATION_PROMPT,
                                status=VariantStatus.READY)]
    job = Job(job_id="j1", scene_id="s1", scene_image_path="/tmp/s.png",
              scene_ids=["s1"], scene_image_paths=["/tmp/s.png"],
              image_model="gpt2-id-swap", characters={"c1": jc})
    d = api._job_to_dict(job)
    disp = d["characters"]["c1"]["images"][0]["prompt_display"]
    assert "Image 1 is ALSO the source of the BACKGROUND" in disp


def test_end_frame_swap_passes_replacement_reference(monkeypatch, tmp_path):
    """A replacement-background job's end-frame swap must supply Image 3 so the
    rebuilt 'replacement' prompt's reference exists (matches the start frame)."""
    bg = tmp_path / "bg.png"; bg.write_bytes(b"x")
    pose = tmp_path / "pose.png"; pose.write_bytes(b"x")
    jc = JobCharacter(char_id="c1", name="A",
                      source_image_path=str(tmp_path / "c.png"),
                      status=CharStatus.AWAITING_APPROVAL)
    job = Job(job_id="j1", scene_id="s1", scene_image_path=str(tmp_path / "s.png"),
              scene_ids=["s1"], scene_image_paths=[str(tmp_path / "s.png")],
              image_model="gpt2-id-swap", characters={"c1": jc},
              extra_reference_path=str(bg))
    captured = {}

    def fake_gen(**kw):
        captured.update(kw)
        Path(kw["dest"]).write_bytes(b"img")
        return Path(kw["dest"])

    monkeypatch.setattr(pipeline, "generate_variant", fake_gen)
    monkeypatch.setattr(runner, "_output_dir", lambda j, c: tmp_path)
    runner._ensure_end_frame_swap(job, jc, "s1", str(pose), force=True)
    assert captured["background_mode"] == "replacement"
    assert captured["extra_reference_image"] == bg


def test_director_swap_system_scene_rule_by_default(monkeypatch, tmp_path):
    from character_swap.clients import anthropic_client
    captured = {}
    monkeypatch.setattr(anthropic_client, "messages_with_tools",
                        lambda **kw: captured.setdefault("system", kw.get("system")))
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: None)
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "text", "text": str(p)})
    prompt_director.direct_swap(
        user_prompt="x", characters=[("c1", "A", tmp_path / "c.png")],
        scenes=[("s1", tmp_path / "s.png")], images_per_character=1)
    assert "BACKGROUND — DO NOT CHANGE" in captured["system"]
