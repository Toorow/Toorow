/**
 * ConnectorLogo — the ONE shared connector-icon component. Resolves brand marks
 * through the managed connector-logo registry: the canonical source of truth is
 * `web/src/data/connector-identities.json` + `web/public/connectors/`, projected
 * into the admin front by `scripts/sync_connector_logos.py` as the generated
 * `connector-logos.json` map (slug -> /connectors/<file>, keyed by both module id
 * and file stem, correct extension). Never hand-copy provider assets or guess a
 * `.svg` path — creating a connector registers its logo once in the canonical
 * source and the sync propagates it here. Unknown providers fall back to
 * `generic.svg`. Brand marks sit on a fixed light chip (`.provider-logo`) so both
 * colored and monochrome logos read in light AND dark.
 */
import type { ReactNode } from "react";
import logoMap from "../generated/connector-logos.json";

const GENERIC = "/connectors/generic.svg";
const LOGOS = (logoMap as { logos: Record<string, string> }).logos;

/** Resolve a provider id/slug to its canonical asset path (generic if unknown).
 * Normalizes spaces AND underscores to dashes so a caller can pass a dash-kebab
 * module id (`meta-ads`), an underscore source_kind (`google_analytics`), or a
 * display slug — all resolve to the same identity key. */
export function connectorSrc(provider: string): string {
  const slug = provider.trim().toLowerCase().replace(/[\s_]+/g, "-");
  return (slug && LOGOS[slug]) || GENERIC;
}

export default function ConnectorLogo({
  provider,
  alt,
  className = "provider-logo",
  extra,
}: {
  provider: string;
  alt?: string;
  className?: string;
  extra?: ReactNode;
}) {
  return (
    <span className={className}>
      <img
        src={connectorSrc(provider)}
        alt={alt ?? provider}
        loading="lazy"
        onError={(e) => {
          const img = e.currentTarget;
          if (!img.src.endsWith("generic.svg")) img.src = GENERIC;
        }}
      />
      {extra}
    </span>
  );
}
