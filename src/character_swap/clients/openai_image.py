from __future__ import annotations

import base64
from contextlib import ExitStack
from pathlib import Path

import openai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from character_swap import content_policy
from character_swap.call_log import record
from character_swap.config import settings


class OpenAIImageError(Exception):
    pass


class ModelNotFoundError(OpenAIImageError):
    pass


_RETRY_EXCS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _client() -> openai.OpenAI:
    settings.require_keys("openai")
    return openai.OpenAI(api_key=settings.openai_api_key)


def _b64_to_bytes(b64: str) -> bytes:
    return base64.b64decode(b64)


def generate(*, prompt: str, **kwargs) -> bytes:
    """Generate an image, auto-recovering from content-policy rejections by
    retrying with a minimally softened prompt (see `content_policy`). Thin
    wrapper around `_generate_once`; all other kwargs pass straight through."""
    return content_policy.generate_with_softening(
        _generate_once, prompt=prompt, **kwargs
    )


@retry(
    retry=retry_if_exception_type(_RETRY_EXCS),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=120),
    reraise=True,
)
def _generate_once(
    *,
    prompt: str,
    reference_images: list[Path] | None = None,
    phase: str,
    character: str,
    size: str | None = None,
    job_id: str | None = None,
    model_override: str | None = None,
    quality: str | None = None,
) -> bytes:
    """
    Generate an image.

    If `reference_images` is non-empty, call the edits endpoint with all of them
    (gpt-image-2 accepts a list — first image is the base, subsequent images are
    additional references). Otherwise call the create endpoint with text only.

    `quality` maps to OpenAI's `quality` param: "low" | "medium" | "high" | "auto".
    Default `None` falls back to `settings.openai_image_quality` (OPENAI_IMAGE_QUALITY,
    "high" by default) so Swap variants render at full detail; pass an explicit
    value to override per-call, or set the env to "" to let OpenAI pick.

    Returns raw PNG bytes.
    """
    client = _client()
    size = size or settings.image_size
    model = model_override or settings.openai_image_model
    refs = reference_images or []

    # None → configured default ("high"); an explicit value (incl. "auto" or "")
    # passed by a caller still wins. Empty string → omit the param entirely so
    # OpenAI applies its own default.
    effective_quality = quality if quality is not None else settings.openai_image_quality
    extra: dict = {}
    if effective_quality:
        extra["quality"] = effective_quality

    try:
        with record(
            phase=phase,
            model=model,
            character=character,
            job_id=job_id,
            mode="edit" if refs else "create",
            n_references=len(refs),
            size=size,
            quality=effective_quality,
        ) as entry:
            if refs:
                with ExitStack() as stack:
                    files = [stack.enter_context(p.open("rb")) for p in refs]
                    response = client.images.edit(
                        model=model,
                        image=files if len(files) > 1 else files[0],
                        prompt=prompt,
                        size=size,
                        n=1,
                        **extra,
                    )
            else:
                response = client.images.generate(
                    model=model,
                    prompt=prompt,
                    size=size,
                    n=1,
                    **extra,
                )
            entry["request_id"] = getattr(response, "_request_id", None) or getattr(
                response, "id", None
            )
    except openai.NotFoundError as e:
        raise ModelNotFoundError(
            f"OpenAI image model '{model}' not found. "
            f"Override with OPENAI_IMAGE_MODEL in .env. Original: {e}"
        ) from e

    item = response.data[0]
    if getattr(item, "b64_json", None):
        return _b64_to_bytes(item.b64_json)
    if getattr(item, "url", None):
        import httpx

        with httpx.Client(timeout=60) as h:
            r = h.get(item.url)
            r.raise_for_status()
            return r.content
    raise OpenAIImageError("OpenAI image response had neither b64_json nor url.")
