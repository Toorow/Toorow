/**
 * Inline SVG icons (AC7 font-subsetting strategy — Option B).
 *
 * Instead of pulling the ~4.4 MB Material Symbols icon font through
 * @toorow/shell, this widget renders the handful of icons it needs as tiny
 * inline <svg> paths. Total cost: a few hundred bytes vs 4.4 MB.
 *
 * Paths are the Material Symbols Outlined 24px glyph outlines for:
 *   info           (provenance affordance, AC5)
 *   arrow_upward   (positive delta, AC2)
 *   arrow_downward (negative delta, AC2)
 *
 * `currentColor` is used for fill so icons inherit MUI theme text/success/error
 * colors (AD-11 — no hardcoded hex).
 */

interface IconProps {
  size?: number;
  title?: string;
  style?: React.CSSProperties;
}

function svgProps(size: number, title?: string) {
  return {
    width: size,
    height: size,
    viewBox: "0 -960 960 960",
    fill: "currentColor",
    role: title ? ("img" as const) : ("presentation" as const),
    "aria-hidden": title ? undefined : true,
    focusable: false,
  };
}

export function InfoIcon({ size = 16, title, style }: IconProps) {
  return (
    <svg {...svgProps(size, title)} style={style}>
      {title ? <title>{title}</title> : null}
      <path d="M440-280h80v-240h-80v240Zm40-320q17 0 28.5-11.5T520-640q0-17-11.5-28.5T480-680q-17 0-28.5 11.5T440-640q0 17 11.5 28.5T480-600Zm0 520q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 83-31.5 156T763-197q-54 54-127 85.5T480-80Z" />
    </svg>
  );
}

export function ArrowUpwardIcon({ size = 18, title, style }: IconProps) {
  return (
    <svg {...svgProps(size, title)} style={style}>
      {title ? <title>{title}</title> : null}
      <path d="M440-160v-487L216-423l-56-57 320-320 320 320-56 57-224-224v487h-80Z" />
    </svg>
  );
}

export function ArrowDownwardIcon({ size = 18, title, style }: IconProps) {
  return (
    <svg {...svgProps(size, title)} style={style}>
      {title ? <title>{title}</title> : null}
      <path d="M480-160 160-480l56-57 224 224v-487h80v487l224-224 56 57-320 320Z" />
    </svg>
  );
}
