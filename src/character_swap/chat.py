"""Claude-driven Chat tab: agent loop + tool dispatcher + tool definitions.

The user types a natural-language request in the Chat tab. We forward the
conversation to Anthropic Claude with a set of tool definitions matching our
existing endpoints (image gen, video gen, swap jobs, captioning, etc).
Claude responds with `tool_use` blocks; we execute the corresponding action
against our own runner code in-process, append the result, and loop until
Claude stops asking for tools (`stop_reason == "end_turn"`).

This module is async because most of our underlying runners are async.

State model: each chat is a `models.ChatSession` row whose `messages` field
stores the full Anthropic message list verbatim (including tool_use /
tool_result content blocks). The `media` field is a flat side-list of
generations the chat produced, so the UI can render thumbnails in a
sidebar / strip without re-walking the message log.

Note on cost: Claude calls go through `anthropic_client.messages_with_tools`
which bills via `call_log.record(phase="chat", ...)`. Each tool that triggers
a downstream generation (image/video/etc) bills again via the existing
runner_media / pipeline call_log entries. So the user sees both Claude
cost AND tool-execution cost in `state/calls.jsonl`.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from character_swap import models
from character_swap.clients import anthropic_client
from character_swap.config import settings
from character_swap.state import store


SYSTEM_PROMPT = """You are a helpful AI assistant embedded in Character Swap Studio, \
a local web app Hugo uses to create AI-generated content (images, videos, talking-head \
avatars, voiceover audio, B-roll, character-swap pipelines, and captioned video edits).

You have access to tools that drive the same pipelines as the app's tabs. \
When the user describes what they want to create, pick the right tool(s), \
call them with sensible defaults, and report back with what you made.

Key guidelines:
- The user is Hugo. He's a solo creator working in Swedish or English — match his language.
- Prefer 9:16 aspect ratio for short-form social content unless he says otherwise.
- For image generation, default to `gpt-image` (highest quality, costs ~$0.04/image).
- For video generation, default to `grok-imagine` (good quality, 9:16-friendly).
- When you call a tool that returns a media URL, the UI automatically renders \
  it inline in the chat — you don't need to repeat the URL in your text response, \
  just describe what you made.
- If a tool fails because an API key is missing, tell Hugo which key to set in `.env` \
  and which provider it's from (e.g. "FAL_API_KEY at fal.ai/dashboard/keys").
- If the user asks something you don't have a tool for, say so plainly and \
  suggest the closest equivalent."""


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool-use JSON schemas)
# ---------------------------------------------------------------------------

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "generate_image",
        "description": (
            "Generate a single image from a text prompt. Returns the image URL "
            "when ready. Defaults to gpt-image at 9:16. Pass reference_paths for "
            "image-to-image (e.g. to keep a character's face consistent)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text describing the image."},
                "model": {
                    "type": "string",
                    "description": (
                        "Image model slug. Options include: gpt-image, dall-e-3, "
                        "grok-image, nano-banana, nano-banana-pro, flux-1.1-pro-ultra, "
                        "flux-pro, flux-schnell, flux-kontext, ideogram-3, recraft-v3, "
                        "sd3.5-large, seedream-3, higgsfield-soul. Use list_available_models "
                        "if unsure which are configured."
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1", "9:16", "16:9", "4:3", "3:4"],
                    "description": "Aspect ratio. Default 9:16 for short-form social.",
                },
                "reference_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional reference image paths (e.g. /files/characters/abc.png).",
                },
                "enrich_prompt": {
                    "type": "boolean",
                    "description": "Cheap GPT-4o prompt expansion (default false).",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_video",
        "description": (
            "Animate a start frame into a short video clip via the chosen video "
            "model. Requires reference_paths[0] as the start frame. Default model "
            "is grok-imagine. Returns the video URL when ready."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Motion / shot direction."},
                "reference_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Required: at least one entry — the start frame to animate.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Video model. Options: grok-imagine, veo-3, veo-3-fast, "
                        "kling-2.1-pro, kling-2.0, kling-1.6, runway-gen-4, runway-gen-3, "
                        "luma-ray-2, pika-2.2, hailuo-02, hailuo-01, sora-2, wan-2.2, "
                        "wan-2.1, seedance, higgsfield-dop, higgsfield-lipsync."
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1", "9:16", "16:9"],
                },
                "duration_secs": {
                    "type": "integer",
                    "description": "Clip length in seconds (typically 5-15).",
                },
            },
            "required": ["prompt", "reference_paths"],
        },
    },
    {
        "name": "generate_audio",
        "description": (
            "Generate spoken audio via ElevenLabs. Two modes: 'tts' takes a script "
            "and a voice_id; 'voice_changer' takes an existing audio file path and "
            "swaps the speaker's voice. Returns mp3 URL when done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["tts", "voice_changer"]},
                "prompt": {
                    "type": "string",
                    "description": "Script text (tts mode) or source audio path (voice_changer mode).",
                },
                "voice_id": {
                    "type": "string",
                    "description": "ElevenLabs voice_id. Use list_elevenlabs_voices to find one.",
                },
            },
            "required": ["mode", "prompt", "voice_id"],
        },
    },
    {
        "name": "generate_avatar",
        "description": (
            "Generate a talking-head avatar video via HeyGen. Two models: "
            "'heygen-avatar-5' uses a catalogue avatar + voice_id + script. "
            "'heygen-photo-avatar' uses a reference photo as the speaking subject. "
            "Returns video URL when done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "enum": ["heygen-avatar-5", "heygen-photo-avatar"]},
                "prompt": {"type": "string", "description": "Script the avatar will say."},
                "avatar_id": {"type": "string", "description": "HeyGen avatar_id for heygen-avatar-5."},
                "voice_id": {"type": "string", "description": "HeyGen voice_id (or ElevenLabs if voice_provider='elevenlabs')."},
                "reference_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For heygen-photo-avatar: [0] is the speaking-subject photo.",
                },
                "voice_provider": {"type": "string", "enum": ["heygen", "elevenlabs"]},
            },
            "required": ["model", "prompt", "voice_id"],
        },
    },
    {
        "name": "create_swap_job",
        "description": (
            "Kick off a full Character Swap job: for each (scene, character) pair "
            "generate `images_per_character` variants. Returns the job_id; check "
            "progress via get_job_status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Scene asset IDs. Get them via list_scenes.",
                },
                "character_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Character asset IDs. Get them via list_characters.",
                },
                "images_per_character": {"type": "integer", "default": 1},
                "image_model": {"type": "string"},
                "prompt": {"type": "string", "description": "Optional override of the default swap prompt."},
                "project_id": {"type": "string", "description": "Optional project the job belongs to."},
                "title": {"type": "string", "description": "Human-readable job title."},
            },
            "required": ["scene_ids", "character_ids"],
        },
    },
    {
        "name": "caption_video",
        "description": (
            "Run the Editor caption pipeline on a finished video. Returns the "
            "captioned video URL when done. For top quality use a veed-* template "
            "(cloud rendering on fal.ai, ~$0.10/min). Defaults to veed-yellow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "video_path": {
                    "type": "string",
                    "description": "Path to the source video (e.g. /files/output/.../video.mp4).",
                },
                "template": {
                    "type": "string",
                    "description": (
                        "Template slug. Cloud-rendered: veed-yellow, veed-purple, "
                        "veed-center, veed-mrbeast. Local Remotion: submagic-pro, "
                        "submagic-pop, mrbeast-bold, capcut-glow. Local ASS (lowest "
                        "quality): popout-yellow, tiktok-pop, instagram-center, etc."
                    ),
                },
                "voice_id": {"type": "string", "description": "Optional ElevenLabs voice swap."},
                "enable_trim": {"type": "boolean", "default": True},
                "enable_wpm_normalize": {"type": "boolean", "default": True},
                "target_wpm": {"type": "number", "default": 190},
            },
            "required": ["video_path"],
        },
    },
    {
        "name": "generate_broll",
        "description": (
            "Generate cinematic B-roll for a narration audio/video file. Whisper "
            "transcribes, GPT-4o plans visuals, Grok generates clips per phrase. "
            "Returns broll_id; pause at awaiting_approval for review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "audio_path": {
                    "type": "string",
                    "description": "Path to the narration source (audio or video).",
                },
                "aspect_ratio": {"type": "string", "enum": ["1:1", "9:16", "16:9"]},
            },
            "required": ["audio_path"],
        },
    },
    {
        "name": "list_characters",
        "description": "List every character in the library with id, name, and primary image URL.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_scenes",
        "description": "List every uploaded scene (background) with id and image URL.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_available_models",
        "description": (
            "Report which providers have API keys configured. Use this when "
            "the user asks 'what can I generate' or before picking a model you're "
            "unsure is available."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_elevenlabs_voices",
        "description": "List the user's ElevenLabs voices (id + name + category).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_generation_status",
        "description": (
            "Poll a previously-started generation by id. Returns current status, "
            "URL when done, error if failed. Use after a long-running call has "
            "timed out the synchronous wait."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"generation_id": {"type": "string"}},
            "required": ["generation_id"],
        },
    },
    {
        "name": "get_job_status",
        "description": "Poll a Swap job by id. Returns char statuses + approved variants + video statuses.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _file_url(path: Path | str) -> str:
    """Convert an output/input path to the HTTP /files/... URL the UI can render."""
    p = Path(path).resolve()
    for mount, root in (
        ("output", settings.output_dir.resolve()),
        ("input/scenes", settings.scenes_dir.resolve()),
        ("characters", settings.characters_dir.resolve()),
    ):
        try:
            rel = p.relative_to(root)
            return f"/files/{mount}/{rel.as_posix()}"
        except ValueError:
            continue
    return str(p)


def _record_media(chat: models.ChatSession, **fields) -> None:
    """Append a generation reference to the chat's media side-list."""
    chat.media.append({
        "created_at": datetime.utcnow().isoformat() + "Z",
        **fields,
    })


async def _tool_generate_image(args: dict, chat: models.ChatSession) -> dict:
    from character_swap import runner_media
    from character_swap.models import GenKind, GenStatus
    gen_id = "gen_" + secrets.token_hex(6)
    gen = models.MediaGeneration(
        gen_id=gen_id,
        kind=GenKind.IMAGE,
        model=args.get("model", "gpt-image"),
        prompt=args["prompt"],
        reference_paths=args.get("reference_paths", []),
        aspect_ratio=args.get("aspect_ratio", "9:16"),
        enrich_prompt=bool(args.get("enrich_prompt", False)),
        status=GenStatus.PENDING,
    )
    store().add_generation(gen)
    try:
        await runner_media.run_image_gen(gen_id)
    except Exception as e:
        return {"status": "failed", "generation_id": gen_id,
                "error": f"{type(e).__name__}: {e}"}
    gen = store().get_generation(gen_id)
    if gen and gen.status == GenStatus.DONE and gen.output_path:
        url = _file_url(gen.output_path)
        _record_media(chat, kind="image", generation_id=gen_id, url=url,
                      prompt=gen.prompt, model=gen.model)
        return {"status": "done", "generation_id": gen_id, "url": url,
                "model": gen.model, "prompt": gen.prompt}
    return {"status": gen.status.value if gen else "missing",
            "generation_id": gen_id,
            "error": gen.error if gen else "generation row missing"}


async def _tool_generate_video(args: dict, chat: models.ChatSession) -> dict:
    from character_swap import runner_media
    from character_swap.models import GenKind, GenStatus
    refs = args.get("reference_paths") or []
    if not refs:
        return {"status": "failed", "error": "generate_video requires reference_paths[0] (start frame)"}
    gen_id = "gen_" + secrets.token_hex(6)
    gen = models.MediaGeneration(
        gen_id=gen_id,
        kind=GenKind.VIDEO,
        model=args.get("model", "grok-imagine"),
        prompt=args["prompt"],
        reference_paths=refs,
        aspect_ratio=args.get("aspect_ratio", "9:16"),
        duration_secs=args.get("duration_secs"),
        status=GenStatus.PENDING,
    )
    store().add_generation(gen)
    try:
        await runner_media.run_video_gen(gen_id)
    except Exception as e:
        return {"status": "failed", "generation_id": gen_id,
                "error": f"{type(e).__name__}: {e}"}
    gen = store().get_generation(gen_id)
    if gen and gen.status == GenStatus.DONE and gen.output_path:
        url = _file_url(gen.output_path)
        _record_media(chat, kind="video", generation_id=gen_id, url=url,
                      prompt=gen.prompt, model=gen.model)
        return {"status": "done", "generation_id": gen_id, "url": url,
                "model": gen.model, "prompt": gen.prompt}
    return {"status": gen.status.value if gen else "missing",
            "generation_id": gen_id,
            "error": gen.error if gen else "generation row missing"}


async def _tool_generate_audio(args: dict, chat: models.ChatSession) -> dict:
    from character_swap import runner_media
    from character_swap.models import GenKind, GenStatus
    mode = args["mode"]
    model_slug = "elevenlabs-tts" if mode == "tts" else "elevenlabs-vc"
    gen_id = "gen_" + secrets.token_hex(6)
    gen = models.MediaGeneration(
        gen_id=gen_id,
        kind=GenKind.AUDIO,
        model=model_slug,
        prompt=args["prompt"],
        voice_id=args["voice_id"],
        voice_provider="elevenlabs",
        status=GenStatus.PENDING,
        reference_paths=[args["prompt"]] if mode == "voice_changer" else [],
    )
    store().add_generation(gen)
    try:
        await runner_media.run_audio_gen(gen_id)
    except Exception as e:
        return {"status": "failed", "generation_id": gen_id, "error": str(e)}
    gen = store().get_generation(gen_id)
    if gen and gen.status == GenStatus.DONE and gen.output_path:
        url = _file_url(gen.output_path)
        _record_media(chat, kind="audio", generation_id=gen_id, url=url,
                      prompt=gen.prompt, model=gen.model)
        return {"status": "done", "generation_id": gen_id, "url": url}
    return {"status": gen.status.value if gen else "missing",
            "generation_id": gen_id,
            "error": gen.error if gen else "row missing"}


async def _tool_generate_avatar(args: dict, chat: models.ChatSession) -> dict:
    from character_swap import runner_media
    from character_swap.models import GenKind, GenStatus
    gen_id = "gen_" + secrets.token_hex(6)
    gen = models.MediaGeneration(
        gen_id=gen_id,
        kind=GenKind.AVATAR,
        model=args["model"],
        prompt=args["prompt"],
        voice_id=args["voice_id"],
        avatar_id=args.get("avatar_id"),
        voice_provider=args.get("voice_provider", "heygen"),
        reference_paths=args.get("reference_paths", []),
        status=GenStatus.PENDING,
    )
    store().add_generation(gen)
    try:
        await runner_media.run_avatar_gen(gen_id)
    except Exception as e:
        return {"status": "failed", "generation_id": gen_id, "error": str(e)}
    gen = store().get_generation(gen_id)
    if gen and gen.status == GenStatus.DONE and gen.output_path:
        url = _file_url(gen.output_path)
        _record_media(chat, kind="avatar", generation_id=gen_id, url=url,
                      prompt=gen.prompt, model=gen.model)
        return {"status": "done", "generation_id": gen_id, "url": url}
    return {"status": gen.status.value if gen else "missing",
            "generation_id": gen_id,
            "error": gen.error if gen else "row missing"}


async def _tool_create_swap_job(args: dict, chat: models.ChatSession) -> dict:
    from character_swap.models import Job, JobCharacter
    s = store()
    scene_ids = args["scene_ids"]
    character_ids = args["character_ids"]
    # Validate
    for sid in scene_ids:
        if sid not in s.state.scenes:
            return {"status": "failed", "error": f"unknown scene_id: {sid}"}
    for cid in character_ids:
        if cid not in s.state.characters:
            return {"status": "failed", "error": f"unknown character_id: {cid}"}

    job_id = "job_" + secrets.token_hex(6)
    primary_scene = s.state.scenes[scene_ids[0]]
    job = Job(
        job_id=job_id,
        title=args.get("title", "Chat-driven swap"),
        project_id=args.get("project_id"),
        scene_id=scene_ids[0],
        scene_ids=scene_ids,
        scene_image_path=str(settings.scenes_dir / primary_scene.filename),
        image_model=args.get("image_model", "gpt-image"),
        images_per_character=int(args.get("images_per_character", 1)),
        prompt=args.get("prompt"),
        characters={
            cid: JobCharacter(
                char_id=cid,
                name=s.state.characters[cid].name,
                source_image_path=str(settings.characters_dir / s.state.characters[cid].filename),
            ) for cid in character_ids
        },
    )
    s.add_job(job) if hasattr(s, "add_job") else None
    # Older store APIs use add_job; some only have update_job. Fallback below.
    if not s.get_job(job_id):
        try:
            s.update_job(job)
        except Exception:
            return {"status": "failed", "error": "could not persist job — store mismatch"}

    # Kick off in background; return immediately with job_id.
    from character_swap import runner
    asyncio.create_task(runner.run_job(job_id))
    return {"status": "started", "job_id": job_id,
            "n_chars": len(character_ids), "n_scenes": len(scene_ids),
            "hint": "Poll with get_job_status — first variants typically land in 30-60s."}


async def _tool_caption_video(args: dict, chat: models.ChatSession) -> dict:
    """Run captions on a finished video file. Calls the same primitives as
    /api/editor/auto_edit but with a known on-disk path instead of an upload."""
    from character_swap import video_edit
    raw_path = args["video_path"]
    # Translate /files/... URL back to a real path if needed.
    p = _path_from_url(raw_path)
    if not p.exists():
        return {"status": "failed", "error": f"video not found: {raw_path}"}
    template = args.get("template", "veed-yellow")
    if template not in video_edit.TEMPLATES:
        return {"status": "failed", "error": f"unknown template: {template}"}
    edit_id = "ed_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)
    out = edit_dir / "04-final.mp4"
    # Transcribe locally first (needed for ASS/Remotion; VEED ignores).
    try:
        words = await asyncio.to_thread(video_edit.transcribe_words, p, job_id=edit_id)
    except Exception as e:
        return {"status": "failed", "error": f"transcribe failed: {e}"}
    style = video_edit.style_from_params(template, None)
    try:
        await asyncio.to_thread(
            video_edit.render_captions, p, out,
            words=words, style=style, job_id=edit_id,
        )
    except Exception as e:
        return {"status": "failed", "error": f"caption render failed: {e}"}
    url = _file_url(out)
    _record_media(chat, kind="edit", generation_id=edit_id, url=url,
                  prompt=f"Captions via {template}", model=template)
    return {"status": "done", "edit_id": edit_id, "url": url, "template": template,
            "n_words": len(words)}


async def _tool_generate_broll(args: dict, chat: models.ChatSession) -> dict:
    # Defer to runner_broll. This is a long-running job; we return the broll_id
    # immediately so the user can review in the B-roll tab.
    from character_swap import runner_broll
    raw_path = args["audio_path"]
    p = _path_from_url(raw_path)
    if not p.exists():
        return {"status": "failed", "error": f"file not found: {raw_path}"}
    aspect = args.get("aspect_ratio", "9:16")
    try:
        broll_id = await runner_broll.start_broll(
            audio_path=p, aspect_ratio=aspect,
        )
    except AttributeError:
        # Older runner_broll API — fall back to inline coroutine.
        return {"status": "failed",
                "error": "runner_broll.start_broll not available; use the B-roll tab directly"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    return {"status": "started", "broll_id": broll_id,
            "hint": "Review clips in the B-roll tab; finalize when happy."}


def _tool_list_characters(_args: dict, _chat: models.ChatSession) -> dict:
    s = store()
    out = []
    for cid, ch in s.state.characters.items():
        out.append({
            "char_id": cid,
            "name": ch.name,
            "n_images": len(ch.images),
            "primary_image_url": f"/files/characters/{ch.filename}" if ch.filename else None,
            "voice_id": ch.voice_id or None,
        })
    return {"characters": out, "count": len(out)}


def _tool_list_scenes(_args: dict, _chat: models.ChatSession) -> dict:
    s = store()
    out = []
    for sid, sc in s.state.scenes.items():
        out.append({
            "scene_id": sid,
            "url": f"/files/input/scenes/{sc.filename}",
            "original_name": sc.original_name,
        })
    return {"scenes": out, "count": len(out)}


def _tool_list_available_models(_args: dict, _chat: models.ChatSession) -> dict:
    providers = {
        name: settings.has_provider(name)
        for name in ("openai", "anthropic", "xai", "gemini", "kling",
                     "bfl", "ideogram", "recraft", "stability", "runway",
                     "luma", "pika", "minimax", "bytedance", "alibaba",
                     "higgsfield", "heygen", "elevenlabs", "fal")
    }
    return {"providers": providers,
            "active": [k for k, v in providers.items() if v]}


def _tool_list_elevenlabs_voices(_args: dict, _chat: models.ChatSession) -> dict:
    if not settings.has_provider("elevenlabs"):
        return {"voices": [], "error": "ELEVENLABS_API_KEY not set"}
    try:
        from character_swap.clients import elevenlabs as _eleven
        voices = _eleven.list_voices()
        # Keep response short — trim to id+name+category.
        trimmed = [{"voice_id": v.get("voice_id"),
                    "name": v.get("name"),
                    "category": v.get("category")}
                   for v in voices[:50]]
        return {"voices": trimmed, "count": len(trimmed)}
    except Exception as e:
        return {"voices": [], "error": str(e)}


def _tool_get_generation_status(args: dict, _chat: models.ChatSession) -> dict:
    gen = store().get_generation(args["generation_id"])
    if not gen:
        return {"error": f"no such generation: {args['generation_id']}"}
    return {
        "generation_id": gen.gen_id,
        "kind": gen.kind.value,
        "status": gen.status.value,
        "url": _file_url(gen.output_path) if gen.output_path else None,
        "error": gen.error,
        "prompt": gen.prompt,
    }


def _tool_get_job_status(args: dict, _chat: models.ChatSession) -> dict:
    job = store().get_job(args["job_id"])
    if not job:
        return {"error": f"no such job: {args['job_id']}"}
    chars_summary = {}
    for cid, jc in job.characters.items():
        chars_summary[cid] = {
            "name": jc.name,
            "status": jc.status.value,
            "n_variants": len(jc.images),
            "n_approved": len(jc.approved_variant_ids or []),
            "n_videos_done": sum(1 for v in jc.videos if v.status.value == "done"),
        }
    return {
        "job_id": job.job_id,
        "title": job.title,
        "characters": chars_summary,
        "scene_ids": job.scene_ids,
    }


# Map tool name → dispatcher function (async or sync; we await everything).
TOOL_DISPATCHERS: dict[str, Any] = {
    "generate_image": _tool_generate_image,
    "generate_video": _tool_generate_video,
    "generate_audio": _tool_generate_audio,
    "generate_avatar": _tool_generate_avatar,
    "create_swap_job": _tool_create_swap_job,
    "caption_video": _tool_caption_video,
    "generate_broll": _tool_generate_broll,
    "list_characters": _tool_list_characters,
    "list_scenes": _tool_list_scenes,
    "list_available_models": _tool_list_available_models,
    "list_elevenlabs_voices": _tool_list_elevenlabs_voices,
    "get_generation_status": _tool_get_generation_status,
    "get_job_status": _tool_get_job_status,
}


async def _dispatch_tool(name: str, args: dict, chat: models.ChatSession) -> dict:
    fn = TOOL_DISPATCHERS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        result = fn(args, chat)
        if asyncio.iscoroutine(result):
            result = await result
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _path_from_url(url: str) -> Path:
    """Reverse of _file_url — accept either a /files/... URL or a real path."""
    if url.startswith("/files/output/"):
        return settings.output_dir / url[len("/files/output/"):]
    if url.startswith("/files/input/scenes/"):
        return settings.scenes_dir / url[len("/files/input/scenes/"):]
    if url.startswith("/files/characters/"):
        return settings.characters_dir / url[len("/files/characters/"):]
    return Path(url)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _serialize_blocks(blocks: Any) -> list[dict]:
    """Convert Anthropic SDK response blocks (objects) into plain dicts so we
    can JSON-persist them in the ChatSession.messages list."""
    def _get(b: Any, name: str, default: Any = None) -> Any:
        # Handle both Pydantic-style attribute access AND dict shapes without
        # short-circuiting through falsy-but-valid values like {} or "".
        if isinstance(b, dict):
            return b.get(name, default)
        sentinel = object()
        v = getattr(b, name, sentinel)
        return default if v is sentinel else v

    out: list[dict] = []
    for b in blocks or []:
        b_type = _get(b, "type")
        if b_type == "text":
            out.append({"type": "text", "text": _get(b, "text", "")})
        elif b_type == "tool_use":
            out.append({
                "type": "tool_use",
                "id": _get(b, "id"),
                "name": _get(b, "name"),
                "input": _get(b, "input", {}) or {},
            })
        # Anthropic occasionally returns other block types (thinking, etc.) —
        # pass them through verbatim if dict-shaped, else skip.
        elif isinstance(b, dict):
            out.append(b)
    return out


def new_chat(title: str = "New chat") -> models.ChatSession:
    chat_id = "chat_" + secrets.token_hex(6)
    chat = models.ChatSession(chat_id=chat_id, title=title)
    store().add_chat(chat)
    return chat


async def run_turn(chat_id: str, user_message: str,
                   *, max_iterations: int = 10) -> models.ChatSession:
    """Append `user_message` and run the agent loop until Claude stops asking
    for tools. Returns the updated chat. Persists after every step so the
    frontend polling sees incremental progress."""
    chat = store().get_chat(chat_id)
    if chat is None:
        raise ValueError(f"unknown chat_id: {chat_id}")

    chat.messages.append({"role": "user", "content": user_message})
    # Auto-title from first user message.
    if chat.title == "New chat" and user_message.strip():
        chat.title = user_message.strip()[:60]
    store().update_chat(chat)

    for _iter in range(max_iterations):
        # Anthropic expects content as either a string or a list of blocks.
        # Our stored messages have content as either string (user input) or
        # a list of dicts (assistant response or tool_result batch). Pass
        # through as-is.
        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in chat.messages
        ]
        try:
            response = await asyncio.to_thread(
                anthropic_client.messages_with_tools,
                system=SYSTEM_PROMPT,
                messages=api_messages,
                tools=TOOL_DEFS,
                phase="chat",
                character="chat",
                job_id=chat.chat_id,
            )
        except Exception as e:
            # Surface as an assistant message so the UI can show it.
            chat.messages.append({
                "role": "assistant",
                "content": [{"type": "text",
                             "text": f"⚠️ Claude API error: {type(e).__name__}: {e}"}],
            })
            store().update_chat(chat)
            return chat

        blocks = _serialize_blocks(response.content)
        chat.messages.append({"role": "assistant", "content": blocks})
        store().update_chat(chat)

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason != "tool_use":
            return chat

        # Execute every tool_use block. We do them sequentially because some
        # share state (e.g. list_characters before create_swap_job); parallel
        # is a V2 optimization.
        tool_results: list[dict] = []
        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            result = await _dispatch_tool(block["name"], block.get("input") or {}, chat)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result, default=str),
            })

        if not tool_results:
            return chat  # stop_reason said tool_use but no blocks?? bail safely

        chat.messages.append({"role": "user", "content": tool_results})
        store().update_chat(chat)

    # Hit the iteration cap — surface a notice so the user can intervene.
    chat.messages.append({
        "role": "assistant",
        "content": [{"type": "text",
                     "text": f"⚠️ Stopped after {max_iterations} tool iterations to "
                             "prevent runaway loops. Ask me to continue or simplify."}],
    })
    store().update_chat(chat)
    return chat
