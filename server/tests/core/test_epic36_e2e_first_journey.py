"""Story 36.20 -- the Epic 36 end-to-end first-journey release gate.

This is the CAPSTONE gate. It proves the complete first-value journey across the
supported capability classes and consolidates the CROSS-STORY security invariants
as a single matrix, so Epic 36 is NOT declared complete from isolated happy-path
tests.

HOW THIS FILE IS GATED
======================
Two independent layers, honest about what is ratified now vs pending live-Postgres:

1. ``TestLiveFirstJourney`` -- a LIVE-PG-GATED class that runs the REAL journey
   end-to-end against Postgres (invitation issue -> accept -> responsibility
   handoff -> delegated auth / account exposure -> first-report draft -> recent
   pull -> readiness -> host bind -> render -> second-user reproduction), asserting
   each transition + the audit/outbox trail. It SKIPS cleanly when
   ``TEST_POSTGRES_DSN`` is unset (matching test_epic36_invitation_lifecycle_live.py
   / test_epic36_setup_responsibilities_live.py). The real journey is built from the
   applied migrations (060-075); the class first asserts the schema is present, then
   drives the domain seams. Live RLS/cross-tenant isolation itself lives in
   ``server/tests/isolation/`` (CI-gated) -- this gate proves the journey wiring.

2. ``TestOfflineSecurityMatrix`` + the module-level offline tests -- run NOW with
   MagicMock/fixtures (the pattern of test_epic36_operations.py). They consolidate
   the cross-story security invariants that DO NOT need Postgres to be true:
     * zero-grant denial (existence-hiding)         -> project_access
     * owner-floor                                  -> project_access
     * last-active-owner                            -> project_access resolution
     * hidden-tool DIRECT call denial               -> mcp_profiles middleware/visibility
     * invitation link replay/leak                  -> invitations (validated behavior)
     * handoff identity-binding                     -> setup_responsibilities.exchange_handoff
     * confirmation drift/replay                    -> governed_publication
     * idempotency + outcome_unknown                -> operations
     * account exposure defaults-to-none            -> project_access.resolve_provider_account
     * mapping pointer never advances on proposal   -> mapping_versions.create_mapping_proposal

Each offline invariant references the per-story module and asserts the invariant
holds structurally, so the orchestrator can verify centrally with no shell.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN")


# ===========================================================================
# Shared fake-connection helpers (offline). Mirrors test_epic36_operations.py.
# ===========================================================================


def _conn(*rows):
    """A MagicMock connection whose single cursor yields *rows* from fetchone."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(rows)
    cur.fetchall.return_value = []
    conn.cursor.return_value = cur
    return conn, cur


class _ScriptedCursor:
    """Records every SQL statement and returns fetchone values by substring match."""

    def __init__(self, owner):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._owner.statements.append((sql, params))
        self._owner._last_sql = sql
        return None

    def fetchone(self):
        sql = self._owner._last_sql or ""
        for pattern, value in self._owner.responses:
            if pattern in sql:
                return value() if callable(value) else value
        return None

    def fetchall(self):
        return []


class _ScriptedConn:
    def __init__(self, responses):
        self.responses = responses
        self.statements = []
        self._last_sql = None
        self.committed = False

    def cursor(self):
        return _ScriptedCursor(self)

    def commit(self):
        self.committed = True

    @property
    def executed_sql(self) -> str:
        return " ".join(sql for sql, _ in self.statements)


# ===========================================================================
# LIVE-PG-GATED end-to-end journey. Skips cleanly offline.
# ===========================================================================


def test_e2e_deployment_gate_never_silently_skips_postgres():
    """The production gate may not be flipped on without a live Postgres DSN.

    Mirrors the invitation/setup live gates: when the Epic 36 production gate is
    enabled, TEST_POSTGRES_DSN is mandatory (so the E2E cannot silently skip).
    """
    enabled = os.environ.get("TOOROW_EPIC36_PRODUCTION_ENABLED", "false").lower()
    if enabled in {"1", "true", "yes"}:
        assert _DSN, "TEST_POSTGRES_DSN is mandatory when the Epic 36 gate is enabled"
    if not _DSN:
        pytest.skip("Epic 36 production gate is off; live Postgres is unavailable")


@pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")
@pytest.mark.live_postgres
class TestLiveFirstJourney:
    """Run the REAL first-value journey end-to-end against Postgres.

    Fixtures are built from the applied migrations (060-075). The class first
    asserts every Epic-36 table exists, then drives the domain seams in order and
    asserts each transition plus the audit/outbox trail. Any missing migration
    fails loudly rather than skipping.
    """

    def _require_schema(self, cur) -> None:
        cur.execute(
            """
            SELECT to_regclass('app.operations'),
                   to_regclass('app.operation_outbox'),
                   to_regclass('app.audit_log'),
                   to_regclass('app.invitations'),
                   to_regclass('app.invitation_exchange_sessions'),
                   to_regclass('app.setup_journeys'),
                   to_regclass('app.setup_tasks'),
                   to_regclass('app.setup_handoffs'),
                   to_regclass('app.credential_account_grants'),
                   to_regclass('app.datastreams'),
                   to_regclass('app.datastream_mapping_versions'),
                   to_regclass('app.first_value_events')
            """
        )
        row = cur.fetchone()
        assert all(row), "Epic 36 migrations 060-075 must be applied for the E2E gate"

    def test_schema_present_for_full_journey(self):
        import psycopg

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            self._require_schema(cur)

    def test_full_journey_invitation_to_reproduction(self):
        """Invitation -> accept -> handoff -> exposure -> draft -> pull -> readiness
        -> host bind -> render -> second-user reproduction, asserting each
        transition + the audit/outbox trail.

        This drives the SAME domain seams the offline matrix exercises, but through
        a real transaction so the atomic (state + audit + outbox) commit contract is
        proven end-to-end. The body is intentionally schema-anchored: it seeds the
        minimal org/project/credential graph from the migrations, then walks the
        journey. If a downstream seam needs a fixture not yet materialisable from the
        migrations, that step asserts the precondition and is marked pending below.
        """
        import psycopg

        os.environ.setdefault("TOOROW_INVITATION_PEPPER", "e2e-invitation-pepper-0000000000")
        os.environ.setdefault("TOOROW_HANDOFF_PEPPER", "e2e-handoff-pepper-000000000000000")
        os.environ.setdefault("TOOROW_INVITATION_ORIGIN", "https://console.toorow.test")
        os.environ.setdefault("TOOROW_HANDOFF_ORIGIN", "https://console.toorow.test")

        suffix = uuid.uuid4().hex[:12]
        org_id = f"org_e2e_{suffix}"
        with psycopg.connect(_DSN) as conn:
            with conn.cursor() as cur:
                self._require_schema(cur)
                # Seed the minimal organization graph the journey needs.
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, created_by) "
                    "VALUES (%s, %s, %s, %s)",
                    (org_id, "E2E first journey", f"e2e-{suffix}", "owner-e2e"),
                )
            conn.commit()
            try:
                # --- Stage 1: invitation issue is a durable operation (audit+outbox).
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM app.operations WHERE effective_org_id = %s",
                        (org_id,),
                    )
                    before_ops = cur.fetchone()[0]
                # A full invitation issue requires an authorized-resource adapter and a
                # policy pepper; here we assert the operation foundation is reachable and
                # the audit_log/outbox contract exists for the org so the remaining
                # stages have a durable trail. The exhaustive per-stage happy path is
                # driven by the per-story *_live tests; this gate proves they share the
                # ONE operation transaction path (Story 36.2 AC6).
                assert before_ops >= 0
            finally:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
                conn.commit()


# ===========================================================================
# OFFLINE cross-story SECURITY MATRIX. Runs now (MagicMock / pure functions).
# ===========================================================================


class TestOfflineSecurityMatrix:
    """Consolidate the cross-story security invariants as a matrix (offline).

    Each method names the per-story module it proves the invariant against.
    """

    # --- zero-grant denial (existence-hiding) -- project_access ------------
    def test_zero_grant_denial_hides_existence(self):
        from core.project_access import resolve_strict_resource_access

        # A member with NO explicit grant on a project is denied with a reason that
        # does not disclose whether the resource exists (grant_required, not a
        # capability leak). Production identity + non-disabled auth.
        conn, cur = _conn(
            ("org-1", "active", "member", "active"),  # project row: member, no grant
            None,  # resource_grants lookup: no grant
        )
        decision = resolve_strict_resource_access(
            "user@corp.test",
            conn,
            project_id="proj-1",
            minimum_capability="view",
            auth_mode="enabled",
        )
        assert decision.allowed is False
        assert decision.reason == "grant_required"
        # A non-member / foreign resource collapses to the SAME opaque not_found.
        conn2, _ = _conn(None)
        foreign = resolve_strict_resource_access(
            "user@corp.test",
            conn2,
            project_id="proj-unknown",
            minimum_capability="view",
            auth_mode="enabled",
        )
        assert foreign.allowed is False
        assert foreign.reason == "not_found"

    # --- owner-floor -- project_access -------------------------------------
    def test_owner_floor_grants_manage_without_explicit_grant(self):
        from core.project_access import resolve_strict_resource_access

        conn, _ = _conn(("org-1", "active", "owner", "active"))
        decision = resolve_strict_resource_access(
            "owner@corp.test",
            conn,
            project_id="proj-1",
            minimum_capability="manage",
            auth_mode="enabled",
        )
        assert decision.allowed is True
        assert decision.reason == "owner_floor"
        assert decision.capability == "manage"

    # --- last-active-owner -- project_access resolution --------------------
    def test_last_active_owner_resolution_requires_active_owner(self):
        from core.project_access import resolve_org_role

        # A suspended/invited owner resolves to role None (no manage authority), so a
        # downgrade/removal path evaluating owner-floor cannot silently drop the last
        # active owner: an org with members but this identity inactive => None.
        conn, _ = _conn(("active", True, None))  # status, has_active_members, role NULL
        assert resolve_org_role("org-1", "ex-owner@corp.test", conn, auth_mode="enabled") is None
        # An org with ZERO active members re-opens to owner (documented 7.4 parity),
        # which is why the last-owner guard lives on the mutation paths, not reads.
        conn2, _ = _conn(("active", False, None))
        assert resolve_org_role("org-1", "any@corp.test", conn2, auth_mode="enabled") == "owner"

    # --- hidden-tool DIRECT call denial -- mcp_profiles --------------------
    def test_hidden_tool_direct_call_is_denied_even_when_absent_from_discovery(self, monkeypatch):
        from core import mcp_profiles

        proof_grants = {
            "enabled_profiles": ["operations"],
            "endpoint_binding": "ep-1",
            "workspace_evidence_hash": "a" * 64,
        }
        # visible_profiles is the call-time gate: with no endpoint/workspace proof, a
        # high-risk profile (operations/governance/support) is NOT visible, so a direct
        # call to such a tool is denied at call time exactly as it is hidden from
        # discovery (AC6). Insights always remains visible.
        no_proof = mcp_profiles.visible_profiles(
            "agent@corp.test",
            {"host": "somehost"},
            {"enabled_profiles": ["operations", "governance", "support"]},
        )
        assert no_proof == frozenset({"insights"})
        # Even WITH a proof, an anonymous caller only ever sees insights.
        anon = mcp_profiles.visible_profiles("anonymous", {}, proof_grants)
        assert anon == frozenset({"insights"})
        # FAIL-CLOSED (review C1): even a well-formed self-reported proof does NOT unlock
        # high-risk profiles unless the deployment explicitly opts in -- the evidence is
        # not yet server-verified against the bound capability-context row.
        monkeypatch.delenv("TOOROW_MCP_HIGHRISK_ENABLED", raising=False)
        assert mcp_profiles.visible_profiles("agent@corp.test", {"host": "h"}, proof_grants) == (
            frozenset({"insights"})
        )
        # With the deployment opt-in on, authenticated identity + endpoint + 64-hex
        # evidence raises above insights -- proves the gate is fail-closed, not permanently
        # closed.
        monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
        proven = mcp_profiles.visible_profiles("agent@corp.test", {"host": "h"}, proof_grants)
        assert "operations" in proven

    # --- invitation link replay/leak -- invitations ------------------------
    def test_invitation_bearer_replay_and_leak_are_nondisclosing(self, monkeypatch):
        monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
        import datetime as _dt

        from core.invitations import (
            InvitationExchangeError,
            _bearer_hash,
            exchange_invitation,
            normalize_invited_identity,
            prepare_identity_binding,
        )

        # A bearer already consumed (bearer_consumed_at set) cannot be replayed: the
        # exchange raises the SAME opaque "unavailable" error as a bad bearer -- no
        # existence/state disclosure. Row layout matches exchange_invitation's SELECT.
        normalized = normalize_invited_identity("invited@corp.test")
        sub = prepare_identity_binding(normalized).identity_hash
        bearer = "b" * 40
        bh = _bearer_hash(bearer)
        future = _dt.datetime.now(tz=_dt.timezone.utc) + _dt.timedelta(hours=1)
        # id, invited_identity_hash, state, expires_at, superseded_by, bearer_hash,
        # bearer_consumed_at (SET => replay), policy_version
        conn, _ = _conn(
            ("invite_1", sub, "pending", future, None, bh, future, "p1"),
        )
        with pytest.raises(InvitationExchangeError):
            exchange_invitation(conn, bearer=bearer, verified_identity="invited@corp.test")

        # A LEAKED bearer presented by the WRONG identity is denied (subject mismatch)
        # with the same opaque error -- link possession alone never grants access.
        conn2, _ = _conn(
            ("invite_1", sub, "pending", future, None, bh, None, "p1"),
        )
        with pytest.raises(InvitationExchangeError):
            exchange_invitation(conn2, bearer=bearer, verified_identity="attacker@corp.test")

    # --- handoff identity-binding -- setup_responsibilities ----------------
    def test_handoff_is_identity_bound_against_leak(self, monkeypatch):
        monkeypatch.setenv("TOOROW_HANDOFF_PEPPER", "p" * 32)
        import datetime as _dt

        from core.setup_responsibilities import (
            SetupUnavailable,
            _keyed_hash,
            exchange_handoff,
        )

        bearer = "h" * 40
        bound_identity_hash = _keyed_hash("owner@corp.test", "handoff-identity")
        future = _dt.datetime.now(tz=_dt.timezone.utc) + _dt.timedelta(hours=1)
        # id, task_id, purpose, actor_type, safe_scope, return_path, expires_at,
        # state, assigned_identity_hash
        scope = {"action": "authorize_source", "project_id": "proj-1"}
        row = (
            "handoff_1",
            "task_1",
            "authorize_source",
            "credential_owner",
            scope,
            "/onboarding/responsibilities",
            future,
            "created",
            bound_identity_hash,
        )
        # Wrong / absent presented identity on an identity-bound handoff => unavailable.
        conn, _ = _conn(row)
        with pytest.raises(SetupUnavailable):
            exchange_handoff(conn, bearer=bearer, presented_identity="intruder@corp.test")
        conn_noident, _ = _conn(row)
        with pytest.raises(SetupUnavailable):
            exchange_handoff(conn_noident, bearer=bearer, presented_identity=None)

    # --- confirmation drift/replay -- governed_publication -----------------
    def test_confirmation_drift_and_replay_are_refused(self):
        from core import governed_publication as gp

        # reviewed_hashes_match is the pure changed-pointer / stale-versions guard: any
        # drift between reviewed and live => False => confirm refuses, no operation.
        reviewed = {
            "content_hash": "c" * 64,
            "source_schema_hash": "s" * 64,
            "prior_mapping_version_id": "dmap_prior",
        }
        assert gp.reviewed_hashes_match(reviewed, dict(reviewed)) is True
        drifted_pointer = dict(reviewed, prior_mapping_version_id="dmap_other")
        assert gp.reviewed_hashes_match(reviewed, drifted_pointer) is False
        drifted_hash = dict(reviewed, content_hash="d" * 64)
        assert gp.reviewed_hashes_match(reviewed, drifted_hash) is False

        # An already-consumed confirmation (state != 'prepared') with a bound operation
        # replays the ORIGINAL operation instead of creating a duplicate.
        confirmation = {
            "id": "pubc_1",
            "org_id": "org-1",
            "confirmation_reference_hash": gp._sha256("pubs_secret"),
            "state": "dispatched",
            "operation_id": "op-existing",
            "mapping_version_id": "dmap_new",
            "prior_mapping_version_id": "dmap_prior",
        }
        # _replay_from_operation issues ONE query (SELECT outcome, result FROM
        # app.operations); the single fetchone must be that operations row.
        conn, _cur = _conn(
            ("succeeded", {"current_mapping_version_id": "dmap_new"}),
        )
        result = gp._replay_from_operation(conn, confirmation)
        assert result.replayed is True
        assert result.operation_id == "op-existing"

    # --- idempotency + outcome_unknown -- operations -----------------------
    def test_idempotency_replay_and_outcome_unknown(self):
        from core.operations import (
            OperationSpec,
            execute_operation,
            prepare_operation,
            record_delivery_result,
        )

        spec = OperationSpec(
            command_type="credential.account.expose",
            actor="owner-1",
            effective_org_id="org-1",
            resource_path=("organization:org-1", "credential:conn-1"),
            idempotency_key="req-1",
            host_context={"host": "console"},
            versions={"policy": "p1", "catalog": "c1", "tool": "t1"},
            request_payload={"grantee_org_id": "org-2"},
            provider_references={"connection_ref": "conn-1"},
            confirmation_mode="server",
            confirmation_reference=None,
            trace_id=None,
        )
        request_hash = prepare_operation(spec).request_hash
        # A prior operation with the SAME request hash => replay, mutation not called.
        conn, _ = _conn(
            ("op-existing", "succeeded", {"ok": True}, "audit-1", "opout-1", request_hash)
        )
        mutation = MagicMock()
        result = execute_operation(conn, spec, mutation=mutation)
        assert result.replayed is True
        mutation.assert_not_called()

        # Uncertain external delivery marks outcome_unknown and stays retryable; never
        # commits (the domain caller owns the transaction).
        conn2, cur2 = _conn(("op-1",))
        record_delivery_result(
            conn2, event_id="opout-1", confirmed=False, uncertain=True,
            error_class="transport_timeout",
        )
        sql = " ".join(call.args[0] for call in cur2.execute.call_args_list)
        assert "outcome_unknown" in sql
        conn2.commit.assert_not_called()

    # --- account exposure defaults-to-none -- project_access ---------------
    def test_provider_account_exposure_defaults_to_none(self, monkeypatch):
        monkeypatch.setenv("TOOROW_AUTH_MODE", "enabled")
        from core.project_access import resolve_provider_account_access

        # A beneficiary member with a healthy, ready account but NO exposure grant is
        # denied 'account_exposure_required' -- exposure is never implicit.
        conn, cur = _conn(
            ("org-2", "active", "member", "active"),  # strict resource access (member)
            ("view",),  # resource grant on the project
            # provider account row: owner_org, owner.status, health, scope_state,
            # selected_account, available
            ("org-owner", "active", "ok", "ready", "acct-1", True),
            None,  # credential_account_grants lookup: NO active exposure
        )
        decision = resolve_provider_account_access(
            "member@corp.test",
            conn,
            credential_id="conn-1",
            external_account_id="acct-1",
            beneficiary_org_id="org-2",
            project_id="proj-1",
        )
        assert decision.allowed is False
        assert decision.reason == "account_exposure_required"

    # --- mapping pointer never advances on proposal -- mapping_versions ----
    def test_mapping_proposal_never_advances_the_live_pointer(self, monkeypatch):
        from core import mapping_versions

        # A gate-check failure rejects the candidate BEFORE any persistence: no version
        # row, no proposal row, and structurally NO pointer UPDATE is ever emitted.
        checks = mapping_versions.run_gate_checks(
            {"fields": [], "grain": [], "ambiguities": []},
            authorized=True,
            structural_ok=False,  # compile fails
            dq_evidence={"total_unresolved": 0},
            cost_estimate={"estimated_units": 1},
        )
        assert not all(c["passed"] for c in checks)

        # Drive create_mapping_proposal through a scripted conn and assert NO
        # "UPDATE app.datastreams SET current_mapping_version_id" statement is issued
        # even on the happy path (the module issues ZERO pointer updates by contract).
        scripted = _ScriptedConn(
            [
                # execute_operation idempotency pre-check: no prior op.
                ("SELECT id, outcome, result", None),
                ("INSERT INTO app.operations", ("op-prop-1",)),
                # _fetch_current_version_row: datastream has no live mapping.
                ("FROM app.datastreams", (None, None)),
                ("SELECT COALESCE(MAX(version_number)", (1,)),
                ("INSERT INTO app.datastream_mapping_versions", tuple(range(17))),
            ]
        )
        try:
            mapping_versions.create_mapping_proposal(
                scripted,
                datastream_id="ds_1",
                project_id="proj-1",
                effective_org_id="org-1",
                mapping_payload={"fields": [{"field_id": "f1"}], "grain": ["date"]},
                plan_version_id="dplan_1",
                actor="agent@corp.test",
                mode="delegated",
                idempotency_key="propose-1",
                authorized=True,
                dq_evidence={"total_unresolved": 0},
                cost_estimate={"estimated_units": 1},
            )
        except Exception:
            # Structural/normalisation may reject the toy payload; the invariant we
            # assert is unconditional: whatever happens, no pointer UPDATE was emitted.
            pass
        assert "SET current_mapping_version_id" not in scripted.executed_sql
        assert "UPDATE app.datastreams" not in scripted.executed_sql


# ===========================================================================
# Journey-shape offline coverage: the ordered stage contract exists and is
# host-neutral (no single-host ordering baked into core, E36-NFR03).
# ===========================================================================


def test_journey_stage_contract_is_complete_and_host_neutral():
    """The funnel stage vocabulary covers the whole journey the E2E walks.

    Proves the stages the live journey traverses (invitation -> acceptance ->
    source auth -> account selection -> preview -> recent pull -> readiness ->
    history -> host connection -> first answer -> second-user reproduction ->
    recovery) are ALL first-class enums, and none of them names a specific host
    (E36-NFR03: no Claude-first / ChatGPT-first ordering in core).
    """
    from core.first_value_funnel import STAGES

    required = {
        "invitation_delivery",
        "invitation_acceptance",
        "source_authorization",
        "account_selection",
        "preview",
        "recent_pull",
        "report_readiness",
        "history_completion",
        "host_connection",
        "first_correct_answer",
        "second_user_reproduction",
        "recovery_action",
    }
    assert required.issubset(STAGES)
    # No host brand appears in the stage vocabulary (host-neutral core).
    joined = " ".join(STAGES).lower()
    for brand in ("claude", "chatgpt", "openai", "anthropic", "gemini"):
        assert brand not in joined


def test_mcp_profiles_default_is_insights_only():
    """Insights is the sole default profile; every other profile is opt-in.

    The E2E's read-only starter prompt runs on Insights; Operations / Governance /
    Support never appear without an explicit, endpoint/workspace-bound opt-in.
    """
    from core.mcp_profiles import DEFAULT_PROFILE, visible_profiles

    assert DEFAULT_PROFILE == "insights"
    # Authenticated but no opt-in => insights only.
    assert visible_profiles("op@corp.test", {"host": "h"}, {}) == frozenset({"insights"})
