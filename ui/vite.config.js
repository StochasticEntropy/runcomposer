import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Production assets build straight into the Python package so the wheel
// pre-bundles the UI — evaluators need no Node toolchain (DESIGN.md §14 P0/P1).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/runcomposer/ui_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8100",
    },
  },
});
