"""
Stub adapters for providers we haven't fully wired yet.

Every stub looks the same: it checks whether the relevant API key is set in
settings. If not, it raises `ProviderNotConfigured` (mapped to HTTP 503 by
the API layer) so the UI can lock the model selector. If the key IS set,
it raises `NotImplementedError` — meaning Hugo has provided the credential
and is ready for the real client implementation in a follow-up.

This keeps the model registry honest: every model in IMAGE_MODELS /
VIDEO_MODELS resolves to a real callable, even if that callable is currently
"add key + ask Claude to implement."
"""
from __future__ import annotations

from pathlib import Path

from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


def _check(provider: str, signup_url: str, *attrs: str) -> None:
    if not all(getattr(settings, a, "") for a in attrs):
        env_names = ", ".join(a.upper() for a in attrs)
        raise ProviderNotConfigured(
            provider,
            f"Add {env_names} to .env (sign up at {signup_url}).",
        )


# --- IMAGE PROVIDERS ----------------------------------------------------------------

def generate_flux(*, prompt: str, model: str, reference_images: list[Path] | None = None,
                  aspect_ratio: str | None = None, app_job_id: str | None = None) -> bytes:
    """Black Forest Labs FLUX (flux-pro-1.1-ultra / flux-pro / flux-schnell)."""
    _check("FLUX (Black Forest Labs)", "https://api.us1.bfl.ai/", "bfl_api_key")
    raise NotImplementedError(f"FLUX {model} client wiring pending — Hugo has the key now.")


def generate_ideogram(*, prompt: str, aspect_ratio: str | None = None,
                      app_job_id: str | None = None) -> bytes:
    _check("Ideogram", "https://developer.ideogram.ai/", "ideogram_api_key")
    raise NotImplementedError("Ideogram 3 client wiring pending.")


def generate_recraft(*, prompt: str, aspect_ratio: str | None = None,
                     app_job_id: str | None = None) -> bytes:
    _check("Recraft", "https://www.recraft.ai/docs", "recraft_api_key")
    raise NotImplementedError("Recraft v3 client wiring pending.")


def generate_stability(*, prompt: str, aspect_ratio: str | None = None,
                       app_job_id: str | None = None) -> bytes:
    _check("Stability AI", "https://platform.stability.ai/", "stability_api_key")
    raise NotImplementedError("Stable Diffusion 3.5 client wiring pending.")


# --- VIDEO PROVIDERS ----------------------------------------------------------------

def submit_runway(*, image: Path, prompt: str, aspect_ratio: str | None = None,
                  duration_secs: int | None = None, app_job_id: str | None = None) -> str:
    _check("Runway", "https://dev.runwayml.com/", "runway_api_key")
    raise NotImplementedError("Runway Gen-4 submit pending.")


def wait_for_runway(*, task_id: str, dest: Path) -> Path:
    _check("Runway", "https://dev.runwayml.com/", "runway_api_key")
    raise NotImplementedError("Runway Gen-4 poll pending.")


def submit_luma(*, image: Path, prompt: str, aspect_ratio: str | None = None,
                duration_secs: int | None = None, app_job_id: str | None = None) -> str:
    _check("Luma Dream Machine", "https://lumalabs.ai/dream-machine/api", "luma_api_key")
    raise NotImplementedError("Luma Ray-2 submit pending.")


def wait_for_luma(*, task_id: str, dest: Path) -> Path:
    _check("Luma Dream Machine", "https://lumalabs.ai/dream-machine/api", "luma_api_key")
    raise NotImplementedError("Luma Ray-2 poll pending.")


def submit_pika(*, image: Path, prompt: str, aspect_ratio: str | None = None,
                duration_secs: int | None = None, app_job_id: str | None = None) -> str:
    _check("Pika", "https://pika.art/api", "pika_api_key")
    raise NotImplementedError("Pika 2.2 submit pending.")


def wait_for_pika(*, task_id: str, dest: Path) -> Path:
    _check("Pika", "https://pika.art/api", "pika_api_key")
    raise NotImplementedError("Pika 2.2 poll pending.")


def submit_minimax(*, image: Path, prompt: str, model: str | None = None,
                   aspect_ratio: str | None = None, duration_secs: int | None = None,
                   app_job_id: str | None = None) -> str:
    _check("MiniMax Hailuo", "https://www.minimax.io/platform", "minimax_api_key")
    raise NotImplementedError(f"MiniMax {model or 'Hailuo'} submit pending.")


def wait_for_minimax(*, task_id: str, dest: Path) -> Path:
    _check("MiniMax Hailuo", "https://www.minimax.io/platform", "minimax_api_key")
    raise NotImplementedError("MiniMax Hailuo poll pending.")


# --- BYTEDANCE (Seedream / SeedEdit / SeedDance) ------------------------------------

def generate_seedream(*, prompt: str, model: str | None = None,
                      reference_images: list[Path] | None = None,
                      aspect_ratio: str | None = None,
                      app_job_id: str | None = None) -> bytes:
    """Covers seedream-3 and seededit (both ByteDance Volcano ARK)."""
    _check("ByteDance (Seedream/SeedEdit)", "https://www.volcengine.com/product/ark",
           "bytedance_api_key")
    raise NotImplementedError(f"ByteDance image client wiring pending (model={model}).")


def submit_seedance(*, image: Path, prompt: str, aspect_ratio: str | None = None,
                    duration_secs: int | None = None, app_job_id: str | None = None) -> str:
    _check("ByteDance Seedance", "https://www.volcengine.com/product/ark", "bytedance_api_key")
    raise NotImplementedError("Seedance submit pending.")


def wait_for_seedance(*, task_id: str, dest: Path) -> Path:
    _check("ByteDance Seedance", "https://www.volcengine.com/product/ark", "bytedance_api_key")
    raise NotImplementedError("Seedance poll pending.")


# --- ALIBABA (Wan 2.1 / 2.2) --------------------------------------------------------

def submit_wan(*, image: Path, prompt: str, model: str | None = None,
               aspect_ratio: str | None = None, duration_secs: int | None = None,
               app_job_id: str | None = None) -> str:
    """Single submit serves wan-2.1 and wan-2.2 — caller picks via `model`."""
    _check("Alibaba Wan (DashScope)", "https://dashscope.aliyun.com/",
           "alibaba_api_key")
    raise NotImplementedError(f"Alibaba {model or 'Wan'} submit pending.")


def wait_for_wan(*, task_id: str, dest: Path) -> Path:
    _check("Alibaba Wan (DashScope)", "https://dashscope.aliyun.com/", "alibaba_api_key")
    raise NotImplementedError("Alibaba Wan poll pending.")


# --- HIGGSFIELD (Soul / DoP / Lipsync / Speak, exclusive) ---------------------------

def submit_higgsfield(*, image: Path | None, prompt: str, model: str,
                      aspect_ratio: str | None = None,
                      duration_secs: int | None = None,
                      app_job_id: str | None = None) -> str:
    """Single submit covers all Higgsfield-exclusive models (Soul / DoP /
    Lipsync / Speak). Caller picks via `model`."""
    _check("Higgsfield", "https://higgsfield.ai/", "higgsfield_api_key")
    raise NotImplementedError(f"Higgsfield {model} submit pending.")


def wait_for_higgsfield(*, task_id: str, dest: Path) -> Path:
    _check("Higgsfield", "https://higgsfield.ai/", "higgsfield_api_key")
    raise NotImplementedError("Higgsfield poll pending.")


def generate_higgsfield_soul_img(*, prompt: str,
                                 reference_images: list[Path] | None = None,
                                 aspect_ratio: str | None = None,
                                 app_job_id: str | None = None) -> bytes:
    """Higgsfield Soul (image) — uses the same auth as the video pipeline."""
    _check("Higgsfield", "https://higgsfield.ai/", "higgsfield_api_key")
    raise NotImplementedError("Higgsfield Soul image client wiring pending.")


# --- OPENAI Sora 2 -------------------------------------------------------------------

def submit_sora(*, image: Path | None, prompt: str,
                aspect_ratio: str | None = None,
                duration_secs: int | None = None,
                app_job_id: str | None = None) -> str:
    """Sora 2 — bills against OPENAI_API_KEY but has its own quota/access tier."""
    _check("OpenAI Sora", "https://platform.openai.com/", "openai_api_key")
    raise NotImplementedError("OpenAI Sora 2 submit pending.")


def wait_for_sora(*, task_id: str, dest: Path) -> Path:
    _check("OpenAI Sora", "https://platform.openai.com/", "openai_api_key")
    raise NotImplementedError("OpenAI Sora 2 poll pending.")
