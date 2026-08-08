# Character Swap Studio — DEV copy

> **This is the live development copy** at `~/character-swap-workflow/`. May change at any time and may temporarily be broken. For day-to-day use, prefer the frozen stable copy at `~/character-swap-stable/` (see its README).

## Working with Hugo — ALWAYS ASK WHEN UNSURE (project standard, 2026-06-11)

Hugo's explicit standing instruction: **whenever you are uncertain about his
GOAL or how he wants something to behave, ask him questions (AskUserQuestion)
BEFORE building — keep asking until you are completely synced.** He would
rather answer 3 quick questions than get a feature that misses the intent.
Concretely:

- Ambiguous scope, multiple reasonable interpretations, or a UX choice that
  isn't obvious → ask, with a recommended option first.
- Quality/cost/speed tradeoffs (image engines, QC retries, model choices) →
  ask; his standing priorities are reliability > quality > speed > cost, but
  confirm when a change shifts the balance.
- NEVER change default behavior of an existing flow without asking — new
  capabilities should be opt-in unless he says otherwise.
- When he reports a broken/wrong output: ask (or check the data) for WHICH
  run/scene/character before guessing, and confirm the expected result.
- Don't ask about things the code, his data, or this file already answers —
  questions are for his INTENT, not for facts you can look up.



## What this project does

Local web app (FastAPI + Alpine.js + Tailwind) for AI media generation. **Four top-level tabs (de-scoped 2026-07-02, Hugo's directive — only the Swap/Reengineer/Editor flows remain):**

- **Swap** (default tab) — "animera dina referensbilder": upload one or more reference images → each becomes a scene every selected character is swapped into and animated (`POST /api/reengineer/from_images`). Reengineer-backed — runs render as Reengineer run-cards with the same approval gate / edit mode / assemble. Optional per-scene 🎯 end frame + `📌 ingen swap` direct rows (see the Reengineer paragraphs below).
- **🎬 Animate** — part of the same Swap flow: Step A drops already-finished images (no image generation) straight into the shared Steps 4–6 machinery — per-image movement prompts → one video each → stitched into one final reel. Shares `this.job` + all Step 4–6 methods with the swap machinery (job pre-approved at creation).
- **♻️ Reengineer** — upload a finished video → scenes detected + transcribed → every character swapped into each scene → clips reanimated → per-character finals assembled. Full details in the Reengineer paragraphs below.
- **Editor** — upload a video and run any combination of: (a) auto-trim silent gaps via ffmpeg silencedetect + concat, (b) **per-clip WPM normalization** (time-stretch each clip independently so the speaker hits target_wpm, pitch-preserving via ffmpeg `atempo`), (c) voice swap via ElevenLabs STS, (d) burn in word-level captions transcribed by OpenAI Whisper. **Captions ship in two engines**: (1) the legacy ASS path with 19 templates (popout-yellow family, submagic, modern-bold, rounded-soft/pop, instagram/-pop, tiktok-pop/-black, mrbeast, tiktok, karaoke, minimal, subtitle, kinetic, clean-shadow, bold-shadow, typewriter, bottom-third) burned in via `ffmpeg subtitles=` filter, and (2) the **Remotion path** with 4 React-rendered animated templates — `submagic-pro` (recommended default: Montserrat 900 italic, 22% active-word scale boost, per-word spring entrance, random per-card emphasis colors, accent glow halo), `submagic-pop` (Inter 900 italic, 20% active scale, random keyword highlights), `mrbeast-bold` (Anton ALLCAPS with 28% keyword size jump + per-word spring), `capcut-glow` (Poppins 900 cyan-glow + 18% active scale + outline stroke). Remotion templates require Node ≥ 18 and a one-time `character-swap remotion-install` (installs `remotion/node_modules/` + builds `web/static/remotion-preview.js` via esbuild so the in-browser preview uses `@remotion/player` — preview matches render exactly). Server-side render: `npx remotion render <CompositionId> <out.mp4> --props=props.json`, wrapped in `src/character_swap/remotion_render.py` with a SHA-256 cache. **Multi-clip mode**: upload N clips + a script, the system transcribes each, fuzzy-matches them to script positions, orders them, normalizes WPM per clip, concats. Plus a **CapCut-style timeline editor** for trim/split/segment-reorder on any finished result. **Visual caption editor** (✎ Edit captions button on any finished caption render): horizontal scrubbing timeline with draggable card rectangles + a rose-colored playhead that auto-follows preview playback and is grab-to-scrub; drag a card's left/right edge to retime the first/last word, drag the card body to shift the whole block; per-card text edit (cards-view) + per-word text+timing edit (per-word view) with split/merge/delete; live Remotion preview re-mounts on edit (180ms debounced) so changes show immediately. Endpoints under `/api/editor/*`. Outputs live under `output/editor/<edit_id>/`.

**What the de-scope deleted (2026-07-02):** the free-form Image/Video/Audio/Avatar tabs, the B-roll tab, and the 💬 Chat tab — with their backends: `runner_broll.py`, `broll.py`, `chat.py`, `clients/heygen.py`, the old Telegram media-notification client, `runner_drive_watcher.py` (the Higgsfield Drive→Editor inbox watcher), the `/api/broll/*`, `/api/chats*`, `/api/heygen/*`, `/api/higgsfield/inbox*` (+ its drive/bootstrap) endpoints, `POST /api/generations`, `POST /api/generations/{id}/retry`, and the HeyGen photo-avatar 🎙 button. `HEYGEN_API_KEY` and `HIGGSFIELD_DRIVE_*` are gone from config. Telegram final delivery was reintroduced separately on 2026-07-27 (see below). KEPT: `GET /api/generations` (Editor saved reels + legacy rows), `GET`/`DELETE /api/generations/{id}`, `GET /api/generations/models`, `/api/editor/*` incl. the legacy `drive_export`, `/api/elevenlabs/voices`, `run_full_pipeline`. `runner_media.py` survives as the model registry only (see Module map). `GenKind.AVATAR`/`AUDIO` + `MediaGeneration.avatar_id`/`voice_id`/`voice_provider` remain ONLY so legacy rows deserialize; `ChatSession` + the chats table/methods are deleted. The Gemini-path Veo 3 / Veo 3 Fast slugs (`veo`/`veo-3-fast`) were also dropped from the video registry + pipeline dispatch — `google_genai.submit_veo` was an unimplemented stub, so every selection failed (`veo-3.1-fast` on fal stays). `app.js` `init()` validates the stored `active_tab` against the surviving tabs so users who last sat on a removed tab don't land on a blank page. Locked models still show "(locked)" in the surviving Step-4/Reengineer pickers when their API key is missing.

**Telegram final delivery (2026-07-27, Hugo's directive; replaces Drive in the final-video UI).** Two bot identities keep the destinations separate. `TELEGRAM_CHARACTER_BOT_TOKEN` sends every Swap/Animate/Reengineer character final and its 🔁 repurpose copy to that character's own channel; the channel's `@username` or numeric `-100…` id is stored on `CharacterAsset.telegram_chat_id` from the character library. `TELEGRAM_EDITOR_BOT_TOKEN` + `TELEGRAM_EDITOR_CHAT_ID` send standalone Editor Single/Multi-clip finals and repurpose copies to one shared Editor destination. Once every approved Swap clip succeeds, `auto_finalize.finalize_swap_job` automatically runs Step-6 compile and sends every character final; Reengineer assemble does the equivalent, and Editor originals/repurposes send on completion. Manual ➤/↻ Telegram buttons remain on all final cards. Receipts persist on `JobCharacter.telegram_sends[variant]`, `state[finals|repurposed][cid]["telegram"]`, and `editor_meta["telegram"][slot]`. Delivery snapshots the mutable final and uploads it with `sendDocument` as `application/octet-stream`, preserving the exact source bytes — Telegram delivery NEVER re-encodes or compresses a final. The official cloud Bot API's 50 MB limit is enforced as a loud failure; `TELEGRAM_API_BASE` must point to a local Bot API server (2 GB upload limit) for larger originals. Failures never invalidate a successfully rendered local final. Endpoints: `/api/jobs/{job_id}/characters/{char_id}/telegram_send`, `/api/reengineer/{re_id}/chars/{char_id}/telegram_send`, their `/telegram_send_all` variants, and `/api/editor/{edit_id}/telegram_send`. The old Drive endpoints remain backend-only for compatibility/Resolve exports, but finished-video UI routes through Telegram. **Delivery is NEVER gated on a build guard (2026-08-03, re_4906fac466).** `_do_repurpose` used to call the auto-send from INSIDE `runner_reengineer._REPURPOSING`, so 7 × ~34 MB of sequential uploads (600 s timeout × 3 attempts each) held the "a build is running" flag for the whole delivery and every manual ➤ click was refused with the false "Bygget pågår — vänta tills finalen är klar". Both runners now send only AFTER releasing their guard (`assemble()` always did — copy it, never move a send inside). Overlap between the automatic and a manual send is handled per TARGET by `telegram_delivery.sending(run_id, target_id, variant)`: an in-process lock keyed (run, character/slot, variant) that refuses ONLY the video being uploaded right now (409 "skickas redan…"), leaves its run-mates sendable, and makes the auto-send SKIP a target a manual click owns instead of posting it twice. Every character path (`_telegram_character_file`, both `telegram_send_all`s) and the Editor slot claim it. NOTE: `_send_reengineer_bucket` persists receipts only AFTER the whole bucket — a restart mid-delivery loses the receipts for videos already posted. Locked by `test_auto_finalize.py` + `test_telegram_delivery.py` + `test_telegram_send_lock.py`.

**Telegram metadata scrub (2026-08-05, Hugo's directive; supersedes the exact-source-byte wording above).** Immediately before EVERY automatic or manual Telegram upload, `telegram_delivery.send_file_core` snapshots the mutable local final and losslessly remuxes that snapshot with ffmpeg stream-copy (`-c copy`). Container tags, per-stream tags, chapters, cover art, timecode/data tracks and attachments are removed; codec-internal metadata units are also removed from SDR H.264 (SEI type 6), HEVC (SEI 39/40), and AV1 (metadata OBU 5). Only real video/audio/subtitle streams plus structurally required decode/timing/rotation fields remain. The codec-metadata bytes intentionally change, but every decoded video frame is byte-identical and audio packets remain byte-identical — ZERO generation loss. HDR is refused loudly instead of stripping presentation-critical SEI/OBUs and changing its look; an unknown codec is likewise refused rather than silently shipping a partial scrub. The local final remains untouched. Delivery is fail-closed: if probe/scrub/remux fails, nothing is uploaded and the real error is surfaced. Both temporary source and scrubbed copies are always deleted. Locked by `test_telegram_delivery.py`, including decoded-frame SHA-256 equality, audio packet equality, x264-SEI absence, HDR refusal, and unknown-codec refusal.

**Automatic black-bar removal in every final build (2026-07-24, Hugo's directive).** Every clip entering a final is scanned with ffmpeg `cropdetect` (union content box over up to the first 60 s — a region only counts as bar if black in EVERY frame, so fade-from-black intros never shrink it) and, when bars are found, minimally crop-zoomed to the target aspect: the LARGEST 9:16 window inside the real content box, centered, then scale-to-COVER + center-crop onto the 1080×1920 canvas (never `pad`, so even-rounding can't reintroduce 1-px bars). This kills BOTH bar sources: bars baked into the clip by the video model, AND the pipeline's own `scale+pad` letterboxing of slightly-off-aspect clips (which now get a tiny crop instead of pad). **Cap = 5% per axis** (`BLACKBAR_MAX_CROP_FRAC`): if eliminating the bars would crop more of the frame than that, the clip keeps the legacy padded look (protects against cropdetect misreading dark scenes and against wildly off-aspect imports); `BLACKBAR_FIX=0` disables entirely. Detection NEVER blocks a build — any probe/detect failure → legacy path. Wired at the three normalize-to-canvas choke points: `video_edit.assemble_clips` (Step-6 compile + Reengineer/Swap assemble + 🔁 repurpose), `video_edit.concat_videos` (multi-clip Editor + legacy fallback chain, incl. its single-input branch), and a conditional Step 0b `crop_video` encode shared by ALL single-clip Editor flows via `api._maybe_crop_black_bars` — `auto_edit` + the one-shot `trim_silences` / `captions` endpoints (they never scale, so baked-in bars needed their own hop — extra encode only when bars are actually found; a <6px-per-axis dead-band skips cropdetect noise on clean clips). Helpers: `detect_content_box` / `compute_bar_crop` (pure geometry) / `bar_crop_for_clip` / `_canvas_vf`. Locked by `test_black_bars.py`.

**Characters are 1-to-many with images.** Each `CharacterAsset` has a list of `CharacterImage`s plus a `primary_image_id` pointing at the "main" thumbnail. Uploading via the modal asks whether the new image(s) belong to an existing character or create a new one. Same hash-named file is reused for duplicate uploads.

**Right-side character library** (toggle via the 📚 button in the header; open/closed persisted in `localStorage.char_lib_open`): per-character image gallery, drag-to-add into Step 2.

**Per-job source-image swap** (Step 2): if a character has 2+ reference images, the "N imgs ↕" badge on its card is clickable → opens a popover with all the character's gallery images → click any to swap it as the source for THIS job. **Works both before AND after the job is created** — before-job picks are staged client-side in `charSourceOverrides[charId]` and sent as `character_source_image_ids` on `POST /api/jobs`; after-job swaps go through `PATCH /api/jobs/{job_id}/characters/{char_id}/source_image`. Library primary stays unchanged. Existing variants keep their reference to the old source; only new variants from a regenerate use the new one.

**"Använd bild N för alla" + reorderable gallery (2026-08-08, Hugo's directive).** Picking each character's reference image one ↕ popover at a time doesn't scale to an 11-character run, so the Swap-from-images and Reengineer forms have a dropdown next to "Karaktärer" that sets EVERY selected character's source image by POSITION — "Bild 2" = the second image in that character's library gallery. It writes the same `sourceOverrides` map the per-character ↕ picker uses, so Hugo's flow works unchanged: **bulk first, then override individual characters for that run** (an individual pick wins for that character; a second bulk pick re-covers everyone). A "★ Primär (återställ)" row drops the picks for the selection. **A character with FEWER images than N takes its LAST image** (Hugo's call — never skipped), and the note beside the dropdown says how many were clamped so the substitution is never silent; the dropdown itself resets to its placeholder after applying (it describes an ACTION, not state) and the note is dropped when the selection changes, since it would otherwise claim characters it never touched. Positions are user-controlled: every library tile shows its number and, on hover, ◀ ⠿ ▶ — arrows move one step, the ⠿ handle drags anywhere. The drag carries its OWN `text/x-charswap-img-order` dataTransfer type and only becomes droppable on the SAME character (`onImageReorderOver` is what calls preventDefault), because those tiles already carry the drag-into-a-job gesture. **Order and the ★ primary are deliberately INDEPENDENT** (Hugo's call): reordering changes what "bild 2" means, never which image a job swaps from — the ☆ control stays, and a character whose primary was never set explicitly gets it pinned before the reorder can imply a different one. Backend: `PATCH /api/characters/{id}` takes `image_order`, which must be a PERMUTATION of the character's image ids (a partial list would silently drop images → 400 naming them; unknown id → 404; duplicates → 400), and persists via the `character_images.position` column that `upsert_character` already wrote. Locked by `test_character_image_order.py` + `test_bulk_source_image.py` (+ its `tests/js/bulk_source_image.mjs` behavioral harness).

**OS-level notifications + audio chime** for milestone events. Browser Notification API + Web Audio synthesized 2-tone bell (no asset file). Fires at two levels: (a) **approval gates** — swap char `awaiting_approval` ("Variant ready — approve"), Reengineer image gate + clip gate; (b) **batch completions** — swap all-chars-terminal, swap Step-6 per-character compile / repurpose done, Reengineer done/failed + re-animation klar, every Editor render (captions / rerender / timeline / multi-clip auto-edit / auto-edit pipeline / trim) done, Editor 🔁 repurpose done, Resolve-pipeline done, Drive export authorized/uploaded. Permission prompted once at `init()`; user toggles in header (🔔 OS popup + 🔊 chime), persisted to localStorage as `notif.os` / `notif.sound`. Greyed when browser permission is `denied`. Approval-pitch chime is higher (880→1320 Hz), done-pitch is softer (660→990 Hz). Tag-based dedup so same milestone doesn't fire twice. Single `notifyMilestone(title, body, opts)` fan-out point in `app.js`; in-app toast remains via existing `notify('info', ...)` channel.

The Swap flow (6 steps): persistent left sidebar of past jobs + main panel:

1. **Scene** — upload **one or more** scene images. Supports drop, click, and **Cmd+V paste** (multiple at once). Each scene becomes a separate reference background; the character gets variants for every scene. Per-tile ✕ to remove. Counter shows "(N scenes — each character gets variants for every scene)".
2. **Character images** — pick one or more from a persistent library (upload new ones inline). Rename via inline ✎ icon. **Preset voice (🎤)** dropdown on each library card sets an ElevenLabs voice that auto-applies in Step 6 compile + Editor tab. Choose **N images per character** (1–4, default 1). Optionally edit the **Generation prompt** (per job override) or save it as the project's default via "★ save as project default". Two opt-in prompt-quality toggles: **✨ enrich** (cheap, gpt-4o single-shot expansion of the user's prompt) and **🎬 AI Director** (Claude Opus agent with vision + tool-use; writes a tailored prompt per (character × scene × variant) — see "AI Director" section below).
3. **Generate** — GPT Image 2 (or Nano Banana / Nano Banana Pro / Grok Image, picked in Step 2) generates `images_per_character × N_scenes` variant images per character. When multiple scenes exist, variants render under per-scene subgroup headers inside each character section. **Multi-variant approval** — user picks ONE variant per (character × scene) by clicking the ✓ on each (re-click un-approves). **"✓ Approve all" button** auto-picks the first ready variant per (char, scene) for all characters at once. Variants can be **edited with a custom prompt** to spawn a new variant for comparison. **Per-variant retry** (↻) re-runs just one failed slot. Per-variant download with friendly filename.
4. **Movement prompt** — **per-IMAGE rows** (one per approved image): each approved image gets its OWN motion prompt AND its own clip duration (the Higgsfield "per-slot" model), so every video can be completely different. Thumbnail + textarea + per-image duration picker per row, plus an "⤓ apply image 1 to all" convenience. **Video provider picker** (a job-wide DEFAULT) lets you pick any of: Grok Imagine, Veo 3.1 Fast (fal — the Gemini-path Veo 3 / Veo 3 Fast slugs were removed 2026-07-02, their submit path was an unimplemented stub), Kling 2.0/2.1/2.5/2.6/3.0 + Pro/Master variants, Runway Gen-4/Gen-3, Luma Ray-2, Pika 2.2, Hailuo 01/02, Sora 2, Wan 2.1/2.2, Seedance, Higgsfield variants. **Per-clip model override (2026-06-18):** each scene row also has a small **Model** dropdown defaulting to "Samma som jobbet" — pick a different provider for one clip and that scene's duration options + generation follow it. Opt-in: empty → the job default; persisted as `Job.video_models_by_scene` (scene_id → slug), resolved per-clip in `runner._eff_video_model` at submit/salvage-repoll/resume + end-frame gating (a scene on a model NOT in `runner_media.END_FRAME_VIDEO_MODELS` — currently `kling-v3` / `seedance-2.0` / `veo-3.1-fast` — ignores its end pose; `veo-3.1-fast` interpolates via fal's separate `first-last-frame-to-video` endpoint, the others via an `end_image_url` arg). `POST /movement` validates every chosen provider's key upfront. **Submit kicks off all approved images × M videos in parallel**, each scene using its effective provider. Backend: `POST /movement` accepts `movement_prompts_by_variant` (variant_id → prompt) + `durations_by_variant` (variant_id → secs); the runner resolves per-image override → per-scene prompt → fallback, and a per-scene `movement_prompts` is derived from the per-image dict for back-compat + the Step 6 compile. Per-image prompts are used verbatim (AI Director / enrich are skipped when they're set). The older per-scene path still works for jobs that send `movement_prompts`.
5. **Videos** — chosen provider animates each approved image M times. Live progress + per-video download with friendly filename.
6. **Compile final videos** — appears once every approved character has ≥1 DONE video. One click → for each character, concatenate that character's per-scene videos (in `scene_ids` order, picking the first DONE take per scene) into ONE final MP4 by running through the Editor pipeline (silence trim → voice swap → Whisper transcribe → WPM normalize → caption burn-in). All M characters compile **in parallel** using shared editor settings (template, target WPM, opt-out toggles). Each character's library-set preset voice auto-applies via ElevenLabs voice-changer; a batch-wide `voice_override` overrides all of them at once. Per-character cards show live `compiling → done / failed` status with preview + download. Failed compiles offer a per-character ↻ retry. Endpoint: `POST /api/jobs/{job_id}/compile_videos`; runner: [src/character_swap/runner_compile.py](src/character_swap/runner_compile.py). Output: `output/<job_id>/compiled/<char_id>.mp4` (plus full editor edit_id under `output/editor/<edit_id>/` so the compile result is also re-renderable from the Editor tab).

**Sidebar:** jobs grouped by **project** (collapsible). "+ New project" → modal. "+" on a project header pre-selects it for the next job. "⇄" icon moves a job between projects. **Cross-kind "Recent media" thumbnail strip at the bottom** shows the 50 newest finished items from the surviving kinds — Reengineer finals + saved Editor reels (incl. their 🔁 repurpose copies) — click a thumbnail to jump to its tab and scroll to the card.

**Per-project default_prompt** (new). Each project can have its own default Swap generation prompt. Set via "★ save as project default" in Step 2, or via `PATCH /api/projects/{id}` with `{default_prompt: "..."}`. New jobs in that project inherit it; jobs without a project fall back to `pipeline.GENERATION_PROMPT`. UI in Step 2 shows a green "● using project default" indicator when active.

**Persistent cross-tab status toast** (bottom-right): aggregates every in-flight or approval-waiting Reengineer/Swap run into one always-visible card (live image progress during the swap phase, scene count otherwise). Click to jump to the tab. Auto-hides when nothing is in flight. Powered by the `activeJobs` computed getter in `app.js` (reengineer-history-only since the de-scope — the freeform histories it used to aggregate are gone).

**Friendly download names.** Every download (variant/video cards, Editor results) uses a slug derived from the prompt + ISO date: `swollen-ankles-2026-05-15.mp4`. Helper: `friendlyName(g, ext?)` in `app.js`.

**Renames are everywhere:** characters in library (retroactive — propagates to all past jobs' snapshot names), job titles (inline above step 1), and download filenames.

**Dark mode is forced** (no toggle). Light mode classes still in DOM but never applied.

Quality is double-gated: (1) automatic vision-QC — every generated swap IMAGE is inspected by a Claude vision call (swap_qc.py — judge: Sonnet 4.6 by default since 2026-06-11, env `SWAP_QC_MODEL`). **CATASTROPHE-ONLY since 2026-06-30 (Hugo's directive — "QC på bilder är alldeles för hård").** The judge now FAILS only four unusable-image classes — WRONG PERSON (identity didn't take / original survived / blend / third face), MISSING/EXTRA PEOPLE, BROKEN IMAGE (black/blank/censored/corrupt/unmodified scene), SEVERE ARTIFACTS (grossly deformed face/hands) — and is told to PASS everything else: framing/zoom/crop/subject-scale, headroom, props/held-objects/action/prop-count, gaze/gesture, outfit, background/symbols, cutout/edge/lighting/style drift. This replaced the old strict judge whose dominant failure was framing: of 131 preserved image rejects, 79 (60%) were WRONG FRAMING/ZOOM head-ruler false-positives (e.g. j_b58d0c0916 — a fine chest-up swap re-rolled into an equivalent frame; verified 8/8 such rejects PASS under the new judge while unmodified-scene → WRONG PERSON and a black frame → BROKEN IMAGE still fail). The retired strict checks (head-ruler, headroom, prop-count, gaze, outfit, background-symbol) are gone; context flags + USER INTENT + the BACKGROUND image are still PASSED to the judge but are informational-only now. Locked by `test_swap_qc.py` (`test_qc_prompt_loosened_to_catastrophe_only` / `test_qc_no_longer_fails_framing_or_background` / `test_qc_prompt_covers_catastrophe_classes`). Image QC is auto-regenerated on failure (first retry = minimal-change REPAIR of the failed image, then fresh re-roll + hint; SWAP_QC=0 disables, or 🚫 skip-QC per run), and every generated video CLIP is checked (video_qc.py: Whisper transcript vs expected dialogue — catches garbled TTS like 'baking goda' — + frame-sampled anatomy check; 1 retry, VIDEO_QC=0 disables). **Video QC is independently tunable (Hugo 2026-07-03, currently set FLAG-ONLY/speech-only in the shared `.env`):** `VIDEO_QC_VISUAL=0` runs ONLY the dialogue check (skips the anatomy vision call — gated in `inspect_clip`); `VIDEO_QC_MAX_RETRIES=0` makes it FLAG-ONLY — a mismatched clip is KEPT and marked `qc_status="failed"` (⚠) but never auto-re-rendered (no cost, no qcreject snapshot, the take IS the clip); `VIDEO_QC_SPEECH_THRESHOLD` (0.7 default → 0.35 in Hugo's env) is the min word-similarity to PASS, lowered so only clips saying something *completely* different get flagged. Locked by `test_video_qc.py` (`test_video_qc_flag_only_no_retry` / `test_inspect_clip_visual_disabled_runs_speech_only`); (2) human approval before any video is kicked off (video is the expensive step). QC never blocks: unavailable → skipped; exhausted retries keep the last output with a ⚠ qc_status chip. **Every QC-rejected take is now PRESERVED (Hugo 2026-06-20).** Before a retry overwrites the slot's file, the rejected image/clip is snapshotted to a `<stem>.qcrejectN.png|.mp4` sidecar and recorded on `GeneratedImage.qc_rejects` / `VideoVariant.qc_rejects` (a `QCReject` = path/reason/attempt/kind). They are serialized by `api._qc_rejects_dicts` (drops files that went missing) and rendered inline BY DEFAULT in the Swap/Reengineer approval strip — dimmed red thumbs for images, small players for clips, tooltip = the QC reason. Numbered by cumulative count so in-place regeneration (`retry_single_variant` / ✎↻ / 🪄, which reuse the variant_id) accumulates rather than clobbers; repair-mode reuses the snapshot as its edit input; the final exhausted take stays at `variant.path` (not duplicated into qc_rejects). Locked by `test_swap_qc.py` / `test_video_qc.py`. QC + retries run OUTSIDE the image-gen semaphore (2026-06-11) so a judging/retrying slot never starves the generation lanes; the semaphore is sized per provider (`IMAGE_CONCURRENCY_FAL=8` / `_OPENAI=4` / `_GEMINI=2`, fallback `IMAGE_CONCURRENCY=2`).

**GPT Image moderation = `low`, always (2026-06-16, Hugo's directive — not switchable).** Every GPT Image call hardcodes OpenAI's `moderation="low"` param in `openai_image._generate_once` — the permissive (but still filtered) tier, accepted on both the create and edit endpoints for gpt-image models. This is the FIRST-line filter, running before the ladder below: the API defaults to the stricter `auto`, which rejected far more than the consumer chatgpt.com product (~49% of swap calls were safety rejections), because chatgpt.com runs its own tuned moderation level you can't set via the API. Applies to every GPT path (Swap `gpt-image`, Swap/Reengineer `gpt2-id-swap` — all route through `_generate_once`). A defensive fallback drops the param + retries once only if a model rejects `moderation` as an unknown argument; a genuine content block still propagates to the ladder. Locked by `test_image_moderation.py`.

**Moderation ladder (2026-06-11; fallback opt-in since 2026-06-12; Director rewrite rung 2026-06-13).** When the chosen engine rejects a swap on content-policy grounds: the client first retries with two escalating append-only softeners (`content_policy.py`, rung 2 is a full fictional-film-production reframe). Then **RUNG A (default when `ANTHROPIC_API_KEY` is set): ONE Director moderation rewrite** — `prompt_director.direct_moderation_rewrite` (phase `director_rewrite`, ~$0.05) sees the scene frame + the ENGINE-EFFECTIVE prompt (`runner.engine_effective_swap_prompt`, shared with the scene-rewrite feature) + the rejection text, and rewords the prompt neutrally (same scene, same visual result — bodies/touch described clinically, one wholesome-context clause, never claiming anything visually false; camera-gaze ensured for reengineer jobs; style clauses stripped/re-appended) → retry on the SAME engine. Hugo validated the approach by hand in ChatGPT (the blocked "pinch back fat" scene generated fine with reworded prompt). Once per slot; recorded on `GeneratedImage.moderation_rewritten` + `variant.moderation_rewrite` event + violet 🪄 chip in the Reengineer strip; the reworded prompt persists on the slot (visible in ✎↻). Director unavailable/None → fall through. The old final rung — falling that one slot back to `nbp-swap` — remains **opt-in via `SWAP_MODERATION_FALLBACK=1`** (Hugo's "100% GPT Image 2" directive 2026-06-12): by default a still-rejected slot FAILS loudly with the moderation reason so the user can ↻ retry or reword, never switching engines. With the flag on, the rescue is loud as before: recorded on `GeneratedImage.fallback_model`, emitted as `variant.fallback`, purple ⇄ chip in Swap + Reengineer UIs. (Measured rationale for the rescue: 49% of gpt-image-2 swap calls were safety rejections burning the full ~131s render each; nbp-swap had 0 moderation failures on the same scenes.) The PIPELINE layer still has no cross-provider fallback — this is a sanctioned runner-level exception.

**VIDEO moderation fallback → Grok Imagine 1.5 (2026-07-26; OPT-IN since 2026-08-03 — `VIDEO_MODERATION_FALLBACK=1`, default OFF).** Hugo 2026-08-03: "ta bort fallbacken till en annan modell om ett klipp failar". By default a clip its model refuses on content-policy grounds now **FAILS LOUDLY with the real reason** — no second provider is tried — so the user can reword or retry. Three reasons it was switched off: the rescue silently moved one clip in a reel onto a different provider (visibly and audibly unlike its neighbours), it dropped any resolved 🎯 end pose, and since the 🗣 language redirect it would have moved a German/Spanish clip off `SPOKEN_LANGUAGE_VIDEO_MODEL` — the one model trusted with that language. `runner_media.video_fallback_model()` is the single resolver both runners read (returns None when disabled). With the flag ON everything below applies unchanged. The video-side counterpart to the image ladder above. When a clip's chosen video model refuses it on CONTENT-POLICY / NSFW grounds, the runner re-submits the SAME clip ONCE on `grok-imagine-1.5` — a different provider stack (xAI via fal), markedly more permissive than Kling/Veo. Only GENUINE content rejections trigger it (`content_policy.is_content_rejection`); timeouts / network / fal-balance failures keep the normal fail path, and if the fallback ALSO refuses the clip fails LOUDLY naming the real reason (a non-content failure on the fallback leg reports its true cause, never a false content block). Single source of truth: `runner_media.VIDEO_MODERATION_FALLBACK_MODEL`. Covers every path that funnels through `runner._animate_one_video` (Swap synthesis, retry_one_video, generate_more_videos, retry_failed_videos, Reengineer animate/reanimate) plus the Reengineer direct/no-swap shared clip (`runner_reengineer._render_direct_clip`). Recorded on `VideoVariant.fallback_model` + a `video.fallback` event → violet ⇄ chip in the Swap Step-5 and Reengineer clip strips. `runner._eff_video_model` is fallback-aware so salvage re-poll and post-restart resume poll the provider the clip was ACTUALLY submitted under (fal request_ids are endpoint-scoped — polling Kling with a grok job id would 404). **END-FRAME CAVEAT:** `grok-imagine-1.5` is NOT in `END_FRAME_VIDEO_MODELS` and `pipeline.submit_video` never forwards `end_image` for it, so a clip with a resolved 🎯 end pose LOSES it on fallback. Hugo's decision: fall back anyway (a clip without the end pose beats no clip) but never silently — `runner_media.fallback_drops_end_frame(chosen)` detects the case, it's recorded on `VideoVariant.fallback_dropped_end_frame`, logged as a warning, and rendered as an amber "🎯✕ slutposen tappad" note beside the ⇄ chip. A scene whose model already ignored the pose is never flagged (false alarm). Originally built 2026-07-14 against `seedance-2.0` (end-frame-capable); that branch was deleted and the feature rebuilt on grok-imagine-1.5 on 2026-07-26. Locked by `test_video_moderation_fallback.py` (14 cases). **Scope narrowed 2026-08-04** — this generic rescue now applies to every clip EXCEPT a SPANISH `veo-3.1-fast` one, which has its own always-on rescue (next paragraph). A German or English Veo clip still falls under this generic, off-by-default rule.

**Refused SPANISH Veo clips → Kling 3.0 (2026-08-04, Hugo: "alla veo klipp som failar fallbackar till seedance 2.0 fast" — Seedance proved impossible, see below). ALWAYS ON — no env flag.** A `veo-3.1-fast` clip belonging to a 🇪🇸 character that fal REFUSES re-submits ONCE on `kling-v3`; every other clip keeps the 2026-08-03 default of failing loudly (see previous paragraph). Veo needed its own carve-out because it is where the 🗣 redirect sends EVERY Spanish clip and fal's checker kills them wholesale even at the least strict `safety_tolerance: 6` — measured on `j_619e0a2cf2`, **11 of 24** redirected Spanish clips (46%) died refused, on dialogue as innocuous as "Mezcla una cucharada de aceite de coco". Under the generic rescue those clips had nowhere to go: it is off by default, and `grok-imagine-1.5` would have undone the redirect. **WHY NOT SEEDANCE 2.0 FAST — do not retry this.** The feature was built on it first and it cannot work: ByteDance refuses EVERY frame containing a real person. Measured 2026-08-04 — **11 of 11** submits refused (7 in-app + 4 direct probes), on BOTH the fast and standard tiers, across three different faces, always `loc: [body, image_url]` / `content_policy_violation` / `partner_validation_failed` / "The images or videos provided may contain likenesses of real people…". A control frame with NO person rendered fine on the same endpoint and key, so it is ByteDance's real-people policy, not our account and not the prompt — and no prompt change can reach an image check. Every frame this app produces is a photoreal person, so Seedance can rescue exactly zero clips here. The `seedance-2.0-fast` slug added for it was removed again; `seedance-2.0` stays registered but has never rendered a clip in this app (0 successes, all-time). Guarded by `test_the_rescue_is_not_seedance`. **WHY KLING 3.0.** It is the only candidate both ACCEPTED and GOOD. On the clips Veo had actually refused, re-rendered verbatim: `kling-v3` 2/2 rendered, `grok-imagine-1.5` 2/2. Spanish speech fidelity over every Spanish clip on disk, scored the way video QC scores it (Scribe transcript vs the line the prompt asked for): **veo-3.1-fast n=37 mean 0.992** (min 0.909, 100% ≥0.8) · **kling-v3 n=106 mean 0.953** (min 0.000, 98% ≥0.8) · **grok-imagine-1.5 n=6 mean 1.000**. Kling costs ~0.04 mean against Veo and carries a ~1% English-leak tail (the one 0.000 clip spoke English on a Spanish prompt — the existing wrong-language net re-renders it). It beat Grok on the two things that make a rescued clip fit the reel it lands in: it IS in `END_FRAME_VIDEO_MODELS` so the 🎯 end pose survives, and it is the runs' own default model so the clip looks and sounds like its neighbours. Grok's 1.000 is 6 clips — too thin to outweigh either. **SPANISH ONLY** (Hugo's call): a refused GERMAN Veo clip still fails loudly, because Kling is measured 0.48 on German against 1.00 English / 0.93 Spanish — rescuing it there would ship a clip the language net has to reject anyway, slower and dearer for the same outcome. Resolver: `runner_media.video_fallback_model(chosen_model, language=…)` + `_veo_rescue_applies` — reads `VEO_VIDEO_MODELS` / `VEO_FALLBACK_LANGUAGES` / `VEO_MODERATION_FALLBACK_MODEL` and ignores `settings.video_moderation_fallback` for that pair; `fallback_drops_end_frame(model, language=…)` resolves the SAME way, so the Veo→Kling leg correctly reports no dropped pose while the generic→grok one still does. **Veo has a SECOND refusal shape** — fal `no_media_generated` ("The model did not generate the expected output for this prompt… including unsafe content"): it accepts the submit, runs, and returns nothing. 4 of the 11 refused Spanish clips failed this way and matched NO signal in `content_policy`, so they were dying with no rescue. `runner_media.triggers_fallback(model, exc)` treats it as a refusal ON THE VEO LEG ONLY — it is deliberately NOT added to `content_policy`'s global list, which also drives the image ladder and the generic rescue and whose other `no_media_generated` causes (incompatible media type, missing attachment) are real bugs that must keep failing loudly. Timeouts / network / fal-balance failures are never refusals anywhere. `_eff_video_model` checks `fallback_model` BEFORE `language_model_redirect`, so a rescued clip re-polls Kling's endpoint on salvage and post-restart resume (fal request_ids are endpoint-scoped — polling Veo with a Kling request_id 404s). If Kling ALSO refuses, the clip fails naming both. Locked by `test_video_moderation_fallback.py` (29 cases).

**Reengineer 🎬 AI Director (2026-06-11, opt-in checkbox at upload, OFF by default).** ONE Claude (Opus) call LOOKS at every detected scene frame and writes a tailored COMPACT swap prompt per SCENE — naming the actual props with position/approximate size in frame, anchoring the camera distance/crop, and matching the scene's light (exactly the static template's observed drift modes: wrong props, zoomed-out framing). Implemented in `prompt_director.direct_reengineer_swap` (+ `plan_from_scene_prompts` replicates per-scene prompts across characters into the standard `SwapDirectorPlan` so `_kick_char`'s existing precedence consumes them unchanged; gpt2-id-swap's dispatch mechanically flips Image 1↔2). Wired in `_create_job_and_swap` (cached on `Job.director_prompts_json` → crash-resume never re-bills); ANY failure → None → normal template chain. ~$0.10 + ~1 min per run; requires `ANTHROPIC_API_KEY`. Prompts stay ≤~120 words per the bake-off's compact-prompt lesson.

**A spoken line stated TWICE was only localized ONCE (2026-08-08, Hugo's directive — "fixa så att det aldrig händer igen").** In re_8e87b525b2 (j_7c2ff95032) all four language finals — wang/Ching 🇪🇸, Helene/Susanne 🇩🇪 — OPENED with the verbatim English source line ("Put baking soda on a raw beet…") in both audio and burned captions, then switched to Spanish/German from clip 2. Scene 1 was also the only one of seven with `qc_status="skipped"`, for every one of the four. The AI Director's structured prompt states the spoken line in TWO places — `ACTION AND CAMERA MOTION — The man … addresses the camera: "<line>"` and `AUDIO — … Language: English; … Dialogue: "<line>"` — and `video_edit.dialogue_matches` returned only the matches of the FIRST pattern that found anything. `_LABELED_DIALOGUE_RE` matched the AUDIO copy, so the ACTION copy was never returned: the localizer rewrote one of the two and the English sentence reached the video model verbatim, sitting EARLIER in the prompt than its own translation. **First-pattern-wins can never be right for a REWRITER** — it must see every occurrence or it leaves one behind. Four changes: (1) `dialogue_matches` now UNIONS all three patterns, dropping overlapping group-1 spans most-specific-first and returning them in document order (the order the localizer rewrites in); (2) `extract_dialogue` de-duplicates by `video_edit.phrase_key` so a twice-stated line is READ once — it feeds the caption script, the STT bias hint, QC's expected speech and the Kling duration estimate, all of which would otherwise double; (3) `_SPOKEN_VERB_DIALOGUE_RE` gained the verbs a Director actually uses for talking to the lens (`addresses`, bare `speak`, `delivering`, …) and accepts a COMMA as well as a colon before the quote — measured over all 1794 distinct movement prompts on disk this captures 8 further genuine spoken lines, ZERO quoted props, and takes prompts carrying a sentence-length quote NO pattern can read from 17 to **0**; verbs that introduce SIGNAGE (`reads`, `states`, `labeled`) are deliberately excluded, and fire nowhere in the corpus anyway. (4) THE DURABLE ONE — `reengineer._surviving_source_line` asserts after localization that no ENGLISH source line is still standing anywhere we did not rewrite, and raises `LocalizationError` if one is. Every earlier fix in this class recognised a SHAPE (2026-06-30, 07-31, 08-02, 08-06) and the Director writes free-form English, so each new shape shipped a silent English clip; this check asks the shape-independent question instead, before a credit is spent. It subtracts the text we WROTE before searching, so the documented translate no-op (a line already in the target language, returned unchanged) is not a false positive — 0 across the corpus. **A FIFTH shape, found by re-reading the data rather than the code:** Susanne's clip (vd_2670d7) had NO localization at all — the 👥 speaker-fix agent had produced a MIXED prompt, an English line in the ACTION block beside the German translation in the AUDIO block, so `spec.marker in prompt` was true and the "already in the target language" short-circuit returned the whole thing untouched. One marker phrase cannot vouch for a prompt that says two different things — the same false inference the 2026-08-03 retry fix found for USER-typed prompts and answered with `force`, here in its machine-written half where no caller knows to pass `force`. `_states_more_than_one_line` now blocks the short-circuit whenever the prompt carries two or more DISTINCT spoken lines; the already-translated copy comes back unchanged from the translator, per each `translate_system`'s explicit no-op order. Measured over every prompt on disk: of 233 marker-carrying prompts only 6 fall through, and all 6 are genuine leaks (an English line sitting beside its own translation) — so the extra cost is 6 cheap calls and the false-positive rate is zero. Also fixed, found by adversarial review: `_inject_inline_attribution`'s idempotency guard was whole-prompt, so when the inline "American accent" phrase sat in the SECOND copy, `_force_language_speech` rewrote it into the attribution FIRST and injection at the first copy was skipped — the sentence the model reads first named no language, which is exactly the 2026-08-02 failure one layer up; the guard is now scoped to the clause introducing that line (`_ATTRIBUTION_SCOPE`). The app.js `klingSpeechSecs` mirror was rebuilt to match (span union via `/gid` + `m.indices[1]`, stage-direction strip before keying, Unicode `phrase_key` — the ASCII form folded every accented letter of an es/de line); it is display-only (the ⚠ badge, never `klingDuration`), and `tests/js/kling_speech_secs_mirror.mjs` now proves JS/Python parity behaviorally instead of pinning regex text — verified to FAIL against the pre-fix JS and against two plausible-but-wrong unions. **KNOWN, accepted on OLD data:** the 7 clips this bug already shipped have a stored `localized_movement_prompt` containing both languages, so `extract_dialogue` now reads them bilingually — rebuilding those runs without RE-RENDERING scene 1 gives bilingual captions. Those clips speak English, so their captions were already wrong; the remedy is a re-render, and the state cannot be produced post-fix. Locked by `test_language_leak_2026_08_08.py` (20 cases, verified to fail with the fix reverted) + `test_kling_speech_secs_mirror.py`.

**A 🇪🇸/🇩🇪 character must SPEAK that language — inline attribution + wrong-language net (2026-08-02, Hugo's directive).** In re_b3170d2118 **8 of 10 German clips spoke ENGLISH**, and the prompts were not obviously broken: the dialogue WAS correct German and the German accent clause WAS present. What was missing was any statement of the language NEXT TO the line — `_force_language_speech` DELETED the source prompt's inline "with an american accent" and appended the German clause ~200 chars further down, leaving `says enthusiastically to the camera: "<German>"` inside an otherwise-English prompt. Kling answered that by speaking English. (Run-level 🗣 runs were fine because the analyst writes the attribution inline itself — `spanish_speech_clause`.) Three prompt layers now, all in `enforce_language_directives` and idempotent: (1) the inline attribution is REPLACED into the says-clause rather than deleted — "…to the camera **in standard German**: …" — and `_inject_inline_attribution` inserts it before the quote for prompt shapes that carried no accent phrase at all (says / `Dialogue:` / AUDIO-block, i.e. every shape `dialogue_matches` knows); (2) the standalone accent clause is kept as a hard guarantee (checked by exact clause, since the inline attribution contains the accent keyword and would falsely suppress `with_accent`'s substring guard); (3) a new `SpokenLanguage.speak_only_clause` — "The person speaks ONLY German … No English is spoken at any point." `with_accent` ALSO rewrites the English accent order for any registered non-English language, so a prompt hand-written at the gate on an es/de run can no longer order an American accent next to the line while the Spanish/German clause sits at the end (Hugo: "alla videoprompts för de spanska och tyska ska automatiskt justeras så att de inte har american accent"). The English path is untouched. **The net:** `video_qc.inspect_clip` takes `expected_language` + `original_speech` (the ENGLISH line the prompt carried BEFORE localization, captured in `_animate_one_video`); a clip whose transcript matches that English line ≥ `VIDEO_QC_LANGUAGE_THRESHOLD` (0.6) AND better than the translated line is `wrong_language`. That verdict outranks the fuzzy similarity check and always earns a re-render (`VIDEO_QC_LANGUAGE_MAX_RETRIES`, default 2) even though Hugo runs the speech check FLAG-ONLY (`VIDEO_QC_MAX_RETRIES=0`); still wrong after that → the clip FAILS loudly instead of entering a final. **Do NOT gate on Whisper's own `language` field** — measured on the real clips it reported `"english"` for vd_26334d, whose audio is plainly German, so it would have re-rendered and then failed CORRECT clips; `transcribe_detailed` exposes it for logs only. Locked by `test_language_enforcement.py` (16 cases incl. the false-positive one), verified against both real clips.

**The extractor is the ONLY real point of failure — five unmet shapes, one silent net (2026-08-06, Hugo's directive).** Four clips shipped speaking the verbatim ENGLISH source line and **20 of 76** language clips had video-QC silently skipped (re_db8e7b4e91 / j_bedcc4d1f5, all four 🗣 characters; every clip transcribed to confirm, not inferred). The AI Director writes its AUDIO block in free-form English while the localizer recognised only a narrow set of literal phrasings, and — the structural half — **the translator and its safety net read the SAME extractor**, so one regex miss disarmed both at once, silently. Five shapes leaked: (A) `Dialogue exact: "…"` — `_LABELED_DIALOGUE_RE` demanded the colon IMMEDIATELY after the label, so the line was invisible, nothing was translated, `video_qc._transcribe` got no expected speech and returned None → `qc_status="skipped"`; (B) `Dialogue in standard German: "…"` — **our own** `_inject_inline_attribution` output, which blinded the extractor on the very prompt it had just repaired (that is the 16 correctly-translated clips that also lost QC); (C) `Voice: Male, American accent` — bare, no "with an" preposition; (D) `Language: English. Accent: American.` — labeled fields, never stripped; (E) `speaking clearly and enthusiastically in English` — adverbs between the verb and "English", which left the order standing next to the appended Spanish attribution and produced vd_295249's English tail. Fixes: `_LABELED_DIALOGUE_RE` accepts a short qualifier between label and colon (body forbids `"` `:` `;` `.` so it can never cross a sentence boundary or a second colon — a quoted PROP stays unreachable); `_EN_SPEECH_ORDER_RE` tolerates up to five intervening word/comma tokens; new `_BARE_EN_ACCENT_RE` / `_EN_LANGUAGE_FIELD_RE` / `_EN_ACCENT_FIELD_RE` REPLACE (never delete — a voice field naming no language reads as English) via `SpokenLanguage.accent_phrase`; `_dedupe_attribution` collapses the attribution the replacement can double. **Two LOUD nets make the NEXT unmet shape non-silent** (Hugo's standing refuse-loudly rule, both pre-submit so they cost zero render credits): `_sanitized_language_prompt` re-scans its OWN output via `_residual_english_order` and raises `LocalizationError` if any English order survived; and a prompt that ORDERS speech while carrying a ≥5-word quote the extractor could not read (`_unparsed_dialogue_line`) is refused rather than rendered blind. Both scan through `_without_own_clauses`, which subtracts our own boilerplate — `speak_only_clause` ends "No English is spoken at any point." and `with_accent` appends the English accent + pronounce + no-music clauses to EVERY scene, silent ones included, so without that subtraction a wordless shot would be refused. The ≥5-word rule is what separates a LINE from a quoted prop (`"Heinz White Vinegar"` = 3). `_EN_ACCENT_FIELD_RE` carries a `(?!\s+Spanish)` lookahead so it cannot re-match its own "Accent: neutral Latin American Spanish" replacement. Mirrored in app.js `klingSpeechSecs`; the JS-mirror sync test pins the shared fragment. Locked by `test_language_leak_2026_08_06.py` (20 cases), each verified to FAIL with its fix reverted.

**Retrying a 🇪🇸/🇩🇪 clip retries in that language (2026-08-03, Hugo's directive).** The "Regenerate this clip" modal prefilled the ENGLISH source prompt for a language-flagged character — it read like the ↻ would come back speaking English (the clip itself was fine: every retry path funnels through `_animate_one_video`, so it localized). It now prefills `VideoVariant.localized_movement_prompt`, the text the clip was ACTUALLY submitted with (exposed per clip in `_job_to_dict`; `app.js regenPromptPrefill(vv, lang)` — gated on the character's CURRENT 🗣 flag so a character switched back to English is never prefilled with its old German take), plus a sky-blue note naming the language. That prefill opened a real hole, and closing it is the other half: `localize_motion_prompt` returns a prompt UNCHANGED as soon as it carries the language marker ("in standard German"), so a NEW **English** line typed into the now-German box would sail through and be spoken in English, silently. A prompt the USER typed (`movement_prompt_override` is set — the regen modal, the ✎↻ prompt button and the scene-level "↻ Ny prompt", which all ride the same override) is therefore always re-localized: `localize_motion_prompt(..., force=True)` skips ONLY that marker check (a silent clip is still left alone, translation failure still fails the clip loudly). Two safety rails make forcing free: each `translate_system` now orders "a line ALREADY in this language is returned EXACTLY as given", so re-localizing an untouched prompt is a no-op instead of a reword; and `original_speech` (the wrong-language net's English source line) is armed ONLY when the dialogue actually CHANGED — arming it with a German "original" would score German audio against a German line and could fail a correct clip as "fel språk". The initial-run path is untouched (no override → the cheap marker short-circuit still applies, no translate call billed). Folded into the (scene × gender × language) prompt cache below: a forced re-localize of a per-clip override carries a distinguishing marker in its cache key so it never reuses (or poisons) a scene-mate's un-forced slot for the same literal text. Locked by `test_retry_language.py` (12 cases).

**Per-character GENDER + 👥 speaker-attribution agent (2026-08-02; WIDENED + SHARED 2026-08-03).** Since 2026-08-03 the gate is simply **a FEMALE character on any scene** — the 👥 tick no longer gates it, it is passed to the agent as a HINT that a second person is in frame. The old two-sided gate left the single-person case shipping "He says … while he is …" for a woman (measured: Veo follows the start frame and renders her anyway, 0.95 word-similarity, so the model was doing the saving, not the prompt). The rewrite is now computed **ONCE per (scene × gender)** and the localization **ONCE per (scene × gender × language)**, shared by every character in that group (Hugo: "en agent körning per scen per språk per kön") — a 9-character run buys one rewrite per scene instead of nine, and every character in the group gets byte-identical wording. Cache lives in `runner._PROMPT_CACHE` / `_cached_prompt`, keyed by (job, kind, scene, gender, language, **prompt-hash**) so a per-clip prompt override never shares a scene-mate's slot; an `asyncio.Lock` per key makes parallel clips collapse into one call; failures are NOT cached; `_clear_prompt_cache` runs at batch end and the dict is hard-capped for the retry paths that bypass it. **Because the rewrite is reused, the agent is told to describe the speaker by POSITION and gender only, never by clothing** — `outfit_mode="character"` dresses each character in her own clothes, so a clothing-based description would be wrong for everyone but the character whose frame the agent saw. The character's name is no longer sent to the model at all.  Every movement prompt in the library was written off a MALE original ("the man in the grey hoodie says to the camera: …"). Swap a FEMALE character into a shot where TWO people are visible and the video model reliably lets the man say the line. Fix: `CharacterAsset.gender` ("male" | "female" | None) — **required when creating a new character** in the upload modal, editable afterwards from the library card (♂/♀ picker next to 🗣) — plus an **opt-in per-scene 👥 "två personer" checkbox** (Swap upload form + the ✎ edit-mode scene row). A scene that is BOTH ticked AND being animated for a FEMALE character runs ONE Claude Sonnet vision call (`speaker_fix.py`, phase `speaker_fix`, ~$0.01) right before submit: it sees that character's actual swapped frame for the scene + the prompt about to be sent, and rewrites ONLY the speaker attribution — who speaks, described by what's visible ("the woman on the left in the beige blazer"), male→female words for the speaker, plus one sentence that the other person listens silently without moving their lips. Dialogue, camera, motion, accent and negatives are kept verbatim; a rewrite that drops >50% of the text is refused. **The gate is two-sided**: ticking a scene never spends credits on the MALE characters in the same run, and a female character on an unticked scene is untouched — `speaker_fix.needs_speaker_fix(gender, scene_id, two_person_scenes)` is the single source of truth. Runs BEFORE the 🗣 language localizer (so a 🇪🇸/🇩🇪 character gets the right speaker in the translated clip too) and before video-QC's expected dialogue is derived. **Failure fails the CLIP LOUDLY** (missing key, API error, no tool call, empty/gutted rewrite) — same contract as the localizer; never ship a clip with the words in the wrong person's mouth. Recorded on `VideoVariant.speaker_fix_prompt` → ⚥ chip with the full rewritten text as tooltip. Storage: `Job.two_person_scenes` (carried on scene duplicate, re-keyed by "byt scenbild", dropped when a scene becomes 📌 direct — a shared clip has no per-character frame, and the PATCH refuses the flag there with a 422). NOTE: `characters` is one of the few SQLite tables still WITHOUT a `model_json` column, so `gender` needed its own column + `upsert_character`/`_char_from_row` lines — the same omission that silently blanked `telegram_chat_id` on every restart. Model override: `SPEAKER_FIX_MODEL`. Locked by `test_speaker_fix.py` (18 cases) + the plumbing tests in `test_reengineer_from_images.py` / `test_reengineer_edit_state.py`.

**Multi-person scenes: WHO gets swapped is asked, never guessed (2026-08-06, Hugo's directive).** In re_a5613a883e scene 2 a two-person photo (older man left holding a shot glass, blonde woman right) was swapped for NINE characters and came back nine different ways — the woman DELETED in most of them, kept in three, and for both FEMALE characters the new face was painted onto HER while the chosen man was left untouched. All nine PASSED image QC. Three independent holes — nobody asked WHO to replace, no prompt forbade deleting anyone, and QC could not see either — closed by four changes:

1. **Detection is no longer the Director's private business.** Multi-person detection lived inside `prompt_director.direct_reengineer_swap`, which runs ONCE at run creation — so a scene added afterwards via "+ egen" (`reengineer_add_scene`) never got a Director prompt, never reached the "välj person"-gate, and fell back to the generic job prompt: *"Replace **the person** in Image 1"*, singular and unanchored. New module **`scene_people.py`** (`detect_people` / `people_count`, phase `scene_people`, Sonnet, ~$0.01 per scene IMAGE — never per character) answers the same question in the same `{multi_person, people[{position, description}]}` shape, so the existing gate and `api._person_directive` consume it unchanged. It is **advisory**: no key / API error / unusable response → None → the scene generates anyway behind the cast lock + QC, because failing a scene over a $0.01 judge call would be worse than the bug. A "multi_person" verdict describing fewer than two people is downgraded — a one-item list is not a choice. It deliberately does NOT count background passers-by, people on posters/screens, or reflections. **Nothing in `prompt_director.py` was touched, on purpose**: `prompt_fingerprint()` hashes that file's own source, so editing it invalidates every cached Director plan — including the person-directives already baked into live runs.
2. **Coverage** (Hugo's choice, 2026-08-06): **added scenes** get a **per-SCENE gate** — `generate_added_scene` detects BEFORE spending any image credits and, on 2+ people, writes `multi_person`/`people`/`awaiting_person` on the entry and returns without generating; the run itself is untouched (it may be sitting at `awaiting_assembly` on finished finals, so the run-level `awaiting_person_choice` status is the wrong instrument). `POST /api/reengineer/{re_id}/scenes/{idx}/resolve_people` `{swap_person_idx}` bakes the choice and releases just that scene via `runner_reengineer.swap_added_scene`. **Director-OFF runs** are covered too: `_create_job_and_swap` falls back to `detect_people_for_scenes` over the swap scenes whenever the Director didn't supply metadata (off, or its call failed), so the run-level gate no longer silently disappears with the checkbox. NOT covered, by Hugo's decision: "byt scenbild" and classic `POST /api/jobs` Swap jobs. The old run-level gate ALSO had a silent hole — it bailed out entirely when `plan is None` (the normal Director-off state), recording the user's pick in the UI and never telling the image model; `api._bake_person_choice` now synthesises a plan from the job's **engine-effective** prompt (`runner.engine_effective_swap_prompt` — gpt2-id-swap's compact identity-first text, stored in scene-first canonical orientation, NOT the long template that engine renders measurably worse from) and appends the per-character directive.
3. **`pipeline.CAST_LOCK`.** Every stock swap prompt forbade ADDING people and said nothing about REMOVING them. The lock ("replace ONLY one of them … never delete, omit, crop out or merge away a person … the SAME number of people as the original") is appended by `with_cast_lock` at **DISPATCH**, not baked into the prompt builders: that keeps `stock_swap_prompts()` returning the exact strings already stored on existing jobs (a builder edit would reclassify every stored prompt as "custom" and feed gpt2-id-swap the long template), and it reaches CUSTOM prompts too — the Director's own per-scene prompts, 🪄 rewrites, anything the user typed. Idempotent on the exact clause.
4. **Image QC can finally see it.** `MISSING/EXTRA PEOPLE` only ever fired on "no person at all" or an invented extra, so 2 people → 1 person passed. Two new catastrophe classes, **`PERSON COUNT`** and **`WRONG PERSON SWAPPED`**, are **explicitly conditional on context flags that are only ever sent for a MEASURED multi-person scene** (`Job.scene_people_counts` > 1 and `Job.scene_swap_targets`) — a single-subject or unmeasured scene reaches the judge with no counting instruction at all, so the 2026-06-30 catastrophe-only policy is untouched everywhere else. The judge is told the chosen person may legitimately differ from CHARACTER in gender or age and that changing them anyway is CORRECT. Both classes are in `_REROLL_MARKERS` (a deleted person only exists in the SCENE, so a minimal-change repair of the failed image cannot bring them back), while plain `WRONG PERSON` stays repairable in place; the judge's corrective hint is appended to the retry prompt — Hugo's own suggestion, "en QC som förstår vad felet var och justerar bildprompten". New settings: `SCENE_PEOPLE_MODEL` (default `claude-sonnet-4-6`) / `SCENE_PEOPLE_PRICE_USD` (0.01).

**Two review-confirmed defects the gate itself introduced, both fixed before release (adversarial review, 2026-08-06):** (a) **a held scene was SILENTLY DROPPED from every final.** A scene waiting on the person choice has ZERO variant slots, and both `_collect_clips` and `_assembly_gaps` read "no slots" as *"this scene was never this character's"* — so it produced neither a `hard` nor a `pending` gap, the manual "Bygg ihop igen" returned `{ok}`, and `_auto_assemble_blockers` (literally `hard + pending`) let the ⚡ auto-build ship and Telegram-deliver a reel missing the scene the user had just added. Before the gate an added scene always had GENERATING placeholders within a second, which produced the loud "ingen godkänd bild" gap — the gate deleted that guard. Both functions now report `awaiting_person` as a **hard gap** ("välj vem som ska bytas ut"), not waitable, mirrored exactly as the `_collect_clips`/`_assembly_gaps` invariant requires. Same failure class as `296c3e1`. (b) **baking a choice twice stacked contradictory gender clauses.** `_bake_person_choice` reads `base` back OUT of the plan, where a previous bake already left the FIRST character's directive — so a second bake gave every character both *"The new character is a man …"* and *"The new character is a woman …"* for the same target. Since GPT Image 2 resolves the target by gender first, that reintroduces the exact mis-swap the feature exists to prevent, on the one scene it was asked to protect. Reachable with no code change: two `state.scenes` entries can share a `scene_id` (byte-identical frames collapse), and "📌 ingen swap" → "↩ ta bort direkt" re-arms the added-scene gate. `_strip_person_directive` now cuts any prior directive at `_PERSON_DIRECTIVE_LEAD` before re-appending, making the bake idempotent the way `pipeline.with_cast_lock` already is. Both are locked by regression tests verified to FAIL with their fix reverted. Locked by `test_scene_people_gate.py` (26 cases).

**Reengineer per-scene END FRAMES (2026-06-13, Hugo's directive).** "🎯 End frame" control on every scene row (gate, awaiting_assembly and ✎ edit mode; honored only by end-frame-capable models — see `runner_media.END_FRAME_VIDEO_MODELS`, currently Kling 3.0 / Seedance 2.0 / Veo 3.1 Fast — a scene on any other model ignores the pose): upload an end pose AFTER the scenes exist → the existing job-level end-frame machinery (2026-06-08) swaps EVERY character into it (`runner.regen_scene_end_frames`, no QC, errors on `end_frame_errors`) → the scene's clip interpolates start → swapped end frame. NOT a new scene — it rides on the existing entry via the job-level endpoints (`POST/DELETE /api/jobs/{id}/scenes/{sid}/end_frame`, `POST .../regen_end_frame`) keyed by scene_id. Enablers: (1) those three endpoints' movement lock is **relaxed for `Job.from_reengineer`** (plain Swap jobs stay locked); (2) `retry_one_video` and `generate_more_videos` — the paths reanimate uses — now resolve the end frame via the shared `runner._resolve_end_image` helper (they silently DROPPED it before, so Kling retries lost the end frame even in plain Swap); (3) post-gate the UI marks the scene `dirty` after set/regen/clear so "▶ Animera om ändrade" picks it up. Per-char swapped end-frame thumbs render inline with ↻ regen / ⇪ replace / ✕ clear; the upload counts as no new scene and costs one swap image per character.

**End frame staged AT the Swap upload step (2026-06-23, Hugo's directive).** On the Swap tab ("animera dina referensbilder", `POST /api/reengineer/from_images`) each scene row now has an optional "🎯 End frame" upload (hidden for `📌 ingen swap`/direct rows) so the end pose can be attached BEFORE the first image gen — the end-frame swap then generates in the same swap phase as the start frames (via `_kick_char`'s existing end-frame block), eliminating the post-gate second wait. The sparse files ride as `end_frame_files` + a parallel `end_frame_idx` row-index array; the endpoint saves them under `<run_dir>/end_frames/` and attaches `end_frame_path` per scene_entry (persisted in state → resume-safe); `runner_reengineer._create_job_and_swap` lifts them onto `Job.end_frames_by_scene` over `uniq_ids` (direct scenes excluded; on a duplicate START image first-wins — distinct end frames per duplicate still need the post-gate "duplicate scene" flow). The post-gate ↻/⇪/✕ controls keep working unchanged on a pre-staged frame. Locked by `test_reengineer_from_images.py` (end_frame staging + direct exclusion + validation 400s + `end_frames_by_scene` population).

**Per-character VERBATIM end-frame upload (2026-07-04, Hugo's directive).** Alongside the shared swap-into-pose mechanism above, each character can be handed its OWN finished end frame — used EXACTLY as-is (no swap, no QC). Stored on new field `JobCharacter.end_frame_uploads: dict[scene_id → path]`; `runner._resolve_end_image` prefers it OVER the swapped `end_frame_paths` (precedence: verbatim upload → swapped frame → swap-now-from-pose → none), so a hand-provided frame wins while characters without an upload still swap into the shared pose. Endpoints: `POST/DELETE /api/jobs/{job_id}/characters/{char_id}/scenes/{scene_id}/end_frame_upload` (multipart `file`; saved verbatim to `output/<job>/<char>/endframe_upload_<scene>.<ext>`), same `from_reengineer`-only movement-lock relaxation as the shared endpoints. Serialized as `characters[].end_frame_upload_urls`. UI: the post-gate end-frame block now ALWAYS shows a per-character row (even with no shared pose) — each character has an "⬆ egen" upload + "✕" clear, the displayed thumb prefers the green-bordered verbatim upload over the indigo swapped one. The shared "🎯 End frame (swappa alla)" + ↻/⇪/✕ controls stay (relabeled "Gemensam slutpose"); the two mechanisms are independent (clearing the shared pose leaves per-char uploads untouched, and vice-versa). Carried on scene duplicate, re-keyed on "byt scenbild" re-point. Locked by `test_char_end_frame_upload.py`.

**Reengineer Kling auto length = ceil + 1 (2026-06-13, Hugo's directive — supersedes 06-12 plain ceil).** AUTO Kling clip length is the ORIGINAL scene clip's length rounded UP to the SECOND-next whole second ("6,4 s original → 8 s Kling"), clamped [3, 15] — one breath of margin, never the old speech-fitted extension. Manual `kling_secs` override still wins exactly as typed. `runner_reengineer._kling_duration` + the app.js `klingDuration` mirror (sync-pinned by `test_kling_duration_js_mirror_in_sync`).

**QC HEAD-RULER TEST (2026-06-13) — RETIRED 2026-06-30.** Historical: the judge used to measure the person's head height as a fraction of frame height in SCENE vs RESULT and FAIL (WRONG FRAMING / ZOOM → re-roll) when RESULT's fraction was < ⅔ of SCENE's or > 1.5× — a hard numeric rule never relaxed. In practice it became the dominant FALSE-positive (60% of all image rejects) — failing usable swaps for a slightly wider crop — so Hugo retired the whole framing/zoom check (see the catastrophe-only note above). The judge no longer measures framing at all.

**Reengineer camera-gaze policy (2026-06-13, Hugo's directive).** EVERY image generated in the Reengineer flow has the person looking directly into the camera, regardless of the original gaze. Three layers: (1) the static templates already contain the sentence ("They look directly into the camera with a natural, composed expression, even if the original person was not." — `build_edit_swap_prompt` + `build_gpt_id_swap_prompt`); (2) `REENGINEER_SWAP_DIRECTOR_SYSTEM` and `SCENE_REWRITE_DIRECTOR_SYSTEM` mandate the sentence verbatim (the old PERFORMANCE-ANCHOR rule that preserved the observed gaze is gone), with `prompt_director.ensure_camera_gaze()` as the code-level guarantee on every Director-written prompt; (3) the QC judge gets `camera_gaze=true` for `Job.from_reengineer` jobs — it ENFORCES camera gaze (WRONG GAZE = looking away) instead of failing it as a scene mismatch like before. Plain Swap-tab QC behavior is unchanged. Hand-gesture anchoring/judging is unaffected.

**Scene-level image change for ALL characters (2026-06-13).** "🪄 Ändra bild" button on every scene row (visible at the gate and in edit mode): the user describes the change in plain language ("byt ut kaffemuggen mot ett glas vatten") → `POST /api/reengineer/{re_id}/scenes/{idx}/rewrite_prompt` runs ONE Claude call (`prompt_director.direct_scene_prompt_rewrite`, phase `director_rewrite`, ~$0.05) that sees the scene frame + the scene's current swap prompt (style clauses stripped before, re-appended in code after; on background-replacement runs the Director ALSO sees Image 3 + gets the STRICTLY-FORBIDDEN-original-background rule) and rewrites the prompt with only that change applied — PURE PREVIEW, shown editable in the modal (Director failure → 502, never blocks; a second Director pass rebases on the modal's `current_prompt` so iterating doesn't lose the previous step) → `POST .../regen_images` regenerates the scene's image for EVERY character with the new prompt: per character the approved slot (else first ready, else first failed; in-flight slots never picked) regenerates IN PLACE via `retry_single_variant` (ONE shared provider semaphore, approvals withdrawn first, QC as usual), the prompt persists on each slot AND in the cached Director plan for that scene (synthesized via `plan_from_scene_prompts` when no plan exists — the Director-off default), post-gate the scene is marked `dirty` + finals go stale — the normal re-approve → "▶ Animera om ändrade" → "▶ Bygg ihop igen" chain takes over. Response carries `regen_variants` {char_id: variant_id} for client cache-busters. **Review-hardened (2026-06-13, 9 adversarially-verified fixes):** (1) the user's `change` text rides to the QC judge as the slot's `qc_intent` (`GeneratedImage.qc_intent`, `user_intent=(variant.qc_intent or job.prompt)`) so QC never "repairs" the requested deviation back — also fixes the per-image ✎↻ edited-prompt path; (2) prompts shown/rewritten are the ENGINE-EFFECTIVE text via `_engine_effective_swap_prompt` (stock templates like GENERATION_PROMPT are substituted at dispatch with gpt2-id-swap's compact prompt / EDIT_SWAP_PROMPT — editing the stored stock string rewrote text the engine never saw); prefill comes from `GET .../scenes/{idx}/swap_prompt` (`?variant_id=` narrows for the ✎↻ modal); (3) `retry_single_variant` re-points slots that SHARE a file with a clone (duplicated scenes) to their own path before regenerating; (4) `_collect_clips` reports a scene with slots but no approval as a LOUD coverage gap ("ingen godkänd bild") instead of silently shipping a final without it; (5) `_do_reanimate` persists only the idxs that actually spawned clip tasks, so an all-pairs-skipped reanimate no longer consumes the dirty flag, and `_reMarkVariantSceneDirty` also fires at `awaiting_assembly`.

**Reengineer "Bygg ihop igen" REFUSES BROKEN finals — but NOT stale ones (2026-06-17; dirty-block REMOVED 2026-06-24, Hugo's directive).** The rebuild must NEVER silently ship a *broken/shorter* final, but an *edited-but-not-reanimated* (stale-clip) scene NO LONGER blocks it. Two layers: (1) the `POST /api/reengineer/{re_id}/assemble` endpoint pre-flights coverage via `runner_reengineer._assembly_gaps(state, job)` and returns **HTTP 409 `{code:"incomplete_rebuild", message, dirty, hard, pending}`** (instead of scheduling the build) **only when `hard` or `pending` is non-empty** — a failed/missing clip or an un-approved image (`hard`), or a clip still rendering (`pending`); the message names which scene/character to fix ("ta om klippet" / vänta) and existing finals are left untouched. A scene merely `dirty` (edited, stale clip) is **still computed** by `_assembly_gaps` but does NOT block — the build proceeds with the existing (older) clip and the endpoint returns `{ok, stale_scenes}` so the UI shows a soft non-blocking note (the "ändrad" badge + ▶ Animera om ändrade stay available to refresh those clips by choice). Hugo 2026-06-24: "ta bort det här kravet — jag vill alltid kunna bygga ihop." `_assembly_gaps` mirrors `_collect_clips`' per-(char,scene) inclusion rules exactly so the gate and the build never disagree. (2) `_do_assemble` itself still FAILS a character loudly (status `failed`, missing scene named) instead of concatenating a shorter video when a scene's clip is genuinely missing after the bounded coverage-wait — guards the auto-assemble path and any direct call too. **NEVER-APPROVED characters are SKIPPED, not blocked (2026-06-27, Hugo's directive).** A character with ZERO approved variants across ALL swap (non-direct) scenes — added to the run but never used (no godkänd bild → no clip) — was previously counted as N "ingen godkänd bild" `hard` gaps and refused the WHOLE rebuild (re_3bedfe62d3: Silas blocked a 5-character reel). `runner_reengineer._char_is_uninvolved(state, jc)` now identifies such a character (deliberately narrow: a *partially*-approved char with one approval and another withdrawn is still a TRUE `hard` gap and refuses loudly; a pure-direct run with no per-char variants is never uninvolved — the shared direct clips ARE its build); `_assembly_gaps` skips it and reports it in a new SOFT `excluded` bucket (never blocks), and `_do_assemble` skips it too (no failed final, run lands `done` not `partial_success`). The assemble endpoint + app.js surface `excluded` as a soft note ("Hoppar över N karaktär(er) utan godkänd bild/klipp") so the drop is visible, never silent. Locked by `test_assemble_coverage.py` + `test_reengineer_assemble_editor.py` (`test_assemble_proceeds_when_scene_dirty`).

**↻ Kör om en körning med NYA KARAKTÄRER (2026-08-08, Hugo's directive).** "↻ Nya karaktärer" on any run card (Swap AND ♻️ Reengineer) clones that run's scene plan onto a different cast as a **brand new run** — the parent is never written to. A modal prefilled from `GET /api/reengineer/{re_id}/rerun_plan` lets the user edit EVERYTHING before starting (Hugo's choice): which characters, which scenes are included and in what order, every motion prompt and clip length, 📌 direct / 👥 two-person, whether to keep each 🎯 end pose, plus the run settings (swap model, outfit, background source, 🎬 Director, 🚫 skip QC, ⚡ auto, 🔁 auto-Telegram). `POST /api/reengineer/{re_id}/rerun` builds the child state SERVER-SIDE in `rerun.py`: `/from_images` accepts only uploaded `files`, so a browser-side clone would have to re-POST every frame, which re-registers new scene ids and forecloses reusing the parent's rendered direct clip. Scene rows are keyed by the parent's list INDEX, not scene_id (scene_id is not unique in `state.scenes`); a scene_id repeated inside the child is re-minted through `runner_reengineer.register_scene_duplicate` (moved there from api.py so both the upload path and this one mint them identically).

**The language rule is re-resolved, not copied — that is the whole point.** `runner._eff_video_model_for_variant` returns the 🗣 language model on its FIRST line, before the per-clip override, the per-scene override and the job default, off a LIVE `CharacterAsset.language` lookup — so an inherited `video_model` / per-scene `video_model` cannot drag a new 🇪🇸/🇩🇪 character off `SPOKEN_LANGUAGE_VIDEO_MODEL`, and an English character in a run cloned from a Spanish one is not dragged ONTO it. What WOULD defeat it is per-character clip state: `VideoVariant.language_model_redirect` / `.fallback_model` outrank the live lookup by design (fal request_ids are endpoint-scoped), so the clone copies NO per-character state at all. New: `rerun.preflight_video_models` refuses up front when the model a clip will ACTUALLY be submitted on — the redirect target for a flagged cast — has no key, instead of dying inside a worker thread an hour later (`POST /movement` only validates the picked models; the Reengineer animate endpoint validates nothing).

**Deliberately NOT inherited** (each one is a silent-wrongness trap the mapping pass found): `job_id` (a copied one makes `_do_create_from_images` RE-ATTACH to the parent's job and rewrite it), `finals`/`repurposed` (their Telegram receipts would show the parent's deliveries as this run's), every runtime key (`finals_stale`, `completed_at`, `consistency_warnings`, `dirty`/`dirty_at`, …), the **Director plan** (keyed by char_id → `plan.lookup(new_char_id, …)` returns [] and every slot silently falls back to the generic template; the child re-runs the Director for its own cast, ~$0.10), the **multi-person choice** (baked PER CHARACTER with that character's gender clause, so the parent's bake is wrong for a new cast in exactly the way the gate exists to prevent — re_a5613a883e; and a carried `awaiting_person` is a HARD assembly gap that would deadlock the build, so the child re-detects at ~$0.01/scene and re-asks), `character_source_image_ids` (keyed by the OLD char_ids), and per-character end frames (`JobCharacter.end_frame_uploads`/`end_frame_paths` belong to characters not in this run — the SHARED pose is inherited and each new character swaps into it as usual).

**Files are COPIED, never referenced across runs.** `DELETE /api/reengineer/{re_id}` rmtree's the whole run dir and no refcount anywhere is cross-run, so an inherited 🎯 pose or background pointing into the parent's dir would vanish SILENTLY (a missing pose is never an error — `_resolve_end_image` just returns None) and a referenced direct clip loudly-but-late (a waitable gap that never resolves). The shared 🎯 pose is read from `Job.end_frames_by_scene` — the live truth, since the post-gate set/regen/clear endpoints write only there while the scene entry's `end_frame_path` is write-once at upload — and copied into the child's dir. Scene images are the one safe reference: content-addressed, and nothing ever unlinks `input/scenes/`. **A scene id does not imply a file.** A ⧉-duplicated scene (`…__dup<hex>`) has NO file of its own — `_apply_scene_duplicate` inserts the SOURCE's path into `Job.scene_image_paths` and never writes one; measured on the real store, **215 of 758 scenes across 87 of 121 runs** are in that state. Since the child's job derives `scenes_dir/<sid>.png` (hardcoded, unvalidated), inheriting such an id verbatim would hand it a path that does not exist and fail every variant, per character, at generation time. `rerun.scene_file` therefore resolves through canonical path → the parent job's `scene_image_paths` → the base id a `__dup` chain was minted from, and `build_scene_entries` re-registers any scene whose bytes are NOT at the canonical path under a FRESH id backed by its own file (minted off the BASE id, so chains don't grow a `__dupA__dupB` tail). Only a scene whose image is genuinely unfindable REFUSES the re-run loudly.

**♻️ Direct-clip reuse (Hugo's choice; per-scene opt-out).** A 📌 direct scene's clip is character-independent by construction — every character's final gets the identical file — so the child copies the parent's `direct_clip_<sid>.mp4` into its own dir instead of re-rendering it (one video credit cheaper, and byte-identical to the parent's). Copied, not referenced: a full animate on the PARENT overwrites that same path in place. The enabler is `runner_reengineer._reset_direct_clips`, extracted from `_do_animate`, which nulls every direct scene's clip at phase start — it now skips an entry marked `direct_clip_reused` whose file still exists, and a vanished file degrades to a normal re-render rather than a permanent gap. An explicit redo clears the marker via the shared `_clear_direct_clip` (a redo is a request for a NEW take). NOTE: no run in Hugo's 121 on disk has ever used a direct scene, so this path is guarded but unexercised in practice.

**The child is ALWAYS `from_images: True`**, even when cloned from a ♻️ video run: there is no source video to re-analyze, and `resume_all` dispatches on that flag — a False would send the child through the video path on restart and KeyError on `source_path`. Consequence: a re-run of a Reengineer run appears on the **Swap** tab, titled "↻ <parent name>" with `rerun_of` pointing back. Locked by `test_rerun_new_characters.py` (30 cases; the copy-not-reference, drop-runtime-keys and direct-reuse fixes each verified to FAIL when reverted).

**Reengineer EDIT MODE (2026-06-11).** Opt-in iteration behind the "✎ Redigera" toggle on a run card (statuses awaiting_approval/done/partial_success/failed) — the default pipeline is untouched. Capabilities: per-scene motion-prompt + duration editing (free at the gate; on finished runs the scene is marked `dirty` and the edit syncs onto the job so redos use the new text), single-clip redo (scene × character) and whole-scene redo via `POST /api/reengineer/{re_id}/scenes/{idx}/redo`, "▶ Animera om ändrade (N)" re-animates all dirty scenes (`POST .../animate_scenes`, status `reanimating` — own resume_all branch, NEVER auto-assembles), add scenes from an uploaded image OR video (mid-frame extracted; optional Whisper dialogue prefill into the prompt) via `POST .../scenes` (multipart), duplicate a scene at zero image cost (`POST .../scenes/{idx}/duplicate` — new `{src}__dup` scene_id + SceneAsset registered, approved variants cloned + auto-approved; only the new Kling clip costs), hard delete (`DELETE .../scenes/{idx}`) and reorder (`PATCH .../scene_order`), then "▶ Bygg ihop igen" reuses the existing assemble endpoint (finals overwritten; `finals_stale` highlights the button). All per-scene endpoints key on the LIST INDEX `idx` — scene_id is NOT unique in state.scenes. Enablers: the Swap movement locks (approve / variant retry / regen_scene_variants / retry_single_variant) are relaxed ONLY for `Job.from_reengineer` (origin prefix) — plain Swap jobs keep the locks; `retry_one_video` replaces clips IN PLACE so assembly picks new takes automatically.

**Per-clip retake/import works MID-RENDER (2026-08-04, Hugo's directive — "gör så man kan retry clips även om alla klipp för körningen inte är renderade").** `POST /api/reengineer/{re_id}/regen_clip` (✎↻ prompt) and `POST .../scenes/{idx}/import_clip` (📥 eget klipp) used to be gated on `{awaiting_assembly, done, partial_success, failed}`, so on a 90-clip run with 7 content-policy rejects among 18 still-rendering clips EVERY ✎↻ answered 409 "cannot edit while run status is 'animating'" for the ~40 min the rest took — and the only working recovery, the blunt "↻ Ta om misslyckade" (a plain job endpoint, never gated), cannot reword the prompt the model refused. Both endpoints now gate on `api._PER_CLIP_RUN_STATES` = that set **+ `animating` + `reanimating`**, and `import_clip`'s run-level `_ANIMATING` refusal is gone. The protection moved to the right GRAIN: the TARGET clip must be idle — `api._refuse_busy_clip` 409s a PENDING/PROCESSING clip in regen_clip (checked BEFORE any per-clip model/length override is persisted, mirroring Swap's `retry_video`), and `attach_imported_clip`'s existing `ClipBusyError` does the same for imports. Nothing else needed wiring: `_watch_video_phase` already waits for EVERY clip to go terminal, so a clip retaken mid-phase is simply waited for and the auto-build fires once at the end; `maybe_auto_assemble_after_clip` still refuses to fire during `animating`/`reanimating` (not in `_AUTO_REASSEMBLE_STATES`) so nothing builds early. CAVEAT: `video_phase_timeout_secs` (1 h) counts from the phase START — a very late retake can expire it and mark the run `failed`, which is cosmetic here (the clip keeps rendering and `failed` IS in `_AUTO_REASSEMBLE_STATES`, so the landing clip still auto-builds). Whole-scene redo (`/scenes/{idx}/redo`) stays blocked mid-render: it runs on the run-locked `reanimate` engine. Locked by `test_clip_retry_midrender.py` + `test_reengineer_regen_clip.py`.

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

Required for Swap (GPT Image) + video (Grok) + Editor (Whisper):
```
OPENAI_API_KEY=...
XAI_API_KEY=...
```

Optional — each unlocks one or more models in the Swap Step-4 / Reengineer
model pickers (registries in `runner_media.py`):
```
ANTHROPIC_API_KEY=...             # 🎬 AI Director (Claude Opus with vision; toggle on
                                  # the Swap + Reengineer forms). ~$0.05 per Director call.
GEMINI_API_KEY=...                # Nano Banana + Nano Banana Pro. (The Gemini-path Veo 3 /
                                  # Veo 3 Fast slugs were removed 2026-07-02 — submit_veo
                                  # was an unimplemented stub. Veo 3.1 Fast is fal-hosted —
                                  # see FAL_API_KEY, billed on fal, no Gemini quota.)
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
                                  # ALSO routes two VIDEO models through fal: "kling-v3"
                                  # (Kling 3.0, see KLING_V3_TIER) and "veo-3.1-fast"
                                  # (Veo 3.1 Fast i2v, clients/fal_veo.py — the only Veo
                                  # left after the Gemini-path stubs were dropped;
                                  # resolution via VEO_FAL_RESOLUTION). Both bill on fal.
ELEVENLABS_API_KEY=...            # ElevenLabs voice library (Editor + Step-6 compile voice
                                  # swap; per-character 🎤 preset voices)
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
IMAGE_SIZE=1008x1792              # true 9:16 AND ÷16 (1024x1792 letterboxed; locked by test_image_aspect.py)
IMAGE_CONCURRENCY=2               # fallback for providers without their own knob
IMAGE_CONCURRENCY_FAL=8           # per-PROVIDER swap-variant parallelism (2026-06-11):
IMAGE_CONCURRENCY_OPENAI=4        # the runner sizes its semaphore from the job's
IMAGE_CONCURRENCY_GEMINI=2        # effective model's provider — fal queues server-side
REMOTION_MAX_CONCURRENT_RENDERS=2 # process-wide cap on simultaneous `npx remotion render`
                                  # subprocesses (2026-06-11): Step-6 compile fans out per
                                  # character, and 11 ungated renders measured 430s median
                                  # each (vs 71s solo) + 30s delayRender frame timeouts +
                                  # a Chrome launch crash. All Remotion render paths funnel
                                  # through this gate in remotion_render.py.
REMOTION_CONCURRENCY=4            # browser tabs PER render (--concurrency). Measured on the
                                  # 18-core machine: 1 tab=99s, 4 tabs=29s, 8 tabs=22s for a
                                  # 39s 1080x1920 PurplePill render — 4 is the knee, and
                                  # gate×tabs = 8 Chrome tabs max by default.
REMOTION_TIMEOUT_MS=120000        # per-frame delayRender budget (--timeout). Remotion's 30s
                                  # default is too tight for cold OffthreadVideo seeks in
                                  # long Step-6 concat videos.
STT_ENGINE=scribe                 # speech-to-text for BOTH captions and video QC
                                  # (2026-08-03): "scribe" = ElevenLabs Scribe,
                                  # "whisper" pins the old whisper-1 path. Scribe
                                  # is the default for its REAL per-word timings
                                  # (see "Whisper word timestamps quirk" below);
                                  # whisper-1 remains the automatic fallback on
                                  # any Scribe failure, so a bad key or an
                                  # ElevenLabs outage never blocks a render.
STT_SCRIBE_MODEL=scribe_v2
SPEAKER_FIX_MODEL=claude-sonnet-4-6
                                  # 👥 speaker-attribution agent (2026-08-02): one
                                  # vision call per (FEMALE character × scene ticked
                                  # 👥) that rewrites the movement prompt so SHE says
                                  # the line. Same judge class as image QC — the job
                                  # is "read the frame, edit one clause", not creative
                                  # direction, so Opus isn't warranted.
SPEAKER_FIX_PRICE_USD=0.01        # per-call estimate recorded in state/calls.jsonl
SCENE_PEOPLE_MODEL=claude-sonnet-4-6
                                  # People detection for swap scenes
                                  # (scene_people.py, 2026-08-06): one vision
                                  # call per scene IMAGE — not per character —
                                  # answering "is it ambiguous which person
                                  # should be replaced here?". Same
                                  # read-the-frame judge class as image QC, so
                                  # Sonnet rather than Opus.
SCENE_PEOPLE_PRICE_USD=0.01       # per-call estimate recorded in state/calls.jsonl
VIDEO_QC_LANGUAGE_MAX_RETRIES=2   # wrong-language re-renders for a 🇪🇸/🇩🇪 character
                                  # (2026-08-02). INDEPENDENT of VIDEO_QC_MAX_RETRIES,
                                  # which Hugo runs at 0 (flag-only) for the fuzzy
                                  # garbled-speech check: a flagged character whose clip
                                  # came out ENGLISH is unusable, not a judgment call.
                                  # Exhausted → the clip FAILS loudly. 0 = flag-only.
VIDEO_QC_LANGUAGE_THRESHOLD=0.6   # how well the clip must match the ENGLISH SOURCE line
                                  # before it counts as "spoke the wrong language"
                                  # (it must also beat the translated line). No extra
                                  # API call — same Whisper pass as the speech check.
SWAP_STALL_TIMEOUT_SECS=600       # Reengineer image-phase watchdog: fail only when NO
                                  # progress (terminal flips / qc_attempts) for this long
SWAP_PHASE_MAX_SECS=7200          # absolute image-phase backstop (replaces old fixed 30 min)
VIDEO_DURATION_SECS=10
VIDEO_ASPECT_RATIO=9:16
VIDEO_RESOLUTION=720p             # Grok only — Kling tier is KLING_V3_TIER below
KLING_V3_TIER=pro                 # fal Kling v3 tier: "pro" (1080p, default since
                                  # 2026-06-12) or "standard" (720p, cheaper). Don't
                                  # flip while clips are in flight — fal request_ids
                                  # are endpoint-scoped, a resumed poll on the other
                                  # tier 404s (the ↻ retry recovers).
VEO_FAL_RESOLUTION=1080p          # fal Veo 3.1 Fast (veo-3.1-fast) render resolution:
                                  # "720p" / "1080p" / "4k". Default 1080p (Hugo
                                  # 2026-06-18 — parity with KLING_V3_TIER=pro so
                                  # mixed-model reels match); 720p is fal's own default.
VEO_SAFETY_TOLERANCE=6            # fal's OWN content-moderation dial on both Veo
                                  # 3.1 endpoints: "1" strictest … "6" least strict.
                                  # fal defaults to "4"; we send 6 (Hugo 2026-08-04,
                                  # after the 08-03 wave where 33 of 43 failed clips
                                  # were content-policy rejections of SPANISH dialogue
                                  # that passed verbatim in English). Relaxes ONLY
                                  # fal's layer — Google's Veo filter sits underneath
                                  # with no such dial, so a clip Google itself refuses
                                  # still fails (usually the silent
                                  # `no_media_generated`). An out-of-range value falls
                                  # back to 6, never to fal's stricter default. The
                                  # sibling `auto_fix` knob (fal rewrites a prompt that
                                  # trips the check) is deliberately NOT used: our
                                  # prompts carry the character's exact spoken line.
VEO_NEGATIVE_PROMPT="subtitles, captions, burned-in text, on-screen text, watermark, blur, distorted face, extra limbs"
                                  # Veo 3.1's OWN negative set (2026-08-03).
                                  # It used to be handed KLING_NEGATIVE_PROMPT,
                                  # which describes Kling's failure modes and
                                  # says nothing about Veo's signature defect:
                                  # burned-in, often garbled SUBTITLES on
                                  # dialogue clips. Those terms lead because
                                  # earlier terms weigh more. Empty → fal's own
                                  # default.
KLING_NEGATIVE_PROMPT="blur, distort, low quality, morphing face, frozen lips, warping fingers, extra limbs"
                                  # sent with every Kling submit (research 2026-06-12:
                                  # talking-head negative set; 5-8 terms beats long
                                  # lists). Empty → fal's own default. cfg_scale and
                                  # shot_type stay at fal defaults deliberately.
FFMPEG_CRF=16                     # every local re-encode in video_edit.py (trims,
FFMPEG_PRESET=medium              # concat, time-stretch, ASS captions). Was hardcoded
                                  # veryfast/CRF-20 → measured ~2-3 Mbps off a 21 Mbps
                                  # Kling master at the FIRST hop (2026-06-12 audit).
FFMPEG_TIMEOUT_SECS=3600          # hard kill+fail ceiling on EVERY video_edit._run
                                  # ffmpeg call (2026-07-02): a wedged encode (e.g. an
                                  # unbounded lavfi source) must fail loudly, never
                                  # hang the pipeline while the output fills the disk.
VIDEO_MODERATION_FALLBACK=0       # content-policy rescue for VIDEO clips
                                  # (2026-08-03, Hugo): OFF = a refused clip
                                  # fails loudly with the real reason on its
                                  # own model. 1 restores the old retry on
                                  # grok-imagine-1.5 (which drops end frames
                                  # and overrides the 🗣 language redirect).
BLACKBAR_FIX=1                    # automatic black-bar removal in every final build
                                  # (2026-07-24, Hugo's directive — see the feature
                                  # paragraph): 0 disables the whole mechanism.
BLACKBAR_MAX_CROP_FRAC=0.05       # max fraction of the frame (per axis) the fix may
                                  # crop away; beyond it the clip keeps today's
                                  # letterboxed/pillarboxed look (protects against
                                  # cropdetect misreading dark scenes and against
                                  # wildly off-aspect imports).
REMOTION_CRF=16                   # Remotion caption render quality (was Remotion
REMOTION_JPEG_QUALITY=100         # defaults: CRF ~23 + JPEG-80 frame captures).
                                  # Both are part of the render-cache SHA key.
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
   ┌────────┼──────────────────────────┬──────────────────────┐
   │        │                          │                      │
runner.py   runner_reengineer.py       video_edit.py     state.json (atomic)
(Swap/      (Reengineer + Swap-tab     (ffmpeg primitives:    + per-edit
 Animate     from_images: analyze →     trim, concat,          state.json files
 job flow)   swap → animate →           time_stretch,          on disk
            │assemble)                  caption render,
       pipeline.py                      silence-detect,
       (generate/edit                   Whisper transcribe)
       +wait_for_video)
```

- FastAPI process. `BackgroundTasks` runs async work; OpenAI/Grok client calls are sync so they go through `asyncio.to_thread`.
- `events.py` — in-process pub/sub keyed by `job_id`. WebSocket clients subscribe; runner publishes.
- `state.py` — atomic JSON persistence with opt-in SQLite backend (`USE_SQLITE_STATE=1`).
- `video_edit.py` — every ffmpeg invocation we make: trim, concat, time-stretch with `atempo`, caption render via `subtitles` filter against generated ASS, Whisper transcribe, silence detect, frame extraction.

---

## Module map

```
src/character_swap/
├── api.py             — FastAPI app: every CRUD endpoint + WebSocket
├── runner.py          — Swap-flow runner: per-(scene, char) variants, edit, multi-video
├── runner_media.py    — Model registry ONLY since the 2026-07-02 de-scope (the free-form
                         tab runners are gone): IMAGE_MODELS / VIDEO_MODELS (+ per-model
                         duration specs) / END_FRAME_VIDEO_MODELS, plus a minimal
                         AUDIO_MODELS whose sole purpose is the elevenlabs-vc
                         availability flag the frontend's voice-swap pickers gate on
├── pipeline.py        — Pure primitives: generate_image, generate_variant (multi-model
                         image dispatch — gpt-image / grok-image / nano-banana /
                         nano-banana-pro), edit_image, submit_video + wait_for_video
                         (multi-model video dispatch — Grok / Veo 3.1 Fast (fal) /
                         Kling / Runway / Luma / Pika / Hailuo / Sora / Wan / Seedance /
                         Higgsfield), GENERATION_PROMPT
├── video_edit.py      — ffmpeg primitives + Whisper + caption templates (ASS engine +
                         Remotion engine branch) + WPM helpers + time_stretch +
                         extract_last_frame + apply_timeline (CapCut) +
                         assemble_clips (2026-06-12: onset-trim + interior-silence
                         trim + scale + concat in ONE encode — the shared Editor
                         pipeline's first generation; every local encode uses
                         _enc_v() = FFMPEG_CRF/FFMPEG_PRESET)
├── remotion_render.py — Python→Node bridge for the Remotion caption engine. Calls
                         `npx remotion render` as a subprocess; SHA-256 caches outputs
                         under `output/cache/remotion/<hash>.mp4`. Wrapped in
                         `call_log.record(phase="remotion_render", ...)`. A process-wide
                         threading.BoundedSemaphore (REMOTION_MAX_CONCURRENT_RENDERS=2)
                         gates simultaneous renders — every caller (Step-6 compile,
                         rerender, auto_edit, timeline) funnels through it, each render
                         gets REMOTION_CONCURRENCY=4 browser tabs, the cache is
                         re-checked after queueing, and queue_wait_secs is logged
                         separately from latency_ms.
├── rerun.py           — ↻ Kör om med nya karaktärer (Hugo 2026-08-08): builds a
                         BRAND NEW run's state from an existing one's scene plan +
                         a different cast, copying every inherited file into the new
                         run dir (never referencing the parent's, which DELETE
                         rmtree's). Copies no per-character state, so the 🗣 language
                         redirect is re-resolved for the new cast rather than
                         inherited. RerunError = loud refusal before any credit.
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
                         MediaGeneration (kind=editor saved reels + legacy free-form
                         rows — GenKind.AVATAR/AUDIO enum members and the
                         avatar_id/voice_id/voice_provider fields are kept ONLY so
                         old rows still deserialize; ChatSession is deleted),
                         AppState + StrEnums
├── config.py          — Settings via pydantic-settings
├── images.py          — sha256, base64, atomic write/copy
├── call_log.py        — JSONL call logger (now also bills director_swap / director_movement)
├── prompt_enrich.py   — Cheap (✨) prompt expansion via gpt-4o JSON-mode
├── speaker_fix.py     — 👥 Speaker-attribution agent (Hugo 2026-08-02): one Claude
                         Sonnet vision call per (FEMALE character × scene ticked
                         "två personer i bild") that rewrites the movement prompt so
                         she — not the man beside her — says the line. Gate lives in
                         needs_speaker_fix(); ALL failures raise SpeakerFixError and
                         fail the clip loudly.
├── scene_people.py    — Who is in a swap scene (Hugo 2026-08-06): one cheap Sonnet
                         vision call per scene IMAGE → {multi_person, people[
                         {position, description}]}, the same shape the Director's
                         metadata used, so the person-choice gate consumes it
                         unchanged. Callable from ANY path that introduces a scene
                         (added scenes, Director-OFF runs) — detection used to live
                         only inside the Director's one run-creation call. Advisory:
                         every failure → None → the scene generates anyway behind
                         pipeline.CAST_LOCK + the QC person-count check.
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
                            (clamped to [5, 15]).
    ├── elevenlabs.py     — list_voices + text_to_speech + voice_changer +
                            transcribe (Scribe STT — the DEFAULT speech-to-text
                            engine since 2026-08-03; whisper-1 is the fallback)
    ├── google_genai.py   — Nano Banana / Nano Banana Pro via Gemini's REST
                            `generateContent` endpoint (httpx, no SDK dep). The Veo
                            submit stub remains in the file but is UNREGISTERED —
                            veo/veo-3-fast slugs removed 2026-07-02. Locked until
                            GEMINI_API_KEY.
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
├── remotion.config.ts     — Chromium config, concurrency=4 (manual-run default; the
                             Python bridge always passes --concurrency explicitly)
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
└── app.js          — Studio component (all tabs, WebSocket client,
                     drag/drop/paste, status toast, sidebar thumbnails, WPM controls,
                     CapCut timeline, per-variant retry, etc.)

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
output/generations/<gen_id>/
                    — ref images + results for LEGACY free-form rows only (the tabs
                      are gone; new MediaGeneration rows are Editor saved reels whose
                      files live under output/editor/<edit_id>/)
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

**Background source = the CHARACTER image by default (2026-06-21, Hugo's directive — supersedes Option-B-by-default for the swap phase).** Swap + Reengineer now STANDARDLY take the output background from the CHARACTER reference image: the scene supplies only pose/action/framing/held props, the person is relit to the character's own environment. A per-job opt-out restores the old "preserve the scene background" behavior, and an explicitly-uploaded replacement ("Image 3" / `extra_reference_path`) still wins over both. Mechanism: a 3-valued mode `pipeline.SWAP_BACKGROUND_MODES = ("scene","character","replacement")`; the prompt builders (`build_edit_swap_prompt` / `build_gpt_id_swap_prompt`) take `background_mode` (legacy `background:bool` kept, `True`==`"replacement"`); `pipeline.stock_swap_prompts()` enumerates every stock prompt across modes so `_dispatch_variant` rebuilds stock prompts for the resolved mode while custom prompts pass through; `runner._swap_background_mode(job)` resolves precedence (`extra_reference_path` → replacement, else `Job.background_source` "character"|"scene"); the vision-QC judge gets `background_replaced=true` for any non-scene mode so it never fails the (intentionally) changed background. `Job.background_source` defaults to `"character"`; `CreateJobBody.background_source`, `GET /api/swap/defaults?...&background_source=` (WYSIWYG — the box shows the mode the engine runs), and the Reengineer `background_source` Form param + run-state carry the choice; the three Director systems (`direct_swap` / `direct_reengineer_swap` / `direct_scene_prompt_rewrite`) are mode-aware. UI: a "Background from: Character image (default) | Keep the scene's background" dropdown on the Swap-from-images + Reengineer forms. Locked by `test_character_background.py`.

**Per-project override**: `ProjectAsset.default_prompt: str | None`. When set, jobs created in that project inherit it instead of the global default. `GET /api/swap/defaults?project_id=...&image_model=...` returns `{prompt, global_prompt, project_prompt, image_model}` so the frontend can show both the active and the global default.

**WYSIWYG identity-first prompts (2026-06-16, Hugo's directive).** The Swap tab + Reengineer default to `gpt2-id-swap`, which RUNS prompts identity-first (Image 1 = person, Image 2 = scene) via the flipped reference order. Everything the USER sees/edits (the Step-2 box default, the ✎↻ / 🪄 modals, reopened jobs) is now shown in that engine's IDENTITY-FIRST view so the box matches what the engine runs. **The whole generation backbone stays SCENE-FIRST canonical and unchanged** — `job.prompt`, the cached Director plan, `GeneratedImage.prompt`, `_kick_char`, and `pipeline._dispatch_variant` all store/reason scene-first; dispatch still calls `_flip_image_roles` to turn scene-first → identity-first for gpt2-id-swap at gen time, and the AI Director still reasons scene-first. ONLY the user-facing boundaries flip, via `api._flip_swap_orientation_for_idfirst(prompt, image_model)` (symmetric — flips iff model == `gpt2-id-swap`; no-op for gpt-image/fal): get_swap_defaults (engine-aware default → `build_gpt_id_swap_prompt`), `_job_to_dict`'s `prompt_display` (job + per-variant), the ✎↻ modal prefill, the input side of create_job / patch_job / retry_variant / regenerate_scene, the Reengineer `swap_prompt` (display) + `rewrite_prompt` (Director I/O) + `regen_images` (input) endpoints, and project-default save/inherit (stored scene-first canonical). No migration / no per-job flag: old jobs keep working because storage orientation never changed. Locked by `test_wysiwyg_idfirst.py` (incl. the round-trip: what the user types == what the engine runs).

---

## AI Director

Opt-in Claude/Opus agent that writes tailored per-variant prompts. Toggle (🎬) sits next to ✨ enrich on the Swap Step-2 form (+ the Reengineer upload checkbox — see the Reengineer AI Director paragraph above). Disabled when `ANTHROPIC_API_KEY` isn't set; UI greys out the checkbox + shows a tooltip.

### What it does

- **Swap** — ONE Claude Opus call before image gen. Sees every character image + every scene image with vision. Uses tool-use (`submit_swap_plan`) to return a complete plan: a tailored prompt per (character × scene × variant_index). Plan is cached as JSON on `Job.director_prompts_json` so retries / resumes don't re-bill. `runner._kick_char` reads the cache and assigns each `GeneratedImage.prompt` from `plan.lookup(char_id, scene_id)[variant_idx]`. Falls back to enrich → raw → `GENERATION_PROMPT` per slot if the plan is missing or fails.
- **Swap movement (Step 4)** — second Claude call with the scene image + every approved variant image + the user's per-scene movement text. Returns one cinematic shot prompt per scene; merged into `enriched_movement_prompts` so the existing per-scene resolver in `run_video_synthesis` transparently picks it up.

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

GET    /api/generations/models                 model registry (runner_media.py)
GET    /api/generations?kind=editor            list — saved Editor reels (+ legacy
                                                free-form rows; POST + retry were
                                                deleted in the 2026-07-02 de-scope)
GET    /api/generations/{gen_id}               single
DELETE /api/generations/{gen_id}

GET    /api/swap/defaults?project_id=...       {prompt, global_prompt, project_prompt, ...}
GET    /api/elevenlabs/voices

POST   /api/editor/auto_edit, /multi_auto_edit, /trim_silences, /captions, /rerender,
       /timeline_render
POST   /api/editor/drive_export/bootstrap      one-time Drive write-scope OAuth
POST   /api/editor/{edit_id}/drive_export      upload the edit's final MP4 to Google Drive
                                                (defaults to Character Swap/Editor/ + overwrite;
                                                 optional gen_id+slot persist the reel receipt)
POST   /api/jobs/{job_id}/characters/{char_id}/drive_push     body {variant: final|repurpose}
POST   /api/reengineer/{re_id}/chars/{char_id}/drive_push     → Character Swap/<char>/
GET    /api/reengineer/{re_id}/rerun_plan      prefill for the "↻ nya karaktärer" modal
POST   /api/reengineer/{re_id}/rerun           clone the scene plan onto a NEW cast as a
                                                brand new run (parent untouched); body:
                                                character_ids, scenes[{idx, motion_prompt,
                                                secs, direct, two_person, keep_end_frame,
                                                reuse_direct_clip, video_model}] + optional
                                                setting overrides (None = inherit)
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

### Whisper word timestamps quirk — why Scribe is now the default
`whisper-1` with `timestamp_granularities=["word"]` returns word durations that are mostly INTERPOLATED inside each segment — `word[i].end == word[i+1].start` for most adjacent words. Real silences only show up as gaps > ~0.4s. The WPM helpers in `video_edit.py` (`compute_wpm`) account for this by computing `active_secs = span − sum(long_gaps)` instead of summing per-word durations.

**This is what drove the 2026-08-03 engine switch.** Measured over 54 of Hugo's own clips, the share of adjacent word pairs with NO gap at all was 97% (en) / 91% (es) / 91% (de) for whisper-1 against 2% / 6% / 5% for ElevenLabs Scribe — i.e. whisper-1's per-word boundaries are mostly fabricated, and every Remotion caption template animates per word off exactly those numbers. Word accuracy moved the same direction (en 1.000 → 1.000, es 0.925 → 0.962, de 0.498 → 0.605, once digits-vs-words are normalized), and Scribe is cheaper ($0.22/h vs $0.36/h) and faster (~1.5s vs ~2.0s per clip). `STT_ENGINE=whisper` pins the old path; whisper-1 also stays the automatic fallback on any Scribe failure, so this is not a single point of failure.

**Language hint:** pass `language=` whenever the caller knows it (the Step-6 compile and Reengineer assemble read it off the character's 🗣 flag). On the German clips it lifted mean word-similarity 0.571 → 0.602, and 0.41 → 1.00 on one clip Scribe had otherwise read as Dutch. **Never force it in video QC's only pass**, though: the wrong-language check works by seeing the clip transcribe back to the ENGLISH source line, and a hinted pass would go blind on exactly the clips it exists to catch — `video_qc._transcribe` therefore runs an UNHINTED pass for that check plus a hinted one for the score. Gating on either engine's own detected language is not an option: whisper-1 called 4 of 20 German clips English, Scribe called 3 of 20 Dutch or English.

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
