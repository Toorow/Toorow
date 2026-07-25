import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Admin console Vite config (Story 2.4, T2.2).
//
// NOT vite-plugin-singlefile: admin is a multi-file SPA served in a real
// browser tab. AD-11 single-file rule applies ONLY to MCP widget iframes.
// Output to dist/; index.html is the SPA entry point.
//
// Dev proxy: API calls to /api/* are forwarded to the mcp-server at :8000.
// Run `pnpm --filter @toorow/admin dev` for hot-reload (port 5174).
// Build: `pnpm --filter @toorow/admin build` then serve via mcp-server /admin.
export default defineConfig({
  plugins: [react()],
  // G-15 (review-global-gaps follow-up): the console is served under /admin by
  // the mcp-server dispatcher. With the default base "/", the built index.html
  // referenced /assets/*.js which 404'd through the server -- the console only
  // ever rendered on the Vite dev server. A relative base works under any mount.
  base: "./",
  build: {
    outDir: "dist",
    target: "esnext",
    sourcemap: false,
  },
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
