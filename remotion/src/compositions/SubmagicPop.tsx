import React from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMontserrat } from "@remotion/google-fonts/Montserrat";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";
import { rgba } from "../lib/colors";

// Submagic-tier weights. We load two faces so font swaps from the UI
// (Inter / Montserrat) both render with full impact-level weight.
loadInter("normal", { weights: ["900"] });
loadMontserrat("normal", { weights: ["900"] });

// Words ignored by the keyword-emphasis pass. Submagic colors RANDOM
// high-signal words with a secondary accent (typically a softer
// orange/red) to keep the eye moving — fillers get the default white.
const FILLER_WORDS = new Set([
  "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
  "to", "of", "in", "on", "at", "for", "and", "or", "but", "i", "you",
  "we", "they", "it", "this", "that", "with", "as", "from", "by", "so",
  "if", "then", "than", "do", "did", "does", "have", "has", "had", "will",
  "would", "should", "could", "can", "may", "might", "just", "very", "much",
  "more", "less", "all", "any", "some", "no", "not", "yes", "ok",
]);

// Submagic palette. The PRIMARY accent (passed as `accent` prop) drives the
// CURRENTLY-SPOKEN word so the eye locks to the karaoke read. Secondary
// emphasis colors are picked deterministically from a small palette by
// hashing the word — same word always gets the same color across renders.
const EMPHASIS_PALETTE = [
  "#FF3B30", // submagic red
  "#34C759", // submagic green
  "#FF9500", // submagic orange
  "#00C7FF", // submagic blue
  "#FF2D92", // submagic pink
];

function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h) + s.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h);
}

function pickEmphasisColor(text: string): string {
  return EMPHASIS_PALETTE[hashString(text.toLowerCase()) % EMPHASIS_PALETTE.length];
}

function isEmphasisCandidate(text: string): boolean {
  const t = text.toLowerCase().replace(/[^a-z']/g, "");
  if (t.length < 4) return false;
  if (FILLER_WORDS.has(t)) return false;
  return true;
}

export const SubmagicPop: React.FC<BaseCaptionProps> = (props) => {
  const {
    videoSrc, words, accent, fontFamily, sizeScale, positionPct,
    allCaps, wordsPerCard, videoWidth, videoHeight,
    fontWeight, opacity, shadowDistance, shadowBlur, outlinePx,
  } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  // Slightly bigger baseline than the old 6% — matches Submagic's punchy size.
  const baseFontSize = Math.round(videoHeight * 0.068 * sizeScale);

  // Pick ONE emphasis word per card (so the line isn't a rainbow). We pick
  // the longest non-filler word; that word's emphasis-color shows when it
  // becomes the active word. Other active words still flash the primary
  // accent, matching Submagic's "mostly yellow + occasional accent" cadence.
  let emphasisIdx = -1;
  if (card) {
    let bestLen = -1;
    for (let i = 0; i < card.words.length; i++) {
      if (!isEmphasisCandidate(card.words[i].text)) continue;
      const len = card.words[i].text.length;
      if (len > bestLen) {
        bestLen = len;
        emphasisIdx = i;
      }
    }
  }

  const containerStyle: React.CSSProperties = {
    position: "absolute",
    left: 0,
    right: 0,
    top: `${positionPct.y * 100}%`,
    display: "flex",
    flexDirection: "row",
    justifyContent: "center",
    alignItems: "center",
    flexWrap: "wrap",
    gap: `${baseFontSize * 0.28}px`,
    padding: `0 ${videoWidth * 0.05}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            // Per-word entrance spring — Submagic's signature is a tight
            // 160ms bounce. Words land staggered (one per frame they spoke)
            // but each is independently animated so the line feels alive.
            const entranceFrames = Math.round(fps * 0.16);
            const enter = spring({
              frame: frame - w.startFrame,
              fps,
              config: { damping: 11, stiffness: 220, mass: 0.55 },
              durationInFrames: entranceFrames,
            });
            const isActive = i === activeWordIdx;
            // Active-word color: emphasis palette when the word is BOTH the
            // chosen emphasis AND currently speaking; otherwise primary accent.
            const activeColor = (isActive && i === emphasisIdx)
              ? pickEmphasisColor(w.text)
              : accent;
            const color = isActive ? activeColor : "#FFFFFF";
            const display = allCaps ? w.text.toUpperCase().trim() : w.text.trim();
            // Active word scales up 20% to lock the eye — this was the
            // single biggest gap vs CapCut/Submagic in the prior version
            // (was just 5%). Inactive words stay at scale 1.0 once entered.
            const activeBoost = isActive ? 0.2 : 0;
            const scale = 0.55 + enter * 0.45 + activeBoost;
            // Outline: user-tunable via `outlinePx` prop. When unset (0),
            // we still ship a sensible default (~5% of font size) so the
            // template doesn't go unreadable on busy backgrounds.
            const effectiveOutline = outlinePx > 0
              ? outlinePx
              : Math.max(3, Math.round(baseFontSize * 0.055));
            // Shadow comes from props if non-zero, else falls back to
            // Submagic's signature drop-shadow recipe (8% offset, 20% blur).
            const shadowOffset = shadowDistance > 0
              ? shadowDistance
              : Math.round(baseFontSize * 0.08);
            const shadowSpread = shadowBlur > 0
              ? shadowBlur
              : Math.round(baseFontSize * 0.2);
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, "Montserrat", "Inter", system-ui, sans-serif`,
              fontWeight,
              fontStyle: "italic",
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.0,
              color,
              textShadow: `0 ${shadowOffset}px ${shadowSpread}px ${rgba("#000000", 0.6)}`,
              WebkitTextStroke: `${effectiveOutline}px #000000`,
              paintOrder: "stroke fill" as React.CSSProperties["paintOrder"],
              transform: `scale(${scale}) translateY(${(1 - enter) * baseFontSize * 0.3}px)`,
              opacity: enter * opacity,
              display: "inline-block",
              letterSpacing: "-0.015em",
              transformOrigin: "center center",
              transition: "color 60ms linear",
              willChange: "transform, opacity, color",
            };
            return (
              <span key={`${w.startFrame}-${i}`} style={wordStyle}>
                {display}
              </span>
            );
          })}
        </div>
      )}
    </AbsoluteFill>
  );
};
