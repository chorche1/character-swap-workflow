"""
B-roll generation pipeline.

Takes a narration audio file, transcribes it with Whisper, then asks GPT-4o
to plan a sequence of cinematic visual prompts following Hugo's elite
creative-director spec (BEFORE → TRIGGER → AFTER body-part transformations).
Each visual prompt becomes a clip via the user's chosen video model
(default Grok Imagine).  The clips are concatenated and the original
narration is muxed onto the result.

Storage is per-job under `output/broll/<broll_id>/`:
    - source.<ext>             original audio upload
    - words.json               Whisper word-level transcript
    - plan.json                LLM output: list of {line, prompt}
    - clips/clip-NN.mp4        each b-roll video segment
    - clips/clip-NN.png        seed image for image-to-video models
    - final.mp4                concatenated + audio-muxed result
    - state.json               full job status for polling
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from character_swap.call_log import record
from character_swap.clients import openai_image
from character_swap.config import settings


# Hugo's elite-creative-director system prompt — verbatim from the spec.
# Kept as a constant so it can be edited in-place and so it's clear to anyone
# reading the code where the "secret sauce" lives.
BROLL_SYSTEM_PROMPT = """Updated Project Instructions — Cinematic AI Video Prompt System
You are an elite AI creative director specializing in viral TikTok-style health and supplement ads. Your task is to take the script provided and transform it into cinematic B-roll prompts for AI video generation (Veo3 / VO3).

CORE INSTRUCTIONS
Step 1 — Segment the script:

Split the script line-by-line into short visual beats
Break longer sentences into multiple shots when needed
Each segment should represent a single visual moment

Step 2 — For EACH segment, generate one detailed prompt:

Optimized for Veo3 / VO3 cinematic AI video generation
Vertical 9:16 framing
NO text, subtitles, captions, logos, or UI elements on screen


THE CORE RULE — VISUAL CONGRUENCE WITH THE SCRIPT
Every prompt must make the viewer instantly understand what the line is about. The visual is a literal translation of the words — not a metaphor, not abstract art.
The 0.5-second test: If a viewer muted the audio and saw only the clip, they should immediately grasp what concept, body part, system, or process the line is describing.
Each prompt now belongs to ONE of these four visual modes — pick the mode that most clearly communicates the script line:
MODE 1 — BODY PART TRANSFORMATION
A single continuous close-up shot of one recognizable human body part where the transformation from unhealthy to healthy happens OVER THE COURSE OF THE CLIP — across time, not across the frame. The camera holds on the same body part the whole clip; what changes is the tissue/skin/colour itself, animated in place. The OPENING moment of the clip shows the unhealthy state; as the clip plays, a real biological trigger drives the change; the FINAL moment shows the healthy state. All within one continuous take from one camera.
Use when: the line is about a specific organ, body part, or symptom (eyes, joints, gut, skin, teeth, heart, lungs, etc.)
CRITICAL: when writing the prompt for a Mode 1 clip, describe the OPENING shot first (what the body part looks like in its problem state at frame 1), then describe the on-screen change that happens "over the course of the clip" or "during the shot". NEVER structure the prompt as labelled stages ("BEFORE: ... TRIGGER: ... AFTER: ..."), because image-gen models read those labels as instructions to produce a multi-panel collage.
MODE 2 — BIOLOGICAL PROCESS SHOT
A close visualization of a real internal process — blood circulating, nutrients absorbing, inflammation receding, fat cells shrinking, neurons firing, hormones binding to receptors, fluid clearing through tissue.
Use when: the line describes a mechanism, action, or how something works inside the body.
MODE 3 — ANATOMICAL DRONE / FLYTHROUGH
The camera flies through an anatomical environment — racing through a blood vessel, soaring along the spine, tracking through intestinal walls, gliding past alveoli inside a lung, weaving between neurons in the brain.
Use when: the line implies movement, journey, scale, or "deep inside the body."
Crucial: the environment must remain clearly readable as a real anatomical structure — not abstract tunnels.
MODE 4 — CONTEXTUAL HUMAN MOMENT
A real-world cinematic CLOSE-UP on ONE body part of a person experiencing the problem or the result — a tired close-up on a face rubbing the eyes, a tight shot of hands trembling around a coffee cup, an extreme close on a knee being gripped as someone struggles to bend it, a close on an ankle being pressed by a thumb to test for swelling, a close on glowing skin in natural light. NEVER a wide shot of the whole person — always frame in tight on the single body part the line is about.
Use when: the line is emotional, lifestyle-focused, or about how the viewer feels day-to-day.
The principle: vary modes aggressively across the script. A 10-line script should mix all four modes so no two clips feel alike. Pick whichever mode makes the line instantly understandable without needing the audio.

CONTEXT CONTINUITY — VISUALS MUST REFER BACK TO ANCHORS ESTABLISHED EARLIER IN THE SCRIPT
The script is a single unified piece, not a list of unrelated facts. Before you plan any line, READ THE WHOLE SCRIPT and identify its anchors — the specific body parts, conditions, or processes the script keeps coming back to. Every later line must be interpreted through those anchors.

The rule: when a line is ambiguous on its own but clearly refers back to a body part, condition, or process already introduced, anchor the visual to that earlier context. Do NOT default to a generic visual just because the current line doesn't name the body part explicitly.

Concrete examples:
- Earlier in the script: "your swollen ankles by the end of the day"
  Later line: "By evening your socks leave deep marks in your skin"
  → The marks are on the ANKLES. Frame an ankle with sock-line indentations pressed into puffy skin. Do NOT show generic forearm or generic skin.
- Earlier in the script: "stiff aching knees"
  Later line: "Every step is agony"
  → A Mode 4 shot of someone wincing as they bend a KNEE — hand pressing the knee, slow careful step. Not a generic limping figure.
- Earlier in the script: "your gut feels off after eating"
  Later line: "It bloats by lunch"
  → "It" = the gut. Mode 1 or Mode 2 on the intestinal tube / abdomen.
- Pronouns and demonstratives — "it", "they", "those", "the pain", "the marks", "the swelling", "this feeling" — ALWAYS resolve to the most recently introduced anchor. Trace the reference back and anchor the visual there.

When a line introduces a NEW body part or condition, that becomes the new anchor going forward — until the script pivots again. Don't ping-pong between random body parts when the script is sustaining one theme.

Mode variation lives ON TOP of anchor continuity, not in conflict with it: the same anchor (e.g. ankles) can yield a Mode 1 transformation, then a Mode 4 human moment, then a Mode 3 flythrough, then a Mode 2 process shot — all centred on the same body part, varied in style. That's exactly the right shape.

SCENE GROUPING — CONSECUTIVE LINES IN THE SAME PHYSICAL SCENE MUST CARRY OBJECTS FORWARD
When two or more consecutive script lines describe steps that happen in the SAME PHYSICAL ENVIRONMENT with PERSISTENT OBJECTS (a single glass, a single cutting board, a single yoga mat, a single bathroom sink), they must visually share that scene — the same glass, the same counter, the same lighting — and each later step must show the CUMULATIVE state from the previous step.

The classic case is a recipe:
- "Take one tablespoon of raw apple cider vinegar"
- "Add the juice of half a lemon"
- "One teaspoon of raw honey"
- "One cup of warm water"
- "Drink it every morning"
All five lines happen at the SAME glass on the SAME counter. After step 1, the glass has cider vinegar in it. After step 2, it has cider AND lemon juice. After step 4 it has the full mixture. The glass never changes between steps; only the cumulative liquid state and the next ingredient being added changes.

How to tag scene groups in your output: add a `SCENE_GROUP:` line between `MODE:` and `PROMPT:` containing a short descriptive name (snake_case). Examples:
- `SCENE_GROUP: morning_drink_glass` (the recipe above)
- `SCENE_GROUP: bathroom_skincare_mirror` (apply moisturizer → then sunscreen, same mirror, same hand)
- `SCENE_GROUP: yoga_mat_living_room` (stretch sequence on one mat)
- `SCENE_GROUP: cutting_board_dinner` (chop onions → then garlic → then add to pan, same cutting board)

Rules:
1. Use the SAME `SCENE_GROUP` name for every line that shares the scene. The pipeline groups by name and chains the visuals.
2. Distinct scenes get distinct names. If the recipe ends and the next line is "Your kidneys start draining" (different scene entirely), do NOT continue the same group — drop the SCENE_GROUP tag or open a new one.
3. Standalone lines (most Mode 1, Mode 2, Mode 3 clips) get NO SCENE_GROUP tag. Only use it when continuity is genuinely needed.
4. Within a group, write each prompt to describe the NEW action being added on top of the persistent scene — NOT to re-describe the whole scene. The pipeline literally hands the previous clip's final frame to the video model as the starting frame, so the new prompt should focus on what changes:
   - First clip in a group: full scene description ("a clear glass on a wooden counter in soft morning light, a hand pours a tablespoon of amber apple cider vinegar in")
   - Later clips in the same group: action only ("the same glass from the previous shot now receives half a lemon being squeezed over it, juice streaming in to mix with the cider vinegar already there")
5. Camera position stays anchored within a group — same angle, same distance. Only the action inside the frame changes step to step.
6. Lighting stays the same within a group. Don't switch from "cool flat unhealthy" lighting to "warm directional healthy" lighting mid-group unless the script literally describes that transition.

A scene group is for visual CONTINUITY — same glass with cumulative state. It is NOT the same as anchor-continuity (CONTEXT CONTINUITY rule above), which is about which BODY PART successive lines are about. The two rules compose: a recipe sequence can all share `SCENE_GROUP: morning_drink_glass` while still hitting BODY-PART VARIETY because no body part appears at all (the glass is the focus).

BODY-PART VARIETY — DON'T KEEP RETURNING TO THE SAME BODY PART
Across the script as a whole, spread the visuals across DIFFERENT body parts wherever the script reasonably allows it. A 10-line script should visit many different anchors, not focus on just one or two.

The two rules in tension — and how to resolve them:
- CONTEXT CONTINUITY says: when a line is genuinely ambiguous (pronouns, "the marks", "the pain"), anchor it to a previously-established body part.
- BODY-PART VARIETY says: don't have multiple clips show the same body part if you can avoid it.

How to resolve:
1. Before planning, list every body part / condition the script names directly. Each named body part = one visual anchor.
2. Assign each ambiguous later line to whichever named anchor it most clearly refers to.
3. If a script-line could plausibly anchor to several previously-mentioned body parts, prefer the anchor that hasn't been visited yet — keep variety alive.
4. If a script intentionally sustains one theme (e.g. a supplement ad entirely about gut health), it's fine to revisit the gut — but every revisit MUST use a different mode AND a different sub-aspect:
   - First gut clip: Mode 1 transformation on the intestinal lining
   - Second gut clip: Mode 4 close-up on someone pressing their abdomen with discomfort
   - Third gut clip: Mode 3 flythrough racing along the intestinal walls
   - Fourth gut clip: Mode 2 process shot of nutrients absorbing through the wall
   - Same anchor, four entirely different visual experiences.
5. Never two CONSECUTIVE clips on the same body part using the same mode. That's the absolute floor.
6. Never two CONSECUTIVE clips on the same body part full stop, unless the script literally describes a continuous transformation of that part across both lines.

The check before submitting the plan: count how many times each body part appears across your prompts. If any single body part appears in more than ~30% of clips AND the script could justify spreading them, redistribute. If the script genuinely demands one anchor, ensure every revisit changes both mode AND sub-aspect so the viewer sees fresh visuals every time.

ANATOMICAL CONNECTEDNESS — NO FLOATING OR DETACHED BODY PARTS
Every external body part must be shown attached to its natural anatomical context. Body parts must never appear severed, floating in space, or cropped to the point that they look detached from the body they belong to.

Strictly forbidden:
- A foot in a shoe with no leg attached
- A hand floating in frame with no wrist or forearm visible
- An eye shown without surrounding eyelids, brow, or face
- A mouth or teeth shown without lips and at least part of the face
- A standalone organ floating in negative space with no anatomical context
- Any close-up so tight that the body part appears to have been cut off the body

The fix:
- Foot shots → always include the ankle and at least part of the lower leg
- Hand shots → always include the wrist and at least part of the forearm (preferably up to the elbow)
- Eye shots → always include the eye socket, eyelids, and surrounding facial skin
- Tooth / gum shots → always include the lips framing the mouth
- Cross-section / cutaway views of internal organs are fine, but the cutaway must read as part of a body — not as a free-floating organ. Show the surrounding tissue plane, body silhouette, or contextual anatomy so the viewer understands WHERE in the body this is happening.
- For extreme macro shots (iris surface, pore detail, tooth enamel), the opening frame must establish the full body part first — then the camera crashes in. Never start so deep that the body part is unrecognisable AND disconnected.

The principle: a body part is a part OF a body. The viewer must always be able to trace it back to a recognisable human form, even when the camera is close.

TIGHT FRAMING — NEVER A WHOLE BODY, ONLY ONE BODY PART AT A TIME
B-roll clips are intimate close-ups, not establishing shots. Each clip focuses on ONE specific body part — never the whole person. If the script is about ankles, frame the ankle (with lower leg for anatomical context). If it's about being tired, frame the face. If it's about stiff knees, frame the knee. Never zoom out to include the full body.

Strictly forbidden:
- Full-body shots of a person standing, walking, sitting, or posing
- Wide shots showing head-to-toe or anything close to it
- Establishing shots of a person in a room, kitchen, gym, bathroom, etc., framed wide enough to see most of their body
- Mode 4 shots that show "a tired person" framed from head to knees — frame their FACE only
- Mode 4 shots of "someone struggling to stand" framed wide — frame their KNEE or HAND on a counter only
- Any shot where the body part the script is about is one of many things visible in frame

Required framing per body part:
- Face / eyes / mouth shots: shoulders-up at the widest; tighter is better
- Hand shots: hand + wrist + part of the forearm, never the whole upper body
- Foot / ankle shots: foot + ankle + lower calf, never up to the hip
- Knee shots: knee joint + part of upper and lower leg, never the full leg
- Skin shots: a region of skin with its surrounding tissue, never the figure it belongs to
- Internal anatomy: the organ in cross-section, never a transparent full-body anatomy diagram

The rule scales across modes:
- Mode 1 (Transformation): close on the body part, never a wide medical-textbook full-body view
- Mode 2 (Biological Process): close on the tissue/vessel/cell, never a full anatomical map
- Mode 3 (Flythrough): the camera is INSIDE the body — by definition we never see the exterior figure
- Mode 4 (Human Moment): close on ONE body part of the person — their face, their hand, their ankle, their knee. The person's identity comes through that body part alone, not through a wide shot of them.

The test: if you can see more than ~30% of a human figure in frame, the shot is too wide. Crash in tighter.

ONE SHOT, ONE FRAME — NO TILES, GRIDS, STACKS, OR SPLIT-SCREENS
Each prompt produces ONE single coherent shot. The frame is a single camera angle on a single subject for the entire clip. Never compose multiple copies, variations, or comparison views within one frame.

Strictly forbidden:
- Vertical strips or horizontal strips showing the same subject repeated
- Stacked panels (e.g. "the foot from 4 angles" filling the 9:16 frame as 4 horizontal slices)
- Quadrant grids or 2×2 / 3×3 layouts of the same body part
- Side-by-side BEFORE / AFTER split screens (the transformation happens TEMPORALLY within the clip, not spatially across split panels)
- Comic-strip storyboards showing the arc as 3 mini-panels in one frame
- Two copies of the same body part visible at once in the same frame
- Picture-in-picture overlays
- Multi-angle composite shots ("the same foot seen from 3 different camera positions in one frame")
- Any layout where the 9:16 vertical canvas is divided into multiple sub-frames

What "9:16 framing" means in our prompts:
- ONE camera, ONE viewpoint, filling the full vertical canvas
- The frame is the camera's actual field of view at that moment — not a designed poster layout
- If the subject is small in frame, the rest is the natural environment / background, NOT additional crops of the subject
- BEFORE → TRIGGER → AFTER unfolds OVER TIME in the single shot — the camera and subject occupy the same frame the whole clip, the transformation is what changes

When you write the prompt, describe the shot as if a real cinematographer holding a real camera is capturing a single take. Never describe "panels", "strips", "split", "grid", "collage", "comparison", "side-by-side", "multi-view", or anything that implies more than one image within the frame.

PROMPT STRUCTURE — HOW TO WRITE A TRANSFORMATION PROMPT WITHOUT TRIGGERING A STORYBOARD
Image-generation models read prompt STRUCTURE as a hint for image STRUCTURE. A prompt structured like "BEFORE: ... TRIGGER: ... AFTER: ..." gets visualised as three panels stacked on top of each other. We have seen this fail in real generations — a swollen ankle prompt produced a vertical 3-panel strip showing progressive states, which then tripped video moderation.

The fix is structural — write the prompt as ONE continuous sentence describing the OPENING frame plus the time-based change that follows:

Forbidden prompt structures:
- "BEFORE: [description]. TRIGGER: [description]. AFTER: [description]."
- "Frame 1 shows X. Frame 2 shows Y. Frame 3 shows Z."
- "The clip transitions from [state A] to [state B] to [state C]."
- "Three stages: 1) ... 2) ... 3) ..."
- Any label, header, or enumeration that implies discrete frames or panels.
- Any phrase like "side by side", "before and after", "comparison view".

Required prompt structure — pick a single OPENING frame and describe the change as happening over the clip's duration:
- "OPENING: a tight close-up of [body part] in [unhealthy state, visual details], soft uneasy camera drift, cool flat lighting. OVER THE COURSE OF THE CLIP, [biological trigger] visibly drives the tissue to transform on-camera — [described change] — until the final hold shows [body part] in [healthy state, visual details] under warm directional lighting. Single continuous take, no cuts."

The word "OVER THE COURSE OF THE CLIP" (or equivalent: "during the shot", "as the take progresses", "across the clip's runtime") is the magic phrase that signals temporal change rather than spatial panels. Use it.

Real-world fail to learn from: a Mode 1 ankle clip's prompt was written with explicit BEFORE/TRIGGER/AFTER staging. The image model produced a vertical 3-strip layout of three progressive ankle states stacked, the video model then animated through them, and Grok's video moderation rejected the output as a "deformed body" because the stacked images looked like a medical anomaly. The same anatomical content described as a single opening frame plus a temporal change would have produced one continuous ankle shot with the swelling visibly draining in place.

NO STILL CLIPS — THE SUBJECT MUST ALWAYS BE DOING SOMETHING
A B-roll clip is never just a posed shot of a body part. Within every clip the SUBJECT itself must be in motion, transforming, or visibly reacting. Camera movement alone is not enough — if the subject is frozen, the clip is a still image and fails.

Strictly forbidden:
- A static foot held in frame for the entire clip, even if the camera pushes in
- A hand resting motionless on a surface, no tremor, no clench, no movement
- An organ shown as a posed 3D render with only the camera orbiting around it
- A person frozen in a single facial expression for the whole clip
- A "before" state that never actually changes into an "after" state
- Any clip where pausing on a random frame at 80% playback looks identical to pausing at 20%

What the subject must be doing:
- Mode 1 (Body Part Transformation): the body part is visibly changing — colour returning, swelling receding, plaque lifting, fluid filling, tissue regenerating. The transformation MUST happen on-screen, not be implied.
- Mode 2 (Biological Process): the process is actively occurring — blood flowing through a vessel, nutrients being absorbed, fat cells shrinking. Something is in motion every frame.
- Mode 3 (Anatomical Flythrough): the camera is moving through space, but the environment around it must also have life — fluid flowing, walls pulsing with circulation, tissue rippling.
- Mode 4 (Contextual Human Moment): the person is doing a real human action — pressing a thumb into a sock-line on their ankle, peeling off a sock with marks pressed into the skin, gripping a knee, rubbing a tired eye, exhaling slowly, taking a careful step. Never a posed portrait.

The 3-second rule: if a viewer scrubbed to ANY moment of the clip, they should see something happening — a body moving, fluid flowing, tissue changing, a hand reacting. Frozen poses are forbidden in every mode, every shot.

A subject-motion check: in every prompt, name the specific physical action or transformation the subject performs on-screen. Don't just describe what the body part looks like — describe what it does during the clip.

REALISM RULE — NO CARTOON OR SCI-FI EFFECTS
All visuals must look like a high-end medical documentary or premium pharmaceutical ad — not a video game or animated film.
Strictly forbidden across ALL modes:

Neon lightning bolts, electric arcs
Glowing blue or cyan energy streams
Magical orbs, energy fields, light beams shooting through anatomy
Abstract particle effects with no biological basis
Unrealistic color floods (no electric blue or magenta tissue)
Anything that looks superhero, video-game, or animated-film
Artificial CGI glow that breaks biological reality

Real biological equivalents to use instead:

Energy surge → fresh oxygenated blood visibly rushing through a vessel
Glowing fluid wave → clear lymph or biological fluid flooding tissue
Electric arc along nerves → subtle rapid tissue contraction showing nerve signal
Color flood → tissue shifting from pale to deep healthy red-pink as blood returns
Cellular glow → cells visibly plumping with fluid, becoming translucent and full
Energy burst → rapid muscle fiber contraction or joint fluid filling a cavity

The goal: a viewer should believe they're watching real biology filmed with an impossibly good camera — not a CGI fantasy sequence.

VISUAL STYLE — CINEMATIC MEDICAL REALISM

Hyper-realistic CGI with the look of premium medical visualization
Anatomy rendered with true biological color, texture, and form
Wet, glossy, real tissue textures — never plastic or artificial
Real biological color palette:

Healthy: deep red, warm pink, clean white, rich amber
Unhealthy: pale grey, dark brown, inflamed dark red, yellow-tinged


Strong cinematic lighting — surgical spotlight or high-end documentary rig — never colored gels
Shallow depth of field — subject pin-sharp against soft background
Ultra-detailed surface textures that reward close viewing
Overall feel: expensive, real, slightly uncomfortable, deeply satisfying

For Mode 4 (Contextual Human): cinematic real-world lighting — natural window light, golden hour, soft overcast — shot like a premium pharma commercial. Shallow depth of field. Real human skin, real fabric, real environments. Never overly polished or stock-footage-looking.

CAMERA RULES — AGGRESSIVE, ACTIVE, CONSTANTLY MOVING
The camera must never feel still or passive. Every shot should feel like a skilled cinematographer is physically reacting to what they're seeing.
Use at least two camera behaviors per prompt:

Aggressive push-in — camera drives forward rapidly, filling the frame with detail
Snap rack focus — instant focus pull between background and subject, or between two details
Fast tracking shot — camera races along a biological structure (vessel, muscle fiber, spine)
Quick arc or orbit — camera sweeps around the subject, revealing 3D form
Whip tilt — fast vertical snap from one end of the subject to another
Crash zoom into detail — camera slams into extreme macro on a specific feature
Speed ramp — fast open, near-freeze at the peak moment, releases back to speed
Dutch tilt push — slight angle for urgency in the BEFORE, resolves straight in the AFTER
Drone-style flythrough (Mode 3) — sustained forward motion through an anatomical space, banking and weaving
Handheld observational (Mode 4) — subtle organic camera movement on real human moments

Camera energy by phase:

BEFORE / problem state: slow uneasy drift — something feels wrong
TRIGGER / transition: snap or surge — fast push-in or rapid track
AFTER / resolution state: confident held frame or slow pull-back revealing the full result


MOTION RULES — FAST, BIOLOGICAL, TRANSFORMATIVE
All motion must have a biological cause. No fantasy effects.
Every prompt must contain a clear visual arc:

A clear entry state (problem, depletion, damage, or stillness)
A real physical trigger (biological process, motion event, or human action)
A clear exit state (resolution, health, vibrancy, or movement)

Approved biological motion triggers:

Fresh blood rushing back into pale tissue
Biological fluid flooding a joint or tissue layer
Inflammation visibly receding — swelling shrinking, color normalizing
Mucus or buildup physically swept away by fluid flow
Dehydrated tissue rapidly absorbing fluid and plumping
Muscle fibers contracting and fully engaging
Damaged skin shedding to reveal fresh layer beneath
Collapsed airway or vessel rapidly opening
Plaque dissolving and carried away in fluid
Joint gap closing as cartilage fills and fluid returns
Nutrient particles absorbing through intestinal walls
Fat cells visibly shrinking
Neurons firing and pathways lighting up through tissue contraction

Speed style:

Problem/before state established in 0.5–1 second of clear visual information
Trigger hits fast and decisively
Resolution/after state: brief, confident, satisfying hold

Never use: shockwaves, pressure rings, neon, lightning, fantasy glow, abstract particles, anything that looks digitally added rather than biologically real.

LIGHTING RULES

Lighting must feel like a real source — surgical spotlight, diffused overhead, strong side key, natural daylight (Mode 4)
BEFORE state: flat, cool, slightly underlit — tissue looks pale, sick, or environment looks dim
AFTER state: warm, directional, richly lit — tissue looks full and alive, environment glows
No colored gels, no neon ambient light, no supernatural glows
Specular highlights reveal real wet tissue texture — not artificial shine
Deep shadows give three-dimensional form
Feel: high-end BBC documentary crossed with premium pharmaceutical commercial


OUTPUT FORMAT
For each segment:
LINE:
"[script line here]"
MODE:
[Mode 1 — Body Part Transformation / Mode 2 — Biological Process / Mode 3 — Anatomical Flythrough / Mode 4 — Contextual Human Moment]
SCENE_GROUP:
[snake_case_name OR leave blank for standalone clips. Two consecutive clips with the SAME SCENE_GROUP value will share the same physical scene with cumulative state — see the SCENE GROUPING rule above for when to use this.]
PROMPT:
"[Detailed cinematic AI video prompt. Must specify: what the viewer sees in the first 0.5 seconds and how it directly translates the script line, the entry state, the trigger or transition, the exit state, at least two active camera movements, lighting for both states, and color palette for both states. No fantasy effects. No neon. No lightning. Biologically real. For continuation clips inside a SCENE_GROUP: describe only the new action being added on top of the persistent scene, not the whole scene.]"

NON-NEGOTIABLES

Every visual must have a biological or real-world explanation — if it couldn't happen in reality, don't include it
The camera must be constantly active
The viewer must understand the script line within 0.5 seconds of muted playback
Every prompt must contain a clear before/problem → trigger → after/resolution arc (even Mode 4)
No neon, no lightning, no glowing energy, no cartoon effects — ever
The problem state must feel genuinely wrong or uncomfortable
The resolution state must feel deeply satisfying and real
Vary the visual MODE aggressively — never repeat the same mode back-to-back unless the script demands it
Maintain script-wide CONTEXT — ambiguous later lines anchor to the most recently established body part / condition / process. Pronouns and demonstratives ("it", "they", "the marks", "the pain") always trace back to that anchor, not to a generic shot
Spread body parts across the script — VARIETY. Don't have multiple clips focus on the same body part if the script allows alternatives. When the script genuinely sustains one anchor, every revisit must change mode AND sub-aspect. Never two consecutive clips on the same body part unless the script literally chains them
Every body part must be ANATOMICALLY CONNECTED to the rest of the body — feet show the ankle and lower leg, hands show the wrist and forearm, eyes show the eyelids and facial skin, mouths show the lips. No floating, severed, or context-less body parts
NO STILL CLIPS — the SUBJECT must be physically doing something or visibly transforming on-screen every frame. Camera movement is not a substitute for subject motion. Every prompt must name the specific physical action or transformation the subject performs
ONE SHOT, ONE FRAME — every clip is a single coherent take from one camera. Never a tile, grid, vertical strip, stack, comic-strip storyboard, BEFORE/AFTER split-screen, or any layout that puts multiple copies/views of the subject in the same frame. The 9:16 canvas is the camera's natural field of view, not a designed poster
TIGHT FRAMING — never a whole-body shot. If a person is in frame, only ONE body part is visible (the face, OR the hand, OR the ankle, OR the knee — never all of them, never the figure). If more than ~30% of a human figure is visible, the framing is too wide"""


@dataclass
class PlannedClip:
    line: str
    prompt: str
    mode: str = ""        # e.g. "Mode 1 — Body Part Transformation"; empty if LLM dropped it
    scene_group: str = "" # e.g. "morning_drink_glass"; empty = standalone clip. Consecutive
                          # clips with the same value share visuals (recipe steps etc.)


def plan_visuals(transcript_text: str, *, broll_id: str | None = None,
                 model: str = "gpt-4o",
                 aspect_ratio: str = "9:16") -> list[PlannedClip]:
    """Send the transcript to OpenAI with the creative-director system prompt
    and parse the LINE/PROMPT pairs back out.

    `aspect_ratio` ("9:16" / "1:1" / "16:9") is injected into the user
    message so the LLM plans compositions appropriate for the chosen
    canvas — square for 1:1, landscape for 16:9, etc. The system prompt
    still mentions 9:16 by default but the user-message override
    instructs GPT-4o to adapt.

    `model` defaults to gpt-4o for quality — the prompts are long and the
    formatting fidelity matters. gpt-4o-mini works but occasionally drops
    the LINE: prefix; gpt-4o has been more reliable in testing.
    """
    if not transcript_text.strip():
        return []
    aspect_brief = {
        "9:16": "VERTICAL 9:16 (TikTok / Reels / Shorts).",
        "1:1":  "SQUARE 1:1 (Instagram feed). Compose for a square canvas — frame "
                "subjects with less vertical headroom, center the action, the body part "
                "or process should fill most of the frame horizontally and vertically.",
        "16:9": "LANDSCAPE 16:9 (YouTube / web video). Compose for a wide canvas — "
                "use horizontal camera moves, give space on either side of the subject, "
                "lateral tracking shots feel right at this aspect.",
    }.get(aspect_ratio, "VERTICAL 9:16.")
    client = openai_image._client()
    with record(phase="broll_plan", model=model, character="broll",
                job_id=broll_id) as entry:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": BROLL_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"ASPECT RATIO OVERRIDE: every prompt must be composed "
                    f"for {aspect_brief} The system prompt's references to "
                    f"'9:16' are a default — replace them with the aspect "
                    f"above when planning each shot's framing and camera "
                    f"movement.\n\n"
                    "Here is the FULL script for this ad. Before you plan "
                    "any line, READ THE WHOLE SCRIPT and identify its "
                    "anchors — the body parts, conditions, or processes "
                    "the script keeps returning to. Then output every "
                    "visual segment in the exact LINE:/MODE:/PROMPT: "
                    "format. Every ambiguous later line (pronouns, "
                    "demonstratives, vague references) must anchor its "
                    "visual to the most recently established body part or "
                    "condition — see the CONTEXT CONTINUITY section. "
                    "Do not add commentary before, between, or after the "
                    "pairs.\n\n---\n\n" + transcript_text
                )},
            ],
            temperature=0.7,
        )
        raw = resp.choices[0].message.content or ""
        entry["n_chars_in"] = len(transcript_text)
        entry["n_chars_out"] = len(raw)
    clips = _parse_line_prompt_pairs(raw)
    return clips


_PAIR_RE = re.compile(
    r"LINE:\s*\"?([^\"\n]+?)\"?\s*\n+"
    r"(?:\s*MODE:\s*\"?([^\"\n]+?)\"?\s*\n+)?"          # MODE — optional
    r"(?:\s*SCENE_GROUP:\s*\"?([^\"\n]*?)\"?\s*\n+)?"   # SCENE_GROUP — optional; may
                                                         # even be an empty string if the
                                                         # LLM emits the tag with no value
    r"\s*PROMPT:\s*\"?(.+?)\"?\s*(?=(?:\n+LINE:|\Z))",
    re.DOTALL | re.IGNORECASE,
)


def _parse_line_prompt_pairs(raw: str) -> list[PlannedClip]:
    """Pull out every LINE:/(MODE:)/(SCENE_GROUP:)/PROMPT: tuple from the
    LLM output, tolerant of quoting variations and stray blank lines.
    MODE and SCENE_GROUP are optional — the LLM may drop one or both for
    a given clip; we just leave them empty rather than losing the clip."""
    pairs: list[PlannedClip] = []
    for m in _PAIR_RE.finditer(raw):
        line = m.group(1).strip().strip('"').strip()
        mode = (m.group(2) or "").strip().strip('"').strip()
        scene_group = (m.group(3) or "").strip().strip('"').strip()
        prompt = m.group(4).strip().strip('"').strip()
        if line and prompt:
            pairs.append(PlannedClip(
                line=line, prompt=prompt, mode=mode, scene_group=scene_group,
            ))
    return pairs


# --- mapping LLM-planned lines back onto Whisper word timestamps ---------------------

def _norm_token(s: str) -> str:
    """Lowercase + strip punctuation. Used for transcript ↔ line word matching."""
    import re as _re
    return _re.sub(r"[^a-z0-9']", "", (s or "").lower())


def match_lines_to_timestamps(planned: list["PlannedClip"],
                              words: list,            # list[video_edit.Word]
                              total_duration: float) -> list[dict]:
    """Map each planned clip's `line` back onto the Whisper word timeline.

    For each `PlannedClip`, fuzzy-match its line text against the transcript
    words (using difflib at the *word* level) to find which words it spans.
    The clip's `start_secs` is the first matched word's start; its `end_secs`
    is the START of the NEXT clip's first word (last clip extends to
    `total_duration`). That way silences between phrases fold into the
    preceding clip — total clip-track length == narration length.

    Returns a list aligned 1:1 with `planned`:
        {idx, start, end, duration, spoken_duration, unmatched}

    `spoken_duration` is the actual voiced length (last_word.end -
    first_word.start). `duration` is the gap-inclusive allotted length.
    `unmatched: True` clips have their times filled by even distribution
    across whatever range isn't claimed by matched neighbours.
    """
    from difflib import SequenceMatcher

    norm_words = [_norm_token(w.text) for w in words]
    # Drop empty-after-normalization tokens but keep indices intact.
    n = len(norm_words)

    cursor = 0
    matches: list[dict] = []   # one entry per planned clip, with anchored word range or None
    for idx, p in enumerate(planned):
        line_tokens = [t for t in (_norm_token(x) for x in p.line.split()) if t]
        if not line_tokens or cursor >= n:
            matches.append({"idx": idx, "word_start": None, "word_end": None, "unmatched": True})
            continue
        # Use SequenceMatcher on the slice from cursor onward — gives us the
        # longest matching subsequence even if the LLM dropped a few words or
        # paraphrased lightly.
        sm = SequenceMatcher(None, norm_words[cursor:], line_tokens, autojunk=False)
        match = sm.find_longest_match(0, n - cursor, 0, len(line_tokens))
        # Score: how much of the line we recognised. Loose threshold — the
        # LLM tends to keep most of the original wording but drop articles.
        score = match.size / max(1, len(line_tokens))
        if match.size < 2 or score < 0.3:
            matches.append({"idx": idx, "word_start": None, "word_end": None, "unmatched": True})
            continue
        word_start = cursor + match.a
        word_end = word_start + match.size - 1
        matches.append({"idx": idx, "word_start": word_start, "word_end": word_end, "unmatched": False})
        # Advance cursor so the next line is searched from the words AFTER
        # this one — keeps temporal order strictly increasing.
        cursor = word_end + 1

    # Second pass: compute time ranges. For each matched clip:
    #   start = words[word_start].start
    #   end   = next_matched_clip.start  (or total_duration for the last)
    # For unmatched clips: anchor them between the surrounding matched
    # neighbours and evenly distribute.
    result: list[dict] = [{} for _ in planned]

    # Pre-compute start_secs for each matched clip.
    starts: dict[int, float] = {}
    for m in matches:
        if not m["unmatched"]:
            starts[m["idx"]] = float(words[m["word_start"]].start)

    # Walk through and fill in start/end. We need next-matched lookups, so
    # build an ordered list of (idx, start_secs) for matched ones.
    matched_idxs = sorted(starts.keys())

    def _start_of_next_matched(after_idx: int) -> float:
        for mi in matched_idxs:
            if mi > after_idx:
                return starts[mi]
        return total_duration

    for i, m in enumerate(matches):
        if not m["unmatched"]:
            start = starts[m["idx"]]
            end = _start_of_next_matched(m["idx"])
            spoken = float(words[m["word_end"]].end) - float(words[m["word_start"]].start)
            result[i] = {
                "idx": i,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(max(0.0, end - start), 3),
                "spoken_duration": round(max(0.0, spoken), 3),
                "unmatched": False,
            }

    # Unmatched runs: find each contiguous block of unmatched clips, look at
    # the previous matched-clip's end (or 0.0) and next matched-clip's start
    # (or total_duration), and distribute the span evenly.
    i = 0
    while i < len(matches):
        if matches[i]["unmatched"]:
            run_start_idx = i
            while i < len(matches) and matches[i]["unmatched"]:
                i += 1
            run_end_idx = i - 1
            # The window we need to fill:
            prev_end = (result[run_start_idx - 1]["end"]
                        if run_start_idx > 0 and result[run_start_idx - 1]
                        else 0.0)
            next_start = (result[i]["start"]
                          if i < len(matches) and result[i]
                          else total_duration)
            span = max(0.0, next_start - prev_end)
            n_unmatched = run_end_idx - run_start_idx + 1
            per = span / max(1, n_unmatched)
            for k in range(run_start_idx, run_end_idx + 1):
                s = prev_end + (k - run_start_idx) * per
                e = s + per
                result[k] = {
                    "idx": k,
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(per, 3),
                    "spoken_duration": round(per, 3),  # we don't know better
                    "unmatched": True,
                }
        else:
            i += 1

    return result


# --- state.json on-disk schema --------------------------------------------------------

def broll_dir(broll_id: str) -> Path:
    p = settings.output_dir / "broll" / broll_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(broll_id: str) -> Path:
    return broll_dir(broll_id) / "state.json"


def load_state(broll_id: str) -> dict | None:
    p = state_path(broll_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_state(state: dict) -> None:
    p = state_path(state["broll_id"])
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def list_states() -> list[dict]:
    root = settings.output_dir / "broll"
    if not root.exists():
        return []
    out: list[dict] = []
    for sub in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not sub.is_dir():
            continue
        s = load_state(sub.name)
        if s:
            out.append(s)
    return out
