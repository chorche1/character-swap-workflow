# Character Swap Studio — DEV copy

> **This is the live development copy** at `~/character-swap-workflow/`. May change at any time and may temporarily be broken. For day-to-day use, prefer the frozen stable copy at `~/character-swap-stable/` (see its README).



## What this project does

Local web app (FastAPI + Alpine.js + Tailwind) for character-swap-in-scene workflows. Persistent left sidebar of past jobs + a 5-step main panel:

1. **Scene** — upload one scene image.
2. **Characters** — pick one or more from a persistent library (upload new ones inline). Rename via inline ✎ icon. Choose **N images per character** (1–4, default 1).
3. **Generate** — GPT Image 2 generates N variant images per character (scene as ref #1, character as ref #2). For each character, user picks ONE variant to approve. Variants can be **edited with a custom prompt** to spawn a new variant for comparison. Per-variant download with friendly filename.
4. **Movement prompt** — type one movement prompt + choose **M videos per approved image** (1–4, default 1).
5. **Videos** — Grok Imagine animates each approved image M times. Live progress + per-video download with friendly filename.

**Sidebar:** jobs are grouped by **project** (collapsible). "+ New project" creates a project; "+" on a project header pre-selects it for the next job. The "⇄" icon on each job opens a move menu to send it to another project (or Unfiled). Unfiled jobs cluster at the bottom. Hover-✕ to hard-delete a job; ✕ on a project header CASCADES (deletes the project AND every job in it AND those jobs' `output/<job_id>/` directories — with a strong confirm that names the project and counts the jobs).

**Project character presets:** each project stores `character_ids: list[str]`. When you start a new job inside a project via the "+" on its header, those characters are auto-selected in Step 2 (filtered against the current library so deleted chars don't show). A "Save selection as project default" button appears in Step 2 when the current selection diverges from the preset — click to update the project. Deleting a character from the library automatically prunes it from every project's preset.

**Renames are everywhere:** characters in library (retroactive — propagates to all past jobs' snapshot names), job titles (inline above step 1), and download filenames are automatically friendly (`<char_name>-variant-N.png`, `<char_name>-edit-N.png`, `<char_name>-video-N.mp4`).

Dark mode toggle in the header, persisted across sessions, with `prefers-color-scheme` fallback for first visit.

No Claude calls. No automatic QC. Quality is gated by human approval before any Grok video is kicked off (Grok is the expensive step).

Resumable across browser closes AND server restarts: in-flight Grok jobs resume polling automatically on startup. Stale image generations from a killed server are marked `failed` so the user can click ↻ to retry.

---

## Quickstart

```bash
cd ~/character-swap-workflow
~/.local/bin/uv sync
~/.local/bin/uv run character-swap serve   # opens http://127.0.0.1:8000
```

Other commands:
```
character-swap status         # text summary of persisted state
character-swap reset --yes    # wipe state/state.json (keeps output/ files)
character-swap serve --reload --no-open
```

---

## Environment / Keys

Both `.env` and `.env.example` are loaded; `.env` wins. `env_ignore_empty=True` — empty shell var does NOT override the file value.

Required:
```
OPENAI_API_KEY=...
XAI_API_KEY=...
```

Optional overrides (defaults shown):
```
OPENAI_IMAGE_MODEL=gpt-image-2
GROK_VIDEO_MODEL=grok-imagine-video
XAI_BASE_URL=https://api.x.ai/v1
IMAGE_SIZE=1024x1792               # 9:16 portrait
IMAGE_CONCURRENCY=2                # parallel OpenAI image calls (caps gen + edit together)
VIDEO_DURATION_SECS=10
VIDEO_ASPECT_RATIO=9:16
VIDEO_RESOLUTION=720p
VIDEO_POLL_INTERVAL_SECS=12
VIDEO_TIMEOUT_SECS=600
HOST=127.0.0.1
PORT=8000
MAX_UPLOAD_BYTES=26214400          # 25 MB — rejects oversize uploads with 413
```

Claude / Anthropic key is not required (no QC in this version).

---

## Architecture

```
Browser (Alpine.js + Tailwind + dark mode)  ←─ WebSocket ─→  FastAPI
                                                                  │
                                                ┌─────────────────┴────────────┐
                                          runner.py                     state.json
                                                │                       (atomic)
                          ┌─────────────────────┼─────────────────────┐
                          │                     │                     │
              pipeline.generate_image   pipeline.edit_image     pipeline.submit_video
              + wait_for_video          (1 ref + custom prompt) + wait_for_video
```

- FastAPI process. `BackgroundTasks` runs async work; OpenAI/Grok client calls are sync so they go through `asyncio.to_thread`.
- `events.py` — in-process pub/sub keyed by `job_id`. WebSocket clients subscribe; runner publishes.
- `state.py` — atomic JSON persistence (scenes, character library, jobs with variants + videos).
- On server restart, `api.py`'s lifespan handler calls `runner.resume_pending(job_id)` for every job: in-flight Grok video polls resume; stuck image gens are marked failed.

---

## Module map

```
src/character_swap/
├── api.py          — FastAPI app: scenes/characters/jobs/edit_variant CRUD + WebSocket
├── runner.py       — Multi-variant image gen, edit, multi-video animation, resume
├── pipeline.py     — Pure primitives: generate_image, edit_image, submit_video, wait_for_video
├── events.py       — Asyncio pub/sub for live updates
├── state.py        — Atomic JSON state (scenes, characters, jobs)
├── models.py       — Pydantic: SceneAsset, CharacterAsset, ProjectAsset, GeneratedImage, VideoVariant, JobCharacter, Job, AppState + StrEnums (CharStatus, VariantStatus, VideoStatus)
├── config.py       — Settings via pydantic-settings
├── images.py       — sha256, base64, atomic write/copy
├── call_log.py     — JSONL call logger
├── cli.py          — Typer: serve, status, reset
└── clients/
    ├── openai_image.py  — GPT Image 2 wrapper; 1-ref (edit) or 2-ref (generate)
    └── grok.py          — xAI Grok Imagine REST client

web/
├── index.html      — Single page; Tailwind + dark mode + Alpine components
└── app.js          — Studio component (theme, counts, edit flow, WebSocket client)

input/scenes/       — Uploaded scenes: <scene_id><ext>
characters/         — Uploaded library: <char_id><ext>
output/<job_id>/<char_id>/
                    — variant_<vid>.png, edit_<vid>.png, video_<vid>.mp4
state/
├── state.json      — Atomic AppState
├── state.json.corrupt — Previous schema (kept on upgrade)
└── calls.jsonl     — Append-only API call log
```

---

## The generation prompt (verbatim — do not paraphrase)

Hardcoded in `pipeline.py::GENERATION_PROMPT`. Used for every initial variant. Edits use the user's custom prompt instead.

> "The man from the second picture is in the exact same pose in the exact same position and holding the exact same stuff in the exact same place as the man in the first picture. Remove any text overlays. 9:16 ratio. The background looks like it is the same environment as the second picture."

Order matters: scene is reference #1, character is reference #2. The user confirmed that "second picture" for background is intentional (the character keeps the scene's pose/items but in their own environment).

---

## API surface

```
GET    /                              → web/index.html
GET    /app.js                        → web/app.js
GET    /files/output/<rel>            → generated outputs (per job)
GET    /files/input/scenes/<rel>      → uploaded scene images
GET    /files/characters/<rel>        → uploaded character images
                                        (state/, .env, source are NOT exposed)

POST   /api/scenes                    multipart upload → {scene_id, url}
                                      max upload size: MAX_UPLOAD_BYTES (default 25 MB)
GET    /api/scenes/{scene_id}         metadata

POST   /api/characters                multipart upload → {char_id, name, url}
GET    /api/characters                list library
PATCH  /api/characters/{char_id}      body {name} — retroactive rename across all jobs
DELETE /api/characters/{char_id}      remove from library + disk

POST   /api/projects                  body {name, character_ids?: [...]}
                                      → {project_id, name, character_ids, n_jobs, ...}
GET    /api/projects                  list projects (with n_jobs, character_ids)
PATCH  /api/projects/{project_id}     body {name?, character_ids?} — at least one field
DELETE /api/projects/{project_id}     CASCADE: deletes project + every job inside
                                      + each job's output/<job_id>/ directory.
                                      Returns {ok, deleted_jobs: [...]}.

POST   /api/jobs                      body {scene_id, character_ids,
                                              images_per_character: 1..4, title?,
                                              project_id?}
                                      project_id null/absent = Unfiled
GET    /api/jobs                      list all jobs (full); ?summary=1 for compact list
GET    /api/jobs/{job_id}             job state (variants + videos, with download_name fields)
PATCH  /api/jobs/{job_id}             body {title?, project_id?}
                                      — project_id explicitly null moves job to Unfiled
                                      — at least one field required
DELETE /api/jobs/{job_id}             hard delete: state entry + output/<job_id>/ directory
POST   /api/jobs/{job_id}/approve     body {char_id, action: "approve"|"reject"|"regenerate", variant_id?}
                                      — approve requires variant_id
                                      — locked once movement_prompt is set (409)
POST   /api/jobs/{job_id}/edit_variant  body {char_id, variant_id, prompt}
                                      — produces a new variant (parent_variant_id set)
                                      — locked once movement_prompt is set (409)
POST   /api/jobs/{job_id}/movement    body {prompt, videos_per_character: 1..4}
                                      — locked once already set (409)

WS     /ws/jobs/{job_id}              live events; sends snapshot on connect

GET    /api/health                    {ok, version, openai_key, xai_key}
```

Per-character status (`models.CharStatus`):
```
queued → generating → awaiting_approval → approved → animating → done
                                       ↘ rejected (terminal)
                generating/animating → failed (retry with regenerate)
```

`awaiting_approval` flips on as soon as the **first** variant lands, so the user can start approving while the rest are still generating.

Per-variant status (`models.VariantStatus`): `generating | ready | failed`.
Per-video status (`models.VideoStatus`): `pending | processing | done | failed | error`.
Unknown intermediate states from Grok (e.g. "queued", "running") are coerced to `processing`.

---

## Working API shapes (preserved from prior debugging)

### OpenAI `images.edit`
- Two-ref (generate): `client.images.edit(image=[scene, char], prompt=..., model="gpt-image-2", size="1024x1792", n=1)`
- One-ref (edit): `client.images.edit(image=variant, prompt=custom, ...)`
- Multi-image must be passed as a list of open file handles. `clients/openai_image.py` uses `ExitStack`.
- 403 = OpenAI org isn't verified for `gpt-image-2`.

### Grok Imagine
```
POST https://api.x.ai/v1/videos/generations
GET  https://api.x.ai/v1/videos/{job_id}

Submit body: {"model": "grok-imagine-video", "prompt": ..., "duration": 10,
              "aspect_ratio": "9:16", "resolution": "720p",
              "image": {"url": "data:<mime>;base64,<b64>"}}
Submit response: {"request_id": "<job_id>", ...}   ← job ID at "request_id"
Status complete: {"status": "done", "video": {"url": "...", "duration": 10}, "progress": 100}
Terminal: {done, failed, error, cancelled}; success: {done}.
Download: plain GET on the video URL (httpx.stream 300s).
```

---

## Cost & safety notes

- Server binds to `127.0.0.1` only.
- Static-file serving uses **three narrow mounts**: `/files/output`, `/files/input/scenes`, `/files/characters`. `state/`, `.env`, source, and call logs are NOT reachable via HTTP — even if HOST is changed.
- Uploads are streamed in chunks and capped by `MAX_UPLOAD_BYTES` (default 25 MB) to prevent OOM from huge files.
- Image gen is gated by `IMAGE_CONCURRENCY` (default 2). With 5 chars × 4 variants = 20 calls; at concurrency 2 that's ~30–60s × 10 batches.
- Video gen fires all in parallel (Grok handles queueing server-side). With 5 chars × 4 videos = 20 Grok jobs; ~5–10 min each. UI shows count hints next to Generate / Animate buttons.
- Approvals + edits are locked after the movement prompt is submitted — protects the contract that videos came from a specific image.
- All API calls logged to `state/calls.jsonl`.

---

## Pending / nice-to-haves

- Edit-chain visualization beyond the small "↳ edit" badge (e.g. parent thumbnail on hover).
- SQLite-backed state (planned in `~/.claude/plans/`) — kills the write-amplification of full-file JSON rewrites.
