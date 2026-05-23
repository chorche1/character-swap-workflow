import React from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadFont as loadMontserrat } from "@remotion/google-fonts/Montserrat";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadAnton } from "@remotion/google-fonts/Anton";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";
import { rgba } from "../lib/colors";

// "SubmagicPro" — Hugo's premium default. Combines the best traits of
// Submagic + CapCut + MrBeast into ONE composition:
//   - Submagic: per-word entrance spring + random-keyword color emphasis
//   - CapCut:   accent glow halo + thick outline for legibility
//   - MrBeast:  italic ALLCAPS + active-word scale boost for karaoke read
//
// Designed to be the recommended template — when in doubt, pick this one.

loadMontserrat("normal", { weights: ["800", "900"] });
loadInter("normal", { weights: ["900"] });
loadAnton();

const FILLER_WORDS = new Set([
  "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
  "to", "of", "in", "on", "at", "for", "and", "or", "but", "i", "you",
  "we", "they", "it", "this", "that", "with", "as", "from", "by", "so",
  "if", "then", "than", "do", "did", "does", "have", "has", "had", "will",
  "would", "should", "could", "can", "may", "might", "just", "very", "much",
  "more", "less", "all", "any", "some", "no", "not", "yes", "ok",
]);

// Hand-picked palette. Submagic uses similar saturated mid-tones that read
// at thumbnail size and pop against most footage.
const EMPHASIS_PALETTE = [
  "#FF3B30",  // red
  "#34C759",  // green
  "#FF9500",  // orange
  "#00C7FF",  // blue
  "#FF2D92",  // pink
  "#AF52DE",  // purple
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

export const SubmagicPro: React.FC<BaseCaptionProps> = (props) => {
  const {
    videoSrc, words, accent, fontFamily, sizeScale, positionPct,
    allCaps, wordsPerCard, videoWidth, videoHeight,
    fontWeight, opacity, shadowDistance, shadowBlur,
    outlinePx: propOutlinePx,
  } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  // Submagic-tier baseline size — 7% of frame height matches their
  // reels-default scale on 1080×1920.
  const baseFontSize = Math.round(videoHeight * 0.07 * sizeScale);

  // Per-card: pick the longest non-filler word as the "hero" word. Its
  // color is deterministic (hash) so the same word always emphasizes the
  // same color across renders.
  let heroIdx = -1;
  let heroColor = accent;
  if (card) {
    let bestLen = -1;
    for (let i = 0; i < card.words.length; i++) {
      if (!isEmphasisCandidate(card.words[i].text)) continue;
      const len = card.words[i].text.length;
      if (len > bestLen) {
        bestLen = len;
        heroIdx = i;
      }
    }
    if (heroIdx !== -1) {
      heroColor = pickEmphasisColor(card.words[heroIdx].text);
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
    gap: `${baseFontSize * 0.26}px`,
    padding: `0 ${videoWidth * 0.05}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            // Per-word spring entrance — tight 160ms bounce keyed off the
            // word's own start frame so words land in sync with speech.
            const entranceFrames = Math.round(fps * 0.16);
            const enter = spring({
              frame: frame - w.startFrame,
              fps,
              config: { damping: 11, stiffness: 240, mass: 0.5 },
              durationInFrames: entranceFrames,
            });
            const isActive = i === activeWordIdx;
            const isHero = i === heroIdx;

            // Color logic:
            //   - hero word, while speaking → palette emphasis color
            //   - any other active word → primary accent
            //   - inactive → white
            // This produces Submagic's characteristic "mostly yellow, with
            // an occasional colored word" pattern.
            let color = "#FFFFFF";
            if (isActive) {
              color = isHero ? heroColor : accent;
            }

            const display = allCaps ? w.text.toUpperCase().trim() : w.text.trim();
            // 22% active-word scale-up — biggest of all templates because
            // Pro is meant to feel the most "karaoke alive".
            const activeBoost = isActive ? 0.22 : 0;
            const scale = 0.55 + enter * 0.45 + activeBoost;

            // Outline: user-tunable via prop; default to 5.5% of font size.
            const outline = propOutlinePx > 0
              ? propOutlinePx
              : Math.max(4, Math.round(baseFontSize * 0.055));

            // Shadow: user-tunable distance + blur; defaults preserve Pro's
            // 7% offset / 18% blur look when the user hasn't touched the sliders.
            const shadowOffset = shadowDistance > 0
              ? shadowDistance
              : Math.round(baseFontSize * 0.07);
            const shadowSpread = shadowBlur > 0
              ? shadowBlur
              : Math.round(baseFontSize * 0.18);

            // Glow: subtle accent-colored halo around the active word only;
            // inactive words get a quieter neutral shadow. This is what
            // separates "pro" from the plain SubmagicPop.
            const activeGlow = isActive
              ? `, 0 0 ${Math.round(baseFontSize * 0.35)}px ${rgba(color, 0.55)}`
              : "";

            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, "Montserrat", "Anton", "Inter", system-ui, sans-serif`,
              fontWeight,
              fontStyle: "italic",
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.0,
              color,
              textShadow: [
                `0 ${shadowOffset}px ${shadowSpread}px ${rgba("#000000", 0.65)}`,
                activeGlow,
              ].filter(Boolean).join(""),
              WebkitTextStroke: outline > 0 ? `${outline}px #000000` : undefined,
              paintOrder: outline > 0 ? ("stroke fill" as React.CSSProperties["paintOrder"]) : undefined,
              transform: `scale(${scale}) translateY(${(1 - enter) * baseFontSize * 0.28}px)`,
              opacity: enter * opacity,
              display: "inline-block",
              letterSpacing: "-0.018em",
              transformOrigin: "center center",
              transition: "color 50ms linear, text-shadow 100ms linear",
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
