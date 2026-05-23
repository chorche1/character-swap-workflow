import React from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadFont as loadAnton } from "@remotion/google-fonts/Anton";
import { loadFont as loadBebas } from "@remotion/google-fonts/BebasNeue";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";
import { rgba } from "../lib/colors";

// Both Anton and Bebas Neue come default-bold; load both so the UI's
// font swap doesn't fall back to a sans serif.
loadAnton();
loadBebas();

// Words to skip when picking the per-card emphasis keyword.
const FILLER_WORDS = new Set([
  "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
  "to", "of", "in", "on", "at", "for", "and", "or", "but", "i", "you",
  "we", "they", "it", "this", "that", "with", "as", "from", "by", "so",
  "if", "then", "than", "do", "did", "does", "have", "has", "had", "will",
  "would", "should", "could", "can", "may", "might", "just",
]);

function pickKeyword(words: { text: string }[]): number {
  let bestIdx = -1;
  let bestLen = -1;
  for (let i = 0; i < words.length; i++) {
    const t = words[i].text.toLowerCase().replace(/[^a-z']/g, "");
    if (FILLER_WORDS.has(t)) continue;
    if (t.length > bestLen) {
      bestLen = t.length;
      bestIdx = i;
    }
  }
  return bestIdx;
}

export const MrBeastBold: React.FC<BaseCaptionProps> = (props) => {
  const {
    videoSrc, words, accent, fontFamily, sizeScale, positionPct,
    allCaps, wordsPerCard, videoWidth, videoHeight,
    fontWeight, opacity, shadowDistance, shadowBlur, outlineColor,
    outlinePx: propOutlinePx,
  } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  const baseFontSize = Math.round(videoHeight * 0.075 * sizeScale);
  // Keyword gets a real size jump (28% bigger). Previously 8% which barely
  // registered as emphasis. MrBeast titles use HUGE keywords for impact.
  const accentFontSize = Math.round(baseFontSize * 1.28);
  const keywordIdx = card ? pickKeyword(card.words) : -1;

  const containerStyle: React.CSSProperties = {
    position: "absolute",
    left: 0,
    right: 0,
    top: `${positionPct.y * 100}%`,
    display: "flex",
    flexDirection: "row",
    justifyContent: "center",
    alignItems: "baseline",
    flexWrap: "wrap",
    gap: `${baseFontSize * 0.18}px`,
    padding: `0 ${videoWidth * 0.04}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const isAccent = i === keywordIdx;
            const isActive = i === activeWordIdx;
            const display = allCaps ? w.text.toUpperCase().trim() : w.text.trim();
            const size = isAccent ? accentFontSize : baseFontSize;
            // Color hierarchy: keyword always accent (yellow), current word
            // accent IF it's also the keyword, otherwise plain white. Inactive
            // non-keyword words sit at 95% opacity to push the focus.
            const color = isAccent ? accent : "#FFFFFF";

            // Per-word entrance — was MISSING entirely before. Subtle pop
            // (180ms, mild damping) since MrBeast captions favor punch over
            // animation noise. The keyword pops a touch harder.
            const entranceFrames = Math.round(fps * 0.18);
            const enter = spring({
              frame: frame - w.startFrame,
              fps,
              config: isAccent
                ? { damping: 9, stiffness: 240, mass: 0.5 }   // keyword: snappier
                : { damping: 14, stiffness: 180, mass: 0.6 }, // normal: gentle
              durationInFrames: entranceFrames,
            });
            // Active word gets an extra subtle scale so karaoke read is
            // still trackable even though the dominant emphasis is the
            // pre-picked keyword.
            const activeBoost = isActive && !isAccent ? 0.06 : 0;
            const scale = 0.7 + enter * 0.3 + activeBoost;

            // Outline scaling — user-tunable; default 6% of size for the
            // bigger keyword so stroke stays proportional.
            const outline = propOutlinePx > 0
              ? propOutlinePx
              : Math.max(3, Math.round(size * 0.06));
            // Shadow: user-tunable distance + blur. Defaults preserve
            // MrBeast's double-layered drop look (5% offset + 10/18% blur).
            const shadowOffset = shadowDistance > 0
              ? shadowDistance
              : Math.round(size * 0.05);
            const shadowSpread = shadowBlur > 0
              ? shadowBlur
              : Math.round(size * 0.18);
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, "Anton", "Bebas Neue", "Impact", system-ui, sans-serif`,
              fontWeight,
              fontSize: `${size}px`,
              lineHeight: 1.0,
              color,
              textShadow: [
                `0 ${shadowOffset}px 0 ${rgba("#000000", 0.95)}`,
                `0 ${Math.round(shadowOffset * 2)}px ${shadowSpread}px ${rgba("#000000", 0.55)}`,
              ].join(", "),
              WebkitTextStroke: outline > 0 ? `${outline}px ${outlineColor}` : undefined,
              paintOrder: outline > 0 ? ("stroke fill" as React.CSSProperties["paintOrder"]) : undefined,
              letterSpacing: "0.005em",
              opacity: enter * (isActive || isAccent ? 1 : 0.96) * opacity,
              display: "inline-block",
              transform: `scale(${scale}) translateY(${(1 - enter) * size * 0.25}px)`,
              transformOrigin: "center center",
              willChange: "transform, opacity",
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
