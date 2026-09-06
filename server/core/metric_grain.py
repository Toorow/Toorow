"""Chantier B -- the same measure at several grains, and the gap it owes.

WHAT THIS MODULE EXISTS TO SEPARATE. Three facts about "which Datastream answers
for `views`" look alike and are not:

* **Who CARRIES it** is derived. A Datastream carries a concept when its *active*
  mapping version binds a field to that concept's canonical target. Nothing here
  stores that list; ``carriers_of`` reads it, every time, from the mappings Data
  has published. A stored copy would be wrong the day a mapping moves, and the
  screen would still be sure of itself.
* **Who is authoritative for the TOTAL** is declared. It cannot be derived from
  any mapping, and picking one in silence would decide, on the client's behalf,
  where their figures come from. Measured on ``proj_01KZGCRSV2XACWRP3RSVNWWGBK``
  on 2026-08-14: eight of the ten Datastreams with an active mapping carry
  ``views``, and the Semantic View pinned exactly one -- a decision nothing
  recorded.
* **What a breakdown SUMS TO** against that total is declared too, and it is the
  half that makes a gap readable. A channel total is not the sum of a per-video
  breakdown, because a share of subscriber changes happens away from a watch page.
  That sentence is prose in a connector manifest today; here it travels with the
  metric.

AND THE RULE THE WHOLE MODULE SERVES: **a gap is shown, never closed.** No total
and no breakdown is scaled, capped or back-filled so that two numbers agree. The
four verdicts (``expected``, ``reconciled``, ``unexplained``, ``undeclared``) and
``unavailable`` are five different answers, and they are not allowed to collapse
into each other -- in particular ``undeclared`` is not ``expected``, and a total
that could not be produced is not a gap of zero.

Contract: ``docs/product-architecture/analyze-and-test.md``, "Amendment,
chantier B". Tables: migration 261. Audit: ``app.metric_semantics_audit``.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ulid import ULID

logger = logging.getLogger(__name__)

#: Prefixed ULIDs, same convention as metric_semantics.py.
_ID_PREFIXES = {
    "metric_grain_declarations": "mgd_",
    "metric_grain_breakdowns": "mgb_",
}

#: What a breakdown sums to. `unknown` is deliberately absent: it is the ABSENCE
#: of a row. A third value would let "nobody looked" be written down as an answer.
SUMS_TO_EQUALS = "equals"
SUMS_TO_PARTIAL = "partial_by_design"
SUMS_TO_VALUES = (SUMS_TO_EQUALS, SUMS_TO_PARTIAL)

#: The floor a `partial_by_design` reason must clear. Same floor story 53.7 puts
#: on a Test gate override: a blank explanation is indistinguishable from none.
MIN_REASON_LENGTH = 20

#: Verdicts a reconciliation can return. Five, because they fail five ways.
VERDICT_EXPECTED = "expected"          # declared partial_by_design, reason printed
VERDICT_RECONCILED = "reconciled"      # declared equals, gap within tolerance
VERDICT_UNEXPLAINED = "unexplained"    # declared equals, gap outside tolerance
VERDICT_UNDECLARED = "undeclared"      # no declaration -- gap stated, no reason claimed
VERDICT_UNAVAILABLE = "unavailable"    # the total could not be produced. NOT a zero gap.
VERDICTS = (
    VERDICT_EXPECTED,
    VERDICT_RECONCILED,
    VERDICT_UNEXPLAINED,
    VERDICT_UNDECLARED,
    VERDICT_UNAVAILABLE,
)


class MetricGrainRefused(ValueError):
    """A declaration was refused. ``code`` is machine-readable, ``args[0]`` names
    the gesture that repairs it -- never the table it would have written to."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _mint_id(prefix: str) -> str:
    return f"{prefix}{ULID()}"


def _fetch(conn: Any, query: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# 1. Derived: who carries this concept, and at which grain.
# ---------------------------------------------------------------------------


def carriers_of(
    conn: Any,
    *,
    project_id: str,
    concept_name: str,
    view_version_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every Datastream whose ACTIVE mapping binds a field to ``concept_name``.

    Read, never stored. ``bound_in_view`` is filled only when a view version is
    given, and it is the countable gap the screen shows: a View that pins one
    carrier out of eight is not an error state to be guessed at.

    The join is the canonical target's equality with the concept's name -- the
    same equality ``query_execution.resolve_physical_plan`` uses to turn a member
    into a physical column. Using a different one here would let this reading and
    the executor disagree about what is available.
    """
    name = str(concept_name or "").strip()
    if not name:
        return []

    rows = _fetch(
        conn,
        """
        SELECT d.id            AS datastream_id,
               d.name          AS datastream_name,
               d.module_name   AS module_name,
               mv.id           AS mapping_version_id,
               mv.mapping_payload AS mapping_payload
        FROM app.datastreams d
        JOIN app.datastream_mapping_versions mv ON mv.id = d.current_mapping_version_id
        WHERE d.project_id = %(project_id)s
        ORDER BY d.name
        """,
        {"project_id": project_id},
    )

    bound: set[str] = set()
    if view_version_id:
        bound = {
            str(row["datastream_id"])
            for row in _fetch(
                conn,
                """
                SELECT b.datastream_id
                FROM app.semantic_view_version_bindings b
                JOIN app.semantic_concept_versions cv ON cv.concept_id = b.concept_id
                WHERE b.view_version_id = %(view_version_id)s AND cv.name = %(name)s
                """,
                {"view_version_id": view_version_id, "name": name},
            )
        }

    carriers: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("mapping_payload") or {}
        if not isinstance(payload, Mapping):
            continue
        targets = {
            str((field.get("binding") or {}).get("canonical_target") or "")
            for field in (payload.get("fields") or ())
            if isinstance(field, Mapping)
        }
        if name not in targets:
            continue
        grain = [str(part) for part in (payload.get("grain") or ()) if str(part)]
        carriers.append(
            {
                "datastream_id": str(row["datastream_id"]),
                "datastream_name": row.get("datastream_name"),
                "module_name": row.get("module_name"),
                "mapping_version_id": str(row["mapping_version_id"]),
                "grain": grain,
                # The dimensions this carrier adds beyond the total's own grain are
                # filled by `grain_coverage`, which knows which one is the total.
                "bound_in_view": (str(row["datastream_id"]) in bound) if view_version_id else None,
            }
        )
    return carriers


def grain_coverage(
    conn: Any,
    *,
    project_id: str,
    concept_id: str,
    concept_name: str,
    view_version_id: str | None = None,
) -> dict[str, Any]:
    """The whole reading for one concept: carriers, declaration, and what is missing.

    This is the shape both the console and the MCP door read. It says three things
    a person can act on, and it says them separately because they are repaired
    separately: how many carriers exist, how many this View binds, and whether a
    total authority has been declared.
    """
    carriers = carriers_of(
        conn, project_id=project_id, concept_name=concept_name, view_version_id=view_version_id
    )
    declaration = get_declaration(conn, project_id=project_id, concept_id=concept_id)
    total_id = str(declaration["total_datastream_id"]) if declaration else None
    breakdowns = {
        str(entry["datastream_id"]): entry
        for entry in (declaration or {}).get("breakdowns", ())
    }

    for carrier in carriers:
        is_total = carrier["datastream_id"] == total_id
        carrier["role"] = "total" if is_total else "breakdown"
        rule = breakdowns.get(carrier["datastream_id"])
        carrier["sums_to"] = None if is_total else (rule or {}).get("sums_to")
        carrier["reason"] = None if is_total else (rule or {}).get("reason")

    unbound = [c for c in carriers if c.get("bound_in_view") is False]
    undeclared = [
        c for c in carriers if c["role"] == "breakdown" and c["sums_to"] is None
    ]
    return {
        "concept_id": concept_id,
        "concept_name": concept_name,
        "project_id": project_id,
        "view_version_id": view_version_id,
        "carriers": carriers,
        "carrier_count": len(carriers),
        "bound_count": (
            sum(1 for c in carriers if c.get("bound_in_view")) if view_version_id else None
        ),
        "total_datastream_id": total_id,
        "declaration": declaration,
        # The two gestures, named rather than the two tables they would write to.
        "next_gesture": _next_gesture(
            carriers=carriers, declaration=declaration, unbound=unbound, undeclared=undeclared
        ),
    }


def _next_gesture(
    *,
    carriers: Sequence[Mapping[str, Any]],
    declaration: Mapping[str, Any] | None,
    unbound: Sequence[Mapping[str, Any]],
    undeclared: Sequence[Mapping[str, Any]],
) -> str | None:
    """One sentence naming what to do next, or None when nothing is owed.

    Order matters: binding first (without it the breakdowns cannot be queried at
    all), then the total, then the reasons. Naming all three at once would make a
    screen that asks three questions where one answer removes two of them.
    """
    if not carriers:
        return (
            "No published Datastream carries this measure yet. Publish a mapping "
            "that binds a field to it."
        )
    if len(carriers) > 1 and unbound:
        return (
            f"{len(unbound)} of the {len(carriers)} Datastreams that carry this "
            "measure are not bound to this Semantic View. Bind them to ask this "
            "measure at their grain."
        )
    if len(carriers) > 1 and declaration is None:
        return (
            "Declare which Datastream is authoritative for the total of this "
            "measure. Until then a request several of them could answer is refused."
        )
    if undeclared:
        return (
            f"State what {len(undeclared)} breakdown(s) sum to against the total: "
            "the same figure, or a share of it with the reason it is a share."
        )
    return None


# ---------------------------------------------------------------------------
# 2. Declared: the total authority and what each breakdown sums to.
# ---------------------------------------------------------------------------


def get_declaration(
    conn: Any, *, project_id: str, concept_id: str
) -> dict[str, Any] | None:
    rows = _fetch(
        conn,
        """
        SELECT id, project_id, concept_id, total_datastream_id, note,
               created_by, created_at, updated_at
        FROM app.metric_grain_declarations
        WHERE project_id = %(project_id)s AND concept_id = %(concept_id)s
        """,
        {"project_id": project_id, "concept_id": concept_id},
    )
    if not rows:
        return None
    declaration = _wire_safe(dict(rows[0]))
    declaration["breakdowns"] = [
        _wire_safe(dict(row))
        for row in _fetch(
            conn,
            """
            SELECT id, datastream_id, sums_to, reason, tolerance_ratio,
                   created_by, created_at, updated_at
            FROM app.metric_grain_breakdowns
            WHERE declaration_id = %(declaration_id)s
            ORDER BY datastream_id
            """,
            {"declaration_id": declaration["id"]},
        )
    ]
    return declaration


def _wire_safe(row: dict[str, Any]) -> dict[str, Any]:
    """A declaration row as the wire can carry it.

    `created_at` / `updated_at` come back as datetimes and `tolerance_ratio` as
    a Decimal; `JSONResponse` refuses both. Measured 2026-09-01 on the deployed
    server: the FIRST declaration ever read through `GET .../metric-grain/{id}`
    and the FIRST `PUT .../total` both answered 500 -- after the write had
    committed -- so every project that had declared an authority could no longer
    read it, and the one declaring it was told the gesture failed.
    """
    safe = dict(row)
    for key in ("created_at", "updated_at"):
        value = safe.get(key)
        if hasattr(value, "isoformat"):
            safe[key] = value.isoformat()
    if isinstance(safe.get("tolerance_ratio"), Decimal):
        safe["tolerance_ratio"] = float(safe["tolerance_ratio"])
    return safe


def declare_total(
    conn: Any,
    *,
    project_id: str,
    concept_id: str,
    concept_name: str,
    total_datastream_id: str,
    identity: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Name the carrier that answers for the total of this measure.

    Refuses a Datastream that does not carry the measure. That refusal is not
    pedantry: a total pinned to a Datastream with no such field would make every
    later gap read `unavailable` for a reason nobody could see.
    """
    carriers = {
        c["datastream_id"]: c
        for c in carriers_of(conn, project_id=project_id, concept_name=concept_name)
    }
    if not carriers:
        raise MetricGrainRefused(
            "no_carrier",
            "No published Datastream of this Project carries this measure. Publish "
            "a mapping that binds a field to it before declaring where its total lives.",
        )
    if total_datastream_id not in carriers:
        raise MetricGrainRefused(
            "not_a_carrier",
            "That Datastream does not publish this measure. Choose one of the "
            f"{len(carriers)} that do, or map the measure on it first.",
        )

    before = get_declaration(conn, project_id=project_id, concept_id=concept_id)
    if before is None:
        declaration_id = _mint_id(_ID_PREFIXES["metric_grain_declarations"])
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.metric_grain_declarations
                    (id, project_id, concept_id, total_datastream_id, note, created_by)
                VALUES (%(id)s, %(project_id)s, %(concept_id)s, %(total)s, %(note)s, %(by)s)
                """,
                {
                    "id": declaration_id,
                    "project_id": project_id,
                    "concept_id": concept_id,
                    "total": total_datastream_id,
                    "note": note,
                    "by": identity,
                },
            )
    else:
        declaration_id = str(before["id"])
        # Moving the total invalidates any breakdown row that now points at it:
        # a Datastream cannot be a breakdown of itself. Deleting it here is the
        # only silent write in this module, and it is silent because keeping it
        # would let a screen show a Datastream reconciling against itself.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.metric_grain_declarations
                SET total_datastream_id = %(total)s, note = %(note)s
                WHERE id = %(id)s
                """,
                {"id": declaration_id, "total": total_datastream_id, "note": note},
            )
            cur.execute(
                """
                DELETE FROM app.metric_grain_breakdowns
                WHERE declaration_id = %(id)s AND datastream_id = %(total)s
                """,
                {"id": declaration_id, "total": total_datastream_id},
            )

    after = get_declaration(conn, project_id=project_id, concept_id=concept_id)
    _write_audit(
        conn,
        identity=identity,
        action="metric_grain_declaration.upserted",
        entity_type="metric_grain_declaration",
        entity_id=declaration_id,
        project_id=project_id,
        before=before,
        after=after,
    )
    return after or {}


def declare_breakdown(
    conn: Any,
    *,
    project_id: str,
    concept_id: str,
    concept_name: str,
    datastream_id: str,
    sums_to: str,
    identity: str,
    reason: str | None = None,
    tolerance_ratio: float | None = None,
) -> dict[str, Any]:
    """State what one breakdown sums to against the declared total."""
    if sums_to not in SUMS_TO_VALUES:
        raise MetricGrainRefused(
            "unknown_sums_to",
            "A breakdown either reconstitutes the total or is a share of it. "
            f"Choose one of: {', '.join(SUMS_TO_VALUES)}.",
        )
    declaration = get_declaration(conn, project_id=project_id, concept_id=concept_id)
    if declaration is None:
        raise MetricGrainRefused(
            "no_total_declared",
            "Declare which Datastream is authoritative for the total of this "
            "measure first. A breakdown sums to something, and that something is "
            "the total.",
        )
    if datastream_id == str(declaration["total_datastream_id"]):
        raise MetricGrainRefused(
            "breakdown_is_the_total",
            "This Datastream is the one you declared authoritative for the total. "
            "Pick another Datastream, or move the total first.",
        )
    carriers = {
        c["datastream_id"]
        for c in carriers_of(conn, project_id=project_id, concept_name=concept_name)
    }
    if datastream_id not in carriers:
        raise MetricGrainRefused(
            "not_a_carrier",
            "That Datastream does not publish this measure, so it breaks nothing "
            "down. Map the measure on it first.",
        )
    if sums_to == SUMS_TO_PARTIAL and len(str(reason or "").strip()) < MIN_REASON_LENGTH:
        raise MetricGrainRefused(
            "reason_required",
            "Write what this breakdown does not contain, in at least "
            f"{MIN_REASON_LENGTH} characters. A share with no stated reason reads "
            "to everyone downstream as an unexplained gap.",
        )
    if tolerance_ratio is not None and not (0 <= float(tolerance_ratio) <= 1):
        raise MetricGrainRefused(
            "tolerance_out_of_range",
            "A tolerance is a share of the total between 0 and 1. Enter 0.01 for "
            "one percent.",
        )

    before = declaration
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.metric_grain_breakdowns
                (id, declaration_id, datastream_id, project_id, sums_to, reason,
                 tolerance_ratio, created_by)
            VALUES (%(id)s, %(declaration_id)s, %(datastream_id)s, %(project_id)s,
                    %(sums_to)s, %(reason)s, %(tolerance)s, %(by)s)
            ON CONFLICT (declaration_id, datastream_id) DO UPDATE
            SET sums_to = EXCLUDED.sums_to,
                reason = EXCLUDED.reason,
                tolerance_ratio = EXCLUDED.tolerance_ratio
            RETURNING id
            """,
            {
                "id": _mint_id(_ID_PREFIXES["metric_grain_breakdowns"]),
                "declaration_id": declaration["id"],
                "datastream_id": datastream_id,
                "project_id": project_id,
                "sums_to": sums_to,
                "reason": (reason or None),
                "tolerance": tolerance_ratio,
                "by": identity,
            },
        )
        breakdown_id = str(cur.fetchone()[0])

    after = get_declaration(conn, project_id=project_id, concept_id=concept_id)
    _write_audit(
        conn,
        identity=identity,
        action="metric_grain_breakdown.upserted",
        entity_type="metric_grain_breakdown",
        entity_id=breakdown_id,
        project_id=project_id,
        before=before,
        after=after,
    )
    return after or {}


def withdraw_breakdown(
    conn: Any, *, project_id: str, concept_id: str, datastream_id: str, identity: str
) -> dict[str, Any]:
    """Remove one breakdown statement. The gap then reads `undeclared`, not gone."""
    declaration = get_declaration(conn, project_id=project_id, concept_id=concept_id)
    if declaration is None:
        raise MetricGrainRefused(
            "no_total_declared",
            "Nothing is declared for this measure yet, so there is nothing to withdraw.",
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM app.metric_grain_breakdowns
            WHERE declaration_id = %(declaration_id)s AND datastream_id = %(datastream_id)s
            RETURNING id
            """,
            {"declaration_id": declaration["id"], "datastream_id": datastream_id},
        )
        row = cur.fetchone()
    if row is None:
        raise MetricGrainRefused(
            "no_such_breakdown",
            "That Datastream carries no breakdown statement for this measure.",
        )
    after = get_declaration(conn, project_id=project_id, concept_id=concept_id)
    _write_audit(
        conn,
        identity=identity,
        action="metric_grain_breakdown.deleted",
        entity_type="metric_grain_breakdown",
        entity_id=str(row[0]),
        project_id=project_id,
        before=declaration,
        after=after,
    )
    return after or {}


# ---------------------------------------------------------------------------
# 3. The gap -- shown, never closed.
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def reconcile_breakdown(
    *,
    declaration: Mapping[str, Any] | None,
    breakdown_datastream_id: str,
    breakdown_sum: Any,
    total: Any,
) -> dict[str, Any]:
    """Compare one breakdown's sum against the declared total, and say what it is.

    Pure: it takes two numbers and a declaration and returns a verdict. Nothing is
    read, nothing is written, and **neither number is adjusted** -- the whole point
    of the exercise is that a product which quietly aligns two different figures
    is lying about one of them.

    ``unavailable`` when the total could not be produced. That is not a gap of
    zero, and it is not `undeclared` either: one means "we could not ask", the
    other means "nobody has said what this should be".
    """
    total_value = _as_float(total)
    sum_value = _as_float(breakdown_sum)

    rule: Mapping[str, Any] | None = None
    if declaration:
        for entry in declaration.get("breakdowns") or ():
            if str(entry.get("datastream_id")) == str(breakdown_datastream_id):
                rule = entry
                break

    base: dict[str, Any] = {
        "total": total_value,
        "breakdown_sum": sum_value,
        "gap": None,
        "gap_ratio": None,
        "sums_to": (rule or {}).get("sums_to"),
        "reason": (rule or {}).get("reason"),
        "tolerance_ratio": _as_float((rule or {}).get("tolerance_ratio")),
        "total_datastream_id": (declaration or {}).get("total_datastream_id"),
        "breakdown_datastream_id": str(breakdown_datastream_id),
    }

    if total_value is None or sum_value is None:
        return {
            **base,
            "verdict": VERDICT_UNAVAILABLE,
            "statement": (
                "The declared total for this measure could not be produced, so this "
                "breakdown is shown on its own. It is not reconciled."
            ),
        }

    gap = total_value - sum_value
    base["gap"] = gap
    base["gap_ratio"] = (abs(gap) / abs(total_value)) if total_value else None

    if rule is None:
        return {
            **base,
            "verdict": VERDICT_UNDECLARED,
            "statement": (
                "Nobody has stated whether this breakdown reconstitutes the total. "
                "The difference is shown as measured, with no reason claimed for it."
            ),
        }

    if str(rule.get("sums_to")) == SUMS_TO_PARTIAL:
        return {
            **base,
            "verdict": VERDICT_EXPECTED,
            "statement": str(rule.get("reason") or "").strip(),
        }

    tolerance = base["tolerance_ratio"] or 0.0
    ratio = base["gap_ratio"] or 0.0
    if ratio <= tolerance:
        return {
            **base,
            "verdict": VERDICT_RECONCILED,
            "statement": (
                "This breakdown reconstitutes the declared total within the "
                "tolerance stated for it."
            ),
        }
    return {
        **base,
        "verdict": VERDICT_UNEXPLAINED,
        "statement": (
            "This breakdown was declared to reconstitute the total and does not. "
            "Either the declaration is wrong, or rows are missing on one side."
        ),
    }


# ---------------------------------------------------------------------------
# 4. Audit -- same registry as the cross-source reconciliation rules (049).
# ---------------------------------------------------------------------------


def _write_audit(
    conn: Any,
    *,
    identity: str,
    action: str,
    entity_type: str,
    entity_id: str,
    project_id: str,
    before: Any,
    after: Any,
) -> None:
    """Insert one append-only row on the CALLER's transaction, so the mutation and
    its evidence commit or roll back together."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.metric_semantics_audit
                (id, identity, action, entity_type, entity_id, scope_level,
                 org_id, project_id, before, after)
            VALUES (%(id)s, %(identity)s, %(action)s, %(entity_type)s, %(entity_id)s,
                    'PROJECT', NULL, %(project_id)s, %(before)s, %(after)s)
            """,
            {
                "id": _mint_id("msaudit_"),
                "identity": identity,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "project_id": project_id,
                "before": json.dumps(before, default=str) if before is not None else None,
                "after": json.dumps(after, default=str) if after is not None else None,
            },
        )
