# Character Swap Studio — UX Improvements

A prioritized plan based on a full app audit (Swap, Editor, B-roll, Image, Video, Audio, Avatar tabs) and research into how Higgsfield AI, Krea, and Leonardo solve similar problems.

---

## Do these 3 first

These three together cost ~half a day and remove the biggest daily friction points.

| # | Improvement | Why it's #1–#3 |
|---|---|---|
| **1** | **Stop clearing prompt + reference images on form submit** in Image / Video / Audio / Avatar tabs | Single highest-leverage change. Today every successful generation wipes the user's inputs, forcing them to retype/re-upload to iterate. Hugo's actual workflow is "drop ref → gen → tweak prompt → gen again" — the form state is the workflow. Trivial code change, immediate quality-of-life. |
| **2** | **Auto-select sensible defaults** (first template, first model, last-used voice) + **promote the "reuse" button to a prominent action** | Today the user opens Editor and sees an empty template grid — no default selected, the Generate button is disabled but no helper text says why. And the existing `reuseImageGen` / `reuseVideoGen` / `reuseAvatarGen` handlers exist but are buried as tiny grey links in history cards. These exist already — just make them discoverable. |
| **3** | **Persistent bottom-right status toast** for in-flight generations across tabs | Long-running B-roll (5-15 min) and Swap (10-20 min) jobs currently require leaving the tab focused to see progress. A non-blocking status card with "B-roll: 8/13 clips · click to jump" lets users start a new job, switch tabs, and not lose context. This is the single biggest perception-of-speed win and it's the foundation for everything else. |

---

## Top 10 ranked

Scored: **Impact** (1–5, how much it improves daily workflow) × **Effort** (1–5, how much code).
Ranked by Impact × (6 − Effort).

### 1. Preserve form state after generation ⭐ DO FIRST
**Impact: 5 · Effort: 1**

- **Current**: `submitImageGen`, `submitVideoGen`, `submitAudioGen`, `submitAvatarGen` all clear prompt / refs / script on success. See `web/app.js` — search for `this.imageGen.prompt = ''`, `this.audioGen.script = ''`, `this.avatarGen.script = ''`.
- **Proposed**: Don't clear. Add a small "✕ Clear form" button next to Generate for users who want a fresh start. Persist `imageGen.prompt`, `videoGen.prompt`, `audioGen.script`, `avatarGen.script`, and all `referenceImages` to localStorage so they survive page reloads too.
- **Files**: `web/app.js` (4 submit handlers + init), `web/index.html` (add Clear button to each form)
- **Higgsfield parallel**: Higgsfield's prompt input is persistent and the form does NOT clear — users iterate inside the same prompt cell with arrow keys to scroll history.

### 2. Default-select first template / first available model / last-used voice ⭐ DO FIRST
**Impact: 4 · Effort: 1**

- **Current**: Editor's template gallery has no selection when first loaded — user must click. B-roll model defaults to `grok-imagine` but no visual indication. Voice pickers in Audio/Avatar reset on tab switch.
- **Proposed**: When `editorTemplates` loads, auto-set `editor.template = editorTemplates[0].slug` (currently `popout-yellow`). Persist last-used voice ID per tab in localStorage. Visually highlight the selected default in every picker with the existing indigo ring (already styled, just not applied on init).
- **Files**: `web/app.js` (`loadEditorTemplates`, `init`, voice loaders); minor changes only.

### 3. Promote the existing "reuse" buttons ⭐ DO FIRST
**Impact: 4 · Effort: 1**

- **Current**: `reuseImageGen` / `reuseVideoGen` / `reuseAvatarGen` exist as small `text-indigo-600 hover:underline` text links in the history-card action row. They're THE primary iteration accelerator and nobody can find them.
- **Proposed**: Make them a real button with a ↺ glyph, same visual weight as ↓ download. Add tooltip "Load this prompt + settings back into the form". Move them to the front of the action row so they're visually first.
- **Audio history is missing the reuse button entirely** (Agent 2 confirmed `reuseAudioGen` exists in app.js but isn't wired in index.html). Add it.
- **Files**: `web/index.html` (4 history grids).
- **Higgsfield parallel**: Higgsfield surfaces "remix" prominently on every gallery card — it's the second action after download.

### 4. Persistent cross-tab status toast for in-flight generations
**Impact: 5 · Effort: 3**

- **Current**: A B-roll job at `awaiting_approval` or `generating_clips` has no visible indicator if the user is on a different tab. The Swap flow's video animation step is similarly opaque outside its tab.
- **Proposed**: A fixed bottom-right card that polls all in-flight jobs across tabs. Shows: "B-roll br_xxx: 8/13 clips · 4 min elapsed · click to view". Same card for Swap jobs, Image/Video/Audio/Avatar gens that are `pending` or `running`. Click navigates to that tab and scrolls to the job. Auto-dismisses when all complete.
- New top-level Alpine state slice `liveJobs: { ... }` that aggregates from `brollHistory`, `imageHistory`, `videoHistory`, `audioHistory`, `avatarHistory`, and the active Swap job. Single 5s polling loop instead of per-tab loops.
- **Files**: new component in `web/index.html`, new state in `web/app.js`, possibly a new endpoint `GET /api/jobs/active` to consolidate the existing per-kind polls.
- **Higgsfield parallel**: Higgsfield's queue uses a persistent status indicator that lets users start the next generation without waiting for the current one to render. Krea uses bottom-right toast for the same purpose.

### 5. Search + filter on every history grid
**Impact: 4 · Effort: 2**

- **Current**: Every history panel (Image, Video, Audio, Avatar, B-roll) is a flat reverse-chronological list. To find "that wolf image I made 3 days ago" you scroll until your eyes bleed.
- **Proposed**: A single text input at the top of each history grid that filters by prompt text (case-insensitive). Add a "model" dropdown filter (e.g., show only Veo videos). For B-roll: filter by status (done / partial / failed). Frontend-only filter — no backend changes needed since all the data is already in the loaded arrays.
- **Files**: `web/index.html` (5 history grids), `web/app.js` (5 computed filter properties).
- **Bonus**: Add a star/favourite toggle that persists to localStorage so users can flag "keepers" without leaving them in chronological hell.

### 6. Higgsfield-style left sidebar with cross-kind generation thumbnails
**Impact: 5 · Effort: 4**

- **Current**: The left sidebar exists for the Swap flow's project/job tree. Free-form tabs (Image/Video/Audio/Avatar/B-roll) have no equivalent — each has its own grid below the form.
- **Proposed**: Expand the left sidebar to host a "Recent generations" pane at the bottom that shows tiny thumbnails across ALL kinds, newest first. Click a thumbnail to scroll to / open that item. Hovering shows the prompt. This is THE single biggest "AI studio" UX pattern from Krea and Leonardo and is what makes their tools feel professional.
- The existing sidebar's project/job tree stays at the top; the cross-kind thumbnails go below it with a collapsible header.
- **Files**: `web/index.html` (sidebar component), `web/app.js` (`recentMedia` computed property aggregating across history arrays).
- **Higgsfield parallel**: Direct copy of Krea's left-sidebar session thumbnails — the pattern Agent 3 cited as most relevant.

### 7. Per-clip live progress + elapsed-time during B-roll generation
**Impact: 4 · Effort: 3**

- **Current**: B-roll job at status `generating_clips` shows per-clip status chips (`pending` / `image_running` / `video_running` / `done` / `failed`) but no elapsed time, no model latency hint, no overall ETA. A 13-clip job at concurrency 3 takes ~6-10 min — feels like forever with no progress signal.
- **Proposed**: Per clip, show `42s elapsed` ticking from the moment status flipped to `image_running`. Backend already writes `updated_at` per state change — just diff against `Date.now()` in the frontend. Add an aggregate progress bar at the top: "8 of 13 clips done · 4 min elapsed · ~3 min remaining (estimated)" using running average of completed clips.
- **Files**: `web/index.html` (B-roll card), `web/app.js` (per-clip elapsed-time computed, ETA calc).

### 8. B-roll Mode badges get a legend / tooltip
**Impact: 3 · Effort: 1**

- **Current**: Each clip shows a coloured badge: "Mode 1 — Body Part Transformation", "Mode 2 — Biological Process", "Mode 3 — Anatomical Flythrough", "Mode 4 — Contextual Human Moment". The 4 colours are arbitrary; users have to read the full string to understand.
- **Proposed**: Add a one-time legend at the top of the B-roll tab (collapsible details element) explaining the 4 modes with example visuals. On each clip badge, just show "Mode 3" with a tooltip on hover that explains.
- **Files**: `web/index.html` only.

### 9. Friendly download filenames everywhere
**Impact: 3 · Effort: 2**

- **Current**: Free-form gens download as `g_a1b2c3d4e5.mp4` / `.png` / `.mp3`. The Swap flow already does this right (`<char_name>-variant-N.png` per CLAUDE.md).
- **Proposed**: Sanitize the prompt's first 40 chars + add date stamp + extension. So a prompt like "A close-up of a swollen ankle..." downloads as `a-close-up-of-a-swollen-ankle-2026-05-14.mp4`. Use existing `_safe_filename_stem` helper in `src/character_swap/api.py:79`.
- For B-roll: `broll-final-{date}.mp4` instead of `broll-br_xxx.mp4`.
- **Files**: `web/index.html` (every `:download="..."` binding), maybe extract to a `friendlyName(g)` JS helper.

### 10. Inline retry-this-variant in Swap (not regenerate-all)
**Impact: 3 · Effort: 3**

- **Current**: When one of N variants fails (OpenAI 403, network blip), the user's only option is "↻ regenerate all" which discards the 3 successful variants and re-pays for them.
- **Proposed**: Per-variant retry icon on failed variants only. Backend endpoint `POST /api/jobs/{job_id}/regenerate_variant` accepting `{char_id, variant_id}`. Mirror the B-roll reject-and-regen pattern that already works.
- **Files**: `src/character_swap/api.py` (new endpoint), `src/character_swap/runner.py` (new function), `web/index.html` (variant card), `web/app.js` (handler).

---

## Notable patterns NOT proposed (and why)

- **Node-based canvas like Higgsfield Canvas / Krea Nodes** — gorgeous and exactly fits this app's multi-step nature, but it's a 2-week build. Park until the simpler wins above are in.
- **Aspect ratio preset buttons grouped by platform (TikTok / YouTube / IG)** — already mostly done (toggle buttons exist) and the current 9:16 default matches the app's portrait bias. Marginal additional gain.
- **Onboarding / example prompts per model** — single-user app, you're past onboarding. Skip.
- **Webhook callbacks for job completion** — Higgsfield exposes this for SDK users; for a local app, polling is fine and webhooks add complexity.
- **Collapsible parameter side-panel** — your current setup with inline form fields is actually cleaner for a single-user tool. Higgsfield's side panel is for managing 100+ users' preferences; you don't need it.

---

## Estimated time per item

| Item | Hours |
|---|---|
| 1. Preserve form state | 1 |
| 2. Default selections | 1 |
| 3. Promote reuse | 1 |
| 4. Status toast | 6 |
| 5. Search + filter | 4 |
| 6. Sidebar thumbnails | 8 |
| 7. B-roll live progress | 4 |
| 8. Mode legend | 1 |
| 9. Friendly downloads | 3 |
| 10. Variant retry | 4 |

**Top 3 total**: ~3 hours. Worth doing in one session.
**Top 5**: ~14 hours. A focused day-and-a-half.
**All 10**: ~33 hours. A solid week.

---

## Sources

- Audit Agent 1: complex flows (Swap + Editor + B-roll)
- Audit Agent 2: simple gen tabs (Image + Video + Audio + Avatar)
- Reference Agent 3: Higgsfield AI, Krea, Leonardo, ComfyUI documentation
- `CLAUDE.md` for intended UX
- Personal observation from running B-roll end-to-end (job `br_89b2559f4a`, 13 clips, 35s narration)
