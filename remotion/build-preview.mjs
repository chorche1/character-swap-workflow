// Build the React-island preview bundle that the FastAPI page loads
// when the user picks a Remotion template in the Editor tab.
//
// Source:    remotion/src/preview/index.tsx
// Output:    web/static/remotion-preview.js  (+ .js.map)
//
// Run via:   npm run build-preview     (or `character-swap remotion-install`)
import * as esbuild from "esbuild";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..");
const OUT_DIR = path.join(REPO_ROOT, "web", "static");

await esbuild.build({
  entryPoints: [path.join(HERE, "src/preview/index.tsx")],
  outfile: path.join(OUT_DIR, "remotion-preview.js"),
  bundle: true,
  platform: "browser",
  format: "iife",
  globalName: "RemotionPreviewBundle",
  target: ["es2020"],
  jsx: "automatic",
  sourcemap: true,
  minify: true,
  loader: { ".tsx": "tsx", ".ts": "ts" },
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  logLevel: "info",
});

console.log(`[remotion-preview] built → ${path.join(OUT_DIR, "remotion-preview.js")}`);
