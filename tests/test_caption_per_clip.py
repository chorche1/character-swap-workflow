"""Per-clip caption alignment (Hugo 2026-06-26).

The bug: Step-6 / Reengineer finals burned captions that were correctly-WORDED
but completely MIS-TIMED — the script smeared uniformly across the whole video.
Root cause: when Whisper couldn't read a character's synthetic Kling voice on
the stitched reel, the fallback rebuilt the WHOLE script evenly-timed across the
WHOLE video (`script_fallback_words(whole_script, whole_duration)`). On the
2026-06-26 Listerine run, 4 of 5 finals fell back this way (36 words at uniform
~0.6 s slots, zero gaps). A second bug truncated the script itself: the
`_DIALOGUE_RE` capture stopped at the first inner quote, so a CTA like
`Comment "Skin" …` became just `Comment `.

The fix aligns captions PER CLIP: each clip is transcribed on its own (short
clips dodge the long-concat 'continuation-skip'); a clip Whisper reads keeps its
real per-word timing; a garbled clip's KNOWN line is even-timed within ITS OWN
slot, offset onto the concat timeline — never smeared across the whole video.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from character_swap import runner_compile, video_edit
from character_swap.runner_reengineer import _DIALOGUE_RE


def _words(text: str, *, t0: float = 0.0, step: float = 1.0,
           last_dur: float | None = None) -> list:
    toks = text.split()
    out = [video_edit.Word(text=t, start=t0 + i * step, end=t0 + i * step + step)
           for i, t in enumerate(toks)]
    if last_dur is not None and out:
        out[-1] = video_edit.Word(text=out[-1].text, start=out[-1].start,
                                  end=out[-1].start + last_dur)
    return out


# --- _DIALOGUE_RE: nested + multi-line quotes ---------------------------------


def test_dialogue_regex_captures_inner_quotes():
    """The CTA bug: `Comment "Skin" …` must NOT truncate at the inner quote."""
    mp = ('He says enthusiastically to the camera: "Comment "Skin" and I\'ll '
          'send you the one thing to make this 10x more powerful, follow me '
          'first or I can\'t reach you." Still camera, 9:16.')
    got = _DIALOGUE_RE.findall(mp)
    assert len(got) == 1
    assert got[0].startswith('Comment "Skin" and')
    assert got[0].endswith("reach you.")          # full CTA, trailing instr cut


def test_dialogue_regex_simple_and_multiline():
    assert _DIALOGUE_RE.findall('He says: "hello there"') == ["hello there"]
    multi = 'She says: "line one.\nand line two continues here"'
    got = _DIALOGUE_RE.findall(multi)
    assert got == ["line one.\nand line two continues here"]


def test_dialogue_regex_does_not_merge_two_clauses():
    """The inner-quote support must NOT swallow two separate says-clauses into
    one (the intervening narration is not an inner quote pair) — a plain
    balanced body merged them."""
    p = ('The person says: "add a teaspoon of baking soda" — casual delivery. '
         'The person says: "stir it well" at the end.')
    assert _DIALOGUE_RE.findall(p) == [
        "add a teaspoon of baking soda", "stir it well"]


def test_dialogue_extractor_is_single_source_of_truth():
    """All THREE callers share ONE compiled regex so they can never drift
    (a review found video_qc's private copy still had the old truncating
    pattern). The cycle-free home is video_edit."""
    from character_swap import runner_reengineer, video_qc
    assert runner_reengineer._DIALOGUE_RE is video_edit.DIALOGUE_RE
    assert video_qc._DIALOGUE_RE is video_edit.DIALOGUE_RE


def test_video_qc_expected_speech_keeps_full_cta():
    """The latent QC bug: expected_speech must NOT truncate a CTA at the inner
    quote (would cause false 'dialogue mismatch' rejections when VIDEO_QC is
    re-enabled)."""
    from character_swap import video_qc
    mp = 'He says: "Comment "Skin" and I\'ll send you the link"'
    assert video_qc.expected_speech(mp) == 'Comment "Skin" and I\'ll send you the link'


# --- remap_words_through_keeps ------------------------------------------------


def test_remap_identity_when_single_full_keep():
    ws = _words("a b c")                                  # (0,1)(1,2)(2,3)
    out = video_edit.remap_words_through_keeps(ws, [(0.0, 3.0)])
    assert [(w.start, w.end) for w in out] == [(0, 1), (1, 2), (2, 3)]


def test_remap_shifts_by_leading_trim():
    """A leading-silence cut shifts every word earlier by the onset amount."""
    ws = _words("a b", t0=0.5)                            # (0.5,1.5)(1.5,2.5)
    out = video_edit.remap_words_through_keeps(ws, [(0.5, 3.0)])
    assert [(w.start, w.end) for w in out] == [(0.0, 1.0), (1.0, 2.0)]


def test_remap_collapses_interior_gap_and_drops_removed_word():
    """Two kept ranges with a 1 s removed gap between them. A word inside the
    gap is dropped; a word in the 2nd range is shifted left by the gap."""
    keeps = [(0.0, 1.0), (2.0, 3.0)]                      # 1 s removed at [1,2]
    ws = [video_edit.Word("keep1", 0.2, 0.6),
          video_edit.Word("gone", 1.2, 1.8),             # entirely in the gap
          video_edit.Word("keep2", 2.2, 2.6)]
    out = video_edit.remap_words_through_keeps(ws, keeps)
    assert [w.text for w in out] == ["keep1", "keep2"]
    assert (out[0].start, out[0].end) == (0.2, 0.6)
    # 2nd range starts at output offset 1.0 (= len of first keep); word at
    # raw 2.2 → 1.0 + (2.2 - 2.0) = 1.2.
    assert (out[1].start, out[1].end) == (1.2, 1.6)


def test_remap_clamps_word_spanning_a_cut():
    ws = [video_edit.Word("span", 0.8, 1.5)]
    out = video_edit.remap_words_through_keeps(ws, [(0.0, 1.0)])
    assert (out[0].start, out[0].end) == (0.8, 1.0)       # clamped to the keep


# --- _resolve_caption_words_per_clip -----------------------------------------


def _run_per_clip(monkeypatch, tx_by_name, *, paths, keeps, dialogues,
                  threshold=0.55):
    def fake_tx(path, *, job_id=None, script_hint=None):
        return tx_by_name(Path(path).name, script_hint)
    monkeypatch.setattr(video_edit, "transcribe_words", fake_tx)
    return asyncio.run(runner_compile._resolve_caption_words_per_clip(
        paths, keeps, dialogues, edit_id="ed_t", threshold=threshold))


def test_per_clip_keeps_real_timing_and_falls_back_only_for_garbled_clip(
        monkeypatch):
    """Clip A reads cleanly → real Whisper timing. Clip B is garbled → its OWN
    line is even-timed within its OWN [3, 7] slot, NOT smeared over the reel."""
    def tx(name, hint):
        if name == "a.mp4":
            return _words("hello world")                  # clean read
        return _words("uh hmm")                           # garbled, both ways

    out = _run_per_clip(
        monkeypatch, tx,
        paths=[Path("a.mp4"), Path("b.mp4")],
        keeps=[[(0.0, 3.0)], [(0.0, 4.0)]],
        dialogues=["hello world", "foo bar baz"])

    assert [w.text for w in out] == ["hello", "world", "foo", "bar", "baz"]
    # Clip A: real timing at offset 0.
    assert (out[0].start, out[0].end) == (0.0, 1.0)
    # Clip B: even-timed "foo bar baz" across 4 s, offset by clip A's 3 s.
    assert out[2].text == "foo" and out[2].start == 3.0
    assert out[-1].end == 7.0                             # reaches end of reel
    # Monotonic, no overlap across the clip boundary.
    assert all(out[i].start <= out[i + 1].start for i in range(len(out) - 1))


def test_per_clip_empty_dialogue_uses_whisper_words(monkeypatch):
    """A clip with no known line (silent action shot that Kling voiced anyway)
    uses the unprompted transcript — real timing, offset onto the reel."""
    def tx(name, hint):
        if name == "a.mp4":
            return _words("intro hook here")
        return _words("the known line")

    out = _run_per_clip(
        monkeypatch, tx,
        paths=[Path("a.mp4"), Path("b.mp4")],
        keeps=[[(0.0, 3.0)], [(0.0, 3.0)]],
        dialogues=["", "the known line"])               # clip A has no script
    assert [w.text for w in out][:3] == ["intro", "hook", "here"]
    assert out[3].text == "the" and out[3].start == 3.0  # clip B offset by 3 s


def test_per_clip_warns_when_a_clip_falls_back(monkeypatch):
    """A garbled clip that falls back to even-timing surfaces a LOUD warning
    (count of fallback clips), so the user knows that clip's timing is
    approximate — never a silent mis-timing."""
    def tx(name, hint):
        return _words("hello world") if name == "a.mp4" else _words("uh hmm")

    warnings: list[str] = []

    async def warn(msg):
        warnings.append(msg)

    def fake_tx(path, *, job_id=None, script_hint=None):
        return tx(Path(path).name, script_hint)
    monkeypatch.setattr(video_edit, "transcribe_words", fake_tx)
    asyncio.run(runner_compile._resolve_caption_words_per_clip(
        [Path("a.mp4"), Path("b.mp4")], [[(0.0, 3.0)], [(0.0, 4.0)]],
        ["hello world", "foo bar baz"], edit_id="ed_t", threshold=0.55,
        warn=warn))
    assert len(warnings) == 1
    assert "1 klipp" in warnings[0]                       # exactly clip B


def test_per_clip_skips_second_whisper_call_when_hint_is_clean(monkeypatch):
    """Frugality: a clip whose script-biased read already covers the line
    cleanly must NOT trigger a 2nd (unprompted) Whisper call."""
    calls: list = []

    def fake_tx(path, *, job_id=None, script_hint=None):
        calls.append((Path(path).name, script_hint))
        return _words("hello world")

    monkeypatch.setattr(video_edit, "transcribe_words", fake_tx)
    asyncio.run(runner_compile._resolve_caption_words_per_clip(
        [Path("a.mp4")], [[(0.0, 3.0)]], ["hello world"],
        edit_id="ed_t", threshold=0.55))
    assert len(calls) == 1                                # only the hint call


# --- run_editor_pipeline wiring ----------------------------------------------


def test_run_editor_pipeline_uses_per_clip_when_dialogues_known(
        monkeypatch, tmp_path):
    """End-to-end: with clip_dialogues + per-clip keeps from assemble_clips,
    the persisted words.json is per-clip aligned (garbled clip even-timed in
    its own slot) — not the whole-script-uniform smear."""
    def fake_assemble(paths, out, **kw):
        Path(out).write_bytes(b"v")
        return {"n_clips": 2, "clip_keeps": [[(0.0, 3.0)], [(0.0, 4.0)]],
                "clip_out_durations": [3.0, 4.0]}
    monkeypatch.setattr(video_edit, "assemble_clips", fake_assemble)

    def fake_tx(path, *, job_id=None, script_hint=None):
        return (_words("pour it on") if Path(path).name == "a.mp4"
                else _words("uh hmm"))                    # clip B garbled
    monkeypatch.setattr(video_edit, "transcribe_words", fake_tx)
    monkeypatch.setattr(video_edit, "render_captions",
                        lambda src, out, **k: Path(out).write_bytes(b"\x00"))

    res = asyncio.run(runner_compile.run_editor_pipeline(
        [tmp_path / "a.mp4", tmp_path / "b.mp4"],
        edit_id="ed_t", edit_dir=tmp_path, template="minimal", overrides=None,
        enable_trim=False, enable_captions=True, enable_wpm_normalize=False,
        target_wpm=190.0, threshold_db=-24.0, min_silence_secs=0.4,
        pad_secs=0.1, voice_id=None, playback_speed=1.0,
        script_hint="pour it on the known cta line",
        clip_dialogues=["pour it on", "the known cta line"]))

    assert res.final.exists()
    words = json.loads((tmp_path / "words.json").read_text())
    texts = [w["text"] for w in words]
    assert texts[:3] == ["pour", "it", "on"]             # clip A real timing
    # Clip B (garbled) → its KNOWN line even-timed in its own [3, 7] slot.
    assert "the" in texts and "line" in texts
    cta_start = next(w["start"] for w in words if w["text"] == "the")
    assert cta_start >= 3.0
    assert words[-1]["end"] == 7.0
