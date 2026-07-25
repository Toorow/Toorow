"""toorow -- Actual x forecast alignment matrix on a conformed dimension (Story 27.5).

A READ-ONLY layer that crosses the plan ALLOCATION (forecast, mediaplan_store) and the
VENTILATED spend (actual, warehouse helper) on a COMMON CONFORMED dimension (e.g.
'placement', 'campaign' -- a client-chosen label, AD-2) so the client sees the
plan<->actual alignment clearly.

NO dbt, NO new table (the dbt parse is blocked by the parallel epic-26 session this
sprint; the mart plan_vs_actual_daily.sql is neither required nor touched -- it stays the
reference implementation). We reproduce its ventilation PRINCIPLE in Python from the
CONFIRMED mappings (mediaplan_mapping.list_mappings) and the spend
(warehouse.query_campaign_spend), to stay independent of the blocked dbt parse.

Honesty (AD-9): a value with NO confirmed conformance stays UNDER ITS SOURCE NAME
(conformed=false), NEVER silently fused with a canonical homonym. conform_value is ALWAYS
called WITH project_id (the "piege pour 27.5" of 27.4: without project_id a confirmed
PROJECT override would be invisible). WarehouseUnavailable PROPAGATES (never a fake empty
perimeter).

Anti-double-count (invariant 2 / AD-4): the ventilation re-sums to the original spend
EXACTLY (Sum split_weight = 1.0 per (connector, campaign_ref), Decimal, zero tolerance),
and NO inter-axis total is computed (summing heterogeneous axes would be a hidden AD-4).

STRICTLY PASSIVE / READ-ONLY: this module never writes warehouse or mappings. Design
mirrors dimension_conformance.py / mediaplan_mapping.py. AD-2: no connector/dimension name
in code. Windows/CI note: ASCII-safe strings only.

----------------------------------------------------------------------------
Output contract (documented for the future card/page 27.6), returned by
build_plan_actual_alignment:

    {
      "plan_id": "...",
      "dimension": "placement",
      "org_id": "...",
      "window": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} | None,
      "cells": [
        {"axis_value": "...", "conformed": true,
         "planned": 0.0, "actual": 0.0, "delta": 0.0, "pacing": 1.0}
      ],
      "unconformed_count": 0,   # nb of cells with conformed=false (AD-9 visibility)
      "ventilation_basis": "window_total",  # D-4: assiette de ventilation (voir note)
      "notes": []               # e.g. 'inactive version', 'no active mapping'
    }

D-4 (assiette de ventilation): ``ventilation_basis`` == "window_total" est expose au
TOP-LEVEL (pas seulement dans la docstring) pour que la card/le LLM le voie. Une note FR
courte est ajoutee a ``notes`` : la ventilation v1 repartit le spend TOTAL de la fenetre au
prorata du split_weight, donc pour des lignes a fenetres DISJOINTES partageant une meme
campagne le montant par-ligne peut diverger du mart daily-bounded (plan_vs_actual_daily.sql,
borne au jour). Le total par-campagne reste exact.

Contract invariants: (1) each ``actual`` comes from a ventilation that re-sums to the
original spend (22.3, tested); (2) NO inter-axis total is computed; (3) an unconformed
value stays DISTINCT (conformed=false), never absorbed into a canonical; (4) pacing=None
when planned==0 (never a division by zero).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")

# D-4: the ventilation assiette exposed at top-level of the contract + a short FR note so
# the card/LLM sees the fidelity boundary without reading the docstring.
VENTILATION_BASIS = "window_total"
_VENTILATION_BASIS_NOTE = (
    "ventilation sur le total de la fenetre (window_total) ; pour des lignes a fenetres "
    "disjointes partageant une campagne, le montant par-ligne peut diverger du mart "
    "daily-bounded -- le total par-campagne reste exact"
)

__all__ = [
    "AlignmentCell",
    "build_alignment_matrix",
    "build_plan_actual_alignment",
]


# ---------------------------------------------------------------------------
# B.2 The pure reducer -- aligns forecast/actual by axis value.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlignmentCell:
    """One axis cell of the matrix (PURE data)."""

    axis_value: str  # conformed value OR source value (when unconformed)
    conformed: bool  # False => source name, never fused (AD-9)
    planned: float  # sum of the forecast allocations on this axis
    actual: float  # sum of the ventilated actual spend on this axis
    delta: float  # actual - planned (indicative, per-axis)
    pacing: float | None  # actual/planned, None when planned == 0 (never /0)


def build_alignment_matrix(
    planned_by_key: dict,
    actual_ventilated: list[dict],
    conform_fn,
    line_axis_fn,
) -> dict:
    """PURE. Project forecast and actual onto the conformed axis and build the matrix.

    Args:
      planned_by_key: {line_key -> planned_amount} (forecast). Amounts are summed onto
        the axis via line_axis_fn(line_key).
      actual_ventilated: [{connector, campaign_ref, line_key, amount}, ...] -- the
        already-ventilated real spend (see _ventilate_actuals).
      conform_fn: (connector, source_value) -> str | None. The bound conform_value; None
        means 'no confirmed conformance' -> the value stays under its source name.
      line_axis_fn: (line_key) -> the plan-side axis value.

    Axis rule: on the actual side, axis = conform_fn(connector, campaign_ref) if not None
    (conformed=True) else campaign_ref raw (conformed=False, AD-9). On the plan side,
    axis = line_axis_fn(line_key). planned and actual accumulate per axis_value; a
    conformed row and an unconformed row NEVER fuse -- the accumulation key is
    (axis_value, conformed), so a source name that coincides with a canonical stays
    distinct until it is confirmed. pacing = None when planned == 0 (never a division by
    zero; a 100%-actual axis with no forecast is visible, honest).

    Deterministic: cells are sorted by (axis_value, conformed).
    """
    # key = (axis_value, conformed) -> {"planned": Decimal, "actual": Decimal}
    accum: dict[tuple[str, bool], dict[str, Decimal]] = {}

    def _bucket(axis_value: str, conformed: bool) -> dict[str, Decimal]:
        key = (axis_value, conformed)
        cell = accum.get(key)
        if cell is None:
            cell = {"planned": _ZERO, "actual": _ZERO}
            accum[key] = cell
        return cell

    # Forecast side: a plan line's axis is ALWAYS a plan-authored value -> conformed=True
    # by construction (it is a first-class axis value, not a source name awaiting
    # confirmation). It is the canonical the actual side conforms TOWARDS.
    for line_key, planned_amount in planned_by_key.items():
        axis_value = line_axis_fn(line_key)
        if axis_value is None:
            continue
        cell = _bucket(axis_value, True)
        cell["planned"] += _to_decimal(planned_amount)

    # Actual side: conform each (connector, campaign_ref); unconformed stays source-named.
    for row in actual_ventilated:
        connector = row.get("connector")
        campaign_ref = row.get("campaign_ref")
        amount = _to_decimal(row.get("amount"))
        canonical = conform_fn(connector, campaign_ref)
        if canonical is not None:
            cell = _bucket(canonical, True)
        else:
            cell = _bucket(campaign_ref, False)
        cell["actual"] += amount

    cells: list[dict] = []
    unconformed_count = 0
    for (axis_value, conformed) in sorted(accum, key=lambda k: (k[0], k[1])):
        sums = accum[(axis_value, conformed)]
        planned = sums["planned"]
        actual = sums["actual"]
        delta = actual - planned
        pacing = float(actual / planned) if planned != _ZERO else None
        if not conformed:
            unconformed_count += 1
        cells.append(
            {
                "axis_value": axis_value,
                "conformed": conformed,
                "planned": float(planned),
                "actual": float(actual),
                "delta": float(delta),
                "pacing": pacing,
            }
        )

    return {"cells": cells, "unconformed_count": unconformed_count}


def _to_decimal(value) -> Decimal:
    """Coerce a spend/allocation amount to Decimal (accepts str/int/float/Decimal/None)."""
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# B.3 Ventilate the actual spend in Python (reuses confirmed mappings + spend).
# ---------------------------------------------------------------------------


def _ventilate_actuals(
    mappings: dict,
    campaign_spend: list[dict],
) -> list[dict]:
    """Reproduce plan_vs_actual_daily.sql's ventilation IN PYTHON (no dbt).

    For each ACTIVE mapping (status='active'), amount_line = spend(connector,
    campaign_ref) * split_weight. Because Sum(split_weight) == 1.0 per (connector,
    campaign_ref) (22.3 invariant), Sum(ventilated) == the original spend EXACTLY (no cent
    created or lost). Decimal for the weights (like 22.3), never a float on the weight.
    Only status='active' mappings ventilate (orphaned ones do not -- 22.3 rule).

    Window (v1): aligns on the window-total (query_campaign_spend); the per-line-window
    (daily) refinement is deferred (story Hors-perimetre).

    CAVEAT -- per-line attribution vs the mart (fidelity boundary, tested): when several
    plan lines with DISJOINT flight windows share the SAME campaign, the v1 per-line
    amount is (window-TOTAL spend x split_weight), so a line only active early in the
    window still receives its weighted share of the spend that occurred later (and vice
    versa). This DIFFERS from the reference mart plan_vs_actual_daily.sql, which bounds each
    day's spend to the line's flight window before ventilating, so a line receives only
    the spend of the days it was live. Consequence: only the per-CAMPAIGN total is faithful
    here (Sum over lines == campaign spend, exact); the split BETWEEN disjoint-window lines
    of a shared campaign is a window-total approximation, not the day-bounded mart figure.
    A caller comparing v1 per-line actuals to the mart must expect this divergence.

    Args:
      mappings: the shape returned by mediaplan_mapping.list_mappings(conn, plan_id=...):
        {plan_id, lines: [{line_key, mappings: [{connector, campaign_ref, split_weight,
        status}, ...]}, ...]}.
      campaign_spend: [{connector, campaign_ref, spend}, ...] (query_campaign_spend).

    Returns [{connector, campaign_ref, line_key, amount}, ...] where ``amount`` is an
    EXACT Decimal (spend * split_weight). Keeping Decimal here means the matrix
    accumulates at Decimal grain and the per-campaign parts re-sum to the campaign spend
    EXACTLY (the float projection happens once, per cell, in build_alignment_matrix).
    Deterministic order.
    """
    # Index the spend by (connector, campaign_ref).
    spend_by_campaign: dict[tuple[str, str], Decimal] = {}
    for row in campaign_spend:
        connector = row.get("connector")
        campaign_ref = row.get("campaign_ref")
        if connector is None or campaign_ref is None:
            continue
        spend_by_campaign[(connector, campaign_ref)] = _to_decimal(row.get("spend"))

    out: list[dict] = []
    for line in mappings.get("lines", []):
        line_key = line.get("line_key")
        for entry in line.get("mappings", []):
            if entry.get("status") != "active":
                continue  # orphaned mappings do not ventilate (22.3)
            connector = entry.get("connector")
            campaign_ref = entry.get("campaign_ref")
            spend = spend_by_campaign.get((connector, campaign_ref))
            if spend is None:
                continue  # a mapped campaign with no spend in the window -> nothing
            weight = _to_decimal(entry.get("split_weight"))
            amount = spend * weight
            out.append(
                {
                    "connector": connector,
                    "campaign_ref": campaign_ref,
                    "line_key": line_key,
                    "amount": amount,  # exact Decimal (spend * split_weight)
                }
            )

    out.sort(
        key=lambda r: (
            str(r["connector"]),
            str(r["campaign_ref"]),
            str(r["line_key"]),
        )
    )
    return out


# ---------------------------------------------------------------------------
# B.4 End-to-end read -- the documented JSON contract.
# ---------------------------------------------------------------------------


def build_plan_actual_alignment(
    plan_id: str,
    *,
    dimension: str,
    org_id: str | None = None,
    campaign_spend_fn=None,
    conform_fn=None,
) -> dict:
    """End-to-end READ. Return the stable JSON contract (see module docstring).

    NO write, NO dbt. Reads the plan (mediaplan_store), its confirmed mappings
    (mediaplan_mapping.list_mappings) and the window spend (warehouse.query_campaign_spend,
    injectable), ventilates in Python, conforms the actual axis via conform_value ALWAYS
    passing project_id (27.4 "piege"), and aligns forecast/actual on the conformed axis.

    org_id: required for conform_value; when None it is read from app.projects.org_id for
    the plan's project (fail-soft to None -> conform_value fails-to-platform).
    WarehouseUnavailable PROPAGATES (AD-9).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_mapping import list_mappings  # noqa: PLC0415
    from core.mediaplan_store import get_plan  # noqa: PLC0415

    notes: list[str] = []

    with get_connection() as conn:
        plan = get_plan(conn, plan_id=plan_id)
        if plan is None:
            return {
                "plan_id": str(plan_id),
                "dimension": dimension,
                "org_id": org_id,
                "window": None,
                "cells": [],
                "unconformed_count": 0,
                "ventilation_basis": VENTILATION_BASIS,
                "notes": ["unknown plan"],
            }
        project_id = plan.get("project_id")
        lines = list(plan.get("lines") or [])
        mappings = list_mappings(conn, plan_id=plan_id)

    if not lines:
        notes.append("inactive version (no active plan lines)")

    if org_id is None and project_id is not None:
        org_id = _project_org_id(project_id)

    window_start, window_end = _plan_window(lines)
    window = (
        {"start": window_start, "end": window_end}
        if window_start is not None and window_end is not None
        else None
    )

    # Real spend of the window (injected warehouse helper). WarehouseUnavailable
    # propagates. When there is no window we honestly read nothing.
    if window is not None:
        fn = campaign_spend_fn
        if fn is None:
            from core import warehouse as _wh  # noqa: PLC0415

            fn = _wh.query_campaign_spend
        campaign_spend = fn(project_id, window_start, window_end)
    else:
        campaign_spend = []
        notes.append("no window to read real spend from")

    ventilated = _ventilate_actuals(mappings, campaign_spend)
    if not any(
        entry.get("status") == "active"
        for line in mappings.get("lines", [])
        for entry in line.get("mappings", [])
    ):
        notes.append("no active mapping")

    # Bind conform_value with the dimension/org/project (ALWAYS project_id -- 27.4 piege).
    bound_conform = _bind_conform_fn(
        conform_fn, dimension=dimension, org_id=org_id, project_id=project_id
    )

    # Forecast allocation per line_key + the plan-side axis value per line_key.
    planned_by_key = _planned_by_key(lines)
    axis_by_key = _axis_by_key(lines)

    def _line_axis_fn(line_key):
        return axis_by_key.get(line_key)

    matrix = build_alignment_matrix(
        planned_by_key, ventilated, bound_conform, _line_axis_fn
    )

    # D-4: surface the ventilation assiette in-band (note + top-level key).
    notes.append(_VENTILATION_BASIS_NOTE)

    return {
        "plan_id": str(plan_id),
        "dimension": dimension,
        "org_id": org_id,
        "window": window,
        "cells": matrix["cells"],
        "unconformed_count": matrix["unconformed_count"],
        "ventilation_basis": VENTILATION_BASIS,
        "notes": notes,
    }


def _bind_conform_fn(conform_fn, *, dimension, org_id, project_id):
    """Return a (connector, source_value) -> str | None callable.

    Defaults to core.dimension_conformance.conform_value, ALWAYS supplying project_id so a
    confirmed PROJECT override is visible (27.4 "piege pour 27.5"). When org_id is None
    (unknown project), conform_value fails-to-platform and returns None -> the value stays
    source-named (AD-9). A caller may inject its own conform_fn for offline tests.
    """
    if conform_fn is not None:
        return conform_fn

    from core.dimension_conformance import conform_value  # noqa: PLC0415

    def _conform(connector, source_value):
        if org_id is None:
            return None
        return conform_value(
            org_id, dimension, connector, source_value, project_id=project_id
        )

    return _conform


def _project_org_id(project_id: str) -> str | None:
    """Read app.projects.org_id for *project_id* (None if unknown). Fail-soft.

    Mirrors dimension_conformance._project_org_id / metric_semantics._project_org_id: an
    unresolvable project falls back to PLATFORM defaults only, never a crash.
    """
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                )
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plan_actual_alignment: project org lookup failed project=%s: %s",
            project_id,
            exc,
        )
        return None


def _plan_window(lines: list[dict]) -> tuple[str | None, str | None]:
    """Envelope [min(start_date), max(end_date)] of the lines (ISO strings), or (None,None).

    list_lines renders start_date/end_date as ISO 'YYYY-MM-DD' strings, so min/max over the
    non-empty strings is the calendar envelope. Mirrors list_unmapped_actuals.
    """
    starts = [s for s in (line.get("start_date") for line in lines) if s]
    ends = [e for e in (line.get("end_date") for line in lines) if e]
    if not starts or not ends:
        return (None, None)
    return (min(starts), max(ends))


def _planned_by_key(lines: list[dict]) -> dict:
    """{line_key -> budget} for the forecast side (budget as-is; _to_decimal coerces).

    list_lines renders ``budget`` as a stable decimal string; _to_decimal parses it
    without any float loss. A line with no budget contributes 0.
    """
    out: dict = {}
    for line in lines:
        line_key = (line.get("line_key") or "").strip()
        if not line_key:
            continue
        out[line_key] = line.get("budget")
    return out


def _axis_by_key(lines: list[dict]) -> dict:
    """{line_key -> plan-side axis value}.

    Decision (Completion Notes): the plan-side axis value is the ``label`` (fallback
    line_key when the label is empty) -- the human-readable identity of the plan line, the
    canonical the actual side conforms towards. The bridge between 27.4's
    canonical_dimension and 27.5's alignment axis is EXPLICIT in the contract (the
    ``dimension`` parameter drives conform_value), never hard-coded.
    """
    out: dict = {}
    for line in lines:
        line_key = (line.get("line_key") or "").strip()
        if not line_key:
            continue
        label = (line.get("label") or "").strip()
        out[line_key] = label if label else line_key
    return out
