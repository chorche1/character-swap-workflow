import React from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadFont as loadPoppins } from "@remotion/google-fonts/Poppins";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";

// CapCut "blue box" template — exact replica of Hugo's reference
// "Silas ears 10.mov" (frame-by-frame decoded June 2026):
//   - Font: Poppins Black (900) ALLCAPS, white fill, with a thinner black
//     outline than the yellow-karaoke sibling (~5.5% of font size) plus a
//     soft drop shadow.
//   - The CURRENTLY SPOKEN word gets a vivid blue (#0070F8 sampled from the
//     reference) ROUNDED RECTANGLE behind it; the text stays white. The box
//     hops word-to-word essentially instantly (verified: one frame between
//     "MINUTE" and "AND" carrying the box).
//   - Cards appear/disappear HARD: no entrance animation.
//   - 1-2 centered lines, mid-screen position (~52% down).
//
// The box is painted via padding + equal NEGATIVE margins, so it consumes no
// layout space: word positions are identical to plain text, and the box
// overlaps into the natural word gaps — exactly like the reference, where
// the line never reflows as the box hops.
loadPoppins("normal", { weights: ["900"] });

export const CapCutBlueBox: React.FC<BaseCaptionProps> = (props) => {
  const {
    videoSrc, words, accent, fontFamily, sizeScale, positionPct,
    wordsPerCard, videoWidth, videoHeight,
    fontWeight, opacity, shadowDistance, shadowBlur, outlinePx, outlineColor,
  } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  const baseFontSize = Math.round(videoHeight * 0.06 * sizeScale);

  const effectiveOutline = outlinePx > 0
    ? outlinePx
    : Math.max(3, Math.round(baseFontSize * 0.055));
  const shadowOffset = shadowDistance > 0
    ? shadowDistance
    : Math.round(baseFontSize * 0.05);
  const shadowSpread = shadowBlur > 0
    ? shadowBlur
    : Math.round(baseFontSize * 0.16);

  // Box geometry, em-relative (measured against the reference: generous
  // horizontal padding, fairly round corners).
  const boxPadX = baseFontSize * 0.24;
  const boxPadY = baseFontSize * 0.10;
  const boxRadius = baseFontSize * 0.22;

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
    gap: `${baseFontSize * 0.30}px`,
    rowGap: `${baseFontSize * 0.16}px`,
    padding: `0 ${videoWidth * 0.05}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const isActive = i === activeWordIdx;
            const display = w.text.toUpperCase().trim();
            // Near-instant box pop: full opacity within ~1 frame of the word
            // becoming active (the reference box lands in a single frame).
            const popFrames = Math.max(1, Math.round(fps * 0.03));
            const sinceActive = frame - w.startFrame;
            const popProgress = isActive
              ? Math.min(1, Math.max(0, sinceActive / popFrames))
              : 0;
            const boxBg = isActive
              ? `${accent}${Math.round(popProgress * 255)
                    .toString(16).padStart(2, "0").toUpperCase()}`
              : "transparent";
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, "Poppins", system-ui, sans-serif`,
              fontWeight,
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.06,
              color: "#F8F8F8",
              opacity,
              letterSpacing: "0em",
              // Box paints around the word without consuming layout space.
              padding: `${boxPadY}px ${boxPadX}px`,
              margin: `${-boxPadY}px ${-boxPadX}px`,
              borderRadius: `${boxRadius}px`,
              backgroundColor: boxBg,
              WebkitTextStroke: `${effectiveOutline}px ${outlineColor}`,
              paintOrder: "stroke fill" as React.CSSProperties["paintOrder"],
              textShadow: `0 ${shadowOffset}px ${shadowSpread}px rgba(0,0,0,0.5)`,
              display: "inline-block",
              whiteSpace: "nowrap",
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
