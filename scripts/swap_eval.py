"""Overnight swap-engine evaluation harness (bake-off).

Runs real (scene, character) pairs through a matrix of candidate engines ×
prompts, saves every output to <worktree>/eval_out/, judges each result with
Claude vision against a strict rubric, and writes an HTML compare gallery +
results.json.

Usage (from the worktree):
    uv run python scripts/swap_eval.py                 # run all engines, all pairs
    uv run python scripts/swap_eval.py gpt-full qwen-compact   # filter by run name
    uv run python scripts/swap_eval.py judge           # judge existing outputs only
    uv run python scripts/swap_eval.py gallery         # rebuild gallery.html only

Engines that error are recorded and skipped. Judge scores are cached in
results.json (re-judging only happens for un-judged outputs).
"""
from __future__ import annotations

import base64
import json
import sys
import time
import traceback
from pathlib import Path

import httpx

from character_swap.config import settings
from character_swap import pipeline
from character_swap.clients import fal_image, higgsfield

OUT = Path(__file__).resolve().parent.parent / "eval_out"
OUT.mkdir(parents=True, exist_ok=True)
RESULTS = OUT / "results.json"

# ------------------------------------------------------------------ test pairs
# Pair 1: ACV kitchen scene (the earlier comparison scene).
# Pair 2: the avocado-oil foot scene + wang — the EXACT pair behind Hugo's
#         "horrendous" Higgsfield result and GPT's NSFW refusals. Hard case:
#         forced perspective, shirtless subject (whole-body skin-tone change),
#         moderation-sensitive content.
PAIRS: dict[str, tuple[Path, Path]] = {
    "acv": (
        Path("/Users/hugonorrbom/character-swap-data/input/scenes/sc_2331fe5612.png"),
        Path("/Users/hugonorrbom/character-swap-data/characters/im_00113bc980.png"),
    ),
    "feet": (
        Path("/Users/hugonorrbom/character-swap-data/input/scenes/sc_cdebf9eefa.png"),
        Path("/Users/hugonorrbom/character-swap-data/characters/im_7627b0eb7b.jpeg"),
    ),
}

# ------------------------------------------------------------------ prompts

P_FULL = pipeline.GENERATION_PROMPT      # current long enforcement prompt
P_COMPACT = pipeline.EDIT_SWAP_PROMPT    # the app's compact editor prompt

P_KONTEXT = (
    "Replace the person in image 1 with the person from image 2, while keeping image 1 "
    "completely unchanged otherwise: same exact camera angle, framing, crop, pose, "
    "clothing, objects, background and lighting. The new person's face, hair and skin "
    "tone come from image 2. Maintain the casual unedited phone-photo style of image 1."
)

# ------------------------------------------------------------------ prompts (wave 2 — research-tuned)

# Master prompt from the overnight research synthesis (Image 1/Image 2 indexing,
# integration paragraph, style paragraph, single constraints block).
P_MASTER = (
    "Image 1 is the fixed master scene and ground truth. Image 2 is only the "
    "identity reference for the replacement person.\n"
    "Recreate Image 1 exactly — same framing, composition, crop, camera angle, "
    "camera height, camera distance, focal-length appearance, perspective, "
    "subject scale, headroom, and the exact placement, size, orientation, color, "
    "material and physical state of every object, surface and background "
    "element. Do not reframe, recrop, zoom, rotate, or shift the camera in any "
    "way. Keep all visible text and brand labels legible and unchanged.\n"
    "Replace the person in Image 1 with the person from Image 2, as if they had "
    "been standing there when the photo was taken — as if part of the same "
    "photo. Take only the face, hairstyle, hair color and skin tone from Image "
    "2. The replacement person keeps the original person's exact pose, body "
    "position, torso angle, shoulder position, arm placement, hand placement "
    "and interaction with objects, and wears exactly the outfit from Image 1 — "
    "same garments, colors, patterns, accessories and fit; do not take any "
    "clothing from Image 2. The replacement person looks directly into the "
    "camera lens with a natural, composed expression, even if the original "
    "person was not.\n"
    "Integration: light the replacement person with the scene's own light "
    "sources and color grade. Match skin texture, facial shadows, perspective, "
    "edge blending, white balance, sharpness, depth of field and image grain to "
    "Image 1 so the person belongs naturally in the photo, including correct "
    "cast shadows and contact shadows where the body meets surfaces.\n"
    "Style: a completely ordinary, unedited iPhone photo taken quickly by "
    "another person — plain, slightly dull phone-camera colors, neutral white "
    "balance, mundane ambient daylight, slightly uneven exposure, mild "
    "softness, subtle sensor noise, natural non-polished skin with visible "
    "pores, imperfect casual framing, small background distractions. It should "
    "look like a normal photo from someone's camera roll, not an advertisement "
    "or a professionally edited social-media image.\n"
    "Constraints — do not violate any of these: do not alter the framing, "
    "camera, background, objects, pose or outfit from Image 1; do not carry any "
    "clothing, background or objects over from Image 2; do not blend the "
    "original person's facial features into the new face; do not add people, "
    "text, captions, subtitles, watermarks or logos, and remove any that are "
    "burnt into Image 1; do not apply professional lighting, studio lighting, "
    "cinematic contrast, dramatic shadows, HDR, warm or golden grading, "
    "oversaturation, glossy highlights, beautification, retouching, filters or "
    "portrait-mode background blur; keep hands and anatomy correctly formed "
    "with the correct number of fingers; keep the image realistic and "
    "non-explicit."
)

# GPT flipped-order prompt: identity ref is now Image 1, scene is Image 2
# (research: gpt-image preserves the FIRST input's face with extra richness).
P_GPT_FLIP = (
    "Image 1 is only the identity reference for the replacement person. Image 2 "
    "is the fixed master scene and ground truth. Recreate Image 2 exactly — "
    "same framing, crop, camera angle, camera height, distance, perspective, "
    "subject scale, headroom, and the exact placement, size, orientation, color "
    "and physical state of every object, surface and background element; keep "
    "all visible text and brand labels legible and unchanged. Replace the "
    "person in Image 2 with the person from Image 1, as if part of the same "
    "photo. Take only face, hairstyle, hair color and skin tone from Image 1. "
    "The replacement person keeps the original person's exact pose, body "
    "position, torso angle, shoulder, arm and hand placement and interaction "
    "with objects, and wears exactly the outfit from Image 2 — do not take any "
    "clothing from Image 1. They look directly into the lens with a natural, "
    "composed expression. Match skin texture, facial shadows, perspective, edge "
    "blending, white balance and image grain so the new person belongs "
    "naturally in the photo, with correct cast and contact shadows from the "
    "scene's own light. Style: a completely ordinary, unedited iPhone photo "
    "taken quickly by another person — plain slightly dull colors, neutral "
    "white balance, slightly uneven exposure, mild softness, subtle sensor "
    "noise, natural non-polished skin. Constraints: do not alter the framing, "
    "camera, background, objects, pose or outfit of Image 2; do not blend the "
    "original person's facial features into the new face; do not add people, "
    "text, captions, watermarks or logos (remove any burnt into Image 2); do "
    "not apply professional lighting, cinematic contrast, HDR, warm grading, "
    "beautification or portrait-mode blur; keep hands correctly formed."
)

# Qwen split: short positive imperative + REAL negative_prompt field.
P_QWEN_POS = (
    "Replace the person in the first image with the person from the second "
    "image. Take only the face, hair and skin tone from the second image. Keep "
    "everything else from the first image exactly the same: identical framing, "
    "camera angle, crop, pose, body position, hands, the first image's "
    "clothing, all objects and their positions, the background, and the "
    "lighting. The person looks directly at the camera. Match the new person's "
    "lighting, shadows, white balance and grain to the first image so it reads "
    "as one ordinary unedited smartphone photo."
)
P_QWEN_NEG = (
    "studio lighting, cinematic color grading, HDR, beauty filter, retouched "
    "glamour skin, airbrushed plastic skin, oversaturated colors, glossy "
    "highlights, razor sharpness, portrait-mode bokeh, warm golden tint, "
    "watermark, text, caption, logo, extra fingers, deformed hands, changed "
    "background, moved objects, clothing from the second image"
)

# Ideogram character-edit inpaints INSIDE a mask — it describes the desired
# person textually (it cannot reference "Image 1/2"). Per-pair prompts.
P_IDEOGRAM: dict[str, str] = {
    "acv": (
        "An older man in his late 50s with swept-back grey hair, a short grey "
        "beard and light skin — the person from the reference image — crouching "
        "barefoot on the lawn with his right foot inside a plastic tub of "
        "brown vinegar water, shirtless, wearing the same grey shorts, hands "
        "resting where the original hands were, looking directly into the "
        "camera with a natural composed expression. Ordinary unedited phone "
        "photo, mundane daylight, neutral white balance, natural skin with "
        "visible pores, no retouching."
    ),
    "feet": (
        "A middle-aged East Asian man with short dark hair and thin round "
        "metal glasses — the person from the reference image — kneeling on the "
        "stone patio pouring avocado oil from a glass bottle onto his raised "
        "bare foot, shirtless, wearing the same dark shorts, same exact pose, "
        "looking directly into the camera with a natural composed expression. "
        "Ordinary unedited phone photo, mundane daylight, neutral white "
        "balance, natural skin with visible pores, no retouching."
    ),
}

# ------------------------------------------------------------------ helpers

def data_uri(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


_FAL_URL_CACHE: dict[str, str] = {}


def fal_url(path: Path) -> str:
    """Upload a local file to fal's CDN and return its URL (cached).

    Replaces multi-MB base64 data URIs in JSON bodies — those intermittently
    blew up TLS (SSLV3_ALERT_BAD_RECORD_MAC) during wave 1."""
    key = str(path)
    if key not in _FAL_URL_CACHE:
        import os
        os.environ.setdefault("FAL_KEY", settings.fal_api_key)
        import fal_client
        _FAL_URL_CACHE[key] = fal_client.upload_file(str(path))
    return _FAL_URL_CACHE[key]


def fal_queue(model_id: str, payload: dict, timeout: float = 420.0) -> dict:
    """Submit to fal's queue API and poll to completion. Returns response JSON."""
    headers = {"Authorization": f"Key {settings.fal_api_key}",
               "Content-Type": "application/json"}
    with httpx.Client(timeout=120) as c:
        r = c.post(f"https://queue.fal.run/{model_id}", json=payload, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"fal submit {model_id}: HTTP {r.status_code} {r.text[:400]}")
        sub = r.json()
        status_url = sub.get("status_url")
        response_url = sub.get("response_url")
        if not status_url:
            return sub
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(f"fal {model_id}: timed out")
            sr = c.get(status_url, headers=headers)
            st = sr.json().get("status")
            if st == "COMPLETED":
                break
            if st in {"FAILED", "ERROR"}:
                raise RuntimeError(f"fal {model_id}: {sr.text[:400]}")
            time.sleep(3)
        rr = c.get(response_url, headers=headers)
        if rr.status_code >= 400:
            raise RuntimeError(f"fal result {model_id}: HTTP {rr.status_code} {rr.text[:400]}")
        return rr.json()


def fal_first_image(resp: dict) -> bytes:
    imgs = resp.get("images") or ([resp["image"]] if resp.get("image") else [])
    if not imgs:
        raise RuntimeError(f"fal: no images in response keys={list(resp.keys())}")
    url = imgs[0]["url"] if isinstance(imgs[0], dict) else imgs[0]
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    with httpx.Client(timeout=120) as c:
        d = c.get(url)
        d.raise_for_status()
        return d.content


# ------------------------------------------------------------------ engines

def run_gpt(scene: Path, char: Path, prompt: str, dest: Path) -> None:
    pipeline.generate_image(scene_image=scene, character_image=char,
                            character_name="eval", dest=dest, prompt=prompt,
                            job_id="eval")


def run_prod_fal(slug: str, scene: Path, char: Path, prompt: str, dest: Path) -> None:
    """Exercise the PRODUCTION client path (clients/fal_image.swap_image)."""
    data = fal_image.swap_image(model_slug=slug, scene_image=scene,
                                character_image=char, prompt=prompt,
                                aspect_ratio="9:16", app_job_id="eval")
    dest.write_bytes(data)


def run_fal_two_image(model_id: str, scene: Path, char: Path, prompt: str,
                      dest: Path, *, extra: dict | None = None) -> None:
    payload = {"prompt": prompt, "image_urls": [fal_url(scene), fal_url(char)],
               "num_images": 1, **(extra or {})}
    resp = fal_queue(model_id, payload)
    dest.write_bytes(fal_first_image(resp))


def run_gpt_flipped(scene: Path, char: Path, dest: Path) -> None:
    """gpt-image-2 with [character, scene] input order — research: the FIRST
    input image's face is preserved with extra richness; the app's [scene,
    char] order may have been self-inflicting the identity loss."""
    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key)
    with open(char, "rb") as f_char, open(scene, "rb") as f_scene:
        result = client.images.edit(
            model=settings.openai_image_model,
            image=[f_char, f_scene],          # FLIPPED: identity first
            prompt=P_GPT_FLIP,
            size="1024x1536",
            quality="high",
            n=1,
        )
    dest.write_bytes(base64.b64decode(result.data[0].b64_json))


def run_grok_edit(scene: Path, char: Path, prompt: str, dest: Path) -> None:
    """xAI /v1/images/edits — multi-image JSON edit endpoint (post-dates
    grok.py, which only wires text-to-image). Lax moderation tier makes it the
    designated fallback for scenes GPT refuses."""
    headers = {"Authorization": f"Bearer {settings.xai_api_key}",
               "Content-Type": "application/json"}
    body = {
        "model": "grok-imagine-image-quality",
        "prompt": prompt,
        "images": [
            {"type": "image_url", "url": data_uri(scene)},
            {"type": "image_url", "url": data_uri(char)},
        ],
    }
    with httpx.Client(timeout=300) as c:
        r = c.post("https://api.x.ai/v1/images/edits", json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"grok edits: HTTP {r.status_code} {r.text[:400]}")
        data = r.json()
    out = (data.get("data") or [{}])[0]
    if out.get("b64_json"):
        dest.write_bytes(base64.b64decode(out["b64_json"]))
    elif out.get("url"):
        with httpx.Client(timeout=120) as c:
            d = c.get(out["url"]); d.raise_for_status()
            dest.write_bytes(d.content)
    else:
        raise RuntimeError(f"grok edits: no image in response keys={list(data.keys())}")


def _person_mask(scene: Path) -> str:
    """BiRefNet matte of the scene -> threshold -> 12px dilate -> white-on-black
    mask PNG at the scene's exact dimensions. Returns a fal CDN URL. Pure PIL."""
    import io
    from PIL import Image, ImageFilter

    resp = fal_queue("fal-ai/birefnet/v2", {"image_url": fal_url(scene)})
    matte_url = (resp.get("image") or {}).get("url") or resp["images"][0]["url"]
    with httpx.Client(timeout=120) as c:
        matte_bytes = c.get(matte_url).content

    with Image.open(scene) as sc:
        w, h = sc.size
    matte = Image.open(io.BytesIO(matte_bytes)).convert("L").resize((w, h))
    binary = matte.point(lambda v: 255 if v > 64 else 0)
    # ~12px dilation via MaxFilter (odd kernel).
    dilated = binary.filter(ImageFilter.MaxFilter(25))
    mask_path = OUT / f"mask_{scene.stem}.png"
    dilated.save(mask_path)
    return fal_url(mask_path)


def run_ideogram_inpaint(scene: Path, char: Path, pair_key: str, dest: Path) -> None:
    """Pixel-exact lane: BiRefNet person mask + Ideogram V3 Character Edit.
    Everything outside the mask is bit-identical to the scene by construction."""
    mask_url = _person_mask(scene)
    payload = {
        "prompt": P_IDEOGRAM[pair_key],
        "image_url": fal_url(scene),
        "mask_url": mask_url,
        "reference_image_urls": [fal_url(char)],
        "rendering_speed": "QUALITY",
        "expand_prompt": False,
        "num_images": 1,
    }
    resp = fal_queue("fal-ai/ideogram/character/edit", payload)
    dest.write_bytes(fal_first_image(resp))


def run_fal_easel(scene: Path, char: Path, dest: Path) -> None:
    # NOTE: endpoint is DEPRECATED per fal (kept as bake-off evidence only —
    # excluded from production integration).
    payload = {
        "face_image_0": fal_url(char),
        "gender_0": "male",
        "target_image": fal_url(scene),
        "workflow_type": "target_hair",
        "upscale": False,
    }
    resp = fal_queue("easel-ai/advanced-face-swap", payload)
    dest.write_bytes(fal_first_image(resp))


def run_higgsfield(scene: Path, char: Path, prompt: str, strength: float,
                   dest: Path) -> None:
    """Inline variant of generate_swap with tunable custom_reference_strength."""
    with higgsfield._client() as client:
        scene_url = higgsfield._upload(scene, client)
        ref_id = higgsfield._ensure_reference(char, client)
        params = {
            "prompt": prompt,
            "custom_reference_id": ref_id,
            "custom_reference_strength": strength,
            settings.higgsfield_scene_field: {"type": "image_url", "image_url": scene_url},
            "width_and_height": "1152x2048",
            "quality": "1080p",
            "batch_size": 1,
            "enhance_prompt": False,
        }
        r = client.post("/v1/text2image/soul", json={"params": params})
        higgsfield._raise_for_status(r, "submit soul")
        data = r.json()
        status = higgsfield._extract_status(data)
        job_set_id = higgsfield._job_set_id(data) or ""
        status_url = higgsfield._status_url(data, job_set_id)
        deadline = time.monotonic() + 300
        while status == "pending":
            if time.monotonic() > deadline:
                raise RuntimeError("higgsfield: timed out")
            time.sleep(3)
            pr = client.get(status_url)
            higgsfield._raise_for_status(pr, "poll soul")
            data = pr.json()
            status = higgsfield._extract_status(data)
        if status != "completed":
            raise RuntimeError(f"higgsfield: {status}")
        url = higgsfield._extract_result_url(data)
        with httpx.Client(timeout=120) as raw:
            d = raw.get(url)
            d.raise_for_status()
            dest.write_bytes(d.content)


# ------------------------------------------------------------------ run matrix
# name -> fn(scene, char, dest). Names are suffixed with the pair key at run
# time: "<name>@<pair>" (e.g. qwen-compact@feet).

RUNS: dict[str, object] = {
    "gpt-full":          lambda s, c, d: run_gpt(s, c, P_FULL, d),
    "gpt-compact":       lambda s, c, d: run_gpt(s, c, P_COMPACT, d),
    # production fal client path — compact prompts
    "qwen-compact":      lambda s, c, d: run_prod_fal("qwen-edit-swap", s, c, P_COMPACT, d),
    "kontext-compact":   lambda s, c, d: run_prod_fal("kontext-max-swap", s, c, P_KONTEXT, d),
    "seedream-compact":  lambda s, c, d: run_prod_fal("seedream-edit-swap", s, c, P_COMPACT, d),
    # same engines with the app's long default prompt — does long hurt editors?
    "qwen-full":         lambda s, c, d: run_prod_fal("qwen-edit-swap", s, c, P_FULL, d),
    "kontext-full":      lambda s, c, d: run_prod_fal("kontext-max-swap", s, c, P_FULL, d),
    # dedicated whole-person face swap (keeps target outfit by design)
    "easel-faceswap":    lambda s, c, d: run_fal_easel(s, c, d),
    # current higgsfield path with tuned-down reference strength
    "higgsfield-0.85":   lambda s, c, d: run_higgsfield(s, c, P_COMPACT, 0.85, d),
    # comparison-only (Google model, banned in prod picker — evidence for report):
    "nano-banana-pro-fal": lambda s, c, d: run_fal_two_image(
        "fal-ai/nano-banana-pro/edit", s, c, P_COMPACT, d,
        extra={"aspect_ratio": "9:16"}),
}

# Wave 2 — research-driven arms (added after the synthesis landed).
RUNS_W2: dict[str, object] = {
    # Rank 1: NBP edit with the research master prompt.
    "nbp-master":        lambda s, c, d: run_fal_two_image(
        "fal-ai/nano-banana-pro/edit", s, c, P_MASTER, d,
        extra={"aspect_ratio": "9:16", "resolution": "1K"}),
    # Rank 7: NB2, same prompt verbatim (isolates the model variable).
    "nb2-master":        lambda s, c, d: run_fal_two_image(
        "fal-ai/nano-banana-2/edit", s, c, P_MASTER, d,
        extra={"aspect_ratio": "9:16", "resolution": "1K"}),
    # Rank 2: Seedream bumped to v4.5.
    "seedream45-master": lambda s, c, d: run_fal_two_image(
        "fal-ai/bytedance/seedream/v4.5/edit", s, c, P_MASTER, d,
        extra={"image_size": {"width": 1152, "height": 2048}}),
    # Rank 3: gpt-image with FLIPPED input order ([char, scene]).
    "gpt-flipped":       lambda s, c, d: run_gpt_flipped(s, c, d),
    # Rank 5: Qwen with a REAL negative_prompt split.
    "qwen-neg":          lambda s, c, d: run_fal_two_image(
        "fal-ai/qwen-image-edit-plus", s, c, P_QWEN_POS, d,
        extra={"negative_prompt": P_QWEN_NEG,
               "image_size": {"width": 1152, "height": 2048},
               "output_format": "png"}),
    # Rank 6: Grok multi-image edits (new xAI endpoint).
    "grok-edit":         lambda s, c, d: run_grok_edit(s, c, P_MASTER, d),
}

# Rank 4 needs the pair key for its per-pair literal prompt.
RUNS_W2_PAIRAWARE: dict[str, object] = {
    "ideogram-inpaint":  lambda s, c, p, d: run_ideogram_inpaint(s, c, p, d),
}

RUNS.update(RUNS_W2)


# ------------------------------------------------------------------ judge

JUDGE_SYSTEM = """You are a ruthless photo-compositing QA inspector for a \
character-swap pipeline. You will see three images in order:
1. ORIGINAL SCENE (the master photo whose everything must be preserved)
2. CHARACTER REFERENCE (the person whose identity must be transferred)
3. CANDIDATE RESULT (the swap output you are grading)

Grade the CANDIDATE on five 0-10 criteria. START AT 10 AND DEDUCT for every \
flaw you can name; a score above 8 is reserved for results where you actively \
looked for the flaw and could not find it. Most real outputs score 4-7. Never \
award round high scores out of politeness — your scores gate a production \
pipeline and inflated scores ship bad content.

1. scene_fidelity: framing, crop, camera angle, every prop (count, position, \
label text), background, and the subject's pose/clothing match the ORIGINAL \
SCENE. Any reframe, moved/missing/added object, changed outfit, or changed \
pose = deductions proportional to severity. A regenerated lookalike scene \
(same idea, different pixels) scores <=3.
2. identity_match: the person's face (and hair where visible) reads as the \
CHARACTER REFERENCE person. A generic person of the same demographic = 3-4. \
Identity bleed from the original scene subject = deduct hard.
3. integration: the inserted person is lit by the scene's own light — \
direction, color temperature, softness; contact shadows exist where body \
meets surfaces; edges blend (no halo/cutout line); the person's sharpness and \
grain match the scene. A visibly pasted-on face or head = <=3.
4. organic_realism: looks like an ordinary unedited phone photo — plain \
colors, neutral white balance, mild softness. AI gloss, beauty-filter skin, \
cinematic grading, HDR pop, or studio lighting = deductions.
5. artifact_free: anatomy (hands, fingers, eyes, teeth), garbled text/logos, \
warped props, duplicated limbs. Each visible artifact deducts 2-4.

Also report `fatal`: true if ANY single flaw alone makes this unusable for a \
believable everyday social-media photo (e.g. pasted-on face, wrong identity, \
regenerated scene, deformed hand in plain view).

Use the score_swap tool. Be specific in `flaws` — name what you saw."""

JUDGE_TOOL = {
    "name": "score_swap",
    "description": "Submit the QA scores for the candidate swap image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scene_fidelity": {"type": "number"},
            "identity_match": {"type": "number"},
            "integration": {"type": "number"},
            "organic_realism": {"type": "number"},
            "artifact_free": {"type": "number"},
            "fatal": {"type": "boolean"},
            "flaws": {"type": "array", "items": {"type": "string"}},
            "verdict": {"type": "string", "description": "one sentence"},
        },
        "required": ["scene_fidelity", "identity_match", "integration",
                     "organic_realism", "artifact_free", "fatal", "flaws",
                     "verdict"],
    },
}

# Weights reflect Hugo's stated unusable-killers: pasted-on look, wrong
# identity, AI-perfect look — then scene fidelity, then artifacts.
WEIGHTS = {"integration": 0.25, "identity_match": 0.25, "organic_realism": 0.20,
           "scene_fidelity": 0.20, "artifact_free": 0.10}


def judge_one(scene: Path, char: Path, candidate: Path) -> dict:
    from character_swap.clients import anthropic_client as ac
    blocks = [
        {"type": "text", "text": "1. ORIGINAL SCENE:"},
        ac._file_to_image_block(scene),
        {"type": "text", "text": "2. CHARACTER REFERENCE:"},
        ac._file_to_image_block(char),
        {"type": "text", "text": "3. CANDIDATE RESULT:"},
        ac._file_to_image_block(candidate),
        {"type": "text", "text": "Grade the candidate now with score_swap."},
    ]
    resp = ac.messages_with_tools(
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": blocks}],
        tools=[JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "score_swap"},
        max_tokens=2048,
        temperature=0.0,
        phase="swap_judge",          # unmapped in call_log -> $0 recorded
        character="judge",
        model="claude-sonnet-4-6",   # vision-capable, cheap, deterministic enough
    )
    scores = ac.extract_tool_call(resp, "score_swap") or {}
    if scores:
        scores["composite"] = round(sum(
            float(scores.get(k, 0)) * w for k, w in WEIGHTS.items()), 2)
    return scores


# ------------------------------------------------------------------ main

def load_results() -> dict:
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else {}


def save_results(results: dict) -> None:
    RESULTS.write_text(json.dumps(results, indent=2))


def generate(only: set[str]) -> None:
    results = load_results()

    def _one(run_id: str, pair_key: str, invoke) -> None:
        if only and run_id.split("@")[0] not in only and run_id not in only:
            return
        if results.get(run_id, {}).get("ok"):
            print(f"SKIP {run_id:28} (already done)")
            return
        dest = OUT / f"{run_id}.png"
        t0 = time.time()
        try:
            invoke(dest)
            results[run_id] = {"ok": True, "secs": round(time.time() - t0, 1),
                               "bytes": dest.stat().st_size, "pair": pair_key}
            print(f"OK   {run_id:28} {results[run_id]['secs']}s")
        except Exception as e:
            results[run_id] = {"ok": False, "secs": round(time.time() - t0, 1),
                               "error": f"{type(e).__name__}: {e}", "pair": pair_key}
            print(f"FAIL {run_id:28} {results[run_id]['error'][:160]}")
            traceback.print_exc(limit=1)
        save_results(results)

    for pair_key, (scene, char) in PAIRS.items():
        for name, fn in RUNS.items():
            _one(f"{name}@{pair_key}", pair_key,
                 lambda d, fn=fn, s=scene, c=char: fn(s, c, d))
        for name, fn in RUNS_W2_PAIRAWARE.items():
            _one(f"{name}@{pair_key}", pair_key,
                 lambda d, fn=fn, s=scene, c=char, p=pair_key: fn(s, c, p, d))


def realism_pass() -> None:
    """Create a '<run>+real' degraded twin of every OK primary output via the
    deterministic camera-pipeline pass (src/character_swap/realism.py). Free,
    judged like any other arm — measures whether the degrade lifts
    organic_realism without hurting identity/integration."""
    from character_swap import realism
    results = load_results()
    for run_id, r in list(results.items()):
        if not r.get("ok") or run_id.split("@")[0].endswith("+real"):
            continue
        name, pair_key = run_id.split("@")
        twin_id = f"{name}+real@{pair_key}"
        if results.get(twin_id, {}).get("ok"):
            continue
        src = OUT / f"{run_id}.png"
        if not src.exists():
            continue
        dest = OUT / f"{twin_id}.png"
        try:
            dest.write_bytes(realism.degrade_to_phone_photo(src.read_bytes(), seed=42))
            results[twin_id] = {"ok": True, "secs": 0.1,
                                "bytes": dest.stat().st_size, "pair": pair_key}
            print(f"REAL {twin_id}")
        except Exception as e:
            results[twin_id] = {"ok": False, "secs": 0.0, "pair": pair_key,
                                "error": f"{type(e).__name__}: {e}"}
            print(f"REAL-FAIL {twin_id}: {e}")
    save_results(results)


def judge() -> None:
    results = load_results()
    for run_id, r in results.items():
        if not r.get("ok") or r.get("judge"):
            continue
        pair_key = r.get("pair") or run_id.split("@")[-1]
        scene, char = PAIRS[pair_key]
        candidate = OUT / f"{run_id}.png"
        if not candidate.exists():
            continue
        try:
            scores = judge_one(scene, char, candidate)
            r["judge"] = scores
            print(f"JUDGED {run_id:26} composite={scores.get('composite')} "
                  f"fatal={scores.get('fatal')} — {scores.get('verdict', '')[:80]}")
        except Exception as e:
            print(f"JUDGE-FAIL {run_id}: {type(e).__name__}: {e}")
        save_results(results)


def gallery() -> None:
    results = load_results()
    sections = []
    for pair_key, (scene, char) in PAIRS.items():
        cells = [
            f'<div class="c"><h3>ORIGINAL SCENE</h3><img src="{scene}"/></div>',
            f'<div class="c"><h3>CHARACTER</h3><img src="{char}"/></div>',
        ]
        ranked = sorted(
            ((rid, r) for rid, r in results.items()
             if (r.get("pair") == pair_key or rid.endswith(f"@{pair_key}"))),
            key=lambda kv: -(kv[1].get("judge", {}).get("composite") or -1))
        for rid, r in ranked:
            name = rid.split("@")[0]
            if r.get("ok"):
                j = r.get("judge") or {}
                score = (f'<p class="s">comp <b>{j.get("composite", "?")}</b>'
                         f' · scene {j.get("scene_fidelity", "?")} · id {j.get("identity_match", "?")}'
                         f' · integ {j.get("integration", "?")} · organic {j.get("organic_realism", "?")}'
                         f'{" · <b class=err>FATAL</b>" if j.get("fatal") else ""}</p>'
                         f'<p class="v">{j.get("verdict", "")}</p>') if j else ""
                cells.append(f'<div class="c"><h3>{name} · {r["secs"]}s</h3>'
                             f'<img src="{rid}.png"/>{score}</div>')
            else:
                cells.append(f'<div class="c"><h3>{name}</h3>'
                             f'<p class="err">{r["error"][:300]}</p></div>')
        sections.append(f'<h2>Pair: {pair_key}</h2><div class="g">{"".join(cells)}</div>')
    html = ('<style>body{background:#111;color:#eee;font-family:sans-serif;padding:16px}'
            '.g{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:32px}.c{width:300px}'
            '.c img{width:100%;border-radius:8px}.err{color:#f66;font-size:12px}'
            '.s{font-size:12px;color:#9fd}.v{font-size:11px;color:#aaa}</style>'
            f'<h1>Swap engine eval</h1>{"".join(sections)}')
    (OUT / "gallery.html").write_text(html)
    print(f"gallery: {OUT}/gallery.html")


def main() -> None:
    args = set(sys.argv[1:])
    if args == {"judge"}:
        judge(); gallery(); return
    if args == {"gallery"}:
        gallery(); return
    if args == {"realism"}:
        realism_pass(); judge(); gallery(); return
    generate(args - {"judge", "gallery", "realism"})
    realism_pass()
    judge()
    gallery()


if __name__ == "__main__":
    main()
