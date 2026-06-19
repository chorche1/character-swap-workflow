"""
Anthropic Claude client wrapper.

Used by `prompt_director.py` for the AI Director feature — one Claude Opus
call per Swap or Video job that does vision + tool-use to write tailored
per-variant prompts. Stays a thin wrapper around the official SDK so:

  - SDK is lazily imported (missing key or missing package doesn't break
    startup; only fails when Director is actually invoked).
  - `messages_with_tools(...)` is wrapped in `call_log.record(...)` so every
    Director call shows up in `state/calls.jsonl` with latency + cost,
    matching the rest of the codebase.
  - Images are encoded as Anthropic-shape content blocks
    (`{type: image, source: {type: base64, media_type, data}}`), with an
    optional Pillow resize to keep the request payload well under the 32 MB
    API limit.

Errors propagate to the caller (`prompt_director.direct_*`) which swallows
them and falls back to existing prompt-enrich / raw-prompt paths.
"""
from __future__ import annotations

import base64
import functools
from io import BytesIO
from pathlib import Path
from typing import Any

from character_swap.call_log import record
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


def _client():
    """Lazy SDK construction. Raises ProviderNotConfigured if no key OR if the
    `anthropic` package isn't installed — both treated as "Director path
    unavailable" by `prompt_director`, which falls back cleanly."""
    if not settings.anthropic_api_key:
        raise ProviderNotConfigured(
            "anthropic",
            hint="Add ANTHROPIC_API_KEY to .env to unlock the AI Director.",
        )
    try:
        import anthropic
    except ImportError as e:
        raise ProviderNotConfigured(
            "anthropic",
            hint=f"`anthropic` package is not installed ({e}). Run `uv sync`.",
        ) from e
    # Explicit timeout: the SDK default is 600s + 2 retries — a hung call
    # could stall the Reengineer analyst (which serializes in front of ALL
    # image generation) for up to ~30 min. Every caller (scene analyst, swap
    # QC, Director) has a clean fallback, so failing fast is strictly safer.
    return anthropic.Anthropic(api_key=settings.anthropic_api_key,
                               timeout=120.0, max_retries=1)


@functools.lru_cache(maxsize=64)
def _encoded_image(path_str: str, mtime_ns: int, max_long_edge_px: int) -> tuple[str, str]:
    """(media_type, base64-data) for a resized/re-encoded image file.

    Cached by (path, mtime, size): the swap-QC judge attaches the SAME scene
    frame + character reference to every one of a run's 45+ inspections —
    re-opening, LANCZOS-resizing, and re-encoding them each call was a few
    hundred GIL-bound ms apiece. `mtime_ns` in the key invalidates naturally
    when a file is regenerated in place.

    Encoding: photographic content (everything without a real alpha channel
    — scene frames, character refs, generated variants) goes out as JPEG q88,
    ~5-10x smaller than the optimized PNG it used to be; Anthropic's vision
    cost is resolution-based, so PNG bought nothing but upload time. Images
    WITH alpha keep lossless PNG.
    """
    from PIL import Image

    with Image.open(Path(path_str)) as img:
        has_alpha = (img.mode in ("RGBA", "LA")
                     or (img.mode == "P" and "transparency" in img.info))
        img = img.convert("RGBA") if has_alpha else img.convert("RGB")
        # Downscale on the LONG edge; preserves aspect ratio.
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > max_long_edge_px:
            scale = max_long_edge_px / long_edge
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.LANCZOS)

        buf = BytesIO()
        if has_alpha:
            img.save(buf, format="PNG")
            media_type = "image/png"
        else:
            img.save(buf, format="JPEG", quality=88)
            media_type = "image/jpeg"
        return media_type, base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _file_to_image_block(path: Path, *, max_long_edge_px: int = 1024) -> dict:
    """Convert a local image file to an Anthropic image content block.

    Resizes large images down so the base64-encoded payload stays well under
    Anthropic's 32 MB request limit (and so vision processing is faster).
    Encoded payloads are LRU-cached per (path, mtime); each call returns a
    FRESH block dict so callers may mutate their copy safely.
    """
    media_type, data = _encoded_image(str(path), path.stat().st_mtime_ns,
                                      max_long_edge_px)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def messages_with_tools(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: dict | None = None,
    max_tokens: int = 8192,
    temperature: float = 0.2,
    job_id: str | None = None,
    phase: str,
    character: str = "director",
    model: str | None = None,
    timeout: float | None = None,
) -> Any:
    """One Anthropic Messages API call wrapped in `call_log.record(...)`.

    `phase` should be one of {"director_swap", "director_movement"} so
    `call_log._cost_usd` charges the Opus per-call estimate. Returns the raw
    response object so callers can pull `tool_use` blocks via
    `extract_tool_call(...)`.

    `model` defaults to `settings.claude_opus_model` (env-overridable).

    `timeout` overrides the shared client's 120s per-attempt timeout for THIS
    call only (via `with_options`). The Reengineer scene analyst sends a large
    multi-scene vision request that legitimately needs longer than 120s — a too
    tight timeout there fails the call and drops to a generic motion prompt.
    """
    client = _client()
    if timeout is not None:
        client = client.with_options(timeout=timeout)
    chosen_model = model or settings.claude_opus_model
    kwargs: dict[str, Any] = {
        "model": chosen_model,
        "system": system,
        "messages": messages,
        "tools": tools,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice

    with record(phase=phase, model=chosen_model, character=character, job_id=job_id):
        response = client.messages.create(**kwargs)
    return response


def extract_tool_call(response: Any, tool_name: str) -> dict | None:
    """Pull the first `tool_use` block matching `tool_name` from a Messages
    response. Returns its `input` dict (i.e. the structured arguments Claude
    populated). Returns None if the tool wasn't called — caller treats that
    as a failure and falls back."""
    content = getattr(response, "content", None) or []
    for block in content:
        # SDK returns objects with attribute access; be defensive about dicts too.
        b_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if b_type != "tool_use":
            continue
        b_name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
        if b_name != tool_name:
            continue
        b_input = getattr(block, "input", None)
        if b_input is None and isinstance(block, dict):
            b_input = block.get("input")
        if isinstance(b_input, dict):
            return b_input
    return None
