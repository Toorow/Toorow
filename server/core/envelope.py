"""toorow — canonical AD-1 envelope builder (Story 1.5, T4).

Builds the structuredContent envelope consumed by the shared ui/shell.

# AD-1: canonical envelope shape — {schema_version, meta: {freshness, provenance, alerts[]}, data}
# AD-2: source-agnostic — no module-specific strings here.
"""

from __future__ import annotations

# Canonical schema version for the Story 1.5 envelope.
# NOTE: main.py uses "1.0.0" for the health/list_modules envelope;
# the Story 1.5 spec (AC3) mandates "1" for the cross-connector data envelope.
# Both are valid per Architecture Spine — schema_version tracks the envelope
# contract, not a semver of the platform.
_SCHEMA_VERSION = "1"


def build_canonical_envelope(
    rows: list[dict],
    meta_freshness: dict,
    meta_provenance: list[dict] | dict,
    date_range: dict,
    connectors: list[str],
    report_profile: str = "standard_daily",
    confidence: dict | None = None,
    branding: dict | None = None,
) -> dict:
    """Build the canonical AD-1 structuredContent envelope.

    Parameters
    ----------
    rows:
        Full dataset (all fact_daily_kpi rows for the query scope).
    meta_freshness:
        Dict: ``{"last_pull": <ISO str>, "cadence_hours": 24, "stale_since": null}``
    meta_provenance:
        Dict or list[dict] for multi-connector responses.
        Shape: ``{"source_system": str, "pull_id": str}``
        Typed as ``list[dict]`` to support the future multi-connector extension
        (Architecture Spine §Canonical Envelope note).
    date_range:
        Dict with ``start`` and ``end`` ISO-8601 date strings.
    connectors:
        List of connector names covered by this report.
    report_profile:
        Widget report profile identifier (default: ``"standard_daily"``).
    confidence:
        Optional confidence dict for AD-9 groundwork (Story 3.5).
        When provided, set as ``meta["confidence"]``.  When None, the key is
        omitted entirely (AD-1 additive key; existing callers unaffected, HG-6).
        Shape: ``{"completeness": float}``  (freshness/provenance terms deferred
        to Story 4.6).
    branding:
        Optional org branding dict from ``core.branding.resolve_org_branding``
        (Story 23.1, AI-31 additive key). When provided, set as
        ``meta["branding"]`` so the widget shell merges the org colors into its
        ThemeProvider. When None the key is OMITTED entirely — the widget
        renders the default toorow theme (never ``null``, AC1).

    Returns
    -------
    dict
        Canonical envelope matching the AD-1 contract exactly.
    """
    meta: dict = {
        "freshness": meta_freshness,
        "provenance": meta_provenance,
        "alerts": [],  # always a list, never null (Architecture Spine)
    }
    if confidence is not None:
        meta["confidence"] = confidence
    if branding is not None:
        meta["branding"] = branding

    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": meta,
        "data": {
            "report_profile": report_profile,
            "date_range": date_range,
            "connectors": connectors,
            "rows": rows,
        },
    }


def derive_meta_from_rows(rows: list[dict], connectors: list[str]) -> tuple[dict, list[dict]]:
    """Derive freshness and provenance from raw fact_daily_kpi rows.

    Returns (meta_freshness, meta_provenance_list).

    meta_freshness shape:
        {"last_pull": <ISO str | None>, "cadence_hours": 24, "stale_since": null}

    meta_provenance_list shape (one entry per connector):
        [{"source_system": str, "pull_id": str}]
    """
    # Aggregate per connector
    pull_ids: dict[str, str] = {}
    freshness_timestamps: dict[str, str] = {}

    for row in rows:
        connector = row.get("connector") or "unknown"
        pull_id = row.get("pull_id") or ""
        loaded_at = str(row.get("loaded_at") or "")

        if pull_id > pull_ids.get(connector, ""):
            pull_ids[connector] = pull_id
        if loaded_at > freshness_timestamps.get(connector, ""):
            freshness_timestamps[connector] = loaded_at

    # Global freshness — latest across all connectors
    all_timestamps = [v for v in freshness_timestamps.values() if v]
    last_pull = max(all_timestamps) if all_timestamps else None

    meta_freshness = {
        "last_pull": last_pull,
        "cadence_hours": 24,
        "stale_since": None,
    }

    # Per-connector provenance list (NFR8 AC5 Story 2.7: include source_field)
    covered = connectors if connectors else sorted(pull_ids.keys())
    meta_provenance: list[dict] = [
        {
            "source_system": connector,
            "source_field": "fact_daily_kpi",
            "pull_id": pull_ids.get(connector),
        }
        for connector in covered
    ]

    return meta_freshness, meta_provenance
