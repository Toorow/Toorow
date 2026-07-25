"""Story 36.7 delegated source authorization and exact account exposure tests.

Offline MagicMock-style, mirroring test_epic36_operations.py /
test_epic36_setup_responsibilities.py: the shared operation seam, the handoff
carrier, exposure and revalidation are stubbed so the delegation state machine
and its fail-closed callback verification are exercised without live Postgres.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _env(monkeypatch):
    monkeypatch.setenv("TOOROW_DELEGATION_STATE_SECRET", "s" * 40)
    monkeypatch.setenv("TOOROW_HANDOFF_PEPPER", "p" * 32)
    monkeypatch.setenv("TOOROW_HANDOFF_ORIGIN", "https://console.toorow.test")
    monkeypatch.setenv(
        "TOOROW_DELEGATION_REDIRECT_URI", "https://console.toorow.test/oauth/delegated/callback"
    )


def _cur(*fetchone_rows, fetchall=None):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = 1
    return cur


def _conn_with(cur):
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _stub_operation(monkeypatch, module, *, capture=None):
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


# ---------------------------------------------------------------------------
# (a) prepare binds source/provider/orgs/scopes/expiry/nonce + PKCE for non-Google.
# ---------------------------------------------------------------------------
def test_prepare_binds_context_and_pkce_for_non_google_provider(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    task_row = ("task-1", "journey-1", "org-owner", "waiting", None, "source_authorization")
    cur = _cur(task_row)
    conn = _conn_with(cur)
    capture: dict = {}
    _stub_operation(monkeypatch, sd, capture=capture)

    handoff = MagicMock()
    handoff.delivery_url = "https://console.toorow.test/handoff#handoff=abc"
    monkeypatch.setattr(sd, "prepare_handoff", MagicMock(return_value=handoff))

    result = sd.prepare_source_delegation(
        conn,
        task_id="task-1",
        source="meta_ads",
        provider="meta",
        owner_org_id="org-owner",
        beneficiary_org_id="org-benef",
        requested_scopes=["ads_read", "business_management"],
        redirect_allowlist_ref="allow-meta",
        actor="camille@example.com",
        idempotency_key="deleg-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    # PKCE S256 present for a non-Google provider.
    assert result.pkce_method == "S256"
    assert result.pkce_challenge and len(result.pkce_challenge) >= 43
    assert result.state == "prepared"
    assert result.authorize_state.count(".") == 1  # <payload>.<hmac>

    spec = capture["specs"][0]
    assert spec.command_type == "source.delegation.prepare"
    assert spec.effective_org_id == "org-owner"
    # Binding is carried in the request payload (identifiers/scopes only).
    payload = spec.request_payload
    assert payload["source"] == "meta_ads"
    assert payload["provider"] == "meta"
    assert payload["beneficiary_org_id"] == "org-benef"
    assert payload["requested_scopes"] == ["ads_read", "business_management"]
    assert payload["google_direct"] is False
    assert payload["pkce_method"] == "S256"

    # The INSERT persists nonce_hash (not the raw nonce) and the pkce challenge.
    insert_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "app.source_delegations" in insert_sql
    assert "nonce_hash" in insert_sql
    assert "pkce_verifier_hash" in insert_sql


def test_prepare_omits_pkce_for_google_direct_provider(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    cur = _cur(("task-1", "journey-1", "org-owner", "waiting", None, "source_authorization"))
    conn = _conn_with(cur)
    capture: dict = {}
    _stub_operation(monkeypatch, sd, capture=capture)
    monkeypatch.setattr(sd, "prepare_handoff", MagicMock(return_value=MagicMock(delivery_url=None)))

    result = sd.prepare_source_delegation(
        conn,
        task_id="task-1",
        source="ga4",
        provider="google",
        owner_org_id="org-owner",
        beneficiary_org_id="org-benef",
        requested_scopes=["analytics.readonly"],
        redirect_allowlist_ref="allow-google",
        actor="camille@example.com",
        idempotency_key="deleg-2",
        host_context={"host": "rest"},
        trace_id=None,
    )

    # Google-direct reuses google_oauth state+nonce; PKCE is intentionally absent.
    assert result.pkce_challenge is None
    assert result.pkce_method is None
    assert capture["specs"][0].request_payload["google_direct"] is True


# ---------------------------------------------------------------------------
# (b) callback REJECTS on redirect/state/nonce/PKCE mismatch BEFORE any transition.
# ---------------------------------------------------------------------------
def _prepared_delegation_row(*, provider="meta", nonce="the-nonce", state="prepared"):
    """A source_delegations SELECT ... FOR UPDATE row for verify_delegated_callback."""
    from core import source_delegation as sd

    nonce_hash = sd._keyed_hash(nonce, "delegation-nonce")
    challenge = "chal" + "x" * 40
    return (
        "deleg-1",  # delegation_id
        "task-1",  # task_id
        "meta_ads",  # source
        provider,  # provider
        "org-owner",  # owner_org_id
        "org-benef",  # beneficiary_org_id
        ["ads_read"],  # requested_scopes
        state,  # state
        nonce_hash,  # nonce_hash
        challenge,  # pkce_challenge
        "S256",  # pkce_method
        "allow-meta",  # redirect_allowlist_ref
        datetime.now(timezone.utc) + timedelta(hours=1),  # expires_at
    )


def test_callback_rejects_bad_state_before_any_transition(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    conn = _conn_with(_cur())  # no SELECT expected -- state fails first
    with pytest.raises(sd.DelegationVerificationError):
        sd.verify_delegated_callback(
            conn,
            delegation_id="deleg-1",
            state_param="forged.tampered",
            redirect_uri="https://console.toorow.test/oauth/delegated/callback",
        )


def test_callback_rejects_redirect_allowlist_mismatch(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    nonce = "the-nonce"
    state = sd._mint_state("deleg-1", nonce)
    row = _prepared_delegation_row(nonce=nonce)
    # First fetchone: delegation row. Second: allow-list fetchall (empty here).
    cur = _cur(row, fetchall=[])
    conn = _conn_with(cur)
    with pytest.raises(sd.DelegationVerificationError):
        sd.verify_delegated_callback(
            conn,
            delegation_id="deleg-1",
            state_param=state,
            redirect_uri="https://evil.example/callback",  # not in the exact allow-list
        )


def test_callback_rejects_bad_nonce(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    # Mint a valid HMAC state (so state passes) but persist a DIFFERENT nonce_hash.
    state = sd._mint_state("deleg-1", "presented-nonce")
    row = list(_prepared_delegation_row(nonce="presented-nonce"))
    row[8] = sd._keyed_hash("a-completely-different-nonce", "delegation-nonce")
    cur = _cur(tuple(row))
    conn = _conn_with(cur)
    with pytest.raises(sd.DelegationVerificationError):
        sd.verify_delegated_callback(
            conn,
            delegation_id="deleg-1",
            state_param=state,
            redirect_uri="https://console.toorow.test/oauth/delegated/callback",
        )


def test_callback_rejects_missing_pkce_for_non_google(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    nonce = "the-nonce"
    state = sd._mint_state("deleg-1", nonce)
    row = list(_prepared_delegation_row(nonce=nonce, provider="meta"))
    row[9] = None  # pkce_challenge stripped
    row[10] = None  # pkce_method stripped
    cur = _cur(tuple(row))
    conn = _conn_with(cur)
    with pytest.raises(sd.DelegationVerificationError):
        sd.verify_delegated_callback(
            conn,
            delegation_id="deleg-1",
            state_param=state,
            redirect_uri="https://console.toorow.test/oauth/delegated/callback",
        )


def test_callback_accepts_exact_redirect_state_nonce_pkce(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    nonce = "the-nonce"
    state = sd._mint_state("deleg-1", nonce)
    row = _prepared_delegation_row(nonce=nonce)
    cur = _cur(row, fetchall=[("https://console.toorow.test/oauth/delegated/callback",)])
    conn = _conn_with(cur)
    context = sd.verify_delegated_callback(
        conn,
        delegation_id="deleg-1",
        state_param=state,
        redirect_uri="https://console.toorow.test/oauth/delegated/callback",
    )
    assert context["delegation_id"] == "deleg-1"
    assert context["beneficiary_org_id"] == "org-benef"


# ---------------------------------------------------------------------------
# (c) exposure defaults to NONE -- no account exposed without an explicit allow.
# ---------------------------------------------------------------------------
def test_prepare_never_exposes_an_account(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    cur = _cur(("task-1", "journey-1", "org-owner", "waiting", None, "source_authorization"))
    conn = _conn_with(cur)
    _stub_operation(monkeypatch, sd)
    monkeypatch.setattr(sd, "prepare_handoff", MagicMock(return_value=MagicMock(delivery_url=None)))

    exposed = {"called": False}

    def _fail_expose(*a, **k):  # pragma: no cover - must never run
        exposed["called"] = True
        raise AssertionError("prepare must not expose any account")

    # expose_account is imported lazily inside complete_source_delegation only.
    import core.account_exposure as ae

    monkeypatch.setattr(ae, "expose_account", _fail_expose)

    sd.prepare_source_delegation(
        conn,
        task_id="task-1",
        source="meta_ads",
        provider="meta",
        owner_org_id="org-owner",
        beneficiary_org_id="org-benef",
        requested_scopes=["ads_read"],
        redirect_allowlist_ref="allow-meta",
        actor="camille@example.com",
        idempotency_key="deleg-3",
        host_context={"host": "rest"},
        trace_id=None,
    )
    assert exposed["called"] is False
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "credential_account_grants" not in sql  # no grant written at prepare


# ---------------------------------------------------------------------------
# (d) on resume, resolve_provider_account_access is re-checked (not trusted).
# ---------------------------------------------------------------------------
def test_complete_revalidates_access_and_fails_closed_when_denied(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd
    from core.project_access import AccessDecision

    nonce = "the-nonce"
    state = sd._mint_state("deleg-1", nonce)
    verify_row = _prepared_delegation_row(nonce=nonce)
    # Sequence of fetchone() across the whole complete() path:
    #  1. verify_delegated_callback SELECT
    #  2. credential-ownership lookup (connection_ref.owner_org_id)
    #  3. beneficiary project lookup (task -> journey.project_id)
    #  4. _transition('failed') SELECT state,owner,benef
    transition_row = ("prepared", "org-owner", "org-benef")
    cur = _cur(
        verify_row,
        ("org-owner",),
        ("proj-1",),
        transition_row,
        fetchall=[("https://console.toorow.test/oauth/delegated/callback",)],
    )
    conn = _conn_with(cur)
    _stub_operation(monkeypatch, sd)

    monkeypatch.setattr(
        sd, "prepare_handoff", MagicMock(return_value=MagicMock(delivery_url=None))
    )
    import core.account_exposure as ae

    expose = MagicMock()
    monkeypatch.setattr(ae, "expose_account", expose)

    revalidate = MagicMock(return_value=AccessDecision(False, "connection_unhealthy"))
    import core.project_access as pa

    monkeypatch.setattr(pa, "resolve_provider_account_access", revalidate)
    # The credential owner authorizes; the caller manages the owner org (review H1).
    monkeypatch.setattr(pa, "identity_can_manage_org", lambda *a, **k: True)

    with pytest.raises(sd.DelegationConflict):
        sd.complete_source_delegation(
            conn,
            delegation_id="deleg-1",
            state_param=state,
            redirect_uri="https://console.toorow.test/oauth/delegated/callback",
            credential_id="conn-1",
            external_account_id="acct-1",
            grant_id="grant-1",
            actor="louis@example.com",
            idempotency_key="complete-1",
            host_context={"host": "rest"},
            trace_id=None,
        )

    # Revalidation ran on resume (health/exposure re-checked, prior state untrusted)
    # WITH a real resource scope (review C1: project resolved from task -> journey).
    revalidate.assert_called_once()
    assert revalidate.call_args.kwargs.get("project_id") == "proj-1"
    # Exposure was attempted (explicit allow) but access still fails closed.
    expose.assert_called_once()


def test_complete_exposes_exact_account_and_reconciles_to_source_authorized(monkeypatch):
    _env(monkeypatch)
    from core import operations
    from core import source_delegation as sd
    from core.project_access import AccessDecision

    nonce = "the-nonce"
    state = sd._mint_state("deleg-1", nonce)
    verify_row = _prepared_delegation_row(nonce=nonce)
    transition_row = ("prepared", "org-owner", "org-benef")
    # _reconcile_source_task SELECT state,return_condition
    task_row = ("waiting", {"kind": "source_authorized", "resource_id": "conn-1"})
    cur = _cur(
        verify_row,
        ("org-owner",),  # credential-ownership lookup (connection_ref.owner_org_id)
        ("proj-1",),  # beneficiary project lookup (task -> journey.project_id)
        transition_row,
        task_row,
        fetchall=[("https://console.toorow.test/oauth/delegated/callback",)],
    )
    conn = _conn_with(cur)
    _stub_operation(monkeypatch, sd)

    import core.project_access as _pa_manage

    monkeypatch.setattr(_pa_manage, "identity_can_manage_org", lambda *a, **k: True)

    import core.account_exposure as ae

    expose_result = operations.OperationResult(
        "op-expose", "succeeded", {"id": "grant-1", "external_account_id": "acct-1"},
        "audit-x", "outbox-x", False,
    )
    monkeypatch.setattr(ae, "expose_account", MagicMock(return_value=expose_result))

    import core.project_access as pa

    monkeypatch.setattr(
        pa,
        "resolve_provider_account_access",
        MagicMock(return_value=AccessDecision(True, "exact_account_exposure", "view", "org-benef")),
    )

    result = sd.complete_source_delegation(
        conn,
        delegation_id="deleg-1",
        state_param=state,
        redirect_uri="https://console.toorow.test/oauth/delegated/callback",
        credential_id="conn-1",
        external_account_id="acct-1",
        grant_id="grant-1",
        actor="louis@example.com",
        idempotency_key="complete-2",
        host_context={"host": "rest"},
        trace_id=None,
    )
    assert result.state == "exposed"
    assert result.task_reconciled_state == "completed"


# ---------------------------------------------------------------------------
# (e) no token/secret appears in any operation payload or model-facing result.
# ---------------------------------------------------------------------------
def test_no_secret_in_operation_payloads_or_result(monkeypatch):
    _env(monkeypatch)
    from core import source_delegation as sd

    cur = _cur(("task-1", "journey-1", "org-owner", "waiting", None, "source_authorization"))
    conn = _conn_with(cur)
    capture: dict = {}
    _stub_operation(monkeypatch, sd, capture=capture)
    monkeypatch.setattr(sd, "prepare_handoff", MagicMock(return_value=MagicMock(delivery_url=None)))

    result = sd.prepare_source_delegation(
        conn,
        task_id="task-1",
        source="meta_ads",
        provider="meta",
        owner_org_id="org-owner",
        beneficiary_org_id="org-benef",
        requested_scopes=["ads_read"],
        redirect_allowlist_ref="allow-meta",
        actor="camille@example.com",
        idempotency_key="deleg-4",
        host_context={"host": "rest"},
        trace_id=None,
    )

    blob = json.dumps(
        {
            "specs": [s.request_payload for s in capture["specs"]],
            "outbox": [c.outbox_payload for c in capture["changes"]],
            "result": [c.result for c in capture["changes"]],
        }
    ).lower()
    for forbidden in ("access_token", "refresh_token", "client_secret", "code_verifier", "secret"):
        assert forbidden not in blob, forbidden
    # The raw PKCE verifier is never returned to the caller (only the challenge).
    assert not hasattr(result, "pkce_verifier")


# ---------------------------------------------------------------------------
# (f) migration 068 text contains the contract.
# ---------------------------------------------------------------------------
def test_migration_068_declares_delegation_contract():
    sql = Path("infra/nango/migrations/068_delegated_source_authorization.sql").read_text()
    for fragment in (
        "app.source_delegations",
        "delegation_id",
        "REFERENCES app.setup_tasks(id) ON DELETE RESTRICT",
        "REFERENCES app.organizations(id)",
        "owner_org_id",
        "beneficiary_org_id",
        "requested_scopes",
        "'prepared'",
        "'authorized'",
        "'exposed'",
        "'failed'",
        "'expired'",
        "'revoked'",
        "nonce_hash",
        "pkce_challenge",
        "redirect_allowlist_ref",
        "expires_at",
        "REFERENCES app.operations(id)",
        "protect_source_delegation_binding",
        "source delegation binding is immutable",
    ):
        assert fragment in sql, fragment
    # NO token/secret material ever stored.
    assert "access_token" not in sql
    assert "refresh_token" not in sql
    assert "client_secret" not in sql
    # verifier persisted only as a keyed hash, never raw.
    assert "pkce_verifier_hash" in sql
    assert "pkce_verifier TEXT" not in sql
