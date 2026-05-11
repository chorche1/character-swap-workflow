# character-swap

Autonomous pipeline that takes one scene image and 5 character reference images and produces 5 character-swapped scene videos.

Phase 1 — Claude Opus 4.7 extracts a Master Scene Prompt from the input image (background, lighting, camera, style — character excluded).
Phase 2 — Claude Opus 4.7 builds a detailed Character Token for each of the 5 reference images.
Phase 3 — GPT Image 2 generates a swap per character; Claude Opus 4.7 QCs each result against scene + reference and auto-corrects up to 3 times. On 3-retry failure, the pipeline pauses for human review.
Phase 4 — Once all 5 are approved, the user supplies a single Movement Prompt; Grok Imagine animates each approved image into an `.mp4`.

## Quickstart

```bash
uv sync
cp .env.example .env  # then fill in keys

# drop your scene image into input/
# drop 5 character images into characters/

uv run character-swap dry-run   # see the call plan + estimated cost
uv run character-swap run       # full pipeline; will pause for Movement Prompt
```

Subcommands: `run`, `scene`, `index`, `generate`, `video`, `status`, `dry-run`.

Outputs land in `output/<character_name>/{generated.png,final.mp4}`. State lives in `state/state.json`. Every API call is logged as one JSONL line in `state/calls.jsonl`.

The run is resumable — re-running picks up wherever it left off (cache keyed by `sha256` of inputs + prompt template version).
