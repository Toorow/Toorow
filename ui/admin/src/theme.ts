/**
 * Admin console MUI theme — Story 8.1 (design system toorow, rebrand 2026-07-19).
 *
 * Imports the token-derived theme from the shared tokens package and builds the
 * MUI theme. The three toorow brand faces are self-hosted via @fontsource
 * packages (AD-11: no CDN URLs): Lexend (titres), Plus Jakarta Sans (corps &
 * tableaux), JetBrains Mono (logs MCP & code dbt).
 *
 * Run `pnpm build:tokens` (from ui/) before consuming this theme to regenerate
 * ui/tokens/dist/theme.ts if you have changed tokens.json.
 */
import "@fontsource-variable/lexend";
import "@fontsource-variable/plus-jakarta-sans";
import "@fontsource-variable/geist";
import "@fontsource-variable/jetbrains-mono";
import { createTheme, type ThemeOptions } from "@mui/material/styles";
import themeOptions from "../../tokens/dist/theme";

/**
 * adminTheme — MUI theme for the Connector admin console.
 * toorow design system: rose accent #FF99C8, brand-black neutrals, pastel
 * lavande decorative, tabular-numeral heroes, generous spacing, left-sidebar shell.
 *
 * Light + dark: MUI follows the OS color scheme via `data-mui-color-scheme`
 * (a user-preference switch will set it explicitly later). The shell + pages read
 * their surface/text colors from the CSS token layer in `shell/application.css`,
 * which carries a matching `[data-mui-color-scheme="dark"]` override so surfaces
 * and text flip together (no white-on-white).
 */
export const adminTheme = createTheme(themeOptions as ThemeOptions);
