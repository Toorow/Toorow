"""Story 36.8 -- bounded first-report draft recommend/save/preview tests.

Offline MagicMock-style, mirroring test_epic36_source_delegation.py: the shared
Epic 12 plan seam, the operation seam, the mapping profiler and the DQ/publication
gates are stubbed so the recommendation logic and the immutable-version / non-
publishing invariants are exercised WITHOUT live Postgres. Live-PG coverage
(pointer non-advance, immutability triggers, RLS) is the established follow-up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures: a minimal governed capability catalog with two reports.
# ---------------------------------------------------------------------------
def _field(field_id, kind, *, physical_type="string", currency="unknown"):
    return {
        "field_id": field_id,
        "source_field": field_id,
        "kind": kind,
        "physical_type": physical_type,
        "description": field_id,
        "semantic_hints": [],
        "canonical_target": None,
        "aggregation": "sum" if kind == "metric" else "none",
        "non_additive": False,
        "currency": currency,
    }


def _report(report_id, *, metrics, dimensions, grains, selectable=True, read_points=5):
    return {
        "id": report_id,
        "availability": {"status": "selectable" if selectable else "unavailable"},
        "metrics": list(metrics),
        "dimensions": list(dimensions),
        "supported_grains": [list(g) for g in grains],
        "quota_cost": {"read_points": read_points, "unit": "read_points"},
        "compatibility": [],
        "filters": [],
    }


def _capabilities(reports, fields):
    return {
        "contract_version": "1",
        "project_id": "proj-1",
        "connection_ref_id": "conn-1",
        "module": {"name": "demo_source", "display_name": "Demo", "module_kind": "kpi"},
        "fields": fields,
        "reports": reports,
    }


def _one_report_catalog():
    fields = [
        _field("date", "dimension", physical_type="date"),
        _field("spend", "metric", physical_type="decimal", currency="EUR"),
        _field("campaign", "dimension"),
    ]
    reports = [
        _report(
            "campaign_daily",
            metrics=["spend"],
            dimensions=["date", "campaign"],
            grains=[["date"], ["date", "campaign"]],
        )
    ]
    return _capabilities(reports, fields)


def _account():
    return {"credential_id": "cred-1", "external_account_id": "acct-1"}


def _cur(*fetchone_rows):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.rowcount = 1
    return cur


def _conn(cur):
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


# ---------------------------------------------------------------------------
# (a) recommends exactly one compatible report + bounded interval + explicit
#     grain/timezone/currency/estimated cost.
# ---------------------------------------------------------------------------
def test_recommends_single_report_with_bounded_interval_and_explicit_choices():
    from core import first_report_draft as frd

    conn = _conn(_cur((30,)))  # date_window_days
    rec = frd.recommend_first_report(
        conn,
        project_id="proj-1",
        capabilities=_one_report_catalog(),
        account=_account(),
        actor="camille@example.com",
        datastream_id="ds-1",
        now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )

    assert rec.outcome == "recommended"
    assert rec.report_id == "campaign_daily"
    # Smallest supported grain deterministically -> ["date"], never fabricated.
    assert rec.grain == ["date"]
    assert rec.metrics == ["spend"]
    # Explicit timezone + currency (single monetary metric currency) + cost.
    assert rec.timezone == "UTC"
    assert rec.currency == "EUR"
    assert rec.interval is not None
    assert rec.interval.window_days == 30
    assert rec.interval.end_exclusive == "2026-07-22T00:00:00Z"
    assert rec.interval.start == "2026-06-22T00:00:00Z"
    assert rec.estimated_cost is not None
    assert rec.estimated_cost.read_points == 5 * 30
    assert rec.capability_contract_version == "1"
    assert rec.capability_fingerprint and len(rec.capability_fingerprint) == 64


# ---------------------------------------------------------------------------
# (b) 0/many candidates -> structured needs-choice / no-safe, never a silent pick.
# ---------------------------------------------------------------------------
def test_no_eligible_account_returns_needs_choice_without_picking():
    from core import first_report_draft as frd

    conn = _conn(_cur((30,)))
    rec = frd.recommend_first_report(
        conn,
        project_id="proj-1",
        capabilities=_one_report_catalog(),
        account=None,  # no exposed eligible account
        actor="camille@example.com",
        datastream_id="ds-1",
    )
    assert rec.outcome == "needs_choice"
    assert rec.report_id is None
    assert rec.compatibility["reason"] == "no_eligible_account"


def test_multiple_compatible_reports_require_choice_never_silent():
    from core import first_report_draft as frd

    fields = [
        _field("date", "dimension", physical_type="date"),
        _field("spend", "metric", physical_type="decimal", currency="EUR"),
        _field("clicks", "metric", physical_type="integer"),
        _field("campaign", "dimension"),
    ]
    reports = [
        _report("spend_daily", metrics=["spend"], dimensions=["date"], grains=[["date"]]),
        _report("clicks_daily", metrics=["clicks"], dimensions=["date"], grains=[["date"]]),
    ]
    conn = _conn(_cur((30,)))
    rec = frd.recommend_first_report(
        conn,
        project_id="proj-1",
        capabilities=_capabilities(reports, fields),
        account=_account(),
        actor="camille@example.com",
        datastream_id="ds-1",
    )
    assert rec.outcome == "needs_choice"
    assert rec.report_id is None  # never a silent pick
    assert rec.compatibility["reason"] == "multiple_compatible_reports"
    assert {c["report_id"] for c in rec.candidates} == {"spend_daily", "clicks_daily"}


def test_no_selectable_report_returns_no_safe_recommendation():
    from core import first_report_draft as frd

    fields = [_field("date", "dimension", physical_type="date"), _field("spend", "metric")]
    reports = [
        _report(
            "campaign_daily",
            metrics=["spend"],
            dimensions=["date"],
            grains=[["date"]],
            selectable=False,
        )
    ]
    conn = _conn(_cur((30,)))
    rec = frd.recommend_first_report(
        conn,
        project_id="proj-1",
        capabilities=_capabilities(reports, fields),
        account=_account(),
        actor="camille@example.com",
        datastream_id="ds-1",
    )
    assert rec.outcome == "no_safe_recommendation"
    assert rec.compatibility["reason"] == "no_selectable_report"


# ---------------------------------------------------------------------------
# (c) save creates immutable plan + mapping versions capturing capability/catalog
#     versions, routed through execute_operation.
# ---------------------------------------------------------------------------
def test_save_creates_immutable_plan_and_mapping_versions(monkeypatch):
    from core import first_report_draft as frd

    plan = {
        "id": "dsp_PLAN1",
        "datastream_id": "ds-1",
        "executable": True,
        "capability_contract_version": "1",
        "capability_fingerprint": "a" * 64,
    }
    save_intent = MagicMock(return_value=plan)
    monkeypatch.setattr(frd, "save_datastream_intent", save_intent, raising=False)
    # Patch the lazily-imported symbol inside the module namespace too.
    import core.datastream_intents as di

    monkeypatch.setattr(di, "save_datastream_intent", save_intent)

    captured = {}

    def execute(operation_conn, spec, *, mutation):
        from core import operations

        changed = mutation(operation_conn, "op-1")
        captured["spec"] = spec
        captured["changed"] = changed
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(frd, "execute_operation", execute)

    cur = _cur((3,))  # next mapping version_number
    conn = _conn(cur)

    saved = frd.save_first_report_draft(
        conn,
        project_id="proj-1",
        datastream_id="ds-1",
        connection_ref_id="conn-1",
        report_id="campaign_daily",
        metrics=["spend"],
        dimensions=["date"],
        grain=["date"],
        timezone="Europe/Paris",
        currency="EUR",
        interval={"start": "2026-06-22T00:00:00Z", "end_exclusive": "2026-07-22T00:00:00Z"},
        capabilities=_one_report_catalog(),
        actor="camille@example.com",
        idempotency_key="draft-1",
        effective_org_id="org-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    # The Epic 12 plan seam was used (no second engine) with the catalog + a scoped
    # idempotency key, and commit=False so the write composes atomically.
    save_intent.assert_called_once()
    kwargs = save_intent.call_args.kwargs
    assert kwargs["commit"] is False
    assert kwargs["idempotency_key"] == "draft-1:plan"
    assert kwargs["capabilities"]["contract_version"] == "1"

    # The mapping version was INSERTED (immutable) WITHOUT a current_mapping_version_id swap.
    all_sql = " ".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "INSERT INTO app.datastream_mapping_versions" in all_sql
    assert "INSERT INTO app.first_report_drafts" in all_sql
    assert "current_mapping_version_id" not in all_sql  # live pointer NEVER advanced

    # The operation captured the capability/catalog versions.
    assert captured["spec"].command_type == "first_report.draft.save"
    assert captured["spec"].versions["catalog"] == "1"
    assert saved.plan_version_id == "dsp_PLAN1"
    assert saved.recommendation["capability_fingerprint"] == "a" * 64
    assert saved.recommendation["currency"] == "EUR"
    conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# (d) preview references saved versions and does NOT publish / advance pointers.
# ---------------------------------------------------------------------------
def test_preview_references_saved_versions_and_never_publishes(monkeypatch):
    import core.datastream_publication as pub
    from core import first_report_draft as frd

    # Any accidental publish/advance attempt must be observable as a failure.
    commit_spy = MagicMock(side_effect=AssertionError("commit_publication must not be called"))
    advance_spy = MagicMock(side_effect=AssertionError("advance_state must not be called"))
    monkeypatch.setattr(pub, "commit_publication", commit_spy)
    monkeypatch.setattr(pub, "advance_state", advance_spy)

    plan_row = ("dsp_PLAN1", "ds-1", True, "a" * 64)
    mapping_row = ("dmap_MAP1", "ds-1", True, 0, "b" * 64, "c" * 64, "dsp_PLAN1")
    cur = _cur(plan_row, mapping_row)
    conn = _conn(cur)

    preview = frd.preview_first_report(
        conn,
        project_id="proj-1",
        plan_version_id="dsp_PLAN1",
        mapping_version_id="dmap_MAP1",
        actor="camille@example.com",
        draft_id="frdraft_1",
        row_count=42,
        content_hash="c" * 64,
    )

    assert preview.plan_version_id == "dsp_PLAN1"
    assert preview.mapping_version_id == "dmap_MAP1"
    assert preview.executable is True
    assert preview.can_publish is False  # INVARIANT
    # Bounded/redacted evidence: counts + hashes only, no rows / provider payloads.
    assert preview.evidence["row_count"] == 42
    assert preview.evidence["mapping_content_hash"] == "c" * 64
    assert "rows" not in preview.evidence and "provider_payload" not in preview.evidence
    commit_spy.assert_not_called()
    advance_spy.assert_not_called()


def test_preview_rejects_inconsistent_saved_versions():
    from core import first_report_draft as frd

    plan_row = ("dsp_PLAN1", "ds-1", True, "a" * 64)
    # mapping references a DIFFERENT plan version -> inconsistent draft.
    mapping_row = ("dmap_MAP1", "ds-1", True, 0, "b" * 64, "c" * 64, "dsp_OTHER")
    conn = _conn(_cur(plan_row, mapping_row))
    with pytest.raises(frd.FirstReportDraftUnavailable):
        frd.preview_first_report(
            conn,
            project_id="proj-1",
            plan_version_id="dsp_PLAN1",
            mapping_version_id="dmap_MAP1",
            actor="camille@example.com",
        )


# ---------------------------------------------------------------------------
# (e) an unsupported metric/dimension/grain is rejected -- no silent grain.
# ---------------------------------------------------------------------------
def test_unsupported_grain_never_recommended():
    from core import first_report_draft as frd

    fields = [_field("campaign", "dimension"), _field("spend", "metric", currency="EUR")]
    # Report declares NO date dimension and its only grain is a non-date grain; the
    # recommender must never invent an unsupported grain -- it recommends the
    # smallest DECLARED supported grain, and rejects anything outside supported_grains.
    reports = [
        _report(
            "no_date_report",
            metrics=["spend"],
            dimensions=["campaign"],
            grains=[["campaign"]],
        )
    ]
    conn = _conn(_cur((30,)))
    rec = frd.recommend_first_report(
        conn,
        project_id="proj-1",
        capabilities=_capabilities(reports, fields),
        account=_account(),
        actor="camille@example.com",
        datastream_id="ds-1",
    )
    # A safe candidate exists but its grain is strictly the declared one.
    assert rec.outcome == "recommended"
    assert rec.grain == ["campaign"]
    assert set(rec.grain).issubset(set(reports[0]["dimensions"]))


def test_first_report_routes_registered():
    from core.admin_api import router

    paths = {route.path for route in router.routes}
    base = "/api/projects/{project_id}/datastreams/{datastream_id}/first-report"
    assert f"{base}/recommend" in paths
    assert f"{base}/draft" in paths
    assert f"{base}/preview" in paths


def test_save_rejects_incomplete_selection():
    from core import first_report_draft as frd

    conn = _conn(_cur())
    with pytest.raises(frd.FirstReportDraftValidationError):
        frd.save_first_report_draft(
            conn,
            project_id="proj-1",
            datastream_id="ds-1",
            connection_ref_id="conn-1",
            report_id="campaign_daily",
            metrics=[],  # unsupported empty selection
            dimensions=["date"],
            grain=["date"],
            timezone="UTC",
            currency="EUR",
            interval={"start": "2026-06-22T00:00:00Z", "end_exclusive": "2026-07-22T00:00:00Z"},
            capabilities=_one_report_catalog(),
            actor="camille@example.com",
            idempotency_key="draft-x",
            effective_org_id="org-1",
            host_context={"host": "rest"},
        )
