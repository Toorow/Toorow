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

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : elles etaient retapees en dur a l appel, donc rien
# ne pouvait distinguer une action d une faute de frappe. Declarees ici,
# a cote du code qui les ecrit.
ACTION_TARGET_FIELD_UPDATED = declare_action("target_field.updated")
ACTION_TARGET_FIELD_DELETED = declare_action("target_field.deleted")
ACTION_MAPPING_UPDATED = declare_action("mapping.updated")


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
                        "This field is fed by distinct connectors whose monetary "
                        "units may differ (e.g. raw USD vs normalized EUR). "
                        "Check currency consistency before aggregating."
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
                        "This monetary field has no resolvable source currency: no "
                        "Datastream declares its currency. Fail-closed: an amount without "
                        "a currency is neither summed nor converted until its currency is "
                        "declared (bind a source currency per Datastream)."
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
    # AI-132 : `report_timezone_has_lever` etait LU ICI et ECRIT NULLE PART.
    #
    # Les deux cles ne sont produites par aucun chemin du depot (mesure : deux
    # occurrences, ces deux lectures). `has_lever` valait donc toujours False et
    # toute la branche levier du moteur etait inatteignable -- la classe AI-85,
    # et la raison pour laquelle le critere [2] de `reporting-timezone.md` ne
    # pouvait pas se fermer. Les 51 tests timezone etaient verts parce qu'ils
    # passent `has_lever` EN ENTREE du moteur pur : aucun n'exigeait un
    # producteur.
    #
    # Le producteur est la DECLARATION du connecteur, resolue par module (la
    # ligne used-by le porte deja) et non par datastream, ce qui couterait un
    # aller-retour en base par flux pour ce que le manifeste sait deja.
    #
    # Import LOCAL : le bloc au-dessus n'importe `report_timezone` que dans la
    # branche `is_monetary`, et ce signal-ci n'est deliberement PAS gate dessus
    # (un decalage de jour entre sessions non monetaires est exactement le cas a
    # signaler). S'appuyer sur cet import laisserait le nom non lie.
    from core import report_timezone as _report_timezone  # noqa: PLC0415

    # AI-167 : le fuseau vient de ce qu'un RUN a observe, faute de quoi il ne vient
    # de nulle part.
    #
    # `_stream_report_timezone` le dit lui-meme : « today the used-by SELECT carries
    # none of these ». Donc en production chaque flux etait non placable, et le signal
    # ne pouvait dire que « N flux sans fuseau resolvable » -- jamais « ces flux tirent
    # leur jour sur des horloges differentes », qui est precisement le signal que
    # l'epic demande. La branche existait et aucun parcours ne l'atteignait.
    #
    # La zone existe maintenant : le pull la rend, le worker l'enregistre (AI-161).
    # UNE requete pour tout le champ, pas une par flux. La cle generique de la ligne
    # used-by garde la priorite : le jour ou la SELECT la portera, elle gagnera sans
    # qu'on touche ici.
    from core.time_boundary import observed_zones_for_datastreams  # noqa: PLC0415

    _observed = observed_zones_for_datastreams(
        project_id, [ub.get("datastream_id") for ub in used_by]
    )
    tz_streams = [
        {
            "datastream": ub.get("datastream_name") or ub.get("module_name"),
            "report_timezone": (
                _stream_report_timezone(ub) or _observed.get(str(ub.get("datastream_id") or ""))
            ),
            **_report_timezone.lever_for_module(ub.get("module_name")),
        }
        for ub in used_by
    ]
    # 2026-08-01 : cette garde recreait A L'EXTERIEUR du moteur l'exclusion que la
    # story 48.3 avait retiree A L'INTERIEUR en la nommant « the defect »
    # (timezone_signal.py:180-184). En ne comptant que les fuseaux CONNUS, elle
    # n'appelait jamais le moteur dans le cas meme que 48.3 repare : deux flux
    # d'accord plus trois non placables. La reparation etait donc livree et
    # inatteignable par tout parcours -- la classe AI-85.
    #
    # Le moteur est PUR et sait deja se taire : il rend None quand il y a moins de
    # deux fuseaux distincts ET rien de non placable (timezone_signal.py:195-197).
    # C'est a lui de decider s'il y a un signal, pas a son appelant de le pre-juger.
    if len(tz_streams) > 1:
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
                    "The aggregation semantics (measure) are not defined for this "
                    "numeric field. Consumers could apply the wrong aggregation "
                    "(sum vs average). Set 'measure' to make rollups safe."
                ),
                "affected_streams": affected,
            }
        )

    return conflicts


# ---------------------------------------------------------------------------
# list_target_fields
# ---------------------------------------------------------------------------


def _active_target_bindings_cte(project_id: str | None) -> tuple[str, list[str]]:
    """Return the canonical active-mapping projection and its scope params.

    Story 8.5 originally read only ``app.datastream_mappings``. Universal
    Datastreams publish immutable mappings through
    ``app.datastream_mapping_versions`` instead. This CTE is the single read
    seam for both brownfield representations. Exact dual-store attributions are
    deduplicated with the current version preferred, while non-overlapping legacy
    rows remain visible during brownfield transitions. The projection never
    advances a pointer and ignores non-current, non-executable, suggested,
    blocking, and excluded bindings.
    """
    project_clause = ""
    params: list[str] = []
    if project_id:
        project_clause = "AND ds.project_id = %s"
        # The scope predicate is applied independently to both physical stores
        # so a project-scoped read never materializes another project's rows.
        params = [project_id, project_id]

    sql = f"""
        WITH legacy_bindings AS (
            SELECT
                dm.datastream_id,
                ds.name AS datastream_name,
                ds.module_name,
                ds.project_id,
                ds.enabled,
                dm.source_field,
                dm.target_field,
                dm.is_key_column,
                'legacy'::text AS binding_source
            FROM app.datastream_mappings dm
            JOIN app.datastreams ds ON ds.id = dm.datastream_id
            WHERE dm.target_field IS NOT NULL
              AND ds.archived_at IS NULL

              {project_clause}
        ),
        versioned_bindings AS (
            SELECT
                ds.id AS datastream_id,
                ds.name AS datastream_name,
                ds.module_name,
                ds.project_id,
                ds.enabled,
                source_field.value ->> 'field_id' AS source_field,
                -- The MDM entry REFINES the declared target; it does not replace
                -- it. `dictionary_field_name` is optional by design
                -- (`canonical_field_registry.py:286`: "optionally derives the
                -- field from the governed dictionary") and this expression used
                -- to consume it as mandatory: the moment a binding carried an
                -- `mdm_target`, the answer became that column and NOTHING else,
                -- so an entry that derives from no dictionary row silently took
                -- the whole binding out of the data model through the
                -- `IS NOT NULL` below. Measured 2026-08-17: 418 confirmed
                -- bindings carried BOTH keys, 0 carried `mdm_target` alone, and
                -- 0 survived. COALESCE restores the documented optionality --
                -- and when there is no `mdm_target` the LEFT JOIN yields NULL,
                -- which is exactly what the old ELSE branch did.
                COALESCE(
                    mdm.dictionary_field_name,
                    NULLIF(source_field.value -> 'binding' ->> 'canonical_target', '')
                ) AS target_field,
                CASE
                    WHEN jsonb_typeof(mv.mapping_payload -> 'grain') = 'array'
                    THEN (mv.mapping_payload -> 'grain')
                         ? (source_field.value ->> 'field_id')
                    ELSE FALSE
                END AS is_key_column,
                'versioned'::text AS binding_source
            FROM app.datastreams ds
            JOIN app.datastream_mapping_versions mv
              ON mv.id = ds.current_mapping_version_id
             AND mv.datastream_id = ds.id
             AND mv.project_id = ds.project_id
             AND mv.executable = TRUE
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(mv.mapping_payload -> 'fields') = 'array'
                    THEN mv.mapping_payload -> 'fields'
                    ELSE '[]'::jsonb
                END
            ) AS source_field(value)
            LEFT JOIN app.mdm_canonical_fields mdm
              ON mdm.id = NULLIF(source_field.value -> 'binding' ->> 'mdm_target', '')
             AND mdm.status = 'active'
             AND (mdm.project_id IS NULL OR mdm.project_id = ds.project_id)
            WHERE ds.archived_at IS NULL
              AND NULLIF(source_field.value ->> 'field_id', '') IS NOT NULL
              AND source_field.value -> 'binding' ->> 'status' IN ('confirmed', 'resolved')
              AND COALESCE(
                    mdm.dictionary_field_name,
                    NULLIF(source_field.value -> 'binding' ->> 'canonical_target', '')
                  ) IS NOT NULL
              {project_clause}
        ),
        ranked_bindings AS (
            SELECT
                combined.*,
                ROW_NUMBER() OVER (
                    PARTITION BY project_id, datastream_id, source_field, target_field
                    ORDER BY CASE binding_source WHEN 'versioned' THEN 0 ELSE 1 END
                ) AS representation_rank
            FROM (
                SELECT * FROM legacy_bindings
                UNION ALL
                SELECT * FROM versioned_bindings
            ) combined
        ),
        active_target_bindings AS (
            SELECT
                datastream_id,
                datastream_name,
                module_name,
                project_id,
                enabled,
                source_field,
                target_field,
                is_key_column,
                binding_source
            FROM ranked_bindings
            WHERE representation_rank = 1
        )
    """
    return sql, params


def list_target_fields(
    conn,
    *,
    project_id: str | None = None,
    kind: str | None = None,
    usage: str | None = None,
    module: str | None = None,
) -> list[dict]:
    """Return target fields with project-scoped active Datastream attribution.

    ``used_by_count`` counts distinct Datastreams, not physical mapping rows.
    ``used_by`` is a compact Datastream summary for the list UI. Both legacy
    and current immutable mappings flow through the same projection.
    """
    bindings_cte, params = _active_target_bindings_cte(project_id)

    conditions = ["tf.status != 'deleted'"]
    if module is not None:
        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM active_target_bindings module_binding
                WHERE module_binding.target_field = tf.name
                  AND module_binding.module_name = %s
            )
            """
        )
        params.append(module)
    if kind is not None:
        conditions.append("tf.field_kind = %s")
        params.append(kind)

    target_where = "WHERE " + " AND ".join(conditions)
    usage_where = ""
    if usage == "used":
        usage_where = "WHERE used_by_count > 0"
    elif usage == "unmapped":
        # API compatibility: the source-facing wire value remains `unmapped`,
        # while the semantic-target UI calls this honest state `Not used`.
        usage_where = "WHERE used_by_count = 0"

    sql = f"""
        {bindings_cte}
        SELECT *
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
                (
                    SELECT COUNT(DISTINCT binding.datastream_id)
                    FROM active_target_bindings binding
                    WHERE binding.target_field = tf.name
                ) AS used_by_count,
                COALESCE(
                    (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'datastream_id', attribution.datastream_id,
                                'datastream_name', attribution.datastream_name,
                                'module_name', attribution.module_name,
                                'enabled', attribution.enabled
                            )
                            ORDER BY attribution.datastream_name, attribution.datastream_id
                        )
                        FROM (
                            SELECT DISTINCT ON (binding.datastream_id)
                                binding.datastream_id,
                                binding.datastream_name,
                                binding.module_name,
                                binding.enabled
                            FROM active_target_bindings binding
                            WHERE binding.target_field = tf.name
                            ORDER BY binding.datastream_id, binding.datastream_name
                        ) attribution
                    ),
                    '[]'::jsonb
                ) AS used_by
            FROM app.target_fields tf
            {target_where}
        ) fields
        {usage_where}
        ORDER BY field_kind ASC, name ASC
    """  # noqa: S608

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        results = []
        for row in cur.fetchall():
            rec = _row_to_dict(cols, row)
            rec["used_by_count"] = int(rec.get("used_by_count") or 0)
            if not isinstance(rec.get("used_by"), list):
                rec["used_by"] = []
            results.append(rec)
    return results

# ---------------------------------------------------------------------------
# get_target_field (with used-by detail + conflict detection)
# ---------------------------------------------------------------------------


def get_target_field(
    name: str,
    conn,
    *,
    project_id: str | None = None,
) -> dict | None:
    """Return one field and its active Datastream attribution.

    When ``project_id`` is supplied, every attribution row is scoped before it
    is materialized. Callers that intentionally need a platform-wide conflict
    view may omit it for backward compatibility.
    """
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

    bindings_cte, binding_params = _active_target_bindings_cte(project_id)
    params: list = [*binding_params, name]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            {bindings_cte}
            SELECT
                binding.datastream_id,
                binding.datastream_name,
                binding.module_name,
                binding.project_id,
                binding.enabled,
                binding.source_field,
                lp.loaded_at AS last_loaded_at,
                lp.verdict AS last_verdict,
                fx.resolved_source_currency AS source_currency,
                binding.binding_source
            FROM active_target_bindings binding
            LEFT JOIN LATERAL (
                SELECT pv.verified_at AS loaded_at, pv.verdict
                FROM app.pull_jobs pj
                JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                WHERE pj.datastream_id = binding.datastream_id
                ORDER BY pv.verified_at DESC
                LIMIT 1
            ) lp ON true
            -- The DECLARED source currency, read from the governed projection
            -- (migration 282) rather than from `app.fx_conflict_resolutions`,
            -- which migration 145 dethroned and 282 sealed. Same three join keys,
            -- same column: what changed is that the value now comes from a
            -- published Rule Set version that names who declared it.
            LEFT JOIN app.fx_source_currency_bindings_v fx
                   ON fx.project_id = binding.project_id
                  AND fx.target_field = binding.target_field
                  AND fx.source_module = binding.module_name
            WHERE binding.target_field = %s
            ORDER BY binding.project_id ASC,
                     binding.datastream_name ASC,
                     binding.source_field ASC
            """,  # noqa: S608
            params,
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

            ds_id = rec.get("datastream_id")
            if ds_id is not None:
                try:
                    from core.report_timezone import (  # noqa: PLC0415
                        report_timezone_for_datastream,
                    )

                    zone = report_timezone_for_datastream(str(ds_id))
                except Exception:  # noqa: BLE001
                    zone = None
                if isinstance(zone, str) and zone.strip():
                    rec["report_timezone"] = zone.strip()
            used_by.append(rec)

    ub_projects = {ub.get("project_id") for ub in used_by if ub.get("project_id")}
    detect_project_id = next(iter(ub_projects)) if len(ub_projects) == 1 else None
    conflicts = _detect_conflicts(field, used_by, project_id=detect_project_id)

    field["used_by"] = used_by
    field["used_by_count"] = len(
        {row.get("datastream_id") for row in used_by if row.get("datastream_id")}
    )
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
            "name must be snake_case (lowercase letters, digits, underscores; "
            "starts with a letter): " + repr(name)
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
            f"Field {name!r} is a ratio (non-additive) and cannot be declared "
            "comme metrique additive (measure='sum', AD-4) : sommer un ratio sur plusieurs "
            "jours ou canaux est mathematiquement faux et corromprait fact_daily_kpi. "
            "Use measure='average' (or none) for a ratio recomputed on the fly, or "
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
            "name is immutable: the name of a target field cannot be changed "
            "apres creation."
        )
    if "data_type" in patch:
        raise ValueError(
            "data_type is immutable: the data type of a target field cannot "
            "etre modifie apres creation."
        )
    if "field_kind" in patch:
        raise ValueError(
            "field_kind is immutable: the metric/dimension nature cannot be "
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
                raise ValueError("display_name cannot be empty")
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
            action=ACTION_TARGET_FIELD_UPDATED,
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
        raise ValueError(f"Field '{name}' not found")

    is_default = bool(row[1])
    if is_default:
        raise FieldInUseError(
            "The platform default fields cannot be deleted."
        )

    used_by_count = int(row[2] or 0)
    if used_by_count > 0:
        raise FieldInUseError(
            f"Cannot delete field '{name}': it is used by "
            f"{used_by_count} active mapping(s). "
            f"Remove those mappings before deleting this field."
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
            action=ACTION_TARGET_FIELD_DELETED,
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
    """Return active mappings feeding one target field.

    The Knowledge Graph and Semantic model now share the same legacy/versioned
    projection, so neither surface can silently lose Universal Datastreams.
    """
    bindings_cte, params = _active_target_bindings_cte(project_id)
    params.append(target_field)
    sql = f"""
        {bindings_cte}
        SELECT
            binding.datastream_id,
            binding.datastream_name,
            binding.module_name,
            binding.project_id,
            binding.enabled,
            binding.source_field,
            binding.is_key_column,
            binding.binding_source
        FROM active_target_bindings binding
        WHERE binding.target_field = %s
        ORDER BY binding.module_name ASC,
                 binding.datastream_name ASC,
                 binding.source_field ASC
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
                action=ACTION_MAPPING_UPDATED,
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
