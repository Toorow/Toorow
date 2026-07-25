"""Tests for the metric-semantics MCP surface (Story 27.6, Epic 27).

Offline, no DB: MagicMock/patch at the lazy-import source (core.metric_semantics.*,
core.metric_reconciliation.*, core.project_access.*, core.db.get_connection). A FakeMCP
captures the mcp.tool(fn) registrations of register(mcp) so the handlers can be invoked
directly. The MCP identity is simulated by patching get_access_token.

Coverage: registration/shape, guards (org read/manage, IDOR F-1, project leak F-3, PLATFORM
forbidden, anonymous), reference contract + synonyms/ai_context, route serialisation for the
6 statuses + F-4 empty-series filtering + determinism, curation delegation + created_by
propagation, and verified queries (canonical_correct only, filters, limit, fail-soft,
read-only).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core import metric_semantics_mcp as msm  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeMCP:
    """Capture mcp.tool(fn) registrations so register(mcp) exposes its handlers."""

    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def _register():
    mcp = FakeMCP()
    msm.register(mcp)
    return mcp.tools


def _token(sub="user@toorow.io"):
    tok = MagicMock()
    tok.claims = {"sub": sub}
    tok.client_id = "client-abc"
    return tok


def _patch_identity(sub="user@toorow.io"):
    # get_access_token is lazy-imported inside _identity(); patch it at the source module.
    return patch(
        "fastmcp.server.dependencies.get_access_token", return_value=_token(sub)
    )


def _err_code(excinfo) -> str:
    import json

    return json.loads(excinfo.value.args[0])["code"]


class _FakeSeries:
    def __init__(self, connector, canonical_name):
        self.connector = connector
        self.canonical_name = canonical_name


class _FakeReason:
    def __init__(self, code, message, emitters=()):
        self.code = code
        self.message = message
        self.emitters = tuple(emitters)


class _FakeDecision:
    def __init__(
        self,
        *,
        metric,
        status,
        method=None,
        scope_level=None,
        overlap_group_id=None,
        target_mart=None,
        series=(),
        priority_order=(),
        reason=None,
    ):
        self.metric = metric
        self.status = status
        self.method = method
        self.scope_level = scope_level
        self.overlap_group_id = overlap_group_id
        self.target_mart = target_mart
        self.series = tuple(series)
        self.priority_order = tuple(priority_order)
        self.reason = reason


# ---------------------------------------------------------------------------
# 1-3: registration & shape & AD-2
# ---------------------------------------------------------------------------


def test_register_exposes_all_tools():
    tools = _register()
    for name in (
        "metric_reference",
        "metric_route",
        "metric_verified_queries",
        "metric_mapping_confirm",
        "metric_mapping_rename",
        "metric_mapping_reject",
        "metric_definition_upsert",
    ):
        assert name in tools


def test_envelope_shape():
    env = msm._envelope({"x": 1})
    assert env["schema_version"] == "1"
    assert set(env["meta"]) == {"freshness", "provenance", "alerts"}
    assert env["data"] == {"x": 1}


def test_no_provider_names_hardcoded():
    """AD-2: the module source contains no known connector name."""
    import pathlib

    src = pathlib.Path(msm.__file__).read_text(encoding="utf-8").lower()
    for provider in (
        "google-analytics",
        "meta-ads",
        "google-ads",
        "doubleverify",
        "pinterest",
        "twitter",
        "amazon-ads",
    ):
        assert provider not in src


# ---------------------------------------------------------------------------
# 4-10: guards
# ---------------------------------------------------------------------------


def test_reference_missing_org_id():
    tools = _register()
    with _patch_identity():
        with pytest.raises(ToolError) as ei:
            tools["metric_reference"](org_id="")
    assert _err_code(ei) == "missing_param"


def test_reference_non_member_not_found_no_builder():
    tools = _register()
    builder = MagicMock()
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_read", return_value=False),
        patch("core.db.get_connection", return_value=_ctx()),
        patch("core.metric_semantics_mcp._build_reference", builder),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_reference"](org_id="orgA")
    assert _err_code(ei) == "not_found"
    builder.assert_not_called()


def test_mutation_non_manage_forbidden_no_upsert():
    tools = _register()
    upsert = MagicMock()
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_manage", return_value=False),
        patch("core.db.get_connection", return_value=_ctx()),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_mapping_confirm"](org_id="orgA", mapping_id="smm_1")
    assert _err_code(ei) == "forbidden"
    upsert.assert_not_called()


def test_idor_mapping_of_other_org_not_found_no_upsert():
    """F-1: manage(orgA) passes but the mapping is orgB -> not_found, no mutation."""
    tools = _register()
    upsert = MagicMock()
    other_org_mapping = {
        "id": "smm_1",
        "org_id": "orgB",
        "scope_level": "ORG",
        "metric_definition_id": "metdef_1",
        "connector": "x",
    }
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch(
            "core.metric_semantics_api._get_mapping_by_id",
            return_value=other_org_mapping,
        ),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_mapping_confirm"](org_id="orgA", mapping_id="smm_1")
    assert _err_code(ei) == "not_found"
    upsert.assert_not_called()


def test_project_leak_foreign_project_not_found():
    """F-3: metric_reference(orgA, project of orgB) -> not_found, builder not called."""
    tools = _register()
    builder = MagicMock()

    def _cursor_returns_other_org():
        # project lookup returns org_id = 'orgB' != 'orgA'
        return _ctx(fetchone=("orgB",))

    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_read", return_value=True),
        patch("core.db.get_connection", side_effect=lambda: _cursor_returns_other_org()),
        patch("core.metric_semantics_mcp._build_reference", builder),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_reference"](org_id="orgA", project_id="projB")
    assert _err_code(ei) == "not_found"
    builder.assert_not_called()


def test_definition_upsert_platform_forbidden():
    tools = _register()
    with _patch_identity():
        with pytest.raises(ToolError) as ei:
            tools["metric_definition_upsert"](
                org_id="orgA",
                scope_level="PLATFORM",
                canonical_name="revenue",
                aggregation_type="SUM",
            )
    assert _err_code(ei) == "forbidden"


def test_anonymous_identity_rejected_by_guard():
    """No token -> identity 'anonymous' -> guard denies (non-member) -> not_found."""
    tools = _register()
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=None),
        patch("core.metric_semantics_api._require_org_read", return_value=False),
        patch("core.db.get_connection", return_value=_ctx()),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_reference"](org_id="orgA")
    assert _err_code(ei) == "not_found"


# ---------------------------------------------------------------------------
# 11-13: reference contract
# ---------------------------------------------------------------------------


def _def_row(**over):
    base = {
        "id": "metdef_rev",
        "canonical_name": "revenue",
        "display_name": "Revenue",
        "aggregation_type": "SUM",
        "additive": True,
        "ratio_numerator": None,
        "ratio_denominator": None,
        "format": None,
        "unit": None,
        "currency_mode": None,
        "non_additive_dimensions": [],
        "synonyms": [{"lang": "fr", "terms": ["CA"]}],
        "ai_context": "Chiffre d'affaires",
        "certified": True,
        "scope_level": "PLATFORM",
    }
    base.update(over)
    return base


def test_reference_contract_keys_and_synonyms():
    tools = _register()
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_read", return_value=True),
        patch("core.db.get_connection", return_value=_ctx(fetchall=[])),
        patch(
            "core.metric_semantics._load_definition_rows", return_value=[_def_row()]
        ),
        patch(
            "core.metric_semantics.reduce_definitions_by_specificity",
            return_value={"revenue": _def_row()},
        ),
        patch("core.metric_semantics._load_reconciliation_rows", return_value=[]),
        patch(
            "core.metric_semantics.reduce_reconciliation_by_specificity",
            return_value=None,
        ),
    ):
        res = tools["metric_reference"](org_id="orgA")
    metric = res.structured_content["data"]["metrics"][0]
    for key in (
        "canonical_name",
        "synonyms",
        "ai_context",
        "resolved_scope",
        "reconciliation",
        "source_mappings",
    ):
        assert key in metric
    assert metric["reconciliation"] is None  # reduce returned None
    assert metric["synonyms"] == [{"lang": "fr", "terms": ["CA"]}]
    assert metric["ai_context"] == "Chiffre d'affaires"
    assert metric["resolved_scope"] == "PLATFORM"


@pytest.mark.anyio
async def test_reference_contract_locked_across_rest_and_mcp():
    """C-2 (anti-divergence): the /reference contract is duplicated between the REST route
    (_reference) and the MCP tool (_build_reference). Drive BOTH with the SAME fake store
    and assert their per-metric ``metrics`` structure is IDENTICAL (same keys, same values).

    This is the inter-surface LOCK: adding/renaming a field on only ONE side makes the two
    structures diverge and fails here. No shared helper is extracted (the two files have
    different owners); the test is the contract guarantee."""
    import json as _json

    from core.metric_semantics_api import _reference

    # One shared def row + one shared reconciliation rule + one shared source mapping.
    def_row = _def_row()
    rec_rule = {
        "method": "PRIORITY",
        "priority_order": ["src-a", "src-b"],
        "join_key": None,
        "truth_connector": None,
        "scope_level": "PLATFORM",
    }
    # Positional source-mapping row: (metric_definition_id, connector, source_field_path, status)
    mapping_rows = [("metdef_rev", "src-a", "revenue", "proposed")]

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda s: fake_cursor
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.description = [
        ("metric_definition_id",), ("connector",), ("source_field_path",), ("status",)
    ]
    fake_cursor.fetchall.return_value = mapping_rows

    fake_conn = MagicMock()
    fake_conn.__enter__ = lambda s: fake_conn
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    from contextlib import ExitStack

    def _store_patches():
        # Fresh patchers each call (a patcher cannot be entered twice).
        return (
            patch("core.metric_semantics._load_definition_rows", return_value=[def_row]),
            patch(
                "core.metric_semantics.reduce_definitions_by_specificity",
                return_value={"revenue": def_row},
            ),
            patch(
                "core.metric_semantics._load_reconciliation_rows", return_value=[rec_rule]
            ),
            patch(
                "core.metric_semantics.reduce_reconciliation_by_specificity",
                return_value=rec_rule,
            ),
            patch("core.db.get_connection", return_value=fake_conn),
        )

    # --- MCP surface -----------------------------------------------------------
    tools = _register()
    with ExitStack() as stack:
        stack.enter_context(_patch_identity())
        stack.enter_context(
            patch("core.metric_semantics_api._require_org_read", return_value=True)
        )
        for p in _store_patches():
            stack.enter_context(p)
        mcp_res = tools["metric_reference"](org_id="orgA")
    mcp_metrics = mcp_res.structured_content["data"]["metrics"]

    # --- REST surface (same fakes) --------------------------------------------
    rest_req = MagicMock()
    rest_req.query_params = {"org_id": "orgA"}
    rest_req.path_params = {}
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "core.metric_semantics_api._check_auth",
                new=AsyncMock(return_value=(True, "user@toorow.io")),
            )
        )
        stack.enter_context(
            patch("core.metric_semantics_api._require_org_read", return_value=True)
        )
        for p in _store_patches():
            stack.enter_context(p)
        rest_resp = await _reference(rest_req)
    rest_metrics = _json.loads(rest_resp.body)["metrics"]

    # LOCK: the per-metric structure must be byte-for-byte equal across surfaces.
    assert mcp_metrics == rest_metrics
    # And non-empty, so the equality is meaningful (a metric + its reconciliation + mapping).
    assert mcp_metrics and mcp_metrics[0]["reconciliation"] is not None
    assert mcp_metrics[0]["source_mappings"]


# ---------------------------------------------------------------------------
# 14-22: route serialisation (pure) + F-4 filtering + determinism
# ---------------------------------------------------------------------------


def test_route_routed_to_mart():
    d = _FakeDecision(
        metric="revenue",
        status="ROUTED_TO_MART",
        method="PRIORITY",
        target_mart="cross_source_revenue",
        priority_order=("a", "b"),
    )
    out = msm._route_decision_to_dict(d, emitters=("a", "b"))
    assert out["target_mart"] == "cross_source_revenue"
    assert out["series_declared"] == []
    assert out["series_present"] == []
    assert out["reason"] is None
    assert "cross_source_revenue" in msm._route_summary(out)


def test_route_keep_separate_lists_series_no_total():
    d = _FakeDecision(
        metric="cost",
        status="KEEP_SEPARATE",
        method="KEEP_SEPARATE",
        series=(_FakeSeries("a", "cost"), _FakeSeries("b", "cost")),
    )
    out = msm._route_decision_to_dict(d, emitters=("a", "b"))
    assert out["target_mart"] is None
    assert out["reason"] is None
    assert [s["connector"] for s in out["series_declared"]] == ["a", "b"]
    summary = msm._route_summary(out).lower()
    # Honest: enumerates per-source series "not to be added", never a combined total.
    assert "ne pas additionner" in summary


def test_route_unruled_overlap_invitation_no_total():
    d = _FakeDecision(
        metric="conversions",
        status="UNRULED_OVERLAP",
        series=(_FakeSeries("a", "conversions"), _FakeSeries("b", "conversions")),
        reason=_FakeReason("UNRULED_OVERLAP", "configure a rule to combine", ("a", "b")),
    )
    out = msm._route_decision_to_dict(d, emitters=("a", "b"))
    assert out["reason"]["code"] == "UNRULED_OVERLAP"
    summary = msm._route_summary(out).lower()
    assert "configure" in summary
    # The summary explicitly disclaims any combined total (honest, AD-9).
    assert "jamais de total combine" in summary


def test_route_override_not_materialized():
    d = _FakeDecision(
        metric="revenue",
        status="OVERRIDE_NOT_MATERIALIZED",
        target_mart=None,
        series=(_FakeSeries("a", "revenue"),),
        reason=_FakeReason("OVERRIDE_NOT_MATERIALIZED", "phase B", ("a",)),
    )
    out = msm._route_decision_to_dict(d, emitters=("a",))
    assert out["target_mart"] is None
    assert out["series_present"] == [{"connector": "a", "canonical_name": "revenue"}]
    assert out["reason"]["code"] == "OVERRIDE_NOT_MATERIALIZED"


def test_route_not_combinable():
    d = _FakeDecision(
        metric="average_position",
        status="NOT_COMBINABLE",
        reason=_FakeReason("NON_ADDITIVE_NO_RULE", "cannot be summed", ("a",)),
    )
    out = msm._route_decision_to_dict(d, emitters=("a",))
    assert out["series_present"] == []
    assert out["reason"]["code"] == "NON_ADDITIVE_NO_RULE"


def test_route_direct_sum():
    d = _FakeDecision(metric="clicks", status="DIRECT_SUM", method="SUM")
    out = msm._route_decision_to_dict(d, emitters=("a",))
    assert out["method"] == "SUM"
    assert out["reason"] is None


def test_f4_empty_series_filtered():
    """F-4: declared members but no emitter -> series_present == [], declared kept."""
    d = _FakeDecision(
        metric="cost",
        status="KEEP_SEPARATE",
        series=(_FakeSeries("verifierA", "cost"), _FakeSeries("verifierB", "cost")),
    )
    out = msm._route_decision_to_dict(d, emitters=set())
    assert out["series_present"] == []
    assert [s["connector"] for s in out["series_declared"]] == ["verifierA", "verifierB"]


def test_f4_partial_series_filtered():
    d = _FakeDecision(
        metric="cost",
        status="KEEP_SEPARATE",
        series=(
            _FakeSeries("a", "cost"),
            _FakeSeries("b", "cost"),
            _FakeSeries("c", "cost"),
        ),
    )
    out = msm._route_decision_to_dict(d, emitters={"a", "c"})
    assert [s["connector"] for s in out["series_present"]] == ["a", "c"]


def test_route_serialisation_deterministic():
    d = _FakeDecision(
        metric="cost",
        status="KEEP_SEPARATE",
        series=(_FakeSeries("a", "cost"), _FakeSeries("b", "cost")),
    )
    assert msm._route_decision_to_dict(d, emitters=("a", "b")) == msm._route_decision_to_dict(
        d, emitters=("a", "b")
    )


def test_metric_route_tool_end_to_end():
    tools = _register()
    d = _FakeDecision(
        metric="cost",
        status="KEEP_SEPARATE",
        series=(_FakeSeries("a", "cost"),),
    )
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_read", return_value=True),
        patch("core.db.get_connection", return_value=_ctx(fetchone=("orgA",))),
        patch("core.metric_reconciliation.resolve_route", return_value=d),
        patch("core.metric_reconciliation.emitters_of", return_value=("a",)),
    ):
        res = tools["metric_route"](org_id="orgA", project_id="projA", metric="cost")
    assert res.structured_content["data"]["status"] == "KEEP_SEPARATE"


# ---------------------------------------------------------------------------
# 23-28: curation delegation + created_by
# ---------------------------------------------------------------------------


def _org_mapping():
    return {
        "id": "smm_1",
        "org_id": "orgA",
        "scope_level": "ORG",
        "metric_definition_id": "metdef_1",
        "connector": "x",
        "source_field_path": "a.b",
        "extraction_note": None,
        "project_id": None,
        "canonical_name": "revenue",
    }


def test_confirm_delegates_with_status_and_identity():
    tools = _register()
    upsert = MagicMock(return_value={**_org_mapping(), "status": "confirmed"})
    with (
        _patch_identity(sub="curator@toorow.io"),
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch(
            "core.metric_semantics_api._get_mapping_by_id",
            return_value=_org_mapping(),
        ),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        tools["metric_mapping_confirm"](org_id="orgA", mapping_id="smm_1")
    kwargs = upsert.call_args.kwargs
    assert kwargs["status"] == "confirmed"
    assert kwargs["created_by"] == "curator@toorow.io"


def test_rename_known_target_status_renamed():
    tools = _register()
    upsert = MagicMock(return_value={**_org_mapping(), "status": "renamed"})
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch(
            "core.metric_semantics_api._get_mapping_by_id",
            return_value=_org_mapping(),
        ),
        patch(
            "core.metric_semantics_api._resolve_definition_id",
            return_value="metdef_target",
        ),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        tools["metric_mapping_rename"](
            org_id="orgA", mapping_id="smm_1", canonical_name="revenue"
        )
    kwargs = upsert.call_args.kwargs
    assert kwargs["status"] == "renamed"
    assert kwargs["metric_definition_id"] == "metdef_target"


def test_rename_unknown_target_invalid_target_no_upsert():
    tools = _register()
    upsert = MagicMock()
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch(
            "core.metric_semantics_api._get_mapping_by_id",
            return_value=_org_mapping(),
        ),
        patch(
            "core.metric_semantics_api._resolve_definition_id", return_value=None
        ),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_mapping_rename"](
                org_id="orgA", mapping_id="smm_1", canonical_name="ghost"
            )
    assert _err_code(ei) == "invalid_target"
    upsert.assert_not_called()


def test_rename_cross_org_not_found_before_resolve_and_no_upsert():
    """F-1 (27.6): renaming a mapping of org B as an admin of org A -> not_found.

    EXACT 27.2 order: guard(manage on orgA) passes, but the fetched mapping belongs to orgB
    -> the IDOR check in _fetch_org_mapping_or_404 raises not_found (existence-hiding) BEFORE
    _resolve_definition_id is reached AND before the store is touched. Mirrors the confirm
    IDOR pattern: both _resolve_definition_id and upsert_source_metric_mapping are
    assert_not_called (no target probing on another org's scope, no write).
    """
    tools = _register()
    upsert = MagicMock()
    resolve = MagicMock()
    other_org_mapping = {**_org_mapping(), "org_id": "orgB"}
    with (
        _patch_identity(),
        # Guard passes for the CALLER's org (orgA) -- the block is the IDOR check, not the guard.
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch(
            "core.metric_semantics_api._get_mapping_by_id",
            return_value=other_org_mapping,
        ),
        patch("core.metric_semantics_api._resolve_definition_id", resolve),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_mapping_rename"](
                org_id="orgA", mapping_id="smm_1", canonical_name="revenue"
            )
    assert _err_code(ei) == "not_found"
    resolve.assert_not_called()  # never probe orgB's scope for a target
    upsert.assert_not_called()   # never write


def test_reject_status_rejected():
    tools = _register()
    upsert = MagicMock(return_value={**_org_mapping(), "status": "rejected"})
    with (
        _patch_identity(),
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch(
            "core.metric_semantics_api._get_mapping_by_id",
            return_value=_org_mapping(),
        ),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        tools["metric_mapping_reject"](org_id="orgA", mapping_id="smm_1")
    assert upsert.call_args.kwargs["status"] == "rejected"


def test_definition_upsert_passes_synonyms_ai_context_and_identity():
    tools = _register()
    up = MagicMock(return_value={"id": "metdef_x", "canonical_name": "revenue"})
    with (
        _patch_identity(sub="owner@toorow.io"),
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch("core.metric_semantics.upsert_metric_definition", up),
    ):
        tools["metric_definition_upsert"](
            org_id="orgA",
            scope_level="ORG",
            canonical_name="revenue",
            aggregation_type="SUM",
            synonyms=[{"lang": "fr", "terms": ["CA"]}],
            ai_context="Chiffre d'affaires",
        )
    kwargs = up.call_args.kwargs
    assert kwargs["synonyms"] == [{"lang": "fr", "terms": ["CA"]}]
    assert kwargs["ai_context"] == "Chiffre d'affaires"
    assert kwargs["created_by"] == "owner@toorow.io"
    assert kwargs["scope_level"] == "ORG"


def test_definition_upsert_invalid_scope_does_not_leak_internal_message():
    """S-1: an InvalidScope from the store surfaces as a CONSTANT FR message; the internal
    exception text is NOT propagated to the caller (only logged)."""
    from core.metric_semantics import InvalidScope

    secret = "org_id 'orgA' inconsistent with project_id 'proj-SECRET-INTERNAL'"

    def _raise(**_kw):
        raise InvalidScope(secret)

    tools = _register()
    with (
        _patch_identity(sub="owner@toorow.io"),
        patch("core.metric_semantics_api._require_org_manage", return_value=True),
        patch("core.db.get_connection", return_value=_ctx()),
        patch("core.metric_semantics.upsert_metric_definition", _raise),
    ):
        with pytest.raises(ToolError) as ei:
            tools["metric_definition_upsert"](
                org_id="orgA",
                scope_level="ORG",
                canonical_name="revenue",
                aggregation_type="SUM",
            )
    assert _err_code(ei) == "invalid_scope"
    payload = ei.value.args[0]
    assert "Scope invalide." in payload
    assert secret not in payload
    assert "proj-SECRET-INTERNAL" not in payload


# ---------------------------------------------------------------------------
# 29-34: verified queries
# ---------------------------------------------------------------------------

_CORPUS_FIXTURE = """
schema_version: '1'
questions:
- id: q1
  question: How many sessions?
  surface: daily_report
  difficulty: easy
  tags: [ga4, sessions]
  reference_queries:
  - reference_sql: SELECT 1
    role: canonical_correct
    note: ok
  tool_invocation:
    tool: get_daily_report
    args: {project_id: default}
- id: q2
  question: A wrong-only entry
  surface: expert_report
  difficulty: medium
  tags: [meta-ads]
  reference_queries:
  - reference_sql: SELECT 999
    role: incorrect
    note: wrong
"""


def _write_corpus(tmp_path, text=_CORPUS_FIXTURE):
    p = tmp_path / "corpus.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_verified_queries_canonical_correct_only(tmp_path):
    p = _write_corpus(tmp_path)
    pairs = msm._load_verified_queries(corpus_path=p)
    by_id = {x["id"]: x for x in pairs}
    assert by_id["q1"]["reference_sql"] == "SELECT 1"
    # q2 has only an 'incorrect' role -> no reference_sql (never few-shot a wrong query).
    assert by_id["q2"]["reference_sql"] is None


def test_load_verified_queries_missing_corpus_returns_empty(tmp_path):
    assert msm._load_verified_queries(corpus_path=tmp_path / "nope.yaml") == []


def test_verified_queries_tool_corpus_absent_note(tmp_path):
    tools = _register()
    with patch(
        "core.metric_semantics_mcp._load_verified_queries", return_value=[]
    ):
        res = tools["metric_verified_queries"]()
    data = res.structured_content["data"]
    assert data["verified_queries"] == []
    assert "note" in data


def test_verified_queries_filter_surface_and_tag(tmp_path):
    p = _write_corpus(tmp_path)
    pairs = msm._load_verified_queries(corpus_path=p)
    daily = msm._filter_verified_queries(pairs, surface="daily_report", tag=None, limit=20)
    assert [x["id"] for x in daily] == ["q1"]
    tagged = msm._filter_verified_queries(pairs, surface=None, tag="meta-ads", limit=20)
    assert [x["id"] for x in tagged] == ["q2"]


def test_verified_queries_limit(tmp_path):
    pairs = [{"id": str(i), "surface": None, "tags": []} for i in range(50)]
    assert len(msm._filter_verified_queries(pairs, surface=None, tag=None, limit=5)) == 5


def test_verified_queries_read_only_no_write_open():
    """LECTURE SEULE: the module never opens a file for writing."""
    import pathlib

    src = pathlib.Path(msm.__file__).read_text(encoding="utf-8")
    assert "'w'" not in src and '"w"' not in src


# ---------------------------------------------------------------------------
# Helpers: a fake DB connection context manager
# ---------------------------------------------------------------------------


def _ctx(fetchone=None, fetchall=None):
    """A get_connection()-compatible context manager with a fake cursor."""

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            return None

        def fetchone(self):
            return fetchone

        def fetchall(self):
            return fetchall or []

        @property
        def description(self):
            return []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    return _Conn()
