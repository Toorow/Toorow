import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// AD-11: the KPI card compiles to ONE self-contained HTML file — no external URL
// imports; the CI bundle gate (ui/scripts/bundle-check.mjs) fails on any http(s)
// reference. Mirrors the google-analytics widget's single-file setup.
//
// The card uses inline <svg> icons (src/icons.tsx), so it needs none of the
// Material Symbols glyphs pulled in by @toorow/shell's fonts.css. We alias the
// Material Symbols CSS to an empty stub so the ~4.4 MB icon font never enters the
// bundle, keeping it well under the 1.5 MB widget gate. Plus Jakarta Sans Variable stays inlined.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: {
    alias: [
      {
        find: "@fontsource/material-symbols-outlined/index.css",
        replacement: fileURLToPath(
          new URL("./src/material-symbols-stub.css", import.meta.url),
        ),
      },
    ],
  },
  build: {
    assetsInlineLimit: Number.POSITIVE_INFINITY,
    cssCodeSplit: false,
    target: "esnext",
    sourcemap: false,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
