"""Anthropic image-block encoding: JPEG for photographic content + LRU cache.

The swap-QC judge attaches the same scene/character images to all 45+ calls
of a run; each used to be re-opened, LANCZOS-resized, and re-encoded as an
optimize=True PNG (3-5x larger than JPEG, a few hundred GIL-bound ms per
encode). Now: JPEG q88 unless the image has real alpha, cached per
(path, mtime, max_edge), fresh dict per call.
"""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from character_swap.clients import anthropic_client


@pytest.fixture(autouse=True)
def _clear_cache():
    anthropic_client._encoded_image.cache_clear()
    yield
    anthropic_client._encoded_image.cache_clear()


def _gradient_png(dest: Path, size=(640, 1136)) -> Path:
    """Photographic-ish PNG: per-pixel noise (like camera grain) defeats
    PNG's lossless prediction the way real photos do."""
    channels = [Image.effect_noise(size, 64) for _ in range(3)]
    img = Image.merge("RGB", channels)
    img.save(dest, format="PNG")
    return dest


def _alpha_png(dest: Path) -> Path:
    img = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
    img.putpixel((1, 1), (0, 255, 0, 255))
    img.save(dest, format="PNG")
    return dest


def test_photographic_png_reencoded_as_smaller_jpeg(tmp_path):
    src = _gradient_png(tmp_path / "frame.png")
    block = anthropic_client._file_to_image_block(src)
    assert block["source"]["media_type"] == "image/jpeg"
    jpeg_bytes = len(base64.b64decode(block["source"]["data"]))
    # Reference: what the old optimized-PNG path would have shipped.
    with Image.open(src) as img:
        buf = BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        png_bytes = len(buf.getvalue())
    assert jpeg_bytes < png_bytes * 0.5


def test_alpha_image_stays_png(tmp_path):
    src = _alpha_png(tmp_path / "overlay.png")
    block = anthropic_client._file_to_image_block(src)
    assert block["source"]["media_type"] == "image/png"
    # Alpha actually survives the round-trip.
    out = Image.open(BytesIO(base64.b64decode(block["source"]["data"])))
    assert out.mode == "RGBA"


def test_long_edge_resize_applies(tmp_path):
    src = _gradient_png(tmp_path / "big.png", size=(1500, 2600))
    block = anthropic_client._file_to_image_block(src, max_long_edge_px=1024)
    out = Image.open(BytesIO(base64.b64decode(block["source"]["data"])))
    assert max(out.size) == 1024


def test_cache_hits_on_same_file_and_invalidates_on_change(tmp_path):
    src = _gradient_png(tmp_path / "frame.png")
    anthropic_client._file_to_image_block(src)
    anthropic_client._file_to_image_block(src)
    info = anthropic_client._encoded_image.cache_info()
    assert info.hits == 1 and info.misses == 1
    # Rewrite in place with a different mtime → cache miss (fresh encode).
    import os
    _gradient_png(src, size=(320, 568))
    os.utime(src, ns=(src.stat().st_atime_ns + 10_000_000,
                      src.stat().st_mtime_ns + 10_000_000))
    anthropic_client._file_to_image_block(src)
    assert anthropic_client._encoded_image.cache_info().misses == 2


def test_block_dict_is_fresh_per_call(tmp_path):
    src = _gradient_png(tmp_path / "frame.png")
    a = anthropic_client._file_to_image_block(src)
    b = anthropic_client._file_to_image_block(src)
    assert a is not b and a["source"] is not b["source"]
    a["source"]["data"] = "mutated"
    assert b["source"]["data"] != "mutated"
