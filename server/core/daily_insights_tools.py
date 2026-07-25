"""Daily-insight MCP tool logic (Epic 35, Story 35.2).

Gate 35.0 decided A. These are the bounded operations the client-LLM scheduled task
calls: check J-1 readiness, discover what a project can render, preview a candidate
insight without publishing, and publish 0..3 insights atomically.

All logic lives here with INJECTED dependencies (availability, freshness, project scope,
existing slots, a connection, ``record_run``) so it is unit-testable offline and keeps the
contended ``server/core/main.py`` to a few thin wrappers. The module reuses, never rebuilds:
  - validation  -> ``daily_insights_schema.validate_published_insight`` (35.0);
  - catalogue   -> ``daily_insights_card_contract`` (35.1);
  - persistence -> ``daily_insights.record_run`` (35.3).

ASCII-only stdout (L-3).
"""

from __future__ import annotations

from core.daily_insights import InsightItem, record_run
from core.daily_insights_card_contract import (
    CARD_KEY_CONTRACT_VERSION,
    agent_card_catalog,
    recommend_card,
)
from core.daily_insights_schema import (
    SCHEMA_VERSION,
    validate_published_insight,
)

# ---------------------------------------------------------------------------
# 1. Readiness (J-1)
# ---------------------------------------------------------------------------


def readiness(
    *,
    insight_date: str,
    freshness_date: str | None,
    dq_blocking: list[str] | None = None,
) -> dict:
    """Decide whether J-1 data is ready for the target ``insight_date``.

    Returns a DISTINCT state: ``ready`` or ``blocked``. ``blocked`` is not "no insight" --
    it means data is not ready, so the task must stop, not publish. Reasons are actionable.
    """

    reasons: list[str] = []
    if not freshness_date:
        reasons.append("no resolved freshness; expected datastreams have not completed")
    elif freshness_date < insight_date:
        reasons.append(
            f"latest data {freshness_date} is behind target {insight_date} (J-1 not complete)"
        )
    for issue in dq_blocking or []:
        reasons.append(f"data quality: {issue}")

    status = "ready" if not reasons else "blocked"
    return {
        "status": status,
        "insight_date": insight_date,
        "freshness_date": freshness_date,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 2. Capability discovery
# ---------------------------------------------------------------------------


def capabilities(
    *,
    available_metrics: set[str],
    available_dimensions: set[str],
) -> dict:
    """What can this project render right now? -- the 35.1 catalogue + recommendation.

    Zero duplicated vocabulary: the catalogue and recommendation are derived from the
    existing card registry (``cards.py``) via the 35.1 contract.
    """

    return {
        "contractVersion": CARD_KEY_CONTRACT_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "metrics": sorted(available_metrics),
        "dimensions": sorted(available_dimensions),
        "catalog": agent_card_catalog(available_metrics, available_dimensions),
        "recommended": recommend_card(available_metrics, available_dimensions),
    }


# ---------------------------------------------------------------------------
# 3. Preview (no persistence)
# ---------------------------------------------------------------------------


def preview(
    *,
    payload: dict,
    available_metrics: set[str],
    available_dimensions: set[str],
    available_templates: set[str],
    resolvable_evidence: set[str],
    freshness_date: str | None,
    has_project_access: bool,
    existing_slots: set[int],
) -> dict:
    """Validate a candidate payload WITHOUT publishing anything.

    Runs the 35.0 fail-closed validator against the project's resolved availability,
    freshness and scope. Never persists, never renders a warehouse write. Returns the
    validation verdict so the agent can fix the spec before publishing.
    """

    result = validate_published_insight(
        payload,
        available_metrics=available_metrics,
        available_dimensions=available_dimensions,
        available_templates=available_templates,
        resolvable_evidence=resolvable_evidence,
        freshness_date=freshness_date,
        has_project_access=has_project_access,
        existing_slots=existing_slots,
    )
    return {
        "ok": result.ok,
        "reasonCode": result.reason_code,
        "message": result.message,
        "fieldPath": result.field_path,
        "slot": payload.get("slot"),
    }


# ---------------------------------------------------------------------------
# 4. Publish (atomic, all-or-nothing, idempotent)
# ---------------------------------------------------------------------------


def publish(
    *,
    project_id: str,
    insight_date: str,
    status: str,
    item_payloads: list[dict] | None = None,
    validate_ctx: dict,
    conn,
    render_snapshot_ids: dict[int, str] | None = None,
    render_fn=None,
    identity: str | None = None,
    trace_id: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    coverage: dict | None = None,
    host: str | None = None,
    prompt_version: str | None = None,
    record_run_fn=record_run,
) -> dict:
    """Validate every item fail-closed, then persist the run + items atomically.

    All-or-nothing: if ANY item fails validation, NOTHING is persisted and the first
    failing ``reason_code`` is returned (``ok=False``). On success, delegates to the
    transactional 35.3 ``record_run`` (idempotent per date/slot) and returns a short
    write-ack.

    ``status`` other than ``published`` (``no_insight`` / ``blocked`` / ``failed``) records
    a run with zero items -- a run that did not run is simply an ABSENT row, kept distinct.
    """

    item_payloads = item_payloads or []
    snapshot_ids = render_snapshot_ids or {}

    if status == "published":
        # Validate each payload against the project's resolved facts (fail-closed).
        for payload in item_payloads:
            slot = payload.get("slot")
            verdict = validate_published_insight(payload, **validate_ctx)
            if not verdict.ok:
                return {
                    "ok": False,
                    "reasonCode": verdict.reason_code,
                    "message": verdict.message,
                    "slot": slot,
                    "published": False,
                }
        # Render happens only AFTER every item validated (no orphan snapshots on rejection).
        items = []
        for p in item_payloads:
            slot = int(p["slot"])
            snap_id = snapshot_ids.get(slot)
            if snap_id is None and render_fn is not None:
                snap_id = render_fn(p)
            items.append(InsightItem(slot=slot, payload=p, render_snapshot_id=snap_id))
    else:
        items = []

    run_id = record_run_fn(
        project_id=project_id,
        insight_date=insight_date,
        status=status,
        items=items,
        period_from=period_from,
        period_to=period_to,
        coverage=coverage,
        host=host,
        prompt_version=prompt_version,
        contract_version=SCHEMA_VERSION,
        identity=identity,
        trace_id=trace_id,
        conn=conn,
    )
    return {
        "ok": True,
        "runId": run_id,
        "status": status,
        "publishedSlots": sorted(int(p["slot"]) for p in item_payloads)
        if status == "published"
        else [],
    }
