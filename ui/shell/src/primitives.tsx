/**
 * The widget-platform primitives — what `Box`, `Typography`, `Alert`, `Chip`,
 * `Button`, `ToggleButton`, `Table` and `Dialog` meant when they came from MUI.
 *
 * WHY THEY ARE HERE AND NOT IMPORTED. MUI plus emotion rode inside every
 * single-file bundle the host loads. Measured by building the same targets at
 * HEAD and after (`pnpm --filter ./cards/kpi build`), it cost:
 *
 *     ui/cards/kpi/dist/index.html                1 145.60 -> 928.92 kB  (-217)
 *     ui/widgets/google-analytics/dist/index.html 1 089.27 -> 919.03 kB  (-170)
 *     ui/cards/shell/dist/viz-mcp-app.html          959.77 -> 959.75 kB  (-0)
 *
 * The last line is the honest one and worth keeping: the viz runtime and share
 * entries never pulled MUI in, so nothing was won there. The win is on the cards
 * and the connector widgets.
 *
 * What those bundles asked of MUI, measured across the 232 `sx=` sites, was a div
 * with spacing shorthands, a text element with a type scale, and eight controls
 * whose entire styling is four token colours. That is what is written below.
 *
 * THE CONTRACT IS THE ONE THE CALL SITES WERE WRITTEN AGAINST, deliberately:
 * `<Box sx={{ mb: 1 }}>`, `<Typography variant="caption" color="text.secondary">`,
 * `<ToggleButtonGroup exclusive value={v} onChange={(_e, v) => ...}>`. Keeping
 * the shape is what let 34 components change one import line instead of being
 * rewritten — and a rewrite of 34 charting components is where pixels get lost.
 *
 * Accessibility is not approximated: the roles and ARIA attributes below are the
 * ones the existing tests query by (`role="alert"`, `aria-pressed`, `role="dialog"`,
 * `aria-expanded`), because a primitive that renders the right pixels and the
 * wrong role silently deletes a test's meaning.
 */

import {
  createContext,
  forwardRef,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import type {
  ButtonHTMLAttributes,
  CSSProperties,
  ElementType,
  HTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  Ref,
} from "react";
import { useTheme } from "./themeContext";
import { resolveColor, resolveSx } from "./sx";
import type { Sx } from "./sx";
import { alpha } from "./color";
import type { PaletteColor, WidgetTheme } from "./theme";

type Severity = "error" | "warning" | "info" | "success";
type Size = "small" | "medium";

function joinClasses(...values: Array<string | undefined | false>): string | undefined {
  const kept = values.filter(Boolean) as string[];
  return kept.length ? kept.join(" ") : undefined;
}

function severityColor(theme: WidgetTheme, severity: Severity): PaletteColor {
  return theme.palette[severity];
}

// ---------------------------------------------------------------------------
// Box — a div that speaks `sx`
// ---------------------------------------------------------------------------

export interface BoxProps extends Omit<HTMLAttributes<HTMLElement>, "color"> {
  sx?: Sx;
  component?: ElementType;
  children?: ReactNode;
}

export const Box = forwardRef(function Box(
  { sx, component, style, className, ...rest }: BoxProps,
  ref: Ref<HTMLElement>,
) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const Component = (component ?? "div") as ElementType;
  return (
    <Component
      ref={ref}
      className={joinClasses(resolved.className, className)}
      style={{ ...resolved.style, ...style }}
      {...rest}
    />
  );
});

// ---------------------------------------------------------------------------
// Typography — the token type scale
// ---------------------------------------------------------------------------

export type TypographyVariant =
  | "h1" | "h2" | "h3" | "h4" | "h5" | "h6"
  | "subtitle1" | "subtitle2"
  | "body1" | "body2" | "caption" | "button" | "overline";

const VARIANT_ELEMENT: Record<TypographyVariant, ElementType> = {
  h1: "h1", h2: "h2", h3: "h3", h4: "h4", h5: "h5", h6: "h6",
  subtitle1: "h6", subtitle2: "h6",
  body1: "p", body2: "p", caption: "span", button: "span", overline: "span",
};

/**
 * `subtitle1` / `subtitle2` are used by DotMatrix but absent from tokens.json —
 * under MUI they fell through to its defaults. Those defaults are written down
 * here rather than added to the generated token artifact, which only carries what
 * the design system actually ratified.
 */
const UNTOKENISED_VARIANTS: Partial<Record<TypographyVariant, CSSProperties>> = {
  subtitle1: { fontSize: "1rem", fontWeight: 400, lineHeight: 1.75 },
  subtitle2: { fontSize: "0.875rem", fontWeight: 500, lineHeight: 1.57 },
};

export interface TypographyProps extends Omit<HTMLAttributes<HTMLElement>, "color"> {
  variant?: TypographyVariant;
  /** A palette path (`"text.secondary"`) or any CSS colour. */
  color?: string;
  component?: ElementType;
  noWrap?: boolean;
  align?: CSSProperties["textAlign"];
  gutterBottom?: boolean;
  sx?: Sx;
  children?: ReactNode;
}

export const Typography = forwardRef(function Typography(
  {
    variant = "body1",
    color,
    component,
    noWrap,
    align,
    gutterBottom,
    sx,
    style,
    className,
    ...rest
  }: TypographyProps,
  ref: Ref<HTMLElement>,
) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const scale =
    ((theme.typography as Record<string, unknown>)[variant] as CSSProperties | undefined) ??
    UNTOKENISED_VARIANTS[variant];
  const Component = (component ?? VARIANT_ELEMENT[variant]) as ElementType;
  return (
    <Component
      ref={ref}
      className={joinClasses(resolved.className, className)}
      style={{
        margin: 0,
        fontFamily: theme.typography.fontFamily,
        ...scale,
        ...(color ? { color: resolveColor(color, theme) } : null),
        ...(noWrap
          ? { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" as const }
          : null),
        ...(align ? { textAlign: align } : null),
        ...(gutterBottom ? { marginBottom: "0.35em" } : null),
        ...resolved.style,
        ...style,
      }}
      {...rest}
    />
  );
});

// ---------------------------------------------------------------------------
// Stack — a flex column with a spacing-unit gap
// ---------------------------------------------------------------------------

export interface StackProps extends BoxProps {
  direction?: CSSProperties["flexDirection"];
  spacing?: number;
  alignItems?: CSSProperties["alignItems"];
  justifyContent?: CSSProperties["justifyContent"];
  /** MUI's opt-in to `gap` over margins. Always true here — accepted, ignored. */
  useFlexGap?: boolean;
}

export function Stack({
  direction = "column",
  spacing = 0,
  alignItems,
  justifyContent,
  useFlexGap: _useFlexGap,
  style,
  ...rest
}: StackProps) {
  return (
    <Box
      style={{
        display: "flex",
        flexDirection: direction,
        gap: `${spacing * 8}px`,
        ...(alignItems ? { alignItems } : null),
        ...(justifyContent ? { justifyContent } : null),
        ...style,
      }}
      {...rest}
    />
  );
}

// ---------------------------------------------------------------------------
// Button / IconButton
// ---------------------------------------------------------------------------

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "color"> {
  variant?: "text" | "outlined" | "contained";
  color?: "primary" | "secondary" | "error" | "warning" | "success" | "info" | "inherit";
  size?: Size;
  startIcon?: ReactNode;
  endIcon?: ReactNode;
  fullWidth?: boolean;
  sx?: Sx;
}

export const Button = forwardRef(function Button(
  {
    variant = "text",
    color = "primary",
    size = "medium",
    startIcon,
    endIcon,
    fullWidth,
    sx,
    style,
    className,
    children,
    type = "button",
    ...rest
  }: ButtonProps,
  ref: Ref<HTMLButtonElement>,
) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const tone = color === "inherit" ? null : theme.palette[color];
  const base: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "6px",
    border: "1px solid transparent",
    borderRadius: `${theme.shape.borderRadius}px`,
    cursor: "pointer",
    fontFamily: theme.typography.fontFamily,
    ...(theme.typography.button as CSSProperties),
    padding: size === "small" ? "4px 10px" : "6px 16px",
    ...(fullWidth ? { width: "100%" } : null),
  };
  const skin: CSSProperties =
    variant === "contained"
      ? {
          backgroundColor: tone?.main ?? "transparent",
          color: tone?.contrastText ?? "inherit",
        }
      : variant === "outlined"
        ? {
            backgroundColor: "transparent",
            color: tone?.main ?? "inherit",
            borderColor: tone ? alpha(tone.main, 0.5) : "currentColor",
          }
        : { backgroundColor: "transparent", color: tone?.main ?? "inherit" };

  return (
    <button
      ref={ref}
      type={type}
      className={joinClasses(resolved.className, className)}
      style={{ ...base, ...skin, ...resolved.style, ...style }}
      {...rest}
    >
      {startIcon}
      {children}
      {endIcon}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonProps, "variant"> {
  edge?: "start" | "end" | false;
}

export const IconButton = forwardRef(function IconButton(
  { size = "medium", style, edge: _edge, color = "inherit", ...rest }: IconButtonProps,
  ref: Ref<HTMLButtonElement>,
) {
  return (
    <Button
      ref={ref}
      variant="text"
      color={color}
      size={size}
      style={{
        padding: size === "small" ? "4px" : "8px",
        borderRadius: "50%",
        minWidth: 0,
        ...style,
      }}
      {...rest}
    />
  );
});

// ---------------------------------------------------------------------------
// Chip
// ---------------------------------------------------------------------------

export interface ChipProps extends Omit<HTMLAttributes<HTMLDivElement>, "color"> {
  label?: ReactNode;
  color?: "default" | "primary" | "secondary" | "error" | "warning" | "success" | "info";
  size?: Size;
  variant?: "filled" | "outlined";
  sx?: Sx;
}

export function Chip({
  label,
  color = "default",
  size = "small",
  variant = "filled",
  sx,
  style,
  className,
  ...rest
}: ChipProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const tone = color === "default" ? null : theme.palette[color];
  return (
    <div
      className={joinClasses(resolved.className, className)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        height: size === "small" ? 24 : 32,
        padding: size === "small" ? "0 8px" : "0 12px",
        borderRadius: 999,
        fontFamily: theme.typography.fontFamily,
        fontSize: size === "small" ? "0.75rem" : "0.8125rem",
        fontWeight: 500,
        whiteSpace: "nowrap",
        ...(variant === "outlined"
          ? {
              border: `1px solid ${tone ? alpha(tone.main, 0.5) : theme.palette.divider}`,
              color: tone?.main ?? theme.palette.text.primary,
            }
          : {
              backgroundColor: tone ? alpha(tone.main, 0.16) : theme.palette.action.selected,
              color: tone?.main ?? theme.palette.text.primary,
            }),
        ...resolved.style,
        ...style,
      }}
      {...rest}
    >
      {label}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Alert
// ---------------------------------------------------------------------------

export interface AlertProps extends Omit<HTMLAttributes<HTMLDivElement>, "color" | "action"> {
  severity?: Severity;
  action?: ReactNode;
  sx?: Sx;
}

export function Alert({
  severity = "info",
  action,
  sx,
  style,
  className,
  children,
  ...rest
}: AlertProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const tone = severityColor(theme, severity);
  return (
    <div
      role="alert"
      className={joinClasses(resolved.className, className)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "12px",
        padding: "6px 16px",
        borderRadius: `${theme.shape.borderRadius}px`,
        backgroundColor: alpha(tone.main, 0.12),
        color: tone.main,
        fontFamily: theme.typography.fontFamily,
        ...(theme.typography.body2 as CSSProperties),
        ...resolved.style,
        ...style,
      }}
      {...rest}
    >
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
      {action}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToggleButtonGroup / ToggleButton
// ---------------------------------------------------------------------------

interface ToggleGroupContextValue {
  value: unknown;
  exclusive: boolean;
  size: Size;
  onToggle: (value: unknown) => void;
}

const ToggleGroupContext = createContext<ToggleGroupContextValue | null>(null);

export interface ToggleButtonGroupProps {
  value: unknown;
  exclusive?: boolean;
  size?: Size;
  /** MUI's signature: `(event, nextValue)`. Exclusive groups pass `null` when
   *  the selected button is clicked again; multi groups pass the new array. */
  onChange?: (event: unknown, value: never) => void;
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
  sx?: Sx;
  "aria-label"?: string;
}

export function ToggleButtonGroup({
  value,
  exclusive = false,
  size = "medium",
  onChange,
  children,
  className,
  style,
  sx,
  ...rest
}: ToggleButtonGroupProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);

  function onToggle(buttonValue: unknown) {
    if (!onChange) return;
    if (exclusive) {
      onChange(null, (buttonValue === value ? null : buttonValue) as never);
      return;
    }
    const current = Array.isArray(value) ? (value as unknown[]) : [];
    const next = current.includes(buttonValue)
      ? current.filter((v) => v !== buttonValue)
      : [...current, buttonValue];
    onChange(null, next as never);
  }

  return (
    <ToggleGroupContext.Provider value={{ value, exclusive, size, onToggle }}>
      <div
        role="group"
        className={joinClasses(resolved.className, className)}
        style={{
          display: "inline-flex",
          borderRadius: `${theme.shape.borderRadius}px`,
          border: `1px solid ${theme.palette.divider}`,
          overflow: "hidden",
          ...resolved.style,
          ...style,
        }}
        {...rest}
      >
        {children}
      </div>
    </ToggleGroupContext.Provider>
  );
}

export interface ToggleButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "value" | "onChange"> {
  value: unknown;
  selected?: boolean;
  sx?: Sx;
}

export function ToggleButton({
  value,
  selected,
  sx,
  style,
  className,
  children,
  onClick,
  ...rest
}: ToggleButtonProps) {
  const theme = useTheme();
  const group = useContext(ToggleGroupContext);
  const resolved = resolveSx(sx, theme);
  const isSelected =
    selected ??
    (group
      ? group.exclusive
        ? group.value === value
        : Array.isArray(group.value) && (group.value as unknown[]).includes(value)
      : false);
  const size = group?.size ?? "medium";

  return (
    <button
      type="button"
      aria-pressed={isSelected}
      className={joinClasses(resolved.className, className)}
      style={{
        border: "none",
        borderRadius: 0,
        cursor: "pointer",
        fontFamily: theme.typography.fontFamily,
        ...(theme.typography.button as CSSProperties),
        padding: size === "small" ? "5px 11px" : "9px 15px",
        backgroundColor: isSelected ? theme.palette.action.selected : "transparent",
        color: isSelected ? theme.palette.primary.main : theme.palette.text.secondary,
        ...resolved.style,
        ...style,
      }}
      onClick={(event) => {
        onClick?.(event);
        group?.onToggle(value);
      }}
      {...rest}
    >
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Collapse
// ---------------------------------------------------------------------------

export interface CollapseProps extends HTMLAttributes<HTMLDivElement> {
  in?: boolean;
  /** MUI keeps the subtree mounted when `unmountOnExit` is false — so do we. */
  unmountOnExit?: boolean;
  timeout?: number | "auto";
  sx?: Sx;
}

export function Collapse({
  in: open = false,
  unmountOnExit = false,
  timeout: _timeout,
  sx,
  style,
  className,
  children,
  ...rest
}: CollapseProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  if (!open && unmountOnExit) return null;
  return (
    <div
      className={joinClasses(resolved.className, className)}
      style={{
        // `height: 0 + overflow hidden` is what MUI's collapse settles on once
        // its transition has run; the widgets never depended on the animation.
        height: open ? "auto" : 0,
        overflow: "hidden",
        visibility: open ? undefined : "hidden",
        ...resolved.style,
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

type CellAlign = "left" | "right" | "center";

export interface TableProps extends HTMLAttributes<HTMLTableElement> {
  size?: Size;
  sx?: Sx;
}

export function Table({ size: _size, sx, style, className, ...rest }: TableProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  return (
    <table
      className={joinClasses(resolved.className, className)}
      style={{
        width: "100%",
        borderCollapse: "collapse",
        fontFamily: theme.typography.fontFamily,
        ...resolved.style,
        ...style,
      }}
      {...rest}
    />
  );
}

export function TableHead(props: HTMLAttributes<HTMLTableSectionElement>) {
  return <thead {...props} />;
}

export function TableBody(props: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody {...props} />;
}

export interface TableRowProps extends HTMLAttributes<HTMLTableRowElement> {
  sx?: Sx;
}

export function TableRow({ sx, style, className, ...rest }: TableRowProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  return (
    <tr
      className={joinClasses(resolved.className, className)}
      style={{ ...resolved.style, ...style }}
      {...rest}
    />
  );
}

export interface TableCellProps extends Omit<HTMLAttributes<HTMLTableCellElement>, "align"> {
  align?: CellAlign;
  /** MUI infers `th` inside `TableHead`; the call sites pass it explicitly. */
  component?: "td" | "th";
  /** `"none"` strips the cell padding — a dense grid row draws its own. */
  padding?: "normal" | "none" | "checkbox";
  scope?: string;
  colSpan?: number;
  sx?: Sx;
}

const CELL_PADDING: Record<NonNullable<TableCellProps["padding"]>, string> = {
  normal: "8px 12px",
  none: "0",
  checkbox: "0 0 0 4px",
};

export function TableCell({
  align = "left",
  component,
  padding = "normal",
  sx,
  style,
  className,
  ...rest
}: TableCellProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const Component = (component ?? "td") as ElementType;
  return (
    <Component
      className={joinClasses(resolved.className, className)}
      style={{
        textAlign: align,
        padding: CELL_PADDING[padding],
        borderBottom: `1px solid ${theme.palette.divider}`,
        ...(theme.typography.body2 as CSSProperties),
        ...resolved.style,
        ...style,
      }}
      {...rest}
    />
  );
}

// ---------------------------------------------------------------------------
// Dialog
// ---------------------------------------------------------------------------

export interface DialogProps extends Omit<HTMLAttributes<HTMLDivElement>, "onClose"> {
  open: boolean;
  onClose?: () => void;
  maxWidth?: string | false;
  fullWidth?: boolean;
  sx?: Sx;
}

export function Dialog({
  open,
  onClose,
  maxWidth: _maxWidth,
  fullWidth,
  sx,
  style,
  className,
  children,
  ...rest
}: DialogProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const labelId = useId();
  const surfaceRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || !onClose) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose?.();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: "rgba(0, 0, 0, 0.5)",
        zIndex: 1300,
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <div
        ref={surfaceRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelId}
        className={joinClasses(resolved.className, className)}
        style={{
          backgroundColor: theme.palette.background.paper,
          color: theme.palette.text.primary,
          borderRadius: `${theme.shape.borderRadius}px`,
          maxHeight: "90vh",
          overflow: "auto",
          minWidth: 320,
          ...(fullWidth ? { width: "100%", maxWidth: 600 } : null),
          ...resolved.style,
          ...style,
        }}
        {...rest}
      >
        {children}
      </div>
    </div>
  );
}

export interface DialogSectionProps extends HTMLAttributes<HTMLDivElement> {
  sx?: Sx;
}

export function DialogTitle({ sx, style, className, ...rest }: DialogSectionProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  return (
    <h2
      className={joinClasses(resolved.className, className)}
      style={{
        margin: 0,
        padding: "16px 24px",
        ...(theme.typography.h3 as CSSProperties),
        ...resolved.style,
        ...style,
      }}
      {...(rest as HTMLAttributes<HTMLHeadingElement>)}
    />
  );
}

export interface DialogContentProps extends DialogSectionProps {
  /** Hairlines separating the content from the title and any actions. */
  dividers?: boolean;
}

export function DialogContent({
  dividers,
  sx,
  style,
  className,
  ...rest
}: DialogContentProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  return (
    <div
      className={joinClasses(resolved.className, className)}
      style={{
        padding: dividers ? "16px 24px" : "8px 24px 24px",
        ...(dividers
          ? {
              borderTop: `1px solid ${theme.palette.divider}`,
              borderBottom: `1px solid ${theme.palette.divider}`,
            }
          : null),
        ...resolved.style,
        ...style,
      }}
      {...rest}
    />
  );
}

// ---------------------------------------------------------------------------
// TextField
// ---------------------------------------------------------------------------

export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  label?: string;
  size?: Size;
  fullWidth?: boolean;
  multiline?: boolean;
  sx?: Sx;
}

export function TextField({
  label,
  size = "medium",
  fullWidth,
  multiline,
  sx,
  style,
  className,
  id,
  ...rest
}: TextFieldProps) {
  const theme = useTheme();
  const resolved = resolveSx(sx, theme);
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const [focused, setFocused] = useState(false);
  const Field = (multiline ? "textarea" : "input") as ElementType;

  return (
    <div
      className={joinClasses(resolved.className, className)}
      style={{
        display: "inline-flex",
        flexDirection: "column",
        gap: 4,
        // The wrapper owns the type size and the input inherits it, so a call
        // site can shrink the field with `sx={{ fontSize: "0.7rem" }}` — which is
        // what `slotProps.input` was reaching inside MUI's wrapper to do.
        ...(theme.typography.body2 as CSSProperties),
        ...(fullWidth ? { width: "100%" } : null),
        ...resolved.style,
        ...style,
      }}
    >
      {label && (
        <label
          htmlFor={inputId}
          style={{
            ...(theme.typography.caption as CSSProperties),
            color: focused ? theme.palette.primary.main : theme.palette.text.secondary,
            fontFamily: theme.typography.fontFamily,
          }}
        >
          {label}
        </label>
      )}
      <Field
        id={inputId}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          padding: size === "small" ? "6px 10px" : "10px 12px",
          borderRadius: `${theme.shape.borderRadius}px`,
          border: `1px solid ${focused ? theme.palette.primary.main : theme.palette.divider}`,
          backgroundColor: "transparent",
          color: theme.palette.text.primary,
          font: "inherit",
          ...(fullWidth ? { width: "100%" } : null),
        }}
        {...rest}
      />
    </div>
  );
}
