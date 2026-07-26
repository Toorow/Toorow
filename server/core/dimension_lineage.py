"""toorow -- Inverse lineage: "what FEEDS this conformed dimension?" (Story 27.9).

Epic-13 knows how to guard a deletion ("who depends on this?"). The symmetric question
had no answer: given a conformed dimension and a project, WHICH connector, WHICH report,
WHICH source field actually feed it, at WHICH mapping status, confirmed at WHICH scope.
This module is that read model. It is GENERIC -- nothing here is specific to any one
dimension; it serves geography, language, and any client-defined conformed dimension
equally (AD-2: every name comes from data, never from this code).

THE JOIN, HONESTLY
------------------
Three pieces of provenance already exist and nobody joined them:

  1. VALUE level -- app.dimension_value_mappings (migration 052, core.dimension_conformance)
     knows (canonical_dimension, CONNECTOR, source_value) -> canonical_value and the
     human status of each row. It does NOT know the report nor the source field.
  2. SCHEMA level -- each module manifest carries canonical_dimension_mapping
     (source_field -> canonical_target), read as DATA (json.load, no import).
     It does NOT know which report was actually pulled.
  3. WHAT WAS ACTUALLY PULLED -- the datastream's CURRENT PLAN VERSION
     (app.datastream_plan_versions.normalized_payload) carries source.report_id and
     source.selection.dimensions. It is the only piece that knows the REPORT.

The report grain is the point of this story: one source field is often available on
SEVERAL report types, and only the plan version says which one was really tired. The read
model therefore keys its rows on (connector, report_id, source_field) and never collapses
back to the connector.

WHAT IS NOT JOINABLE IS SAID, NOT GUESSED
-----------------------------------------
  * a datastream with no current plan version           -> gap 'no_plan_version'
  * a datastream whose connector is not recorded        -> gap 'connector_unknown'
  * a connector with no readable manifest mapping       -> gap 'schema_mapping_unavailable'
  * a plan that declares no dimensions (non-pull kinds) -> gap 'no_declared_dimensions'
  * a plan with no report_id in its payload             -> report_id = 'unknown'
  * value mappings whose connector no live plan feeds   -> row kept, report/field
                                                           'unknown', gap 'no_plan_evidence'
Never an invented report, never an invented field.

STATUS IS EXPOSED, NEVER BYPASSED
---------------------------------
The 27.4 invariant is untouched: nothing is auto-confirmed and conform_value still
resolves confirmed rows only. This view REPORTS the status (and the scope at which each
pair is confirmed) so a human can see that a source feeds a dimension through mappings
that are still merely proposed. That is the readable counterpart of AD-9.

TRUST CONTRACT (S-4, mirrors dimension_conformance.conform_value): the DB entry points
here assume project_id/org_id are ALREADY AUTHORIZED. The org guard belongs to the calling
surface (core.dimension_lineage_api).

Design mirrors dimension_conformance.py / metric_semantics_bootstrap.py:
``from __future__ import annotations``, module logger, lazy ``core.db`` imports inside the
DB functions, PURE functions separated from I/O so the invariants are testable without
Postgres, ASCII-only log strings, ZERO provider/dimension vocabulary (AD-2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract constants (named so a surface never spells them inline).
# ---------------------------------------------------------------------------

# The three provenance pieces come from three places; when one is missing we say which.
UNKNOWN = "unknown"            # the piece exists in principle, the payload does not say
SCHEMA_LINK_MANIFEST = "manifest"      # source_field -> dimension proven by a manifest
SCHEMA_LINK_UNAVAILABLE = "unavailable"  # no schema-level proof for this row

# Gap reasons (why a datastream / a mapping could not be fully joined).
GAP_NO_PLAN_VERSION = "no_plan_version"
GAP_CONNECTOR_UNKNOWN = "connector_unknown"
GAP_SCHEMA_MAPPING_UNAVAILABLE = "schema_mapping_unavailable"
GAP_NO_DECLARED_DIMENSIONS = "no_declared_dimensions"
GAP_NO_PLAN_EVIDENCE = "no_plan_evidence"

# Aggregate mapping status of a connector for one dimension (value level).
MAPPING_STATUS_NONE = "none"          # no value mapping row at all
MAPPING_STATUS_CONFIRMED = "confirmed"  # every row is confirmed
MAPPING_STATUS_PARTIAL = "partial"    # some confirmed, some not
MAPPING_STATUS_PROPOSED = "proposed"  # rows exist, none confirmed, at least one proposed
MAPPING_STATUS_REJECTED = "rejected"  # rows exist and all of them are rejected

# The value level knows the CONNECTOR, not the report: the mapping block of a row is a
# connector-grain fact and says so, rather than pretending to be report-grain.
MAPPING_GRAIN_CONNECTOR = "connector"

# Manifest key holding the SCHEMA-level mapping (source_field -> canonical target).
_MANIFEST_DIMENSION_KEY = "canonical_dimension_mapping"


# ---------------------------------------------------------------------------
# A. SCHEMA level -- manifests read as DATA (PURE apart from the file read).
# ---------------------------------------------------------------------------


def _default_modules_dir() -> Path:
    """server/modules relative to this file (server/core/dimension_lineage.py)."""
    return Path(__file__).resolve().parents[1] / "modules"


def read_manifest_dimension_mappings(
    modules_dir: str | Path | None = None,
) -> dict[str, dict[str, str]]:
    """Return {connector -> {source_field -> canonical_target}} from the manifests.

    Connector identity is the module DIRECTORY name (opaque, AD-2) -- the same identity
    the value-level rows and app.datastreams.module_name use. A directory without a
    manifest, without the mapping key, or with an unreadable manifest is simply ABSENT
    from the index (the caller then reports 'schema_mapping_unavailable' instead of
    guessing). A mapping value may be a plain string or a dict carrying a 'canonical'
    key (some manifests qualify their target); both shapes are accepted, anything else
    is skipped. Deterministic: directories and fields sorted by name.
    """
    base = Path(modules_dir) if modules_dir is not None else _default_modules_dir()
    index: dict[str, dict[str, str]] = {}
    try:
        subdirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    except OSError as exc:
        logger.warning("dimension_lineage: cannot iterate modules_dir=%s: %s", base, exc)
        return index

    for subdir in subdirs:
        manifest_path = subdir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "dimension_lineage: skipping manifest connector=%s reason=%s",
                subdir.name,
                exc,
            )
            continue
        mapping = manifest.get(_MANIFEST_DIMENSION_KEY)
        if not isinstance(mapping, dict):
            continue
        fields: dict[str, str] = {}
        for source_field in sorted(mapping.keys()):
            target = mapping[source_field]
            if isinstance(target, dict):
                target = target.get("canonical")
            if isinstance(target, str) and target:
                fields[source_field] = target
        if fields:
            index[subdir.name] = fields
    return index


# ---------------------------------------------------------------------------
# B. PLAN level -- what was actually pulled (PURE over supplied rows).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanFieldUse:
    """One piece of evidence: this datastream pulls this field, from this report."""

    connector: str
    report_id: str
    source_field: str
    datastream_id: str
    datastream_name: str | None
    plan_version_id: str | None
    version_number: int | None
    enabled: bool
    archived: bool

    def as_dict(self) -> dict:
        return {
            "datastream_id": self.datastream_id,
            "datastream_name": self.datastream_name,
            "plan_version_id": self.plan_version_id,
            "plan_version_number": self.version_number,
            "enabled": self.enabled,
            "archived": self.archived,
        }


def _payload_of(plan_row: dict) -> dict:
    """Return the plan payload as a dict ({} when absent or unusable)."""
    payload = plan_row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
    return payload if isinstance(payload, dict) else {}


def extract_plan_field_uses(
    plan_rows,
    *,
    manifest_index: dict[str, dict[str, str]],
    canonical_dimension: str,
) -> tuple[list[PlanFieldUse], list[dict]]:
    """Join PLAN x MANIFEST for one dimension (PURE). Returns (uses, gaps).

    *plan_rows* are dicts: datastream_id, datastream_name, connector, plan_version_id,
    version_number, payload (the normalized plan payload), enabled, archived.

    A use is emitted for EVERY selected dimension field whose manifest target equals
    *canonical_dimension*, keyed with the report the plan really names. When the payload
    carries no report_id, the report is 'unknown' -- the use is still emitted (the field
    IS pulled), it just cannot name the report.

    Everything that could not be joined lands in *gaps* with an explicit reason; nothing
    is guessed. Deterministic ordering on both lists.
    """
    uses: list[PlanFieldUse] = []
    gaps: list[dict] = []

    for plan_row in plan_rows:
        datastream_id = plan_row.get("datastream_id")
        connector = plan_row.get("connector")
        base_gap = {
            "datastream_id": datastream_id,
            "datastream_name": plan_row.get("datastream_name"),
            "connector": connector or UNKNOWN,
        }
        if not connector:
            gaps.append({**base_gap, "reason": GAP_CONNECTOR_UNKNOWN})
            continue
        payload = _payload_of(plan_row)
        if not payload:
            gaps.append({**base_gap, "reason": GAP_NO_PLAN_VERSION})
            continue

        source = payload.get("source")
        source = source if isinstance(source, dict) else {}
        report_id = source.get("report_id") or UNKNOWN
        selection = source.get("selection")
        selection = selection if isinstance(selection, dict) else {}
        dimensions = selection.get("dimensions")
        dimensions = [d for d in dimensions if isinstance(d, str)] if isinstance(
            dimensions, list
        ) else []

        if not dimensions:
            gaps.append(
                {**base_gap, "reason": GAP_NO_DECLARED_DIMENSIONS, "report_id": report_id}
            )
            continue

        fields = manifest_index.get(connector)
        if not fields:
            gaps.append(
                {
                    **base_gap,
                    "reason": GAP_SCHEMA_MAPPING_UNAVAILABLE,
                    "report_id": report_id,
                }
            )
            continue

        matched = [f for f in sorted(set(dimensions)) if fields.get(f) == canonical_dimension]
        for source_field in matched:
            uses.append(
                PlanFieldUse(
                    connector=connector,
                    report_id=report_id,
                    source_field=source_field,
                    datastream_id=datastream_id,
                    datastream_name=plan_row.get("datastream_name"),
                    plan_version_id=plan_row.get("plan_version_id"),
                    version_number=plan_row.get("version_number"),
                    enabled=bool(plan_row.get("enabled")),
                    archived=bool(plan_row.get("archived")),
                )
            )

    uses.sort(
        key=lambda u: (u.connector, u.report_id, u.source_field, u.datastream_id or "")
    )
    gaps.sort(
        key=lambda g: (
            str(g.get("connector") or ""),
            str(g.get("reason") or ""),
            str(g.get("datastream_id") or ""),
        )
    )
    return uses, gaps


# ---------------------------------------------------------------------------
# C. VALUE level -- per-connector mapping summary (PURE over supplied rows).
# ---------------------------------------------------------------------------


def summarize_mappings_by_connector(mapping_rows) -> dict[str, dict]:
    """Summarize value-mapping rows per connector (PURE).

    For each connector: the status counts, the aggregate status, how many
    (connector, source_value) pairs actually RESOLVE through the cascade, and at which
    scopes those confirmations live. Status is EXPOSED, never bypassed: a connector whose
    rows are all merely proposed comes out with resolved_value_count = 0.
    """
    from core.dimension_conformance import (  # noqa: PLC0415
        STATUS_CONFIRMED,
        STATUS_PROPOSED,
        STATUS_REJECTED,
        reduce_conformance_by_specificity,
    )

    per_connector: dict[str, dict] = {}
    rows_by_connector: dict[str, list[dict]] = {}
    for row in mapping_rows:
        connector = row.get("connector")
        if not connector:
            continue
        rows_by_connector.setdefault(connector, []).append(row)

    for connector in sorted(rows_by_connector):
        rows = rows_by_connector[connector]
        counts = {
            STATUS_CONFIRMED: 0,
            STATUS_PROPOSED: 0,
            STATUS_REJECTED: 0,
        }
        for row in rows:
            status = row.get("status")
            if status in counts:
                counts[status] += 1

        resolved = reduce_conformance_by_specificity(rows, confirmed_only=True)
        # The scope that actually WON each confirmed pair (that is where the human said
        # yes) -- reduce keeps the most specific, so recompute the winners' scopes.
        winners: dict[tuple, dict] = {}
        for row in rows:
            if row.get("status") != STATUS_CONFIRMED:
                continue
            key = (row.get("connector"), row.get("source_value"))
            if key not in resolved:
                continue
            current = winners.get(key)
            if current is None or _rank(row) > _rank(current):
                winners[key] = row
        confirmed_scopes = sorted(
            {row.get("scope_level") for row in winners.values() if row.get("scope_level")}
        )

        if counts[STATUS_CONFIRMED] and counts[STATUS_CONFIRMED] == len(rows):
            status = MAPPING_STATUS_CONFIRMED
        elif counts[STATUS_CONFIRMED]:
            status = MAPPING_STATUS_PARTIAL
        elif counts[STATUS_PROPOSED]:
            status = MAPPING_STATUS_PROPOSED
        elif counts[STATUS_REJECTED]:
            status = MAPPING_STATUS_REJECTED
        else:
            status = MAPPING_STATUS_NONE

        per_connector[connector] = {
            "grain": MAPPING_GRAIN_CONNECTOR,
            "status": status,
            "counts": dict(counts),
            "row_count": len(rows),
            "resolved_value_count": len(resolved),
            "confirmed_scopes": confirmed_scopes,
        }
    return per_connector


def _rank(row: dict) -> int:
    from core.dimension_conformance import _SCOPE_RANK  # noqa: PLC0415

    return _SCOPE_RANK.get(row.get("scope_level"), -1)


def _empty_mapping_summary() -> dict:
    from core.dimension_conformance import (  # noqa: PLC0415
        STATUS_CONFIRMED,
        STATUS_PROPOSED,
        STATUS_REJECTED,
    )

    return {
        "grain": MAPPING_GRAIN_CONNECTOR,
        "status": MAPPING_STATUS_NONE,
        "counts": {STATUS_CONFIRMED: 0, STATUS_PROPOSED: 0, STATUS_REJECTED: 0},
        "row_count": 0,
        "resolved_value_count": 0,
        "confirmed_scopes": [],
    }


# ---------------------------------------------------------------------------
# D. The read model itself (PURE assembly of the three levels).
# ---------------------------------------------------------------------------


def build_fed_by(
    *,
    canonical_dimension: str,
    label: dict,
    plan_uses,
    gaps,
    mapping_rows,
    project_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Assemble the "fed-by" read model (PURE).

    One row per (connector, report_id, source_field) -- the report grain is never
    collapsed. Connectors that carry value mappings but that NO plan feeds are still
    listed, with report/field 'unknown' and a 'no_plan_evidence' gap: a mapping without a
    live source is exactly the kind of thing this view exists to make visible.
    """
    summaries = summarize_mappings_by_connector(mapping_rows)

    grouped: dict[tuple[str, str, str], list] = {}
    for use in plan_uses:
        grouped.setdefault((use.connector, use.report_id, use.source_field), []).append(use)

    all_gaps = list(gaps)
    entries: list[dict] = []
    for key in sorted(grouped):
        connector, report_id, source_field = key
        uses = sorted(grouped[key], key=lambda u: (u.datastream_id or ""))
        entries.append(
            {
                "connector": connector,
                "report_id": report_id,
                "source_field": source_field,
                "schema_link": SCHEMA_LINK_MANIFEST,
                "datastreams": [use.as_dict() for use in uses],
                "mapping": summaries.get(connector) or _empty_mapping_summary(),
            }
        )

    connectors_with_plan = {use.connector for use in plan_uses}
    for connector in sorted(set(summaries) - connectors_with_plan):
        entries.append(
            {
                "connector": connector,
                "report_id": UNKNOWN,
                "source_field": UNKNOWN,
                "schema_link": SCHEMA_LINK_UNAVAILABLE,
                "datastreams": [],
                "mapping": summaries[connector],
            }
        )
        all_gaps.append({"connector": connector, "reason": GAP_NO_PLAN_EVIDENCE})

    all_gaps.sort(
        key=lambda g: (
            str(g.get("connector") or ""),
            str(g.get("reason") or ""),
            str(g.get("datastream_id") or ""),
        )
    )
    return {
        "canonical_dimension": canonical_dimension,
        "display_label": label.get("display_label"),
        "label_scope": label.get("scope_level"),
        "label_source": label.get("label_source"),
        "scope": {"project_id": project_id, "org_id": org_id},
        "fed_by": entries,
        "gaps": all_gaps,
    }


# ---------------------------------------------------------------------------
# E. DB loaders + the public entry point.
# ---------------------------------------------------------------------------


def load_project_plan_rows(project_id: str) -> list[dict]:
    """Load the CURRENT plan version of every datastream of one project.

    Project isolation is structural: the datastream is filtered on project_id AND the
    plan version is joined on the SAME project_id, so no foreign project's payload can
    ever enter the read model. A datastream without a current plan version is returned
    with payload=None so the caller can report the gap instead of dropping it silently.
    """
    from core.db import get_connection  # noqa: PLC0415

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.name, d.module_name, d.enabled, d.archived_at,
                       v.id, v.version_number, v.normalized_payload
                FROM app.datastreams d
                LEFT JOIN app.datastream_plan_versions v
                       ON v.id = d.current_plan_version_id
                      AND v.project_id = d.project_id
                WHERE d.project_id = %s
                ORDER BY d.name, d.id
                """,
                (project_id,),
            )
            for row in cur.fetchall():
                rows.append(
                    {
                        "datastream_id": row[0],
                        "datastream_name": row[1],
                        "connector": row[2],
                        "enabled": bool(row[3]),
                        "archived": row[4] is not None,
                        "plan_version_id": row[5],
                        "version_number": row[6],
                        "payload": row[7],
                    }
                )
    return rows


def get_fed_by(
    *,
    canonical_dimension: str,
    project_id: str,
    modules_dir: str | Path | None = None,
) -> dict:
    """THE inverse-lineage answer for (dimension, project). Never raises on missing data.

    S-4: *project_id* is assumed ALREADY AUTHORIZED -- the surface owns the org guard.
    The project's org is resolved from app.projects (fail-to-platform when unknown, same
    contract as resolve_dimension_conformance).
    """
    from core.dimension_conformance import (  # noqa: PLC0415
        _load_conformance_rows,
        _project_org_id,
        resolve_dimension_label,
    )

    org_id = _project_org_id(project_id)
    label = resolve_dimension_label(
        canonical_dimension, org_id=org_id, project_id=project_id
    )
    try:
        mapping_rows = _load_conformance_rows(
            canonical_dimension=canonical_dimension, org_id=org_id, project_id=project_id
        )
    except Exception as exc:  # noqa: BLE001
        # Fail-soft: the plan/schema levels still answer; the value level is reported as
        # absent (status 'none'), never as confirmed.
        logger.warning(
            "dimension_lineage: value-mapping load failed project=%s: %s", project_id, exc
        )
        mapping_rows = []
    try:
        plan_rows = load_project_plan_rows(project_id)
    except Exception as exc:  # noqa: BLE001
        # Fail-soft and HONEST: without the plan level we still answer, but the report
        # grain is unavailable and the caller sees why.
        logger.warning(
            "dimension_lineage: plan load failed project=%s: %s", project_id, exc
        )
        plan_rows = []
    manifest_index = read_manifest_dimension_mappings(modules_dir)
    uses, gaps = extract_plan_field_uses(
        plan_rows,
        manifest_index=manifest_index,
        canonical_dimension=canonical_dimension,
    )
    return build_fed_by(
        canonical_dimension=canonical_dimension,
        label=label,
        plan_uses=uses,
        gaps=gaps,
        mapping_rows=mapping_rows,
        project_id=project_id,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# F. Carrying the client label to the READING surfaces (render / narrative / LLM).
#
# The stable identifier never leaves the machine layer: reading surfaces read
# meta.dimension_labels[<identifier>].display_label. When a dimension has no client
# label the identifier is shown and label_source says 'fallback_identifier', so a
# narrative or an LLM can tell "this is the client's word" from "this is our key".
# ---------------------------------------------------------------------------


def build_label_map(dimensions, resolved_labels: dict[str, dict]) -> dict[str, dict]:
    """Return {identifier -> {display_label, description, scope_level, label_source}} (PURE).

    Every requested identifier is present in the output (a reading surface must always
    find something to print); the ones without a stored label carry the identifier itself
    and declare the fallback."""
    from core.dimension_conformance import (  # noqa: PLC0415
        LABEL_SOURCE_CLIENT,
        LABEL_SOURCE_FALLBACK,
    )

    label_map: dict[str, dict] = {}
    for dimension in sorted({d for d in dimensions if d}):
        entry = resolved_labels.get(dimension)
        if entry and entry.get("display_label"):
            label_map[dimension] = {
                "display_label": entry.get("display_label"),
                "description": entry.get("description"),
                "scope_level": entry.get("scope_level"),
                "label_source": LABEL_SOURCE_CLIENT,
            }
        else:
            label_map[dimension] = {
                "display_label": dimension,
                "description": None,
                "scope_level": None,
                "label_source": LABEL_SOURCE_FALLBACK,
            }
    return label_map


def resolve_label_map(
    dimensions, *, org_id: str | None, project_id: str | None = None
) -> dict[str, dict]:
    """DB-backed build_label_map: one load, then the pure projection. Never raises."""
    from core.dimension_conformance import resolve_dimension_labels  # noqa: PLC0415

    try:
        resolved = resolve_dimension_labels(org_id=org_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dimension_lineage: label resolution failed org=%s: %s", org_id, exc)
        resolved = {}
    return build_label_map(dimensions, resolved)


def decorate_envelope_with_labels(envelope: dict, label_map: dict[str, dict]) -> dict:
    """Return a COPY of *envelope* carrying meta.dimension_labels (PURE, additive).

    AD-1 additive key, exactly like meta.branding: an empty map OMITS the key entirely
    (never a null), so existing consumers are unaffected. The input envelope is not
    mutated."""
    if not isinstance(envelope, dict):
        return envelope
    if not label_map:
        return dict(envelope)
    decorated = dict(envelope)
    meta = dict(decorated.get("meta") or {})
    meta["dimension_labels"] = label_map
    decorated["meta"] = meta
    return decorated
