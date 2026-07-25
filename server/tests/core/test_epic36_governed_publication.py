"""Story 36.18 governed-publication tests (review + confirm/publish + rollback).

Fully offline (no live Postgres): the pure helpers are exercised directly, the DB is
a MagicMock cursor, and the capability middleware is driven with lightweight stand-in
tool/context objects. Mirrors test_epic36_operations_mcp.py / test_epic36_mapping_*.

Coverage:
  (a) the review object contains profile/scope/versions/diff/impact/checks/rollback/
      expiry AND the confirmation secret is NOT present in any model-facing output.
  (b) a valid confirmation creates EXACTLY ONE durable operation bound to reviewed
      hashes and advances current_mapping_version_id atomically.
  (c) expired / used / replayed / actor-mismatch / workspace-mismatch / stale /
      changed-pointer / failed-check (proposal-not-ready) each block creation+dispatch.
  (d) the prior version stays rollbackable and rollback is a DISTINCT idempotent
      operation with its own audit/outbox.
  (e) failure / outcome_unknown reconciles with original refs, no blind fresh mutation.
  (f) governance tools register and pass validate_catalog; hidden from discovery
      without a governance opt-in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # High-risk profiles are fail-closed OFF by default (review C1); this suite
    # exercises the Governance profile, so it opts in like a deployment that has
    # wired server-side host/workspace verification.
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


class _Recorder:
    def __init__(self):
        self.handlers = {}
        self.tool = MagicMock()


def _register_all(recorder):
    from core import governance_mcp, mcp_profiles

    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        recorder.handlers[handler.__name__] = handler
        return real(mcp, handler, **kwargs)

    mcp_profiles.register_profiled = spy  # type: ignore[assignment]
    try:
        governance_mcp.register(recorder)
    finally:
        mcp_profiles.register_profiled = real  # type: ignore[assignment]
    return recorder.handlers


class _ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


def _fake_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# (f) registration + validate_catalog + discovery gating.
# ---------------------------------------------------------------------------


def test_governance_tools_register_and_validate_catalog_accepts_them():
    from core import mcp_profiles

    _register_all(_Recorder())
    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert set(decls) == {"review_agent_change", "confirm_agent_change", "rollback_agent_change"}
    assert all(d.profile == "governance" for d in decls.values())
    for name in ("confirm_agent_change", "rollback_agent_change"):
        assert decls[name].effect == "write"
        assert decls[name].confirmation_mode == "human"
    assert decls["review_agent_change"].effect == "read"
    assert decls["review_agent_change"].confirmation_mode == "none"
    validated = {d.name for d in mcp_profiles.validate_catalog()}
    assert validated == set(decls)


def _tool(name, profile):
    return SimpleNamespace(name=name, meta={"profile": profile}, tags={f"profile:{profile}"})


def test_governance_tools_hidden_without_opt_in(monkeypatch):
    from core import mcp_profiles

    _register_all(_Recorder())
    mw = mcp_profiles.build_middleware()
    tools = [
        _tool("report_read", "insights"),
        _tool("review_agent_change", "governance"),
        _tool("confirm_agent_change", "governance"),
    ]

    async def call_next(_context):
        return tools

    monkeypatch.setattr(
        mcp_profiles, "_capability_context",
        lambda: ("user-1", {}, {"enabled_profiles": ["insights"]}),
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == ["report_read"]


def test_governance_tools_visible_with_governance_opt_in(monkeypatch):
    from core import mcp_profiles

    _register_all(_Recorder())
    mw = mcp_profiles.build_middleware()
    tools = [
        _tool("report_read", "insights"),
        _tool("review_agent_change", "governance"),
        _tool("confirm_agent_change", "governance"),
    ]

    async def call_next(_context):
        return tools

    grants = {
        "enabled_profiles": ["insights", "governance"],
        "endpoint_binding": "admin-endpoint",
        "workspace_evidence_hash": "a" * 64,
    }
    monkeypatch.setattr(
        mcp_profiles, "_capability_context", lambda: ("user-1", {"host": "opaque"}, grants)
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == [
        "report_read",
        "review_agent_change",
        "confirm_agent_change",
    ]


def test_direct_call_to_confirm_denied_without_opt_in(monkeypatch):
    from core import mcp_profiles

    _register_all(_Recorder())
    mw = mcp_profiles.build_middleware()
    call_next = MagicMock()
    monkeypatch.setattr(
        mcp_profiles, "_capability_context",
        lambda: ("user-1", {}, {"enabled_profiles": ["insights"]}),
    )
    context = SimpleNamespace(message=SimpleNamespace(name="confirm_agent_change"))
    with pytest.raises(Exception) as exc:
        asyncio.run(mw.on_call_tool(context, call_next))
    assert "introuvable" in str(exc.value)
    call_next.assert_not_called()


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def test_reviewed_hashes_match_detects_drift_and_changed_pointer():
    from core import governed_publication as gp

    reviewed = {"content_hash": "c1", "source_schema_hash": "s1", "prior_mapping_version_id": "v0"}
    assert gp.reviewed_hashes_match(reviewed, dict(reviewed))
    # candidate hash drift
    assert not gp.reviewed_hashes_match(reviewed, {**reviewed, "content_hash": "c2"})
    # changed pointer (someone else published)
    assert not gp.reviewed_hashes_match(reviewed, {**reviewed, "prior_mapping_version_id": "v9"})


def test_forward_and_rollback_idempotency_keys_are_distinct():
    from core import governed_publication as gp

    pub = gp._publication_idempotency_key("org", "ds", "vC", "conf")
    rb = gp._rollback_idempotency_key("org", "ds", "v0", "conf")
    assert pub != rb
    assert pub.startswith(gp.COMMAND_PUBLISH)
    assert rb.startswith(gp.COMMAND_ROLLBACK)


# ---------------------------------------------------------------------------
# (a) review object shape + the secret is NEVER in model-facing output.
# ---------------------------------------------------------------------------


def _proposal_row(state="ready"):
    return {
        "id": "mprop_1", "mapping_version_id": "dmap_new", "datastream_id": "ds-1",
        "project_id": "proj-1", "org_id": "org-1", "mode": "delegated", "state": state,
        "checks": [{"check": "compile", "passed": True, "code": None}],
        "diff": {"summary": "Diff de mapping : 1 champ(s) ajoute(s)."},
        "content_hash": "c1", "source_schema_hash": "s1",
        "policy_version": "pol-1", "catalog_version": "cat-1", "tool_version": "t-1",
    }


def test_prepare_review_shape_and_secret_is_model_hidden(monkeypatch):
    from core import governed_publication as gp

    monkeypatch.setattr(gp, "_load_ready_proposal", lambda conn, pid: _proposal_row())
    monkeypatch.setattr(gp, "_load_live_pointer", lambda conn, ds, pj: "dmap_old")
    monkeypatch.setattr(
        gp, "_fetch_version_row",
        lambda cur, vid, ds, pj: {"id": vid, "version_number": 2, "content_hash": "c1",
                                  "source_schema_hash": "s1", "mapping_payload": {"fields": []}},
    )
    monkeypatch.setattr(gp, "_current_policy_version", lambda: "pol-1")
    inserted = {}
    monkeypatch.setattr(
        gp, "_insert_confirmation",
        lambda conn, **kw: inserted.update(kw),
    )
    conn, _cur = _fake_conn()

    review = gp.prepare_publication_review(
        conn, proposal_id="mprop_1", actor="user-1", org_id="org-1"
    )

    obj = review.review
    for key in ("profile", "scope", "versions", "diff", "interval", "destination",
                "impact", "checks", "rollback", "expires_in_seconds"):
        assert key in obj, key
    assert obj["profile"] == "governance"
    assert obj["impact"]["advances_pointer"] is True
    assert obj["rollback"]["target_mapping_version_id"] == "dmap_old"
    assert obj["versions"]["candidate_mapping_version_id"] == "dmap_new"
    # The secret is minted and returned OUT-OF-BAND, but NEVER inside the review obj.
    assert review.confirmation_secret and review.confirmation_secret.startswith("pubs_")
    serialized = repr(obj)
    assert review.confirmation_secret not in serialized
    assert "confirmation_secret" not in obj
    # Persisted only as a one-way hash.
    assert inserted["confirmation_reference_hash"] == gp._sha256(review.confirmation_secret)


def test_review_tool_does_not_return_secret_to_model(monkeypatch):
    from core import governance_mcp
    from core import governed_publication as gp
    from core.project_access import AccessDecision

    handlers = _register_all(_Recorder())
    monkeypatch.setattr(governance_mcp, "_identity", lambda: "user-1")
    monkeypatch.setattr(governance_mcp, "_host_context", lambda: {})
    monkeypatch.setattr(governance_mcp, "_proposal_datastream", lambda conn, pid: ("ds-1", "org-1"))
    monkeypatch.setattr(
        governance_mcp, "_guard_datastream",
        lambda ds, ident, *, minimum_capability: AccessDecision(
            True, "owner_floor", "manage", "org-1", ("organization:org-1", f"flux:{ds}")
        ),
    )
    secret = "pubs_topsecret"
    monkeypatch.setattr(
        gp, "prepare_publication_review",
        lambda conn, **kw: gp.PublicationReview(
            confirmation_id="pubc_1",
            review={"profile": "governance", "scope": {}, "versions": {}, "diff": {},
                    "interval": None, "destination": {}, "impact": {}, "checks": [],
                    "rollback": {}, "expires_in_seconds": 900, "confirmation_id": "pubc_1"},
            confirmation_secret=secret,
        ),
    )
    conn, _cur = _fake_conn()
    import core.db as db
    monkeypatch.setattr(db, "get_connection", lambda: _ctx(conn))

    result = handlers["review_agent_change"]("mprop_1")
    text = result.content[0].text
    data = result.structured_content["data"]
    assert secret not in text
    assert secret not in repr(data)
    assert data.get("confirmation_secret_present") is False
    assert data["confirmation_id"] == "pubc_1"


# ---------------------------------------------------------------------------
# (b) valid confirmation -> EXACTLY ONE execute_operation advancing the pointer.
# ---------------------------------------------------------------------------


def _confirmation_row(**over):
    from core import governed_publication as gp

    base = {
        "id": "pubc_1", "proposal_id": "mprop_1", "mapping_version_id": "dmap_new",
        "datastream_id": "ds-1", "project_id": "proj-1", "org_id": "org-1",
        "actor": "user-1", "confirmation_reference_hash": None,
        "reviewed_hashes": {"content_hash": "c1", "source_schema_hash": "s1",
                            "prior_mapping_version_id": "dmap_old"},
        "prior_mapping_version_id": "dmap_old", "policy_version": "pol-1",
        "idempotency_key_hash": None, "state": "prepared", "operation_id": None,
        "expired": False,
    }
    base.update(over)
    # Pin the idempotency hash so the tamper guard passes by default.
    if base["idempotency_key_hash"] is None:
        key = gp._publication_idempotency_key(
            base["org_id"], base["datastream_id"], base["mapping_version_id"], base["id"]
        )
        base["idempotency_key_hash"] = gp._sha256(key)
    return base


def _confirm_env(monkeypatch, confirmation, exec_return, *, secret="pubs_ok"):
    from core import governed_publication as gp
    from core import operations

    monkeypatch.setattr(gp, "_load_confirmation", lambda conn, cid: confirmation)
    monkeypatch.setattr(gp, "_load_ready_proposal", lambda conn, pid: _proposal_row())
    monkeypatch.setattr(gp, "_load_live_pointer", lambda conn, ds, pj: "dmap_old")
    monkeypatch.setattr(gp, "_current_policy_version", lambda: "pol-1")
    # Bind the secret hash on the confirmation.
    confirmation["confirmation_reference_hash"] = gp._sha256(secret)

    marked = {}
    monkeypatch.setattr(
        gp, "_mark_confirmation_state",
        lambda conn, cid, state, op_id: marked.update({"state": state, "op_id": op_id}),
    )

    captured = {"calls": 0}

    def fake_execute(conn, spec, *, mutation):
        captured["calls"] += 1
        captured["spec"] = spec
        return exec_return

    monkeypatch.setattr(operations, "execute_operation", fake_execute)
    conn, _cur = _fake_conn()
    return conn, captured, marked, secret


def test_confirm_creates_exactly_one_operation_bound_to_reviewed_hashes(monkeypatch):
    from core import governed_publication as gp
    from core import operations

    op = operations.OperationResult(
        "op-1", "succeeded",
        {"current_mapping_version_id": "dmap_new", "prior_mapping_version_id": "dmap_old"},
        "audit-1", "opout-1", False,
    )
    conf = _confirmation_row()
    conn, captured, marked, secret = _confirm_env(monkeypatch, conf, op)

    result = gp.confirm_and_publish(
        conn, confirmation_id="pubc_1", confirmation_secret=secret, actor="user-1", org_id="org-1"
    )
    assert captured["calls"] == 1
    spec = captured["spec"]
    assert spec.command_type == gp.COMMAND_PUBLISH
    assert spec.confirmation_mode == "human"
    # Bound to the reviewed hashes/versions/policy.
    assert spec.request_payload["content_hash"] == "c1"
    assert spec.request_payload["source_schema_hash"] == "s1"
    assert spec.request_payload["mapping_version_id"] == "dmap_new"
    assert spec.versions["policy"] == "pol-1"
    assert result.outcome == "succeeded"
    assert result.current_mapping_version_id == "dmap_new"
    assert result.prior_mapping_version_id == "dmap_old"
    assert marked["state"] == "dispatched"


def test_publish_mutation_advances_pointer_atomically_and_logs(monkeypatch):
    from core import governed_publication as gp

    conn, cur = _fake_conn()
    # FOR UPDATE read returns the expected live pointer; UPDATE affects 1 row.
    cur.fetchone.return_value = ("dmap_old",)
    cur.rowcount = 1
    conf = _confirmation_row()

    res = gp._publish_mutation(
        conn, "op-1", confirmation=conf, command=gp.COMMAND_PUBLISH,
        to_version_id="dmap_new", from_version_id="dmap_old",
    )
    assert res.outcome == "succeeded"
    assert res.result["current_mapping_version_id"] == "dmap_new"
    sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list if c.args)
    assert "UPDATE app.datastreams" in sql
    assert "current_mapping_version_id" in sql
    assert "datastream_mapping_publication_log" in sql  # rollback evidence written


# ---------------------------------------------------------------------------
# (c) each precondition breach blocks creation + dispatch (no execute_operation).
# ---------------------------------------------------------------------------


def _assert_refused(
    monkeypatch, confirmation, code, *, secret="pubs_ok", presented_secret=None,
    actor="user-1", host_context=None, proposal_state="ready", live_pointer="dmap_old",
):
    from core import governed_publication as gp
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    conn, captured, _marked, real_secret = _confirm_env(
        monkeypatch, confirmation, op, secret=secret
    )
    monkeypatch.setattr(
        gp, "_load_ready_proposal", lambda conn, pid: _proposal_row(state=proposal_state)
    )
    monkeypatch.setattr(gp, "_load_live_pointer", lambda conn, ds, pj: live_pointer)

    with pytest.raises(gp.PublicationConfirmationRefused) as exc:
        gp.confirm_and_publish(
            conn, confirmation_id=confirmation["id"],
            confirmation_secret=(presented_secret if presented_secret is not None else real_secret),
            actor=actor, org_id="org-1", host_context=host_context,
        )
    assert exc.value.code == code
    assert captured["calls"] == 0  # NO operation created / dispatched.


def test_invalid_secret_refuses(monkeypatch):
    _assert_refused(monkeypatch, _confirmation_row(), "confirmation_invalid",
                    presented_secret="pubs_WRONG")


def test_expired_refuses(monkeypatch):
    _assert_refused(monkeypatch, _confirmation_row(expired=True), "expired")


def test_actor_mismatch_refuses(monkeypatch):
    _assert_refused(monkeypatch, _confirmation_row(actor="someone-else"), "actor_mismatch")


def test_workspace_mismatch_refuses(monkeypatch):
    conf = _confirmation_row()
    conf["reviewed_hashes"] = {**conf["reviewed_hashes"], "workspace_id": "ws-a"}
    _assert_refused(monkeypatch, conf, "workspace_mismatch", host_context={"workspace_id": "ws-b"})


def test_used_state_without_operation_refuses(monkeypatch):
    _assert_refused(monkeypatch, _confirmation_row(state="failed"), "used")


def test_policy_changed_refuses(monkeypatch):
    _assert_refused(monkeypatch, _confirmation_row(policy_version="OLD"), "policy_changed")


def test_proposal_not_ready_refuses(monkeypatch):
    _assert_refused(
        monkeypatch, _confirmation_row(), "proposal_not_ready", proposal_state="rejected"
    )


def test_changed_pointer_refuses(monkeypatch):
    # Live pointer moved since review -> changed_pointer.
    _assert_refused(
        monkeypatch, _confirmation_row(), "changed_pointer", live_pointer="dmap_someone_else"
    )


def test_stale_versions_refuses(monkeypatch):
    from core import governed_publication as gp
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    conf = _confirmation_row()
    conn, captured, _m, secret = _confirm_env(monkeypatch, conf, op)
    # Proposal content hash drifted from the reviewed hash, pointer unchanged.
    monkeypatch.setattr(
        gp, "_load_ready_proposal",
        lambda conn, pid: {**_proposal_row(), "content_hash": "c_DRIFTED"},
    )
    monkeypatch.setattr(gp, "_load_live_pointer", lambda conn, ds, pj: "dmap_old")
    with pytest.raises(gp.PublicationConfirmationRefused) as exc:
        gp.confirm_and_publish(conn, confirmation_id="pubc_1", confirmation_secret=secret,
                               actor="user-1", org_id="org-1")
    assert exc.value.code == "stale_versions"
    assert captured["calls"] == 0


def test_duplicate_confirm_replays_original_operation_no_duplicate(monkeypatch):
    from core import governed_publication as gp
    from core import operations

    op = operations.OperationResult("op-1", "succeeded", {}, "a", "b", False)
    # Already-consumed confirmation carrying its operation_id -> replay path.
    conf = _confirmation_row(state="dispatched", operation_id="op-1")
    conn, captured, _m, secret = _confirm_env(monkeypatch, conf, op)
    conn.cursor().__enter__().fetchone.return_value = (
        "succeeded",
        {"current_mapping_version_id": "dmap_new", "prior_mapping_version_id": "dmap_old"},
    )
    result = gp.confirm_and_publish(conn, confirmation_id="pubc_1", confirmation_secret=secret,
                                    actor="user-1", org_id="org-1")
    assert captured["calls"] == 0  # never re-run the mutation
    assert result.replayed is True
    assert result.operation_id == "op-1"


# ---------------------------------------------------------------------------
# (d) prior version rollbackable + rollback is a DISTINCT idempotent operation.
# ---------------------------------------------------------------------------


def test_rollback_is_a_distinct_operation_with_own_idempotency(monkeypatch):
    from core import governed_publication as gp
    from core import operations

    op = operations.OperationResult(
        "op-rb", "succeeded",
        {"current_mapping_version_id": "dmap_old", "prior_mapping_version_id": "dmap_new"},
        "audit-2", "opout-2", False,
    )
    conf = _confirmation_row()
    monkeypatch.setattr(gp, "_load_confirmation", lambda conn, cid: conf)
    monkeypatch.setattr(gp, "_current_policy_version", lambda: "pol-1")
    monkeypatch.setattr(gp, "_mark_confirmation_state", lambda *a, **k: None)

    captured = {"specs": []}

    def fake_execute(conn, spec, *, mutation):
        captured["specs"].append(spec)
        return op

    monkeypatch.setattr(operations, "execute_operation", fake_execute)
    conn, _cur = _fake_conn()

    result = gp.rollback_publication(
        conn, confirmation_id="pubc_1", target_mapping_version_id="dmap_old",
        actor="user-1", org_id="org-1",
    )
    assert len(captured["specs"]) == 1
    spec = captured["specs"][0]
    # DISTINCT command_type + idempotency namespace from the forward publish.
    assert spec.command_type == gp.COMMAND_ROLLBACK
    assert spec.idempotency_key.startswith(gp.COMMAND_ROLLBACK)
    assert result.current_mapping_version_id == "dmap_old"
    assert result.outcome == "succeeded"


def test_rollback_target_must_be_prior_version(monkeypatch):
    from core import governed_publication as gp

    conf = _confirmation_row(prior_mapping_version_id="dmap_old")
    monkeypatch.setattr(gp, "_load_confirmation", lambda conn, cid: conf)
    conn, _cur = _fake_conn()
    with pytest.raises(gp.PublicationConfirmationRefused) as exc:
        gp.rollback_publication(conn, confirmation_id="pubc_1",
                                target_mapping_version_id="dmap_UNRELATED",
                                actor="user-1", org_id="org-1")
    assert exc.value.code == "rollback_target_invalid"


# ---------------------------------------------------------------------------
# (e) failure / outcome_unknown reconciles with original refs (no blind mutation).
# ---------------------------------------------------------------------------


def test_changed_pointer_inside_mutation_reports_failed_with_original_refs(monkeypatch):
    from core import governed_publication as gp

    conn, cur = _fake_conn()
    # The last-line guard sees a DIFFERENT live pointer -> rowcount 0.
    cur.fetchone.return_value = ("dmap_someone_else",)
    cur.rowcount = 0
    conf = _confirmation_row()
    res = gp._publish_mutation(
        conn, "op-1", confirmation=conf, command=gp.COMMAND_PUBLISH,
        to_version_id="dmap_new", from_version_id="dmap_old",
    )
    assert res.outcome == "failed"
    assert res.result["reason"] == "changed_pointer"
    # No fresh blind mutation: the original expected/observed refs are carried.
    assert res.result["expected_pointer"] == "dmap_old"
    assert res.result["observed_pointer"] == "dmap_someone_else"


def test_pointer_advance_exception_is_outcome_unknown(monkeypatch):
    from core import governed_publication as gp

    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(gp, "_advance_pointer", boom)
    conn, _cur = _fake_conn()
    conf = _confirmation_row()
    res = gp._publish_mutation(
        conn, "op-1", confirmation=conf, command=gp.COMMAND_PUBLISH,
        to_version_id="dmap_new", from_version_id="dmap_old",
    )
    assert res.outcome == "outcome_unknown"


def test_reconcile_uses_original_refs(monkeypatch):
    from core import governed_publication as gp
    from core import operations

    seen = {}

    def fake_reconcile(conn, *, operation_id, resolver):
        seen["operation_id"] = operation_id
        seen["resolver"] = resolver
        return True

    monkeypatch.setattr(operations, "reconcile_uncertain_operation", fake_reconcile)
    conn, _cur = _fake_conn()
    ok = gp.reconcile_publication_outcome(
        conn, operation_id="op-1", resolver=lambda a, b: ("succeeded", "0" * 64)
    )
    assert ok is True
    assert seen["operation_id"] == "op-1"


# ---------------------------------------------------------------------------
# static guard + migration 073 contract.
# ---------------------------------------------------------------------------


def test_governance_mcp_never_returns_the_raw_secret_key():
    from pathlib import Path

    src = Path("server/core/governance_mcp.py").read_text(encoding="utf-8")
    # The review tool drops the secret; no handler places confirmation_secret into a
    # returned data dict.
    assert 'data["confirmation_secret"]' not in src
    assert "confirmation_secret_present" in src  # explicit model-hidden marker


# ---------------------------------------------------------------------------
# (g) TRUSTED CONSOLE REST endpoints (Story 36.18 gap fix): the out-of-band
# secret-retrieval + human confirm/rollback surface. The secret reaches the
# authenticated manage-authority human over REST but NEVER any MCP surface.
# ---------------------------------------------------------------------------


def _console_request(method: str, *, path_params=None, body=None, idempotency="pub-1"):
    import json as _json

    from starlette.requests import Request

    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": _json.dumps(body or {}).encode(),
            "more_body": False,
        }

    raw_headers = []
    if idempotency is not None:
        raw_headers.append((b"idempotency-key", idempotency.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/governance/publication-reviews",
            "path_params": path_params or {},
            "headers": raw_headers,
        },
        receive,
    )


def _console_env(monkeypatch, *, identity="camille", org_id="org-1", allowed=True):
    from contextlib import contextmanager

    from core import admin_api, db, project_access

    conn, cur = _fake_conn()

    @contextmanager
    def get_connection():
        yield conn

    async def _auth(_request):
        return (True, identity)

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(admin_api, "_check_auth", _auth)
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    decision = SimpleNamespace(allowed=allowed, org_id=org_id)
    monkeypatch.setattr(
        project_access, "resolve_strict_resource_access", lambda *_a, **_k: decision
    )
    return admin_api, conn, cur


def test_console_prepare_route_registered():
    from core.admin_api import router

    paths = {route.path for route in router.routes}
    assert "/api/governance/publication-reviews" in paths
    assert "/api/governance/publication-reviews/{confirmation_id}/confirm" in paths
    assert "/api/governance/publication-reviews/{confirmation_id}/rollback" in paths


def test_console_prepare_returns_secret_to_manage_authority_human(monkeypatch):
    import asyncio

    from core import governed_publication as gp

    admin_api, conn, cur = _console_env(monkeypatch)
    # The proposal-scope resolution row: datastream_id, project_id, org_id.
    cur.fetchone.return_value = ("ds-1", "proj-1", "org-1")
    secret = "pubs_topsecret_console"
    captured = {}

    def fake_prepare(conn, **kw):
        captured.update(kw)
        return gp.PublicationReview(
            confirmation_id="pubc_1",
            review={"profile": "governance", "scope": {}, "versions": {},
                    "confirmation_id": "pubc_1"},
            confirmation_secret=secret,
        )

    monkeypatch.setattr(gp, "prepare_publication_review", fake_prepare)

    response = asyncio.run(
        admin_api._prepare_publication_review_console(
            _console_request("POST", body={"proposal_id": "mprop_1"})
        )
    )
    assert response.status_code == 201
    import json as _json

    payload = _json.loads(response.body)
    # The out-of-band retrieval: the secret IS present in the REST JSON, once.
    assert payload["confirmation_secret"] == secret
    assert payload["confirmation_secret_single_return"] is True
    assert payload["confirmation_id"] == "pubc_1"
    # It went to the manage floor, bound to the proposal actor.
    assert captured["actor"] == "camille"
    assert captured["org_id"] == "org-1"
    assert response.headers["cache-control"].startswith("no-store")


def test_console_prepare_denies_without_manage_and_hides_existence(monkeypatch):
    import asyncio

    from core import governed_publication as gp

    admin_api, conn, cur = _console_env(monkeypatch, allowed=False)
    cur.fetchone.return_value = ("ds-1", "proj-1", "org-1")
    prepare = MagicMock()
    monkeypatch.setattr(gp, "prepare_publication_review", prepare)

    response = asyncio.run(
        admin_api._prepare_publication_review_console(
            _console_request("POST", body={"proposal_id": "mprop_1"})
        )
    )
    assert response.status_code == 404  # existence-hiding
    import json as _json

    body = _json.loads(response.body)
    assert body["code"] == "not_found"
    # An unauthorized caller NEVER reaches the domain -> never sees the secret.
    prepare.assert_not_called()
    conn.commit.assert_not_called()


def test_console_confirm_with_retrieved_secret_creates_one_operation(monkeypatch):
    import asyncio

    from core import governed_publication as gp

    admin_api, conn, cur = _console_env(monkeypatch)
    cur.fetchone.return_value = ("ds-1", "proj-1", "org-1")
    secret = "pubs_retrieved_by_console"
    captured = {"calls": 0}

    def fake_confirm(conn, **kw):
        captured["calls"] += 1
        captured["secret"] = kw["confirmation_secret"]
        return gp.PublicationResult(
            confirmation_id="pubc_1", operation_id="op-1", outcome="succeeded",
            replayed=False, current_mapping_version_id="dmap_new",
            prior_mapping_version_id="dmap_old", result={},
        )

    monkeypatch.setattr(gp, "confirm_and_publish", fake_confirm)

    response = asyncio.run(
        admin_api._confirm_publication_review_console(
            _console_request(
                "POST",
                path_params={"confirmation_id": "pubc_1"},
                body={"confirmation_secret": secret},
            )
        )
    )
    assert response.status_code == 200
    assert captured["calls"] == 1
    assert captured["secret"] == secret
    import json as _json

    payload = _json.loads(response.body)
    assert payload["operation_id"] == "op-1"
    assert payload["outcome"] == "succeeded"
    # The confirm response NEVER echoes the secret back.
    assert secret not in response.body.decode()


def test_console_confirm_requires_idempotency_key(monkeypatch):
    import asyncio

    from core import governed_publication as gp

    admin_api, conn, cur = _console_env(monkeypatch)
    confirm = MagicMock()
    monkeypatch.setattr(gp, "confirm_and_publish", confirm)

    response = asyncio.run(
        admin_api._confirm_publication_review_console(
            _console_request(
                "POST",
                path_params={"confirmation_id": "pubc_1"},
                body={"confirmation_secret": "pubs_x"},
                idempotency=None,
            )
        )
    )
    assert response.status_code == 422
    confirm.assert_not_called()


def test_console_rollback_is_distinct_and_takes_no_secret(monkeypatch):
    import asyncio

    from core import governed_publication as gp

    admin_api, conn, cur = _console_env(monkeypatch)
    cur.fetchone.return_value = ("ds-1", "proj-1", "org-1")
    captured = {}

    def fake_rollback(conn, **kw):
        captured.update(kw)
        return gp.PublicationResult(
            confirmation_id="pubc_1", operation_id="op-rb", outcome="succeeded",
            replayed=False, current_mapping_version_id="dmap_old",
            prior_mapping_version_id="dmap_new", result={},
        )

    monkeypatch.setattr(gp, "rollback_publication", fake_rollback)

    response = asyncio.run(
        admin_api._rollback_publication_review_console(
            _console_request(
                "POST",
                path_params={"confirmation_id": "pubc_1"},
                body={"target_mapping_version_id": "dmap_old"},
            )
        )
    )
    assert response.status_code == 200
    assert captured["target_mapping_version_id"] == "dmap_old"
    import json as _json

    payload = _json.loads(response.body)
    assert payload["distinct_operation"] is True
    assert payload["operation_id"] == "op-rb"


def test_secret_never_appears_in_any_mcp_facing_output(monkeypatch):
    """The MCP review tool must NOT return the secret; only the REST console does."""
    from core import governance_mcp
    from core import governed_publication as gp
    from core.project_access import AccessDecision

    handlers = _register_all(_Recorder())
    monkeypatch.setattr(governance_mcp, "_identity", lambda: "user-1")
    monkeypatch.setattr(governance_mcp, "_host_context", lambda: {})
    monkeypatch.setattr(
        governance_mcp, "_proposal_datastream", lambda conn, pid: ("ds-1", "org-1")
    )
    monkeypatch.setattr(
        governance_mcp, "_guard_datastream",
        lambda ds, ident, *, minimum_capability: AccessDecision(
            True, "owner_floor", "manage", "org-1", ("organization:org-1", f"flux:{ds}")
        ),
    )
    secret = "pubs_must_stay_hidden"
    monkeypatch.setattr(
        gp, "prepare_publication_review",
        lambda conn, **kw: gp.PublicationReview(
            confirmation_id="pubc_1",
            review={"profile": "governance", "scope": {}, "versions": {}, "diff": {},
                    "interval": None, "destination": {}, "impact": {}, "checks": [],
                    "rollback": {}, "expires_in_seconds": 900, "confirmation_id": "pubc_1"},
            confirmation_secret=secret,
        ),
    )
    conn, _cur = _fake_conn()
    import core.db as db
    monkeypatch.setattr(db, "get_connection", lambda: _ctx(conn))

    result = handlers["review_agent_change"]("mprop_1")
    # Neither the human-readable text nor the structured MCP envelope carries it.
    assert secret not in result.content[0].text
    assert secret not in repr(result.structured_content)


def test_migration_073_contains_publication_confirmation_contract():
    from pathlib import Path

    sql = Path("infra/nango/migrations/073_governed_publication.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.publication_confirmations" in sql
    assert "confirmation_reference_hash" in sql
    assert "reviewed_hashes" in sql
    assert "prior_mapping_version_id" in sql
    assert "operation_id" in sql
    assert "REFERENCES app.operations(id)" in sql
    assert "CHECK (state IN ('prepared', 'confirmed', 'dispatched', 'expired', 'failed'))" in sql
    # Immutable review evidence + append-only publication log.
    assert "review evidence is immutable" in sql
    assert "trg_publication_confirmation_immutable" in sql
    assert "app.datastream_mapping_publication_log" in sql
    assert "append-only" in sql
    assert sql.strip().startswith("-- Story 36.18")
    assert "BEGIN;" in sql and "COMMIT;" in sql
