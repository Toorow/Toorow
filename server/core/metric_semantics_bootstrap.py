"""toorow -- Bootstrap source_metric_mappings from module manifests (Story 27.2).

Reads each server/modules/*/manifest.json as DATA (json.load, no module import -- AD-2)
and proposes PROPOSED source_metric_mappings rows for an org.  Idempotent: never
overwrites a confirmed/renamed/rejected mapping (human curation is sacred).

TRUST CONTRACT (S-5, lesson 27.2): this bootstrap WRITES rows WITHOUT any authorization
guard -- it trusts its caller entirely. It MUST be exposed ONLY behind a manage-level guard
(org-manage: owner/admin), never on a read surface. Any surface that triggers a bootstrap
must check org-manage on the target org FIRST.

Design mirrors account_topology.py and metric_semantics.py:
  - ``from __future__ import annotations``
  - module logger
  - lazy ``core.db`` / ``core.metric_semantics`` imports inside functions
  - PURE functions kept separate from I/O (testable offline)
  - ASCII-only log strings
  - ZERO provider names hard-coded (AD-2: names come from directory names / manifests)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Statuses that represent a human curation decision -- bootstrap NEVER overwrites these.
_CURATED_STATUSES = frozenset({"confirmed", "renamed", "rejected"})


# ---------------------------------------------------------------------------
# A.1 Manifest reader (PURE, offline-testable).
# ---------------------------------------------------------------------------


def _default_modules_dir() -> Path:
    """server/modules relative to this file (server/core/metric_semantics_bootstrap.py).

    server/core/<this file> -> parents[1] = server -> / 'modules'.
    Tests override via the modules_dir argument.
    """
    return Path(__file__).resolve().parents[1] / "modules"


def read_manifest_metric_mappings(
    modules_dir: str | Path | None = None,
) -> list[dict]:
    """Scan modules_dir/*/manifest.json and return one entry PER (connector, source_field).

    PURE (no DB).  For each immediate subdirectory holding a manifest.json:
      connector = <dir name> (opaque, AD-2); for k, v in
      manifest.get('canonical_metric_mapping', {}).items():
        yield {'connector': connector, 'source_field': k, 'canonical_name': v}

    A directory without manifest.json, without canonical_metric_mapping, or with a
    non-dict mapping is skipped (not an error).  Deterministic order: dir name asc,
    then source_field asc (stable output for the bit-identical-ish bootstrap test).
    """
    base = Path(modules_dir) if modules_dir is not None else _default_modules_dir()
    entries: list[dict] = []

    # Sort subdirectories by name for deterministic order (AD-2: names from filesystem).
    try:
        subdirs = sorted(
            (d for d in base.iterdir() if d.is_dir()),
            key=lambda d: d.name,
        )
    except OSError as exc:
        logger.warning("bootstrap: cannot iterate modules_dir=%s: %s", base, exc)
        return entries

    for subdir in subdirs:
        manifest_path = subdir / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "bootstrap: skipping manifest connector=%s reason=%s", subdir.name, exc
            )
            continue

        mapping = manifest.get("canonical_metric_mapping")
        if not isinstance(mapping, dict):
            # Missing key, null value, or non-dict -> silently skip.
            continue

        connector = subdir.name  # opaque AD-2 identifier (kebab-case dir name)
        # Sort source_fields for deterministic order within a connector.
        for source_field in sorted(mapping.keys()):
            canonical_name = mapping[source_field]
            if not isinstance(canonical_name, str) or not canonical_name:
                continue
            entries.append(
                {
                    "connector": connector,
                    "source_field": source_field,
                    "canonical_name": canonical_name,
                }
            )

    return entries


# ---------------------------------------------------------------------------
# A.3 PLATFORM definition resolver (DB, lazy import).
# ---------------------------------------------------------------------------


def _platform_definition_id(canonical_name: str) -> str | None:
    """Return the PLATFORM metric_definition id for *canonical_name*, or None.

    Reads via metric_semantics.get_metric_definition(scope_level='PLATFORM',
    canonical_name=...).  None when the canonical metric has no PLATFORM definition
    (=> the manifest entry is skipped and reported, never invented).
    """
    from core.metric_semantics import (  # noqa: PLC0415
        SCOPE_PLATFORM,
        get_metric_definition,
    )

    row = get_metric_definition(scope_level=SCOPE_PLATFORM, canonical_name=canonical_name)
    if row is None:
        return None
    return row.get("id")


# ---------------------------------------------------------------------------
# A.2 Bootstrap -- propose ORG mappings from manifests (idempotent).
# ---------------------------------------------------------------------------


def bootstrap_org_source_mappings(
    *,
    org_id: str,
    connectors: list[str] | None = None,
    identity: str = "system",
    modules_dir: str | Path | None = None,
) -> dict:
    """Propose PROPOSED source_metric_mappings for *org_id* from the manifests.

    For each manifest entry (optionally filtered to *connectors*):
      * resolve the PLATFORM metric_definition id for entry.canonical_name (A.3);
        no definition => SKIP (counted), never invent one;
      * look up the existing ORG mapping at (scope=ORG, org_id, definition_id, connector);
      * IDEMPOTENCE RULE:
          - no existing row              -> upsert status='proposed'   (counted: proposed)
          - existing status='proposed'   -> re-upsert identical content -> NO audit
                                            (_mapping_changed guard)    (counted: unchanged)
          - existing status in {confirmed,renamed,rejected} -> LEAVE UNTOUCHED
                                            (human curation is sacred)         (counted: preserved)
    Returns a report: {'proposed': int, 'unchanged': int, 'preserved': int,
                       'skipped': int, 'connectors': [...] }.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.metric_semantics import (  # noqa: PLC0415
        SCOPE_ORG,
        _select_mapping,
        upsert_source_metric_mapping,
    )

    entries = read_manifest_metric_mappings(modules_dir)

    # Filter by connectors if provided.
    if connectors is not None:
        connector_set = set(connectors)
        entries = [e for e in entries if e["connector"] in connector_set]

    seen_connectors: list[str] = sorted({e["connector"] for e in entries})

    proposed = 0
    unchanged = 0
    preserved = 0
    skipped = 0

    for entry in entries:
        connector = entry["connector"]
        source_field = entry["source_field"]
        canonical_name = entry["canonical_name"]

        # Resolve the PLATFORM definition id.
        definition_id = _platform_definition_id(canonical_name)
        if definition_id is None:
            logger.debug(
                "bootstrap: no PLATFORM def canonical=%s connector=%s field=%s -- skipped",
                canonical_name,
                connector,
                source_field,
            )
            skipped += 1
            continue

        # Check the existing ORG-scope mapping.
        with get_connection() as conn:
            existing = _select_mapping(
                conn,
                scope_level=SCOPE_ORG,
                org_id=org_id,
                project_id=None,
                metric_definition_id=definition_id,
                connector=connector,
            )

        if existing is not None and existing.get("status") in _CURATED_STATUSES:
            # Human curation is sacred: never overwrite.
            preserved += 1
            logger.debug(
                "bootstrap: preserved curated mapping id=%s status=%s connector=%s field=%s",
                existing.get("id"),
                existing.get("status"),
                connector,
                source_field,
            )
            continue

        # No row, or existing status='proposed': delegate to the store (which audits).
        upsert_source_metric_mapping(
            metric_definition_id=definition_id,
            connector=connector,
            source_field_path=source_field,
            scope_level=SCOPE_ORG,
            org_id=org_id,
            project_id=None,
            status="proposed",
            created_by=identity,
        )

        if existing is None:
            proposed += 1
        else:
            # existing status='proposed' -> re-upsert; the store's _mapping_changed
            # guard emits no audit when content is identical.
            unchanged += 1

    report = {
        "proposed": proposed,
        "unchanged": unchanged,
        "preserved": preserved,
        "skipped": skipped,
        "connectors": seen_connectors,
    }
    logger.info(
        "bootstrap: org=%s proposed=%d unchanged=%d preserved=%d skipped=%d connectors=%s",
        org_id,
        proposed,
        unchanged,
        preserved,
        skipped,
        seen_connectors,
    )
    return report
