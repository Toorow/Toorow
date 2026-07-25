import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest config for admin console (Story 2.4, AC8).
// Separate from vite.config.ts so test setup does not pollute build config.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Disable CSS processing in tests — behaviour, not styles.
    // Prevents Vitest from trying to resolve font @imports.
    css: false,
  },
});
