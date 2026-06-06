"""Step-5 clip download names.

Every clip of a single character shares ONE filename (the character's name) so
they group together when organizing files on disk; different characters get
different names. Hermetic: pure function calls, no store/API needed.
"""
from __future__ import annotations

from character_swap import api
from character_swap.models import JobCharacter, VideoStatus, VideoVariant


def _vid(vid: str) -> VideoVariant:
    return VideoVariant(video_id=vid, grok_job_id="", status=VideoStatus.DONE)


def test_all_clips_for_a_character_share_one_name():
    jc = JobCharacter(
        char_id="cA", name="Chang", source_image_path="/a.png",
        videos=[_vid("a1"), _vid("a2"), _vid("a3")],
    )
    names = {api._video_download_name(jc, v) for v in jc.videos}
    assert len(names) == 1                                      # all identical
    assert names == {api._safe_filename_stem("Chang") + ".mp4"}


def test_different_characters_get_different_names():
    a = JobCharacter(char_id="cA", name="Chang", source_image_path="/a.png",
                     videos=[_vid("a1")])
    b = JobCharacter(char_id="cB", name="Ching", source_image_path="/b.png",
                     videos=[_vid("b1")])
    assert (api._video_download_name(a, a.videos[0])
            != api._video_download_name(b, b.videos[0]))


def test_name_has_no_per_clip_index():
    # Regression: the old "-video-N" suffix is gone.
    jc = JobCharacter(char_id="cA", name="Chang", source_image_path="/a.png",
                      videos=[_vid("a1"), _vid("a2")])
    assert "-video-" not in api._video_download_name(jc, jc.videos[1])
