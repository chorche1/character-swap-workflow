"""Reengineer assemble v2 (Hugo 2026-06-12): finals are built like Swap
Step 6 — full-length clips concatenated and finished through the shared
Editor pipeline — and the prompt the user sees at the gate is EXACTLY what
Kling receives (no movement-Director/enrich rewrite, no hidden layers).

Hermetic: store + run-state I/O stubbed, run_editor_pipeline recorded (its
real ffmpeg path is covered by test_leading_silence_trim.py).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import BackgroundTasks

from character_swap import api, reengineer as reengineer_mod, runner, runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)
from character_swap.runner_compile import EditorResult

_APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


# --------------------------------------------------------------- builders

def _job(*, origin: str | None = "reengineer:re_t", use_director: bool = False,
         clip: str = "/clip.mp4") -> Job:
    v = GeneratedImage(variant_id="va", path="/a.png", prompt="p",
                       scene_id="s1", status="ready")
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/c.png",
        status=CharStatus.APPROVED, images=[v],
        approved_variant_ids=["va"],
        videos=[VideoVariant(video_id="vidA", grok_job_id="g1",
                             status=VideoStatus.DONE, source_variant_id="va",
                             final_video_path=clip)])
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters={"cA": jc}, origin=origin, use_director=use_director)


def _state(status: str = "animating") -> dict:
    return {"re_id": "re_t", "job_id": "j1", "status": status,
            "scenes": [{"idx": 0, "scene_id": "s1", "start": 0.0, "end": 2.0,
                        "duration": 2.0, "motion_prompt": "He pours the oil.",
                        "speech": "", "summary": "s"}]}


def _wire_assemble(monkeypatch, tmp_path, job, *, character=None):
    """Fake store + run dir + recorded run_editor_pipeline → (updates, calls)."""
    run_dir = tmp_path / "re_run"
    run_dir.mkdir(exist_ok=True)

    class _S:
        def get_job(self, jid):
            return job if jid == "j1" else None

        def get_character(self, cid):
            return character
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.runner_compile, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(type(runner_reengineer.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)

    calls: list[dict] = []

    async def fake_pipeline(paths, **kw):
        calls.append({"paths": [str(p) for p in paths], **kw})
        out = kw["edit_dir"] / "04-final.mp4"
        out.write_bytes(b"mp4")
        return EditorResult(final=out, voice_applied=False)
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "run_editor_pipeline", fake_pipeline)

    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))
    return updates, calls


# ------------------------------------------------- assemble → Editor pipeline

def test_assemble_passes_full_clips_with_kling_defaults(tmp_path, monkeypatch):
    """No assemble_settings stored → Step-6-like defaults EXCEPT voice swap
    + WPM normalize off (Kling's own voice and pacing survive), and the raw
    clip path goes in untouched — no duration cap anywhere."""
    clip = tmp_path / "kling.mp4"
    clip.write_bytes(b"x")
    job = _job(clip=str(clip))
    updates, calls = _wire_assemble(monkeypatch, tmp_path, job)

    asyncio.run(runner_reengineer._do_assemble("re_t", _state()))

    assert len(calls) == 1
    kw = calls[0]
    assert kw["paths"] == [str(clip)]                  # full clip, uncut
    # Hugo 2026-06-12: capcut-bluebox @ size 68 is the Reengineer standard.
    assert kw["template"] == "capcut-bluebox"
    assert kw["overrides"] == {"size": 68}
    assert kw["enable_trim"] is True
    assert kw["enable_captions"] is True
    assert kw["enable_wpm_normalize"] is False         # Kling pacing kept
    assert kw["voice_id"] is None                      # Kling voice kept
    assert kw["playback_speed"] == 1.0                 # global speed off
    # Hugo 2026-06-13: the Editor-tab trim values he actually runs with.
    assert kw["threshold_db"] == -30.0
    assert kw["min_silence_secs"] == 0.20
    assert kw["pad_secs"] == 0.02
    assert updates["status"] == "done"
    f = updates["finals"]["cA"]
    assert f["edit_id"].startswith("ed_")              # Editor-tab re-renderable
    assert Path(f["final_path"]).read_bytes() == b"mp4"


def test_assemble_respects_stored_settings(tmp_path, monkeypatch):
    """state['assemble_settings'] (the ⚙ panel, persisted at animate time)
    overrides the defaults; unknown keys are ignored."""
    clip = tmp_path / "kling.mp4"
    clip.write_bytes(b"x")
    job = _job(clip=str(clip))
    updates, calls = _wire_assemble(monkeypatch, tmp_path, job)
    st = _state()
    st["assemble_settings"] = {"template": "submagic-pro",
                               "enable_captions": False,
                               "enable_wpm_normalize": True,
                               "target_wpm": 160.0,
                               "playback_speed": 1.25,
                               "junk_key": "ignored"}

    asyncio.run(runner_reengineer._do_assemble("re_t", st))

    kw = calls[0]
    assert kw["template"] == "submagic-pro"
    assert kw["enable_captions"] is False
    assert kw["enable_wpm_normalize"] is True
    assert kw["target_wpm"] == 160.0
    assert kw["playback_speed"] == 1.25
    assert "junk_key" not in kw


def test_assemble_voice_swap_uses_character_preset(tmp_path, monkeypatch):
    """enable_voice_swap=True without an override → the character's library
    preset voice is resolved, exactly like Step 6."""
    clip = tmp_path / "kling.mp4"
    clip.write_bytes(b"x")
    job = _job(clip=str(clip))

    class _Char:
        voice_id = "v-preset"
    updates, calls = _wire_assemble(monkeypatch, tmp_path, job,
                                    character=_Char())
    st = _state()
    st["assemble_settings"] = {"enable_voice_swap": True}

    asyncio.run(runner_reengineer._do_assemble("re_t", st))
    assert calls[0]["voice_id"] == "v-preset"


# ------------------------------------------------- endpoint settings plumbing

def _wire_api(monkeypatch, status: str = "awaiting_approval") -> dict:
    box = {"state": _state(status), "saved": None}

    def load_state(re_id):
        return dict(box["state"]) if re_id == "re_t" else None

    def save_state(s):
        box["state"] = dict(s)
        box["saved"] = dict(s)
    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    return box


def test_editor_pipeline_applies_playback_speed(tmp_path, monkeypatch):
    """run_editor_pipeline's global-speed step (Hugo 2026-06-13): the same
    Speed control as the Editor tab — pitch-preserving time-stretch with
    word timestamps scaled in lockstep so captions stay in sync."""
    from character_swap import runner_compile
    calls: dict = {}

    def fake_assemble(paths, out, **kw):
        Path(out).write_bytes(b"v")
    monkeypatch.setattr(runner_compile.video_edit, "assemble_clips",
                        fake_assemble)
    monkeypatch.setattr(runner_compile.video_edit, "transcribe_words",
                        lambda *a, **k: ["w1", "w2"])

    def fake_stretch(src, out, *, speed_factor, job_id=None):
        calls["speed"] = speed_factor
        Path(out).write_bytes(b"s")
    monkeypatch.setattr(runner_compile.video_edit, "time_stretch",
                        fake_stretch)

    def fake_scale(words, s):
        calls["scaled"] = s
        return words
    monkeypatch.setattr(runner_compile.video_edit, "scale_word_timestamps",
                        fake_scale)
    monkeypatch.setattr(runner_compile.video_edit, "words_to_json",
                        lambda w: "[]")

    def _run_pipeline(speed):
        return asyncio.run(runner_compile.run_editor_pipeline(
            [tmp_path / "a.mp4"], edit_id="ed_t", edit_dir=tmp_path,
            template="minimal", overrides=None, enable_trim=True,
            enable_captions=False, enable_wpm_normalize=False,
            target_wpm=190.0, threshold_db=-30.0, min_silence_secs=0.3,
            pad_secs=0.03, voice_id=None, playback_speed=speed))

    res = _run_pipeline(1.25)
    assert calls["speed"] == 1.25
    assert calls["scaled"] == 1.25                  # captions stay in sync
    assert res.final.name == "035-speed.mp4"

    calls.clear()
    res = _run_pipeline(1.0)
    assert "speed" not in calls                     # 1.0 = off, no encode
    assert res.final.name != "035-speed.mp4"


def test_animate_persists_panel_settings(monkeypatch):
    box = _wire_api(monkeypatch)
    bg = BackgroundTasks()
    body = api.ReAssembleSettingsBody(template="submagic-pro",
                                      enable_wpm_normalize=True,
                                      voice_override="  v9  ")
    out = asyncio.run(api.reengineer_animate("re_t", bg, body))
    assert out["ok"] is True
    assert len(bg.tasks) == 1
    cfg = box["saved"]["assemble_settings"]
    assert cfg["template"] == "submagic-pro"
    assert cfg["enable_wpm_normalize"] is True
    assert cfg["voice_override"] == "v9"               # trimmed
    # None fields are NOT written — stored values / runner defaults apply.
    assert "enable_captions" not in cfg


def test_animate_without_body_keeps_state_untouched(monkeypatch):
    box = _wire_api(monkeypatch)
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_animate("re_t", bg, None))
    assert out["ok"] is True
    assert box["saved"] is None                        # nothing persisted


def test_assemble_endpoint_persists_and_clears_override(monkeypatch):
    box = _wire_api(monkeypatch, status="done")
    box["state"]["assemble_settings"] = {"voice_override": "old",
                                         "enable_voice_swap": True}
    bg = BackgroundTasks()
    body = api.ReAssembleSettingsBody(voice_override="",
                                      enable_voice_swap=False)
    asyncio.run(api.reengineer_assemble("re_t", bg, body))
    cfg = box["saved"]["assemble_settings"]
    assert cfg["voice_override"] is None               # "" clears it
    assert cfg["enable_voice_swap"] is False
    assert len(bg.tasks) == 1


# ------------------------------------------------- exact-prompt guarantees

def _wire_synthesis(monkeypatch, job):
    class _S:
        def get_job(self, jid):
            return job

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner, "store", lambda: _S())

    director_calls: list = []
    from character_swap import prompt_director

    def fake_direct_movement(*a, **kw):
        director_calls.append(kw)
        return None
    monkeypatch.setattr(prompt_director, "direct_movement", fake_direct_movement)

    animated: list = []

    async def fake_animate(job_, jc, m, prompt_for_scene):
        animated.append(prompt_for_scene("s1"))
    monkeypatch.setattr(runner, "_animate_character", fake_animate)
    return director_calls, animated


def test_movement_director_skipped_for_reengineer_jobs(monkeypatch):
    """REGRESSION (Hugo 2026-06-12 'I want to see the prompt exactly'):
    a reengineer job with use_director=True (the flag belongs to the swap-
    IMAGE phase) must NOT get a movement-Director rewrite — the clip uses
    movement_prompts[sid] verbatim."""
    job = _job(use_director=True)
    job.movement_prompts = {"s1": "He pours the oil. EXACT."}
    job.movement_prompt = "He pours the oil. EXACT."
    director_calls, animated = _wire_synthesis(monkeypatch, job)

    asyncio.run(runner.run_video_synthesis("j1"))

    assert director_calls == []                        # Director never ran
    assert animated == ["He pours the oil. EXACT."]    # verbatim prompt


def test_movement_director_still_runs_for_plain_swap_jobs(monkeypatch):
    """Control: the same flags on a NON-reengineer job keep today's
    behavior (Director consulted; returning None falls back gracefully)."""
    job = _job(origin=None, use_director=True)
    job.movement_prompts = {"s1": "He pours the oil."}
    job.movement_prompt = "He pours the oil."
    director_calls, animated = _wire_synthesis(monkeypatch, job)

    asyncio.run(runner.run_video_synthesis("j1"))

    assert len(director_calls) == 1
    assert animated == ["He pours the oil."]


def test_do_animate_wipes_enriched_layer(monkeypatch):
    """_do_animate must clear enriched_movement_prompts — stale Director
    output from before the skip-fix would outrank the visible prompt."""
    job = _job()
    job.enriched_movement_prompts = {"s1": "DIRECTOR REWRITE"}
    job.enriched_movement_prompt = "DIRECTOR REWRITE"

    class _S:
        def get_job(self, jid):
            return job

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer, "_update", lambda re_id, **kw: None)

    async def noop(*a, **kw):
        return None
    monkeypatch.setattr(runner_reengineer, "_watch_video_phase", noop)
    monkeypatch.setattr(runner_reengineer.runner, "run_video_synthesis", noop)

    asyncio.run(runner_reengineer._do_animate("re_t", _state()))

    assert job.enriched_movement_prompts == {}
    assert job.enriched_movement_prompt is None
    assert job.movement_prompts["s1"].startswith("He pours the oil.")
    assert "American" in job.movement_prompts["s1"]


def test_sync_movement_pops_enriched_for_edited_scene():
    """Editing a scene post-gate syncs the new text AND drops any enriched
    shadow for that scene — redo clips must use the text the user sees."""
    job = _job()
    job.movement_prompts = {"s1": "old"}
    job.enriched_movement_prompts = {"s1": "DIRECTOR REWRITE", "s2": "keep"}

    import unittest.mock as mock
    with mock.patch.object(runner_reengineer, "store") as m:
        m.return_value.update_job = lambda j: None
        runner_reengineer._sync_movement_from_state(job, _state(), [0])

    assert "s1" not in job.enriched_movement_prompts
    assert job.enriched_movement_prompts == {"s2": "keep"}
    assert job.movement_prompts["s1"].startswith("He pours the oil.")


def test_assemble_surfaces_pipeline_warnings(tmp_path, monkeypatch):
    """A caption-render failure inside the Editor pipeline must be LOUD:
    logged + recorded on finals[cid]['warning'] (shipped to the UI), never
    silently swallowed (review finding 2026-06-12)."""
    clip = tmp_path / "kling.mp4"
    clip.write_bytes(b"x")
    job = _job(clip=str(clip))
    run_dir = tmp_path / "re_run"
    run_dir.mkdir(exist_ok=True)

    class _S:
        def get_job(self, jid):
            return job

        def get_character(self, cid):
            return None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.runner_compile, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(type(runner_reengineer.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)

    async def failing_pipeline(paths, **kw):
        assert kw.get("warn") is not None      # callback must be wired
        await kw["warn"]("caption render failed: boom")
        out = kw["edit_dir"] / "01-trimmed.mp4"
        out.write_bytes(b"mp4")
        return EditorResult(final=out, voice_applied=False)
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "run_editor_pipeline", failing_pipeline)
    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))

    asyncio.run(runner_reengineer._do_assemble("re_t", _state()))

    f = updates["finals"]["cA"]
    assert f["status"] == "done"               # final still ships…
    assert "caption render failed" in f["warning"]   # …but the gap is visible


def test_assemble_duplicate_guard(monkeypatch):
    """A second assemble for the same run while one is in flight is a no-op
    (assembly now bills Whisper/Remotion — overlap double-bills and races
    the same final_<cid>.mp4 paths)."""
    calls: list[str] = []

    async def fake_do(re_id, state):
        calls.append(re_id)
    monkeypatch.setattr(runner_reengineer, "_do_assemble", fake_do)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: {"re_id": rid, "job_id": "j1"})

    runner_reengineer._ASSEMBLING.add("re_t")
    try:
        asyncio.run(runner_reengineer.assemble("re_t"))
        assert calls == []                     # duplicate skipped
    finally:
        runner_reengineer._ASSEMBLING.discard("re_t")
    asyncio.run(runner_reengineer.assemble("re_t"))
    assert calls == ["re_t"]                   # normal path still runs


def test_assemble_endpoint_409_while_assembling(monkeypatch):
    """POST /assemble while status='assembling' is refused — the watcher or a
    prior click already owns the build."""
    from fastapi import HTTPException
    _wire_api(monkeypatch, status="assembling")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_assemble("re_t", BackgroundTasks(), None))
    assert e.value.status_code == 409


def test_reasm_body_clamps_target_wpm():
    """The client clamps target_wpm to the server's ge=80/le=400 — a typed
    out-of-range value must never 422-block ▶ Generate videos."""
    js = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"_reAsmBody\(\)\s*{(.*?)\n    },", js, re.S)
    assert m, "_reAsmBody not found in app.js"
    assert "Math.min(400, Math.max(80," in m.group(1)
    # Global speed (2026-06-13): clamped to the server's ge=0.5/le=2.0.
    assert "playback_speed" in m.group(1)
    assert "Math.min(2, Math.max(0.5," in m.group(1)


# ------------------------------------------------- dialogue-fitted durations

def test_kling_duration_is_scene_length_ceil_plus_one():
    """Hugo 2026-06-13 (supersedes the 06-12 plain ceil): AUTO Kling length
    = the ORIGINAL scene clip's length rounded UP to the SECOND-next whole
    second (ceil + 1) — "6,4 s original → 8 s Kling". Dialogue never
    extends it (the gate shows a ⚠ hint and the manual field owns that
    tradeoff)."""
    # Hugo's literal example.
    assert runner_reengineer._kling_duration(
        {"duration": 6.4, "motion_prompt": "He talks.", "speech": ""}) == 8

    short_scene_long_line = {
        "duration": 1.4,
        "motion_prompt": 'He smiles. The person says: "Put baking soda on '
                         'kiwis and just watch what happens because half '
                         'their customers would disappear overnight"',
        "speech": "",
    }
    # ceil(1.4) + 1 = 3; the 17-word line does NOT extend it further (the
    # ⚠ hint covers that).
    assert runner_reengineer._kling_duration(short_scene_long_line) == 3

    no_dialogue = {"duration": 7.567, "motion_prompt": "He pours.", "speech": ""}
    assert runner_reengineer._kling_duration(no_dialogue) == 9   # ceil + 1

    speech_fallback = {"duration": 2.0, "motion_prompt": "He nods.",
                       "speech": "this little thing beats your multivitamin"}
    assert runner_reengineer._kling_duration(speech_fallback) == 3   # 2 + 1

    assert runner_reengineer._kling_duration(
        {"duration": 14.2, "motion_prompt": 'Says: "hi"', "speech": ""}) == 15

    # The MANUAL override is used exactly as typed (just clamped) — no +1.
    assert runner_reengineer._kling_duration(
        {"duration": 6.4, "kling_secs": 7, "motion_prompt": "x",
         "speech": ""}) == 7


def test_do_animate_uses_scene_length_durations(monkeypatch):
    """_do_animate's durations_by_scene must come from _kling_duration —
    the original scene length, rounded up."""
    job = _job()

    class _S:
        def get_job(self, jid):
            return job

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer, "_update", lambda re_id, **kw: None)

    async def noop(*a, **kw):
        return None
    monkeypatch.setattr(runner_reengineer, "_watch_video_phase", noop)
    monkeypatch.setattr(runner_reengineer.runner, "run_video_synthesis", noop)

    st = _state()
    st["scenes"][0]["duration"] = 6.2
    st["scenes"][0]["motion_prompt"] = (
        'The person says: "one two three four five six seven eight nine ten '
        'eleven"')
    asyncio.run(runner_reengineer._do_animate("re_t", st))
    # Scene length 6.2 → ceil + 1 = 8 (dialogue does not extend — Hugo's
    # directive; the manual kling_secs override is tested separately).
    assert job.durations_by_scene["s1"] == 8


def test_template_selects_resync_after_async_options():
    """The caption-template <select>s render their options from the async
    /api/editor/templates fetch — without an x-effect re-sync the browser
    DISPLAYS the first option (e.g. Mrbeast) while the model correctly holds
    the persisted default (Hugo 2026-06-13: the ⚙ Slutvideo header said
    capcut-bluebox but the dropdown showed Mrbeast)."""
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html"
            ).read_text(encoding="utf-8")
    for model in ("reAsmSettings.template", "compileSettings.template"):
        i = html.find(f'x-model="{model}"')
        assert i != -1, f"select for {model} not found"
        chunk = html[i:i + 400]
        assert f"$el.value = {model}" in chunk, \
            f"{model} select lost its x-effect option re-sync"


def test_kling_duration_js_mirror_in_sync():
    """app.js klingDuration must use the same constants + dialogue regex as
    the Python source of truth. Edit both together."""
    js = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"klingDuration\(run, sc\)\s*{(.*?)\n    },", js, re.S)
    assert m, "klingDuration not found in app.js"
    body = m.group(1)
    assert "kling_secs" in body                  # manual override honored
    assert "Math.ceil" in body and "Math.max(3, Math.min(15" in body
    assert "+ 1)" in body                        # AUTO = ceil + 1 (2026-06-13)
    # The speech estimate lives in klingSpeechSecs (HINT only since the
    # evening directive) — constants + the #7 attribution regex pinned there.
    m2 = re.search(r"klingSpeechSecs\(run, sc\)\s*{(.*?)\n    },", js, re.S)
    assert m2, "klingSpeechSecs not found in app.js"
    hint = m2.group(1)
    wps = str(runner_reengineer._SPEECH_WORDS_PER_SEC)
    margin = str(runner_reengineer._SPEECH_MARGIN_SECS)
    assert f"/ {wps} + {margin}" in hint, "speech pace constants drifted"
    assert 'says[^"“”]{0,160}?' in hint, "JS dialogue regex drifted"
    assert 'says[^"“”]{0,160}?' in runner_reengineer._DIALOGUE_RE.pattern


def test_speech_secs_parses_descriptor_attribution():
    """Backlog #7 (audit 2026-06-12): the analyst's documented dialogue
    idiom is 'The person says, in a casual conversational tone with a
    natural American accent: "..."'. The old regex required the quote right
    after `says` and never matched it — an edited line at the gate fell back
    to the STALE analyst speech field, so the clip kept the old shorter
    duration and Kling chopped the new line."""
    entry = {
        "motion_prompt": ('The person says, in a casual conversational tone '
                          'with a natural American accent: "one two three '
                          'four five six seven eight nine ten eleven"'),
        "speech": "old short line",      # stale — must NOT win
        "duration": 1.2,
    }
    # 11 words / 2.2 + 1.0 = 6.0 — parsed from the EDITED says-clause, not
    # the stale 3-word speech field. (Hint-only since the evening directive;
    # _kling_duration itself is plain ceil(duration).)
    assert runner_reengineer._speech_secs(entry) == pytest.approx(6.0)
    assert runner_reengineer._kling_duration(entry) == 3   # ceil(1.2)→floor


def test_speech_secs_still_parses_bare_says():
    entry = {"motion_prompt": 'He says: "one two three four five six seven '
                              'eight nine ten eleven"',
             "speech": "", "duration": 1.0}
    assert runner_reengineer._speech_secs(entry) == pytest.approx(6.0)


def test_kling_suffix_js_mirrors_with_accent():
    """The gate UI's klingSuffix(text) must stay byte-identical with
    _with_accent's clauses — it is the 'exact prompt' promise. Edit both
    together or this test fails."""
    js = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"klingSuffix\(text\)\s*{(.*?)\n    },", js, re.S)
    assert m, "klingSuffix not found in app.js"
    body = m.group(0)
    clauses = re.findall(r"const clause = '([^']+)';", body)
    assert len(clauses) == 3

    accent, pronounce, music = clauses
    # Byte-identical with the Python source of truth:
    assert runner_reengineer._with_accent("x") == "x" + accent + pronounce + music
    assert runner_reengineer._with_accent("") == accent + pronounce + music
    # Same guards (case-insensitive substring checks) on both sides.
    assert "american" in body and "pronounc" in body and "music" in body
    p = runner_reengineer._with_accent(
        "An American narrator pronounces this. No music.")
    assert p == "An American narrator pronounces this. No music."
    # The new analyst attribution covers accent+pronunciation… the central
    # layer still adds ONLY the music guarantee.
    attributed = ('The person says, in a casual conversational tone with a '
                  'natural American accent: "hi there folks, pronounced well"')
    assert runner_reengineer._with_accent(attributed) == attributed + music


def test_do_animate_uses_freshest_scenes_not_snapshot(monkeypatch, tmp_path):
    """Backlog #35 (2026-06-12): a prompt edit saved between the animate
    trigger and _do_animate's body was (a) ignored for the Kling prompts and
    (b) clobbered by the stale-snapshot write-back. _do_animate must re-read
    the on-disk state both at entry and before writing scenes back."""
    from character_swap import reengineer
    from character_swap.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)
    (tmp_path / "reengineer" / "re_t").mkdir(parents=True)

    job = _job()

    class _S:
        def get_job(self, jid):
            return job

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    async def noop(*a, **kw):
        return None
    monkeypatch.setattr(runner_reengineer, "_watch_video_phase", noop)
    monkeypatch.setattr(runner_reengineer.runner, "run_video_synthesis", noop)

    # On disk: the user's EDITED prompt (+ dirty flag from the edit).
    fresh = _state(status="awaiting_approval")
    fresh["scenes"][0]["motion_prompt"] = 'She says: "the edited line"'
    fresh["scenes"][0]["dirty"] = True
    reengineer.save_state(fresh)

    # The trigger captured a STALE snapshot before the edit landed.
    stale = _state(status="awaiting_approval")
    asyncio.run(runner_reengineer._do_animate("re_t", stale))

    # Kling gets the edited text (with the central suffix appended)...
    assert job.movement_prompts["s1"].startswith('She says: "the edited line"')
    # ...and the write-back kept the edited scenes (dirty cleared).
    on_disk = reengineer.load_state("re_t")
    assert on_disk["scenes"][0]["motion_prompt"] == 'She says: "the edited line"'
    assert "dirty" not in on_disk["scenes"][0]
    assert on_disk["status"] == "animating"


def test_assemble_caption_size_override_from_panel(tmp_path, monkeypatch):
    """The ⚙ panel's caption size rides as overrides.size and beats the
    68 default (Hugo 2026-06-12)."""
    clip = tmp_path / "kling.mp4"
    clip.write_bytes(b"x")
    job = _job(clip=str(clip))
    updates, calls = _wire_assemble(monkeypatch, tmp_path, job)
    st = _state()
    st["assemble_settings"] = {"overrides": {"size": 92}}

    asyncio.run(runner_reengineer._do_assemble("re_t", st))
    assert calls[0]["overrides"] == {"size": 92}
    assert calls[0]["template"] == "capcut-bluebox"    # default unaffected


def test_panel_ui_has_caption_size_control():
    """Backlog follow-up (Hugo 2026-06-12): bluebox default + selectable
    caption size 68 in the ⚙ Slutvideo panel."""
    js = _APP_JS.read_text(encoding="utf-8")
    assert "template: 'capcut-bluebox'" in js
    assert "captionSize: 68" in js
    body = js.split("_reAsmBody()")[1][:1200]
    assert "overrides: { size:" in body
    html = (Path(__file__).resolve().parents[1] / "web" /
            "index.html").read_text(encoding="utf-8")
    assert "reAsmSettings.captionSize" in html
    assert "Caption-storlek" in html


# --- manual Kling clip length (Hugo 2026-06-12) -------------------------------


def test_kling_duration_manual_override_wins():
    """A user-set kling_secs beats BOTH the scene length and the speech
    fit — clamped to Kling's [3, 15], never extended."""
    long_line = ('The person says: "one two three four five six seven eight '
                 'nine ten eleven twelve thirteen fourteen fifteen sixteen"')
    entry = {"duration": 8.0, "motion_prompt": long_line, "speech": ""}
    assert runner_reengineer._kling_duration(entry) == 9      # auto = ceil+1
    assert runner_reengineer._kling_duration(
        {**entry, "kling_secs": 5}) == 5                      # manual wins
    assert runner_reengineer._kling_duration(
        {**entry, "kling_secs": 20}) == 15                    # clamped
    assert runner_reengineer._kling_duration(
        {**entry, "kling_secs": 1}) == 3                      # clamped


def test_edit_scene_endpoint_sets_and_clears_kling_override(monkeypatch):
    box = _wire_api(monkeypatch)
    out = asyncio.run(api.reengineer_edit_scene(
        "re_t", 0, api.ReSceneEditBody(kling_secs=20)))
    assert box["saved"]["scenes"][0]["kling_secs"] == 15.0    # clamped
    # 0 clears the override back to auto.
    asyncio.run(api.reengineer_edit_scene(
        "re_t", 0, api.ReSceneEditBody(kling_secs=0)))
    assert "kling_secs" not in box["saved"]["scenes"][0]


def test_kling_override_ui_wired():
    js = _APP_JS.read_text(encoding="utf-8")
    body = js.split("klingDuration(run, sc)")[1][:900]
    assert "kling_secs" in body                    # JS mirror honors override
    html = (Path(__file__).resolve().parents[1] / "web" /
            "index.html").read_text(encoding="utf-8")
    assert "reSceneVal(r, sc, 'kling_secs')" in html
    assert "reengineerUploadOwnImage" in html      # own-image upload button
