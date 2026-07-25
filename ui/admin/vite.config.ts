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
  // The console is served at the ROOT of app.toorow.com (Firebase Hosting, with
  // `**` rewritten to /index.html). It must therefore be an ABSOLUTE base.
  //
  // History, and why this changed: G-15 set `base: "./"` when the console was
  // mounted under /admin by the mcp-server dispatcher. That mount is gone (it
  // now 404s), and a relative base is actively harmful under a SPA rewrite: from
  // a route two levels deep such as /p/:project/:workspace, `./assets/x.js`
  // resolves to /p/:project/assets/x.js, the rewrite answers it with index.html
  // (HTTP 200, text/html), the browser refuses to run HTML as a module — and the
  // page stays BLANK. Measured on the deployed console 2026-07-25: `/`,
  // `/overview` and `/create-org` rendered, `/p/default/overview` did not. Every
  // real application route lives at that depth, so every deep link and every
  // page refresh inside the app was broken.
  base: "/",
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
