"""Scene-level image change for ALL characters (Hugo 2026-06-13).

Flow: the user describes the change in plain language → ONE Claude call
(direct_scene_prompt_rewrite) rewrites the scene's swap prompt (pure
preview) → POST regen_images regenerates every character's slot for that
scene IN PLACE with the new prompt (approvals withdrawn, scene marked
dirty post-gate, finals stale).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, prompt_director
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    SceneAsset,
    VariantStatus,
)


def _job(*, movement: bool, n_chars: int = 2):
    def _char(cid):
        images = [
            GeneratedImage(variant_id=f"{cid}-v1", path="/1.png", prompt="cur s1",
                           scene_id="s1", status=VariantStatus.READY),
            GeneratedImage(variant_id=f"{cid}-v2", path="/2.png", prompt="cur s2",
                           scene_id="s2", status=VariantStatus.READY),
        ]
        return JobCharacter(char_id=cid, name=cid, source_image_path="/c.png",
                            status=CharStatus.APPROVED, images=images,
                            approved_variant_ids=[f"{cid}-v1", f"{cid}-v2"])
    chars = {f"c{i}": _char(f"c{i}") for i in range(n_chars)}
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
               scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
               characters=chars, origin="reengineer:re_t",
               movement_prompt=("animate" if movement else None),
               movement_prompts=({"s1": "old", "s2": "old"} if movement else {}),
               durations_by_scene=({"s1": 5, "s2": 5} if movement else {}))


def _state(status="awaiting_approval", finals=False):
    st = {"re_id": "re_t", "status": status, "job_id": "j1", "n_scenes": 2,
          "scenes": [
              {"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
               "duration": 5.0, "motion_prompt": "p one", "speech": "",
               "summary": "one"},
              {"idx": 1, "scene_id": "s2", "start": 5.0, "end": 10.0,
               "duration": 5.0, "motion_prompt": "p two", "speech": "",
               "summary": "two"},
          ]}
    if finals:
        st["finals"] = {"c0": {"status": "done", "final_path": "/f.mp4"}}
    return st


@pytest.fixture
def wired(monkeypatch):
    """Fake store + state IO for api-level endpoint tests (mirror of
    test_reengineer_edit_state)."""
    box = {"job": None, "states": {}, "scenes": {}}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j1" else None

        def update_job(self, j):
            box["job_updated"] = True

        def get_scene(self, sid):
            return box["scenes"].get(sid)
    monkeypatch.setattr(api, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)
    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    from character_swap import runner_reengineer
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state", load_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state", save_state)
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    return box


def _register_frame(box):
    """SceneAsset + real frame file so _scene_frame_path resolves."""
    settings.scenes_dir.mkdir(parents=True, exist_ok=True)
    (settings.scenes_dir / "s1.png").write_bytes(b"png")
    box["scenes"]["s1"] = SceneAsset(scene_id="s1", filename="s1.png",
                                     original_name="s1.png")


# -------------------------------------------------- direct_scene_prompt_rewrite

def _stub_director(monkeypatch, tool_payload, captured):
    def fake_messages(**kw):
        captured.update(kw)
        return object()
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "messages_with_tools", fake_messages)
    monkeypatch.setattr(prompt_director.anthropic_client, "extract_tool_call",
                        lambda resp, name: tool_payload)
    monkeypatch.setattr(prompt_director.anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "text", "text": str(p)})


def test_rewrite_returns_prompt_with_style_clause(monkeypatch, tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    captured = {}
    _stub_director(monkeypatch, {"prompt": "NEW PROMPT"}, captured)
    out = prompt_director.direct_scene_prompt_rewrite(
        scene_id="s1", frame_path=frame,
        current_prompt="old scene prompt" + prompt_director.ORGANIC_STYLE_CLAUSE,
        change_request="byt ut kaffemuggen mot ett glas vatten")
    # Camera-gaze policy (Hugo 2026-06-13) guarantees the gaze sentence
    # before the code-appended style clause.
    assert out == ("NEW PROMPT " + prompt_director.CAMERA_GAZE_SENTENCE
                   + prompt_director.ORGANIC_STYLE_CLAUSE)
    # Director sees the STRIPPED current prompt + the change request.
    texts = " ".join(b.get("text", "") for b in captured["messages"][0]["content"])
    assert "old scene prompt" in texts
    assert prompt_director.ORGANIC_STYLE_CLAUSE.strip() not in texts
    assert "kaffemuggen" in texts
    assert captured["phase"] == "director_rewrite"


def test_rewrite_none_when_tool_not_called(monkeypatch, tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    _stub_director(monkeypatch, None, {})
    assert prompt_director.direct_scene_prompt_rewrite(
        scene_id="s1", frame_path=frame, current_prompt="p",
        change_request="change it") is None


def test_rewrite_none_on_empty_change(monkeypatch, tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    assert prompt_director.direct_scene_prompt_rewrite(
        scene_id="s1", frame_path=frame, current_prompt="p",
        change_request="  ") is None


def test_strip_style_clauses_removes_appended_clauses():
    p = ("scene part" + prompt_director.ORGANIC_STYLE_CLAUSE
         + prompt_director.SWAP_AVOID_CLAUSE)
    assert prompt_director.strip_style_clauses(p) == "scene part"


def test_replace_scene_prompt_in_plan_updates_all_chars():
    plan = prompt_director.plan_from_scene_prompts(
        "intent", {"s1": "old1", "s2": "old2"}, [("cA", "A"), ("cB", "B")])
    assert prompt_director.replace_scene_prompt_in_plan(plan, "s1", "NEW")
    for cp in plan.characters:
        by_scene = {sp.scene_id: sp for sp in cp.scenes}
        assert all(v.prompt == "NEW" for v in by_scene["s1"].variants)
        assert all(v.prompt == "old2" for v in by_scene["s2"].variants)
    assert not prompt_director.replace_scene_prompt_in_plan(plan, "nope", "x")


# -------------------------------------------------------- rewrite_prompt endpoint

def test_rewrite_endpoint_returns_preview(wired, monkeypatch):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    _register_frame(wired)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    seen = {}

    def fake_rewrite(**kw):
        seen.update(kw)
        return "REWRITTEN"
    monkeypatch.setattr(prompt_director, "direct_scene_prompt_rewrite",
                        fake_rewrite)
    out = asyncio.run(api.reengineer_rewrite_scene_prompt(
        "re_t", 0, api.ReRewritePromptBody(change="byt mugg mot glas")))
    assert out["prompt"] == "REWRITTEN"
    assert out["current_prompt"] == "cur s1"        # approved slot's prompt
    assert out["scene_id"] == "s1"
    assert seen["change_request"] == "byt mugg mot glas"
    assert seen["current_prompt"] == "cur s1"
    # PURE PREVIEW — nothing persisted.
    assert "re_t" not in wired["states"] or \
        wired["states"]["re_t"]["scenes"][0].get("dirty") is None


def test_rewrite_endpoint_502_when_director_fails(wired, monkeypatch):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    _register_frame(wired)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(prompt_director, "direct_scene_prompt_rewrite",
                        lambda **kw: None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_rewrite_scene_prompt(
            "re_t", 0, api.ReRewritePromptBody(change="x")))
    assert e.value.status_code == 502


def test_rewrite_endpoint_503_without_key(wired, monkeypatch):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_rewrite_scene_prompt(
            "re_t", 0, api.ReRewritePromptBody(change="x")))
    assert e.value.status_code == 503


def test_rewrite_endpoint_400_on_empty_change(wired, monkeypatch):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_rewrite_scene_prompt(
            "re_t", 0, api.ReRewritePromptBody(change="  ")))
    assert e.value.status_code == 400


def test_rewrite_endpoint_404_bad_idx(wired, monkeypatch):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_rewrite_scene_prompt(
            "re_t", 9, api.ReRewritePromptBody(change="x")))
    assert e.value.status_code == 404


# --------------------------------------------------------- regen_images endpoint

def test_regen_images_withdraws_approvals_and_queues(wired):
    job = _job(movement=True)
    wired["job"] = job
    wired["states"]["re_t"] = _state("done", finals=True)
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_regen_scene_images(
        "re_t", 0, bg, api.ReRegenImagesBody(prompt="NEW PROMPT",
                                             change="byt mugg mot glas")))
    # One slot per character, the s1-approved one.
    assert out["regen_variants"] == {"c0": "c0-v1", "c1": "c1-v1"}
    for cid in ("c0", "c1"):
        jc = job.characters[cid]
        assert f"{cid}-v1" not in (jc.approved_variant_ids or [])
        assert f"{cid}-v2" in (jc.approved_variant_ids or [])   # s2 untouched
    # Background fan-out queued once with (job_id, prompt, targets, change).
    assert len(bg.tasks) == 1
    assert bg.tasks[0].args[1:] == ("j1", "NEW PROMPT",
                                    {"c0": "c0-v1", "c1": "c1-v1"},
                                    "byt mugg mot glas")
    saved = wired["states"]["re_t"]
    assert saved["scenes"][0]["dirty"] is True       # post-gate: clip is stale
    assert saved["finals_stale"] is True


def test_regen_images_at_gate_no_dirty(wired):
    job = _job(movement=False)
    wired["job"] = job
    wired["states"]["re_t"] = _state("awaiting_approval")
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_regen_scene_images(
        "re_t", 0, bg, api.ReRegenImagesBody(prompt="NEW")))
    saved = wired["states"]["re_t"]
    assert "dirty" not in saved["scenes"][0]         # no clips exist yet
    assert len(bg.tasks) == 1


def test_regen_images_updates_cached_director_plan(wired):
    job = _job(movement=True)
    job.director_prompts_json = prompt_director.plan_from_scene_prompts(
        "intent", {"s1": "old1", "s2": "old2"},
        [(cid, cid) for cid in job.characters]).model_dump_json()
    job.use_director = True
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    asyncio.run(api.reengineer_regen_scene_images(
        "re_t", 0, BackgroundTasks(), api.ReRegenImagesBody(prompt="NEW")))
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        job.director_prompts_json)
    for cp in plan.characters:
        by_scene = {sp.scene_id: sp for sp in cp.scenes}
        assert all(v.prompt == "NEW" for v in by_scene["s1"].variants)
        assert all(v.prompt == "old2" for v in by_scene["s2"].variants)


def test_regen_images_400_on_empty_prompt(wired):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_regen_scene_images(
            "re_t", 0, BackgroundTasks(), api.ReRegenImagesBody(prompt=" ")))
    assert e.value.status_code == 400


def test_regen_images_409_when_all_slots_generating(wired):
    job = _job(movement=True)
    for jc in job.characters.values():
        for v in jc.images:
            if v.scene_id == "s1":
                v.status = VariantStatus.GENERATING
        jc.approved_variant_ids = [f"{jc.char_id}-v2"]
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_regen_scene_images(
            "re_t", 0, BackgroundTasks(), api.ReRegenImagesBody(prompt="NEW")))
    assert e.value.status_code == 409


def test_regen_images_blocked_mid_phase(wired):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("swapping")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_regen_scene_images(
            "re_t", 0, BackgroundTasks(), api.ReRegenImagesBody(prompt="NEW")))
    assert e.value.status_code == 409


# ------------------------------------------------- runner fan-out helper

def test_regen_scene_images_with_prompt_shares_semaphore(wired, monkeypatch):
    from character_swap import runner_reengineer
    job = _job(movement=True)
    wired["job"] = job
    seen = []

    async def fake_retry(job_id, cid, vid, prompt=None, *,
                         qc_intent=None, sem=None):
        seen.append((job_id, cid, vid, prompt, qc_intent, sem))
    monkeypatch.setattr(runner_reengineer.runner,
                        "retry_single_variant", fake_retry)
    asyncio.run(runner_reengineer.regen_scene_images_with_prompt(
        "j1", "NEW", {"c0": "c0-v1", "c1": "c1-v1"}, "byt mugg"))
    assert sorted((c, v, p, i) for _, c, v, p, i, _ in seen) == [
        ("c0", "c0-v1", "NEW", "byt mugg"), ("c1", "c1-v1", "NEW", "byt mugg")]
    sems = {s for *_, s in seen}
    assert len(sems) == 1 and None not in sems       # ONE shared semaphore


# ------------------------------------------ review 2026-06-13 regression fixes

def test_regen_images_synthesizes_plan_when_none(wired):
    """Director-off runs (the default) have no cached plan — the rewritten
    prompt must still persist for future whole-scene regens."""
    job = _job(movement=True)
    assert job.director_prompts_json is None
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    asyncio.run(api.reengineer_regen_scene_images(
        "re_t", 0, BackgroundTasks(), api.ReRegenImagesBody(prompt="NEW")))
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        job.director_prompts_json)
    assert plan.prompt_version == prompt_director.prompt_fingerprint()
    assert plan.lookup("c0", "s1") == ["NEW"]
    assert plan.lookup("c0", "s2") == []             # other scenes untouched
    assert job.use_director is False                 # flag NOT flipped


def test_regen_images_skips_generating_approved_slot(wired):
    """Concurrent submits must not double-run a slot already in flight."""
    job = _job(movement=True)
    for jc in job.characters.values():
        for v in jc.images:
            if v.scene_id == "s1":
                v.status = VariantStatus.GENERATING
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_regen_scene_images(
            "re_t", 0, BackgroundTasks(), api.ReRegenImagesBody(prompt="NEW")))
    assert e.value.status_code == 409


def test_engine_effective_prompt_substitutes_stock_for_gpt2(wired):
    """Slots store GENERATION_PROMPT verbatim, but gpt2-id-swap actually
    generated with the compact identity-first prompt — the rewrite must
    operate on THAT text (standard Image1=scene orientation)."""
    from character_swap import pipeline
    job = _job(movement=True)
    job.image_model = "gpt2-id-swap"
    out = api._engine_effective_swap_prompt(job, pipeline.GENERATION_PROMPT)
    assert "Image 1 is the fixed master scene" in out     # flipped to standard
    assert "Do NOT zoom out" in out                       # framing lock kept
    assert "recognizable likeness" in out                 # identity sentence
    # Non-stock prompts pass through untouched.
    assert api._engine_effective_swap_prompt(job, "custom text") == "custom text"


def test_rewrite_endpoint_uses_engine_effective_current(wired, monkeypatch):
    """Default config: slot prompt = GENERATION_PROMPT → the Director must
    see the engine-effective compact prompt, not the stored stock string."""
    from character_swap import pipeline
    job = _job(movement=True)
    job.image_model = "gpt2-id-swap"
    for jc in job.characters.values():
        for v in jc.images:
            v.prompt = pipeline.GENERATION_PROMPT
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    _register_frame(wired)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    seen = {}

    def fake_rewrite(**kw):
        seen.update(kw)
        return "REWRITTEN"
    monkeypatch.setattr(prompt_director, "direct_scene_prompt_rewrite",
                        fake_rewrite)
    asyncio.run(api.reengineer_rewrite_scene_prompt(
        "re_t", 0, api.ReRewritePromptBody(change="byt mugg")))
    assert "Do NOT zoom out" in seen["current_prompt"]
    assert seen["current_prompt"] != pipeline.GENERATION_PROMPT


def test_rewrite_endpoint_rebases_on_supplied_current_prompt(wired, monkeypatch):
    """A second Director pass sends the modal's textarea content so the
    previous rewrite / hand edits aren't lost."""
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    _register_frame(wired)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    seen = {}

    def fake_rewrite(**kw):
        seen.update(kw)
        return "REWRITTEN2"
    monkeypatch.setattr(prompt_director, "direct_scene_prompt_rewrite",
                        fake_rewrite)
    asyncio.run(api.reengineer_rewrite_scene_prompt(
        "re_t", 0, api.ReRewritePromptBody(
            change="och ta bort hatten",
            current_prompt="first rewrite with a glass")))
    assert seen["current_prompt"] == "first rewrite with a glass"


def test_rewrite_endpoint_passes_background(wired, monkeypatch, tmp_path):
    """Background-replacement runs: the Director must SEE Image 3."""
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"bg")
    job = _job(movement=True)
    job.extra_reference_path = str(bg)
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    _register_frame(wired)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    seen = {}

    def fake_rewrite(**kw):
        seen.update(kw)
        return "R"
    monkeypatch.setattr(prompt_director, "direct_scene_prompt_rewrite",
                        fake_rewrite)
    asyncio.run(api.reengineer_rewrite_scene_prompt(
        "re_t", 0, api.ReRewritePromptBody(change="x")))
    assert seen["background_path"] == bg


def test_get_swap_prompt_endpoint(wired):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    out = asyncio.run(api.reengineer_scene_swap_prompt("re_t", 0))
    assert out["prompt"] == "cur s1"
    assert out["scene_id"] == "s1"


def test_get_swap_prompt_variant_narrowing_and_substitution(wired):
    """?variant_id targets one slot; stock slot prompts come back
    engine-effective (the per-image ✎↻ prefill path)."""
    from character_swap import pipeline
    job = _job(movement=True)
    job.image_model = "gpt2-id-swap"
    job.characters["c1"].images[0].prompt = "c1's own edited prompt"
    job.characters["c0"].images[0].prompt = pipeline.GENERATION_PROMPT
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    out = asyncio.run(api.reengineer_scene_swap_prompt(
        "re_t", 0, variant_id="c1-v1"))
    assert out["prompt"] == "c1's own edited prompt"
    out = asyncio.run(api.reengineer_scene_swap_prompt(
        "re_t", 0, variant_id="c0-v1"))
    assert "Do NOT zoom out" in out["prompt"]        # stock → engine-effective


def test_direct_rewrite_attaches_background_and_forbidden_rule(monkeypatch,
                                                               tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"y")
    captured = {}

    def fake_messages(**kw):
        captured.update(kw)
        return object()
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "messages_with_tools", fake_messages)
    monkeypatch.setattr(prompt_director.anthropic_client, "extract_tool_call",
                        lambda resp, name: {"prompt": "P"})
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "_file_to_image_block",
                        lambda p: {"type": "text", "text": f"IMG:{p}"})
    out = prompt_director.direct_scene_prompt_rewrite(
        scene_id="s1", frame_path=frame, current_prompt="p",
        change_request="x", background_path=bg)
    assert out is not None
    texts = " ".join(b.get("text", "")
                     for b in captured["messages"][0]["content"])
    assert f"IMG:{bg}" in texts
    assert "REPLACEMENT BACKGROUND" in texts
    assert "STRICTLY FORBIDDEN" in captured["system"]
    # No background → the no-bg rule, no leftover placeholder.
    prompt_director.direct_scene_prompt_rewrite(
        scene_id="s1", frame_path=frame, current_prompt="p",
        change_request="x")
    assert "{bg_rule}" not in captured["system"]
    assert "scene's own background is preserved" in captured["system"]


def test_retry_single_variant_sets_qc_intent_and_unshares_path(monkeypatch):
    """(a) A prompt override becomes the slot's QC intent (change text wins);
    (b) a slot sharing its file with a clone is re-pointed to its own file
    before regenerating, so the sibling scene's image survives."""
    from character_swap import runner
    job = _job(movement=True, n_chars=1)
    jc = job.characters["c0"]
    # Clone c0-v1's file onto a second variant (duplicate-scene shape).
    clone = GeneratedImage(variant_id="c0-dup", path="/1.png", prompt="p",
                           scene_id="s1__dup", status=VariantStatus.READY)
    jc.images.append(clone)

    class _S:
        def get_job(self, jid):
            return job if jid == "j1" else None

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner, "store", lambda: _S())
    gen_calls = []

    async def fake_gen(j, c, v, sem):
        gen_calls.append(v)
    monkeypatch.setattr(runner, "_generate_one_variant", fake_gen)

    asyncio.run(runner.retry_single_variant(
        "j1", "c0", "c0-v1", "new prompt", qc_intent="byt mugg"))
    target = next(v for v in jc.images if v.variant_id == "c0-v1")
    assert target.qc_intent == "byt mugg"
    assert target.prompt == "new prompt"
    assert target.path != "/1.png"                   # re-pointed off the share
    assert target.path.endswith("variant_c0-v1.png")
    assert clone.path == "/1.png"                    # sibling untouched
    assert gen_calls and gen_calls[0].variant_id == "c0-v1"


def test_qc_call_site_prefers_slot_intent():
    import inspect
    from character_swap import runner
    src = inspect.getsource(runner)
    assert "user_intent=(variant.qc_intent or job.prompt)" in src


def test_collect_clips_reports_unapproved_scene_as_missing(tmp_path):
    """Withdrawn approvals (scene-level image regen) must surface as a LOUD
    coverage gap instead of silently shipping a final without the scene."""
    from character_swap import runner_reengineer
    from character_swap.models import VideoStatus, VideoVariant
    job = _job(movement=True, n_chars=1)
    jc = job.characters["c0"]
    jc.approved_variant_ids = ["c0-v2"]              # s1 approval withdrawn
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"v")
    jc.videos = [VideoVariant(video_id="vid2", grok_job_id="g1",
                              source_variant_id="c0-v2",
                              status=VideoStatus.DONE,
                              final_video_path=str(clip))]
    state = _state("done")
    clips, _dialogues, missing, waitable = runner_reengineer._collect_clips(
        state, jc)
    assert len(clips) == 1                            # s2's clip collected
    assert any("scen 1" in m for m in missing)
    assert waitable is False                          # needs manual approve
    # A scene the character has NO slots for stays silently skipped.
    jc.images = [v for v in jc.images if v.scene_id != "s1"]
    clips, _dialogues, missing, _ = runner_reengineer._collect_clips(state, jc)
    assert not any("scen 1" in m for m in missing)


def test_do_reanimate_keeps_dirty_when_all_pairs_skipped(wired, monkeypatch):
    """'Animera om ändrade' during an image regen (approvals withdrawn) must
    NOT consume the dirty flag — nothing was actually re-animated."""
    from character_swap import runner_reengineer
    job = _job(movement=True)
    for jc in job.characters.values():
        jc.approved_variant_ids = []                 # regen withdrew them all
    wired["job"] = job
    st = _state("awaiting_assembly")
    st["scenes"][0]["dirty"] = True
    wired["states"]["re_t"] = st
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", set())
    asyncio.run(runner_reengineer._do_reanimate(
        "re_t", [0], char_id=None, clear_dirty=True))
    saved = wired["states"]["re_t"]
    assert saved["scenes"][0].get("dirty") is True   # preserved
    assert saved["status"] == "awaiting_assembly"


def test_do_reanimate_clears_dirty_only_for_acted_scenes(wired, monkeypatch):
    from character_swap import runner_reengineer
    job = _job(movement=True)
    # s1 approved (will act); s2 approvals withdrawn (will skip).
    for jc in job.characters.values():
        jc.approved_variant_ids = [f"{jc.char_id}-v1"]
    wired["job"] = job
    st = _state("done")
    st["scenes"][0]["dirty"] = True
    st["scenes"][1]["dirty"] = True
    wired["states"]["re_t"] = st
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", set())
    calls = []

    async def fake_more(job_id, cid, n, *, source_variant_id=None):
        calls.append((cid, source_variant_id))
    monkeypatch.setattr(runner_reengineer.runner,
                        "generate_more_videos", fake_more)
    asyncio.run(runner_reengineer._do_reanimate(
        "re_t", [0, 1], char_id=None, clear_dirty=True))
    saved = wired["states"]["re_t"]
    assert "dirty" not in saved["scenes"][0]         # acted → cleared
    assert saved["scenes"][1].get("dirty") is True   # skipped → preserved
    assert {c for c, _ in calls} == {"c0", "c1"}
