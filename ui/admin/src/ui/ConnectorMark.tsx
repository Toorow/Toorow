/**
 * ConnectorMark — a provider's brand mark on the standard chip.
 *
 * The asset is NEVER chosen by the calling screen. It is resolved through the
 * managed registry: `web/src/data/connector-identities.json` is the canonical
 * source, `scripts/sync_connector_logos.py` projects it into
 * `generated/connector-logos.json`, and every one of the 37 registered connectors
 * resolves through it. An unknown provider gets `generic.svg` — never a
 * letter, never a blank.
 *
 * This replaces per-screen maps. `DatastreamOverview` carried a hand-written
 * table of five providers, so the other thirty-two silently fell back to an
 * initial; the same table was one edit away from appearing in the next screen.
 * A screen that needs a logo names the provider and nothing else.
 *
 * The chip itself is the library's: `rounded-control`, a quiet border and the
 * light surface, so a coloured logo and a monochrome one both read in light
 * and dark. The legacy `.provider-logo` class did the same job for screens not
 * yet migrated; no `.tsx` sets it any more.
 */
import logoMap from "../generated/connector-logos.json";
import { cn } from "../lib/cn";

const GENERIC = "/connectors/generic.svg";
const LOGOS = (logoMap as { logos: Record<string, string> }).logos;
const NAMES = (logoMap as { names?: Record<string, string> }).names ?? {};

/**
 * Resolve a provider id to its canonical asset path.
 *
 * Spaces and underscores normalise to dashes, so a dash-kebab module id
 * (`meta-ads`), an underscore `source_kind` (`google_analytics`) and a display
 * slug all land on the same identity key.
 *
 * THE ONE resolver, and now the only one: `shell/ConnectorLogo` re-exported it
 * and was deleted on 2026-08-05 with its last caller. Two copies — two resolvers would drift the first time a connector is
 * added, and the one that drifts is the one nobody is looking at.
 */
export function connectorSrc(provider: string | null | undefined): string {
  const slug = (provider ?? "").trim().toLowerCase().replace(/[\s_]+/g, "-");
  return (slug && LOGOS[slug]) || GENERIC;
}

/**
 * The provider's public name — "Google Analytics 4", not `ga4`.
 *
 * From the same registry entry as the logo, so a screen cannot show one
 * vendor's mark beside another's label. Falls back to the raw string, which is
 * information, rather than to a guess.
 */
export function connectorName(provider: string | null | undefined): string {
  const raw = (provider ?? "").trim();
  const slug = raw.toLowerCase().replace(/[\s_]+/g, "-");
  return NAMES[slug] ?? raw;
}

export function ConnectorMark({
  provider,
  alt,
  size = 34,
  className,
}: {
  /** A module id, a `source_kind`, or a display slug — all resolve alike. */
  provider: string | null | undefined;
  /** Say what it is when the mark carries meaning; omit when it is decorative
   *  beside a name that already says the same thing. */
  alt?: string;
  size?: number;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "grid shrink-0 place-items-center overflow-hidden rounded-control border border-divider-base bg-surface-light",
        className,
      )}
      style={{ width: size, height: size }}
    >
      <img
        className="size-[65%] object-contain"
        src={connectorSrc(provider)}
        alt={alt ?? ""}
        aria-hidden={alt ? undefined : true}
        loading="lazy"
        onError={(event) => {
          const img = event.currentTarget;
          if (!img.src.endsWith("generic.svg")) img.src = GENERIC;
        }}
      />
    </span>
  );
}
