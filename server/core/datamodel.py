"""toorow -- Data model (target fields + mappings) CRUD (Story 8.5, Epic 8).

Provides:
  list_target_fields(project_id?, kind?, usage?, conn)  -> list[dict]
  get_target_field(name, conn)                          -> dict | None
  create_target_field(data, created_by, conn)           -> dict
  update_target_field(name, patch, conn)                -> dict | None
  approve_target_field(name, identity, conn)            -> dict  [Story 13.1]
  delete_target_field(name, conn)                       -> None  [Story 13.1]
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

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging
import re

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
_TS_COLS = {"created_at", "approved_at", "updated_at"}

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


def create_target_field(data: dict, created_by: str, conn) -> dict:
    """INSERT a new user-defined target field. Returns the created dict.

    Required keys: name (snake_case), display_name, data_type, field_kind.
    Optional: measure, description.

    Raises ValueError on validation failure.
    NOTE: is_default is always False for user-created fields.
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
            (name, display_name, data_type, field_kind, measure, description, created_by),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        result = _row_to_dict(cols, row)
    conn.commit()
    logger.info(
        "datamodel: target_field_created name=%s kind=%s type=%s by=%s status=draft",
        name, field_kind, data_type, created_by,
    )
    return result


# ---------------------------------------------------------------------------
# update_target_field
# ---------------------------------------------------------------------------


def update_target_field(name: str, patch: dict, conn) -> dict | None:
    """PATCH an existing target field. Returns updated dict or None if not found.

    Immutability rules:
      - 'name' change -> ValueError (immutable PK).
      - 'data_type' change -> ValueError (immutable post-creation).

    Patchable fields: display_name, measure, description.
    is_default is NOT patchable (system seeded fields stay is_default=True).
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

    if not set_pairs:
        # Nothing to update — return current state
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
            return _row_to_dict(cols, row)

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
    conn.commit()

    # Audit (literal action string — no new ACTION_ constant per story notes)
    try:
        from core.audit import write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity="system",
            action="target_field.updated",
            provider_account="",
            connection_ref="",
            metadata={"name": name, "patched_fields": list(patch.keys())},
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


def delete_target_field(name: str, conn) -> None:
    """DELETE a target field from the dictionary.

    Guards:
    - Refuses deletion if used_by_count > 0 (FieldInUseError -> HTTP 409).
    - Refuses deletion if is_default=TRUE (FieldInUseError -> HTTP 409).
    Raises ValueError if the field does not exist (-> HTTP 404).

    Audit rows in app.target_field_approvals are intentionally preserved after
    deletion: they form a historical journal (no FK -> target_fields, so they
    survive as orphan rows tracking the lifecycle of a deleted field).
    """
    # 1. Check existence, is_default, and used_by_count in one query
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                tf.name,
                tf.is_default,
                (SELECT COUNT(*) FROM app.datastream_mappings dm
                 WHERE dm.target_field = tf.name) AS used_by_count
            FROM app.target_fields tf
            WHERE tf.name = %s
            """,
            (name,),
        )
        row = cur.fetchone()

    if row is None:
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

    # 2. Delete only the field row.
    # Audit rows in app.target_field_approvals are intentionally left intact
    # (historical trace; no FK constraint blocks this -- see migration 050).
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.target_fields WHERE name = %s",
            (name,),
        )

    conn.commit()
    logger.info("datamodel: target_field_deleted name=%s", name)


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
        psycopg FK violation if target_field_name does not exist in target_fields.
    """
    if not datastream_id:
        raise ValueError("datastream_id est requis")
    if not source_field:
        raise ValueError("source_field est requis")

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

    # Audit
    try:
        from core.audit import write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity=identity or "anonymous",
            action="mapping.updated",
            provider_account="",
            connection_ref="",
            metadata={
                "datastream_id": datastream_id,
                "source_field": source_field,
                "target_field": target_field_name,
            },
        )
    except Exception as exc:
        logger.warning("datamodel: audit_write_failed: %s", exc)

    logger.info(
        "datamodel: mapping_upserted ds=%s src=%s -> target=%s",
        datastream_id, source_field, target_field_name,
    )
    return result
