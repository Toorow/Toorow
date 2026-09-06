import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest config for admin console (Story 2.4, AC8).
// Separate from vite.config.ts so test setup does not pollute build config.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    // A single run must not claim the whole machine. Several sessions run
    // suites here at the same time, and an unbounded fork pool opens one
    // worker per logical CPU (32 here) at 45-400 MB each: two concurrent
    // runs were enough to exhaust RAM. Four avoids the load-sensitive, moving
    // async failures observed at six forks without retrying failed assertions.
    pool: "forks",
    poolOptions: { forks: { maxForks: 4, minForks: 1 } },
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // The shared setup raises Testing Library's async floor to 5s (see the
    // comment there). Vitest's own default is 5s, so a single findBy that
    // spends its budget would trip the test timeout before the query gave up
    // and the failure would name the wrong cause. 20s still catches a genuine
    // hang; it is not a licence for slow tests.
    testTimeout: 20000,
    // Disable CSS processing in tests — behaviour, not styles.
    // Prevents Vitest from trying to resolve font @imports.
    css: false,
  },
});
