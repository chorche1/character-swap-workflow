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


_AUTOMATE_PY = '''"""Full auto-pipeline: Resolve project → render → Google Drive upload.

End-to-end automation for one character\\'s edit:
  1. Connect to a running DaVinci Resolve instance
  2. Create (or open) a project named after this folder
  3. Import the video + SRT subtitles onto a fresh timeline
  4. Try to auto-balance every clip on the Color page (free-version best-effort;
     manual fallback documented in README)
  5. Configure 1080×1920 H.264 render settings with subtitle burn-in
  6. Render the timeline to <PROJECT>-final.mp4 in this folder
  7. (Optional) Upload the rendered MP4 to Google Drive

Quick start:
  - Install Resolve free: https://www.blackmagicdesign.com/products/davinciresolve
  - Open Resolve and KEEP IT RUNNING
  - From this folder: python automate.py
  - For Drive upload also: follow README "Google Drive setup" once.

Toggle which steps run via the CONFIG block below — useful when iterating.
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_NAME = HERE.name

# ===== CONFIG =====================================================================

# Set False to skip individual phases (handy while iterating).
DO_AUTO_COLOR  = True
DO_RENDER      = True
DO_DRIVE       = True

# Render output spec — match what the app produces (9:16 portrait, 1080×1920).
# Change FORMAT_WIDTH/HEIGHT for other aspect ratios.
FORMAT_WIDTH   = 1080
FORMAT_HEIGHT  = 1920
FRAME_RATE     = "30"
VIDEO_CODEC    = "H.264"
RENDER_NAME    = f"{PROJECT_NAME}-final"

# Drive parent folder ID. Find it in the URL when you open a folder in Drive
# (e.g. https://drive.google.com/drive/folders/THIS_LONG_ID_HERE). Leave None to
# upload to the root of My Drive. Override via env var DRIVE_FOLDER_ID.
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")  # or hard-code your folder id here

# ==================================================================================

# Resolve\\'s Python module lives outside the default path. Add the per-OS location.
if sys.platform == "darwin":
    sys.path.append(
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules/"
    )
elif sys.platform == "win32":
    sys.path.append(
        r"C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting\\Modules"
    )
else:
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

# ===== STEP 1: import video + subtitles =========================================

video_path = HERE / "video-pre-captions.mp4"
if not video_path.exists():
    video_path = HERE / "video-final.mp4"
if not video_path.exists():
    sys.exit("Neither video-pre-captions.mp4 nor video-final.mp4 exists in this folder.")

clips = media_storage.AddItemListToMediaPool(str(video_path))
if not clips:
    sys.exit(f"Could not import {video_path.name} into the media pool.")
video_clip = clips[0]

timeline = project.GetCurrentTimeline()
if timeline is None or timeline.GetName() != PROJECT_NAME:
    timeline = media_pool.CreateTimelineFromClips(PROJECT_NAME, [video_clip])
else:
    media_pool.AppendToTimeline([video_clip])

srt_path = HERE / "captions.srt"
if srt_path.exists():
    sub_clips = media_storage.AddItemListToMediaPool(str(srt_path))
    if sub_clips:
        media_pool.AppendToTimeline(sub_clips)

print(f"Project ready: {PROJECT_NAME!r}")
print(f"  video        : {video_path.name}")
print(f"  subtitles    : {'captions.srt' if srt_path.exists() else '(none)'}")
print(f"  video tracks : {timeline.GetTrackCount('video')}")
print(f"  subtitle trks: {timeline.GetTrackCount('subtitle')}")

# ===== STEP 2: Auto Color Balance (best-effort) =================================
# Free Resolve\\'s scripting surface for the Color page is limited; the explicit
# "Auto Color" / "Auto Balance" UI buttons don\\'t have a public scripting binding.
# The closest scripted alternative: switch to the Color page so the user can hit
# the shortcut (Shift+B = Auto Balance, currently selected node). If that fails
# silently, fall back to a neutral color-space tag on the clip so playback isn\\'t
# tinted by mismatched input/timeline color spaces.
if DO_AUTO_COLOR:
    try:
        resolve.OpenPage("color")
        video_items = timeline.GetItemListInTrack("video", 1) or []
        for item in video_items:
            # Tag with neutral Rec.709 if the API supports it. Wrapped in try/except
            # because some Resolve builds lack the property.
            try:
                item.SetProperty("ColorSpace", "Rec.709")
                item.SetProperty("Gamma", "Rec.709 Gamma")
            except Exception:
                pass
        print(f"  auto color   : opened Color page · {len(video_items)} clip(s) tagged Rec.709")
        print(f"                 (free Resolve: hit Shift+B per clip for full Auto Balance)")
    except Exception as e:
        print(f"  auto color   : skipped ({e})")
    finally:
        resolve.OpenPage("edit")

# ===== STEP 3: Render to MP4 =====================================================

if DO_RENDER:
    print()
    print(f"Rendering → {HERE / (RENDER_NAME + '.mp4')}")
    project.DeleteAllRenderJobs()  # start fresh

    render_settings = {
        "SelectAllFrames": True,
        "TargetDir": str(HERE),
        "CustomName": RENDER_NAME,
        "ExportVideo": True,
        "ExportAudio": True,
        "FormatWidth": FORMAT_WIDTH,
        "FormatHeight": FORMAT_HEIGHT,
        "FrameRate": FRAME_RATE,
        "VideoCodec": VIDEO_CODEC,
        "AudioCodec": "AAC",
        # Burn subtitles into the video instead of exporting as sidecar.
        "ExportSubtitle": True,
    }
    project.SetRenderSettings(render_settings)
    job_id = project.AddRenderJob()
    project.StartRendering([job_id])

    started = time.time()
    while project.IsRenderingInProgress():
        time.sleep(2)
        # Quick status print every 10s to show progress without spamming.
        if int(time.time() - started) % 10 == 0:
            sys.stdout.write(".")
            sys.stdout.flush()
    print()
    rendered_path = HERE / f"{RENDER_NAME}.mp4"
    if rendered_path.exists():
        size_mb = rendered_path.stat().st_size / 1_000_000
        print(f"  render OK    : {rendered_path.name} ({size_mb:.1f} MB)")
    else:
        # Resolve sometimes adds the codec suffix automatically (rare); look around.
        candidates = sorted(HERE.glob(f"{RENDER_NAME}*.mp4"))
        if candidates:
            rendered_path = candidates[-1]
            print(f"  render OK    : {rendered_path.name}")
        else:
            print(f"  render WARN  : no output found matching {RENDER_NAME}*.mp4")
            rendered_path = None
else:
    rendered_path = None

# ===== STEP 4: Upload to Google Drive (optional) ================================

def upload_to_drive(file_path: Path) -> str | None:
    """Upload one file to Google Drive via OAuth. Returns the webViewLink on
    success, None on skip/failure. On first use opens a browser for consent
    and caches a token.json so subsequent runs are quiet."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("  drive SKIP   : install with")
        print("    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        return None

    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    creds_path = HERE / "credentials.json"
    token_path = HERE / "token.json"

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            if not creds_path.exists():
                print("  drive SKIP   : no credentials.json next to automate.py")
                print("                 (see README for one-time Google Cloud setup)")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            print("  drive       : opening browser for first-time consent…")
            creds = flow.run_local_server(port=0, open_browser=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    metadata: dict = {"name": file_path.name}
    if DRIVE_FOLDER_ID:
        metadata["parents"] = [DRIVE_FOLDER_ID]
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)
    print(f"  drive       : uploading {file_path.name} ({file_path.stat().st_size / 1_000_000:.1f} MB)…")
    result = service.files().create(
        body=metadata, media_body=media,
        fields="id,name,webViewLink",
    ).execute()
    link = result.get("webViewLink")
    print(f"  drive OK    : {link}")
    return link

if DO_DRIVE and rendered_path and rendered_path.exists():
    try:
        upload_to_drive(rendered_path)
    except Exception as e:
        print(f"  drive ERROR : {e}")

print()
print(f"Done. Project {PROJECT_NAME!r} is open in Resolve.")


def customize() -> None:
    """Override default behavior here. Common things:

      # Use a specific LUT instead of the neutral Rec.709 tag
      for item in timeline.GetItemListInTrack("video", 1):
          item.SetLUT(1, "/Library/.../Some.cube")

      # Different render preset
      project.LoadRenderPreset("YouTube 1080p")

      # Color tag clips so you can see which were processed
      for item in timeline.GetItemListInTrack("video", 1):
          item.SetClipColor("Teal")

      # Add a Fusion title overlay
      timeline.InsertFusionTitleIntoTimeline("Text+")
    """
    pass


customize()
'''


_README_MD = """# Edit export — DaVinci Resolve auto-pipeline

Generated from the character-swap-workflow app. One folder per character.
`python automate.py` runs the full pipeline: create Resolve project →
import video + captions → render → upload to Google Drive.

## What's in this folder

| File | Purpose |
|---|---|
| `video-final.mp4` | Rendered output from the app (captions burned in if you used the captioned Compile) |
| `video-pre-captions.mp4` | Same video WITHOUT captions — preferred source for re-captioning in Resolve |
| `captions.srt` | SubRip subtitles — Resolve imports as a subtitle track + burns into render |
| `words.json` | Per-word timestamps (Whisper output) — raw data if you want custom captions |
| `automate.py` | Full pipeline script (Resolve API → render → Drive upload) |
| `README.md` | This file |
| `credentials.json` | You add this — Google OAuth client secret (see Drive setup below) |
| `token.json` | Auto-generated on first Drive run — cached OAuth token |
| `<PROJECT>-final.mp4` | Created by the render step |

## Quick start

```
python automate.py
```

Does everything end-to-end:
1. Connects to a running Resolve instance
2. Creates a project named after this folder
3. Imports the video + SRT
4. Switches to Color page + tags clips Rec.709 (best-effort auto-balance —
   free Resolve limits scripted color; see "Auto color caveat" below)
5. Renders 1080×1920 H.264 with burn-in subtitles → `<PROJECT>-final.mp4`
6. Uploads the rendered file to Google Drive (if credentials configured)

Toggle individual phases at the top of `automate.py`:
```python
DO_AUTO_COLOR  = True
DO_RENDER      = True
DO_DRIVE       = True
```

## One-time setup

### DaVinci Resolve

1. Download Resolve free: https://www.blackmagicdesign.com/products/davinciresolve
2. Install + open it once, accept all prompts (create a Blackmagic account)
3. On macOS: System Settings → Privacy & Security → Automation → grant your
   Terminal (or IDE) permission to control "DaVinci Resolve"
4. Keep Resolve RUNNING when you execute `automate.py`

### Google Drive (only needed for upload)

You need an OAuth Client ID. Free Google Cloud project, ~10 min setup.

1. Go to https://console.cloud.google.com/
2. Create a new project (any name)
3. APIs & Services → Library → search **"Google Drive API"** → Enable
4. APIs & Services → OAuth consent screen → User Type **External** →
   App name = anything, your email as developer contact → Save
   (Skip scopes step. Add your own email as a Test User.)
5. APIs & Services → Credentials → Create Credentials → OAuth Client ID →
   Application type **Desktop app** → Create
6. Click the download icon next to your new credential → save the JSON file
   as `credentials.json` in THIS folder (same dir as `automate.py`)
7. Run `python automate.py` — first time it opens a browser asking for
   Drive permission. Approve. `token.json` gets cached so future runs
   are silent.

Optional: pick a specific Drive folder for uploads. Open the folder in Drive,
copy the ID from the URL (`drive/folders/<THIS_PART>`), then set the env var:
```
export DRIVE_FOLDER_ID=1abcDeFgHiJkLmNoPqRsTuVwXyZ
```
Or hard-code it at the top of `automate.py`.

### Python deps (Drive only)

```
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

Skip if you don't want Drive — `automate.py` detects the missing import and
just skips the upload step.

## Extending automate.py

Resolve's Python API: https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-API.pdf

The `customize()` function at the bottom is where you add per-project tweaks.
Common operations:

- **Apply a LUT** — `timeline_item.SetLUT(1, "/path/to/look.cube")`
- **Different render preset** — `project.LoadRenderPreset("YouTube 1080p")`
- **Color-tag clips** — `timeline_item.SetClipColor("Teal")`
- **Add Fusion title** — `timeline.InsertFusionTitleIntoTimeline("Text+")`
- **Get/set timeline properties** — `timeline.GetSetting(...)`
- **Iterate render jobs** — `project.GetRenderJobList()`

## Auto color caveat

Free Resolve doesn't expose a scripting binding for the "Auto Color" / "Auto
Balance" buttons on the Color page. The script tags each clip with Rec.709
input/output color space as a neutral baseline — better than nothing for
unbalanced footage. For real per-clip auto-balance:

- **Manual** (free): after the script runs, switch to Color page in Resolve,
  select each clip, press **Shift+B** (Auto Balance shortcut). Then trigger
  the render manually OR re-run the script with `DO_AUTO_COLOR=False` to skip
  ahead.
- **Resolve Studio** (paid ~$300): full Color page API is exposed. Replace the
  STEP 2 block with calls to the Studio color API.
- **Apply a LUT**: bake in a "Look" LUT instead of relying on auto-balance.
  Put it in `customize()` (see Extending section above).

## Troubleshooting

- **"Could not import DaVinciResolveScript"** — Resolve isn't installed, or
  the install path is non-standard. Check the platform-specific path in
  `automate.py` (top of file) and adjust if your Resolve lives elsewhere.
- **"Open DaVinci Resolve first"** — Resolve isn't running. Open it, then
  re-run the script. Don't close Resolve mid-render.
- **Render finishes but no MP4** — Resolve may have added a codec suffix.
  Look for `<PROJECT>-final*.mp4` in this folder.
- **Drive: "redirect_uri_mismatch"** — your OAuth consent screen is in
  "Testing" mode and your Gmail isn't a Test User. Add it under OAuth
  consent screen → Test users.
- **Drive: "access_denied"** — your consent screen is in production but
  not verified, and Google blocks unfamiliar apps. Keep it in Testing mode.
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
