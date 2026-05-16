import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { Player, type PlayerRef } from "@remotion/player";
import type { BaseCaptionProps } from "../types";
import { SubmagicPop } from "../compositions/SubmagicPop";
import { MrBeastBold } from "../compositions/MrBeastBold";
import { CapCutGlow } from "../compositions/CapCutGlow";
import { SubmagicPro } from "../compositions/SubmagicPro";

const FPS = 30;

type CompId = "SubmagicPop" | "MrBeastBold" | "CapCutGlow" | "SubmagicPro";

const COMPONENTS: Record<CompId, React.ComponentType<BaseCaptionProps>> = {
  SubmagicPop,
  MrBeastBold,
  CapCutGlow,
  SubmagicPro,
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

// --- Playback control surface ---------------------------------------------
//
// The caption-editor timeline drives the Player externally: drag the
// scrubbing playhead → call seekTo(secs); during playback → poll
// getCurrentTimeSecs(); listen to play/pause to keep the UI playhead in
// sync. These helpers wrap the Player's frame-based API and convert to
// seconds since the host UI thinks in seconds and Whisper word timings
// are in seconds.

function _player(elementId: string): PlayerRef | null {
  return PLAYER_REFS.get(elementId)?.current ?? null;
}

function seekToSecs(elementId: string, secs: number): void {
  const p = _player(elementId);
  if (!p) return;
  const frame = Math.max(0, Math.round((Number(secs) || 0) * FPS));
  try { p.seekTo(frame); } catch { /* player not ready yet */ }
}

function getCurrentTimeSecs(elementId: string): number {
  const p = _player(elementId);
  if (!p) return 0;
  try { return (p.getCurrentFrame() || 0) / FPS; } catch { return 0; }
}

function play(elementId: string): void {
  const p = _player(elementId);
  if (!p) return;
  try { p.play(); } catch { /* nothing */ }
}

function pause(elementId: string): void {
  const p = _player(elementId);
  if (!p) return;
  try { p.pause(); } catch { /* nothing */ }
}

function isPlaying(elementId: string): boolean {
  const p = _player(elementId);
  if (!p) return false;
  try { return p.isPlaying(); } catch { return false; }
}

// Register a callback that fires on EVERY frame change (~30/sec). Returns
// an unsubscribe fn so callers don't leak listeners across re-mounts.
type FrameUpdate = { detail: { frame: number } };
function onFrameUpdate(
  elementId: string,
  cb: (secs: number) => void,
): () => void {
  const p = _player(elementId);
  if (!p) return () => undefined;
  const handler = (e: FrameUpdate) => {
    cb((e?.detail?.frame ?? 0) / FPS);
  };
  try {
    // Remotion's Player exposes "frameupdate" + "play"/"pause" events.
    (p as unknown as { addEventListener: (k: string, h: (e: FrameUpdate) => void) => void })
      .addEventListener("frameupdate", handler);
  } catch {
    return () => undefined;
  }
  return () => {
    try {
      (p as unknown as { removeEventListener: (k: string, h: (e: FrameUpdate) => void) => void })
        .removeEventListener("frameupdate", handler);
    } catch { /* ignore */ }
  };
}

export const RemotionPreview = {
  mount,
  update,
  unmount,
  seekToSecs,
  getCurrentTimeSecs,
  play,
  pause,
  isPlaying,
  onFrameUpdate,
};
// Also attach to window so plain <script src=...> usage works without modules.
(globalThis as unknown as { RemotionPreview?: typeof RemotionPreview }).RemotionPreview = RemotionPreview;
