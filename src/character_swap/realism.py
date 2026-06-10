"""Deterministic "camera-pipeline" realism pass.

AI image models output suspiciously clean images: noise-free shadows, perfect
optics, single-generation JPEG/PNG. Real phone photos accumulate artifacts in a
fixed physical order — optics -> sensor -> ISP -> storage. This module replays
that pipeline on a finished swap variant so it reads as an ordinary unedited
phone photo instead of a render.

Pure PIL (no numpy / no diffusion / no API calls), so it can NEVER alter
identity, scene layout, or pose — Hugo's three unusable-killers are all
upstream concerns. Applied as an opt-in post-step on swap variants.

Recipe (each step small; the sum reads "phone camera", not "filter"):
  optics:  72% LANCZOS downscale + BICUBIC back up (kills sub-pixel AI crisp),
           ±1px R/B channel shift (lateral chromatic aberration),
           0.6px Gaussian blur (lens softness),
           ~10% corner vignette
  sensor:  shadow-weighted luma noise (sigma ~9/255, stronger in dark areas,
           like real photon/read noise; randomized ±30% per image)
  ISP:     white-balance drift (R*1.01 / B*0.99), blacks lifted ~3%,
           saturation * 0.97, then UnsharpMask AFTER noise — bakes the grain
           in the way a phone's sharpening stage does
  storage: double JPEG encode (q86 then q80, 4:2:0 subsampling) — first-save
           plus the re-encode every social upload applies
"""
from __future__ import annotations

import io
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageFilter


def _shift_channel(band: Image.Image, dx: int) -> Image.Image:
    """Horizontal channel shift (lateral chromatic aberration)."""
    return ImageChops.offset(band, dx, 0)


def _add_noise(img: Image.Image, sigma: float, rng: random.Random) -> Image.Image:
    """Shadow-weighted Gaussian luma noise.

    PIL's effect_noise gives 128-centered Gaussian noise; we add it per the
    luma weight: shadows get ~1.3x the noise, highlights ~0.7x — approximated
    by compositing a strong-noise and weak-noise version through an inverted
    luma mask.
    """
    w, h = img.size
    seed_noise = Image.effect_noise((w, h), sigma)            # L mode, mean 128
    # effect_noise has no seed parameter — roll the canvas by a seeded offset
    # so a fixed `seed` still yields a deterministic pattern.
    seed_noise = ImageChops.offset(seed_noise, rng.randrange(w), rng.randrange(h))
    noise_rgb = Image.merge("RGB", (seed_noise, seed_noise, seed_noise))
    gray128 = Image.new("RGB", (w, h), (128, 128, 128))

    strong = ImageChops.add(ImageChops.subtract(img, gray128, 1, 0), noise_rgb, 1, 0)
    # ^ img - 128 + noise == img + (noise - 128): additive zero-mean noise.
    weak = Image.blend(img, strong, 0.5)                      # half-strength

    # Mask: dark pixels -> strong noise. Invert luma, soften.
    luma = img.convert("L").filter(ImageFilter.GaussianBlur(4))
    inv_luma = luma.point(lambda v: 255 - v)
    return Image.composite(strong, weak, inv_luma)


def _vignette(img: Image.Image, strength: float = 0.10) -> Image.Image:
    """Radial corner darkening (multiplicative, `strength` at the far corners)."""
    w, h = img.size
    grad = Image.radial_gradient("L").resize((w, h))          # 0 center -> 255 edge
    # Map gradient -> multiplier image: 255 (center) .. 255*(1-strength) (edge).
    lut = [int(255 * (1.0 - strength * (v / 255.0) ** 2)) for v in range(256)]
    mult = grad.point(lut)
    mult_rgb = Image.merge("RGB", (mult, mult, mult))
    return ImageChops.multiply(img, mult_rgb)


def degrade_to_phone_photo(data: bytes, *, seed: int | None = None) -> bytes:
    """Run image bytes through the camera-pipeline degrade. Returns JPEG bytes.

    `seed` fixes the random *parameters* (noise sigma, pattern offset). The
    underlying PIL noise canvas is unseeded, so byte-level output still varies
    call-to-call — tests should assert structural properties, not hashes.
    """
    rng = random.Random(seed)

    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size

    # --- optics ---------------------------------------------------------
    img = img.resize((max(1, int(w * 0.72)), max(1, int(h * 0.72))), Image.LANCZOS)
    img = img.resize((w, h), Image.BICUBIC)

    r, g, b = img.split()
    img = Image.merge("RGB", (_shift_channel(r, 1), g, _shift_channel(b, -1)))

    # --- sensor ---------------------------------------------------------
    sigma = 9.0 * rng.uniform(0.7, 1.3)            # ~3.5% of full scale nominal
    img = _add_noise(img, sigma, rng)

    # --- ISP ------------------------------------------------------------
    r, g, b = img.split()
    r = r.point(lambda v: min(255, int(v * 1.01)))
    b = b.point(lambda v: int(v * 0.99))
    img = Image.merge("RGB", (r, g, b))
    img = img.point(lambda v: int(v * 0.97 + 255 * 0.03))     # blacks up ~3%
    img = ImageEnhance.Color(img).enhance(0.97)
    img = _vignette(img, 0.10)

    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))

    # --- storage --------------------------------------------------------
    buf1 = io.BytesIO()
    img.save(buf1, "JPEG", quality=86, subsampling=2)
    img2 = Image.open(io.BytesIO(buf1.getvalue()))
    buf2 = io.BytesIO()
    img2.save(buf2, "JPEG", quality=80, subsampling=2)
    return buf2.getvalue()


def degrade_file(src: Path, dest: Path | None = None, *, seed: int | None = None) -> Path:
    """File-path convenience wrapper. dest=None overwrites src in place."""
    out = dest or src
    out.write_bytes(degrade_to_phone_photo(src.read_bytes(), seed=seed))
    return out
