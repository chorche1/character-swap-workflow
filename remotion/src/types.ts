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
};
