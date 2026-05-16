import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { Player, type PlayerRef } from "@remotion/player";
import type { BaseCaptionProps } from "../types";
import { SubmagicPop } from "../compositions/SubmagicPop";
import { MrBeastBold } from "../compositions/MrBeastBold";
import { CapCutGlow } from "../compositions/CapCutGlow";

const FPS = 30;

type CompId = "SubmagicPop" | "MrBeastBold" | "CapCutGlow";

const COMPONENTS: Record<CompId, React.ComponentType<BaseCaptionProps>> = {
  SubmagicPop,
  MrBeastBold,
  CapCutGlow,
};

const ROOTS = new Map<string, Root>();
const PLAYER_REFS = new Map<string, React.RefObject<PlayerRef | null>>();

function PlayerHost(p: { compositionId: CompId; props: BaseCaptionProps; playerRef: React.RefObject<PlayerRef | null> }) {
  const Comp = COMPONENTS[p.compositionId];
  if (!Comp) {
    return React.createElement("div", { style: { color: "#f87171", padding: 12 } }, `Unknown composition: ${p.compositionId}`);
  }
  const duration = Math.max(30, Math.ceil(p.props.videoDurationSecs * FPS));
  return React.createElement(Player, {
    ref: p.playerRef,
    component: Comp,
    compositionWidth: p.props.videoWidth,
    compositionHeight: p.props.videoHeight,
    durationInFrames: duration,
    fps: FPS,
    inputProps: p.props,
    controls: true,
    loop: true,
    style: { width: "100%", borderRadius: "12px", overflow: "hidden", backgroundColor: "#000" },
    acknowledgeRemotionLicense: true,
  });
}

function mount(elementId: string, compositionId: CompId, props: BaseCaptionProps) {
  const el = document.getElementById(elementId);
  if (!el) {
    console.warn("[remotion-preview] target element not found:", elementId);
    return;
  }
  let root = ROOTS.get(elementId);
  if (!root) {
    root = createRoot(el);
    ROOTS.set(elementId, root);
  }
  let ref = PLAYER_REFS.get(elementId);
  if (!ref) {
    ref = React.createRef<PlayerRef | null>();
    PLAYER_REFS.set(elementId, ref);
  }
  root.render(React.createElement(PlayerHost, { compositionId, props, playerRef: ref }));
}

function update(elementId: string, compositionId: CompId, props: BaseCaptionProps) {
  mount(elementId, compositionId, props);
}

function unmount(elementId: string) {
  const root = ROOTS.get(elementId);
  if (root) {
    root.unmount();
    ROOTS.delete(elementId);
  }
  PLAYER_REFS.delete(elementId);
}

export const RemotionPreview = { mount, update, unmount };
// Also attach to window so plain <script src=...> usage works without modules.
(globalThis as unknown as { RemotionPreview?: typeof RemotionPreview }).RemotionPreview = RemotionPreview;
