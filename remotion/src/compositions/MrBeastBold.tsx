import React from "react";
import { AbsoluteFill, OffthreadVideo } from "remotion";
import { loadFont } from "@remotion/google-fonts/Anton";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";
import { rgba } from "../lib/colors";

loadFont();

const FILLER_WORDS = new Set([
  "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "in", "on",
  "at", "for", "and", "or", "but", "i", "you", "we", "they", "it", "this",
  "that", "with", "as", "from", "by", "so", "if", "then", "than",
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
  const { videoSrc, words, accent, fontFamily, sizeScale, positionPct, allCaps, wordsPerCard, videoWidth, videoHeight } = props;
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  const baseFontSize = Math.round(videoHeight * 0.07 * sizeScale);
  const accentFontSize = Math.round(baseFontSize * 1.08);
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
    gap: `${baseFontSize * 0.2}px`,
    padding: `0 ${videoWidth * 0.05}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const isAccent = i === keywordIdx;
            const isActiveButNotAccent = i === activeWordIdx && !isAccent;
            const display = allCaps ? w.text.toUpperCase().trim() : w.text.trim();
            const size = isAccent ? accentFontSize : baseFontSize;
            const color = isAccent ? accent : "#FFFFFF";
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, Impact, system-ui, sans-serif`,
              fontWeight: 900,
              fontSize: `${size}px`,
              lineHeight: 1.0,
              color,
              textShadow: `0 ${Math.round(size * 0.05)}px 0 ${rgba("#000000", 0.9)}, 0 ${Math.round(size * 0.1)}px ${Math.round(size * 0.18)}px ${rgba("#000000", 0.5)}`,
              WebkitTextStroke: `${Math.max(3, Math.round(size * 0.06))}px #000000`,
              paintOrder: "stroke fill" as React.CSSProperties["paintOrder"],
              letterSpacing: "0.01em",
              opacity: isActiveButNotAccent ? 1 : 0.96,
              display: "inline-block",
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
