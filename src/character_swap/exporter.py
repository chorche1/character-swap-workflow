"""
Export rendered Editor outputs as a ready-to-script DaVinci Resolve package.

Each export is a ZIP containing:
  - video-final.mp4         the rendered output (with whatever captions/edits applied)
  - video-pre-captions.mp4  the same video BEFORE captions burn-in (if available)
  - captions.srt            SubRip subtitles built from words.json
  - words.json              per-word timestamps (Whisper output)
  - automate.py             starter script using DaVinci Resolve's Python API
  - README.md               quick-start guide

Hugo's workflow:
  1. Download the zip from the Editor tab's rendered result
  2. Unzip
  3. Open DaVinci Resolve (free version is fine on Mac)
  4. Run `python automate.py` — creates a Resolve project, imports the video,
     attaches the SRT as a subtitle track
  5. Edit by hand OR extend automate.py with Resolve's Python API

The SRT writer groups words into short cards (3 words per line by default)
so playback feels like CapCut/Submagic without needing to burn captions in.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path


def _format_srt_timestamp(secs: float) -> str:
    """SRT format: HH:MM:SS,mmm (comma not period for milliseconds)."""
    if secs < 0:
        secs = 0.0
    td = timedelta(seconds=float(secs))
    total_ms = int(round(td.total_seconds() * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def write_srt(words: list[dict], output_path: Path, words_per_line: int = 3) -> Path:
    """Build a SubRip (.srt) file from Whisper-style word list.

    `words` is a list of `{text, start, end}` dicts. Groups every N words into
    one subtitle cue. Picks the first word's `start` and the last word's `end`
    as the cue range.
    """
    if not words:
        output_path.write_text("", encoding="utf-8")
        return output_path

    cues: list[str] = []
    for cue_idx, i in enumerate(range(0, len(words), words_per_line), start=1):
        group = words[i:i + words_per_line]
        if not group:
            continue
        start = float(group[0].get("start", 0))
        end = float(group[-1].get("end", start + 0.5))
        if end <= start:
            end = start + 0.3
        text = " ".join(str(w.get("text", "")).strip() for w in group).strip()
        if not text:
            continue
        cues.append(
            f"{cue_idx}\n"
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n"
            f"{text}\n"
        )

    output_path.write_text("\n".join(cues), encoding="utf-8")
    return output_path


_AUTOMATE_PY = '''"""Drive DaVinci Resolve on this exported edit.

Setup (one-time):
  1. Install DaVinci Resolve (free version is fine):
       https://www.blackmagicdesign.com/products/davinciresolve
  2. Open Resolve once, accept any prompts, then close it
  3. Open Resolve again — it must be RUNNING when you execute this script
  4. From this folder, run:
       python automate.py

What this script does:
  - Connects to the running Resolve instance via its Python API
  - Creates (or opens) a project named after this folder
  - Adds the rendered video to the timeline
  - Imports captions.srt as a subtitle track you can hand-edit
  - Prints stats so you know it worked

Once that runs you have a real Resolve project. Extend `customize()`
with whatever automation you want — Resolve's full Python API is at
https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-API.pdf

Quick reference for common ops (uncomment in customize()):
  - timeline.GetTrackCount("video") / .GetItemListInTrack("video", 1)
  - clip.SetClipColor("Orange") / .GetDuration() / .SetProperty(...)
  - project.SetRenderSettings({...}) / .AddRenderJob() / .StartRendering()
  - media_pool.CreateEmptyTimeline("name")
  - timeline.InsertTitleIntoTimeline(...) / .InsertFusionTitleIntoTimeline(...)
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_NAME = HERE.name

# Resolve's Python module is installed outside the standard path.
# Add the platform-specific location so we can import it.
if sys.platform == "darwin":
    sys.path.append(
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules/"
    )
elif sys.platform == "win32":
    sys.path.append(
        r"C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting\\Modules"
    )
else:  # linux
    sys.path.append("/opt/resolve/Developer/Scripting/Modules/")

try:
    import DaVinciResolveScript as bmd  # type: ignore[import-not-found]
except ImportError:
    sys.exit(
        "Could not import DaVinciResolveScript. Install Resolve from\\n"
        "  https://www.blackmagicdesign.com/products/davinciresolve\\n"
        "and run this script again."
    )

resolve = bmd.scriptapp("Resolve")
if resolve is None:
    sys.exit("Open DaVinci Resolve first, then re-run this script.")

project_manager = resolve.GetProjectManager()
project = project_manager.LoadProject(PROJECT_NAME) or project_manager.CreateProject(PROJECT_NAME)
if project is None:
    sys.exit(f"Could not create/open project {PROJECT_NAME!r}")

media_pool = project.GetMediaPool()
media_storage = resolve.GetMediaStorage()

# Pick the cleanest source for editing: pre-captions if it exists, else final.
video_path = HERE / "video-pre-captions.mp4"
if not video_path.exists():
    video_path = HERE / "video-final.mp4"
if not video_path.exists():
    sys.exit("Neither video-pre-captions.mp4 nor video-final.mp4 exists in this folder.")

# Import the video file into the media pool, then onto a fresh timeline.
clips = media_storage.AddItemListToMediaPool(str(video_path))
if not clips:
    sys.exit(f"Could not import {video_path.name} into the media pool.")
video_clip = clips[0]

timeline = project.GetCurrentTimeline()
if timeline is None or timeline.GetName() != PROJECT_NAME:
    timeline = media_pool.CreateTimelineFromClips(PROJECT_NAME, [video_clip])
else:
    media_pool.AppendToTimeline([video_clip])

# Import captions.srt as a subtitle track Resolve can render or edit visually.
srt_path = HERE / "captions.srt"
if srt_path.exists():
    sub_clips = media_storage.AddItemListToMediaPool(str(srt_path))
    if sub_clips:
        media_pool.AppendToTimeline(sub_clips)

print(f"Project ready: {PROJECT_NAME!r}")
print(f"  video        : {video_path.name}")
print(f"  subtitles    : {'captions.srt' if srt_path.exists() else '(none)'}")
print(f"  video tracks : {timeline.GetTrackCount('video')}")
print(f"  audio tracks : {timeline.GetTrackCount('audio')}")
print(f"  subtitle trks: {timeline.GetTrackCount('subtitle')}")
print()
print("Resolve is now open with your project. Add automation in customize().")


def customize() -> None:
    """Extend this with whatever you want to automate.

    Examples (uncomment + adapt):
        # render the timeline to a fresh MP4 next to this script
        project.SetRenderSettings({
            "SelectAllFrames": True,
            "TargetDir": str(HERE),
            "CustomName": "rendered-from-script",
        })
        job_id = project.AddRenderJob()
        project.StartRendering([job_id])

        # tint the first clip orange for visibility
        first_clip = timeline.GetItemListInTrack("video", 1)[0]
        first_clip.SetClipColor("Orange")

        # add a Fusion title overlay
        title = timeline.InsertFusionTitleIntoTimeline("Text+")
    """
    pass


customize()
'''


_README_MD = """# Edit export — DaVinci Resolve

Generated from the character-swap-workflow Editor tab.

## What's in this folder

| File | Purpose |
|---|---|
| `video-final.mp4` | The rendered output from the Editor (captions burned in if you enabled them) |
| `video-pre-captions.mp4` | Same video WITHOUT captions — preferred for re-captioning in Resolve |
| `captions.srt` | SubRip subtitles. Resolve imports these as a subtitle track you can hand-edit |
| `words.json` | Per-word timestamps (Whisper output) — raw data if you want to build custom captions |
| `automate.py` | Starter script using Resolve's Python API |
| `README.md` | This file |

## Quick start

1. Install DaVinci Resolve free: https://www.blackmagicdesign.com/products/davinciresolve
2. Open Resolve once (accept any prompts), then keep it open
3. From this folder:

```
python automate.py
```

This creates a Resolve project named after this folder, imports the video onto
a timeline, and attaches `captions.srt` as a subtitle track. You can then edit
by hand in Resolve (mouse-drag captions, change colors, add transitions, etc.)
or extend `automate.py` with more Python.

## Extending automate.py

Resolve's full Python API is documented at:
https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-API.pdf

Common things you'll want:

- **Render to MP4** — `project.SetRenderSettings(...) / .AddRenderJob() / .StartRendering()`
- **Trim a clip** — `timeline.GetItemListInTrack("video", 1)[0].SetProperty(...)`
- **Add a transition** — `media_pool.AppendToTimeline([...])` with cross-dissolve clips
- **Apply a LUT** — `timeline_item.SetLUT(...)`
- **Burn captions back in** — Resolve auto-renders subtitle tracks unless excluded

The `customize()` function at the bottom of `automate.py` is where you should
add per-project automation logic.

## Notes

- The free version of Resolve supports Python scripting on Mac/Win/Linux.
- Scripting only works while Resolve is RUNNING — open it before running the script.
- If you get permission errors, check System Settings → Privacy & Security → Automation
  and grant Terminal (or your IDE) permission to control Resolve.
"""


def build_export_zip(
    *,
    final_video: Path,
    pre_caption_video: Path | None,
    words: list[dict] | None,
    project_name: str,
) -> bytes:
    """Bundle the export into an in-memory zip and return the bytes.

    Caller serves these bytes as a file download. The zip layout matches the
    `README.md` table above; missing inputs (e.g. no pre-caption file because
    the user skipped captions, or no words because they skipped transcribe)
    are silently omitted.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # The video bundle root inside the zip carries the project name so
        # `unzip foo.zip` creates a tidy folder instead of dumping files into
        # the current directory.
        root = project_name
        zf.write(final_video, f"{root}/video-final.mp4")
        if pre_caption_video and pre_caption_video.exists():
            zf.write(pre_caption_video, f"{root}/video-pre-captions.mp4")
        if words:
            zf.writestr(f"{root}/words.json", json.dumps(words, indent=2))
            # Materialize the SRT in-memory then add to zip.
            import tempfile
            with tempfile.NamedTemporaryFile(
                "w", suffix=".srt", delete=False, encoding="utf-8",
            ) as tf:
                tmp_srt = Path(tf.name)
            try:
                write_srt(words, tmp_srt)
                zf.write(tmp_srt, f"{root}/captions.srt")
            finally:
                tmp_srt.unlink(missing_ok=True)
        zf.writestr(f"{root}/automate.py", _AUTOMATE_PY)
        zf.writestr(f"{root}/README.md", _README_MD)
    return buf.getvalue()
