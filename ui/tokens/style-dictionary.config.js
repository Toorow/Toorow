/**
 * Style Dictionary build script — Story 8.1 (thème toorow, rebrand 2026-07-19).
 *
 * Pipeline: tokens.json (DTCG format) → dist/theme.ts (MUI v9 ThemeOptions)
 *
 * Run via: node ui/tokens/style-dictionary.config.js
 * Output:  ui/tokens/dist/theme.ts  (gitignored — regenerated in CI before widget build)
 *
 * toorow design rules enforced in output:
 *   1. ONE accent: #FF99C8 rose (hover #F77FB4). #D1C4E9 lavande = decorative only.
 *   2. Brand black (#111111) / near-white (#FAFAFA) + #EBEBF3-derived dividers.
 *   3. Tabular numerals on ALL numeric displays.
 *   4. No default-MUI look (no purple AppBar, no raised blue buttons, etc.).
 *   5. Generous spacing: card 24px, section 32-48px, table rows 52px.
 *   6. Tokens consumable by shell/widgets packages.
 *   7. Typo: Lexend (titres) / Plus Jakarta Sans (corps & tableaux) /
 *      JetBrains Mono (logs MCP & code dbt).
 */

import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const tokensPath = resolve(__dirname, "tokens.json");
const outDir = resolve(__dirname, "dist");
const outFile = resolve(outDir, "theme.ts");

// Read and parse the token file
const raw = JSON.parse(readFileSync(tokensPath, "utf8"));

// Helper: safely traverse nested object by dot-path like "origin.accent.primary"
function getToken(path) {
  const parts = path.split(".");
  let cur = raw;
  for (const p of parts) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = cur[p];
  }
  const v = cur?.$value;
  if (typeof v !== "string" && typeof v !== "number") {
    throw new Error(`tokens.json: missing or invalid $value at "${path}"`);
  }
  return v;
}

// ---------------------------------------------------------------------------
// Read Origin tokens
// ---------------------------------------------------------------------------

const ACCENT          = getToken("origin.accent.primary");
const ACCENT_HOVER    = getToken("origin.accent.primary-hover");
const ON_ACCENT       = getToken("origin.accent.on-primary");

const TEXT_DARK                = getToken("origin.neutral.text");
const TEXT_SECONDARY           = getToken("origin.neutral.text-secondary");
const TEXT_ON_DARK             = getToken("origin.neutral.text-on-dark");
const SURFACE_DARK             = getToken("origin.neutral.surface-dark");
const SURFACE_DARK_ELEVATED    = getToken("origin.neutral.surface-dark-elevated");
const SURFACE_LIGHT            = getToken("origin.neutral.surface-light");
const BG_LIGHT                 = getToken("origin.neutral.background-light");
const DIVIDER_FULL             = getToken("origin.neutral.divider-base");

const ERROR    = getToken("origin.semantic.error");
const ON_ERROR = getToken("origin.semantic.on-error");
const ERROR_CONTAINER = getToken("origin.semantic.error-container");
const WARNING  = getToken("origin.semantic.warning");
const ON_WARNING = getToken("origin.semantic.on-warning");
const WARNING_CONTAINER = getToken("origin.semantic.warning-container");
const SUCCESS  = getToken("origin.semantic.success");
const ON_SUCCESS = getToken("origin.semantic.on-success");
const SUCCESS_CONTAINER = getToken("origin.semantic.success-container");
const INFO     = getToken("origin.semantic.info");
const ON_INFO  = getToken("origin.semantic.on-info");
const INFO_CONTAINER = getToken("origin.semantic.info-container");

const FONT_PRIMARY = getToken("origin.typography.font-family-primary");
const FONT_DISPLAY = getToken("origin.typography.font-family-display");
const FONT_MONO    = getToken("origin.typography.font-family-mono");
const H1_WEIGHT = getToken("origin.typography.h1-weight");
const H2_WEIGHT = getToken("origin.typography.h2-weight");
const H3_WEIGHT = getToken("origin.typography.h3-weight");

const SHADOW_CARD_LIGHT = getToken("origin.shadow.card-light");
const SHADOW_CARD_DARK  = getToken("origin.shadow.card-dark");

// Viz palette (Epic 9 card rule c: no per-component hex in viz primitives).
// Was hand-patched into the generated dist during review-9 fixes without a
// source-of-truth — now declared in tokens.json (origin.viz.*) so
// `pnpm build:tokens` is deterministic (found 2026-07-17, story 9-10 gate).
const ORIGIN_CATEGORICAL_PALETTE = [1, 2, 3, 4, 5, 6].map((i) =>
  getToken(`origin.viz.categorical-${i}`),
);
const TRACK_LIGHT = getToken("origin.viz.track-light");

// ---------------------------------------------------------------------------
// Generate theme.ts content
// ---------------------------------------------------------------------------

const tsContent = `/**
 * toorow theme for toorow — Story 8.1 (rebrand 2026-07-19).
 *
 * DO NOT EDIT MANUALLY in production — re-run \`pnpm build:tokens\` to regenerate
 * from ui/tokens/tokens.json via style-dictionary.config.js.
 *
 * toorow design rules:
 *   1. ONE accent: #FF99C8 rose (hover #F77FB4). #D1C4E9 lavande = decorative only.
 *   2. Brand black (#111111) / near-white (#FAFAFA) + #EBEBF3-derived dividers.
 *   3. The number is the hero — tabular-nums on ALL numeric displays.
 *   4. No default-MUI look: no purple AppBar, no raised blue buttons, etc.
 *   5. Generous spacing: card padding 24, section gaps 32-48, table rows 52px.
 *   6. Tokens consumable by shell/widgets packages.
 *   7. Typo: Lexend (titres) / Plus Jakarta Sans (corps) / JetBrains Mono (code).
 *
 * Uses MUI v9 CSS Variables mode (cssVariables: true) for runtime light/dark
 * switching without full re-render.
 */

// Dependency-free generated artifact: pure data, no imports. The consumer
// (ui/admin/src/theme.ts) applies the MUI ThemeOptions typing.

// ---------------------------------------------------------------------------
// Origin token constants (matches tokens.json)
// ---------------------------------------------------------------------------

// Accent
const ACCENT = ${JSON.stringify(ACCENT)};
const ACCENT_HOVER = ${JSON.stringify(ACCENT_HOVER)};
const ON_ACCENT = ${JSON.stringify(ON_ACCENT)};

// Neutrals
const TEXT_DARK = ${JSON.stringify(TEXT_DARK)};
const TEXT_SECONDARY = ${JSON.stringify(TEXT_SECONDARY)};
const TEXT_ON_DARK = ${JSON.stringify(TEXT_ON_DARK)};
const SURFACE_DARK = ${JSON.stringify(SURFACE_DARK)};
const SURFACE_DARK_ELEVATED = ${JSON.stringify(SURFACE_DARK_ELEVATED)};
const SURFACE_LIGHT = ${JSON.stringify(SURFACE_LIGHT)};
const BG_LIGHT = ${JSON.stringify(BG_LIGHT)};
const DIVIDER_FULL = ${JSON.stringify(DIVIDER_FULL)};

// #EBEBF3 at various opacities
const DIVIDER_SUBTLE = "rgba(235,235,243,0.08)";
const DIVIDER_LIGHT = "rgba(235,235,243,0.12)";
const DIVIDER_MEDIUM = "rgba(235,235,243,0.30)";

// Semantics (mains accessibles + conteneurs pastel toorow)
const ERROR = ${JSON.stringify(ERROR)};
const ON_ERROR = ${JSON.stringify(ON_ERROR)};
const ERROR_CONTAINER = ${JSON.stringify(ERROR_CONTAINER)};
const WARNING = ${JSON.stringify(WARNING)};
const ON_WARNING = ${JSON.stringify(ON_WARNING)};
const WARNING_CONTAINER = ${JSON.stringify(WARNING_CONTAINER)};
const SUCCESS = ${JSON.stringify(SUCCESS)};
const ON_SUCCESS = ${JSON.stringify(ON_SUCCESS)};
const SUCCESS_CONTAINER = ${JSON.stringify(SUCCESS_CONTAINER)};
const INFO = ${JSON.stringify(INFO)};
const ON_INFO = ${JSON.stringify(ON_INFO)};
const INFO_CONTAINER = ${JSON.stringify(INFO_CONTAINER)};

// Shadows
const SHADOW_CARD_LIGHT = ${JSON.stringify(SHADOW_CARD_LIGHT)};
const SHADOW_CARD_DARK = ${JSON.stringify(SHADOW_CARD_DARK)};

// Typography (toorow : Lexend titres / Plus Jakarta Sans corps / JetBrains Mono code)
const FONT_PRIMARY = ${JSON.stringify(FONT_PRIMARY)};
const FONT_DISPLAY = ${JSON.stringify(FONT_DISPLAY)};
const FONT_MONO = ${JSON.stringify(FONT_MONO)};

// Viz palette (card rule c — consumed by card-shell LineChart/BarChart/Donut)
const ORIGIN_CATEGORICAL_PALETTE = ${JSON.stringify(ORIGIN_CATEGORICAL_PALETTE)};
const TRACK_LIGHT = ${JSON.stringify(TRACK_LIGHT)};

// ---------------------------------------------------------------------------
// Shared component overrides
// ---------------------------------------------------------------------------

const sharedComponentOverrides = {
  MuiCssBaseline: {
    styleOverrides: {
      body: {
        background: "transparent",
        fontVariantNumeric: "lining-nums tabular-nums",
      },
      "td, th, [data-numeric]": {
        fontVariantNumeric: "lining-nums tabular-nums",
      },
    },
  },

  MuiAppBar: {
    defaultProps: {
      elevation: 0,
      color: "default",
    },
    styleOverrides: {
      root: {
        backgroundColor: SURFACE_LIGHT,
        color: TEXT_DARK,
        borderBottom: \`1px solid \${DIVIDER_FULL}\`,
      },
    },
  },

  MuiButton: {
    defaultProps: {
      disableElevation: true,
    },
    styleOverrides: {
      root: {
        textTransform: "none",
        fontWeight: 600,
        borderRadius: 999,
        letterSpacing: 0,
        paddingLeft: 18,
        paddingRight: 18,
      },
      containedPrimary: {
        backgroundColor: ACCENT,
        color: ON_ACCENT,
        "&:hover": {
          backgroundColor: ACCENT_HOVER,
        },
      },
      outlinedPrimary: {
        borderColor: ACCENT,
        "&:hover": {
          backgroundColor: \`\${ACCENT}14\`,
        },
      },
    },
  },

  MuiIconButton: {
    styleOverrides: {
      root: {
        color: TEXT_SECONDARY,
        "&:hover": {
          backgroundColor: DIVIDER_FULL,
        },
      },
    },
  },

  MuiTable: {
    defaultProps: {
      size: "medium",
    },
  },
  MuiTableCell: {
    styleOverrides: {
      root: {
        height: 52,
        borderBottom: \`1px solid \${DIVIDER_FULL}\`,
        padding: "0 16px",
        fontVariantNumeric: "lining-nums tabular-nums",
      },
      head: {
        fontWeight: 600,
        fontSize: "0.75rem",
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: TEXT_SECONDARY,
        height: 40,
        borderBottom: \`1px solid \${DIVIDER_FULL}\`,
        backgroundColor: "transparent",
      },
    },
  },
  MuiTableRow: {
    styleOverrides: {
      root: {
        "&:last-of-type td": {
          borderBottom: 0,
        },
        "&:hover": {
          backgroundColor: \`\${DIVIDER_FULL}40\`,
        },
      },
      head: {
        "&:hover": {
          backgroundColor: "transparent",
        },
      },
    },
  },

  MuiCard: {
    styleOverrides: {
      root: {
        borderRadius: 16,
        boxShadow: SHADOW_CARD_LIGHT,
        border: "none",
      },
    },
  },
  MuiPaper: {
    defaultProps: {
      elevation: 0,
    },
    styleOverrides: {
      root: {
        borderRadius: 12,
      },
      outlined: {
        borderColor: DIVIDER_FULL,
      },
      elevation1: {
        boxShadow: SHADOW_CARD_LIGHT,
      },
    },
  },

  MuiChip: {
    styleOverrides: {
      root: {
        borderRadius: 999,
        fontSize: "0.75rem",
        fontWeight: 500,
        height: 24,
      },
      colorDefault: {
        backgroundColor: DIVIDER_FULL,
        color: TEXT_SECONDARY,
      },
    },
  },

  MuiDrawer: {
    styleOverrides: {
      paper: {
        border: "none",
        borderRight: \`1px solid \${DIVIDER_FULL}\`,
        backgroundColor: SURFACE_LIGHT,
      },
    },
  },

  MuiListItemButton: {
    styleOverrides: {
      root: {
        borderRadius: 8,
        margin: "1px 8px",
        padding: "8px 12px",
        "&.Mui-selected": {
          backgroundColor: \`\${ACCENT}20\`,
          color: TEXT_DARK,
          "&:hover": {
            backgroundColor: \`\${ACCENT}30\`,
          },
          "& .MuiListItemIcon-root": {
            color: TEXT_DARK,
          },
        },
        "&:hover": {
          backgroundColor: DIVIDER_FULL,
        },
      },
    },
  },

  MuiListItemIcon: {
    styleOverrides: {
      root: {
        minWidth: 36,
        color: TEXT_SECONDARY,
      },
    },
  },

  MuiListItemText: {
    styleOverrides: {
      primary: {
        fontSize: "0.875rem",
        fontWeight: 500,
      },
    },
  },

  MuiTab: {
    styleOverrides: {
      root: {
        textTransform: "none",
        fontWeight: 500,
      },
    },
  },

  MuiSwitch: {
    styleOverrides: {
      switchBase: {
        "&.Mui-checked": {
          color: ACCENT,
          "& + .MuiSwitch-track": {
            backgroundColor: ACCENT,
          },
        },
      },
    },
  },

  MuiOutlinedInput: {
    styleOverrides: {
      root: {
        borderRadius: 8,
        "& fieldset": {
          borderColor: DIVIDER_FULL,
        },
        "&:hover fieldset": {
          borderColor: TEXT_SECONDARY,
        },
        "&.Mui-focused fieldset": {
          borderColor: ACCENT,
          borderWidth: 1.5,
        },
      },
    },
  },

  MuiDivider: {
    styleOverrides: {
      root: {
        borderColor: DIVIDER_FULL,
      },
    },
  },
};

// ---------------------------------------------------------------------------
// Full theme options object
// ---------------------------------------------------------------------------

const themeOptions = {
  cssVariables: {
    colorSchemeSelector: "data",
  },

  colorSchemes: {
    light: {
      palette: {
        primary: {
          main: ACCENT,
          dark: ACCENT_HOVER,
          contrastText: ON_ACCENT,
        },
        secondary: {
          main: TEXT_SECONDARY,
          contrastText: TEXT_ON_DARK,
        },
        error: {
          main: ERROR,
          light: ERROR_CONTAINER,
          contrastText: ON_ERROR,
        },
        warning: {
          main: WARNING,
          light: WARNING_CONTAINER,
          contrastText: ON_WARNING,
        },
        success: {
          main: SUCCESS,
          light: SUCCESS_CONTAINER,
          contrastText: ON_SUCCESS,
        },
        info: {
          main: INFO,
          light: INFO_CONTAINER,
          contrastText: ON_INFO,
        },
        background: {
          default: BG_LIGHT,
          paper: SURFACE_LIGHT,
        },
        text: {
          primary: TEXT_DARK,
          secondary: TEXT_SECONDARY,
        },
        divider: DIVIDER_FULL,
        action: {
          hover: \`\${DIVIDER_FULL}80\`,
          selected: \`\${ACCENT}14\`,
          focus: \`\${ACCENT}20\`,
        },
      },
    },

    dark: {
      palette: {
        primary: {
          main: ACCENT,
          dark: ACCENT_HOVER,
          contrastText: ON_ACCENT,
        },
        secondary: {
          main: TEXT_ON_DARK,
          contrastText: SURFACE_DARK,
        },
        error: {
          main: "#F28B93",
          contrastText: TEXT_DARK,
        },
        warning: {
          main: "#F6C177",
          contrastText: TEXT_DARK,
        },
        success: {
          main: "#7FC8A4",
          contrastText: TEXT_DARK,
        },
        info: {
          main: "#B39DDB",
          contrastText: TEXT_DARK,
        },
        background: {
          default: SURFACE_DARK,
          paper: SURFACE_DARK_ELEVATED,
        },
        text: {
          primary: TEXT_ON_DARK,
          secondary: "rgba(250,250,250,0.60)",
        },
        divider: DIVIDER_MEDIUM,
        action: {
          hover: DIVIDER_SUBTLE,
          selected: DIVIDER_LIGHT,
        },
      },
    },
  },

  typography: {
    fontFamily: FONT_PRIMARY,
    fontSize: 15,
    h1: {
      fontFamily: FONT_DISPLAY,
      fontSize: "2.75rem",
      fontWeight: ${JSON.stringify(H1_WEIGHT)},
      letterSpacing: "-0.02em",
      lineHeight: 1.1,
    },
    h2: {
      fontFamily: FONT_DISPLAY,
      fontSize: "2rem",
      fontWeight: ${JSON.stringify(H2_WEIGHT)},
      letterSpacing: "-0.01em",
      lineHeight: 1.2,
    },
    h3: {
      fontFamily: FONT_DISPLAY,
      fontSize: "1.5rem",
      fontWeight: ${JSON.stringify(H3_WEIGHT)},
      lineHeight: 1.3,
    },
    h4: {
      fontFamily: FONT_DISPLAY,
      fontSize: "1.25rem",
      fontWeight: 500,
      lineHeight: 1.4,
    },
    h5: {
      fontFamily: FONT_DISPLAY,
      fontSize: "1.125rem",
      fontWeight: 500,
      lineHeight: 1.4,
    },
    h6: {
      fontFamily: FONT_DISPLAY,
      fontSize: "1rem",
      fontWeight: 500,
      lineHeight: 1.4,
    },
    body1: {
      fontSize: "0.9375rem",
      fontWeight: 400,
      lineHeight: 1.6,
    },
    body2: {
      fontSize: "0.875rem",
      fontWeight: 400,
      lineHeight: 1.5,
    },
    caption: {
      fontSize: "0.75rem",
      fontWeight: 400,
      lineHeight: 1.4,
    },
    button: {
      fontSize: "0.875rem",
      fontWeight: 500,
      textTransform: "none",
      letterSpacing: 0,
    },
    overline: {
      fontSize: "0.6875rem",
      fontWeight: 600,
      textTransform: "uppercase",
      letterSpacing: "0.08em",
    },
  },

  shape: {
    borderRadius: 12,
  },

  components: sharedComponentOverrides,
};

export default themeOptions;
export type ConnectorThemeTokens = typeof themeOptions;

// Named exports for consuming packages
export {
  ACCENT,
  ACCENT_HOVER,
  ON_ACCENT,
  TEXT_DARK,
  TEXT_SECONDARY,
  TEXT_ON_DARK,
  SURFACE_DARK,
  SURFACE_DARK_ELEVATED,
  SURFACE_LIGHT,
  BG_LIGHT,
  DIVIDER_FULL,
  DIVIDER_SUBTLE,
  DIVIDER_LIGHT,
  DIVIDER_MEDIUM,
  ERROR,
  ON_ERROR,
  ERROR_CONTAINER,
  WARNING,
  ON_WARNING,
  WARNING_CONTAINER,
  SUCCESS,
  ON_SUCCESS,
  SUCCESS_CONTAINER,
  INFO,
  ON_INFO,
  INFO_CONTAINER,
  FONT_PRIMARY,
  FONT_DISPLAY,
  FONT_MONO,
  SHADOW_CARD_LIGHT,
  SHADOW_CARD_DARK,
  ORIGIN_CATEGORICAL_PALETTE,
  TRACK_LIGHT,
};
`;

mkdirSync(outDir, { recursive: true });
writeFileSync(outFile, tsContent, "utf8");
console.log(`✅ toorow theme build complete: ${outFile}`);
