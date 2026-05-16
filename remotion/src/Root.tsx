import React from "react";
import { Composition } from "remotion";
import { DEFAULT_CAPTION_PROPS } from "./types";
import { SubmagicPop } from "./compositions/SubmagicPop";
import { MrBeastBold } from "./compositions/MrBeastBold";
import { CapCutGlow } from "./compositions/CapCutGlow";
import { SubmagicPro } from "./compositions/SubmagicPro";

const FPS = 30;

function durationInFrames(props: typeof DEFAULT_CAPTION_PROPS): number {
  return Math.max(30, Math.ceil(props.videoDurationSecs * FPS));
}

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="SubmagicPop"
        component={SubmagicPop}
        fps={FPS}
        width={DEFAULT_CAPTION_PROPS.videoWidth}
        height={DEFAULT_CAPTION_PROPS.videoHeight}
        durationInFrames={durationInFrames(DEFAULT_CAPTION_PROPS)}
        defaultProps={DEFAULT_CAPTION_PROPS}
        calculateMetadata={({ props }) => ({
          durationInFrames: durationInFrames(props),
          width: props.videoWidth,
          height: props.videoHeight,
        })}
      />
      <Composition
        id="MrBeastBold"
        component={MrBeastBold}
        fps={FPS}
        width={DEFAULT_CAPTION_PROPS.videoWidth}
        height={DEFAULT_CAPTION_PROPS.videoHeight}
        durationInFrames={durationInFrames(DEFAULT_CAPTION_PROPS)}
        defaultProps={{ ...DEFAULT_CAPTION_PROPS, accent: "#FFFF00", fontFamily: "Anton", allCaps: true }}
        calculateMetadata={({ props }) => ({
          durationInFrames: durationInFrames(props),
          width: props.videoWidth,
          height: props.videoHeight,
        })}
      />
      <Composition
        id="CapCutGlow"
        component={CapCutGlow}
        fps={FPS}
        width={DEFAULT_CAPTION_PROPS.videoWidth}
        height={DEFAULT_CAPTION_PROPS.videoHeight}
        durationInFrames={durationInFrames(DEFAULT_CAPTION_PROPS)}
        defaultProps={{ ...DEFAULT_CAPTION_PROPS, accent: "#00E5FF", fontFamily: "Poppins", wordsPerCard: 5, allCaps: false }}
        calculateMetadata={({ props }) => ({
          durationInFrames: durationInFrames(props),
          width: props.videoWidth,
          height: props.videoHeight,
        })}
      />
      <Composition
        id="SubmagicPro"
        component={SubmagicPro}
        fps={FPS}
        width={DEFAULT_CAPTION_PROPS.videoWidth}
        height={DEFAULT_CAPTION_PROPS.videoHeight}
        durationInFrames={durationInFrames(DEFAULT_CAPTION_PROPS)}
        defaultProps={{
          ...DEFAULT_CAPTION_PROPS,
          accent: "#FFD400",
          fontFamily: "Montserrat",
          wordsPerCard: 3,
          allCaps: true,
        }}
        calculateMetadata={({ props }) => ({
          durationInFrames: durationInFrames(props),
          width: props.videoWidth,
          height: props.videoHeight,
        })}
      />
    </>
  );
};
