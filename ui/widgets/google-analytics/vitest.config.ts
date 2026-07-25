import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Disable CSS processing in tests — we test behaviour, not styles.
    // This prevents Vitest from trying to resolve font @imports in fonts.css.
    css: false,
  },
});
