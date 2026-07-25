/**
 * Inline SVG icons for the KPI card (AD-11 — no Material Symbols font pulled in).
 * currentColor fill so icons inherit the MUI theme text/success/error colors.
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

export function ArrowUpwardIcon({ size = 14, title, style }: IconProps) {
  return (
    <svg {...svgProps(size, title)} style={style}>
      {title ? <title>{title}</title> : null}
      <path d="M440-160v-487L216-423l-56-57 320-320 320 320-56 57-224-224v487h-80Z" />
    </svg>
  );
}

export function ArrowDownwardIcon({ size = 14, title, style }: IconProps) {
  return (
    <svg {...svgProps(size, title)} style={style}>
      {title ? <title>{title}</title> : null}
      <path d="M480-160 160-480l56-57 224 224v-487h80v487l224-224 56 57-320 320Z" />
    </svg>
  );
}
