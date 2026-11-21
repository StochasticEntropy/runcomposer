import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

const LOCALES = join(import.meta.dirname, "src", "locales");

// The UI carries its messages inside the bundle. The server also serves them
// as `/locales/<locale>.json`, so the files are shipped next to the bundle,
// byte for byte as they are in the source tree.
function shipLocales(): Plugin {
  return {
    name: "runcomposer-ship-locales",
    generateBundle() {
      for (const name of readdirSync(LOCALES).sort()) {
        if (!name.endsWith(".json")) continue;
        this.emitFile({ type: "asset", fileName: `locales/${name}`, source: readFileSync(join(LOCALES, name)) });
      }
    },
  };
}

// Production assets build straight into the Python package, so the wheel
// carries the UI and nobody needs a Node toolchain to use it (DESIGN.md §10).
export default defineConfig({
  plugins: [react(), shipLocales()],
  build: {
    outDir: "../src/runcomposer/ui_dist",
    emptyOutDir: true,
    target: "es2022",
  },
  // The dev server passes the API through to `runcomposer serve` on its
  // default port. The `Host` header is passed through unchanged
  // (`changeOrigin: false` — Vite's string shorthand would rewrite it): the
  // server refuses a state-changing request whose `Origin` is not its own
  // `Host` (or listed in `core.api.cors`), and the page's origin is the dev
  // server's.
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8100", changeOrigin: false },
      "/artifacts": { target: "http://127.0.0.1:8100", changeOrigin: false },
    },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "node",
  },
});
