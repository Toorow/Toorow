/**
 * Surfaces — the boxes every screen is made of.
 *
 * These exist because the same box was written 61 times under 61 names:
 * `.dq-panel`, `.render-panel`, `.kg-panel`, `.mapping-panel`. A class can be
 * copied; a component has to be imported, which is what makes reuse hold.
 *
 * Every component forwards `className` and its native props, so a screen can
 * extend without forking. Nothing here encodes a screen-specific decision.
 */
import type { ReactNode, HTMLAttributes } from "react";
import { cn } from "../lib/cn";

export type PanelProps = HTMLAttributes<HTMLDivElement> & {
  /** Removes the inner padding for panels whose child is a full-bleed table. */
  flush?: boolean;
};

/**
 * `application-v3.css` l. 275: `.panel { overflow: hidden; background: var(--surface);
 * border: 1px solid var(--line); border-radius: 16px }` — the card radius from
 * `design-direction-foundation.md`, not the 12px field radius. `overflow-hidden`
 * is part of the mockup: it is what lets a full-bleed table sit inside the
 * rounded corners without clipping itself.
 */
export function Panel({ flush, className, ...rest }: PanelProps) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-large border border-divider-base bg-surface-light shadow-card-light",
        !flush && "p-card-padding",
        className,
      )}
      {...rest}
    />
  );
}

export type PanelBodyProps = HTMLAttributes<HTMLDivElement> & {
  /** Removes standard padding for full-bleed content like tables inside a Panel. */
  flush?: boolean;
};

/**
 * Standard body container inside a Panel. Use under PanelHeader inside `<Panel flush>`
 * to guarantee exact p-card-padding without breaking full-bleed headers.
 */
export function PanelBody({ flush, className, ...rest }: PanelBodyProps) {
  return (
    <div
      className={cn(
        !flush && "p-card-padding",
        className,
      )}
      {...rest}
    />
  );
}

export const PanelContent = PanelBody;

export type PanelHeaderProps = {
  title: ReactNode;
  /** One line of context. Never a second sentence — it becomes a paragraph. */
  description?: ReactNode;
  /** Actions sit at the trailing edge, vertically centred on the title. */
  actions?: ReactNode;
  className?: string;
};

/**
 * The band at the top of a panel — `application-v3.css` l. 262:
 *
 *     .section-header { min-height: 58px; padding: 0 20px;
 *                       border-bottom: 1px solid var(--line) }
 *     .section-header h2 { font: 600 20px Lexend }
 *     .section-header p  { margin: 4px 0 0; color: var(--muted); font-size: 13px }
 *
 * The mockup calls it `.section-header`; here it is `PanelHeader`, because it
 * only ever appears as the head of a `Panel` and `SectionHeader` below is a
 * different, lighter object. Use it inside `<Panel flush>` — the band brings
 * its own padding, so a padded panel would double it.
 */
export function PanelHeader({ title, description, actions, className }: PanelHeaderProps) {
  return (
    <div
      className={cn(
        "flex min-h-[58px] items-center justify-between gap-4 border-b border-divider-base px-5 py-3",
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="m-0 font-display text-h2 font-h2 text-text">{title}</h2>
        {description && (
          <p className="mt-1 mb-0 text-label font-normal text-text-secondary">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

export type PageHeaderProps = PanelHeaderProps & {
  /** Rendered above the title: breadcrumb, back link, or object kind. */
  eyebrow?: ReactNode;
  /**
   * The screen's `StatusLegend`, when it draws three tones or more
   * (`console-presentation.md` §3: « mounts it once, in its `PageHeader` »).
   *
   * A PROP AND NOT THE `actions` SLOT, which is where the first attempt would
   * put it: `actions` is for controls, it is right-aligned beside the title and
   * it is `shrink-0`, so a five-entry key there would either squeeze the title
   * or wrap into a column of marks. A key belongs under the sentence it
   * explains, at the width of that sentence. One slot, so six screens cannot
   * mount it six places.
   */
  legend?: ReactNode;
  /** On the `h1` itself, so a region can be `aria-labelledby` it. */
  id?: string;
};

/**
 * `application-v3.css` l. 241: `.page-header { margin-bottom: 28px; gap: 24px }`,
 * `h1 { font: 30px/1.2 Lexend; letter-spacing: -.02em }`, `p { max-width: 700px;
 * font-size: 15px }`.
 *
 * One deliberate divergence: the mockup writes weight 600, the token file says
 * 800. The 800 is Jean's arbitration of 2026-07-24 — titles aligned on the
 * weight of the logo — and a ratified arbitration outranks the sheet the mock
 * was captured with.
 */
export function PageHeader({ eyebrow, title, description, actions, legend, className, id }: PageHeaderProps) {
  return (
    <header className={cn("mb-7 flex items-start justify-between gap-6", className)}>
      <div className="min-w-0">
        {eyebrow && (
          <div className="mb-1 text-caption text-text-secondary">{eyebrow}</div>
        )}
        <h1 id={id} className="m-0 font-display text-h1 font-h1 tracking-[var(--h1-letter-spacing)] text-text">
          {title}
        </h1>
        {description && (
          <p className="mt-2 mb-0 max-w-[70ch] text-body text-text-secondary">{description}</p>
        )}
        {legend && <div className="mt-3">{legend}</div>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

export type SectionHeaderProps = PanelHeaderProps & {
  /**
   * The heading rank this section holds in the page outline. `3` by default —
   * a section of a panel, under the page's `PageHeader` `h1` and whatever `h2`
   * the screen puts between them. A screen whose section IS the level under the
   * page title passes `2` rather than writing its own `<h2>`: two ways to title
   * one screen is how an outline stops being one (story 57.5).
   */
  level?: 2 | 3;
  /** On the HEADING itself, so a region can be `aria-labelledby` it. */
  id?: string;
};

/**
 * A grouping heading *inside* a panel body — the mockup's `.drawer-section h3`
 * (`application-v3.css` l. 454: `margin: 0 0 10px; font-size: 14px`). No band,
 * no divider: it separates two paragraphs of the same panel, where
 * `PanelHeader` separates the panel from the page.
 */
export function SectionHeader({
  title, description, actions, className, level = 3, id,
}: SectionHeaderProps) {
  const Heading = level === 2 ? "h2" : "h3";
  // The rank sets the size. `level` used to change only the TAG, so a section
  // that is the level under the page title rendered an `h2` at the h3 step —
  // the same 16px as the card titles beneath it, which is no hierarchy at all.
  const step = level === 2 ? "text-h2 font-h2" : "text-h3 font-h3";
  return (
    <div className={cn("flex items-baseline justify-between gap-4", className)}>
      <div className="min-w-0">
        <Heading id={id} className={cn("m-0 text-text", step)}>{title}</Heading>
        {description && (
          <p className="mt-1 mb-0 text-label font-normal text-text-secondary">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/**
 * The content column a page opens with — width only, never padding.
 *
 * `application-v3.css` l. 236: `.main { padding: 32px 40px 40px }`. That gutter
 * belongs to the shell's `<main>`, which every route already renders inside.
 * Four screens declared their own on top of it — `p-6 lg:p-8` on Project
 * Overview, `p-2 sm:p-4` on Platform Clocks — so the same console showed a 32px,
 * a 48px and a 64px gutter depending on the route, and two of them opened a
 * SECOND `<main>` inside the shell's to do it. A page states the column; the
 * frame states the gutter, once, in `--spacing-page-gutter`.
 */
export function PageFrame({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mx-auto w-full max-w-[1440px]", className)} {...rest} />;
}

/** Vertical rhythm between the stacked sections of a page. */
export function Stack({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-section-gap", className)} {...rest} />;
}

/** A horizontal run of items that wraps rather than overflowing. */
export function Cluster({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-wrap items-center gap-2", className)} {...rest} />;
}
