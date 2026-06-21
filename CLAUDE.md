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
4. **Movement prompt** — **per-IMAGE rows** (one per approved image): each approved image gets its OWN motion prompt AND its own clip duration (the Higgsfield "per-slot" model), so every video can be completely different. Thumbnail + textarea + per-image duration picker per row, plus an "⤓ apply image 1 to all" convenience. **Video provider picker** (a job-wide DEFAULT) lets you pick any of: Grok Imagine, Veo 3, Veo 3 Fast, Veo 3.1 Fast (fal), Kling 2.0/2.1/2.5/2.6/3.0 + Pro/Master variants, Runway Gen-4/Gen-3, Luma Ray-2, Pika 2.2, Hailuo 01/02, Sora 2, Wan 2.1/2.2, Seedance, Higgsfield variants. **Per-clip model override (2026-06-18):** each scene row also has a small **Model** dropdown defaulting to "Samma som jobbet" — pick a different provider for one clip and that scene's duration options + generation follow it. Opt-in: empty → the job default; persisted as `Job.video_models_by_scene` (scene_id → slug), resolved per-clip in `runner._eff_video_model` at submit/salvage-repoll/resume + end-frame gating (a scene on a non-Kling model ignores its end pose — only `kling-v3` interpolates). `POST /movement` validates every chosen provider's key upfront. **Submit kicks off all approved images × M videos in parallel**, each scene using its effective provider. Backend: `POST /movement` accepts `movement_prompts_by_variant` (variant_id → prompt) + `durations_by_variant` (variant_id → secs); the runner resolves per-image override → per-scene prompt → fallback, and a per-scene `movement_prompts` is derived from the per-image dict for back-compat + the Step 6 compile. Per-image prompts are used verbatim (AI Director / enrich are skipped when they're set). The older per-scene path still works for jobs that send `movement_prompts`.
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

Quality is double-gated: (1) automatic vision-QC — every generated swap IMAGE is inspected by a Claude vision call (swap_qc.py — judge: Sonnet 4.6 by default since 2026-06-11, env `SWAP_QC_MODEL`; checks: right person? holding/doing the SAME thing as the scene (wrong-prop images passed the old Haiku judge)? broken/cutout?) and auto-regenerated on failure (first retry = minimal-change REPAIR of the failed image, then fresh re-roll + hint; SWAP_QC=0 disables), and every generated video CLIP is checked (video_qc.py: Whisper transcript vs expected dialogue — catches garbled TTS like 'baking goda' — + frame-sampled anatomy check; 1 retry, VIDEO_QC=0 disables); (2) human approval before any video is kicked off (video is the expensive step). QC never blocks: unavailable → skipped; exhausted retries keep the last output with a ⚠ qc_status chip. **Every QC-rejected take is now PRESERVED (Hugo 2026-06-20).** Before a retry overwrites the slot's file, the rejected image/clip is snapshotted to a `<stem>.qcrejectN.png|.mp4` sidecar and recorded on `GeneratedImage.qc_rejects` / `VideoVariant.qc_rejects` (a `QCReject` = path/reason/attempt/kind). They are serialized by `api._qc_rejects_dicts` (drops files that went missing) and rendered inline BY DEFAULT in the Swap/Reengineer approval strip — dimmed red thumbs for images, small players for clips, tooltip = the QC reason. Numbered by cumulative count so in-place regeneration (`retry_single_variant` / ✎↻ / 🪄, which reuse the variant_id) accumulates rather than clobbers; repair-mode reuses the snapshot as its edit input; the final exhausted take stays at `variant.path` (not duplicated into qc_rejects). Locked by `test_swap_qc.py` / `test_video_qc.py`. QC + retries run OUTSIDE the image-gen semaphore (2026-06-11) so a judging/retrying slot never starves the generation lanes; the semaphore is sized per provider (`IMAGE_CONCURRENCY_FAL=8` / `_OPENAI=4` / `_GEMINI=2`, fallback `IMAGE_CONCURRENCY=2`).

**GPT Image moderation = `low`, always (2026-06-16, Hugo's directive — not switchable).** Every GPT Image call hardcodes OpenAI's `moderation="low"` param in `openai_image._generate_once` — the permissive (but still filtered) tier, accepted on both the create and edit endpoints for gpt-image models. This is the FIRST-line filter, running before the ladder below: the API defaults to the stricter `auto`, which rejected far more than the consumer chatgpt.com product (~49% of swap calls were safety rejections), because chatgpt.com runs its own tuned moderation level you can't set via the API. Applies to every GPT path (Swap `gpt-image`, Swap/Reengineer `gpt2-id-swap`, free-form Image tab — all route through `_generate_once`). A defensive fallback drops the param + retries once only if a model rejects `moderation` as an unknown argument; a genuine content block still propagates to the ladder. Locked by `test_image_moderation.py`.

**Moderation ladder (2026-06-11; fallback opt-in since 2026-06-12; Director rewrite rung 2026-06-13).** When the chosen engine rejects a swap on content-policy grounds: the client first retries with two escalating append-only softeners (`content_policy.py`, rung 2 is a full fictional-film-production reframe). Then **RUNG A (default when `ANTHROPIC_API_KEY` is set): ONE Director moderation rewrite** — `prompt_director.direct_moderation_rewrite` (phase `director_rewrite`, ~$0.05) sees the scene frame + the ENGINE-EFFECTIVE prompt (`runner.engine_effective_swap_prompt`, shared with the scene-rewrite feature) + the rejection text, and rewords the prompt neutrally (same scene, same visual result — bodies/touch described clinically, one wholesome-context clause, never claiming anything visually false; camera-gaze ensured for reengineer jobs; style clauses stripped/re-appended) → retry on the SAME engine. Hugo validated the approach by hand in ChatGPT (the blocked "pinch back fat" scene generated fine with reworded prompt). Once per slot; recorded on `GeneratedImage.moderation_rewritten` + `variant.moderation_rewrite` event + violet 🪄 chip in the Reengineer strip; the reworded prompt persists on the slot (visible in ✎↻). Director unavailable/None → fall through. The old final rung — falling that one slot back to `nbp-swap` — remains **opt-in via `SWAP_MODERATION_FALLBACK=1`** (Hugo's "100% GPT Image 2" directive 2026-06-12): by default a still-rejected slot FAILS loudly with the moderation reason so the user can ↻ retry or reword, never switching engines. With the flag on, the rescue is loud as before: recorded on `GeneratedImage.fallback_model`, emitted as `variant.fallback`, purple ⇄ chip in Swap + Reengineer UIs. (Measured rationale for the rescue: 49% of gpt-image-2 swap calls were safety rejections burning the full ~131s render each; nbp-swap had 0 moderation failures on the same scenes.) The PIPELINE layer still has no cross-provider fallback — this is a sanctioned runner-level exception.

**Reengineer 🎬 AI Director (2026-06-11, opt-in checkbox at upload, OFF by default).** ONE Claude (Opus) call LOOKS at every detected scene frame and writes a tailored COMPACT swap prompt per SCENE — naming the actual props with position/approximate size in frame, anchoring the camera distance/crop, and matching the scene's light (exactly the static template's observed drift modes: wrong props, zoomed-out framing). Implemented in `prompt_director.direct_reengineer_swap` (+ `plan_from_scene_prompts` replicates per-scene prompts across characters into the standard `SwapDirectorPlan` so `_kick_char`'s existing precedence consumes them unchanged; gpt2-id-swap's dispatch mechanically flips Image 1↔2). Wired in `_create_job_and_swap` (cached on `Job.director_prompts_json` → crash-resume never re-bills); ANY failure → None → normal template chain. ~$0.10 + ~1 min per run; requires `ANTHROPIC_API_KEY`. Prompts stay ≤~120 words per the bake-off's compact-prompt lesson.

**Reengineer per-scene END FRAMES (2026-06-13, Hugo's directive).** "🎯 End frame" control on every scene row (gate, awaiting_assembly and ✎ edit mode; kling-v3 runs only): upload an end pose AFTER the scenes exist → the existing job-level end-frame machinery (2026-06-08) swaps EVERY character into it (`runner.regen_scene_end_frames`, no QC, errors on `end_frame_errors`) → the scene's Kling 3.0 clip interpolates start → swapped end frame. NOT a new scene — it rides on the existing entry via the job-level endpoints (`POST/DELETE /api/jobs/{id}/scenes/{sid}/end_frame`, `POST .../regen_end_frame`) keyed by scene_id. Enablers: (1) those three endpoints' movement lock is **relaxed for `Job.from_reengineer`** (plain Swap jobs stay locked); (2) `retry_one_video` and `generate_more_videos` — the paths reanimate uses — now resolve the end frame via the shared `runner._resolve_end_image` helper (they silently DROPPED it before, so Kling retries lost the end frame even in plain Swap); (3) post-gate the UI marks the scene `dirty` after set/regen/clear so "▶ Animera om ändrade" picks it up. Per-char swapped end-frame thumbs render inline with ↻ regen / ⇪ replace / ✕ clear; the upload counts as no new scene and costs one swap image per character.

**Reengineer Kling auto length = ceil + 1 (2026-06-13, Hugo's directive — supersedes 06-12 plain ceil).** AUTO Kling clip length is the ORIGINAL scene clip's length rounded UP to the SECOND-next whole second ("6,4 s original → 8 s Kling"), clamped [3, 15] — one breath of margin, never the old speech-fitted extension. Manual `kling_secs` override still wins exactly as typed. `runner_reengineer._kling_duration` + the app.js `klingDuration` mirror (sync-pinned by `test_kling_duration_js_mirror_in_sync`).

**QC HEAD-RULER TEST (2026-06-13).** The vision-QC judge measures the person's head height as a fraction of frame height in SCENE vs RESULT, states both estimates, and FAILS (WRONG FRAMING / ZOOM → re-roll) when RESULT's fraction is < ⅔ of SCENE's (zoomed out) or > 1.5× (zoomed in) — a hard numeric rule NEVER relaxed by background_replaced / outfit flags / user intent (unless the intent explicitly requests a different framing). Added after a tight chest-up scene came back as a staged waist-up portrait at half subject scale and PASSED QC (re_a83822b0d1 scene 4); the strengthened judge was validated against that exact image (now fails: outfit mismatch + mangled flag canton — multiple real defects).

**Reengineer camera-gaze policy (2026-06-13, Hugo's directive).** EVERY image generated in the Reengineer flow has the person looking directly into the camera, regardless of the original gaze. Three layers: (1) the static templates already contain the sentence ("They look directly into the camera with a natural, composed expression, even if the original person was not." — `build_edit_swap_prompt` + `build_gpt_id_swap_prompt`); (2) `REENGINEER_SWAP_DIRECTOR_SYSTEM` and `SCENE_REWRITE_DIRECTOR_SYSTEM` mandate the sentence verbatim (the old PERFORMANCE-ANCHOR rule that preserved the observed gaze is gone), with `prompt_director.ensure_camera_gaze()` as the code-level guarantee on every Director-written prompt; (3) the QC judge gets `camera_gaze=true` for `Job.from_reengineer` jobs — it ENFORCES camera gaze (WRONG GAZE = looking away) instead of failing it as a scene mismatch like before. Plain Swap-tab QC behavior is unchanged. Hand-gesture anchoring/judging is unaffected.

**Scene-level image change for ALL characters (2026-06-13).** "🪄 Ändra bild" button on every scene row (visible at the gate and in edit mode): the user describes the change in plain language ("byt ut kaffemuggen mot ett glas vatten") → `POST /api/reengineer/{re_id}/scenes/{idx}/rewrite_prompt` runs ONE Claude call (`prompt_director.direct_scene_prompt_rewrite`, phase `director_rewrite`, ~$0.05) that sees the scene frame + the scene's current swap prompt (style clauses stripped before, re-appended in code after; on background-replacement runs the Director ALSO sees Image 3 + gets the STRICTLY-FORBIDDEN-original-background rule) and rewrites the prompt with only that change applied — PURE PREVIEW, shown editable in the modal (Director failure → 502, never blocks; a second Director pass rebases on the modal's `current_prompt` so iterating doesn't lose the previous step) → `POST .../regen_images` regenerates the scene's image for EVERY character with the new prompt: per character the approved slot (else first ready, else first failed; in-flight slots never picked) regenerates IN PLACE via `retry_single_variant` (ONE shared provider semaphore, approvals withdrawn first, QC as usual), the prompt persists on each slot AND in the cached Director plan for that scene (synthesized via `plan_from_scene_prompts` when no plan exists — the Director-off default), post-gate the scene is marked `dirty` + finals go stale — the normal re-approve → "▶ Animera om ändrade" → "▶ Bygg ihop igen" chain takes over. Response carries `regen_variants` {char_id: variant_id} for client cache-busters. **Review-hardened (2026-06-13, 9 adversarially-verified fixes):** (1) the user's `change` text rides to the QC judge as the slot's `qc_intent` (`GeneratedImage.qc_intent`, `user_intent=(variant.qc_intent or job.prompt)`) so QC never "repairs" the requested deviation back — also fixes the per-image ✎↻ edited-prompt path; (2) prompts shown/rewritten are the ENGINE-EFFECTIVE text via `_engine_effective_swap_prompt` (stock templates like GENERATION_PROMPT are substituted at dispatch with gpt2-id-swap's compact prompt / EDIT_SWAP_PROMPT — editing the stored stock string rewrote text the engine never saw); prefill comes from `GET .../scenes/{idx}/swap_prompt` (`?variant_id=` narrows for the ✎↻ modal); (3) `retry_single_variant` re-points slots that SHARE a file with a clone (duplicated scenes) to their own path before regenerating; (4) `_collect_clips` reports a scene with slots but no approval as a LOUD coverage gap ("ingen godkänd bild") instead of silently shipping a final without it; (5) `_do_reanimate` persists only the idxs that actually spawned clip tasks, so an all-pairs-skipped reanimate no longer consumes the dirty flag, and `_reMarkVariantSceneDirty` also fires at `awaiting_assembly`.

**Reengineer "Bygg ihop igen" REFUSES incomplete finals (2026-06-17, Hugo's directive).** The rebuild must NEVER silently ship a stale or shorter final. Two layers: (1) the `POST /api/reengineer/{re_id}/assemble` endpoint pre-flights coverage via `runner_reengineer._assembly_gaps(state, job)` and returns **HTTP 409 `{code:"incomplete_rebuild", message, dirty, hard, pending}`** (instead of scheduling the build) whenever the run can't produce complete, up-to-date finals — a scene edited-but-not-reanimated (`dirty` → its existing clip is stale), a failed/missing clip or an un-approved image (`hard`), or a clip still rendering (`pending`); the message names which scene/character to fix ("▶ Animera om ändrade först" / "ta om klippet") and existing finals are left untouched. `_assembly_gaps` mirrors `_collect_clips`' per-(char,scene) inclusion rules exactly so the gate and the build never disagree. (2) `_do_assemble` itself now FAILS a character loudly (status `failed`, missing scene named) instead of concatenating a shorter video when a scene's clip is still missing after the bounded coverage-wait — guards the auto-assemble path and any direct call too (supersedes the old soft `coverage_warning` that built short + warned). Hugo chose "refuse, don't auto-reanimate": the user fixes the named gap, then rebuilds. Locked by `test_assemble_coverage.py` + `test_reengineer_assemble_editor.py`.

**Reengineer EDIT MODE (2026-06-11).** Opt-in iteration behind the "✎ Redigera" toggle on a run card (statuses awaiting_approval/done/partial_success/failed) — the default pipeline is untouched. Capabilities: per-scene motion-prompt + duration editing (free at the gate; on finished runs the scene is marked `dirty` and the edit syncs onto the job so redos use the new text), single-clip redo (scene × character) and whole-scene redo via `POST /api/reengineer/{re_id}/scenes/{idx}/redo`, "▶ Animera om ändrade (N)" re-animates all dirty scenes (`POST .../animate_scenes`, status `reanimating` — own resume_all branch, NEVER auto-assembles), add scenes from an uploaded image OR video (mid-frame extracted; optional Whisper dialogue prefill into the prompt) via `POST .../scenes` (multipart), duplicate a scene at zero image cost (`POST .../scenes/{idx}/duplicate` — new `{src}__dup` scene_id + SceneAsset registered, approved variants cloned + auto-approved; only the new Kling clip costs), hard delete (`DELETE .../scenes/{idx}`) and reorder (`PATCH .../scene_order`), then "▶ Bygg ihop igen" reuses the existing assemble endpoint (finals overwritten; `finals_stale` highlights the button). All per-scene endpoints key on the LIST INDEX `idx` — scene_id is NOT unique in state.scenes. Enablers: the Swap movement locks (approve / variant retry / regen_scene_variants / retry_single_variant) are relaxed ONLY for `Job.from_reengineer` (origin prefix) — plain Swap jobs keep the locks; `retry_one_video` replaces clips IN PLACE so assembly picks new takes automatically.

**Reengineer "kör samma recept för fler karaktärer" (2026-06-21, Hugo's directive).** Once a run is FINISHED (status done/partial_success/failed), the "👥 Fler karaktärer" button on the run-header cluster opens a picker (reuses the creation multiselect + per-character source-override popover, excluding chars already in the run) to run the EXACT same recipe for ADDITIONAL characters — they join the SAME run as new columns. The new chars get char 1's CURRENT state: same scenes + per-scene swap prompts (incl. 🪄 ändra-bild edits) + motion prompts + durations + per-clip models + end-frame poses + background + language. Two action buttons — `⚡ Kör helautomatiskt` (auto-approve → animate → assemble, no stops) and `✔︎ Godkänn i steg` (stop at the image-approval gate as usual). char 1 is NEVER touched: EVERY per-character phase is explicitly scoped to the new chars via `state["add_scope_char_ids"]` — swap gen gets `char_ids=new_ids`; `_auto_approve(char_ids)` + `_do_animate`'s status-flip only touch the new chars + `run_video_synthesis(char_ids)` only animates them (so an existing DONE char is never re-animated/re-billed) + shared direct clips are reused; `_watch_swap_phase` scopes its "every variant failed" guard + consistency QC; `_watch_video_phase`/`_videos_terminal(char_ids)` wait for the NEW chars' clips (an existing char's already-DONE videos don't end the phase before the new clips exist — matters with per-scene end frames); `_assembly_gaps(scope)` + `_do_assemble` gate and build only the new chars and seed/preserve existing finals, then clear the scope (and restore the run's pre-add `auto_mode`). The scope is KEPT on a swap-phase failure so a later manual rebuild stays scoped. A concurrent-add guard (`_ADDING`, acquired synchronously in the endpoint) blocks double-submits. Recipe replication (`_ref_scene_prompt`) falls back to any stored variant prompt so partial/failed-origin scenes still clone the real recipe; the Director plan is MERGED (new chars only) so char 1's cached plan is never overwritten. Two reviews (`/code-review high` + max) hardened this; locked by `test_reengineer_add_characters.py`. Endpoint `POST /api/reengineer/{re_id}/add_characters` ({character_ids, character_source_image_ids?, auto?}) → `runner_reengineer.add_characters`, which builds the new `JobCharacter`s, replicates the recipe into the Director plan via `plan_from_scene_prompts` from the reference char's approved slot prompts (fresh `prompt_version`), then mirrors `_resolve_people_and_swap`'s swap-watch tail (shared `_watch_swap_phase` gate + stall watchdog). One enabler: `runner.run_image_generation`'s movement lock now also relaxes for `Job.from_reengineer` (it was the lone swap-phase entry that didn't). Locked by `test_reengineer_add_characters.py`.

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
(14 image + 18 video models registered):
```
ANTHROPIC_API_KEY=...             # 🎬 AI Director (Claude Opus with vision; opt-in toggle
                                  # on Swap, Image, Video tabs). ~$0.05 per Director call.
GEMINI_API_KEY=...                # Nano Banana + Nano Banana Pro + Veo 3 + Veo 3 Fast
                                  # (Veo 3.1 Fast is fal-hosted — see FAL_API_KEY,
                                  # billed on fal, no Gemini quota.)
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
                                  # (Veo 3.1 Fast i2v, clients/fal_veo.py — the Gemini
                                  # path only carries Veo 3 / Veo 3 Fast; resolution via
                                  # VEO_FAL_RESOLUTION). Both bill on the fal key.
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
KLING_NEGATIVE_PROMPT="blur, distort, low quality, morphing face, frozen lips, warping fingers, extra limbs"
                                  # sent with every Kling submit (research 2026-06-12:
                                  # talking-head negative set; 5-8 terms beats long
                                  # lists). Empty → fal's own default. cfg_scale and
                                  # shot_type stay at fal defaults deliberately.
FFMPEG_CRF=16                     # every local re-encode in video_edit.py (trims,
FFMPEG_PRESET=medium              # concat, time-stretch, ASS captions). Was hardcoded
                                  # veryfast/CRF-20 → measured ~2-3 Mbps off a 21 Mbps
                                  # Kling master at the FIRST hop (2026-06-12 audit).
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

**Per-project override**: `ProjectAsset.default_prompt: str | None`. When set, jobs created in that project inherit it instead of the global default. `GET /api/swap/defaults?project_id=...&image_model=...` returns `{prompt, global_prompt, project_prompt, image_model}` so the frontend can show both the active and the global default.

**WYSIWYG identity-first prompts (2026-06-16, Hugo's directive).** The Swap tab + Reengineer default to `gpt2-id-swap`, which RUNS prompts identity-first (Image 1 = person, Image 2 = scene) via the flipped reference order. Everything the USER sees/edits (the Step-2 box default, the ✎↻ / 🪄 modals, reopened jobs) is now shown in that engine's IDENTITY-FIRST view so the box matches what the engine runs. **The whole generation backbone stays SCENE-FIRST canonical and unchanged** — `job.prompt`, the cached Director plan, `GeneratedImage.prompt`, `_kick_char`, and `pipeline._dispatch_variant` all store/reason scene-first; dispatch still calls `_flip_image_roles` to turn scene-first → identity-first for gpt2-id-swap at gen time, and the AI Director still reasons scene-first. ONLY the user-facing boundaries flip, via `api._flip_swap_orientation_for_idfirst(prompt, image_model)` (symmetric — flips iff model == `gpt2-id-swap`; no-op for gpt-image/fal): get_swap_defaults (engine-aware default → `build_gpt_id_swap_prompt`), `_job_to_dict`'s `prompt_display` (job + per-variant), the ✎↻ modal prefill, the input side of create_job / patch_job / retry_variant / regenerate_scene, the Reengineer `swap_prompt` (display) + `rewrite_prompt` (Director I/O) + `regen_images` (input) endpoints, and project-default save/inherit (stored scene-first canonical). No migration / no per-job flag: old jobs keep working because storage orientation never changed. Locked by `test_wysiwyg_idfirst.py` (incl. the round-trip: what the user types == what the engine runs).

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
