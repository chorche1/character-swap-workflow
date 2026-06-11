# Character Swap Studio — DEV copy

> **This is the live development copy** at `~/character-swap-workflow/`. May change at any time and may temporarily be broken. For day-to-day use, prefer the frozen stable copy at `~/character-swap-stable/` (see its README).



## What this project does

Local web app (FastAPI + Alpine.js + Tailwind) for AI media generation. **Seven top-level tabs:**

- **Swap** (the original 6-step character-swap flow described below — default tab).
- **Image** — free-form text-to-image. Pick model (GPT Image, DALL·E 3, Grok Imagine, Nano Banana, FLUX variants, Ideogram, Recraft, SD3.5, Seedream, Higgsfield Soul). Optional reference images, aspect ratio, prompt → output appears in a grid below. Two opt-in prompt-quality toggles: **✨ enrich** (gpt-4o text expansion) and **🎬 AI Director** (Claude Opus reads the reference image with vision and writes a tailored prompt — requires `ANTHROPIC_API_KEY`).
- **Video** — free-form image-to-video. Pick model (Grok Imagine, Veo 3, Kling, Runway, Luma, Pika, Hailuo, Sora 2, Wan, Seedance, Higgsfield variants). Required reference image + motion prompt + aspect/duration → polled output appears in a grid. Same two prompt-quality toggles as Image tab: **✨ enrich** + **🎬 AI Director** (Director looks at the start frame and writes a cinematic shot direction with camera move + performance cues).
- **Avatar** — talking-head avatar video via HeyGen. Pick avatar + voice, paste a script, hit Generate. Two avatar models:
  - `heygen-avatar-5` — uses a HeyGen catalogue avatar (`avatar_id` + `voice_id` + script)
  - `heygen-photo-avatar` — uses an uploaded photo as the talking subject (`reference_paths[0]` + `voice_id` + script). Triggered via the 🎙 button on any ready variant in the Swap flow's Step 3.
  - **Voice source** can be either HeyGen's voice library OR the user's ElevenLabs voices, picked via a toggle. When ElevenLabs is selected, `voice_provider="elevenlabs"` and the runner renders via ElevenLabs TTS first, then feeds the audio into HeyGen.

- **Audio** — ElevenLabs voice library. Two modes via the `Mode` dropdown:
  - `elevenlabs-vc` — Voice Changer (Speech-to-Speech). **Accepts audio OR video uploads.** Video inputs: ffmpeg extracts the audio, ElevenLabs swaps the voice, ffmpeg re-muxes the new audio back into the original video stream → `result.mp4`. Pure-audio inputs return `result.mp3`.
  - `elevenlabs-tts` — Text-to-Speech. Paste a script, pick a voice, get an mp3.

- **B-roll** — drop a narration audio/video → Whisper transcribes → GPT-4o plans cinematic medical-realism B-roll prompts (the 4-mode "elite creative director" system prompt) → for each line, Grok generates a seed image + a short clip → trim each clip to match its phrase's spoken duration → concat in order → mux the original narration on top. Endpoints under `/api/broll/*`. The pipeline pauses at `awaiting_approval` so the user can reject + regenerate specific clips before finalizing. State lives per-job at `output/broll/<broll_id>/state.json`. See "B-roll details" section below.

- **Editor** — upload a video and run any combination of: (a) auto-trim silent gaps via ffmpeg silencedetect + concat, (b) **per-clip WPM normalization** (time-stretch each clip independently so the speaker hits target_wpm, pitch-preserving via ffmpeg `atempo`), (c) voice swap via ElevenLabs STS, (d) burn in word-level captions transcribed by OpenAI Whisper. **Captions ship in two engines**: (1) the legacy ASS path with 19 templates (popout-yellow family, submagic, modern-bold, rounded-soft/pop, instagram/-pop, tiktok-pop/-black, mrbeast, tiktok, karaoke, minimal, subtitle, kinetic, clean-shadow, bold-shadow, typewriter, bottom-third) burned in via `ffmpeg subtitles=` filter, and (2) the **Remotion path** with 4 React-rendered animated templates — `submagic-pro` (recommended default: Montserrat 900 italic, 22% active-word scale boost, per-word spring entrance, random per-card emphasis colors, accent glow halo), `submagic-pop` (Inter 900 italic, 20% active scale, random keyword highlights), `mrbeast-bold` (Anton ALLCAPS with 28% keyword size jump + per-word spring), `capcut-glow` (Poppins 900 cyan-glow + 18% active scale + outline stroke). Remotion templates require Node ≥ 18 and a one-time `character-swap remotion-install` (installs `remotion/node_modules/` + builds `web/static/remotion-preview.js` via esbuild so the in-browser preview uses `@remotion/player` — preview matches render exactly). Server-side render: `npx remotion render <CompositionId> <out.mp4> --props=props.json`, wrapped in `src/character_swap/remotion_render.py` with a SHA-256 cache. **Multi-clip mode**: upload N clips + a script, the system transcribes each, fuzzy-matches them to script positions, orders them, normalizes WPM per clip, concats. Plus a **CapCut-style timeline editor** for trim/split/segment-reorder on any finished result. **Visual caption editor** (✎ Edit captions button on any finished caption render): horizontal scrubbing timeline with draggable card rectangles + a rose-colored playhead that auto-follows preview playback and is grab-to-scrub; drag a card's left/right edge to retime the first/last word, drag the card body to shift the whole block; per-card text edit (cards-view) + per-word text+timing edit (per-word view) with split/merge/delete; live Remotion preview re-mounts on edit (180ms debounced) so changes show immediately. Endpoints under `/api/editor/*`. Outputs live under `output/editor/<edit_id>/`.

The Image/Video/Audio/Avatar tabs share the same sidebar (project/job history is swap-specific). Each tab has its own generation history grid loaded from `/api/generations?kind=...`. Locked models show a 🔒 chip with a tooltip naming the missing API key — they're rendered in the picker so users can see what's available but disabled.

**Characters are 1-to-many with images.** Each `CharacterAsset` has a list of `CharacterImage`s plus a `primary_image_id` pointing at the "main" thumbnail. Uploading via the modal asks whether the new image(s) belong to an existing character or create a new one. Same hash-named file is reused for duplicate uploads.

**Right-side character library** (toggle via the 📚 button in the header; open/closed persisted in `localStorage.char_lib_open`): per-character image gallery, drag-to-add into Step 2.

**Per-job source-image swap** (Step 2): if a character has 2+ reference images, the "N imgs ↕" badge on its card is clickable → opens a popover with all the character's gallery images → click any to swap it as the source for THIS job. **Works both before AND after the job is created** — before-job picks are staged client-side in `charSourceOverrides[charId]` and sent as `character_source_image_ids` on `POST /api/jobs`; after-job swaps go through `PATCH /api/jobs/{job_id}/characters/{char_id}/source_image`. Library primary stays unchanged. Existing variants keep their reference to the old source; only new variants from a regenerate use the new one.

**OS-level notifications + audio chime** for milestone events. Browser Notification API + Web Audio synthesized 2-tone bell (no asset file). Fires at two levels: (a) **approval gates** — swap char `awaiting_approval`, b-roll `awaiting_approval`; (b) **batch completions** — swap all-chars-terminal, swap Step-6 per-character compile done, every freeform gen done/failed, broll done/partial_success/failed, editor render (captions / rerender / timeline / multi-clip auto-edit / trim) done. Permission prompted once at `init()`; user toggles in header (🔔 OS popup + 🔊 chime), persisted to localStorage as `notif.os` / `notif.sound`. Greyed when browser permission is `denied`. Approval-pitch chime is higher (880→1320 Hz), done-pitch is softer (660→990 Hz). Tag-based dedup so same milestone doesn't fire twice. Single `notifyMilestone(title, body, opts)` fan-out point in `app.js`; in-app toast remains via existing `notify('info', ...)` channel.

The Swap flow (6 steps): persistent left sidebar of past jobs + main panel:

1. **Scene** — upload **one or more** scene images. Supports drop, click, and **Cmd+V paste** (multiple at once). Each scene becomes a separate reference background; the character gets variants for every scene. Per-tile ✕ to remove. Counter shows "(N scenes — each character gets variants for every scene)".
2. **Character images** — pick one or more from a persistent library (upload new ones inline). Rename via inline ✎ icon. **Preset voice (🎤)** dropdown on each library card sets an ElevenLabs voice that auto-applies in Step 6 compile + Editor tab. Choose **N images per character** (1–4, default 1). Optionally edit the **Generation prompt** (per job override) or save it as the project's default via "★ save as project default". Two opt-in prompt-quality toggles: **✨ enrich** (cheap, gpt-4o single-shot expansion of the user's prompt) and **🎬 AI Director** (Claude Opus agent with vision + tool-use; writes a tailored prompt per (character × scene × variant) — see "AI Director" section below).
3. **Generate** — GPT Image 2 (or Nano Banana / Nano Banana Pro / Grok Image, picked in Step 2) generates `images_per_character × N_scenes` variant images per character. When multiple scenes exist, variants render under per-scene subgroup headers inside each character section. **Multi-variant approval** — user picks ONE variant per (character × scene) by clicking the ✓ on each (re-click un-approves). **"✓ Approve all" button** auto-picks the first ready variant per (char, scene) for all characters at once. Variants can be **edited with a custom prompt** to spawn a new variant for comparison. **Per-variant retry** (↻) re-runs just one failed slot. Per-variant download with friendly filename.
4. **Movement prompt** — **per-IMAGE rows** (one per approved image): each approved image gets its OWN motion prompt AND its own clip duration (the Higgsfield "per-slot" model), so every video can be completely different. Thumbnail + textarea + per-image duration picker per row, plus an "⤓ apply image 1 to all" convenience. **Video provider picker** (one model for the whole job) lets you pick any of: Grok Imagine, Veo 3, Veo 3 Fast, Kling 2.0/2.1/2.5/2.6/3.0 + Pro/Master variants, Runway Gen-4/Gen-3, Luma Ray-2, Pika 2.2, Hailuo 01/02, Sora 2, Wan 2.1/2.2, Seedance, Higgsfield variants. **Submit kicks off all approved images × M videos in parallel**, all using the chosen provider. Backend: `POST /movement` accepts `movement_prompts_by_variant` (variant_id → prompt) + `durations_by_variant` (variant_id → secs); the runner resolves per-image override → per-scene prompt → fallback, and a per-scene `movement_prompts` is derived from the per-image dict for back-compat + the Step 6 compile. Per-image prompts are used verbatim (AI Director / enrich are skipped when they're set). The older per-scene path still works for jobs that send `movement_prompts`.
5. **Videos** — chosen provider animates each approved image M times. Live progress + per-video download with friendly filename.
6. **Compile final videos** — appears once every approved character has ≥1 DONE video. One click → for each character, concatenate that character's per-scene videos (in `scene_ids` order, picking the first DONE take per scene) into ONE final MP4 by running through the Editor pipeline (silence trim → voice swap → Whisper transcribe → WPM normalize → caption burn-in). All M characters compile **in parallel** using shared editor settings (template, target WPM, opt-out toggles). Each character's library-set preset voice auto-applies via ElevenLabs voice-changer; a batch-wide `voice_override` overrides all of them at once. Per-character cards show live `compiling → done / failed` status with preview + download. Failed compiles offer a per-character ↻ retry. Endpoint: `POST /api/jobs/{job_id}/compile_videos`; runner: [src/character_swap/runner_compile.py](src/character_swap/runner_compile.py). Output: `output/<job_id>/compiled/<char_id>.mp4` (plus full editor edit_id under `output/editor/<edit_id>/` so the compile result is also re-renderable from the Editor tab).

**Sidebar:** jobs grouped by **project** (collapsible). "+ New project" → modal. "+" on a project header pre-selects it for the next job. "⇄" icon moves a job between projects. **Cross-kind "Recent media" thumbnail strip at the bottom** shows the 32 newest items across all tabs (image / video / audio / avatar / broll) — click a thumbnail to jump to its tab and scroll to the card.

**Per-project default_prompt** (new). Each project can have its own default Swap generation prompt. Set via "★ save as project default" in Step 2, or via `PATCH /api/projects/{id}` with `{default_prompt: "..."}`. New jobs in that project inherit it; jobs without a project fall back to `pipeline.GENERATION_PROMPT`. UI in Step 2 shows a green "● using project default" indicator when active.

**Persistent cross-tab status toast** (bottom-right): aggregates all in-flight generations across tabs into one always-visible card. Each entry shows kind/status/progress. Click to jump to the tab. Auto-hides when no jobs are in flight. Powered by the `activeJobs` computed getter in `app.js`.

**History grids: search + filter on every kind.** Each grid (Image, Video, Audio, Avatar, B-roll) has a free-text search box (matches against prompt + model) and a model/status dropdown filter. Shows `filtered/total` count.

**Form-state persistence.** Image/Video/Audio/Avatar forms NO LONGER clear after submit — prompt + refs survive so the user can iterate. Each form has an explicit ✕ Clear button. Model/voice/aspect/duration picks per tab persist to `localStorage` and restore on next session.

**Reuse buttons** (`↺ Reuse` indigo pill) on every history card load the prompt + settings back into the active form.

**Friendly download names.** Every download (history cards, B-roll final, Editor result) uses a slug derived from the prompt + ISO date: `swollen-ankles-2026-05-15.mp4`. Helper: `friendlyName(g, ext?)` in `app.js`.

**B-roll "Mode" legend** at top of the tab (collapsible) explains the 4 visual modes the LLM picks from. Each clip card shows a small mode chip (violet/sky/amber/emerald per mode 1/2/3/4) with the full label on hover. Plus an orange `🔗 <scene_group>` chip on clips that visually continue from the previous clip in the same scene group.

**Renames are everywhere:** characters in library (retroactive — propagates to all past jobs' snapshot names), job titles (inline above step 1), and download filenames.

**Dark mode is forced** (no toggle). Light mode classes still in DOM but never applied.

Quality is double-gated: (1) automatic vision-QC — every generated swap IMAGE is inspected by a Claude vision call (swap_qc.py — judge: Sonnet 4.6 by default since 2026-06-11, env `SWAP_QC_MODEL`; checks: right person? holding/doing the SAME thing as the scene (wrong-prop images passed the old Haiku judge)? broken/cutout?) and auto-regenerated on failure (first retry = minimal-change REPAIR of the failed image, then fresh re-roll + hint; SWAP_QC=0 disables), and every generated video CLIP is checked (video_qc.py: Whisper transcript vs expected dialogue — catches garbled TTS like 'baking goda' — + frame-sampled anatomy check; 1 retry, VIDEO_QC=0 disables); (2) human approval before any video is kicked off (video is the expensive step). QC never blocks: unavailable → skipped; exhausted retries keep the last output with a ⚠ qc_status chip. QC + retries run OUTSIDE the image-gen semaphore (2026-06-11) so a judging/retrying slot never starves the generation lanes; the semaphore is sized per provider (`IMAGE_CONCURRENCY_FAL=8` / `_OPENAI=4` / `_GEMINI=2`, fallback `IMAGE_CONCURRENCY=2`).

**Moderation ladder (2026-06-11).** When the chosen engine rejects a swap prompt on content-policy grounds: the client first retries with two escalating append-only softeners (`content_policy.py`, rung 2 is a full fictional-film-production reframe); if STILL rejected and fal is configured, the runner falls back that one slot to `nbp-swap` — loud, never silent: recorded on `GeneratedImage.fallback_model`, emitted as `variant.fallback`, purple ⇄ chip in Swap + Reengineer UIs. (Measured rationale: 49% of gpt-image-2 swap calls were safety rejections burning the full ~131s render each; nbp-swap had 0 moderation failures on the same scenes.) The PIPELINE layer still has no cross-provider fallback — this is a sanctioned runner-level exception.

Resumable across browser closes AND server restarts: in-flight Grok jobs resume polling automatically on startup. Stale image generations from a killed server are marked `failed` so the user can click ↻ to retry. Reengineer resume (2026-06-11) reuses the run dir's `words.json`/`plan.json` instead of re-billing Whisper + the Claude analyst, and the swap job's id is persisted to the run state BEFORE job creation so a crash in that window re-attaches instead of creating a duplicate job. The Reengineer image-phase watchdog is PROGRESS-based (`SWAP_STALL_TIMEOUT_SECS`, default 10 min of zero progress; absolute backstop `SWAP_PHASE_MAX_SECS` 2 h) and actually CANCELS the generation tasks on stall — the old fixed 30-min deadline marked runs failed while generation kept billing. The Reengineer tab gets live per-variant updates over the existing `/ws/jobs/{job_id}` WebSocket (5s slim poll `GET /api/reengineer/{re_id}?slim=1` — variant prompts omitted — remains as fallback), shows a "k/N images · m QC retries" counter during the swap phase, and renders thumbnails via `x-if` so they appear the moment each file lands (the old `x-show` version 404'd before the file existed and stayed broken forever).

---

## Quickstart

```bash
cd ~/character-swap-workflow
~/.local/bin/uv sync
~/.local/bin/uv run character-swap serve   # opens http://127.0.0.1:8000
```

**Optional — Remotion captions** (3 modern animated templates in the Editor tab).
Requires Node.js ≥ 18 (`node --version`). One-time setup:

```bash
~/.local/bin/uv run character-swap remotion-install
```

This installs `remotion/node_modules/` and builds the in-browser preview
bundle to `web/static/remotion-preview.js`. Without this, the templates
`submagic-pop`, `mrbeast-bold`, and `capcut-glow` are hidden from the
Editor picker — the 19 ASS-rendered templates remain available.

Other commands:
```
character-swap status              # text summary of persisted state
character-swap reset --yes         # wipe state/state.json (keeps output/ files)
character-swap serve --reload --no-open
character-swap remotion-install [--force]   # rebuild Remotion preview bundle
```

### Shared data store (multi-worktree safe)

By default `state/`, `characters/`, `input/`, and `output/` live inside the active worktree. For multi-worktree dev (or to survive `git worktree remove`), move them to a shared location and point env vars at it:

```
~/character-swap-data/
├── .env              ← real file, symlinked into each worktree
├── state/            ← state.sqlite3 + calls.jsonl
├── characters/       ← uploaded character images
├── input/scenes/     ← uploaded scenes
└── output/           ← variants, videos, Editor renders, compile output
```

Add to the shared `.env`:
```
USE_SQLITE_STATE=1
CHARACTERS_DIR=/Users/hugonorrbom/character-swap-data/characters
INPUT_DIR=/Users/hugonorrbom/character-swap-data/input
OUTPUT_DIR=/Users/hugonorrbom/character-swap-data/output
STATE_DIR=/Users/hugonorrbom/character-swap-data/state
```

Then symlink in each worktree:
```bash
ln -s ~/character-swap-data/.env .env
```

All four data dirs are env-overridable via `CHARACTERS_DIR` / `INPUT_DIR` / `OUTPUT_DIR` / `STATE_DIR` ([config.py:107-110](src/character_swap/config.py)). Fall back to per-worktree defaults if env vars are unset.

---

## Environment / Keys

Both `.env` and `.env.example` are loaded; `.env` wins. `env_ignore_empty=True` — empty shell var does NOT override the file value.

Required for Swap + Image (GPT Image) + Video (Grok) + B-roll planning + Editor (Whisper):
```
OPENAI_API_KEY=...
XAI_API_KEY=...
```

Optional — each unlocks one or more models in the Image/Video model picker
(14 image + 17 video models registered):
```
ANTHROPIC_API_KEY=...             # 🎬 AI Director (Claude Opus with vision; opt-in toggle
                                  # on Swap, Image, Video tabs). ~$0.05 per Director call.
GEMINI_API_KEY=...                # Nano Banana + Nano Banana Pro + Veo 3 + Veo 3 Fast
KLING_ACCESS_KEY=...              # Both required for Kling 2.0 / 2.1 Pro / 1.6
KLING_SECRET_KEY=...
BFL_API_KEY=...                   # FLUX 1.1 Pro Ultra / Pro / Schnell / Kontext
IDEOGRAM_API_KEY=...              # Ideogram 3
RECRAFT_API_KEY=...               # Recraft v3
STABILITY_API_KEY=...             # Stable Diffusion 3.5
RUNWAY_API_KEY=...                # Runway Gen-4 + Gen-3 Alpha
LUMA_API_KEY=...                  # Luma Ray-2
PIKA_API_KEY=...                  # Pika 2.2
MINIMAX_API_KEY=...               # MiniMax Hailuo 02 + Hailuo 01
BYTEDANCE_API_KEY=...             # Seedream 3.0 + SeedEdit + Seedance (Volcano ARK)
ALIBABA_API_KEY=...               # Wan 2.1 + 2.2 (DashScope)
HIGGSFIELD_API_KEY=...            # Higgsfield Soul (image+video) / DoP / Lipsync / Speak
HIGGSFIELD_API_SECRET=...         # Required WITH the key for the official REST API
                                  # (Authorization: Key {key}:{secret}; create both at
                                  # cloud.higgsfield.ai/api-keys — distinct from the
                                  # CLI/MCP device-login). The "higgsfield-swap" Swap model
                                  # built on this (clients/higgsfield.py) was RETIRED from
                                  # the picker 2026-06-10: Soul regenerates an unrelated
                                  # scene instead of editing it (bake-off: fatal on every
                                  # output). Old jobs coerce to gpt-image on regenerate.
FAL_API_KEY=...                   # fal.ai — VEED captions AND the Swap instruction-edit
                                  # engines picked by the 2026-06-10 overnight bake-off
                                  # (clients/fal_image.py): "nbp-swap" (Nano Banana Pro
                                  # edit — the bake-off winner: best scene-fidelity +
                                  # identity + integration, zero fatals, survives
                                  # moderation-sensitive scenes GPT refuses), "nb2-swap"
                                  # (≈same look, half price), "seedream-edit-swap"
                                  # (Seedream 4.5, budget tier). These are Google/ByteDance
                                  # models HOSTED ON FAL — billed on the fal key, no Gemini
                                  # API quota. Swap default remains gpt-image.
HEYGEN_API_KEY=...                # HeyGen Avatar 5 — talking-head videos (Avatar tab)
ELEVENLABS_API_KEY=...            # ElevenLabs voice library (Audio tab + Editor voice swap +
                                  # optional voice source for HeyGen avatars)
```
Sora 2 (video) also uses `OPENAI_API_KEY` but requires separate API-tier access.

Optional overrides (defaults shown):
```
OPENAI_IMAGE_MODEL=gpt-image-2
GROK_VIDEO_MODEL=grok-imagine-video
GROK_IMAGE_MODEL=grok-imagine-image      # bumped from grok-2-image-1212 (deprecated 2026-02-24)
XAI_BASE_URL=https://api.x.ai/v1
CLAUDE_OPUS_MODEL=claude-opus-4-5        # AI Director — override to roll forward to a newer Opus
CLAUDE_OPUS_PRICE_USD=0.05               # rough per-call estimate, recorded in state/calls.jsonl
IMAGE_SIZE=1024x1792
IMAGE_CONCURRENCY=2               # fallback for providers without their own knob
IMAGE_CONCURRENCY_FAL=8           # per-PROVIDER swap-variant parallelism (2026-06-11):
IMAGE_CONCURRENCY_OPENAI=4        # the runner sizes its semaphore from the job's
IMAGE_CONCURRENCY_GEMINI=2        # effective model's provider — fal queues server-side
SWAP_STALL_TIMEOUT_SECS=600       # Reengineer image-phase watchdog: fail only when NO
                                  # progress (terminal flips / qc_attempts) for this long
SWAP_PHASE_MAX_SECS=7200          # absolute image-phase backstop (replaces old fixed 30 min)
VIDEO_DURATION_SECS=10
VIDEO_ASPECT_RATIO=9:16
VIDEO_RESOLUTION=720p
VIDEO_POLL_INTERVAL_SECS=12
VIDEO_TIMEOUT_SECS=600
HOST=127.0.0.1
PORT=8000
MAX_UPLOAD_BYTES=26214400
USE_SQLITE_STATE=1                # opt-in SQLite backend (vs full-file JSON)
```

---

## Architecture

```
Browser (Alpine.js + Tailwind, dark mode forced)  ←─ WebSocket ─→  FastAPI
                                                                       │
            ┌──────────────────────────────────────────────────────────┘
            │
   ┌────────┼─────────────────┬───────────────┬──────────────┐
   │        │                 │               │              │
runner.py   runner_media.py   runner_broll.py    state.json (atomic)
(Swap)      (Image/Video/     (B-roll: transcribe        + per-broll/per-edit
            Audio/Avatar       → plan → seed → vid       state.json files
            free-form)         → trim → concat → mux)    on disk
            │                  │
   ┌────────┴────────┐    video_edit.py
   │                 │    (ffmpeg primitives:
pipeline.            broll.py        trim, concat,
(generate/edit       (creative-      time_stretch,
+wait_for_video)     director         caption render,
                     prompt +        extract_last_frame,
                     plan_visuals    silence-detect,
                     + matcher)      Whisper transcribe)
```

- FastAPI process. `BackgroundTasks` runs async work; OpenAI/Grok client calls are sync so they go through `asyncio.to_thread`.
- `events.py` — in-process pub/sub keyed by `job_id`. WebSocket clients subscribe; runner publishes.
- `state.py` — atomic JSON persistence with opt-in SQLite backend (`USE_SQLITE_STATE=1`).
- `runner_broll.py` — full B-roll pipeline with bucket-by-scene-group scheduler (clips in a group chain off the previous clip's last frame via `extract_last_frame`).
- `video_edit.py` — every ffmpeg invocation we make: trim, concat, time-stretch with `atempo`, caption render via `subtitles` filter against generated ASS, Whisper transcribe, silence detect, extract last frame for B-roll continuity.

---

## Module map

```
src/character_swap/
├── api.py             — FastAPI app: every CRUD endpoint + WebSocket
├── runner.py          — Swap-flow runner: per-(scene, char) variants, edit, multi-video
├── runner_media.py    — Background runner for free-form Image/Video/Audio/Avatar
├── runner_broll.py    — B-roll pipeline: transcribe → plan → seed image → video →
                         scene-group chaining → trim → concat → mux audio
├── broll.py           — System prompt + plan_visuals (GPT-4o) + parser +
                         match_lines_to_timestamps + state I/O helpers
├── pipeline.py        — Pure primitives: generate_image, generate_variant (multi-model
                         image dispatch — gpt-image / grok-image / nano-banana /
                         nano-banana-pro), edit_image, submit_video + wait_for_video
                         (multi-model video dispatch — Grok / Veo / Kling / Runway /
                         Luma / Pika / Hailuo / Sora / Wan / Seedance / Higgsfield),
                         GENERATION_PROMPT
├── video_edit.py      — ffmpeg primitives + Whisper + caption templates (ASS engine +
                         Remotion engine branch) + WPM helpers + time_stretch +
                         extract_last_frame + apply_timeline (CapCut)
├── remotion_render.py — Python→Node bridge for the Remotion caption engine. Calls
                         `npx remotion render` as a subprocess; SHA-256 caches outputs
                         under `output/cache/remotion/<hash>.mp4`. Wrapped in
                         `call_log.record(phase="remotion_render", ...)`.
├── runner_compile.py  — Step 6: per-character compile. `compile_job_videos()` fans
                         out across every approved character via asyncio.gather; each
                         character concatenates its per-scene DONE videos (in
                         `scene_ids` order) and runs the result through the Editor
                         pipeline (trim → voice swap → transcribe → WPM → captions).
                         Settings apply uniformly batch-wide. Failure is per-character.
├── events.py          — Asyncio pub/sub for live updates
├── state.py           — Atomic JSON state OR SQLite (depending on USE_SQLITE_STATE).
                         Every entity (scene, character, project, job, generation) has
                         add_/update_/remove_ mutators that flush their own row(s)
                         inline. save() is a bulk-jobs re-flush only — call it when
                         you've mutated many job rows in one transaction (e.g. retroactive
                         character rename); use update_<entity> for everything else.
├── models.py          — Pydantic: SceneAsset, ProjectAsset (+default_prompt),
                         CharacterAsset (+voice_id, +voice_provider preset),
                         GeneratedImage (+scene_id), VideoVariant,
                         JobCharacter (+approved_variant_ids list — supports one approval
                         per scene per character; +compile_status / compiled_video_path
                         / compile_edit_id / compile_error for Step 6),
                         Job (+scene_ids list, +video_model, +movement_prompts dict
                         {scene_id: prompt}, +enriched_movement_prompts dict,
                         +use_director, +director_prompts_json cache),
                         MediaGeneration (+enrich_prompt, +enriched_prompt,
                         +use_director, +director_prompt),
                         AppState + StrEnums
├── config.py          — Settings via pydantic-settings
├── images.py          — sha256, base64, atomic write/copy
├── call_log.py        — JSONL call logger (now also bills director_swap / director_movement)
├── prompt_enrich.py   — Cheap (✨) prompt expansion via gpt-4o JSON-mode
├── prompt_director.py — Heavy (🎬) AI Director: Claude Opus vision + tool-use writes
                         tailored per-(char × scene × variant) prompts (direct_swap)
                         and per-scene cinematic shot prompts (direct_movement)
├── cli.py             — Typer: serve, status, reset, migrate
└── clients/
    ├── __init__.py       — `ProviderNotConfigured` exception (→ HTTP 503)
    ├── openai_image.py   — GPT Image 2 wrapper; text-only or with refs
    ├── anthropic_client.py — Lazy Anthropic SDK wrapper. messages_with_tools(...) +
                              extract_tool_call(...). Pillow-resizes images to 1024 px
                              long edge before base64. Wrapped in call_log.record.
    ├── grok.py           — xAI Grok REST: video submit/poll/download +
                            image generate. submit() accepts duration_secs kwarg
                            (clamped to [5, 15]) for B-roll per-clip duration matching.
    ├── elevenlabs.py     — list_voices + text_to_speech + voice_changer (live)
    ├── heygen.py         — list_avatars / voices / submit_avatar_video /
                            submit_photo_avatar / submit_avatar_video_with_audio (live)
    ├── google_genai.py   — Nano Banana / Nano Banana Pro via Gemini's REST
                            `generateContent` endpoint (httpx, no SDK dep). Veo still
                            a stub. Locked until GEMINI_API_KEY.
    ├── kling.py          — stub (locked until KLING_*_KEY)
    ├── higgsfield.py     — Higgsfield official REST API (platform.higgsfield.ai,
                            Authorization: Key {key}:{secret}). generate_swap():
                            upload scene+character → /v1/custom-references (cached
                            per char sha256 in state/higgsfield_refs.json) →
                            /v1/text2image/soul (custom_reference_id + scene
                            image_reference) → poll job-set → download. Powers the
                            Swap "higgsfield-swap" model. Locked until
                            HIGGSFIELD_API_KEY + HIGGSFIELD_API_SECRET.
    └── _stubs.py         — collected stubs for FLUX/Ideogram/Recraft/Stability
                            + Runway/Luma/Pika/MiniMax/Sora/Wan/Seedance/Higgsfield(soul/DoP/etc).

remotion/                  — React + Remotion project for the caption engine.
├── package.json           — remotion 4.0.247, @remotion/player, @remotion/google-fonts, react 19
├── remotion.config.ts     — Chromium config, concurrency=1
├── build-preview.mjs      — esbuild → web/static/remotion-preview.js (in-browser Player)
├── src/index.ts           — registerRoot(Root)
├── src/Root.tsx           — four <Composition> registrations
├── src/types.ts           — BaseCaptionProps, Word, DEFAULT_CAPTION_PROPS
├── src/lib/useCurrentWord.ts  — frame → active-card / active-word helpers
├── src/lib/colors.ts          — hex→rgb / rgba helpers
├── src/compositions/SubmagicPro.tsx   — RECOMMENDED: Montserrat italic ALLCAPS,
                                          22% active scale, random emphasis palette,
                                          per-word spring entrance, accent glow halo
├── src/compositions/SubmagicPop.tsx   — Inter 900 italic, 20% active scale, random
                                          keyword color emphasis, thick outline
├── src/compositions/MrBeastBold.tsx   — Anton ALLCAPS + 28% keyword size jump +
                                          per-word spring (snappy for keyword, gentle
                                          for filler), double-layered drop shadow
├── src/compositions/CapCutGlow.tsx    — Poppins 900, 18% active scale, per-word
                                          entrance spring, cyan glow + outline stroke
└── src/preview/index.tsx      — @remotion/player mount/update + playback API surface:
                                  seekToSecs, getCurrentTimeSecs, play, pause, isPlaying,
                                  onFrameUpdate (used by the visual caption editor's
                                  scrubbing playhead to auto-follow + drag-to-seek)

web/
├── index.html      — Single page; Tailwind via CDN + Alpine
└── app.js          — Studio component (all tabs, WebSocket client, history grids,
                     drag/drop/paste, status toast, sidebar thumbnails, WPM controls,
                     CapCut timeline, B-roll review gate, per-variant retry, etc.)

state/
├── fonts/           — Cached Google Fonts + locally-installed TTFs
│                      (Anton, Bebas Neue, Montserrat *, Poppins ExtraBold/Black,
│                      Inter ExtraBold/Black, Arial Rounded MT Bold,
│                      Instagram Sans Bold/Medium, TikTok Sans Bold/ExtraBold/Black)
├── state.json       — Atomic AppState (or SQLite at state/state.db)
└── calls.jsonl      — Append-only API call log

input/scenes/       — Uploaded scenes: sc_<hash><ext>
characters/         — Uploaded library: <char_id><ext>
output/<job_id>/<char_id>/
                    — variant_<vid>.png, edit_<vid>.png, video_<vid>.mp4
output/editor/<edit_id>/
                    — uploads, trimmed, swapped, captioned mp4s,
                      stretched clips, words.json, wpm_decisions.json,
                      pre_caption.txt, rerender-NN.mp4, timeline-NN.mp4
output/broll/<broll_id>/
                    — source audio, words.json, plan.json, state.json,
                      clips/clip-NN.png + clip-NN.mp4 (+ clip-NN-stretched.mp4
                      for trim-and-concat), concat.mp4, final.mp4
output/generations/<gen_id>/
                    — ref images + result for free-form tabs
output/<job_id>/compiled/<char_id>.mp4
                    — Step 6 per-character compiled final MP4 (concatenated scenes +
                      editor pipeline). Each compile also produces a parallel copy
                      under `output/editor/<edit_id>/04-final.mp4` so the result is
                      re-renderable from the Editor tab.
output/cache/remotion/<sha>.mp4
                    — SHA-256-keyed Remotion render cache
web/static/remotion-preview.js
                    — esbuild output for the in-browser @remotion/player bundle
```

---

## The Swap generation prompt

Hardcoded in `pipeline.py::GENERATION_PROMPT`. Used for every initial variant unless the job has a custom prompt OR its project has a `default_prompt`. Edits use the user's custom prompt instead.

As of the scene-regeneration-skill port, this is a **structured Option-B enforcement prompt** (not the old one-liner). It locks: full demographic override (zero identity bleed from the original subject), pixel-exact prop/layout preservation (count/color/material/position/physical-state), **background preserved exactly (Option B — no longer "change the background")**, exact framing & pose anchor, brand-label legibility, burnt-in caption/watermark removal, a **LIGHTING & INTEGRATION** section (relight the inserted person with the scene's own light + color grade, add contact/cast shadows, blend edges, match DoF/grain — so they don't look "pasted in"/cutout), and an inline `AVOID:` negative clause (the image models — GPT Image / Grok / Nano Banana / Nano Banana Pro — have no separate negative-prompt field, so negatives live inline). The **AI Director** system prompt (`prompt_director.py::SWAP_DIRECTOR_SYSTEM`) enforces the same directive set per (character × scene × variant) but can name specific props/demographics/background details + the scene's actual light sources because it sees the actual images. Both ported from Hugo's `scene-regeneration-prompt-v4` Higgsfield skill; the lighting-integration directives were added after observing "pasted-in subject" output.

Order matters: scene is reference #1, character is reference #2.

**Per-project override**: `ProjectAsset.default_prompt: str | None`. When set, jobs created in that project inherit it instead of the global default. `GET /api/swap/defaults?project_id=...` returns `{prompt, global_prompt, project_prompt}` so the frontend can show both the active and the global default.

---

## B-roll details

### Pipeline

1. **Transcribe** the source audio via Whisper (`verbose_json` + word timestamps). Word-level timestamps are noisy — most adjacent words come back back-to-back (no inter-word gap). True silences only show up as `next_word.start - prev_word.end > 0.4s`.
2. **Plan visuals** via GPT-4o with the system prompt in `broll.BROLL_SYSTEM_PROMPT` (~30KB). The prompt teaches:
   - The 4 visual MODES (Body-Part Transformation / Biological Process / Anatomical Flythrough / Contextual Human Moment)
   - CONTEXT CONTINUITY (ambiguous later lines anchor to earlier body parts)
   - BODY-PART VARIETY (don't keep returning to the same anchor)
   - SCENE GROUPING (consecutive recipe steps share a `SCENE_GROUP` tag → chained visuals)
   - ANATOMICAL CONNECTEDNESS (no floating feet/hands)
   - TIGHT FRAMING (never whole-body, always one body part)
   - NO STILL CLIPS (subject must be moving)
   - ONE SHOT ONE FRAME (no tile/grid/strip/split-screen)
   - The desired output format is `LINE:/MODE:/SCENE_GROUP:/PROMPT:` per segment; `SCENE_GROUP` is optional.
3. **Match each line to Whisper timestamps** via `broll.match_lines_to_timestamps` (difflib at the word level). Each clip gets `timing: {start, end, duration, spoken_duration, unmatched}`. Silences between phrases fold into the preceding clip so total clip-track length == total audio length.
4. **Per-clip duration target**: `max(5, ceil(spoken_duration + 1))` (5s minimum for Grok, +1s safety margin). Appended to the prompt as "Clip duration target: ~Ns seconds."
5. **Generate clips**: scene groups run concurrently (cap 3); clips WITHIN a group run strictly sequentially. The first clip in a group gets a fresh Grok seed image; later clips in the same group get the previous clip's last frame extracted via ffmpeg `-sseof -1.0` as their seed → cumulative state preserved (recipe step 2 shows water + lemon over water from step 1).
6. **Image guard suffix** is appended to EVERY Grok text-to-image call to defend against the 3-panel-storyboard fail mode that Grok's image model tends to produce for "BEFORE → AFTER" prompts.
7. **Variation hint** on retries: "previous version was rejected, pick a different camera angle / framing / lighting" — appended to both image and video prompts.
8. **Auto-retry**: failed clips get 1 automatic retry (constant `_CLIP_MAX_ATTEMPTS = 2` at top of `runner_broll.py`).
9. **Pause at `awaiting_approval`**: pipeline does NOT auto-finalize. User reviews each clip in the UI, can ✕ Reject & regenerate any (with variation hint + cache-buster query string on URLs), then clicks ✓ Finalize.
10. **Finalize**: trim each clip to its allotted duration, concat in script order, mux original narration on top via `replace_audio` (with `-shortest`).

### Endpoints

```
POST   /api/broll/generate              multipart {file, video_model, aspect_ratio}
GET    /api/broll                       list all
GET    /api/broll/{broll_id}            single (poll)
DELETE /api/broll/{broll_id}            full cleanup
POST   /api/broll/{broll_id}/regenerate_clip   body {idx} — chains from prior in scene group
POST   /api/broll/{broll_id}/finalize          trim+concat+mux
```

### State machine

```
queued → transcribing → planning → generating_clips → awaiting_approval ⇄ regen
                                          ↓ ✓ Finalize
                                    concatenating → done | partial_success | failed
```

---

## AI Director

Opt-in Claude/Opus agent that writes tailored per-variant prompts. Toggle (🎬) sits next to ✨ enrich on the Swap (Step 2), Image-tab, and Video-tab forms. Disabled when `ANTHROPIC_API_KEY` isn't set; UI greys out the checkbox + shows a tooltip.

### What it does

- **Swap** — ONE Claude Opus call before image gen. Sees every character image + every scene image with vision. Uses tool-use (`submit_swap_plan`) to return a complete plan: a tailored prompt per (character × scene × variant_index). Plan is cached as JSON on `Job.director_prompts_json` so retries / resumes don't re-bill. `runner._kick_char` reads the cache and assigns each `GeneratedImage.prompt` from `plan.lookup(char_id, scene_id)[variant_idx]`. Falls back to enrich → raw → `GENERATION_PROMPT` per slot if the plan is missing or fails.
- **Swap movement (Step 4)** — second Claude call with the scene image + every approved variant image + the user's per-scene movement text. Returns one cinematic shot prompt per scene; merged into `enriched_movement_prompts` so the existing per-scene resolver in `run_video_synthesis` transparently picks it up.
- **Image tab (freeform)** — single-char/single-scene shape — Director sees the reference image as both "scene" and "character" and writes ONE tailored prompt. Stored on `MediaGeneration.director_prompt`.
- **Video tab (freeform)** — single-scene movement — Director sees the start frame and writes one cinematic shot description. Stored on `MediaGeneration.director_prompt`.

### Architecture

```
clients/anthropic_client.py    — Lazy SDK wrapper. messages_with_tools(...) + extract_tool_call(...).
                                 Pillow-resizes images to max 1024 px long edge before base64.
                                 Wrapped in call_log.record(phase="director_swap"|"director_movement").
prompt_director.py             — Orchestrator. SwapDirectorPlan / MovementDirectorPlan Pydantic
                                 schemas. SWAP_DIRECTOR_TOOL + MOVEMENT_DIRECTOR_TOOL JSON schemas.
                                 Forces tool_choice so the agent MUST call the structured-output tool.
                                 ALL failures → returns None → caller falls back transparently.
```

System prompts instruct the agent to:
- Refer to characters by **visible features** ("the woman in the yellow sundress…"), NEVER by image index.
- Preserve every verbatim user constraint WORD-FOR-WORD (e.g. "exact same pose", hex codes, brand names).
- For swap-only intent: preserve scene composition / framing / camera angle EXACTLY.
- Vary the N variants per (char, scene) only with subtle lighting / expression / micro-framing — never identity or scene changes.

### Failure modes (all silent fallback — image gen never blocks)

| Trigger | Behavior |
|---|---|
| `ANTHROPIC_API_KEY` missing | `_client()` raises `ProviderNotConfigured` → `direct_*` returns None. UI toggle greyed out. |
| `anthropic` SDK not installed | Lazy import fails → `direct_*` returns None. Logged in calls.jsonl. |
| API timeout / 5xx | Caught → None. Logged. |
| Tool not called in response | `extract_tool_call` returns None. |
| Pydantic validation fails | Returns None with reason logged. |
| Plan missing some (char, scene) pairs | Per-pair fallback: pairs covered get tailored prompts; missing pairs fall back. |

### Cost tracking

`call_log._cost_usd` returns `settings.claude_opus_price_usd` ($0.05 default) when phase ∈ `{director_swap, director_movement}` and `ok=True`. Aggregated by existing `read_costs(job_id=...)`.

### Precedence

When `enrich_prompt=True` AND `use_director=True`: Director wins where it succeeds, enrich is the safety net. When Director returns None for a slot, that slot uses `enriched_image_prompt`; when enrich is also off, falls back to raw / `GENERATION_PROMPT`.

---

## Editor details

**Always-on audio-onset start trim (2026-06-11, Hugo's directive).** Every clip
entering ANY pipeline is first cut so it starts exactly when there is enough
AUDIO — `video_edit.trim_leading_silence` (silencedetect energy vs the flow's
`threshold_db`, `min_silence_secs=0.05`), UNCONDITIONALLY: the `enable_trim`
toggle governs interior pauses only. Applies at clip entry in single-clip
auto_edit, per clip in multi_auto_edit (before transcription, so timestamps
need no shifting), per scene in Step-6 compile, and per Kling scene clip in
Reengineer assemble (where the original-duration match became a CAP — finals
are never longer than the original scene, usually tighter). The marker is
audio ENERGY, deliberately NOT Whisper's first word (the old
`trim_to_first_word` recuts were removed from the flows; the utility remains).
No-audio clips pass through untouched; any trim failure falls back to the
untrimmed clip — the start trim never blocks a render.

### Single-clip auto-edit
`POST /api/editor/auto_edit` runs (in order; steps 1-5 opt-out via Form):
0. **Audio-onset start trim — ALWAYS** (see above)
1. Trim silences (`enable_trim` — interior pauses)
2. Voice swap via ElevenLabs STS (only if `voice_id` set)
3. Transcribe (Whisper, needed if captions OR WPM normalize is on)
4. **WPM normalize** (`enable_wpm_normalize`, default true; `target_wpm` default 190): compute `active-WPM` (= words / (span − sum_of_long_pauses>0.4s)), compute `speed_factor = target / current` clamped to [0.5, 2.0] with 3% dead zone, time-stretch via `atempo`, scale word timestamps in lockstep.
5. Render captions (`enable_captions`)

### Multi-clip auto-edit
`POST /api/editor/multi_auto_edit` accepts N video files + a script:
1. Save each clip
2. **Audio-onset start trim per clip — ALWAYS** (before transcription)
3. Transcribe each (parallel)
4. Fuzzy-match each clip's transcript to a position in the script (difflib via `match_clips_by_transcript`); reorder to script order
5. Per-clip WPM normalize (same logic as above, on each clip independently)
6. Concat in script order
7. Trim silences, voice swap, captions (same as single-clip from step 1 onward)

UI surfaces per-clip pacing decisions in a "Pace normalization" panel after rendering: `clip 3 · 245 WPM    ↑ 1.29× → 190 WPM` per clip.

### CapCut-style timeline
After any successful render, a "Trim & split" button opens a horizontal timeline below the result video. Segments are colored bars proportional to their played length. Drag handles trim each segment. Click "Split at playhead" to cut a segment in two. Per-segment ←/→ to reorder, ✕ to delete. "Apply timeline" POSTs to `/api/editor/timeline_render` which uses ffmpeg trim+concat in a single filter_complex.

### Endpoints

```
GET  /api/editor/templates       list of caption templates with metadata
POST /api/editor/trim_silences   silence-detect + cut
POST /api/editor/captions        transcribe + burn captions
POST /api/editor/auto_edit       full single-clip pipeline
POST /api/editor/multi_auto_edit full multi-clip pipeline
POST /api/editor/rerender        re-render captions on a cached result (no re-Whisper)
POST /api/editor/timeline_render apply a CapCut-style timeline
```

---

## Caption templates

In `video_edit.TEMPLATES`. Each is a `CaptionStyle` dataclass with font, size, colors, outline, shadow, margin, words-per-card, optional highlight color, optional all-caps. Two render engines: **ASS** (legacy ffmpeg subtitles filter, 19 templates) and **Remotion** (4 React-rendered animated templates — recommended for modern social reels). Available templates:

### Remotion engine (engine="remotion") — animated, CapCut/Submagic-grade

- **`submagic-pro`** (RECOMMENDED DEFAULT) — Montserrat 900 italic ALLCAPS, **22% active-word scale boost**, per-word spring entrance (160ms bounce), random per-card emphasis colors (6-palette deterministic hash by word), accent glow halo on active word only, 5.5% font-size outline (4px min), drop shadow.
- **`submagic-pop`** — Inter 900 italic, **20% active-word scale boost** (was 5% pre-upgrade), random keyword emphasis colors flashed when speaking, thick outline + drop shadow. Submagic's "mostly yellow + occasional accent" pattern.
- **`mrbeast-bold`** — Anton ALLCAPS, **28% keyword size jump** (was 8%), per-word entrance spring (snappier for keywords, gentle for fillers), double-layered drop shadow (flat + soft), 6% font-size outline.
- **`capcut-glow`** — Poppins 900, **18% active-word scale boost**, per-word entrance spring (was card-level only), 5% outline stroke (was missing — glow-only before), triple-layered text-shadow (cyan glow + soft drop + crisp stroke).

### ASS engine (engine="ass") — burned via ffmpeg subtitles filter

- **Submagic-style**: `popout-yellow`, `popout-white`, `popout-pink`, `popout-green` (Anton, all-caps, big outline)
- **Modern bold**: `modern-bold` (Poppins ExtraBold), `bold-shadow` (Montserrat Black)
- **Clean / soft**: `clean-shadow` (Helvetica, no outline + drop shadow), `rounded-soft` + `rounded-pop` (Arial Rounded MT Bold)
- **Platform-branded**: `instagram` + `instagram-pop` (Instagram Sans Bold), `tiktok-pop` + `tiktok-black` (TikTok Sans ExtraBold / Black)
- **Specialty**: `kinetic` (one word per card, Bebas Neue 160px), `typewriter` (Courier monospace boxed), `bottom-third` (broadcast lower-third), `submagic` (Montserrat Bold mixed-case)
- **Legacy**: `mrbeast`, `tiktok`, `karaoke`, `minimal`, `subtitle`

Fonts are auto-downloaded from Google Fonts on first use into `state/fonts/` (ASS engine). Locally-installed TTFs (Arial Rounded MT, Instagram Sans, TikTok Sans) are dropped directly into `state/fonts/` and resolved by `_ensure_font` which checks for local files BEFORE consulting the download URL dict. Remotion engine ships `@remotion/google-fonts` subpath modules for Inter, Montserrat, Anton, Bebas Neue, Poppins.

### Visual caption editor (✎ Edit captions)

After any successful caption render, click ✎ Edit captions on the result panel:

- **Horizontal scrubbing timeline** at the top — each caption card rendered as an amber rectangle on a track proportional to its `[start, end]` in seconds. A rose-colored playhead line moves across them.
- **Auto-follow during playback** — `window.RemotionPreview.onFrameUpdate(...)` callback updates `playheadSecs` ~30 times/sec so the playhead tracks the live Remotion Player frame stream.
- **Drag-to-scrub** — grab the rose handle; pauses playback, follows the cursor in real time, seeks the Remotion Player to that position via `seekToSecs(...)`.
- **Card-edge drag** (1.5 px-wide handles, fade in on hover) → retime first/last word of the card. Clamped between neighbor cards' edges so cards never overlap.
- **Card-body drag** (≥4 px delta) → shift every word in the card by the same time delta. Click-without-drag falls back to seeking to the card's start.
- **Active-card highlight** — the card whose `[start, end]` contains the playhead gets a brighter fill + ring on the timeline, AND its corresponding edit row in the Cards-view list below gets an amber border. Keeps the editor and timeline visually in sync.
- **Cards-view editor** — words grouped by `words_per_card`, each card shows start/end timecodes + inline-editable word inputs sized to text length.
- **Per-word view editor** — one row per word with numeric start/end inputs (0.05s steps), text, plus split (halve duration + insert placeholder), merge-left, and delete actions.
- **Live preview re-mount** — `editor.editedWords` is watched (180ms debounce) and re-mounts the Remotion preview so changes show in the preview within a fraction of a second.
- **Save = re-render** — clicking "▶ Apply changes" posts `words_json=...` to `POST /api/editor/rerender`. The server persists the edits to `words.json` (with a `.original.json` backup on first edit) so all future rerenders inherit them.

---

## API surface (summary)

```
GET    /                                       → web/index.html
GET    /app.js                                 → web/app.js
GET    /files/output/<rel>                     → generated outputs
GET    /files/input/scenes/<rel>               → uploaded scene images
GET    /files/characters/<rel>                 → uploaded character images

POST   /api/scenes                             multipart upload
GET    /api/scenes/{scene_id}                  metadata

POST   /api/characters                         multipart upload
GET    /api/characters                         list
PATCH  /api/characters/{char_id}               rename
DELETE /api/characters/{char_id}               delete
DELETE /api/characters/{char_id}/images/{image_id}
GET    /api/characters/{char_id}/gallery       all appearances across jobs

POST   /api/projects                           create
GET    /api/projects                           list
PATCH  /api/projects/{project_id}              body: name? / character_ids? / default_prompt?
DELETE /api/projects/{project_id}              CASCADE

POST   /api/jobs                               body: scene_id OR scene_ids, character_ids,
                                                images_per_character, prompt?, image_model?,
                                                enrich_prompt?, use_director?, ...
GET    /api/jobs                               list (?summary=1 for compact)
GET    /api/jobs/{job_id}                      full state (exposes use_director +
                                                director_plan_summary = {present, intent, n_chars,
                                                n_scenes, n_prompts})
PATCH  /api/jobs/{job_id}                      title / project_id
DELETE /api/jobs/{job_id}                      hard delete
POST   /api/jobs/{job_id}/approve              body: char_id, action, variant_id?
                                                (action=approve TOGGLES variant_id in
                                                approved_variant_ids list; allows one approval
                                                per scene per character)
POST   /api/jobs/{job_id}/approve_all          bulk: picks the first ready variant per
                                                (character, scene) where none is approved yet
POST   /api/jobs/{job_id}/edit_variant         body: char_id, variant_id, prompt
POST   /api/jobs/{job_id}/movement             body: prompt? (legacy single) OR
                                                movement_prompts: {scene_id: prompt} OR
                                                movement_prompts_by_variant: {variant_id: prompt} +
                                                durations_by_variant: {variant_id: secs}, video_model?,
                                                videos_per_character
POST   /api/jobs/{job_id}/duplicate            new job with same scenes + chars
POST   /api/jobs/{job_id}/compact              strip non-approved files
POST   /api/jobs/{job_id}/retry_video          retry one failed video
POST   /api/jobs/{job_id}/unlock_movement      clear movement_prompts + videos so user can re-prompt
PATCH  /api/jobs/{job_id}/characters/{char_id}/source_image   body: image_id
POST   /api/jobs/{job_id}/characters/{char_id}/variants/{variant_id}/retry   per-variant retry

WS     /ws/jobs/{job_id}                       live events

GET    /api/generations/models                 model registry
POST   /api/generations                        multipart kind + model + prompt + files
GET    /api/generations?kind=image             list
GET    /api/generations/{gen_id}               single
DELETE /api/generations/{gen_id}
POST   /api/generations/{gen_id}/retry

GET    /api/swap/defaults?project_id=...       {prompt, global_prompt, project_prompt, ...}
GET    /api/heygen/avatars, /api/heygen/voices, /api/elevenlabs/voices

POST   /api/broll/generate                     full pipeline
GET    /api/broll, /api/broll/{id}, DELETE /api/broll/{id}
POST   /api/broll/{id}/regenerate_clip
POST   /api/broll/{id}/finalize

POST   /api/editor/auto_edit, /multi_auto_edit, /trim_silences, /captions, /rerender,
       /timeline_render
POST   /api/editor/rerender                    body: edit_id, template, overrides?,
                                                trim_start_secs?, trim_end_secs?, words_json?
                                                (words_json = JSON list of {text, start, end} from
                                                the visual caption editor — persists back to
                                                words.json with words.original.json backup so all
                                                future rerenders inherit the edits)
GET    /api/editor/templates                   (each row carries `engine: 'ass' | 'remotion'` +
                                                `composition_id` for remotion entries:
                                                SubmagicPro / SubmagicPop / MrBeastBold / CapCutGlow)

POST   /api/jobs/{job_id}/compile_videos       Step 6: per-character compile. Body:
                                                template? overrides? enable_trim? enable_captions?
                                                enable_wpm_normalize? target_wpm? voice_override?
                                                char_ids? (filter — used by retry-one).
                                                Schedules runner_compile.compile_job_videos via
                                                BackgroundTasks; chars flip to compile_status=
                                                "compiling" immediately. WS emits char.compile_started
                                                / char.compile_done / char.compile_failed events.

PATCH  /api/characters/{char_id}                body: name? voice_id? voice_provider? — all optional.
                                                voice_id="" clears the preset.

POST   /api/generations                        multipart form fields include enrich_prompt? +
                                                use_director? (Image + Video kinds only — Avatar /
                                                Audio scripts are literal and would corrupt under
                                                enrichment/Director)

GET    /api/health                             {ok, version, openai_key, anthropic_key, xai_key,
                                                gemini_key, kling_key, ..., remotion_available}
```

---

## Working API shapes (preserved from prior debugging)

### OpenAI `images.edit`
- Two-ref (generate): `client.images.edit(image=[scene, char], prompt=..., model="gpt-image-2", size="1024x1792", n=1)`
- One-ref (edit): `client.images.edit(image=variant, prompt=custom, ...)`
- Multi-image must be passed as a list of open file handles (`ExitStack`).
- 403 = OpenAI org isn't verified for `gpt-image-2`.

### Grok Imagine
```
POST https://api.x.ai/v1/videos/generations
GET  https://api.x.ai/v1/videos/{job_id}

Submit body: {"model": "grok-imagine-video", "prompt": ..., "duration": <int 5-15>,
              "aspect_ratio": "9:16" | "1:1" | "16:9", "resolution": "720p",
              "image": {"url": "data:<mime>;base64,<b64>"}}
Submit response: {"request_id": "<job_id>", ...}
Status terminal: {done, failed, error, cancelled}; success: {done}.
Grok image model: `grok-imagine-image` (xAI deprecated `grok-2-image-1212` on 2026-02-24).
```

### Whisper word timestamps quirk
`whisper-1` with `timestamp_granularities=["word"]` returns word durations that are mostly INTERPOLATED inside each segment — `word[i].end == word[i+1].start` for most adjacent words. Real silences only show up as gaps > ~0.4s. The WPM helpers in `video_edit.py` (`compute_wpm`) account for this by computing `active_secs = span − sum(long_gaps)` instead of summing per-word durations.

---

## Cost & safety notes

- Server binds to `127.0.0.1` only.
- Static-file serving uses **three narrow mounts**: `/files/output`, `/files/input/scenes`, `/files/characters`. `state/`, `.env`, source, and call logs are NOT reachable via HTTP.
- Uploads streamed in chunks, capped by `MAX_UPLOAD_BYTES` (default 25 MB).
- Image gen gated by `IMAGE_CONCURRENCY` (default 2). With multi-scene jobs: 5 chars × 3 scenes × 4 variants = 60 calls.
- Video gen fires in parallel (Grok queues server-side).
- Approvals + edits locked after movement prompt submitted.
- All API calls logged to `state/calls.jsonl`.

---

## Known issues / pending

- **Step 2 source-image swap shows "Start the job first, then swap the reference image" when no job is loaded yet.** Picker is open for "preview" but PATCH endpoint requires an existing JobCharacter. Fix path: either disable the picker when `!job`, or implement a client-side override map that gets sent to `POST /api/jobs` at creation time.
- Head-of-chain B-roll regen: rejecting the FIRST clip in a scene group doesn't auto-rechain downstream clips (they still point at the old first-clip's last frame). UI hint to manually reject downstream too is a follow-up.
- Edit-chain visualization beyond the small "↳ edit" badge.
- SQLite-backed state is opt-in via `USE_SQLITE_STATE=1` but full migration tooling isn't shipped yet.
- **Persistent data location**: by default `state/`, `characters/`, `input/scenes/`, `output/<job_id>/`, and `.env` live in the active worktree — meaning `git worktree remove` wipes them (no Trash). Set `CHARACTERS_DIR` / `INPUT_DIR` / `OUTPUT_DIR` / `STATE_DIR` in `.env` (see Quickstart → "Shared data store") to point at `~/character-swap-data/` so all worktrees share one library + DB and removal is safe.
- Mobile / iPad UI: not optimized. Sidebar layout assumes ≥md breakpoint.

---

## Pending / nice-to-haves

- Cross-clip pause-length normalization in Editor multi-clip (currently each clip's pause structure is preserved; could optionally collapse all pauses to a target length).
- Visual preview of WPM-stretch decisions BEFORE rendering (the data is computed instantly from the transcript).
- Drag-to-reorder in CapCut timeline (current UI has ← → buttons per segment).
- Node-based canvas (Higgsfield/Krea style) for chaining tab outputs into pipelines. Parked.
- `.otio` export for finishing edits in DaVinci Resolve / CapCut Desktop.
