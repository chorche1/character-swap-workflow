import React from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadFont as loadMontserrat } from "@remotion/google-fonts/Montserrat";
import type { BaseCaptionProps } from "../types";
import { useActiveCard } from "../lib/useCurrentWord";

// CapCut "purple pill" template — exact match for the look Hugo brought as
// a reference: white ALLCAPS Montserrat Black text where the CURRENTLY
// SPOKEN word gets a vibrant violet pill background, and inactive words
// are plain white text on whatever the underlying video is.
//
// Why this exists as its own composition (rather than a VEED-API render):
// fal.ai's VEED Subtitle Styling endpoint applies `background_color` to the
// entire caption card, not per-active-word. The pill-follows-spoken-word
// behavior is the defining visual of this template and is impossible to
// reproduce via that API without modifications. We render it locally via
// Remotion + ffmpeg instead.
//
// Key visual choices (eyeballed from Hugo's 33.mov reference frames):
//   - Font: Montserrat 900 (Black), ALLCAPS, slight letter-spacing tightening
//   - Color: pure white text, no outline/stroke
//   - Active-word pill: #8B5CF6 (Tailwind violet-500), ~10px corner radius,
//     padded ~0.18em horizontally + ~0.08em vertically around the word
//   - No entrance animation on the words — they appear/disappear hard with
//     the card. The pill is the only animated element and it pops in with
//     a 60ms ease, which matches the snappy CapCut feel.
//   - Position: middle-screen by default (positionPct.y = 0.55), slightly
//     below center so it doesn't compete with faces in talking-head shots.
loadMontserrat("normal", { weights: ["900"] });

export const CapCutPurplePill: React.FC<BaseCaptionProps> = (props) => {
  const {
    videoSrc, words, accent, fontFamily, sizeScale, positionPct,
    wordsPerCard, videoWidth, videoHeight,
    fontWeight, opacity, shadowDistance, shadowBlur, outlinePx,
  } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { card, activeWordIdx } = useActiveCard(words, wordsPerCard);

  // ~6% of video height looks right at 1080×1920 — matches the reference
  // (where caption height is ~115px on a 1920-tall canvas).
  const baseFontSize = Math.round(videoHeight * 0.06 * sizeScale);

  // Compose text-shadow from user-tunable distance + blur. Skip entirely
  // when both are zero so we don't ship an empty " 0px 0px 0 rgba(...)".
  const textShadowCss = (shadowDistance > 0 || shadowBlur > 0)
    ? `${shadowDistance}px ${shadowDistance}px ${shadowBlur}px rgba(0,0,0,0.55)`
    : "none";

  // Pill geometry. Em-relative so it scales with font.
  const pillPadX = baseFontSize * 0.18;
  const pillPadY = baseFontSize * 0.08;
  const pillRadius = baseFontSize * 0.12;

  // Container: middle-row layout with wrap. We mirror the SubmagicPop
  // pattern (flex/wrap row + translateY(-50%) to vertically center on
  // positionPct.y) so the position semantics match other Remotion templates.
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
    gap: `${baseFontSize * 0.35}px`,
    rowGap: `${baseFontSize * 0.2}px`,
    padding: `0 ${videoWidth * 0.06}px`,
    transform: `translateY(-50%) translateX(${(positionPct.x - 0.5) * videoWidth}px)`,
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}
      {card && (
        <div style={containerStyle}>
          {card.words.map((w, i) => {
            const isActive = i === activeWordIdx;
            // Force ALLCAPS regardless of allCaps prop — this template is
            // defined by its ALLCAPS look; lowercase breaks the aesthetic.
            const display = w.text.toUpperCase().trim();

            // The pill "pops" in with a quick ease when the word becomes
            // active. Implemented as a CSS transition on background-color
            // would be smoother but Remotion renders frame-by-frame, so we
            // compute it explicitly.
            const popFrames = Math.max(1, Math.round(fps * 0.06));
            const sinceActive = frame - w.startFrame;
            const popProgress = isActive
              ? Math.min(1, Math.max(0, sinceActive / popFrames))
              : 0;
            // Pill background — accent (default violet) when active, fully
            // transparent otherwise. The 0.95 max-opacity gives the pill a
            // tiny bit of see-through to mimic CapCut's render.
            const pillBg = isActive
              ? `${accent}${Math.round(popProgress * 0.95 * 255)
                    .toString(16).padStart(2, "0")
                    .toUpperCase()}`
              : "transparent";

            const wordStyle: React.CSSProperties = {
              fontFamily: `${fontFamily}, "Montserrat", system-ui, sans-serif`,
              fontWeight,
              fontSize: `${baseFontSize}px`,
              lineHeight: 1.0,
              color: "#FFFFFF",
              opacity,
              letterSpacing: "-0.01em",
              padding: `${pillPadY}px ${pillPadX}px`,
              borderRadius: `${pillRadius}px`,
              backgroundColor: pillBg,
              textShadow: textShadowCss,
              WebkitTextStroke: outlinePx > 0 ? `${outlinePx}px #000000` : undefined,
              paintOrder: outlinePx > 0 ? ("stroke fill" as React.CSSProperties["paintOrder"]) : undefined,
              display: "inline-block",
              whiteSpace: "nowrap",
              transition: "background-color 60ms linear",
              willChange: "background-color",
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
