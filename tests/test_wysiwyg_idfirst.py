"""WYSIWYG identity-first swap prompts (Hugo 2026-06-16).

gpt2-id-swap RUNS prompts identity-first (Image 1 = person, Image 2 = scene).
The whole generation backbone (storage, the Director plan, pipeline dispatch)
stays SCENE-FIRST canonical — `pipeline._dispatch_variant` still flips
scene-first → identity-first at gen time. Only the USER-FACING boundaries flip,
so the Step-2 box and the ✎↻ / 🪄 modals SHOW and ACCEPT identity-first text.

These tests lock the round-trip (what the user types == what the engine runs),
the engine-gating + symmetry of the boundary flip, the engine-aware default,
and the `prompt_display` field — WITHOUT touching the generation path (a
regression there would silently swap the scene into the character).
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api, pipeline
from character_swap.models import GeneratedImage, Job, JobCharacter


# --- the boundary flip helper ----------------------------------------------

def test_flip_only_applies_to_gpt2_id_swap():
    p = "Identity from Image 1, scene is Image 2."
    flipped = api._flip_swap_orientation_for_idfirst(p, "gpt2-id-swap")
    assert flipped == "Identity from Image 2, scene is Image 1."
    # Every other engine is scene-first end to end → no-op.
    for model in ("gpt-image", "nbp-swap", "seedream-edit-swap", None, ""):
        assert api._flip_swap_orientation_for_idfirst(p, model) == p


def test_flip_is_symmetric_and_empty_safe():
    p = "Use Image 1 as the master scene. Image 2 is the person."
    once = api._flip_swap_orientation_for_idfirst(p, "gpt2-id-swap")
    twice = api._flip_swap_orientation_for_idfirst(once, "gpt2-id-swap")
    assert twice == p                      # flip is its own inverse
    for empty in ("", None):
        assert api._flip_swap_orientation_for_idfirst(empty, "gpt2-id-swap") == empty


# --- the crown-jewel round-trip: WYSIWYG ------------------------------------

def test_user_idfirst_prompt_round_trips_to_engine(monkeypatch, tmp_path):
    """A prompt typed in the identity-first USER view, after the API flips it to
    scene-first storage, is flipped BACK by dispatch — so the engine receives
    the user's ORIGINAL text verbatim. This is the whole point of WYSIWYG."""
    user_typed = ("Put the person from Image 1 into Image 2's scene; keep "
                  "Image 2's exact pose and the objects Image 2's person holds.")
    # 1) API input boundary (create_job / patch / retry / regen) → storage.
    stored = api._flip_swap_orientation_for_idfirst(user_typed, "gpt2-id-swap")
    assert stored != user_typed            # storage really is the other orientation

    # 2) Generation dispatch flips storage back to the engine's view.
    seen = {}
    monkeypatch.setattr(pipeline.openai_image, "generate",
                        lambda **kw: seen.update(kw) or b"png")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="X", prompt=stored, dest=tmp_path / "o.png", job_id=None)

    assert seen["prompt"] == user_typed                  # engine == what user typed
    assert seen["reference_images"] == [char, scene]     # identity-first ref order


def test_display_round_trip_matches_engine(monkeypatch, tmp_path):
    """The DISPLAY flip (storage → user view) must reproduce exactly what the
    engine runs: flip(stored) for a custom prompt == the dispatched prompt."""
    stored = "Master scene is Image 1. The replacement person is Image 2."
    display = api._flip_swap_orientation_for_idfirst(stored, "gpt2-id-swap")
    seen = {}
    monkeypatch.setattr(pipeline.openai_image, "generate",
                        lambda **kw: seen.update(kw) or b"png")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="X", prompt=stored, dest=tmp_path / "o.png", job_id=None)
    assert seen["prompt"] == display         # what you SEE == what the engine RUNS


# --- engine-aware default (get_swap_defaults) -------------------------------

def _defaults(image_model=None):
    return asyncio.run(api.get_swap_defaults(image_model=image_model))


def test_swap_defaults_identity_first_for_gpt2():
    d = _defaults("gpt2-id-swap")
    assert d["image_model"] == "gpt2-id-swap"
    # The compact identity-first prompt the engine actually runs.
    assert d["prompt"].startswith("Image 1 is only the identity reference")


def test_swap_defaults_scene_first_for_gpt_image():
    d = _defaults("gpt-image")
    assert d["image_model"] == "gpt-image"
    assert d["prompt"] == pipeline.GENERATION_PROMPT
    assert d["prompt"].startswith("Use Image 1 as the fixed master scene")


def test_swap_defaults_default_engine_is_gpt2_id_swap():
    # Unset image_model → the Swap-tab default engine (identity-first) since
    # 2026-06-16.
    assert _defaults()["image_model"] == "gpt2-id-swap"


# --- prompt_display on the job dict -----------------------------------------

def _job(image_model: str, prompt: str | None) -> Job:
    return Job(job_id="j1", scene_id="s1", scene_image_path="/x/s1.png",
               image_model=image_model, prompt=prompt,
               characters={"c1": JobCharacter(char_id="c1", name="A",
                                              source_image_path="/x/c1.png")})


def test_prompt_display_flips_for_gpt2_id_swap():
    stored = "Image 1 is the scene. Image 2 is the person to insert."
    d = api._job_to_dict(_job("gpt2-id-swap", stored))
    assert d["prompt"] == stored                                   # storage scene-first
    assert d["prompt_display"] == "Image 2 is the scene. Image 1 is the person to insert."


def test_prompt_display_is_noop_for_gpt_image_and_none():
    stored = "Image 1 is the scene. Image 2 is the person."
    assert api._job_to_dict(_job("gpt-image", stored))["prompt_display"] == stored
    assert api._job_to_dict(_job("gpt2-id-swap", None))["prompt_display"] is None


def test_variant_prompt_display_flips_for_gpt2(tmp_path):
    """The ✎↻ edit modal (plain Swap) prefills from the variant's
    prompt_display — must be identity-first for gpt2-id-swap so an unedited
    submit round-trips through the retry endpoint's input flip."""
    v = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                       prompt="Image 1 is the scene. Image 2 is the person.",
                       scene_id="s1")
    job = Job(job_id="j1", scene_id="s1", scene_image_path=str(tmp_path / "s1.png"),
              image_model="gpt2-id-swap",
              characters={"c1": JobCharacter(char_id="c1", name="A",
                          source_image_path=str(tmp_path / "c1.png"), images=[v])})
    vd = api._job_to_dict(job)["characters"]["c1"]["images"][0]
    assert vd["prompt"] == "Image 1 is the scene. Image 2 is the person."
    assert vd["prompt_display"] == "Image 2 is the scene. Image 1 is the person."


# --- create_job orientation (the CRITICAL project-default fix) ---------------

def _setup_scene_char(s):
    from character_swap.config import settings
    from character_swap.models import CharacterAsset, SceneAsset
    settings.scenes_dir.mkdir(parents=True, exist_ok=True)
    settings.characters_dir.mkdir(parents=True, exist_ok=True)
    (settings.scenes_dir / "s1.png").write_bytes(b"s")
    (settings.characters_dir / "c1.png").write_bytes(b"c")
    s.add_scene(SceneAsset(scene_id="s1", filename="s1.png", original_name="s1.png"))
    s.add_character(CharacterAsset(char_id="c1", name="A", filename="c1.png"))


def _create_job(body):
    from fastapi import BackgroundTasks
    return asyncio.run(api.create_job(body, BackgroundTasks()))   # bg task never runs


@pytest.fixture
def _store_no_keys(monkeypatch):
    from character_swap import state
    # Settings is a pydantic model → patch the METHODS on the class, not the
    # instance (instance setattr is intercepted/blocked by pydantic).
    monkeypatch.setattr(type(api.settings), "require_keys",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(type(api.settings), "has_provider",
                        lambda self, *a, **k: True)
    s = state.store()
    _setup_scene_char(s)
    return s


def test_create_job_does_not_double_flip_project_default(_store_no_keys):
    """REGRESSION (review 2026-06-16): an inherited project default is ALREADY
    scene-first canonical (patch_project flips on save). create_job must NOT
    flip it again, or the scene-first backbone flips a THIRD time at gen and
    reverses Image 1/2 for the whole job."""
    from character_swap.models import ProjectAsset
    scene_first = "Use Image 1 as the master scene. Image 2 is the person."
    _store_no_keys.add_project(ProjectAsset(project_id="p1", name="P",
                                            default_prompt=scene_first))
    out = _create_job(api.CreateJobBody(
        scene_id="s1", character_ids=["c1"], project_id="p1",
        image_model="gpt2-id-swap", prompt=None))
    job = _store_no_keys.get_job(out["job_id"])
    assert job.prompt == scene_first        # stored unchanged — NOT double-flipped


def test_create_job_flips_explicit_idfirst_prompt_to_storage(_store_no_keys):
    """An explicit prompt from the box arrives identity-first for gpt2-id-swap
    and must be flipped to scene-first storage (dispatch flips it back)."""
    out = _create_job(api.CreateJobBody(
        scene_id="s1", character_ids=["c1"], image_model="gpt2-id-swap",
        prompt="Identity from Image 1. Scene is Image 2."))
    job = _store_no_keys.get_job(out["job_id"])
    assert job.prompt == "Identity from Image 2. Scene is Image 1."
