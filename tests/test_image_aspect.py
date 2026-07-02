"""The Swap seed image size must equal its TARGET aspect — no letterbox bars.

The old 1024x1792 (0.5714) was NOT true 9:16 (0.5625), so it letterboxed once
the seed fed a 9:16 video / the 1080x1920 caption canvas → black bars
top+bottom in the compiled output. Lock the default so it can't silently
regress. (The free-form-tab aspect map tests died with runner_media's
_openai_size_for in the 2026-07-02 de-scope; settings.image_size is still the
default size for every Swap render — openai_image.py.)
"""
from __future__ import annotations

from character_swap.config import settings


def _ratio(size: str) -> float:
    w, h = (int(x) for x in size.lower().split("x"))
    return round(w / h, 4)


def test_swap_image_size_divisible_by_16():
    # gpt-image rejects any size whose W or H isn't divisible by 16 with a 400
    # ("Width and height must both be divisible by 16" — e.g. 1080 is not).
    w, h = (int(x) for x in settings.image_size.lower().split("x"))
    assert w % 16 == 0 and h % 16 == 0, \
        f"{settings.image_size}: W and H must both be ÷16"


def test_swap_image_size_is_true_9_16():
    # The Swap default seed must be exactly 9:16 so nothing downstream letterboxes.
    assert _ratio(settings.image_size) == round(9 / 16, 4)   # 0.5625
