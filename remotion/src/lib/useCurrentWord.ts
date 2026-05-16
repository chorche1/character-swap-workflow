import { useCurrentFrame, useVideoConfig } from "remotion";
import type { Word } from "../types";

export type Card = {
  startFrame: number;
  endFrame: number;
  words: Array<Word & { startFrame: number; endFrame: number }>;
};

export function groupIntoCards(words: Word[], perCard: number, fps: number): Card[] {
  const cards: Card[] = [];
  for (let i = 0; i < words.length; i += perCard) {
    const chunk = words.slice(i, i + perCard);
    if (chunk.length === 0) continue;
    const enriched = chunk.map((w) => ({
      ...w,
      startFrame: Math.max(0, Math.round(w.start * fps)),
      endFrame: Math.max(1, Math.round(w.end * fps)),
    }));
    cards.push({
      startFrame: enriched[0].startFrame,
      endFrame: enriched[enriched.length - 1].endFrame,
      words: enriched,
    });
  }
  return cards;
}

export function useActiveCard(words: Word[], perCard: number): {
  card: Card | null;
  activeWordIdx: number;
  frame: number;
} {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const cards = groupIntoCards(words, perCard, fps);
  const card = cards.find((c) => frame >= c.startFrame && frame <= c.endFrame) ?? null;
  let activeWordIdx = -1;
  if (card) {
    for (let i = 0; i < card.words.length; i++) {
      const w = card.words[i];
      if (frame >= w.startFrame && frame <= w.endFrame) {
        activeWordIdx = i;
        break;
      }
    }
    if (activeWordIdx === -1) {
      const earlier = card.words.findIndex((w) => frame < w.startFrame);
      activeWordIdx = earlier === -1 ? card.words.length - 1 : Math.max(0, earlier - 1);
    }
  }
  return { card, activeWordIdx, frame };
}
