import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// AD-11: single self-contained HTML file — no external URL imports.
// Material Symbols CSS aliased to empty stub to keep bundle under gate.
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
