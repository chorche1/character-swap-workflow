"""Spanish Reengineer mode (Hugo 2026-06-20).

A per-run `language` ("en" | "es") makes the GENERATED videos speak Spanish:
ONLY the quoted dialogue is translated to neutral Latin American Spanish (by
the analyst, or a fallback translation) + the accent clause flips — every other
prompt stays English. These tests lock each layer the language flows through.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from character_swap import api, reengineer, runner_reengineer
from character_swap.clients import anthropic_client
from character_swap.video_edit import Word


# --- _with_accent: the central clause that drives Kling's spoken language ----

def test_with_accent_spanish_clause():
    p = runner_reengineer._with_accent("He pours water into the glass.", "es")
    assert "Latin American Spanish accent" in p
    assert "American accent" not in p           # never the English clause
    assert "American English" not in p
    # The instruction clauses stay English (they are directions, not speech).
    assert "pronounced clearly" in p
    assert "No background music" in p
    # Idempotent — the analyst's Spanish attribution already carries "Spanish".
    assert runner_reengineer._with_accent(p, "es") == p


def test_with_accent_english_unchanged():
    p = runner_reengineer._with_accent("He pours water.", "en")
    assert "American accent" in p
    assert "Latin American Spanish" not in p
    assert runner_reengineer._with_accent(p, "en") == p


def test_with_accent_defaults_to_english():
    assert "American accent" in runner_reengineer._with_accent("x")


# --- analyst: Spanish system directive is appended only for es ---------------

def _wire_analyst(monkeypatch, capture):
    monkeypatch.setattr(anthropic_client, "messages_with_tools",
                        lambda **kw: capture.update(kw) or object())
    monkeypatch.setattr(
        anthropic_client, "extract_tool_call",
        lambda resp, name: {"scenes": [
            {"idx": 0, "motion_prompt": "He pours. He says in Spanish: \"hola\"",
             "speech": "hola", "summary": "x"}]})
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "image_stub"})


def _run_analyst(tmp_path, language):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    return reengineer.analyze_scenes(
        frames=[frame], spans=[(0.0, 2.0)],
        words=[Word("hello", 0.0, 0.5)], re_id="re_t",
        motion_frames=[[(frame, 0.0)]], language=language)


def test_analyst_spanish_directive_added_for_es(monkeypatch, tmp_path):
    cap: dict = {}
    _wire_analyst(monkeypatch, cap)
    _run_analyst(tmp_path, "es")
    assert "Latin American" in cap["system"]
    assert "SPANISH DIALOGUE" in cap["system"]


def test_analyst_no_spanish_directive_for_en(monkeypatch, tmp_path):
    cap: dict = {}
    _wire_analyst(monkeypatch, cap)
    _run_analyst(tmp_path, "en")
    assert "OUTPUT LANGUAGE OVERRIDE" not in cap["system"]
    assert cap["system"] == reengineer.REENGINEER_ANALYST_SYSTEM


# --- fallback translation ----------------------------------------------------

def test_spanishize_plans_translates_dialogue(monkeypatch):
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, re_id=None: ["añade bicarbonato"])
    plans = reengineer.fallback_plans(
        [(0.0, 2.0)], [Word("add", 0.1, 0.4), Word("soda", 0.4, 0.8)])
    out = reengineer.spanishize_plans(plans, re_id="re_t")
    assert out[0].speech == "añade bicarbonato"
    assert "añade bicarbonato" in out[0].motion_prompt
    assert "neutral Latin American Spanish" in out[0].motion_prompt
    assert "American accent" not in out[0].motion_prompt


def test_spanishize_plans_keeps_english_on_translate_failure(monkeypatch):
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, re_id=None: None)
    plans = reengineer.fallback_plans([(0.0, 2.0)], [Word("add", 0.1, 0.4)])
    out = reengineer.spanishize_plans(plans, re_id="re_t")
    assert out == plans                          # unchanged — never half-done


def test_speech_clause_spanish():
    c = runner_reengineer._speech_clause("hola mundo", "es")
    assert "neutral Latin American Spanish" in c
    assert "hola mundo" in c
    assert "American accent" not in c


# --- the es path is wired into _analyze's fallback branch --------------------

def test_analyze_runs_spanishize_on_es_fallback(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "scenes").mkdir(parents=True)
    monkeypatch.setattr(runner_reengineer.reengineer, "detect_scenes",
                        lambda src, threshold=None: [(0.0, 2.0)])
    monkeypatch.setattr(runner_reengineer.video_edit, "transcribe_words",
                        lambda src, job_id=None: [Word("add", 0.1, 0.5)])
    monkeypatch.setattr(runner_reengineer.reengineer, "extract_frame",
                        lambda src, t, dest: dest.write_bytes(b"x"))
    monkeypatch.setattr(runner_reengineer.reengineer, "analyze_scenes",
                        lambda **kw: None)                  # force fallback
    monkeypatch.setattr(runner_reengineer, "_register_frame_as_scene",
                        lambda f: (f"sc_{f.stem}", f))
    seen = {"called": False, "lang": None}

    def fake_spanishize(plans, re_id=None):
        seen["called"] = True
        for p in plans:
            p.motion_prompt = "ES:" + p.motion_prompt
        return plans
    monkeypatch.setattr(runner_reengineer.reengineer, "spanishize_plans",
                        fake_spanishize)

    state = {"re_id": "re_t", "scene_sensitivity": "high", "language": "es"}
    entries = asyncio.run(
        runner_reengineer._analyze("re_t", state, tmp_path / "src.mp4", run_dir))
    assert seen["called"] is True
    assert entries[0]["motion_prompt"].startswith("ES:")


# --- create endpoint: language is validated + persisted ----------------------

@pytest.fixture
def wired(monkeypatch, tmp_path):
    box = {"states": {}}

    class _S:
        def get_character(self, cid):
            return object() if cid == "cA" else None
    monkeypatch.setattr(api, "store", lambda: _S())
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda s: box["states"].update({s["re_id"]: dict(s)}))

    def _redir(rid):
        d = tmp_path / rid
        d.mkdir(parents=True, exist_ok=True)
        return d
    monkeypatch.setattr(reengineer_mod, "reengineer_dir", _redir)
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    monkeypatch.setattr(type(api.settings), "openai_api_key",
                        property(lambda self: "k"), raising=False)
    return box


def _create(language="en"):
    bg = BackgroundTasks()
    f = UploadFile(file=io.BytesIO(b"vid-bytes"), filename="clip.mp4")
    return asyncio.run(api.reengineer_create(
        bg, file=f, character_ids=json.dumps(["cA"]),
        image_model="gpt2-id-swap", video_model="kling-v3",
        auto_mode=False, outfit_mode="scene", outfit_text="",
        scene_sensitivity="high", language=language,
        use_director=False, background_file=None,
        background_source="character",
        character_source_image_ids=""))


def test_create_persists_language(wired):
    out = _create("es")
    assert wired["states"][out["re_id"]]["language"] == "es"


def test_create_defaults_english(wired):
    out = _create()
    assert wired["states"][out["re_id"]]["language"] == "en"


def test_create_rejects_unknown_language(wired):
    with pytest.raises(HTTPException) as e:
        _create("fr")
    assert e.value.status_code == 400


# --- JS mirror stays in sync with the Python clause --------------------------

def test_app_js_klingsuffix_mirrors_spanish_clause():
    app_js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
        encoding="utf-8")
    es_clause = runner_reengineer._ACCENT_CLAUSE["es"][0].strip()
    en_clause = runner_reengineer._ACCENT_CLAUSE["en"][0].strip()
    assert es_clause in app_js
    assert en_clause in app_js
    # The picker + submit wiring is present.
    assert "reengineerGen.language" in app_js
    assert "fd.append('language'" in app_js
