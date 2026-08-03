"""Speech-to-text runs on ElevenLabs Scribe, with whisper-1 as the fallback.

Hugo 2026-08-03, measured over 54 of his own clips (whisper-1 → Scribe):

    ordträff   en 1.000 → 1.000   es 0.925 → 0.962   de 0.498 → 0.605
    0-gap      en   97% →    2%   es   91% →    6%   de   91% →    5%

The word-match numbers are the smaller half of it. "0-gap" is the share of
adjacent word pairs with NO gap between them — whisper-1's per-word boundaries
are interpolated inside each segment, and every Remotion caption template
animates per word off exactly those numbers.

Locked here: the engine choice + its fallback, the language hint's two-pass
contract in QC, and the number/umlaut normalization that was quietly costing
both engines points.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import video_edit, video_qc
from character_swap.config import settings


# ------------------------------------------------------------------ engine ---

class _FakeScribe:
    def __init__(self, words=None, raises=None):
        self.words = words
        self.raises = raises
        self.calls = []

    def __call__(self, *, audio, model_id, language_code, app_job_id):
        self.calls.append({"model_id": model_id, "language_code": language_code})
        if self.raises:
            raise self.raises
        return list(self.words), "german"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """No ffmpeg, no network: a fake audio extract + both engines stubbed."""
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: True)
    audio = tmp_path / "a.wav"

    def _extract(_p):
        # Re-created per call, like the real one: each leg unlinks the wav in
        # its `finally`, so the whisper fallback must get a fresh extract.
        audio.write_bytes(b"wav")
        return audio
    monkeypatch.setattr(video_edit, "_extract_audio", _extract)

    whisper_calls = []

    def _whisper_leg(*a, **k):
        whisper_calls.append(k)
        raise AssertionError("whisper must not run unless Scribe declined")
    return {"audio": audio, "whisper_calls": whisper_calls,
            "whisper_leg": _whisper_leg, "monkeypatch": monkeypatch}


def _install_scribe(monkeypatch, fake):
    from character_swap.clients import elevenlabs
    monkeypatch.setattr(elevenlabs, "transcribe", fake)


def test_scribe_is_the_default_engine(monkeypatch, wired):
    fake = _FakeScribe(words=[{"text": "hallo", "start": 0.5, "end": 0.9}])
    _install_scribe(monkeypatch, fake)
    words, detected = video_edit.transcribe_detailed(Path("/x.mp4"), language="de")
    assert [w.text for w in words] == ["hallo"]
    assert (words[0].start, words[0].end) == (0.5, 0.9)
    assert detected == "german"
    # The hint reaches the provider, and the configured model is used.
    assert fake.calls == [{"model_id": settings.stt_scribe_model,
                           "language_code": "de"}]


def test_scribe_failure_falls_back_to_whisper(monkeypatch, wired):
    _install_scribe(monkeypatch, _FakeScribe(raises=RuntimeError("502 bad gateway")))
    used = {}

    def _fake_whisper_client():
        class _C:
            class audio:
                class transcriptions:
                    @staticmethod
                    def create(**kw):
                        used.update(kw)
                        class _R:
                            words = [{"word": "fallback", "start": 0.0, "end": 1.0}]
                            language = "english"
                        return _R()
        return _C()
    monkeypatch.setattr(video_edit.openai_image, "_client", _fake_whisper_client)

    words, detected = video_edit.transcribe_detailed(Path("/x.mp4"), language="de")
    # A transcription engine being down must degrade, never block a render.
    assert [w.text for w in words] == ["fallback"]
    assert detected == "english"
    # The hint is passed to whisper too, so the fallback isn't a downgrade twice.
    assert used.get("language") == "de"
    assert used.get("model") == "whisper-1"


def test_empty_scribe_result_falls_back_rather_than_captioning_nothing(
        monkeypatch, wired):
    # Ambiguous between "silent clip" and "bad response" — let whisper try.
    _install_scribe(monkeypatch, _FakeScribe(words=[]))
    called = {}

    def _fake_client():
        class _C:
            class audio:
                class transcriptions:
                    @staticmethod
                    def create(**kw):
                        called["yes"] = True
                        class _R:
                            words = []
                            language = None
                        return _R()
        return _C()
    monkeypatch.setattr(video_edit.openai_image, "_client", _fake_client)
    video_edit.transcribe_detailed(Path("/x.mp4"))
    assert called.get("yes"), "empty Scribe output must not short-circuit whisper"


def test_engine_can_be_pinned_to_whisper(monkeypatch, wired):
    monkeypatch.setattr(type(settings), "stt_engine",
                        property(lambda self: "whisper"), raising=False)
    fake = _FakeScribe(words=[{"text": "no", "start": 0, "end": 1}])
    _install_scribe(monkeypatch, fake)

    def _fake_client():
        class _C:
            class audio:
                class transcriptions:
                    @staticmethod
                    def create(**kw):
                        class _R:
                            words = [{"word": "whisper", "start": 0.0, "end": 1.0}]
                            language = "english"
                        return _R()
        return _C()
    monkeypatch.setattr(video_edit.openai_image, "_client", _fake_client)

    words, _ = video_edit.transcribe_detailed(Path("/x.mp4"))
    assert [w.text for w in words] == ["whisper"]
    assert fake.calls == [], "STT_ENGINE=whisper must not call Scribe at all"


def test_no_audio_track_still_short_circuits(monkeypatch):
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: False)
    assert video_edit.transcribe_detailed(Path("/silent.mp4")) == ([], None)


# ------------------------------------------------------- QC two-pass hint ----

def _passes(monkeypatch, by_language):
    """Stub transcribe_detailed to answer differently hinted vs unhinted."""
    seen = []

    def _tx(video, *, job_id=None, script_hint=None, language=None):
        seen.append(language)
        text = by_language[language]
        return ([video_edit.Word(text=t, start=i, end=i + 1)
                 for i, t in enumerate(text.split())], "german")
    monkeypatch.setattr(video_edit, "transcribe_detailed", _tx)
    return seen


def test_language_flagged_clip_runs_both_an_unhinted_and_a_hinted_pass(monkeypatch):
    seen = _passes(monkeypatch, {None: "unhinted text", "de": "hallo welt"})
    got = video_qc._transcribe(Path("/x.mp4"), "hallo welt", language="de")
    assert got is not None
    ok, heard, sim, _detected, heard_unhinted = got
    assert seen == [None, "de"], "unhinted first (for the language check), then hinted"
    assert heard == "hallo welt"            # the SCORE uses the hinted pass
    assert heard_unhinted == "unhinted text"  # the language check uses the other
    assert sim == 1.0 and ok


def test_english_clip_costs_exactly_one_pass(monkeypatch):
    seen = _passes(monkeypatch, {None: "baking soda"})
    got = video_qc._transcribe(Path("/x.mp4"), "baking soda")
    assert seen == [None]
    assert got[1] == got[4] == "baking soda"


def test_empty_hinted_pass_keeps_the_unhinted_text(monkeypatch):
    # A failed second pass must not score the clip against "".
    _passes(monkeypatch, {None: "hallo welt", "de": ""})
    got = video_qc._transcribe(Path("/x.mp4"), "hallo welt", language="de")
    assert got[1] == "hallo welt" and got[2] == 1.0


def test_wrong_language_check_reads_the_unhinted_pass(monkeypatch):
    """The check works by seeing the clip transcribe back to the ENGLISH line.
    Told the audio is German, English audio comes back as German-ish nonsense
    that matches nothing — so it must never be scored on the hinted pass."""
    monkeypatch.setattr(type(settings), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(settings), "video_qc_visual_enabled",
                        property(lambda self: False), raising=False)
    english = "put fresh garlic under your tongue"
    _passes(monkeypatch, {None: english, "de": "putt frisch garlik under jor tang"})

    v = video_qc.inspect_clip(Path("/x.mp4"),
                              movement_prompt=f'says: "hallo welt"',
                              expected_language="de", original_speech=english)
    assert v is not None and not v.passed and v.wrong_language
    assert english[:20] in v.reason


# ------------------------------------------------- similarity normalization --

def test_number_words_and_digits_score_as_equal():
    # Scribe writes "ten seconds", whisper writes "10 seconds"; both correct.
    assert video_qc.speech_similarity(
        "Put fresh garlic under your tongue for 10 seconds",
        "Put fresh garlic under your tongue for ten seconds") == 1.0
    # And the German direction that cost whisper-1 points.
    assert video_qc.speech_similarity("Nummer eins, Reis", "Nummer 1, Reis") == 1.0
    assert video_qc.speech_similarity("Número tres", "Número 3") == 1.0


def test_umlauts_no_longer_split_a_word_in_two():
    """The old `[^a-z0-9' ]` class DELETED umlauts, so "Stärke" tokenized as
    "st" + "rke" — every German and Spanish transcript was scored against a
    differently-tokenized expectation and lost points for nothing."""
    assert video_qc._norm_words("Kalte Stärke").split() == ["kalte", "stärke"]
    assert video_qc._norm_words("El almidón frío").split() == ["el", "almidón", "frío"]
    assert video_qc.speech_similarity("Kalte Stärke", "Kalte Stärke") == 1.0
    # ß and ss are the same word; accents are NOT folded ("año" ≠ "ano").
    assert video_qc._norm_words("Straße") == video_qc._norm_words("Strasse")
    assert video_qc._norm_words("año") != video_qc._norm_words("ano")


def test_a_real_mishearing_still_scores_low():
    # The normalization must not paper over actual garbled German.
    assert video_qc.speech_similarity(
        "Kalte Stärke verwandelt sich in Zucker",
        "Kalber starke Vervantics in Zucker") < 0.5
