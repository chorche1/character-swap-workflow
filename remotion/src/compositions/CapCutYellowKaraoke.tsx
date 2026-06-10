import React from "react";
import { AbsoluteFill, OffthreadVideo } from "remotion";
import { loadFont as loadPoppins } from "@remotion/google-fonts/Poppins";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";

// CapCut "yellow karaoke" template — exact replica of Hugo's reference
// "silas ears 11.mov" (frame-by-frame decoded June 2026):
//   - Font: Poppins Black (900) ALLCAPS — matched against the reference
//     crops letter-by-letter (round O, barred G, geometric forms).
//   - Base text: near-white #F8F8F8 with a THICK black outline (~9.5% of
//     font size) plus a soft drop shadow.
//   - The CURRENTLY SPOKEN word switches its fill to yellow (#F8F800
//     sampled from the reference) — an INSTANT color swap, no scale pop,
//     no pill, no fade. The yellow hops word-to-word karaoke style.
//   - Cards appear/disappear HARD: no entrance animation of any kind
//     (verified across three card transitions in the reference video).
//   - 1-2 centered lines, mid-screen position (~52% down).
loadPoppins("normal", { weights: ["900"] });

export const CapCutYellowKaraoke: React.FC<BaseCaptionProps> = (props) => {
  const {
    videoSrc, words, accent, fontFamily, sizeScale, positionPct,
    wordsPerCard, videoWidth, videoHeight,
    fontWeight, opacity, shadowDistance, shadowBlur, outlinePx, outlineColor,
  } = props;
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  // Reference: cap-height ≈ 60px on a 1896-tall frame → ~85px font at 1920.
  const baseFontSize = Math.round(videoHeight * 0.06 * sizeScale);

  // Outline: the reference has a notably thick stroke. User override via
  // outlinePx; default ~9.5% of font size.
  const effectiveOutline = outlinePx > 0
    ? outlinePx
    : Math.max(4, Math.round(baseFontSize * 0.095));
  const shadowOffset = shadowDistance > 0
    ? shadowDistance
    : Math.round(baseFontSize * 0.05);
  const shadowSpread = shadowBlur > 0
    ? shadowBlur
    : Math.round(baseFontSize * 0.16);

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
    padding: `0 ${videoWidth * 0.07}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const isActive = i === activeWordIdx;
            // ALLCAPS is the template's identity (reference is 100% caps).
            const display = w.text.toUpperCase().trim();
            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, "Poppins", system-ui, sans-serif`,
              fontWeight,
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.06,
              // INSTANT karaoke swap — no transition property on purpose:
              // the reference switches color in a single frame.
              color: isActive ? accent : "#F8F8F8",
              opacity,
              letterSpacing: "0em",
              WebkitTextStroke: `${effectiveOutline}px ${outlineColor}`,
              paintOrder: "stroke fill" as React.CSSProperties["paintOrder"],
              textShadow: `0 ${shadowOffset}px ${shadowSpread}px rgba(0,0,0,0.55)`,
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
