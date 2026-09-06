"""Tests for the metric-semantics MCP surface (Story 27.6, Epic 27).

Offline, no DB: MagicMock/patch at the lazy-import source (core.metric_semantics.*,
core.metric_reconciliation.*, core.project_access.*, core.db.get_connection). A FakeMCP
captures the mcp.tool(fn) registrations of register(mcp) so the handlers can be invoked
directly. The MCP identity is simulated by patching get_access_token.

Coverage: registration/shape, guards (org read/manage, IDOR F-1, project leak F-3, PLATFORM
forbidden, anonymous), reference contract + synonyms/ai_context, route serialisation for the
6 statuses + F-4 empty-series filtering + determinism, curation delegation + created_by
propagation, and verified queries.

The verified-query tests changed shape on 2026-07-31. They used to pin a behaviour that
should never have shipped: the tool read `server/tests/evals/corpus.yaml` and served its
50 question<->SQL pairs to the model. They now pin the opposite -- that the eval corpus is
never read, that the Golden Question is not used as grounding either, and that the tool
STATES its absence with a reason code and an owner instead of returning a bare empty list.
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
    """Capture mcp.tool(fn) registrations so register(mcp) exposes its handlers.

    `**_declaration` because AD-43 made every registration carry its capability
    profile: `register_profiled` forwards `name`/`tags`/`meta` to `mcp.tool`. A
    one-argument recorder raises a TypeError inside the registrar instead of
    telling this file anything about metric semantics.
    """

    def __init__(self):
        self.tools = {}

    def tool(self, fn, **_declaration):
        self.tools[fn.__name__] = fn
        return fn


def _register():
    """Register into a throwaway app WITHOUT leaving declarations behind.

    The profiled registry is process-global, and every call here would otherwise
    add seven declarations to the catalog every other suite in this session reads.
    """
    from core import mcp_profiles

    mcp = FakeMCP()
    before = dict(mcp_profiles._REGISTRY.declarations)
    try:
        msm.register(mcp)
    finally:
        mcp_profiles._REGISTRY.declarations.clear()
        mcp_profiles._REGISTRY.declarations.update(before)
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
    ):
        assert name in tools


def test_the_definition_writer_is_not_offered_any_more():
    """Story 49.3 AC1, cutover of 2026-08-25 -- and this is the whole test of it.

    `metric_definition_upsert` was the MCP door onto `app.metric_definitions`, the
    second store that declares how a metric aggregates. `governance.md` settled the
    precedence (the Semantic Model wins) and left two ways to finish it: "either a
    projection or the retirement of the lower layer". This is the retirement, and
    the REST doors went in the same commit.

    IT IS REMOVED, NOT REFUSING, and the asymmetry with REST is deliberate. A REST
    caller holds a URL, so those doors answer 409 `legacy_store_is_read_only` with
    a sentence naming the Semantic Model -- unmounting them would answer 404 and
    send the caller looking for its object. An MCP catalogue is DISCOVERED: a tool
    that is not offered misleads nobody. And a tool kept alive only to refuse would
    still declare `org_id` / `project_id` while resolving no access, which
    `tests/conformance/test_mcp_tools_resolve_project_scope.py` reads -- correctly
    -- as an unguarded scoped tool.
    """
    tools = _register()

    assert "metric_definition_upsert" not in tools
    # Not vacuous: the three curation writes that STAYED are still offered, so an
    # empty registration would not pass this test by accident.
    for still_there in (
        "metric_mapping_confirm",
        "metric_mapping_rename",
        "metric_mapping_reject",
    ):
        assert still_there in tools


def test_the_module_no_longer_reaches_the_definition_store():
    """The permanent attack: bringing the tool back needs the import, and goes red here."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(msm))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "core.metric_semantics"
        for alias in node.names
    }

    # Not vacuous: the mapping curation path still imports from the same module.
    assert imported, "no core.metric_semantics import found -- the guard reads nothing"
    assert "upsert_metric_definition" not in imported, sorted(imported)
    assert "delete_metric_definition" not in imported, sorted(imported)


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
        # AI-295 : les deux patchs du cascade retire sont partis avec lui. La
        # fonction `_load_reconciliation_rows` a ete SUPPRIMEE, donc la patcher
        # levait AttributeError -- ce test etait rouge pour la meilleure raison
        # qui soit, ce qu il stubbait n existe plus. Le contrat teste ici est
        # celui des CLES du reference, pas la reconciliation : rien a remplacer.
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
    # AI-295: a GOVERNED reference entry -- `resolved_scope` is always PROJECT,
    # because a published Rule Set belongs to the Project that published it.
    rec_rule = {
        "method": "PRIORITY",
        "priority_order": ["src-a", "src-b"],
        "join_key": None,
        "truth_connector": None,
        "resolved_scope": "PROJECT",
        "rule_set_version_id": "grsv_EXAMPLE",
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
                "core.metric_semantics.reference_reconciliation", return_value=rec_rule
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
    assert "must not be added up" in summary


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
    assert "never a combined total" in summary


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


# ---------------------------------------------------------------------------
# 29-34: verified queries
# ---------------------------------------------------------------------------

def _executable_code(module) -> str:
    """The module's EXECUTABLE code: no comments, no docstrings.

    The two guards below search for forbidden reads, and searching the raw file is the
    wrong instrument twice over: it matches the header comment that EXPLAINS why the read
    is forbidden, and it matches the docstrings that repeat the explanation. Documenting
    the rule would then break the test that enforces it -- which is exactly what happened
    here, twice, before this helper existed.

    Round-tripping through the AST drops comments; stripping the leading string Expr of
    every module, class and function drops docstrings. What is left is what actually runs.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_module_no_longer_reads_the_eval_corpus():
    """The regression guard. Two independent reasons make this permanent.

    `server/tests/evals/corpus.yaml` declares itself TEST CODE on its first line
    (AD-17), and it is the answer key `server/tests/evals/test_eval_gate.py` grades
    the system against. Serving it to the model at runtime -- which Story 27.6 did
    from 2026-07-21 -- meant the adherence gate reported how well the model read what
    it had just been handed.
    """

    body = _executable_code(msm)
    for banned in ("corpus.yaml", "CORPUS_PATH", "evals"):
        assert banned not in body, (
            f"the eval corpus is being read again through `{banned}`: it is test code, "
            "and it is the answer key the adherence gate grades against"
        )


def test_the_store_exists_now_and_the_three_outcomes_stay_apart():
    """Story 52.2 delivered the store, so the absence this tool used to state is gone.

    What replaces it is NOT one empty list. Three facts used to hide behind that
    list, and a reader that cannot tell them apart cannot act on any of them:

      * pairs exist                    -> no reason code at all
      * the project bound none         -> a fact about the PROJECT
      * the store could not be read    -> a fact about the RUN

    The third is the one that must never be reported as the second: "there are
    none" is a claim about the project, and a failed read is not entitled to it.
    """
    from core import answerable_topics as topics

    # The project bound nothing: an empty list WITH its reason.
    empty = ([], topics.NO_BINDING_REASON)
    with patch.object(topics, "verified_query_pairs", return_value=empty), patch(
        "core.db.get_connection", lambda *a, **k: _ctx()
    ):
        pairs, reason = msm._governed_verified_queries("proj_EXAMPLE", "owner@example.com")
    assert pairs == []
    assert reason == topics.NO_BINDING_REASON

    # The store is unreadable: a DIFFERENT reason, never the one above.
    with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
        pairs, reason = msm._governed_verified_queries("proj_EXAMPLE", "owner@example.com")
    assert pairs == []
    assert reason == topics.UNAVAILABLE_REASON

    # Bindings exist: pairs, and no reason code to explain an emptiness there is not.
    bound = [{"id": "atq_1", "question": "How is spend pacing?", "surface": "pacing",
              "tags": ["headline"]}]
    with patch.object(topics, "verified_query_pairs", return_value=(bound, None)), patch(
        "core.db.get_connection", lambda *a, **k: _ctx()
    ):
        pairs, reason = msm._governed_verified_queries("proj_EXAMPLE", "owner@example.com")
    assert [p["id"] for p in pairs] == ["atq_1"]
    assert reason is None


def test_the_tool_carries_the_reason_and_refuses_an_unguarded_read():
    """The emptiness is never bare, and the guard runs BEFORE the read."""
    from core import answerable_topics as topics

    tools = _register()

    empty = ([], topics.NO_BINDING_REASON)
    with patch("core.project_access.identity_can_read_project", return_value=True), patch(
        "core.db.get_connection", lambda *a, **k: _ctx()
    ), patch.object(topics, "verified_query_pairs", return_value=empty):
        res = tools["metric_verified_queries"]("proj_EXAMPLE")
    data = res.structured_content["data"]
    assert data["verified_queries"] == []
    assert data["reason_code"] == topics.NO_BINDING_REASON
    assert "answer key" in data["note"], (
        "the note must say WHY the corpus is not used, not merely that nothing is there"
    )

    # Denied identity -> not_found, and the store is never read.
    with patch("core.project_access.identity_can_read_project", return_value=False), patch(
        "core.db.get_connection", lambda *a, **k: _ctx()
    ), patch.object(topics, "verified_query_pairs") as never:
        with pytest.raises(Exception):
            tools["metric_verified_queries"]("proj_SOMEONE_ELSE")
    never.assert_not_called()


def test_the_views_a_topic_declares_reach_the_model_and_only_then():
    """Story 75-5: `views` is ONE sibling key, and only for a topic that has some.

    THE HALF THAT MATTERS IS THE SECOND ONE. Adding a key is easy to prove; not
    adding it is the acceptance criterion, because every consumer of this tool
    that predates story 75-5 reads a payload it never asked to change. So the
    unbound pair below is asserted key by key, not merely for the absence of
    `views`.
    """
    from core import answerable_topics as topics

    tools = _register()

    unbound = [{"id": "atq_1", "question": "How is spend pacing?", "surface": "pacing",
                "tags": ["headline"], "topic_key": "pacing", "role": "headline"}]
    with patch("core.project_access.identity_can_read_project", return_value=True), patch(
        "core.db.get_connection", lambda *a, **k: _ctx()
    ), patch.object(topics, "verified_query_pairs", return_value=(unbound, None)):
        res = tools["metric_verified_queries"]("proj_EXAMPLE")
    served = res.structured_content["data"]["verified_queries"][0]
    assert set(served) == {"id", "question", "surface", "tags", "topic_key", "role"}, (
        "a topic that declares no Semantic View must reach the model with the exact "
        "payload it had before story 75-5 -- not `views: []`"
    )

    bound = [dict(unbound[0], views=[{
        "view_id": "sv_EXAMPLE",
        "view_version_id": "svv_EXAMPLE",
        "view_version_number": 3,
        "view_name": "spend",
        "status": "published",
        "stale": False,
        "paths": [{"relation_ids": ["campaign_to_account"], "from": "campaigns",
                   "to": "accounts", "cardinality": "many_to_one",
                   "fan_out_policy": "forbid", "resolved": True,
                   "relations": []}],
    }])]
    with patch("core.project_access.identity_can_read_project", return_value=True), patch(
        "core.db.get_connection", lambda *a, **k: _ctx()
    ), patch.object(topics, "verified_query_pairs", return_value=(bound, None)):
        res = tools["metric_verified_queries"]("proj_EXAMPLE")
    served = res.structured_content["data"]["verified_queries"][0]
    assert served["views"][0]["view_version_id"] == "svv_EXAMPLE", (
        "the pin the model is told must be the EXACT version, never the head"
    )
    assert served["views"][0]["paths"][0]["fan_out_policy"] == "forbid", (
        "a path reaches the model WITH its fan-out policy: a join whose policy the "
        "model cannot see is a join it will assume is safe"
    )


def test_the_golden_question_is_not_used_as_grounding_either():
    """Epic 51's Golden Question is the EVALUATION specification.

    Grounding the runtime with it would reintroduce the same contamination one layer
    up: a system cannot be measured against the examples it was handed.
    """
    body = _executable_code(msm)
    for banned in ("golden_question", "eval_benchmark_questions"):
        assert banned not in body


# ---------------------------------------------------------------------------
# The same guard, on the modules the anchoring MOVED to (Epic 52).
#
# The two guards above sweep `metric_semantics_mcp` and nothing else, because
# when they were written that module WAS the grounding path. Epic 52 moved it:
# a topic now resolves its catalog, its verified-query bindings and its
# knowledge pins in `core.answerable_topics`, serves them over
# `core.answerable_topics_api`, and projects the result through
# `core.answer_contract`. A `from tests.evals import corpus` in any of those
# three would reintroduce exactly the contamination the guards above forbid,
# and neither of them would see it. The module's own local guard
# (`test_answerable_topics.py:650`) bans function names and imported module
# names containing `evaluation` or `golden_question` -- not `corpus`, not
# `evals`.
#
# Same instrument, same banned words, wider sweep. The two guards above are
# untouched: this is an addition, never a replacement.
# ---------------------------------------------------------------------------

#: Where an answer is anchored TODAY. A module that leaves this tuple leaves the
#: guard, so the tuple is asserted non-empty and every name must import.
_ANCHORING_MODULES = (
    "core.answerable_topics",
    "core.answerable_topics_api",
    "core.answer_contract",
)

#: The union of the two banned lists above. One list, so a word added for one
#: module is added for all of them.
_BANNED_IN_ANCHORING_CODE = (
    "corpus.yaml",
    "CORPUS_PATH",
    "evals",
    "golden_question",
    "eval_benchmark_questions",
)


@pytest.mark.parametrize("module_name", _ANCHORING_MODULES)
def test_the_anchoring_path_does_not_read_the_corpus_either(module_name):
    """The answer path of Epic 52 is held to the guard written for Story 27.6.

    `server/tests/evals/corpus.yaml` is test code (AD-17) and the answer key
    `server/tests/evals/test_eval_gate.py` grades against; Epic 51's Golden
    Question is the evaluation specification. Neither may ground a runtime
    answer, and it does not matter which module does the grounding.
    """
    import importlib

    module = importlib.import_module(module_name)
    body = _executable_code(module)
    for banned in _BANNED_IN_ANCHORING_CODE:
        assert banned not in body, (
            f"{module_name} reads the evaluation material through `{banned}`. "
            "It is test code and the answer key the adherence gate grades against: "
            "a system cannot be measured against the examples it was handed."
        )


def test_the_anchoring_guard_covers_every_module_of_the_answer_path():
    """The guard narrows silently if a module is renamed out of the tuple.

    A sweep is only worth its coverage. This asserts the three names still
    resolve -- a rename that leaves one behind fails here rather than passing
    quietly with two thirds of the path unswept.
    """
    import importlib

    assert len(_ANCHORING_MODULES) >= 3
    for name in _ANCHORING_MODULES:
        assert importlib.import_module(name) is not None


def test_verified_queries_filter_surface_and_tag():
    """The filter survives the source change: it is pure, and the governed store will
    hand it the same shape."""
    pairs = [
        {"id": "q1", "surface": "daily_report", "tags": ["ga4", "sessions"]},
        {"id": "q2", "surface": "report", "tags": ["meta-ads"]},
    ]
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
    """A connection double the ACQUISITION SEAM can arm, not just read from.

    The surfaces of this module acquire through `core.db.request_connection`
    since 2026-08-21, and that seam installs the access context on the
    connection it hands back: `SET ROLE`, two `set_config` calls, then a
    `commit()`. A double with a cursor but no `commit` made every tool here
    raise `AttributeError` inside `core/db.py` -- the double was shaped for an
    acquisition that no longer exists. `commit`/`rollback` are answered here so
    the double keeps standing for a real connection.
    """

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

        def commit(self):
            return None

        def rollback(self):
            return None

    return _Conn()
