import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    // A single run must not claim the whole machine. Several sessions run
    // suites here at the same time, and an unbounded fork pool opens one
    // worker per logical CPU (32 here) at 45-400 MB each: two concurrent
    // runs were enough to exhaust RAM. Six keeps a run under ~1.5 GB.
    pool: "forks",
    poolOptions: { forks: { maxForks: 6, minForks: 1 } },
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Disable CSS processing in tests — we test behaviour, not styles.
    // This prevents Vitest from trying to resolve font @imports in fonts.css.
    css: false,
  },
});
