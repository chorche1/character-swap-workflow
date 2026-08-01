"""A 🇪🇸/🇩🇪 character must SPEAK that language (Hugo 2026-08-02).

Regression for re_b3170d2118: 8 of 10 German clips spoke ENGLISH. The prompts
were not the obvious kind of broken — the dialogue WAS correct German and the
German accent clause WAS present. What was missing was any statement of the
language next to the line: the localizer DELETED the source prompt's inline
"with an american accent" and appended the German clause ~200 characters
further down, leaving `says enthusiastically to the camera: "<German>"` inside
an otherwise-English prompt. Kling answered that by speaking English.

Two layers are locked here:

1. PROMPT — the inline attribution ("…to the camera in standard German: …")
   is guaranteed next to the dialogue, the English accent order is REPLACED
   rather than deleted, and an explicit "speaks ONLY German" order is added.
2. NET — a clip that transcribes back to the ENGLISH SOURCE LINE is failed as
   WRONG LANGUAGE, which always earns a re-render even though the fuzzy speech
   check runs flag-only, and fails the clip loudly when the re-renders don't
   take. (Whisper's own `language` field looked like the obvious signal and is
   NOT usable: measured "english" on a plainly German clip.)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import reengineer, runner, video_qc
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus, VideoStatus,
    VideoVariant,
)

# The exact shape Hugo writes at the gate, verbatim from re_b3170d2118.
HUGO_PROMPT = (
    'He says enthusiastically to the camera with an american accent: '
    '"This point under your tongue connects directly to your lymphatic '
    'system." while he is pointing at the garlic clove. Every word is '
    'pronounced clearly, correctly and distinctly. No background music — '
    'natural ambient room sound only.')


# --- 1. prompt layer ---------------------------------------------------------

@pytest.mark.parametrize("lang", ["de", "es"])
def test_inline_attribution_sits_next_to_the_dialogue(monkeypatch, lang):
    """THE regression. The language must be named INSIDE the says-clause, not
    only in a clause at the far end of the prompt."""
    spec = reengineer.SPOKEN_LANGUAGES[lang]
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, *, language="es", re_id=None: ["ÜBERSETZT"])
    out = reengineer.localize_motion_prompt(HUGO_PROMPT, lang)

    # Named right where the speaking happens — before the quote, not after it.
    assert f"to the camera {spec.attribution}:" in out
    assert out.index(spec.attribution) < out.index("ÜBERSETZT")
    # …and the old, too-distant guarantees are all still there.
    assert spec.accent_clause.strip() in out
    assert spec.speak_only_key in out.lower()


@pytest.mark.parametrize("lang", ["de", "es"])
def test_american_accent_never_survives(monkeypatch, lang):
    """Hugo's second ask: no Spanish/German prompt may still order an American
    accent. Two contradictory language orders in one prompt is how the English
    one — the one closest to the line — kept winning."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, *, language="es", re_id=None: ["X"])
    out = reengineer.localize_motion_prompt(HUGO_PROMPT, lang)
    assert "american accent" not in out.lower()
    assert "american english" not in out.lower()
    # The run-level path (a prompt hand-written at the gate on an es/de run)
    # is cleaned the same way — with_accent used to append the target clause
    # while leaving the American order in place.
    assert "american accent" not in reengineer.with_accent(
        HUGO_PROMPT, lang).lower()
    # English runs are untouched: there an American accent is what belongs.
    assert "american accent" in reengineer.with_accent(HUGO_PROMPT, "en").lower()


@pytest.mark.parametrize("prompt", [
    'She says to the camera: "Hallo zusammen." She smiles.',          # no accent phrase
    'AUDIO — Deep, clear male voice speaking English enthusiastically: "Hallo."',
    'Dialogue: "Guten Tag." Still camera.',                            # labeled
    'She says to the camera with an american accent: "Hallo."',        # accent phrase
])
def test_every_prompt_shape_gets_the_language_inline(monkeypatch, prompt):
    """The attribution must land for every dialogue shape the extractor knows —
    a shape that only got the trailing clause is a shape that can speak
    English."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, *, language="es", re_id=None: ["Hallo"])
    out = reengineer.localize_motion_prompt(prompt, "de")
    assert reengineer.SPOKEN_LANGUAGES["de"].attribution in out
    assert "speaks only german" in out.lower()


def test_enforcement_is_idempotent(monkeypatch):
    """A redo re-localizes text that is already localized — it must not stack
    a second attribution or a second speak-only order."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, *, language="es", re_id=None: ["Hallo"])
    once = reengineer.localize_motion_prompt(HUGO_PROMPT, "de")
    twice = reengineer._force_language_speech(once, "de")
    assert twice == once
    assert once.lower().count("speaks only german") == 1
    assert once.count(reengineer.SPOKEN_LANGUAGES["de"].attribution) == 1


def test_silent_clip_gets_no_speech_directive(monkeypatch):
    """A purely visual clip must stay untouched — we never inject speech into
    a shot that has none."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda *a, **k: pytest.fail("must not translate"))
    silent = "She walks across the room and picks up the box. Still camera."
    assert reengineer.localize_motion_prompt(silent, "de") == silent


# --- 2. the net: does the clip speak the ENGLISH source line? ---------------

EN_LINE = "Within minutes your sinuses begin to open"
DE_LINE = "Innerhalb von Minuten beginnen sich deine Nebenhöhlen zu öffnen"


def _qc_on(monkeypatch):
    cfg = __import__("character_swap.config", fromlist=["settings"]).settings
    monkeypatch.setattr(type(cfg), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(cfg), "video_qc_visual_enabled",
                        property(lambda self: False), raising=False)


def _heard(monkeypatch, text, sim):
    """Whisper heard `text`; `sim` is its similarity to the TRANSLATED line.
    The 4th element is Whisper's own language field — deliberately unused by
    the check (see below), so the tests feed it the wrong answer on purpose."""
    monkeypatch.setattr(video_qc, "_transcribe",
                        lambda *a, **k: (sim >= 0.35, text, sim, "english"))


def test_clip_that_speaks_the_english_source_line_is_wrong_language(monkeypatch):
    """THE detection. A German clip that spoke English used to be reported as a
    0.00 'dialogue mismatch', which buried the real cause AND inherited the
    flag-only retry policy."""
    _qc_on(monkeypatch)
    _heard(monkeypatch, "Within minutes your sinuses will begin to open", 0.0)

    v = video_qc.inspect_clip(Path("/x.mp4"),
                              movement_prompt=f'says: "{DE_LINE}"',
                              expected_language="de",
                              original_speech=EN_LINE)
    assert v is not None and not v.passed and v.wrong_language is True
    assert "fel språk" in v.reason and "German" in v.reason
    assert "ENGLISH" in v.corrective_hint

    # Same clip, character NOT language-flagged → the old similarity verdict.
    v2 = video_qc.inspect_clip(Path("/x.mp4"),
                               movement_prompt=f'says: "{DE_LINE}"')
    assert v2 is not None and not v2.passed and v2.wrong_language is False
    assert "dialogue mismatch" in v2.reason


def test_german_audio_is_never_called_wrong_language(monkeypatch):
    """The false positive that killed the first design. Whisper's own
    `language` field reported "english" for vd_26334d, whose audio is plainly
    German — gating on it would have re-rendered and then FAILED correct
    clips. Matching the ENGLISH SOURCE LINE cannot fail that way: German audio
    does not transcribe into the English sentence."""
    _qc_on(monkeypatch)
    _heard(monkeypatch,
           "Innerhalb von Minuten beginnen sich deine Nebenhöhlen zu öffnen",
           0.95)
    v = video_qc.inspect_clip(Path("/x.mp4"),
                              movement_prompt=f'says: "{DE_LINE}"',
                              expected_language="de",
                              original_speech=EN_LINE)
    assert v is not None and v.passed          # NOT flagged, despite "english"

    # Even GARBLED German (the real vd_26334d transcript) must not be called
    # wrong-language — it simply does not match the English line.
    _heard(monkeypatch,
           "Dieser Punkt unter der Sutna ist direkt mit dem Lymphsystem", 0.40)
    v2 = video_qc.inspect_clip(Path("/x.mp4"),
                               movement_prompt=f'says: "{DE_LINE}"',
                               expected_language="de",
                               original_speech=EN_LINE)
    assert v2 is not None and v2.wrong_language is False


def test_wrong_language_needs_a_strong_english_match(monkeypatch):
    """Unrelated noise is a dialogue mismatch, not a language failure — the
    loud path must not fire on a weak signal."""
    _qc_on(monkeypatch)
    _heard(monkeypatch, "uh hmm what", 0.0)
    v = video_qc.inspect_clip(Path("/x.mp4"),
                              movement_prompt=f'says: "{DE_LINE}"',
                              expected_language="de",
                              original_speech=EN_LINE)
    assert v is not None and not v.passed and v.wrong_language is False
    assert "dialogue mismatch" in v.reason


# --- 3. the net in the runner: forced retry, then a loud failure ------------

def _job(tmp_path):
    img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                         prompt="p", scene_id="s1", status=VariantStatus.READY)
    Path(img.path).write_bytes(b"img")
    jc = JobCharacter(char_id="cA", name="Helene",
                      source_image_path=str(tmp_path / "c.png"),
                      status=CharStatus.APPROVED, images=[img],
                      approved_variant_ids=["v1"], approved_variant_id="v1")
    (tmp_path / "c.png").write_bytes(b"c")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path=str(scene), scene_ids=["s1"],
              scene_image_paths=[str(scene)], video_model="kling-v3",
              characters={"cA": jc})
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    return job, jc, video


def _stub_runner(monkeypatch, tmp_path, *, verdicts, lang_retries=2):
    """Hermetic runner. `verdicts` is consumed one per take."""
    cfg = runner.settings
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir", lambda j, c: tmp_path)
    monkeypatch.setattr(runner, "_character_gender", lambda cid: None)
    monkeypatch.setattr(runner, "_character_language", lambda cid: "de")
    monkeypatch.setattr(reengineer, "localize_motion_prompt",
                        lambda p, code, job_id=None: p + " [DE]")

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(type(cfg), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    # Hugo's production setting: the fuzzy speech check is FLAG-ONLY.
    monkeypatch.setattr(type(cfg), "video_qc_max_retries",
                        property(lambda self: 0), raising=False)
    monkeypatch.setattr(type(cfg), "video_qc_language_max_retries",
                        property(lambda self: lang_retries), raising=False)

    takes: list[str] = []

    def fake_submit(**kw):
        takes.append(kw["movement_prompt"])
        return f"req-{len(takes)}"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    seq = list(verdicts)
    monkeypatch.setattr(video_qc, "inspect_clip",
                        lambda *a, **k: seq.pop(0) if seq else None)
    return takes


_WRONG = video_qc.ClipVerdict(False, "fel språk: klippet talar english, inte German",
                              "speak German", wrong_language=True)
_OK = video_qc.ClipVerdict(True, "", "")


def test_wrong_language_retries_even_though_speech_qc_is_flag_only(
        monkeypatch, tmp_path):
    job, jc, video = _job(tmp_path)
    takes = _stub_runner(monkeypatch, tmp_path, verdicts=[_WRONG, _OK])

    asyncio.run(runner._animate_one_video(job, jc, video, "He says hi"))

    assert len(takes) == 2                       # re-rendered despite max_retries=0
    assert "speak German" in takes[1]            # hardened prompt on the retry
    assert video.status == VideoStatus.DONE
    assert video.qc_status == "passed"


def test_wrong_language_fails_the_clip_loudly_when_retries_dont_take(
        monkeypatch, tmp_path):
    """Never ship a clip in the wrong language into a final — Hugo's standing
    'refuse loudly over silent partial'."""
    job, jc, video = _job(tmp_path)
    takes = _stub_runner(monkeypatch, tmp_path,
                         verdicts=[_WRONG, _WRONG, _WRONG], lang_retries=2)

    asyncio.run(runner._animate_one_video(job, jc, video, "He says hi"))

    assert len(takes) == 3                       # original + 2 language retries
    assert video.status == VideoStatus.ERROR
    assert "fel språk" in (video.error or "")


def test_garbled_take_on_a_flagged_character_also_retries(monkeypatch, tmp_path):
    """3 of the 8 English-speaking German clips came back as MANGLED English
    that matches neither language ("Lay a friction knob lock for 10 seconds
    under the netting"). They are not confidently wrong_language, but they
    still don't say the line — on a flagged character that earns a retry.
    A garbled LAST take ships flagged rather than failing the run."""
    job, jc, video = _job(tmp_path)
    garbled = video_qc.ClipVerdict(False, "dialogue mismatch (similarity 0.02)",
                                   "say it right")
    takes = _stub_runner(monkeypatch, tmp_path, verdicts=[garbled, _OK])
    asyncio.run(runner._animate_one_video(job, jc, video, "He says hi"))
    assert len(takes) == 2 and video.status == VideoStatus.DONE

    job2, jc2, video2 = _job(tmp_path)
    takes2 = _stub_runner(monkeypatch, tmp_path,
                          verdicts=[garbled, garbled, garbled], lang_retries=2)
    asyncio.run(runner._animate_one_video(job2, jc2, video2, "He says hi"))
    assert len(takes2) == 3
    assert video2.status == VideoStatus.DONE       # kept, flagged — not failed
    assert video2.qc_status == "failed"


def test_language_retries_are_off_for_unflagged_characters(monkeypatch, tmp_path):
    """An English character keeps the flag-only behavior exactly as before."""
    job, jc, video = _job(tmp_path)
    takes = _stub_runner(monkeypatch, tmp_path,
                         verdicts=[video_qc.ClipVerdict(
                             False, "dialogue mismatch", "say it right")])
    monkeypatch.setattr(runner, "_character_language", lambda cid: None)

    asyncio.run(runner._animate_one_video(job, jc, video, "He says hi"))

    assert len(takes) == 1                       # flag-only: no re-render
    assert video.status == VideoStatus.DONE      # clip kept, marked ⚠
    assert video.qc_status == "failed"
