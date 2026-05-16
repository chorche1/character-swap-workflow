# Remotion compositions — character-swap captions

React + Remotion project that renders the modern animated caption
templates exposed in the FastAPI Editor tab:

- `SubmagicPop` — word-by-word spring entrance, active word in accent.
- `MrBeastBold` — ALLCAPS, single keyword popped, no entry animation.
- `CapCutGlow` — phrase-by-phrase entrance with multi-layer glow.

The Python side calls `npx remotion render <id> <out.mp4> --props=<json>`
from `src/character_swap/remotion_render.py`. The same compositions also
power the in-browser preview via `@remotion/player` (built into a single
bundle at `../web/static/remotion-preview.js` by `build-preview.mjs`).

## Setup

```bash
# from the repo root
uv run character-swap remotion-install
```

That runs `npm install` here and bundles the preview. Run it again with
`--force` after editing files under `src/preview/` or bumping a
dependency.

## Scripts

```
npm run studio          # open Remotion's design Studio for live tweaking
npm run render -- ID OUT.mp4 --props=props.json    # one-off render
npm run build-preview   # rebuild web/static/remotion-preview.js (esbuild)
```

## Files

```
src/
├── index.ts                  # registerRoot(Root)
├── Root.tsx                  # registers the 3 compositions
├── types.ts                  # Word, BaseCaptionProps, DEFAULT_CAPTION_PROPS
├── lib/
│   ├── useCurrentWord.ts     # frame → active word index helper
│   └── colors.ts             # hex → rgba helper
├── compositions/
│   ├── SubmagicPop.tsx
│   ├── MrBeastBold.tsx
│   └── CapCutGlow.tsx
└── preview/
    └── index.tsx             # mount()/update() for the in-page Player
```

Compositions are 9:16 (1080×1920) at 30 fps. Duration is computed from
`videoDurationSecs` in props via `calculateMetadata`.
