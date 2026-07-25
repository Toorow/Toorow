import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest config for @toorow/shell (Story 2.5, T5).
// Pattern mirrors ui/admin/vitest.config.ts and ui/widgets/google-analytics/vitest.config.ts.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Disable CSS processing in tests -- tests cover behaviour, not styles.
    // Prevents Vitest from trying to resolve font @imports in fonts.css.
    css: false,
  },
});
