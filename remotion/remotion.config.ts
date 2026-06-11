import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
// Default for manual `npx remotion render` runs. The Python bridge
// (remotion_render.py) always passes --concurrency explicitly, which
// overrides this — it pairs tabs-per-render with a process-wide cap on
// simultaneous renders, so total Chrome tab count stays bounded.
Config.setConcurrency(4);
Config.setChromiumOpenGlRenderer("angle");
Config.setEntryPoint("./src/index.ts");
