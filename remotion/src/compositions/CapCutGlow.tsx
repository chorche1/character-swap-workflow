import React from "react";
import { AbsoluteFill, OffthreadVideo, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { loadFont } from "@remotion/google-fonts/Poppins";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";
import { rgba } from "../lib/colors";

loadFont("normal", { weights: ["800"] });

export const CapCutGlow: React.FC<BaseCaptionProps> = (props) => {
  const { videoSrc, words, accent, fontFamily, sizeScale, positionPct, allCaps, wordsPerCard, videoWidth, videoHeight } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  const baseFontSize = Math.round(videoHeight * 0.05 * sizeScale);
  const cardEntrance = card
    ? spring({
        frame: frame - card.startFrame,
        fps,
        config: { damping: 14, stiffness: 140, mass: 0.7 },
        durationInFrames: Math.round(fps * 0.22),
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
    gap: `${baseFontSize * 0.3}px`,
    padding: `0 ${videoWidth * 0.05}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px) translateY(${(1 - cardEntrance) * baseFontSize * 0.25}px)`,
    opacity: cardEntrance,
    filter: `drop-shadow(0 0 ${Math.round(baseFontSize * 0.25)}px ${rgba(accent, 0.55)})`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const isActive = i === activeWordIdx;
            const display = allCaps ? w.text.toUpperCase().trim() : w.text.trim();
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, system-ui, sans-serif`,
              fontWeight: 800,
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.1,
              color: isActive ? accent : "#FFFFFF",
              textShadow: `0 0 ${Math.round(baseFontSize * 0.3)}px ${rgba(accent, 0.85)}, 0 0 ${Math.round(baseFontSize * 0.6)}px ${rgba(accent, 0.4)}, 0 ${Math.round(baseFontSize * 0.06)}px ${Math.round(baseFontSize * 0.12)}px ${rgba("#000000", 0.5)}`,
              letterSpacing: "0.01em",
              display: "inline-block",
              transform: isActive ? "scale(1.04)" : "scale(1)",
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
