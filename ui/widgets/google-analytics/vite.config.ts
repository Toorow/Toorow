import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// AD-11: every widget compiles to ONE self-contained HTML file — no external
// URL imports; CI fails the build on any http(s) reference in the bundle.
//
// vite-plugin-singlefile 2.3.3 supports Vite 8 (Rolldown). It inlines all JS
// and CSS into dist/index.html. We additionally force full inlining so no asset
// escapes as a separate file or CDN link.
//
// Font subsetting (AC7 / review-1-2 M3): @toorow/shell's fonts.css @imports
// the FULL Material Symbols icon font (7 weights × ~600 KB ≈ 4.4 MB). This
// widget uses inline <svg> icons instead (src/icons.tsx), so it needs none of
// those glyphs. We alias the Material Symbols CSS to an empty stub so the icon
// font never enters this widget's single-file bundle — dropping ~4.4 MB and
// keeping the total under the 1.5 MB target. Roboto Flex (text face) stays.
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
    // Inline every asset regardless of size (no separate asset files / data-URL cutoff).
    assetsInlineLimit: Number.POSITIVE_INFINITY,
    // Emit a single CSS payload inlined into the HTML.
    cssCodeSplit: false,
    // Keep the output deterministic and self-contained.
    target: "esnext",
    // Do not emit source maps into a separate file for the shippable bundle.
    sourcemap: false,
    rollupOptions: {
      // Belt-and-suspenders: no code splitting so everything lands in one chunk.
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
