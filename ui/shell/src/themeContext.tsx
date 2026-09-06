/**
 * The theme context — what `ThemeProvider` / `useTheme` / `useColorScheme` meant
 * when they came from `@mui/material/styles`.
 *
 * WHY IT IS THIS SMALL. MUI's provider carried a style engine (emotion), a CSS
 * variable emitter and a component-override registry. The widget platform used
 * exactly three things from it: a theme object down the tree, the active colour
 * scheme, and a setter the host's `data-color-scheme` signal drives. That is all
 * that is here.
 *
 * `useTheme()` returns the theme with `palette` already resolved to the ACTIVE
 * scheme — the same contract the 30 consumer components were written against, so
 * `theme.palette.text.primary` keeps flipping with the host.
 *
 * A component rendered OUTSIDE a provider gets `connectorTheme` (light) rather
 * than throwing. MUI behaved the same way, and several card tests render a chart
 * bare; making that a crash would turn a styling default into a test failure.
 */

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { connectorTheme } from "./theme";
import type { ColorScheme, WidgetTheme } from "./theme";

interface ThemeContextValue {
  theme: WidgetTheme;
  colorScheme: ColorScheme;
  setColorScheme: (scheme: ColorScheme) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export interface ThemeProviderProps {
  theme?: WidgetTheme;
  /** Initial scheme; the host's `data-color-scheme` overrides it at mount. */
  defaultColorScheme?: ColorScheme;
  children?: ReactNode;
}

export function ThemeProvider({
  theme = connectorTheme,
  defaultColorScheme = "light",
  children,
}: ThemeProviderProps) {
  const [colorScheme, setScheme] = useState<ColorScheme>(defaultColorScheme);

  const setColorScheme = useCallback((scheme: ColorScheme) => {
    setScheme(scheme);
    // MUI's `colorSchemeSelector: "data"` wrote the active scheme back onto
    // <html>. Keeping that write means host CSS and our own attribute selectors
    // still agree, and `WidgetShell`'s observer still has its re-entry guard.
    if (typeof document !== "undefined") {
      document.documentElement.setAttribute("data-color-scheme", scheme);
    }
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({
      theme: { ...theme, palette: theme.colorSchemes[colorScheme].palette },
      colorScheme,
      setColorScheme,
    }),
    [theme, colorScheme, setColorScheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/** The theme, with `palette` resolved to the active colour scheme. */
export function useTheme(): WidgetTheme {
  return useContext(ThemeContext)?.theme ?? connectorTheme;
}

/** The active scheme and its setter — the host binding in `WidgetShell` uses it. */
export function useColorScheme(): {
  colorScheme: ColorScheme;
  setColorScheme: (scheme: ColorScheme) => void;
} {
  const ctx = useContext(ThemeContext);
  return {
    colorScheme: ctx?.colorScheme ?? "light",
    setColorScheme: ctx?.setColorScheme ?? (() => {}),
  };
}
