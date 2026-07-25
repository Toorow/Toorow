import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// AD-11: every widget compiles to ONE self-contained HTML file — no external
// URL imports; CI fails the build on any http(s) reference in the bundle.
//
// vite-plugin-singlefile 2.3.3 supports Vite 8 (Rolldown). It inlines all JS
// and CSS into dist/index.html. We additionally force full inlining so no asset
// escapes as a separate file or CDN link.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
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
