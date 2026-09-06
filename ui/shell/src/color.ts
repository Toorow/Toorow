/**
 * Colour math for the widget platform — the four MUI helpers, without MUI.
 *
 * WHY THIS FILE EXISTS. `@mui/material/styles` was imported by 34 source files
 * across `ui/shell`, `ui/cards/*` and `ui/widgets/*` for exactly five things:
 * `alpha`, `darken`, `lighten`, `getContrastRatio` and `decomposeColor`. Five
 * pure functions over a colour string were holding a runtime dependency worth
 * 217 kB on the KPI card bundle alone (see `primitives.tsx` for the measurement).
 *
 * The implementations below are FAITHFUL to MUI v9's: same luminance rounding
 * (3 decimals), same `parseInt` truncation when recomposing an rgb triple, same
 * coefficient clamping. That fidelity is not decoration — `ensureContrast()` in
 * `theme.ts` walks `darken`/`lighten` in 0.08 steps until it crosses 3:1, and
 * `BrandedTheme.test.tsx` pins the colours that walk produces. A helper that
 * rounded differently would move a ratified brand colour by a step.
 *
 * Accepted inputs: `#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb(...)`, `rgba(...)`.
 * The token file uses hex and `rgba()`; nothing here needs hsl.
 */

export interface DecomposedColor {
  type: "rgb" | "rgba";
  values: number[];
}

/** `#FF99C8` / `#fa0` / `#FF99C880` -> `rgb(...)` / `rgba(...)`, as MUI does. */
export function hexToRgb(hex: string): string {
  const raw = hex.slice(1);
  const size = raw.length < 6 ? 1 : 2;
  const re = new RegExp(`.{1,${size}}`, "g");
  const matched: string[] = raw.match(re) ?? [];
  const parts = size === 1 ? matched.map((n) => n + n) : matched;
  const channels = parts.map((n) => parseInt(n, 16));
  return channels.length === 4
    ? // The 4th hex pair is 0-255; MUI expresses alpha as 0-1 with 3 decimals.
      `rgba(${channels[0]}, ${channels[1]}, ${channels[2]}, ${Number(
        (channels[3] / 255).toFixed(3),
      )})`
    : `rgb(${channels[0]}, ${channels[1]}, ${channels[2]})`;
}

export function decomposeColor(color: string): DecomposedColor {
  if (color.charAt(0) === "#") return decomposeColor(hexToRgb(color));

  const marker = color.indexOf("(");
  const type = color.substring(0, marker);
  if (type !== "rgb" && type !== "rgba") {
    throw new Error(`toorow: unsupported color "${color}" — hex, rgb() and rgba() only.`);
  }
  const values = color
    .substring(marker + 1, color.length - 1)
    .split(",")
    .map((value) => parseFloat(value));
  return { type, values };
}

/** `rgb(234, 140, 184)` — the 3 colour channels are TRUNCATED, exactly as MUI. */
function recomposeColor({ type, values }: DecomposedColor): string {
  const out = values.map((n, i) => (i < 3 ? Math.trunc(n) : n));
  return `${type}(${out.join(", ")})`;
}

/** Relative luminance (WCAG), rounded to 3 decimals like MUI's `getLuminance`. */
export function getLuminance(color: string): number {
  const { values } = decomposeColor(color);
  const rgb = values.map((val) => {
    const v = val / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return Number((0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]).toFixed(3));
}

export function getContrastRatio(foreground: string, background: string): number {
  const lumA = getLuminance(foreground);
  const lumB = getLuminance(background);
  return (Math.max(lumA, lumB) + 0.05) / (Math.min(lumA, lumB) + 0.05);
}

/** Sets the alpha channel, keeping the colour. `alpha("#FF99C8", 0.12)`. */
export function alpha(color: string, value: number): string {
  const decomposed = decomposeColor(color);
  const clamped = Math.min(Math.max(value, 0), 1);
  return recomposeColor({
    type: "rgba",
    values: [decomposed.values[0], decomposed.values[1], decomposed.values[2], clamped],
  });
}

export function darken(color: string, coefficient: number): string {
  const decomposed = decomposeColor(color);
  const k = Math.min(Math.max(coefficient, 0), 1);
  const values = [...decomposed.values];
  for (let i = 0; i < 3; i += 1) values[i] *= 1 - k;
  return recomposeColor({ ...decomposed, values });
}

export function lighten(color: string, coefficient: number): string {
  const decomposed = decomposeColor(color);
  const k = Math.min(Math.max(coefficient, 0), 1);
  const values = [...decomposed.values];
  for (let i = 0; i < 3; i += 1) values[i] += (255 - values[i]) * k;
  return recomposeColor({ ...decomposed, values });
}
