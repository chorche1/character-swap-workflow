import React from "react";
import { AbsoluteFill, OffthreadVideo, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { loadFont } from "@remotion/google-fonts/Inter";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";
import { rgba } from "../lib/colors";

loadFont("normal", { weights: ["900"] });

export const SubmagicPop: React.FC<BaseCaptionProps> = (props) => {
  const { videoSrc, words, accent, fontFamily, sizeScale, positionPct, allCaps, wordsPerCard, videoWidth, videoHeight } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  const baseFontSize = Math.round(videoHeight * 0.06 * sizeScale);
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
    gap: `${baseFontSize * 0.25}px`,
    padding: `0 ${videoWidth * 0.05}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const entranceFrames = Math.round(fps * 0.18);
            const enter = spring({
              frame: frame - w.startFrame,
              fps,
              config: { damping: 12, stiffness: 180, mass: 0.6 },
              durationInFrames: entranceFrames,
            });
            const isActive = i === activeWordIdx;
            const display = allCaps ? w.text.toUpperCase().trim() : w.text.trim();
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, system-ui, sans-serif`,
              fontWeight: 900,
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.0,
              color: isActive ? accent : "#FFFFFF",
              textShadow: `0 ${Math.round(baseFontSize * 0.06)}px ${Math.round(baseFontSize * 0.18)}px ${rgba("#000000", 0.55)}`,
              WebkitTextStroke: `${Math.max(2, Math.round(baseFontSize * 0.045))}px #000000`,
              paintOrder: "stroke fill" as React.CSSProperties["paintOrder"],
              transform: `scale(${0.6 + enter * 0.4 + (isActive ? 0.05 : 0)}) translateY(${(1 - enter) * baseFontSize * 0.35}px)`,
              opacity: enter,
              display: "inline-block",
              letterSpacing: "-0.01em",
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
