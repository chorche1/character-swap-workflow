"""Generated image sizes must equal their TARGET aspect — no letterbox bars.

The old 1024x1792 (0.5714) was NOT true 9:16 (0.5625), so it letterboxed once the
seed fed a 9:16 video / the 1080x1920 caption canvas → black bars top+bottom in
the compiled output. Lock every size to its label so it can't silently regress.
"""
from __future__ import annotations

from character_swap import runner_media
from character_swap.config import settings


def _ratio(size: str) -> float:
    w, h = (int(x) for x in size.lower().split("x"))
    return round(w / h, 4)


def test_swap_image_size_is_true_9_16():
    # The Swap default seed must be exactly 9:16 so nothing downstream letterboxes.
    assert _ratio(settings.image_size) == round(9 / 16, 4)   # 0.5625


def test_freeform_aspect_map_matches_its_label():
    expected = {
        "1:1": round(1 / 1, 4),
        "9:16": round(9 / 16, 4),
        "16:9": round(16 / 9, 4),
        "4:5": round(4 / 5, 4),
    }
    for aspect, want in expected.items():
        got = _ratio(runner_media._openai_size_for(aspect))
        assert got == want, f"{aspect}: got ratio {got}, want {want}"
