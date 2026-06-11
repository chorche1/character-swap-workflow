"""One-off benchmark: PurplePill render time at --concurrency=1 (old) vs 4 (new).

Drives the real remotion_render bridge end-to-end on a real past edit
(ed_b1d8077f3d, 39s 1080x1920, 113 words). Deletes the SHA cache entry
between runs so both runs render for real. Run with:

    REMOTION_CONCURRENCY=<n> uv run python scripts/bench_remotion_concurrency.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from character_swap import remotion_render
from character_swap.config import settings
from character_swap.video_edit import TEMPLATES

edit_dir = Path.home() / "character-swap-data/output/editor/ed_b1d8077f3d"
input_video = edit_dir / "035-speed.mp4"
words = json.loads((edit_dir / "words.json").read_text())

tpl = TEMPLATES["capcut-purple-pill"]
props = tpl.to_remotion_props()

out = Path("/tmp/bench-purple-pill.mp4")
out.unlink(missing_ok=True)

# Pre-compute and clear the cache entry so the render is real.
probe = remotion_render._probe_video(input_video)
full_props = {
    "videoSrc": f"local://{input_video.name}",
    "words": words,
    "videoDurationSecs": probe.duration_secs,
    "videoWidth": probe.width,
    "videoHeight": probe.height,
    **props,
}
key = remotion_render._hash_render_inputs(
    "CapCutPurplePill", full_props, input_video.resolve())
cache_file = remotion_render._cache_dir() / f"{key}.mp4"
cache_file.unlink(missing_ok=True)

t0 = time.monotonic()
summary = remotion_render.render_remotion(
    input_video, out, composition_id="CapCutPurplePill",
    props=props, words=words, job_id="bench")
elapsed = time.monotonic() - t0
print(f"concurrency={settings.remotion_concurrency} "
      f"timeout={settings.remotion_timeout_ms} "
      f"cached={summary['cached']} elapsed={elapsed:.1f}s "
      f"out={out.stat().st_size/1e6:.1f}MB")
sys.exit(0)
