"""toorow -- MDM conflict resolutions (Story 13.2, Epic 13).

Provides:
  list_conflicts(project_id, conn)                          -> list[dict]
  list_fx_resolutions(project_id, conn)                     -> list[dict]
  get_fx_resolution(project_id, target_field, source_module, conn) -> dict | None
  upsert_fx_resolution(project_id, target_field, source_module,
                       resolved_source_currency, decided_by, note, conn) -> dict
  delete_fx_resolution(project_id, target_field, source_module, conn) -> None

WHERE A RESOLUTION LIVES NOW (2026-08-17).

Every function above used to read and write `app.fx_conflict_resolutions`. That
table was dethroned by migration 145 -- it is one of the seven stores listed under
"They stop being AUTHORITIES" -- and migration 282 sealed it against writes. The
five functions kept their names and their signatures, because the REST routes and
the console dialog that call them are correct; only the store underneath moved, to
the one 144 built and 145 adopted:

    app.governance_rule_sets / app.governance_rule_set_versions,
    family `source_currency`, via core.source_currency_bindings.

What that buys, concretely: the old `ON CONFLICT DO UPDATE` overwrote `decided_by`
and `decided_at` in place, so re-declaring a currency erased who had decided the
previous one. Each write now publishes an immutable version, the previous one is
superseded rather than edited, and every declaration carries its own attribution,
carried forward untouched when a neighbouring one changes.

`delete_fx_resolution` is now a WITHDRAWAL: it publishes the set without that
declaration. The withdrawn one stays readable in the superseded version, so a
figure published while it was in force can still be explained.

Design decisions:
  - AD-6: AUCUNE conversion FX dans ce fichier Python. The governed declaration
    GIVES a datum to dbt; dbt staging (stg_*_daily.sql) does the FX JOIN with
    COALESCE(res.resolved_source_currency, raw.cost_source_currency). dbt now
    reads `mirror.fx_source_currency_bindings`, projected from the published
    version by `app.fx_source_currency_bindings_v` (migration 282).
  - AD-5: endpoints REST dénégation cross-projet (identity_can_read_project).
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
# The currency vocabulary — the governed one, not a second list
# ---------------------------------------------------------------------------
#
# This module used to carry a hand-written frozenset of 30 codes, "subset,
# extended on demand". Story 48.3 gave the product ONE governed ISO 4217
# vocabulary (`core.currency_vocabulary`, an immutable content-hashed version
# with a rendered dbt projection), and `/api/reference/currencies` serves its
# 156 selectable tender currencies to every picker in the console.
#
# Two lists meant a person could choose a currency the selector offered and be
# refused by this door with a message naming neither the gesture nor the reason.
# Measured before removing it: the 30 are a STRICT SUBSET of the 156, so nothing
# this function accepted yesterday is refused today.


def _validate_currency(currency: str, field_label: str) -> None:
    """Raise ValueError unless the code is a selectable reporting currency."""
    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415

    if not currency or not currency.strip():
        raise ValueError(f"{field_label} is required")
    resolved = resolve_currency(currency)
    if resolved is None or not resolved.is_tender:
        raise ValueError(
            f"{field_label} is not a valid currency: {currency!r}. "
            f"Pick an ISO 4217 code offered by /api/reference/currencies."
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
        detail = get_target_field(name, conn, project_id=project_id)
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


#: The columns of the governed projection, in the order the old table returned
#: them. `app.fx_source_currency_bindings_v` (migration 282) exposes exactly the
#: shape `app.fx_conflict_resolutions` did, plus the version that carries the
#: declaration -- so a caller can now answer "under which version was this read?",
#: which the flat table could never answer.
_BINDING_COLUMNS = (
    "project_id",
    "target_field",
    "source_module",
    "resolved_source_currency",
    "decided_by",
    "decided_at",
    "note",
    "rule_set_id",
    "rule_set_version_id",
)

_BINDING_VIEW = "app.fx_source_currency_bindings_v"


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    rows = []
    for row in cur.fetchall():
        rec: dict = {}
        for col, val in zip(cols, row):
            if col == "decided_at" and val is not None and hasattr(val, "isoformat"):
                rec[col] = val.isoformat()
            else:
                rec[col] = val
        rows.append(rec)
    return rows


def _fetch_resolutions_for_field(
    project_id: str | None, target_field: str, conn
) -> list[dict]:
    """Fetch the governed source-currency declarations for one field.

    Reads the projection of the PUBLISHED version, not the dethroned table.
    Optionally project-scoped: `list_conflicts` may be called without a Project.
    """
    params: list = [target_field]
    where_extra = ""
    if project_id:
        where_extra = " AND project_id = %s"
        params.append(project_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(_BINDING_COLUMNS)}
            FROM {_BINDING_VIEW}
            WHERE target_field = %s{where_extra}
            ORDER BY source_module ASC
            """,  # noqa: S608
            params,
        )
        return _rows(cur)


# ---------------------------------------------------------------------------
# list_fx_resolutions
# ---------------------------------------------------------------------------


def list_fx_resolutions(project_id: str | None, conn) -> list[dict]:
    """Return the governed source-currency declarations, optionally project-scoped."""
    params: list = []
    where = ""
    if project_id:
        where = "WHERE project_id = %s"
        params.append(project_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(_BINDING_COLUMNS)}
            FROM {_BINDING_VIEW}
            {where}
            ORDER BY project_id ASC, target_field ASC, source_module ASC
            """,  # noqa: S608
            params,
        )
        return _rows(cur)


# ---------------------------------------------------------------------------
# get_fx_resolution
# ---------------------------------------------------------------------------


def get_fx_resolution(
    project_id: str, target_field: str, source_module: str, conn
) -> dict | None:
    """Return a single governed source-currency declaration, or None."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(_BINDING_COLUMNS)}
            FROM {_BINDING_VIEW}
            WHERE project_id = %s AND target_field = %s AND source_module = %s
            """,  # noqa: S608
            (project_id, target_field, source_module),
        )
        rows = _rows(cur)
    return rows[0] if rows else None


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
    from core.source_currency_bindings import declare_binding  # noqa: PLC0415

    if not project_id:
        raise ValueError("project_id is required")
    if not target_field:
        raise ValueError("target_field is required")
    if not source_module:
        raise ValueError("source_module is required")
    if not decided_by:
        decided_by = "anonymous"

    resolved_source_currency = (resolved_source_currency or "").strip().upper()
    _validate_currency(resolved_source_currency, "resolved_source_currency")

    # One governed act: the whole declaration set is republished as a new
    # immutable version. `RuleSetError` subclasses `ValueError`, so the route's
    # existing 422 mapping keeps working with no change at the seam.
    rec = declare_binding(
        conn,
        project_id=project_id,
        target_field=target_field,
        source_module=source_module,
        source_currency=resolved_source_currency,
        actor=decided_by,
        note=note,
    )

    conn.commit()

    logger.info(
        "conflict_resolutions: fx_resolution_declared project=%s field=%s module=%s "
        "resolved_currency=%s version=%s by=%s",
        project_id, target_field, source_module, resolved_source_currency,
        rec.get("rule_set_version_id"), decided_by,
    )
    return rec


# ---------------------------------------------------------------------------
# delete_fx_resolution
# ---------------------------------------------------------------------------


def delete_fx_resolution(
    project_id: str, target_field: str, source_module: str, conn
) -> None:
    """Withdraw a source-currency declaration.

    Not a DELETE any more, and the name is kept only because the route and the
    dialog that call it are unchanged. Withdrawing publishes the declaration set
    WITHOUT this entry; the withdrawn one stays readable in the superseded
    version, so a figure published while it was in force can still be explained.

    Raises ValueError when nothing is declared for that pair (-> HTTP 404).
    """
    from core.source_currency_bindings import withdraw_binding  # noqa: PLC0415

    result = withdraw_binding(
        conn,
        project_id=project_id,
        target_field=target_field,
        source_module=source_module,
        actor="anonymous",
    )

    conn.commit()
    logger.info(
        "conflict_resolutions: fx_resolution_withdrawn project=%s field=%s module=%s "
        "version=%s",
        project_id, target_field, source_module, result.get("rule_set_version_id"),
    )
