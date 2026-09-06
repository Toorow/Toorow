"""Publishing a daily insight produces a governed Result (AI-294, design 2026-08-17).

THE PIECE THIS MODULE IS. `proactive-assertions.md` answered the design question
"does publishing an insight produce a Result?" with yes, and named the cost: a
card is built from the card catalogue, not from a Query Spec, while a Share --
the ONE Share mechanism -- needs an immutable `app.renders` row, which needs a
real Result, which `app.query_results` only accepts with an `attempt_id` AND a
`query_spec_version_id`. The missing piece is therefore a governed Query Spec
BEHIND the card path, never a bridge between two frozen stores (the cheap route
over `app.render_snapshots` was refused as `governance.md`'s parallel-store
clause arriving at the Share layer).

WHAT IS DERIVED, AND FROM WHAT. The insight's card contract already names its
members: `daily_insights_schema._collect_metrics` / `_collect_dimensions` read
the metrics and dimensions the card actually draws, and `period` carries the
window the claim is about. Those canonical names are resolved BY STABLE NAME
against the project's semantic concepts, and the pair set is proven against the
compiled queryability matrix of the project's published Semantic View version --
by `query_specs.validate_query_spec`, the same validator Explore uses. No
near-miss, no substitution: an unresolvable member is a NAMED refusal.

WHO WRITES WHAT. Nothing new writes anywhere. The spec version is written by
`query_specs.create_query_spec_version` (the existing writer of
`app.query_specs` / `app.query_spec_versions`); the attempt and the Result are
written by `query_execution.accept_execution` + `run_execution` (the only
writer of `app.query_results`). This module only composes the request and
returns the LINEAGE -- `{query_spec_version_id, result_id, outcome}` -- which
`daily_insights.record_run` stores on the item (migration 281).

THE REFUSAL NEVER BLOCKS PUBLICATION. An insight is an editorial claim; the
Result is its governed evidence path. A project whose Semantic View does not
yet cover the card's members still publishes -- with
`result_unavailable_reason` naming the exact missing link, so the surface says
"not shareable, because X" instead of offering a control that fails. Every
failure path rolls back to a savepoint, so a refused derivation can never
poison the publish transaction it rides in.

SHARING IS UNCHANGED. With a Result named, the single existing mechanism
serves the insight end to end: a Render is created over the Result
(`analyze_artifacts.create_render`) and a Share over the Render
(`render_shares.create_share`). This module adds no share code and no reader.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: One place spells the reason a lineage is absent, so tests and surfaces can
#: match on the missing link rather than on prose.
REPORT_BACKED_CARD = "report_backed_card"
CONTEXT_CARD = "context_card"
CARD_NAMES_NO_METRIC = "card_names_no_metric"
METRIC_NOT_GOVERNED = "metric_not_governed"
DIMENSION_NOT_GOVERNED = "dimension_not_governed"
NO_SEMANTIC_VIEW = "no_semantic_view"
AMBIGUOUS_SEMANTIC_VIEW = "ambiguous_semantic_view"
NO_TIME_DIMENSION = "no_time_dimension"
AMBIGUOUS_TIME_DIMENSION = "ambiguous_time_dimension"
QUERY_SPEC_REFUSED = "query_spec_refused"
EXECUTION_FAILED = "execution_failed"


class InsightResultUnavailable(Exception):
    """The card could not be put behind a governed Query Spec. Named, never mute."""

    def __init__(self, missing_link: str, message: str):
        super().__init__(message)
        self.missing_link = missing_link

    def as_reason(self) -> dict[str, str]:
        return {
            "result_unavailable_reason": str(self),
            "missing_link": self.missing_link,
        }


def resolve_org_id(conn, project_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _card_member_names(payload: dict) -> tuple[set[str], set[str]]:
    """The canonical names the card contract actually draws. Pure.

    Reuses the schema module's collectors -- the same functions the publication
    validator reads -- so the derivation universe cannot drift from the gate.
    """
    from core.daily_insights_schema import (  # noqa: PLC0415
        _collect_dimensions,
        _collect_metrics,
    )

    card = (payload or {}).get("card") or {}
    return _collect_metrics(card), _collect_dimensions(card)


def _refuse_underivable_card(payload: dict) -> None:
    """The card shapes that cannot stand behind a Query Spec, refused by name.

    A report-backed card replays a Report, which has its own governed run path;
    a plan-scoped context card reads pacing marts, not fact metrics. Neither is
    an analytical request over governed members, and pretending otherwise would
    freeze a Result that answers a different question than the card shows.
    """
    card = (payload or {}).get("card") or {}
    if card.get("reportRef"):
        raise InsightResultUnavailable(
            REPORT_BACKED_CARD,
            "This card replays a named report. A report has its own governed run "
            "path; a report-backed insight does not derive a Query Spec.",
        )
    if card.get("planId"):
        raise InsightResultUnavailable(
            CONTEXT_CARD,
            "This card reads a media plan context, not governed fact metrics, "
            "so no Query Spec can stand behind it.",
        )


def derive_insight_query_spec(conn, *, project_id: str, payload: dict) -> dict[str, Any]:
    """Derive the analytical request behind one insight's card. Raises, by name.

    Returns ``{semantic_view_id, semantic_view_version_id, spec_payload}`` where
    ``spec_payload`` is the exact document `validate_query_spec` accepts. The
    resolution is exact-name only (AC3's posture): an unknown member, zero or
    several covering views, and a missing time dimension are each a distinct
    `InsightResultUnavailable` naming the link, never a guess.
    """
    _refuse_underivable_card(payload)
    metric_names, dimension_names = _card_member_names(payload)
    if not metric_names:
        raise InsightResultUnavailable(
            CARD_NAMES_NO_METRIC,
            "This card names no metric, so there is no analytical request to derive.",
        )

    wanted = sorted(metric_names | dimension_names)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, name FROM app.semantic_concepts
            WHERE (project_id = %s OR project_id IS NULL)
              AND lifecycle_status <> 'archived'
              AND name = ANY(%s)
            ORDER BY (project_id IS NULL), name
            """,
            (project_id, wanted),
        )
        rows = cur.fetchall()
    metric_ids: dict[str, str] = {}
    dimension_ids: dict[str, str] = {}
    for concept_id, kind, name in rows:
        # A project-scoped concept shadows a platform one of the same name: the
        # project's meaning wins, exactly as the unique indexes allow both to exist.
        target = metric_ids if kind == "metric" else dimension_ids
        target.setdefault(str(name), str(concept_id))

    missing_metrics = sorted(metric_names - set(metric_ids))
    if missing_metrics:
        raise InsightResultUnavailable(
            METRIC_NOT_GOVERNED,
            "These card metrics are not governed semantic concepts of this project: "
            + ", ".join(missing_metrics)
            + ". Declare them in the Semantic Model, then publish again.",
        )
    missing_dimensions = sorted(dimension_names - set(dimension_ids))
    if missing_dimensions:
        raise InsightResultUnavailable(
            DIMENSION_NOT_GOVERNED,
            "These card dimensions are not governed semantic concepts of this project: "
            + ", ".join(missing_dimensions)
            + ". Declare them in the Semantic Model, then publish again.",
        )

    measure_ids = sorted(metric_ids[name] for name in metric_names)
    split_ids = sorted(dimension_ids[name] for name in dimension_names)

    # The governed time dimensions of this scope, by the version's own marker.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id FROM app.semantic_concepts c
            JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
            WHERE (c.project_id = %s OR c.project_id IS NULL)
              AND c.kind = 'dimension'
              AND c.lifecycle_status <> 'archived'
              AND v.semantic_type = 'time'
            """,
            (project_id,),
        )
        time_concepts = {str(r[0]) for r in cur.fetchall()}

    # Which published Semantic View version PROVES this request. The matrix is
    # the one authority (query_specs.py's rule); this only asks which pinned
    # matrix covers every member, and refuses zero and several alike.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.current_version_id
            FROM app.semantic_views s
            JOIN app.semantic_view_versions v ON v.id = s.current_version_id
            WHERE s.project_id = %s AND v.status = 'published'
            ORDER BY s.id
            """,
            (project_id,),
        )
        views = [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    candidates: list[tuple[str, str, list[str]]] = []
    for view_id, version_id in views:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT queryability_matrix FROM app.semantic_compiled_artifacts
                WHERE view_version_id = %s AND project_id = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (version_id, project_id),
            )
            compiled = cur.fetchone()
        matrix = dict(compiled[0]) if compiled and compiled[0] else {}
        matrix_metrics = {
            str(m.get("concept_id")) for m in matrix.get("metrics") or [] if m.get("concept_id")
        }
        matrix_dimensions = {
            str(d.get("concept_id"))
            for d in matrix.get("dimensions") or []
            if d.get("concept_id")
        }
        view_time = sorted(matrix_dimensions & time_concepts)
        if (
            set(measure_ids) <= matrix_metrics
            and set(split_ids) <= matrix_dimensions
            and view_time
        ):
            candidates.append((view_id, version_id, view_time))

    if not candidates:
        raise InsightResultUnavailable(
            NO_SEMANTIC_VIEW,
            "No published Semantic View of this project proves every member of this "
            "card together with a time dimension. Open the Semantic Model and "
            "publish a view that carries them.",
        )
    if len(candidates) > 1:
        raise InsightResultUnavailable(
            AMBIGUOUS_SEMANTIC_VIEW,
            f"{len(candidates)} published Semantic Views can answer this card, so "
            "which one answers is a decision this derivation refuses to take.",
        )
    view_id, version_id, view_time = candidates[0]
    if len(view_time) > 1:
        raise InsightResultUnavailable(
            AMBIGUOUS_TIME_DIMENSION,
            f"{len(view_time)} time dimensions are queryable on this Semantic View, "
            "so which one carries the insight's window is a decision this "
            "derivation refuses to take.",
        )
    time_member = view_time[0]

    period = (payload or {}).get("period") or {}
    dimensions = [{"id": time_member}] + [
        {"id": member} for member in split_ids if member != time_member
    ]
    spec_payload = {
        "measures": [{"id": member} for member in measure_ids],
        "dimensions": dimensions,
        "filters": [],
        "sort": [],
        "comparison": "none",
        "grain": "day",
        "time": {
            "member_id": time_member,
            "start": str(period.get("dateFrom") or "") or None,
            "end": str(period.get("dateTo") or "") or None,
        },
    }
    return {
        "semantic_view_id": view_id,
        "semantic_view_version_id": version_id,
        "spec_payload": spec_payload,
    }


def produce_insight_result(
    conn, *, org_id: str, project_id: str, payload: dict, actor: str
) -> dict[str, Any]:
    """Derive, validate, version and execute -- or return the named reason.

    Returns either ``{"query_spec_version_id", "result_id", "outcome"}`` (the
    lineage `record_run` stores) or ``{"result_unavailable_reason",
    "missing_link"}``. Never raises for a derivation refusal, and never leaves
    the caller's transaction poisoned: everything runs under a savepoint that is
    rolled back on any failure.
    """
    from core import query_execution  # noqa: PLC0415
    from core.query_specs import (  # noqa: PLC0415
        QuerySpecNotFound,
        QuerySpecRefused,
        create_query_spec_version,
        validate_query_spec,
    )

    savepoint = "daily_insight_result"
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {savepoint}")
    try:
        derived = derive_insight_query_spec(conn, project_id=project_id, payload=payload)
        validated = validate_query_spec(
            conn,
            project_id=project_id,
            semantic_view_id=derived["semantic_view_id"],
            semantic_view_version_id=derived["semantic_view_version_id"],
            payload=derived["spec_payload"],
        )
        period = (payload or {}).get("period") or {}
        slot = (payload or {}).get("slot")
        created = create_query_spec_version(
            conn,
            org_id=org_id,
            project_id=project_id,
            validated=validated,
            actor=actor,
            name=f"daily-insight {period.get('dateTo') or ''} slot {slot}".strip(),
        )
        version_id = str(created.get("id") or "")
        attempt = query_execution.accept_execution(
            conn,
            org_id=org_id,
            project_id=project_id,
            query_spec_version_id=version_id,
            actor=actor,
        )
        result = query_execution.run_execution(
            conn,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            spec=validated.spec,
            semantic_view_version_id=derived["semantic_view_version_id"],
        )
        with conn.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        return {
            "query_spec_version_id": version_id,
            "result_id": str(result["result_id"]),
            "outcome": str(result["outcome"]),
        }
    except InsightResultUnavailable as exc:
        _rollback_to(conn, savepoint)
        return exc.as_reason()
    except QuerySpecRefused as exc:
        _rollback_to(conn, savepoint)
        detail = "; ".join(r.message for r in exc.refusals) or str(exc)
        return {
            "result_unavailable_reason": f"The derived request was refused: {detail}",
            "missing_link": QUERY_SPEC_REFUSED,
        }
    except QuerySpecNotFound:
        _rollback_to(conn, savepoint)
        return {
            "result_unavailable_reason": (
                "The Semantic View this derivation pinned is not resolvable in "
                "this project."
            ),
            "missing_link": NO_SEMANTIC_VIEW,
        }
    except Exception as exc:  # noqa: BLE001 -- publication must not die on its evidence path
        _rollback_to(conn, savepoint)
        logger.warning(
            "daily_insight_result: execution failed project=%s: %s: %s",
            project_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return {
            "result_unavailable_reason": (
                "The governed execution could not produce a Result: "
                f"{type(exc).__name__}: {exc}"
            ),
            "missing_link": EXECUTION_FAILED,
        }


def _rollback_to(conn, savepoint: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as exc:  # noqa: BLE001 -- a broken connection reports, never masks
        logger.warning("daily_insight_result: savepoint rollback failed: %s", exc)


def lineage_ack(lineage: dict[str, Any] | None, slot: int) -> dict[str, Any]:
    """The per-slot acknowledgement block the publish ack carries. Pure."""
    if not lineage:
        return {"slot": slot, "resultId": None, "resultUnavailableReason": None}
    if lineage.get("result_id"):
        return {
            "slot": slot,
            "resultId": lineage["result_id"],
            "querySpecVersionId": lineage.get("query_spec_version_id"),
            "outcome": lineage.get("outcome"),
        }
    return {
        "slot": slot,
        "resultId": None,
        "resultUnavailableReason": lineage.get("result_unavailable_reason"),
        "missingLink": lineage.get("missing_link"),
    }


__all__ = [
    "InsightResultUnavailable",
    "derive_insight_query_spec",
    "lineage_ack",
    "produce_insight_result",
    "resolve_org_id",
]
