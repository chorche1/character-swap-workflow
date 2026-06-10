"""The camera-pipeline realism pass: structural guarantees.

Byte-determinism is NOT promised (PIL's effect_noise is unseeded), so we
assert structure: JPEG output, dimensions preserved, pixels actually changed,
and the pass never explodes on small/odd-sized inputs.
"""
from __future__ import annotations

import io

from PIL import Image

from character_swap import realism


def _png_bytes(w: int = 220, h: int = 380, color=(120, 130, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_outputs_jpeg_with_same_dimensions():
    out = realism.degrade_to_phone_photo(_png_bytes(), seed=7)
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (220, 380)


def test_pixels_actually_change():
    src = _png_bytes()
    out = realism.degrade_to_phone_photo(src, seed=7)
    src_img = Image.open(io.BytesIO(src)).convert("RGB")
    out_img = Image.open(io.BytesIO(out)).convert("RGB")
    # Compare a center crop — vignette + noise + WB drift must move pixels.
    a = list(src_img.crop((100, 180, 120, 200)).getdata())
    b = list(out_img.crop((100, 180, 120, 200)).getdata())
    assert a != b


def test_handles_tiny_and_odd_sizes():
    for w, h in [(8, 8), (33, 47), (1, 100)]:
        out = realism.degrade_to_phone_photo(_png_bytes(w, h), seed=1)
        assert Image.open(io.BytesIO(out)).size == (w, h)


def test_degrade_file_roundtrip(tmp_path):
    src = tmp_path / "in.png"
    src.write_bytes(_png_bytes())
    dest = tmp_path / "out.jpg"
    got = realism.degrade_file(src, dest, seed=3)
    assert got == dest and dest.exists()
    assert Image.open(dest).format == "JPEG"
