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

    # gpt-image moderation: ALWAYS "low" — hardcoded, not switchable (Hugo's
    # directive 2026-06-16). The consumer ChatGPT product runs its own tuned
    # moderation level; the API defaults to the stricter "auto", which was
    # rejecting ~49% of swap calls on safety grounds. "low" is permissive but
    # still filtered, and OpenAI's reference confirms it on BOTH the create and
    # edit endpoints for gpt-image models. This is the single biggest lever for
    # the "more NSFW errors than chatgpt.com" gap. Applies to every GPT path:
    # Swap (gpt-image), Swap/Reengineer (gpt2-id-swap), and the free-form Image
    # tab — all of them route through here.
    #
    # Sent via extra_body, NOT as a top-level kwarg: the openai SDK's typed
    # images.edit() does NOT expose `moderation` (only images.generate() does,
    # as of openai 2.36), so a top-level kwarg raises a CLIENT-SIDE TypeError
    # before the request even goes out — which failed EVERY swap (swaps always
    # use the edit endpoint). The /images/edits REST endpoint itself accepts
    # moderation, so extra_body merges it into the request body for BOTH create
    # and edit. Verified live 2026-06-17 against gpt-image-2.
    extra["extra_body"] = {"moderation": "low"}

    def _call(params: dict):
        if refs:
            with ExitStack() as stack:
                files = [stack.enter_context(p.open("rb")) for p in refs]
                return client.images.edit(
                    model=model,
                    image=files if len(files) > 1 else files[0],
                    prompt=prompt,
                    size=size,
                    n=1,
                    **params,
                )
        return client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            n=1,
            **params,
        )

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
            moderation="low",
        ) as entry:
            try:
                response = _call(extra)
            except (openai.BadRequestError, TypeError) as e:
                # Distinguish "this endpoint/SDK doesn't KNOW `moderation`" — an
                # unknown-argument 400, OR a client-side TypeError from an SDK
                # whose typed images.edit() signature lacks the param — from a
                # genuine content block (a 400 carrying a safety message). Only
                # the former drops the param and retries; a real content
                # rejection must propagate so content_policy's softening ladder
                # handles it.
                msg = str(e).lower()
                param_unknown = "moderation" in msg and (
                    isinstance(e, TypeError)
                    or any(
                        s in msg
                        for s in (
                            "unknown parameter", "unrecognized", "unexpected",
                            "unsupported", "not supported", "does not support",
                            "extra fields",
                        )
                    )
                )
                if param_unknown:
                    # Rebuild extra_body without moderation (fresh dict — never
                    # mutate the one already handed to the first call).
                    eb = dict(extra.get("extra_body") or {})
                    eb.pop("moderation", None)
                    if eb:
                        extra["extra_body"] = eb
                    else:
                        extra.pop("extra_body", None)
                    entry["moderation"] = None
                    response = _call(extra)
                else:
                    raise
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
