"""toorow -- MDM conflict resolutions (Story 13.2, Epic 13).

Provides:
  list_conflicts(project_id, conn)                          -> list[dict]
  list_fx_resolutions(project_id, conn)                     -> list[dict]
  get_fx_resolution(project_id, target_field, source_module, conn) -> dict | None
  upsert_fx_resolution(project_id, target_field, source_module,
                       resolved_source_currency, decided_by, note, conn) -> dict
  delete_fx_resolution(project_id, target_field, source_module, conn) -> None

Design decisions:
  - AD-6: AUCUNE conversion FX dans ce fichier Python.
    La table app.fx_conflict_resolutions DONNE une donnée à dbt.
    C'est dbt staging (stg_*_daily.sql) qui fait le JOIN FX en utilisant
    COALESCE(res.resolved_source_currency, raw.cost_source_currency).
  - AD-5: endpoints REST dénégation cross-projet (identity_has_project_access).
  - Résolution NON rétroactive: elle s'applique à la prochaine publication/reprocess.
  - Réutilise _detect_conflicts de datamodel.py (AI-53 -- PAS de seconde détection).
  - MEASURE_NULL resolution = PATCH sur app.target_fields.measure via update_target_field
    (API existante -- DRY, pas de table dédiée).

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid ISO 4217 currency codes (subset, extended on demand)
# ---------------------------------------------------------------------------

_VALID_CURRENCIES: frozenset[str] = frozenset(
    [
        "EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "JPY", "CAD", "AUD",
        "NZD", "SGD", "HKD", "CNY", "BRL", "MXN", "PLN", "CZK", "HUF", "RON",
        "TRY", "ZAR", "INR", "KRW", "IDR", "THB", "MYR", "PHP", "AED", "SAR",
    ]
)


def _validate_currency(currency: str, field_label: str) -> None:
    """Raise ValueError if currency is not in the known subset."""
    if not currency or not currency.strip():
        raise ValueError(f"{field_label} est requis")
    if currency.upper() not in _VALID_CURRENCIES:
        raise ValueError(
            f"{field_label} invalide : {currency!r}. "
            f"Codes ISO 4217 acceptes : {sorted(_VALID_CURRENCIES)}"
        )


# ---------------------------------------------------------------------------
# list_conflicts — réutilise _detect_conflicts de datamodel.py (AI-53)
# ---------------------------------------------------------------------------


def list_conflicts(project_id: str | None, conn) -> list[dict]:
    """Return all CURRENCY_CONFLICT, CURRENCY_GAP and MEASURE_NULL conflicts, optionally
    scoped to a project.

    Réutilise get_target_field (qui appelle _detect_conflicts) sans dupliquer
    la logique de détection (AI-53).

    Returns:
        list of conflict dicts enriched with field metadata and existing resolution
        (resolution=None when no resolution exists yet):
            {
                field: {name, display_name, data_type, field_kind, measure, status},
                conflict: {code, message, affected_streams},
                resolution: dict | None   (FX resolution row, or None)
            }
    """
    from core.datamodel import get_target_field, list_target_fields  # noqa: PLC0415

    # Scope to project when given; otherwise iterate all fields
    all_fields = list_target_fields(conn, project_id=project_id)

    result: list[dict] = []
    for field_summary in all_fields:
        name = field_summary["name"]
        # get_target_field returns full detail incl. used_by and conflicts
        detail = get_target_field(name, conn)
        if detail is None:
            continue
        conflicts = detail.get("conflicts") or []
        if not conflicts:
            continue

        # Fetch existing resolutions for this field
        existing_resolutions = _fetch_resolutions_for_field(
            project_id=project_id, target_field=name, conn=conn
        )
        # Index by source_module for quick lookup
        res_by_module: dict[str, dict] = {
            r["source_module"]: r for r in existing_resolutions
        }

        field_meta = {
            "name": detail["name"],
            "display_name": detail["display_name"],
            "data_type": detail["data_type"],
            "field_kind": detail["field_kind"],
            "measure": detail["measure"],
            "status": detail.get("status"),
            "used_by": detail.get("used_by", []),
        }

        for conflict in conflicts:
            code = conflict["code"]
            # CURRENCY_CONFLICT and CURRENCY_GAP (Story 39.3) are both currency conflicts
            # resolved by binding a per-stream source currency (Epic 13 FX resolution). Render
            # them WITH per-module resolutions -- not via the MEASURE_NULL `else` branch, which
            # would drop the resolutions_by_module the UI needs to bind a currency.
            if code in ("CURRENCY_CONFLICT", "CURRENCY_GAP"):
                # Find which modules are affected
                used_by = detail.get("used_by", [])
                modules_affected = list(
                    {ub.get("module_name") for ub in used_by if ub.get("module_name")}
                )
                result.append(
                    {
                        "field": field_meta,
                        "conflict": conflict,
                        "resolutions_by_module": {
                            m: res_by_module.get(m) for m in modules_affected
                        },
                    }
                )
            elif code == "TIMEZONE_DAY_OFFSET":
                # TIMEZONE_DAY_OFFSET (Story 39.8) is an ADVISORY (severity='advisory'), not a
                # refusal: a cross-source day-offset does not make a total wrong, only a daily
                # comparison possibly misaligned. It is NOT a per-module currency bind, so it
                # renders with an empty resolutions_by_module -- the user acts at the SOURCE
                # (when a lever exists) or accepts the honest "fixed, no lever" posture. Rendered
                # EXPLICITLY here (not dumped into the MEASURE_NULL `else` branch) so the surface
                # can present it as advisory provenance, never gate an aggregation on it.
                result.append(
                    {
                        "field": field_meta,
                        "conflict": conflict,
                        "resolutions_by_module": {},
                    }
                )
            else:
                # MEASURE_NULL -- no FX resolution, resolution = patch measure.
                # TIMEZONE_GAP (Story 39.7) also falls here: it is NOT a per-module currency
                # bind (resolvable_via='source_timezone_declaration', not 'fx_conversion'), so
                # it renders with an empty resolutions_by_module -- the source zone is declared
                # once per stream via the time_context contract, not resolved per module here.
                result.append(
                    {
                        "field": field_meta,
                        "conflict": conflict,
                        "resolutions_by_module": {},
                    }
                )

    return result


def _fetch_resolutions_for_field(
    project_id: str | None, target_field: str, conn
) -> list[dict]:
    """Fetch existing fx_conflict_resolutions for a given field, optionally project-scoped."""
    params: list = [target_field]
    where_extra = ""
    if project_id:
        where_extra = " AND project_id = %s"
        params.append(project_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, project_id, target_field, source_module,
                   resolved_source_currency, decided_by, decided_at, note
            FROM app.fx_conflict_resolutions
            WHERE target_field = %s{where_extra}
            ORDER BY source_module ASC
            """,  # noqa: S608
            params,
        )
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            rec: dict = {}
            for col, val in zip(cols, row):
                if col == "decided_at" and val is not None:
                    rec[col] = val.isoformat()
                else:
                    rec[col] = val
            rows.append(rec)
    return rows


# ---------------------------------------------------------------------------
# list_fx_resolutions
# ---------------------------------------------------------------------------


def list_fx_resolutions(project_id: str | None, conn) -> list[dict]:
    """Return all fx_conflict_resolutions, optionally scoped to a project."""
    params: list = []
    where = ""
    if project_id:
        where = "WHERE project_id = %s"
        params.append(project_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, project_id, target_field, source_module,
                   resolved_source_currency, decided_by, decided_at, note
            FROM app.fx_conflict_resolutions
            {where}
            ORDER BY project_id ASC, target_field ASC, source_module ASC
            """,  # noqa: S608
            params,
        )
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            rec: dict = {}
            for col, val in zip(cols, row):
                if col == "decided_at" and val is not None:
                    rec[col] = val.isoformat()
                else:
                    rec[col] = val
            rows.append(rec)
    return rows


# ---------------------------------------------------------------------------
# get_fx_resolution
# ---------------------------------------------------------------------------


def get_fx_resolution(
    project_id: str, target_field: str, source_module: str, conn
) -> dict | None:
    """Return a single fx_conflict_resolution row or None if not found."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, target_field, source_module,
                   resolved_source_currency, decided_by, decided_at, note
            FROM app.fx_conflict_resolutions
            WHERE project_id = %s AND target_field = %s AND source_module = %s
            """,
            (project_id, target_field, source_module),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        rec: dict = {}
        for col, val in zip(cols, row):
            if col == "decided_at" and val is not None:
                rec[col] = val.isoformat()
            else:
                rec[col] = val
        return rec


# ---------------------------------------------------------------------------
# upsert_fx_resolution
# ---------------------------------------------------------------------------


def upsert_fx_resolution(
    project_id: str,
    target_field: str,
    source_module: str,
    resolved_source_currency: str,
    decided_by: str,
    note: str | None,
    conn,
) -> dict:
    """Upsert a CURRENCY_CONFLICT resolution.

    Validates currency code. Does NOT convert currencies (AD-6).
    The resolution is consumed by dbt staging stg_*_daily.sql at the next run.

    NON RÉTROACTIF: applies only to the next dbt materialisation/reprocess.
    Past committed published outputs are never mutated.

    Args:
        project_id:               Project scope (AD-5).
        target_field:             Canonical field name (e.g. 'cost', 'revenue').
        source_module:            Module name (e.g. 'meta-ads', 'tiktok-ads').
        resolved_source_currency: ISO 4217 code to use as the source currency.
        decided_by:               Identity of the decision-maker.
        note:                     Optional free-text justification.
        conn:                     Open psycopg connection.

    Returns:
        The upserted resolution row as dict.

    Raises:
        ValueError on validation failure.
    """
    if not project_id:
        raise ValueError("project_id est requis")
    if not target_field:
        raise ValueError("target_field est requis")
    if not source_module:
        raise ValueError("source_module est requis")
    if not decided_by:
        decided_by = "anonymous"

    resolved_source_currency = (resolved_source_currency or "").strip().upper()
    _validate_currency(resolved_source_currency, "resolved_source_currency")

    note_val = (note or "").strip() or None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.fx_conflict_resolutions
                (project_id, target_field, source_module, resolved_source_currency,
                 decided_by, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, target_field, source_module) DO UPDATE SET
                resolved_source_currency = EXCLUDED.resolved_source_currency,
                decided_by               = EXCLUDED.decided_by,
                decided_at               = NOW(),
                note                     = EXCLUDED.note
            RETURNING id, project_id, target_field, source_module,
                      resolved_source_currency, decided_by, decided_at, note
            """,
            (project_id, target_field, source_module,
             resolved_source_currency, decided_by, note_val),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        rec: dict = {}
        for col, val in zip(cols, row):
            if col == "decided_at" and val is not None:
                rec[col] = val.isoformat()
            else:
                rec[col] = val

    conn.commit()

    logger.info(
        "conflict_resolutions: fx_resolution_upserted project=%s field=%s module=%s "
        "resolved_currency=%s by=%s",
        project_id, target_field, source_module, resolved_source_currency, decided_by,
    )
    return rec


# ---------------------------------------------------------------------------
# delete_fx_resolution
# ---------------------------------------------------------------------------


def delete_fx_resolution(
    project_id: str, target_field: str, source_module: str, conn
) -> None:
    """Delete a fx_conflict_resolution row.

    Raises ValueError if the resolution does not exist (-> HTTP 404).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM app.fx_conflict_resolutions
            WHERE project_id = %s AND target_field = %s AND source_module = %s
            """,
            (project_id, target_field, source_module),
        )
        deleted = cur.rowcount

    if deleted == 0:
        raise ValueError(
            f"Resolution introuvable pour projet='{project_id}', "
            f"champ='{target_field}', module='{source_module}'"
        )

    conn.commit()
    logger.info(
        "conflict_resolutions: fx_resolution_deleted project=%s field=%s module=%s",
        project_id, target_field, source_module,
    )
