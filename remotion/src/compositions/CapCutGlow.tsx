import React from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadFont as loadPoppins } from "@remotion/google-fonts/Poppins";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";
import { rgba } from "../lib/colors";

// Load multiple bold-family weights so the UI font picker can swap freely
// while still rendering at impact-grade weight.
loadPoppins("normal", { weights: ["800", "900"] });
loadInter("normal", { weights: ["800", "900"] });

export const CapCutGlow: React.FC<BaseCaptionProps> = (props) => {
  const {
    videoSrc, words, accent, fontFamily, sizeScale, positionPct,
    allCaps, wordsPerCard, videoWidth, videoHeight,
  } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  const baseFontSize = Math.round(videoHeight * 0.058 * sizeScale);
  // Card-level entrance keeps the line cohesive on first show; individual
  // word springs handle the per-word read.
  const cardEntrance = card
    ? spring({
        frame: frame - card.startFrame,
        fps,
        config: { damping: 14, stiffness: 160, mass: 0.6 },
        durationInFrames: Math.round(fps * 0.2),
      })
    : 0;

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
    gap: `${baseFontSize * 0.28}px`,
    padding: `0 ${videoWidth * 0.05}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
    opacity: cardEntrance,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const isActive = i === activeWordIdx;
            // Per-word entrance — staggered with the speaker so each word
            // bounces in as it's read, then settles.
            const entranceFrames = Math.round(fps * 0.18);
            const wordEnter = spring({
              frame: frame - w.startFrame,
              fps,
              config: { damping: 13, stiffness: 200, mass: 0.55 },
              durationInFrames: entranceFrames,
            });
            const display = allCaps ? w.text.toUpperCase().trim() : w.text.trim();
            // Outline keeps text legible on any background; was missing
            // before (glow-only). 5% of font size, min 3px. Matches CapCut's
            // signature stroke-on-glow look.
            const outlinePx = Math.max(3, Math.round(baseFontSize * 0.05));
            // Active word scale boost up to 18% so the karaoke read is
            // unmistakable. Previously stuck at 4%.
            const activeBoost = isActive ? 0.18 : 0;
            const scale = 0.65 + wordEnter * 0.35 + activeBoost;
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, "Poppins", "Inter", system-ui, sans-serif`,
              fontWeight: 900,
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.05,
              color: isActive ? accent : "#FFFFFF",
              // Outline (stroke) + cyan glow + soft drop shadow. The triple
              // layering is the CapCut signature: legible on busy footage
              // AND visually distinctive.
              WebkitTextStroke: `${outlinePx}px #000000`,
              paintOrder: "stroke fill" as React.CSSProperties["paintOrder"],
              textShadow: [
                `0 0 ${Math.round(baseFontSize * 0.28)}px ${rgba(accent, 0.85)}`,
                `0 0 ${Math.round(baseFontSize * 0.55)}px ${rgba(accent, 0.45)}`,
                `0 ${Math.round(baseFontSize * 0.06)}px ${Math.round(baseFontSize * 0.14)}px ${rgba("#000000", 0.55)}`,
              ].join(", "),
              letterSpacing: "0.005em",
              display: "inline-block",
              transform: `scale(${scale}) translateY(${(1 - wordEnter) * baseFontSize * 0.25}px)`,
              transformOrigin: "center center",
              opacity: wordEnter,
              transition: "color 80ms linear",
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
