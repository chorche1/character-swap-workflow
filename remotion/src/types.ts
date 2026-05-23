export type Word = {
  text: string;
  start: number;
  end: number;
};

export type BaseCaptionProps = {
  videoSrc: string;
  words: Word[];
  accent: string;
  fontFamily: string;
  sizeScale: number;
  positionPct: { x: number; y: number };
  allCaps: boolean;
  wordsPerCard: number;
  videoDurationSecs: number;
  videoWidth: number;
  videoHeight: number;
  // User-tunable visual params (Hugo, May 2026). Each Remotion composition
  // reads these and applies them so the user can dial in per-render from
  // the Style tab without rebuilding the React composition. Defaults below
  // keep prior compositions visually identical.
  fontWeight: number;       // 100-900
  opacity: number;          // 0.0-1.0 (text alpha)
  shadowDistance: number;   // text-shadow offset px
  shadowBlur: number;       // text-shadow blur radius px
  outlinePx: number;        // text stroke width px (0 = no outline)
};

export const DEFAULT_CAPTION_PROPS: BaseCaptionProps = {
  videoSrc: "",
  words: [],
  accent: "#FFD400",
  fontFamily: "Inter",
  sizeScale: 1.0,
  positionPct: { x: 0.5, y: 0.78 },
  allCaps: true,
  wordsPerCard: 3,
  videoDurationSecs: 10,
  videoWidth: 1080,
  videoHeight: 1920,
  fontWeight: 900,
  opacity: 1.0,
  shadowDistance: 0,
  shadowBlur: 0,
  outlinePx: 0,
};
