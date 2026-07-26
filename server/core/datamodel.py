"""toorow -- Data model (target fields + mappings) CRUD (Story 8.5, Epic 8).

Provides:
  list_target_fields(project_id?, kind?, usage?, conn)  -> list[dict]
  get_target_field(name, conn)                          -> dict | None
  create_target_field(data, created_by, conn)           -> dict
  update_target_field(name, patch, conn, identity=)     -> dict | None
  approve_target_field(name, identity, conn)            -> dict  [Story 13.1]
  delete_target_field(name, identity, conn)             -> None  [Story 13.1;
                                                            soft delete since 44.7]
  list_field_versions(name, conn)                       -> list[dict]  [Story 44.7]
  upsert_mapping(datastream_id, source_field,
                 target_field_name|None, identity, conn) -> dict

Design decisions:
  - AD-2: module_name comes from datastream rows (never hardcoded).
  - AD-5: datastream-joined queries carry WHERE ds.project_id = %s when project_id given.
  - name (PK on target_fields) is immutable: enforced in update_target_field (422-like ValueError).
  - data_type is also immutable post-creation (same guard).
  - Conflict detection (R2 + Story 39.1) is computed at read time (no extra table):
      CURRENCY_CONFLICT: field is classified MONETARY (Epic-27 semantic layer) OR legacy
        data_type IN ('currency','decimal'), AND >=2 distinct modules feed it (superset-
        compatible refinement -- the classifier tightens, the legacy heuristic stays).
      CURRENCY_GAP (classified MONETARY but no feeding stream carries a resolvable source
        currency -> never a naked amount, E39-FR02): DECISION helpers ship in 39.1; EMISSION
        is DEFERRED to Story 39.3 (which adds the used-by currency provenance seam + teaches
        conflict_resolutions.list_conflicts to render it). Not emitted here yet.
      MEASURE_NULL: data_type IN ('integer', 'decimal', 'currency') AND measure IS NULL.
  - Audit uses write_audit_row with literal string actions (no new ACTION_ constants needed;
    orchestrator can consolidate at review).
  - Timestamp serialisation pattern mirrors datastreams.py (_row_to_dict with isoformat).
  - Story 13.1: status='draft'|'approved' added. Approval is audited in
    app.target_field_approvals (append-only). DELETE is guarded by used_by_count > 0.
    Seuls les champs status='approved' alimentent les reports/cards (filtre dans
    report_chain._fetch_target_fields). Champs is_default=true toujours 'approved'.
  - Story 44.7: every create/patch/approve/delete mutation appends one row to
    app.target_fields_versions in the SAME transaction (pattern copied from
    context_store.py's context_topics_versions handling): full column snapshot +
    change_kind + diff (before/after per changed field, patch only) + real
    changed_by identity + changed_at. DELETE is now a soft delete
    (status='deleted'); list/get paths exclude it by default so deleted fields
    disappear from the API/UI while the row and its full version history
    survive. Recreating a name that is currently 'deleted' revives the row
    (see create_target_field docstring).

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_DATA_TYPES = {"string", "integer", "decimal", "date", "boolean", "currency"}
_VALID_FIELD_KINDS = {"metric", "dimension"}
_VALID_MEASURES = {"sum", "average", "min", "max", "count", None}
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# data_types that are numeric and must have a measure when used as metrics
_NUMERIC_TYPES = {"integer", "decimal", "currency"}

# Timestamp columns to ISO-format on serialisation
_TS_COLS = {"created_at", "approved_at", "updated_at", "changed_at"}

# Valid status values (Story 13.1)
_VALID_STATUSES = {"draft", "approved"}

# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise a row; ISO-format timestamps."""
    out: dict = {}
    for col, val in zip(cols, row):
        if col in _TS_COLS and val is not None:
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


# ---------------------------------------------------------------------------
# Story 44.7: target_fields_versions append helper (pattern copied from
# context_store.py's context_topics_versions handling -- same-transaction
# append, MAX(version_number) + 1).
# ---------------------------------------------------------------------------

# Patchable fields eligible for diff computation on 'updated'/'restored' versions.
_TFV_DIFFABLE_FIELDS = ("display_name", "measure", "description")


def _insert_target_field_version_row(
    conn,
    field: dict,
    *,
    version_number: int,
    change_kind: str,
    diff: dict | None,
    changed_by: str,
) -> None:
    """INSERT one app.target_fields_versions row -- no MAX() lookup.

    Internal helper: always go through _append_target_field_version, which
    computes version_number = MAX(version_number) + 1 first. This keeps the
    revival path (create_target_field on a name whose PK already carries
    version history) correct -- a hardcoded version_number=1 here would
    collide with pk(name, version_number) the second time a name is
    deleted-then-recreated.
    """
    diff_json = json.dumps(diff) if diff is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.target_fields_versions
                (name, version_number, display_name, data_type, field_kind,
                 measure, description, created_by, is_default, created_at,
                 status, approved_at, approved_by, updated_at,
                 change_kind, diff, changed_by, changed_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s,
                 %s::timestamptz, %s, %s::timestamptz,
                 %s, %s::jsonb, %s, now())
            """,
            (
                field["name"],
                version_number,
                field.get("display_name"),
                field.get("data_type"),
                field.get("field_kind"),
                field.get("measure"),
                field.get("description"),
                field.get("created_by"),
                field.get("is_default"),
                field.get("created_at"),
                field.get("status"),
                field.get("approved_at"),
                field.get("approved_by"),
                field.get("updated_at"),
                change_kind,
                diff_json,
                changed_by,
            ),
        )


def _append_target_field_version(
    conn,
    field: dict,
    *,
    change_kind: str,
    diff: dict | None,
    changed_by: str,
) -> int:
    """Append one row to app.target_fields_versions on the given connection.

    Computes version_number = MAX(version_number) + 1 for this name (0 + 1 = 1
    on the first version). Returns the version_number written. Used by ALL
    mutations -- create/update/approve/delete -- including create_target_field's
    revival path (finding 44.7#1): a hardcoded version_number=1 there would
    collide with pk(name, version_number) on a name that already carries
    version history from before its soft-delete.
    """
    name = field["name"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0)
            FROM app.target_fields_versions
            WHERE name = %s
            """,
            (name,),
        )
        row = cur.fetchone()
        next_version = (row[0] if row else 0) + 1

    _insert_target_field_version_row(
        conn, field, version_number=next_version, change_kind=change_kind,
        diff=diff, changed_by=changed_by,
    )
    return next_version


def _diff_target_field_patch(before: dict, after: dict) -> dict:
    """Return {field: {before, after}} for every patchable field that changed.

    Only _TFV_DIFFABLE_FIELDS are considered (display_name, measure,
    description) -- name/data_type/field_kind are immutable and never diffed.
    """
    diff: dict = {}
    for field_name in _TFV_DIFFABLE_FIELDS:
        old_val = before.get(field_name)
        new_val = after.get(field_name)
        if old_val != new_val:
            diff[field_name] = {"before": old_val, "after": new_val}
    return diff


# ---------------------------------------------------------------------------
# Conflict detection (R2 heuristic — pure Python, no extra SQL)
# ---------------------------------------------------------------------------


# Story 39.1: keys a used-by row MAY carry a source-currency provenance signal under.
# AD-2: read whatever signal the row already exposes -- NEVER a provider-specific field name.
# Today (39.1) the used-by rows carry NONE of these (get_target_field's SELECT has no currency
# column), so a monetary field always trips CURRENCY_GAP. This tuple is the DOCUMENTED SEAM
# for Story 39.3 to populate the provenance signal on the used-by rows (bind a source currency
# per stream, migration 053 flow) -- add the column to get_target_field's used-by SELECT and it
# flows through here with zero change to the gap logic.
_CURRENCY_PROVENANCE_KEYS = ("currency", "source_currency", "currency_code")


def _used_by_currency(ub: dict) -> str | None:
    """Return the resolvable source currency on a used-by row (source-agnostic), or None.

    Reads only generic provenance keys (never a provider's field name, AD-2). A blank/None
    value counts as ABSENT (fail-closed: an empty currency is no currency, E39-NFR02).
    Story 39.3 populates ``source_currency`` on used-by rows from the currency-binding
    provenance (get_target_field's used-by SELECT joins the declared per-stream currency)."""
    for key in _CURRENCY_PROVENANCE_KEYS:
        val = ub.get(key)
        if isinstance(val, str):
            stripped = val.strip()
            if stripped:
                return stripped.upper()
            continue  # blank / whitespace-only string == no currency (fail-closed)
        if val not in (None, "", False):
            return str(val).upper()
    return None


def _used_by_has_currency(ub: dict) -> bool:
    """True if a used-by row carries ANY resolvable source-currency signal (source-agnostic).

    Thin predicate over ``_used_by_currency`` (fail-closed on blank/None, AD-2)."""
    return _used_by_currency(ub) is not None


# Story 39.8: keys a used-by row MAY carry the captured report-timezone provenance under.
# AD-2: read whatever generic signal the row already exposes -- NEVER a provider field name
# (``timeZone``). Mirrors _CURRENCY_PROVENANCE_KEYS / report_timezone._TIMEZONE_PROVENANCE_KEYS.
_REPORT_TIMEZONE_KEYS = ("report_timezone", "captured_report_timezone")


def _stream_report_timezone(used_by_row: dict) -> str | None:
    """Return the captured report timezone for a used-by stream (source-agnostic), or None.

    THE seam onto Story 39.7's generalized capture (mirrors ``_used_by_currency`` exactly:
    same fail-closed-on-blank posture, same generic-key list, same AD-2 discipline). Reads ONLY
    a generic provenance key (never a provider's field name, AD-2). A blank/None value counts as
    ABSENT (fail-closed: an unknown timezone is 39.7's GAP, never a fabricated UTC -- E39-NFR02).

    TODAY (39.7's generalized capture landed as the accessor
    ``report_timezone.report_timezone_for_datastream``): the used-by SELECT surfaces the captured
    zone under a generic ``report_timezone`` key (additive-JOIN discipline, mirroring 39.3's
    ``source_currency``); this reads it. When a persisted per-mapping timezone column lands
    (a migration owned by 39.7), the used-by SELECT populates the same generic key -- this call
    site does NOT change.
    """
    if not isinstance(used_by_row, dict):
        return None
    for key in _REPORT_TIMEZONE_KEYS:
        val = used_by_row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _field_is_monetary(field: dict, project_id: str | None) -> bool:
    """Whether *field* is a MONETARY metric per the Epic-27 semantic layer (Story 39.1).

    Prefers the resolved semantic-layer ``monetary`` when a project is in context (cascade
    PROJECT > ORG > PLATFORM), else falls back to the pure classifier by canonical name.
    FAIL-SOFT: any import/resolution failure degrades to non-monetary (no crash) so the
    read-time detector never blows up on a metric-semantics hiccup."""
    name = field.get("name") or ""
    if not name:
        return False
    try:
        # Lazy import (same pattern as create_target_field importing is_ratio_name).
        from core.metric_semantics import is_metric_monetary  # noqa: PLC0415

        return is_metric_monetary(name, project_id=project_id)
    except Exception:  # noqa: BLE001 -- fail-soft, degraded not crashed.
        return False


def _detect_conflicts(
    field: dict, used_by: list[dict], *, project_id: str | None = None
) -> list[dict]:
    """Return a list of structured conflict warnings for a target field.

    Criteria (R2 heuristics + Story 39.1 semantic refinement):
      CURRENCY_CONFLICT: the field is CLASSIFIED MONETARY (Epic-27 semantic layer) OR the
        legacy heuristic data_type in ('currency','decimal') holds, AND >=2 distinct
        module_names feed it -> units/currencies may differ per source. Story 39.1 tightens
        the trigger to classified-monetary (the semantic truth) while KEEPING the legacy
        data_type heuristic as a superset so no existing conflict disappears (AC-5).
      CURRENCY_GAP (E39-FR02): classified MONETARY but no used-by row carries a resolvable
        source currency. DECISION helpers ship here in 39.1; EMISSION is DEFERRED to Story 39.3
        (adds the used-by currency provenance seam + list_conflicts rendering). Not emitted yet.
      MEASURE_NULL: data_type in numeric types AND measure IS NULL AND field is a metric.
        Aggregation semantics undefined — rollups silently sum or average.

    Args:
        field:      The target_field dict (including name, data_type, measure, field_kind).
        used_by:    List of used-by dicts (each has 'module_name'; MAY carry a currency signal).
        project_id: Optional project context; when given, `monetary` resolves through the
                    semantic-layer cascade (project override wins), else the platform classifier.

    Returns:
        A (possibly empty) list of conflict dicts. CURRENCY_CONFLICT / CURRENCY_GAP carry the
        Story 39.3 additive keys (severity, monetary, conflicting_currencies, metric,
        resolvable_via) alongside the unchanged {code, message, affected_streams}; MEASURE_NULL
        keeps the base shape.
    """
    conflicts: list[dict] = []
    data_type = field.get("data_type") or ""
    measure = field.get("measure")
    field_kind = field.get("field_kind") or ""

    # Story 39.1: the semantic truth "is this metric money?" -- the single source the money
    # rules key off. Legacy data_type heuristic is kept as a superset so nothing regresses.
    is_monetary = _field_is_monetary(field, project_id)
    legacy_currency_heuristic = data_type in ("currency", "decimal")

    # CURRENCY_CONFLICT (tightened to classified-monetary OR the legacy heuristic).
    # Story 39.3: when the used-by rows carry >=2 DISTINCT observed source currencies, the
    # conflict is CERTAIN (not just possible) and names them; otherwise conflicting_currencies
    # stays None (honest -- the field layer does not open a warehouse cursor, so it only knows
    # currencies the currency-binding provenance already surfaced on the mapping rows).
    if is_monetary or legacy_currency_heuristic:
        modules = {ub.get("module_name") for ub in used_by if ub.get("module_name")}
        if len(modules) >= 2:
            affected = [ub.get("datastream_name") or ub.get("module_name", "") for ub in used_by]
            observed = sorted({c for ub in used_by if (c := _used_by_currency(ub)) is not None})
            conflicts.append(
                {
                    "code": "CURRENCY_CONFLICT",
                    "message": (
                        "Ce champ est alimenté par des modules distincts dont les unités "
                        "monétaires peuvent différer (ex. USD brut vs EUR normalisé). "
                        "Vérifiez la cohérence des devises avant d'agréger."
                    ),
                    "affected_streams": affected,
                    # --- Story 39.3 additive enrichment (backward-compatible) ---
                    "severity": "refusal",              # monetary => refusal-eligible
                    "monetary": bool(is_monetary),      # from the 39.1 classifier seam
                    "conflicting_currencies": (         # DISTINCT observed currencies, else None
                        observed if len(observed) >= 2 else None
                    ),
                    "metric": field.get("name"),        # the offending canonical metric
                    "resolvable_via": "fx_conversion",  # Epic 13 binds ccy; 39.4 converts
                }
            )

    # CURRENCY_GAP (E39-FR02): a CLASSIFIED-MONETARY field whose feeding streams carry NO
    # resolvable source currency -> a naked amount that cannot be denominated (fail-closed,
    # AD-9). Story 39.3 RE-ENABLES emission here (39.1 shipped the decision helpers dormant):
    # the used-by SELECT now surfaces per-stream currency provenance, and
    # conflict_resolutions.list_conflicts renders/skips this code instead of mis-dumping it
    # into its MEASURE_NULL `else` branch. Emitted ONLY for classified-monetary fields (not
    # the legacy data_type heuristic) so a plain 'decimal' field never trips a money gap.
    if is_monetary:
        streams_with_currency = any(_used_by_has_currency(ub) for ub in used_by)
        if not streams_with_currency:
            affected = [ub.get("datastream_name") or ub.get("module_name", "") for ub in used_by]
            conflicts.append(
                {
                    "code": "CURRENCY_GAP",
                    "message": (
                        "Ce champ monétaire n'a aucune devise source résolvable : aucun flux "
                        "ne déclare sa devise. Fail-closed : un montant sans devise n'est ni "
                        "sommé ni converti tant que sa devise n'est pas déclarée (liez une "
                        "devise source par flux)."
                    ),
                    "affected_streams": affected,
                    # --- Story 39.3 additive keys (shape-aligned with CURRENCY_CONFLICT) ---
                    "severity": "refusal",
                    "monetary": True,
                    "conflicting_currencies": None,     # no currency observed = the gap itself
                    "metric": field.get("name"),
                    "resolvable_via": "fx_conversion",
                }
            )

    # TIMEZONE_GAP (Story 39.7, E39-FR12 / E39-NFR02): a date-grain figure whose feeding
    # streams carry NO resolvable report timezone -> a "day" that cannot be honestly placed on
    # a timezone (and so cannot be aligned/signalled across sources later, Story 39.8). The
    # capture contract fails CLOSED: an undetermined report timezone surfaces this typed gap,
    # NEVER a silent UTC/default. Gated the SAME way as CURRENCY_GAP (classified-monetary) so it
    # fires only where a report timezone is load-bearing for cross-source day alignment -- a
    # plain non-monetary field never trips a spurious timezone gap. The used-by SELECT surfaces
    # per-stream report_timezone provenance (the documented seam in core.report_timezone);
    # today it carries none, so a monetary field with no declared source zone trips this gap.
    # Shape-aligned with CURRENCY_GAP so conflict_resolutions.list_conflicts renders it via its
    # existing non-currency branch (resolvable_via='source_timezone_declaration', not an FX bind).
    if is_monetary:
        from core import report_timezone  # noqa: PLC0415

        tz_gap = report_timezone.timezone_gap(field, used_by)
        if tz_gap is not None:
            conflicts.append(tz_gap)

    # TIMEZONE_DAY_OFFSET (Story 39.8, E39-FR13 / E39-FR14 amended): a cross-source day-offset
    # ADVISORY -- NOT a refusal (a day-offset does not make a total wrong, only a daily
    # comparison possibly misaligned). Fires when a field is fed by used-by streams carrying
    # >= 2 DISTINCT KNOWN report timezones. Deliberately NOT gated on is_monetary (unlike the
    # 39.7 TIMEZONE_GAP capture-gap, F1): a day-offset between non-monetary sessions/impressions
    # across timezones is exactly the case to signal. Streams whose report timezone is unknown
    # are EXCLUDED by the engine (defer to 39.7's GAP, never a fabricated UTC). The engine is
    # PURE (no realign, no warehouse read, realignable:false constant); append its dict verbatim.
    # Appended AFTER the currency/gap blocks and BEFORE MEASURE_NULL -- strictly additive, no
    # existing code/key touched (backward-compatible, mirrors 39.3's discipline).
    tz_streams = [
        {
            "datastream": ub.get("datastream_name") or ub.get("module_name"),
            "report_timezone": _stream_report_timezone(ub),
            "has_lever": bool(ub.get("report_timezone_has_lever")),
            "lever_hint": ub.get("report_timezone_lever_hint"),
        }
        for ub in used_by
    ]
    if len({s["report_timezone"] for s in tz_streams if s["report_timezone"]}) >= 2:
        from core.timezone_signal import check_cross_source_day_offset  # noqa: PLC0415

        offset_signal = check_cross_source_day_offset(
            metric=field.get("name"), streams=tz_streams
        )
        if offset_signal is not None:
            conflicts.append(offset_signal)

    # MEASURE_NULL
    if field_kind == "metric" and data_type in _NUMERIC_TYPES and measure is None:
        affected = [ub.get("datastream_name") or ub.get("module_name", "") for ub in used_by]
        conflicts.append(
            {
                "code": "MEASURE_NULL",
                "message": (
                    "La sémantique d'agrégation (measure) n'est pas définie pour ce champ "
                    "numérique. Les consommateurs pourraient appliquer une agrégation incorrecte "
                    "(somme vs moyenne). Définissez 'measure' pour sécuriser les rollups."
                ),
                "affected_streams": affected,
            }
        )

    return conflicts


# ---------------------------------------------------------------------------
# list_target_fields
# ---------------------------------------------------------------------------


def list_target_fields(
    conn,
    *,
    project_id: str | None = None,
    kind: str | None = None,
    usage: str | None = None,
    module: str | None = None,
) -> list[dict]:
    """Return target fields with used_by_count.

    Args:
        project_id: When given, scopes used_by_count to datastreams in that project (AD-5).
        kind:       'metric' | 'dimension' | None (all).
        usage:      'used' (used_by_count > 0) | 'unmapped' (used_by_count = 0) | None.
        module:     When given, restricts to fields that have at least one mapping via a
                    datastream whose module_name = module (JOIN datastream_mappings ->
                    datastreams). Unknown module -> empty list (200, not an error).

    Response shape per item:
        name, display_name, data_type, field_kind, measure, description,
        created_by, is_default, created_at, used_by_count, status
    """
    # Build the used_by_count sub-select, optionally scoped to a project
    if project_id:
        used_by_subq = """
            (
                SELECT COUNT(*)
                FROM app.datastream_mappings dm
                JOIN app.datastreams ds ON ds.id = dm.datastream_id
                WHERE dm.target_field = tf.name
                  AND ds.project_id = %s
            ) AS used_by_count
        """
        base_params: list = [project_id]
    else:
        used_by_subq = """
            (
                SELECT COUNT(*)
                FROM app.datastream_mappings dm
                WHERE dm.target_field = tf.name
            ) AS used_by_count
        """
        base_params = []

    conditions: list[str] = []
    extra_params: list = []

    # Story 44.7: soft-deleted fields never appear in list results (history
    # outlives visibility -- the row and its versions survive in the DB, but
    # the API/UI never lists a deleted field). No opt-in flag: history is
    # read via list_field_versions(name, conn), not this listing.
    conditions.append("tf.status != 'deleted'")

    # AI-49: ?module= filter -- restrict to fields mapped by datastreams of the given module.
    # Unknown module name -> no rows match -> empty list (200, never an error).
    if module is not None:
        conditions.append(
            """
            tf.name IN (
                SELECT dm2.target_field
                FROM app.datastream_mappings dm2
                JOIN app.datastreams ds2 ON ds2.id = dm2.datastream_id
                WHERE ds2.module_name = %s
                  AND dm2.target_field IS NOT NULL
            )
            """
        )
        extra_params.append(module)

    if kind is not None:
        conditions.append("tf.field_kind = %s")
        extra_params.append(kind)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # usage filter requires the count -> wrap in a CTE / subquery
    having_clause = ""
    if usage == "used":
        having_clause = "HAVING used_by_count > 0"
    elif usage == "unmapped":
        having_clause = "HAVING used_by_count = 0"

    if having_clause:
        sql = f"""
            SELECT name, display_name, data_type, field_kind, measure, description,
                   created_by, is_default, created_at, used_by_count, status
            FROM (
                SELECT
                    tf.name,
                    tf.display_name,
                    tf.data_type,
                    tf.field_kind,
                    tf.measure,
                    tf.description,
                    tf.created_by,
                    tf.is_default,
                    tf.created_at,
                    tf.status,
                    {used_by_subq}
                FROM app.target_fields tf
                {where_clause}
            ) sub
            {having_clause}
            ORDER BY field_kind ASC, name ASC
        """  # noqa: S608
    else:
        sql = f"""
            SELECT
                tf.name,
                tf.display_name,
                tf.data_type,
                tf.field_kind,
                tf.measure,
                tf.description,
                tf.created_by,
                tf.is_default,
                tf.created_at,
                tf.status,
                {used_by_subq}
            FROM app.target_fields tf
            {where_clause}
            ORDER BY tf.field_kind ASC, tf.name ASC
        """  # noqa: S608

    params = base_params + extra_params

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        results = []
        for row in cur.fetchall():
            rec = _row_to_dict(cols, row)
            # Coerce count to int (psycopg may return Decimal from COUNT)
            rec["used_by_count"] = int(rec.get("used_by_count") or 0)
            results.append(rec)
    return results


# ---------------------------------------------------------------------------
# get_target_field (with used-by detail + conflict detection)
# ---------------------------------------------------------------------------


def get_target_field(name: str, conn) -> dict | None:
    """Return full field detail including used-by list and conflict warnings.

    The USED-BY view (Jean's «quels flux alimentent clicks ?») returns for each
    mapping: datastream (name, module, project, enabled), source_field, last extract
    info (latest pull loaded_at + verification verdict via LATERAL join).

    Returns None if the field does not exist.
    """
    # 1. Fetch the field itself
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, display_name, data_type, field_kind, measure,
                   description, created_by, is_default, created_at,
                   status, approved_at, approved_by, updated_at
            FROM app.target_fields
            WHERE name = %s
              AND status != 'deleted'
            """,
            (name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        field = _row_to_dict(cols, row)

    # 2. Fetch used-by list with last pull info (LATERAL join mirrors datastreams.py summaries)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                dm.datastream_id,
                ds.name          AS datastream_name,
                ds.module_name,
                ds.project_id,
                ds.enabled,
                dm.source_field,
                lp.loaded_at     AS last_loaded_at,
                lp.verdict       AS last_verdict,
                fx.resolved_source_currency AS source_currency
            FROM app.datastream_mappings dm
            JOIN app.datastreams ds ON ds.id = dm.datastream_id
            LEFT JOIN LATERAL (
                SELECT pv.verified_at AS loaded_at, pv.verdict
                FROM app.pull_jobs pj
                JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                WHERE pj.datastream_id = dm.datastream_id
                ORDER BY pv.verified_at DESC
                LIMIT 1
            ) lp ON true
            -- Story 39.3: source-agnostic currency provenance seam. The per-stream declared
            -- source currency (Epic 13's currency binding, app.fx_conflict_resolutions) is
            -- surfaced on the used-by row so _detect_conflicts can name conflicting_currencies
            -- and decide the CURRENCY_GAP. Source-agnostic: keyed by (project, target_field,
            -- module_name), never a provider-specific column (AD-2).
            LEFT JOIN app.fx_conflict_resolutions fx
                   ON fx.project_id   = ds.project_id
                  AND fx.target_field = dm.target_field
                  AND fx.source_module = ds.module_name
            WHERE dm.target_field = %s
            ORDER BY ds.project_id ASC, ds.name ASC
            """,
            (name,),
        )
        ub_cols = [d[0] for d in cur.description]
        used_by: list[dict] = []
        for ub_row in cur.fetchall():
            rec: dict = {}
            for col, val in zip(ub_cols, ub_row):
                if col == "last_loaded_at" and val is not None:
                    rec[col] = val.isoformat()
                else:
                    rec[col] = val
            # Story 39.8: surface the captured report timezone per used-by stream under the
            # generic ``report_timezone`` key (the source-agnostic seam _detect_conflicts /
            # _stream_report_timezone reads). Resolved via 39.7's single-source-of-truth accessor
            # ``report_timezone.report_timezone_for_datastream`` (manifest/declaration read -- NO
            # new warehouse cursor; datamodel stays app-DB only). None/unknown => the key is
            # simply absent (fail-closed: an undetermined zone is 39.7's GAP, never a fabricated
            # UTC), so a stream without a resolvable zone is EXCLUDED from the day-offset compare.
            ds_id = rec.get("datastream_id")
            if ds_id is not None:
                try:
                    from core.report_timezone import (  # noqa: PLC0415
                        report_timezone_for_datastream,
                    )

                    zone = report_timezone_for_datastream(str(ds_id))
                except Exception:  # noqa: BLE001 -- fail-soft: an accessor hiccup drops to None.
                    zone = None
                if isinstance(zone, str) and zone.strip():
                    rec["report_timezone"] = zone.strip()
            used_by.append(rec)

    # 3. Conflict detection (R2, pure Python). Story 39.1/39.3: pass a project context when the
    # used-by streams share a single project, so the monetary classification resolves through
    # the PROJECT > ORG > PLATFORM cascade (a project may reclassify a custom metric); else
    # None -> platform default (target_fields is platform-global, so a mixed-project field has
    # no single project context).
    ub_projects = {ub.get("project_id") for ub in used_by if ub.get("project_id")}
    detect_project_id = next(iter(ub_projects)) if len(ub_projects) == 1 else None
    conflicts = _detect_conflicts(field, used_by, project_id=detect_project_id)

    field["used_by"] = used_by
    field["used_by_count"] = len(used_by)
    field["conflicts"] = conflicts
    return field


# ---------------------------------------------------------------------------
# create_target_field
# ---------------------------------------------------------------------------


def _is_unique_violation(exc: Exception) -> bool:
    """Best-effort detection of a Postgres unique-constraint violation.

    Matches on exception class name / message rather than importing psycopg
    error classes at module level (same style as datamodel_api's
    ForeignKeyViolation detection) -- works whether the driver raised its own
    exception class or a wrapped one.
    """
    return (
        "UniqueViolation" in type(exc).__name__
        or "unique constraint" in str(exc).lower()
        or "duplicate key" in str(exc).lower()
    )


class DuplicateFieldError(ValueError):
    """Raised on create when the name identifies a LIVE (non-deleted) field.

    Distinct from a bare ValueError so datamodel_api can keep mapping genuine
    duplicates to HTTP 409 (route contract) while validation errors stay 422.
    """


def _revive_deleted_target_field(
    conn,
    *,
    name: str,
    display_name: str,
    data_type: str,
    field_kind: str,
    measure: str | None,
    description: str | None,
    created_by: str,
) -> dict:
    """Revive a soft-deleted app.target_fields row under create_target_field.

    Story 44.7: recreating a field whose name is currently status='deleted'
    keeps the PK/name history continuous instead of raising a duplicate-key
    error -- the row is UPDATEd back to a fresh draft with the caller's new
    content (data_type/field_kind MAY change on revival: the old row is dead,
    only the name identity is reused, unlike a PATCH on a live field where
    they stay immutable).

    Raises ValueError if the name identifies a LIVE (non-deleted) field --
    a genuine duplicate name, not a revival.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM app.target_fields WHERE name = %s FOR UPDATE",
            (name,),
        )
        row = cur.fetchone()

    if row is None or row[0] != "deleted":
        raise DuplicateFieldError(f"Le champ '{name}' existe deja.")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.target_fields
            SET display_name = %s, data_type = %s, field_kind = %s, measure = %s,
                description = %s, created_by = %s, is_default = FALSE,
                status = 'draft', approved_at = NULL, approved_by = NULL,
                updated_at = now()
            WHERE name = %s
            RETURNING name, display_name, data_type, field_kind, measure,
                      description, created_by, is_default, created_at,
                      status, approved_at, approved_by, updated_at
            """,
            (display_name, data_type, field_kind, measure, description, created_by, name),
        )
        row2 = cur.fetchone()
        cols = [d[0] for d in cur.description]
        return _row_to_dict(cols, row2)


def create_target_field(data: dict, created_by: str, conn) -> dict:
    """INSERT a new user-defined target field. Returns the created dict.

    Required keys: name (snake_case), display_name, data_type, field_kind.
    Optional: measure, description.

    Raises ValueError on validation failure.
    NOTE: is_default is always False for user-created fields.

    Story 44.7: always appends a version row (change_kind='created', diff
    null) to app.target_fields_versions, via the MAX(version_number)+1 helper
    (_append_target_field_version) -- on a fresh name that naturally yields 1;
    on a revived name (see below) it continues that name's existing version
    sequence instead of colliding with a hardcoded 1. The row INSERT/UPDATE and
    the version append happen in ONE transaction (one commit) so a failure in
    either rolls back both. If `name` currently identifies a soft-deleted row,
    this REVIVES it instead of failing on the PK conflict -- see
    _revive_deleted_target_field's docstring; that revival is still recorded
    as a 'created' version (fresh content, not a restore from history).
    """
    name = (data.get("name") or "").strip()
    display_name = (data.get("display_name") or "").strip()
    data_type = (data.get("data_type") or "").strip()
    field_kind = (data.get("field_kind") or "").strip()
    measure = data.get("measure") or None
    description = (data.get("description") or "").strip() or None

    if not name:
        raise ValueError("name est requis")
    if not _SNAKE_CASE_RE.match(name):
        raise ValueError(
            "name doit etre en snake_case (lettres minuscules, chiffres, underscores; "
            "commence par une lettre) : " + repr(name)
        )
    if not display_name:
        raise ValueError("display_name est requis")
    if data_type not in _VALID_DATA_TYPES:
        raise ValueError(
            f"data_type invalide : {data_type!r}. "
            f"Valeurs acceptees : {sorted(_VALID_DATA_TYPES)}"
        )
    if field_kind not in _VALID_FIELD_KINDS:
        raise ValueError(
            f"field_kind invalide : {field_kind!r}. "
            f"Valeurs acceptees : {sorted(_VALID_FIELD_KINDS)}"
        )
    if measure is not None and measure not in _VALID_MEASURES:
        raise ValueError(
            f"measure invalide : {measure!r}. "
            f"Valeurs acceptees : {sorted(m for m in _VALID_MEASURES if m)}"
        )

    # Ratio circuit-breaker (Story 32.1 / AD-4): a ratio (ROAS, CTR, *_rate...) is
    # non-additive. Declaring it as an additive numeric metric (measure='sum') would let
    # it land in fact_daily_kpi, where SUM over days/channels is mathematically wrong.
    # A ratio averaged at view time (measure='average'/None) stays allowed; the guard
    # fires only for the additive-sum misuse. is_ratio_name is the shared detector (AD-4).
    from core.metric_semantics import is_ratio_name  # noqa: PLC0415

    if (
        field_kind == "metric"
        and data_type in _NUMERIC_TYPES
        and measure == "sum"
        and is_ratio_name(name)
    ):
        raise ValueError(
            f"Le champ {name!r} est un ratio (non additif) et ne peut pas etre declare "
            "comme metrique additive (measure='sum', AD-4) : sommer un ratio sur plusieurs "
            "jours ou canaux est mathematiquement faux et corromprait fact_daily_kpi. "
            "Utilisez measure='average' (ou aucun) pour un ratio recalcule a la volee, ou "
            "declarez son numerateur/denominateur via le dictionnaire de metriques."
        )

    # Story 44.7 finding #3: the row INSERT-or-revive AND the version append
    # live in ONE transaction (single commit at the end) -- matching
    # context_store.py's pattern -- so a failure appending the version row
    # rolls back the field row too, instead of leaving an orphan row with no
    # version 1.
    with conn.transaction():
        try:
            # This nested conn.transaction() becomes a SAVEPOINT (we are
            # already inside the outer transaction() above): psycopg3 nests
            # Connection.transaction() as SAVEPOINT / RELEASE / ROLLBACK TO
            # SAVEPOINT. Without it, a real Postgres connection would be left
            # in "current transaction is aborted" state after the unique-
            # violation below, and the revival queries in the except branch
            # would themselves fail.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.target_fields
                            (name, display_name, data_type, field_kind, measure, description,
                             created_by, is_default, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, 'draft')
                        RETURNING name, display_name, data_type, field_kind, measure,
                                  description, created_by, is_default, created_at,
                                  status, approved_at, approved_by, updated_at
                        """,
                        (name, display_name, data_type, field_kind, measure,
                         description, created_by),
                    )
                    row = cur.fetchone()
                    cols = [d[0] for d in cur.description]
                    result = _row_to_dict(cols, row)
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            # Story 44.7: the name identifies an existing row -- if it is
            # soft-deleted, revive it; otherwise _revive_deleted_target_field
            # re-raises a clear ValueError (genuine duplicate).
            result = _revive_deleted_target_field(
                conn,
                name=name,
                display_name=display_name,
                data_type=data_type,
                field_kind=field_kind,
                measure=measure,
                description=description,
                created_by=created_by,
            )

        # Story 44.7 finding #1: ALWAYS the MAX(version_number)+1 helper, on
        # both the fresh-INSERT path (no prior version row for a brand-new
        # name -> MAX is NULL -> next is 1) and the revival path (the name's
        # PK already carries version history from before its soft-delete --
        # a hardcoded version_number=1 here would collide with pk(name,
        # version_number) on this exact row).
        _append_target_field_version(
            conn, result, change_kind="created", diff=None, changed_by=created_by,
        )

    conn.commit()
    logger.info(
        "datamodel: target_field_created name=%s kind=%s type=%s by=%s status=draft",
        name, field_kind, data_type, created_by,
    )
    return result


# ---------------------------------------------------------------------------
# update_target_field
# ---------------------------------------------------------------------------


def update_target_field(
    name: str, patch: dict, conn, *, identity: str = "system"
) -> dict | None:
    """PATCH an existing target field. Returns updated dict or None if not found.

    Immutability rules:
      - 'name' change -> ValueError (immutable PK).
      - 'data_type' change -> ValueError (immutable post-creation).
      - 'field_kind' change -> ValueError (immutable post-creation).

    Patchable fields: display_name, measure, description.
    is_default is NOT patchable (system seeded fields stay is_default=True).

    Story 44.7: appends one app.target_fields_versions row per REAL change --
    change_kind='updated', diff={field: {"before": x, "after": y}} for every
    actually-changed patchable field. A no-op patch (empty patch dict, or a
    patch whose values match the current row) appends no version row.
    `identity` is the real caller identity (threaded from
    datamodel_api._patch_field's _check_auth) -- it is written to changed_by
    on the version row AND to the audit row, never the literal "system" (kept
    only as this parameter's default for callers that pass none).

    If `patch` carries a `restored_from` key (Story 44.8's PATCH-driven
    restore hint -- not a real target_fields column, ignored by the SET
    clause below), the version is recorded with change_kind='restored'
    instead of 'updated' and the hint is copied into the version's diff under
    key '_restored_from'. Story 44.8 finding #3: unlike a plain 'updated'
    version (which is only appended on a real diff), a 'restored' version is
    ALWAYS appended when `restored_from` is present, even if the restored
    snapshot's values equal the current row (diff falls back to
    {'_restored_from': <version>} alone) -- a restore must always leave a
    timeline trace, never a silent no-op.
    """
    # Immutability guards
    if "name" in patch:
        raise ValueError(
            "name est immuable : le nom d'un champ cible ne peut pas etre modifie "
            "apres creation."
        )
    if "data_type" in patch:
        raise ValueError(
            "data_type est immuable : le type de donnee d'un champ cible ne peut pas "
            "etre modifie apres creation."
        )
    if "field_kind" in patch:
        raise ValueError(
            "field_kind est immuable : la nature metrique/dimension ne peut pas etre "
            "modifiee apres creation."
        )

    # Story 44.8 finding #4: restored_from is validated by the API layer
    # (datamodel_api._patch_field) BEFORE this store fn is called -- by the
    # time it arrives here it is either absent or a plain int naming a real
    # (name, version_number) row in app.target_fields_versions. This store
    # never re-validates it; it only copies the int into the diff metadata.
    restored_from = patch.get("restored_from")

    # Validate patchable fields
    allowed = {"display_name", "measure", "description"}
    set_pairs: list[str] = []
    params: list = []

    for field in allowed:
        if field not in patch:
            continue
        val = patch[field]
        if field == "measure":
            if val is not None and val not in _VALID_MEASURES:
                raise ValueError(
                    f"measure invalide : {val!r}. "
                    f"Valeurs acceptees : {sorted(m for m in _VALID_MEASURES if m)}"
                )
            set_pairs.append("measure = %s")
            params.append(val)
        elif field == "display_name":
            val = (val or "").strip()
            if not val:
                raise ValueError("display_name ne peut pas etre vide")
            set_pairs.append("display_name = %s")
            params.append(val)
        elif field == "description":
            set_pairs.append("description = %s")
            params.append((val or "").strip() or None)

    # Fetch the BEFORE state. Also serves the "field not found" (404) check
    # and, when the patch is empty, the "nothing to update" early return.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, display_name, data_type, field_kind, measure,
                   description, created_by, is_default, created_at,
                   status, approved_at, approved_by, updated_at
            FROM app.target_fields WHERE name = %s
            """,
            (name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        before = _row_to_dict(cols, row)

    # Story 44.7 finding #4: a soft-deleted field is not patchable -- treat it
    # exactly like "not found" (HTTP 404), same as get_target_field/
    # list_target_fields already do for status='deleted'. (44.8's restore
    # flow only ever PATCHes live fields, so this guard does not block it.)
    if before.get("status") == "deleted":
        return None

    if not set_pairs:
        # Nothing to update — return current state (no version, no audit).
        return before

    params.append(name)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.target_fields
            SET {', '.join(set_pairs)}
            WHERE name = %s
            RETURNING name, display_name, data_type, field_kind, measure,
                      description, created_by, is_default, created_at,
                      status, approved_at, approved_by, updated_at
            """,  # noqa: S608
            params,
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        result = _row_to_dict(cols, row)

    # Story 44.7: version-append only on a REAL change (non-empty diff).
    # Story 44.8 finding #3: a restore is an exception to that rule -- when
    # `restored_from` is present the caller explicitly asked to restore a
    # snapshot, and that intent must leave a timeline trace EVEN IF the
    # restored values happen to already match the current row (e.g.
    # restoring the current version, or a version identical to it). Silently
    # doing nothing there would look like the restore failed.
    diff = _diff_target_field_patch(before, result)
    # `is not None`, not truthiness: a validated restored_from of 0 must not
    # silently degrade into a plain update (44.8 re-review; unreachable today
    # since version_number starts at 1, but the guard should not rely on it).
    if restored_from is not None:
        change_kind = "restored"
        diff["_restored_from"] = restored_from
        _append_target_field_version(
            conn, result, change_kind=change_kind, diff=diff, changed_by=identity,
        )
    elif diff:
        _append_target_field_version(
            conn, result, change_kind="updated", diff=diff, changed_by=identity,
        )

    conn.commit()

    # Audit (literal action string — no new ACTION_ constant per story notes)
    try:
        from core.audit import write_audit_row  # noqa: PLC0415

        # patched_fields lists only REAL columns: 'restored_from' is a hint,
        # not a field of app.target_fields -- it gets its own metadata key
        # (44.8 re-review: the immutable audit log must not claim a
        # nonexistent column was patched).
        audit_metadata: dict = {
            "name": name,
            "patched_fields": [k for k in patch if k != "restored_from"],
        }
        if restored_from is not None:
            audit_metadata["restored_from"] = restored_from
        write_audit_row(
            identity=identity,
            action="target_field.updated",
            provider_account="",
            connection_ref="",
            metadata=audit_metadata,
        )
    except Exception as exc:
        logger.warning("datamodel: audit_write_failed: %s", exc)

    logger.info("datamodel: target_field_updated name=%s fields=%s", name, list(patch.keys()))
    return result


# ---------------------------------------------------------------------------
# approve_target_field (Story 13.1)
# ---------------------------------------------------------------------------


def approve_target_field(name: str, identity: str, conn) -> dict | None:
    """Approve a target field (status draft -> approved).

    Idempotent: if the field is already 'approved', returns the current row
    without rewriting approved_at/approved_by and without inserting a new audit
    row (re-approving the same field must not pollute the immutable audit log).

    On a genuine draft -> approved transition:
    - Updates status, approved_at, approved_by on the field row.
    - Appends one row to app.target_field_approvals (append-only audit log).
    - Story 44.7: appends one app.target_fields_versions row
      (change_kind='approved'). The no-op (already-approved) path appends
      NOTHING (no version, no approvals row): idempotent re-approve.

    Returns the (possibly unchanged) field dict, or None if the field does not exist.
    """
    if not identity:
        identity = "anonymous"

    # 1. Fetch current state to decide whether a real transition is needed.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, display_name, data_type, field_kind, measure,
                   description, created_by, is_default, created_at,
                   status, approved_at, approved_by, updated_at
            FROM app.target_fields
            WHERE name = %s
            """,
            (name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        current = _row_to_dict(cols, row)

    # Story 44.7 finding #2: a soft-deleted field must NOT be resurrected by
    # approval -- treat it exactly like "not found" (HTTP 404), same as
    # get_target_field/list_target_fields already do for status='deleted'.
    if current.get("status") == "deleted":
        return None

    # 2. Idempotency check: already approved -> no-op (return current state, no audit row).
    if current.get("status") == "approved":
        logger.info(
            "datamodel: target_field_approve_noop name=%s already_approved_by=%s",
            name, current.get("approved_by"),
        )
        return current

    # 3. Real transition draft -> approved.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.target_fields
            SET status      = 'approved',
                approved_at = NOW(),
                approved_by = %s
            WHERE name = %s
            RETURNING name, display_name, data_type, field_kind, measure,
                      description, created_by, is_default, created_at,
                      status, approved_at, approved_by, updated_at
            """,
            (identity, name),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        result = _row_to_dict(cols, row)

    # 4. Append audit row (same connection, before commit).
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.target_field_approvals (field_name, action, decided_by)
            VALUES (%s, 'approved', %s)
            """,
            (name, identity),
        )

    # 5. Story 44.7: append version row (change_kind='approved').
    _append_target_field_version(
        conn, result, change_kind="approved", diff=None, changed_by=identity,
    )

    conn.commit()

    logger.info(
        "datamodel: target_field_approved name=%s by=%s",
        name, identity,
    )
    return result


# ---------------------------------------------------------------------------
# delete_target_field (Story 13.1)
# ---------------------------------------------------------------------------


class FieldInUseError(ValueError):
    """Raised when DELETE is attempted on a field with active mappings."""


def delete_target_field(name: str, identity: str, conn) -> None:
    """Soft-delete a target field from the dictionary (Story 44.7).

    Guards (unchanged from Story 13.1):
    - Refuses deletion if used_by_count > 0 (FieldInUseError -> HTTP 409).
    - Refuses deletion if is_default=TRUE (FieldInUseError -> HTTP 409).
    Raises ValueError if the field does not exist, or already status='deleted'
    (-> HTTP 404 -- an already-deleted field is not visible, so "not found" is
    the correct answer for a second delete attempt).

    Story 44.7: this is now a SOFT delete (UPDATE ... SET status='deleted'),
    not a hard DELETE -- the row and its full app.target_fields_versions
    history survive; only list_target_fields/get_target_field stop returning
    it. Appends one version row (change_kind='deleted') and one audit row
    ('target_field.deleted') carrying the real caller identity.

    Audit rows in app.target_field_approvals are intentionally preserved
    (historical journal, no FK -> target_fields).

    Recreating this name later (create_target_field) revives the row -- see
    that function's docstring.
    """
    # 1. Check existence, is_default, used_by_count, and current status.
    # FOR UPDATE OF tf (finding 44.7#11): locks the target_fields row for the
    # duration of this transaction so a concurrent mapping upsert cannot slip
    # in between this used_by_count read and the UPDATE below (both the
    # guard read and the soft-delete write see a consistent row).
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                tf.name,
                tf.is_default,
                (SELECT COUNT(*) FROM app.datastream_mappings dm
                 WHERE dm.target_field = tf.name) AS used_by_count,
                tf.status
            FROM app.target_fields tf
            WHERE tf.name = %s
            FOR UPDATE OF tf
            """,
            (name,),
        )
        row = cur.fetchone()

    if row is None or row[3] == "deleted":
        raise ValueError(f"Champ '{name}' introuvable")

    is_default = bool(row[1])
    if is_default:
        raise FieldInUseError(
            "Les champs par defaut du socle ne peuvent pas etre supprimes."
        )

    used_by_count = int(row[2] or 0)
    if used_by_count > 0:
        raise FieldInUseError(
            f"Impossible de supprimer le champ '{name}' : il est utilisé par "
            f"{used_by_count} mapping(s) actif(s). "
            f"Retirez les mappings avant de supprimer ce champ."
        )

    # 2. Soft-delete: flip status only. Audit rows in app.target_field_approvals
    # are intentionally left intact (historical trace; no FK constraint blocks
    # this -- see migration 050).
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.target_fields
            SET status = 'deleted', updated_at = now()
            WHERE name = %s
            RETURNING name, display_name, data_type, field_kind, measure,
                      description, created_by, is_default, created_at,
                      status, approved_at, approved_by, updated_at
            """,
            (name,),
        )
        after_row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        result = _row_to_dict(cols, after_row)

    # 3. Story 44.7: append version row (change_kind='deleted').
    _append_target_field_version(
        conn, result, change_kind="deleted", diff=None, changed_by=identity,
    )

    conn.commit()

    # 4. Audit (best-effort, separate connection -- same style as update_target_field).
    try:
        from core.audit import write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity=identity,
            action="target_field.deleted",
            provider_account="",
            connection_ref="",
            metadata={"name": name},
        )
    except Exception as exc:
        logger.warning("datamodel: audit_write_failed: %s", exc)

    logger.info("datamodel: target_field_deleted name=%s by=%s", name, identity)


# ---------------------------------------------------------------------------
# list_field_versions (Story 44.7 store fn; API route lands in Story 44.8)
# ---------------------------------------------------------------------------


# Story 44.8 finding #7: cap the unbounded timeline read. No UI pagination
# control this round -- this is a server-side safety limit, not a product
# feature; revisit if a field's history legitimately exceeds this.
_FIELD_VERSIONS_LIMIT = 200


def list_field_versions(name: str, conn) -> list[dict]:
    """Return all app.target_fields_versions rows for `name`, newest first.

    History outlives visibility: this reads target_fields_versions directly
    (never filters on the parent row's status), so a soft-deleted field's
    full lifecycle remains inspectable.

    Capped at _FIELD_VERSIONS_LIMIT rows (Story 44.8 finding #7) -- no UI
    pagination this round.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, version_number, display_name, data_type, field_kind,
                   measure, description, created_by, is_default, created_at,
                   status, approved_at, approved_by, updated_at,
                   change_kind, diff, changed_by, changed_at
            FROM app.target_fields_versions
            WHERE name = %s
            ORDER BY version_number DESC
            LIMIT %s
            """,
            (name, _FIELD_VERSIONS_LIMIT),
        )
        cols = [d[0] for d in cur.description]
        return [_row_to_dict(cols, row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# list_target_field_nodes (Story 44.10 -- knowledge-graph projection)
# ---------------------------------------------------------------------------


def list_target_field_nodes(
    conn,
    *,
    names: Sequence[str] = (),
    include_approved: bool = False,
) -> list[dict]:
    """Return dictionary fields as knowledge-graph node rows (Story 44.10).

    The mindmap does NOT draw the whole dictionary by default -- hundreds of
    unlinked fields would drown the knowledge nodes. The inclusion rule is
    therefore: the fields named in ``names`` (those referenced by at least one
    app.context_graph edge) plus, when ``include_approved`` is set
    (``?include_fields=all``), every APPROVED field. The two sets are UNIONed
    rather than exclusive: a draft field that already carries an edge must
    still appear under ``include_fields=all``, otherwise widening the view
    would make an existing edge dangle.

    Soft-deleted fields (status='deleted', migration 107) are NEVER returned,
    in any mode -- same rule as list_target_fields.

    ``version_number`` is the MAX of app.target_fields_versions for the field,
    floored at 1: fields seeded before 44.7 have no version rows at all, and
    reporting v0 for them would read as "never saved" rather than "no history
    captured yet". This mirrors the COALESCE(MAX(...), 1) that list_topics /
    list_procedures use, and deliberately NOT list_schema_docs' "+1" form
    (which exists only because schema_context snapshots the PRE-update body).

    Returns, per row: name, display_name, description, field_kind, created_by,
    status, version_number.
    """
    name_list = list(dict.fromkeys(names))
    if not name_list and not include_approved:
        # Nothing referenced and no opt-in: no query, no rows.
        return []

    conditions = ["tf.status != 'deleted'"]
    params: list = []

    if include_approved and name_list:
        conditions.append("(tf.status = 'approved' OR tf.name = ANY(%s))")
        params.append(name_list)
    elif include_approved:
        conditions.append("tf.status = 'approved'")
    else:
        conditions.append("tf.name = ANY(%s)")
        params.append(name_list)

    where_clause = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT
            tf.name,
            tf.display_name,
            tf.description,
            tf.field_kind,
            tf.created_by,
            tf.status,
            (
                SELECT COALESCE(MAX(v.version_number), 1)
                FROM app.target_fields_versions v
                WHERE v.name = tf.name
            ) AS version_number
        FROM app.target_fields tf
        {where_clause}
        ORDER BY tf.name ASC
    """  # noqa: S608

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            rec = _row_to_dict(cols, row)
            rec["version_number"] = int(rec.get("version_number") or 1)
            rows.append(rec)
    return rows


def list_mappings_for_target_field(
    conn,
    *,
    target_field: str,
    project_id: str | None = None,
) -> list[dict]:
    """Return the datastream mappings that FEED ``target_field`` (Story 44.10).

    Powers the knowledge graph's "Fed by" panel: which source field of which
    datastream (and therefore which connector module) lands in this dictionary
    field. Deliberately a thin read -- get_target_field's used_by view carries
    last-pull verdicts, currency provenance and conflict detection, none of
    which the graph drawer shows; running that whole pipeline for a read-only
    list would be a lot of work thrown away.

    ``project_id``, when given, restricts to datastreams of that project.
    """
    conditions = ["dm.target_field = %s"]
    params: list = [target_field]
    if project_id:
        conditions.append("ds.project_id = %s")
        params.append(project_id)
    where_clause = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT
            dm.datastream_id,
            ds.name          AS datastream_name,
            ds.module_name,
            ds.project_id,
            ds.enabled,
            dm.source_field,
            dm.is_key_column
        FROM app.datastream_mappings dm
        JOIN app.datastreams ds ON ds.id = dm.datastream_id
        {where_clause}
        ORDER BY ds.module_name ASC, ds.name ASC, dm.source_field ASC
    """  # noqa: S608

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [_row_to_dict(cols, row) for row in cur.fetchall()]


def target_field_exists(name: str, conn) -> bool:
    """Return True iff `name` exists in app.target_fields in ANY status.

    Story 44.8 finding #5: history must be servable for soft-deleted fields
    (status='deleted' rows still exist), so this deliberately does NOT filter
    on status -- it only tells the caller whether the name ever existed at
    all, to distinguish a genuine 404 from "no history yet".
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app.target_fields WHERE name = %s", (name,))
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# upsert_mapping
# ---------------------------------------------------------------------------


def upsert_mapping(
    datastream_id: str,
    source_field: str,
    target_field_name: str | None,
    identity: str,
    conn,
) -> dict:
    """Upsert a datastream_mappings row. Returns the current mapping dict.

    Args:
        datastream_id:     The datastream to update.
        source_field:      The source field name (PK component).
        target_field_name: Target field name to map to, or None to unmap.
        identity:          Caller identity for audit.
        conn:              Open psycopg connection.

    Raises:
        ValueError if datastream_id or source_field is empty.
        ValueError if target_field_name identifies a soft-deleted field
            (Story 44.7 finding #8: a mapping must never bind to a field the
            dictionary no longer shows -- the FK alone does not catch this,
            since the row still physically exists after a soft delete).
        psycopg FK violation if target_field_name does not exist in target_fields.
    """
    if not datastream_id:
        raise ValueError("datastream_id est requis")
    if not source_field:
        raise ValueError("source_field est requis")

    if target_field_name is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM app.target_fields WHERE name = %s",
                (target_field_name,),
            )
            row = cur.fetchone()
        if row is not None and row[0] == "deleted":
            raise ValueError(
                f"Impossible de mapper vers '{target_field_name}' : ce champ a ete "
                "supprime du dictionnaire."
            )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT target_field FROM app.datastream_mappings
            WHERE datastream_id = %s AND source_field = %s
            """,
            (datastream_id, source_field),
        )
        existing = cur.fetchone()
    before_target = existing[0] if existing is not None else None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_mappings
                (datastream_id, source_field, target_field, is_key_column)
            VALUES (%s, %s, %s, FALSE)
            ON CONFLICT (datastream_id, source_field) DO UPDATE
                SET target_field = EXCLUDED.target_field
            RETURNING datastream_id, source_field, target_field, is_key_column, created_at
            """,
            (datastream_id, source_field, target_field_name),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        result = _row_to_dict(cols, row)
    conn.commit()

    changed = before_target != target_field_name

    # Audit (Story 44.9: before/after; no-op upserts write no audit row)
    if changed:
        # Owning project id in the metadata (44.9 re-review, non-blocking):
        # /api/audit is org-wide with no metadata filter today, so a future
        # scoped "last mapping change" surface needs a project key to filter
        # on. Best-effort: a lookup failure degrades to null, never blocks
        # the audit row itself.
        project_id: str | None = None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT project_id FROM app.datastreams WHERE id = %s",
                    (datastream_id,),
                )
                ds_row = cur.fetchone()
            if ds_row is not None:
                project_id = ds_row[0]
        except Exception:
            logger.warning(
                "datamodel: mapping_audit_project_lookup_failed ds=%s", datastream_id
            )
        try:
            from core.audit import write_audit_row  # noqa: PLC0415

            write_audit_row(
                identity=identity or "anonymous",
                action="mapping.updated",
                provider_account="",
                connection_ref="",
                metadata={
                    "datastream_id": datastream_id,
                    "project_id": project_id,
                    "source_field": source_field,
                    "before": before_target,
                    "after": target_field_name,
                    "changed": changed,
                },
            )
        except Exception as exc:
            logger.warning("datamodel: audit_write_failed: %s", exc)
    else:
        logger.info(
            "datamodel: mapping_upsert_noop ds=%s src=%s target=%s (unchanged, no audit row)",
            datastream_id, source_field, target_field_name,
        )

    logger.info(
        "datamodel: mapping_upserted ds=%s src=%s before=%s -> after=%s",
        datastream_id, source_field, before_target, target_field_name,
    )
    return result
