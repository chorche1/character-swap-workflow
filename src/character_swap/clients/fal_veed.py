"""Wrapper around fal.ai's `fal-ai/workflow-utilities/auto-subtitle` endpoint
(powered by VEED's Subtitle Styling API).

Upload a local MP4 to fal's storage, submit the auto-subtitle job, poll until
done, then download the rendered MP4 with burned-in animated captions.

Hugo's reason for picking this: native ASS + Remotion renders don't match
Submagic/CapCut quality. fal.ai exposes VEED's actual subtitle engine at
~$0.10/min pay-as-you-go with no monthly floor.

API docs: https://fal.ai/models/fal-ai/workflow-utilities/auto-subtitle/api

Request fields (all optional except video_url):
  video_url            string  public URL — we upload first via fal.storage
  language             enum    "en" (also es/fr/de/it/pt/nl/ja/zh/ko)
  font_name            string  any Google Font (default "Montserrat")
  font_size            int     default 100
  font_weight          enum    "normal" | "bold" | "black"
  font_color           enum    13 named colors (default "white")
  highlight_color      enum    13 named colors (default "purple")
  stroke_width         int     default 3
  stroke_color         enum    default "black"
  background_color     enum    13 named or "none"/"transparent"
  background_opacity   float   0.0..1.0
  position             enum    "top" | "center" | "bottom"
  y_offset             int     default 75 (px from chosen edge)
  words_per_subtitle   int     default 3
  enable_animation     bool    default true

Response: {video: {url, ...}, transcription, subtitle_count, words, ...}
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from character_swap import call_log
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


ENDPOINT = "fal-ai/workflow-utilities/auto-subtitle"


def _client():
    """Lazy import + auth-check. Raises ProviderNotConfigured when no key set."""
    if not settings.fal_api_key:
        raise ProviderNotConfigured(
            "FAL_API_KEY not set — sign up at https://fal.ai/dashboard/keys "
            "and add `FAL_API_KEY=fal_...` to your .env"
        )
    try:
        import fal_client  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "fal-client package not installed. Run `uv add fal-client` "
            "(or `pip install fal-client`) and retry."
        ) from e
    # fal_client reads FAL_KEY from the environment by convention.
    os.environ["FAL_KEY"] = settings.fal_api_key
    return fal_client


def _probe_duration_secs(path: Path) -> float:
    """Best-effort duration probe so we can record cost. Uses ffprobe via the
    video_edit helpers; returns 0.0 on failure (cost-recording falls back to 0)."""
    try:
        from character_swap import video_edit
        return float(video_edit._probe_duration(path))
    except Exception:
        return 0.0


def render_captions(
    input_video: Path,
    output_video: Path,
    *,
    font_name: str = "Montserrat",
    font_size: int = 100,
    font_weight: str = "bold",
    font_color: str = "white",
    highlight_color: str = "yellow",
    stroke_width: int = 3,
    stroke_color: str = "black",
    background_color: str = "none",
    background_opacity: float | None = None,
    position: str = "bottom",
    y_offset: int = 75,
    words_per_subtitle: int = 3,
    enable_animation: bool = True,
    language: str = "en",
    extra_params: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> dict:
    """Submit input_video to fal.ai's VEED Subtitle Styling, block until done,
    and write the rendered MP4 to output_video.

    `extra_params` lets callers pass any field we haven't typed here — useful
    for future fal endpoint additions without code churn.

    Returns a dict with:
      output_url        public fal CDN URL we just downloaded
      transcription     full transcript string
      subtitle_count    int
      words             list of {text, start, end}
      n_words           convenience int
      duration_secs     input duration we billed against
    """
    fal = _client()
    duration_secs = _probe_duration_secs(input_video)

    with call_log.record(
        phase="fal_caption",
        model="fal-veed-subtitle-styling",
        job_id=job_id,
        duration_secs=duration_secs,
    ) as payload:
        # 1. Upload local file → fal storage → public URL.
        try:
            video_url = fal.upload_file(str(input_video))
        except Exception as e:
            raise RuntimeError(f"fal.upload_file failed: {e}") from e
        payload["upload_url"] = video_url

        # 2. Submit job and block until result is ready.
        arguments: dict[str, Any] = {
            "video_url": video_url,
            "language": language,
            "font_name": font_name,
            "font_size": font_size,
            "font_weight": font_weight,
            "font_color": font_color,
            "highlight_color": highlight_color,
            "stroke_width": stroke_width,
            "stroke_color": stroke_color,
            "background_color": background_color,
            "position": position,
            "y_offset": y_offset,
            "words_per_subtitle": words_per_subtitle,
            "enable_animation": enable_animation,
        }
        if background_opacity is not None:
            arguments["background_opacity"] = background_opacity
        if extra_params:
            arguments.update(extra_params)

        try:
            handler = fal.submit(ENDPOINT, arguments=arguments)
            result = handler.get()
        except Exception as e:
            raise RuntimeError(f"fal {ENDPOINT} failed: {e}") from e

        # 3. Download the rendered MP4 from the returned CDN URL.
        video = result.get("video") if isinstance(result, dict) else None
        if not video or not isinstance(video, dict) or not video.get("url"):
            raise RuntimeError(
                f"fal response missing video.url; got: {result!r}"
            )
        output_url = video["url"]
        output_video.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", output_url, timeout=120,
                          follow_redirects=True) as r:
            r.raise_for_status()
            with output_video.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)

        words = result.get("words") or []
        payload["subtitle_count"] = result.get("subtitle_count", 0)
        payload["n_words"] = len(words)
        return {
            "output_url": output_url,
            "transcription": result.get("transcription"),
            "subtitle_count": result.get("subtitle_count", 0),
            "words": words,
            "n_words": len(words),
            "duration_secs": duration_secs,
            "template": "veed-auto",
        }
