"""Story 36.19 -- first-value funnel WIRING tests (E36-FR09 / AD-32 / E36-NFR05).

The funnel domain (``core.first_value_funnel``) is safe but INERT unless the Epic 36
stage points actually call its single instrumentation seam. These tests assert the
wiring, NOT the funnel domain itself (that is ``test_epic36_first_value_funnel.py``):

  (a) a representative stage point (recent_pull) actually calls ``record_funnel_stage``
      with an ALLOWLISTED stage + outcome and a PSEUDONYMOUS ref -- and the call args
      carry NO raw email / org / project / datastream string (AD-32).
  (b) invitation acceptance drives the seam with ``invitation_acceptance`` succeeded and
      binds the journey to the project ONCE (``bind_journey_to_project``).
  (c) a funnel-recording EXCEPTION NEVER breaks the underlying domain operation
      (best-effort / non-fatal) -- the recent-first publication still returns its result.
  (d) with NO funnel pepper the seam no-ops (so existing MagicMock domain tests are
      undisturbed) and never touches the connection.
  (e) the ``GET /api/projects/{project_id}/first-value/journeys`` endpoint returns ONLY
      authorized journeys and EXISTENCE-HIDES (404) on access denial (fail-closed).

Fully offline: MagicMock connections; ``record_funnel_stage`` monkeypatched to capture
the exact call args so we can prove no raw identifier travels through the seam.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import first_value_instrumentation as fvi  # noqa: E402

_PEPPER = "w" * 40
_RAW_ORG = "org_secret_123"
_RAW_PROJECT = "proj_secret_456"
_RAW_EMAIL = "operator@secret-tenant.example.com"

# Every closed allowlist the seam is permitted to emit (kept in sync with the funnel
# domain enums; a wiring call that used anything else would fail these assertions).
_ALLOWED_STAGES = {
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
_ALLOWED_OUTCOMES = {"started", "succeeded", "degraded", "failed", "abandoned", "blocked"}


@pytest.fixture
def pepper(monkeypatch):
    monkeypatch.setenv("TOOROW_FUNNEL_PEPPER", _PEPPER)
    monkeypatch.setenv("TOOROW_POLICY_VERSION", "v-test")


def _capture_record(monkeypatch):
    """Monkeypatch ``first_value_funnel.record_funnel_stage`` to capture call kwargs.

    Returns the list of captured kwargs dicts. The real pseudonymisers stay live, so we
    also prove the persisted ref is a keyed hash, never a raw identifier.
    """
    import core.first_value_funnel as fvf

    calls: list[dict] = []

    def _spy(_conn, **kwargs):
        calls.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(fvf, "record_funnel_stage", _spy)
    return calls


def _no_raw_identifiers(kwargs: dict) -> None:
    """Assert NO raw org / project / email string appears anywhere in the call kwargs."""
    blob = repr(kwargs)
    assert _RAW_ORG not in blob
    assert _RAW_PROJECT not in blob
    assert _RAW_EMAIL not in blob
    # The journey ref that IS present must be a 64-hex keyed hash, not a raw value.
    ref = kwargs.get("journey_ref")
    assert isinstance(ref, str) and len(ref) == 64 and all(c in "0123456789abcdef" for c in ref)


# ---------------------------------------------------------------------------
# (a) representative stage point: recent_pull calls the seam with allowlisted
#     enums + a pseudonymous ref, carrying no raw identifier.
# ---------------------------------------------------------------------------
def test_recent_pull_records_allowlisted_stage_with_pseudonymous_ref(pepper, monkeypatch):
    from core import recent_first_publication as rfp

    calls = _capture_record(monkeypatch)
    conn = MagicMock()
    rfp._record_recent_pull(
        conn, project_id=_RAW_PROJECT, actor=_RAW_EMAIL, disposition="published"
    )

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["stage"] == "recent_pull" and kwargs["stage"] in _ALLOWED_STAGES
    assert kwargs["outcome"] == "succeeded" and kwargs["outcome"] in _ALLOWED_OUTCOMES
    _no_raw_identifiers(kwargs)


def test_recent_pull_disposition_maps_to_allowlisted_outcomes(pepper, monkeypatch):
    from core import recent_first_publication as rfp

    calls = _capture_record(monkeypatch)
    conn = MagicMock()
    for disposition, expected in (
        ("published", "succeeded"),
        ("blocked", "blocked"),
        ("failed", "failed"),
    ):
        rfp._record_recent_pull(
            conn, project_id=_RAW_PROJECT, actor=_RAW_EMAIL, disposition=disposition
        )
    assert [c["outcome"] for c in calls] == ["succeeded", "blocked", "failed"]
    assert all(c["stage"] == "recent_pull" for c in calls)


# ---------------------------------------------------------------------------
# (b) invitation acceptance drives the seam: acceptance succeeded + bind ONCE.
# ---------------------------------------------------------------------------
def test_invitation_acceptance_records_and_binds_journey(pepper, monkeypatch):
    """The acceptance seam records ``invitation_acceptance`` + binds the journey once.

    We exercise ``record_stage`` exactly as invitations.accept_invitation's mutation does
    (with bind_project=True) and assert the funnel record + the project bridge both fire
    with only pseudonymous data.
    """
    import core.first_value_funnel as fvf

    record_calls: list[dict] = []
    bind_calls: list[dict] = []

    monkeypatch.setattr(
        fvf, "record_funnel_stage", lambda _c, **k: record_calls.append(k) or MagicMock()
    )
    monkeypatch.setattr(
        fvf, "bind_journey_to_project", lambda _c, **k: bind_calls.append(k)
    )

    fvi.record_stage(
        MagicMock(),
        org_id=_RAW_ORG,
        project_id=_RAW_PROJECT,
        subject=_RAW_EMAIL,
        stage="invitation_acceptance",
        outcome="succeeded",
        bind_project=True,
    )

    assert len(record_calls) == 1
    assert record_calls[0]["stage"] == "invitation_acceptance"
    assert record_calls[0]["outcome"] == "succeeded"
    _no_raw_identifiers(record_calls[0])
    # Bound exactly once, and the bridge carries the pseudonymous hash + project only.
    assert len(bind_calls) == 1
    assert _RAW_EMAIL not in repr(bind_calls[0])
    assert len(bind_calls[0]["journey_ref"]) == 64


# ---------------------------------------------------------------------------
# (c) best-effort: a funnel-recording EXCEPTION never breaks the domain operation.
# ---------------------------------------------------------------------------
def test_seam_swallows_funnel_exception(pepper, monkeypatch):
    import core.first_value_funnel as fvf

    def _boom(_conn, **_kwargs):
        raise RuntimeError("analytics sink is on fire")

    monkeypatch.setattr(fvf, "record_funnel_stage", _boom)
    # The seam must swallow and return None -- it must NOT propagate the RuntimeError.
    assert (
        fvi.record_stage(
            MagicMock(),
            org_id=_RAW_ORG,
            project_id=_RAW_PROJECT,
            subject=_RAW_EMAIL,
            stage="recent_pull",
            outcome="succeeded",
        )
        is None
    )


def test_recent_first_publish_survives_funnel_failure(pepper, monkeypatch):
    """A recent-first publish still returns its result even if the funnel raises."""
    from core import first_value_funnel as fvf
    from core import recent_first_publication as rfp

    monkeypatch.setattr(
        fvf,
        "record_funnel_stage",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # A minimal terminal-replay path: an already-published execution returns without any
    # of the Epic 12 machinery. The funnel raise inside _record_recent_pull must not
    # bubble out.
    monkeypatch.setattr(
        rfp,
        "_load_saved_draft",
        MagicMock(
            return_value={
                "draft_id": "frdraft_1",
                "datastream_id": "ds-1",
                "plan_version_id": "dsp_1",
                "mapping_version_id": "dmap_1",
                "projection_plan": {"executable": True},
                "interval": {
                    "start": "2026-06-22T00:00:00Z",
                    "end_exclusive": "2026-07-22T00:00:00Z",
                },
            }
        ),
    )
    monkeypatch.setattr(rfp, "_current_pointer", lambda *_a, **_k: "dse_pub")
    monkeypatch.setattr(rfp, "_read_coverage", lambda *_a, **_k: None)
    monkeypatch.setattr(
        rfp,
        "create_execution",
        MagicMock(return_value={"id": "dse_pub", "state": "published"}),
    )

    result = rfp.execute_recent_first(
        MagicMock(),
        project_id=_RAW_PROJECT,
        actor=_RAW_EMAIL,
        idempotency_key="idem-1",
        draft_id="frdraft_1",
    )
    # The domain op completed despite the funnel raising.
    assert result.execution_id == "dse_pub"
    assert result.published is True


# ---------------------------------------------------------------------------
# (d) with NO pepper the seam no-ops and never touches the connection.
# ---------------------------------------------------------------------------
def test_seam_noops_without_pepper(monkeypatch):
    monkeypatch.delenv("TOOROW_FUNNEL_PEPPER", raising=False)
    assert fvi.funnel_enabled() is False
    conn = MagicMock()
    assert (
        fvi.record_stage(
            conn,
            org_id=_RAW_ORG,
            project_id=_RAW_PROJECT,
            subject=_RAW_EMAIL,
            stage="recent_pull",
            outcome="succeeded",
        )
        is None
    )
    # Never opened a cursor / touched the connection when the seam is disabled.
    conn.cursor.assert_not_called()


def test_recent_pull_noops_without_pepper_and_skips_org_lookup(monkeypatch):
    """Without a pepper, _record_recent_pull must add NO query (pure-read guarantee)."""
    from core import recent_first_publication as rfp

    monkeypatch.delenv("TOOROW_FUNNEL_PEPPER", raising=False)
    conn = MagicMock()
    rfp._record_recent_pull(
        conn, project_id=_RAW_PROJECT, actor=_RAW_EMAIL, disposition="published"
    )
    conn.cursor.assert_not_called()


# ---------------------------------------------------------------------------
# (e) the read endpoint: authorized -> journeys; denied -> existence-hidden 404.
# ---------------------------------------------------------------------------
def _request(project_id: str):
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    return req


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_journeys_endpoint_returns_only_authorized_journeys(monkeypatch):
    import core.db as db
    import core.first_value_funnel as fvf
    import core.project_access as pa
    from core import admin_api

    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "u@x.com")))
    monkeypatch.setattr(pa, "epic36_production_access_enabled", lambda: True)

    decision = MagicMock()
    decision.allowed = True
    monkeypatch.setattr(pa, "resolve_strict_resource_access", lambda *a, **k: decision)

    view = fvf.JourneyView(
        "a" * 64, [{"stage": "recent_pull", "outcome": "succeeded"}], ["toorow_platform"]
    )
    monkeypatch.setattr(fvf, "tenant_journeys", lambda *a, **k: [view])

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    monkeypatch.setattr(db, "get_connection", lambda: conn)

    resp = _run(admin_api._get_first_value_journeys(_request(_RAW_PROJECT)))
    assert resp.status_code == 200
    import json as _json

    body = _json.loads(resp.body)
    assert body["project_id"] == _RAW_PROJECT
    assert len(body["journeys"]) == 1
    assert body["journeys"][0]["journey_ref"] == "a" * 64
    assert body["journeys"][0]["wait_state_owners"] == ["toorow_platform"]


def test_journeys_endpoint_existence_hides_on_denial(monkeypatch):
    import core.db as db
    import core.first_value_funnel as fvf
    import core.project_access as pa
    from core import admin_api

    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "u@x.com")))
    monkeypatch.setattr(pa, "epic36_production_access_enabled", lambda: True)

    decision = MagicMock()
    decision.allowed = False  # access DENIED
    monkeypatch.setattr(pa, "resolve_strict_resource_access", lambda *a, **k: decision)

    # tenant_journeys must NEVER be reached once access is denied up-front.
    sentinel = MagicMock(side_effect=AssertionError("tenant_journeys called on denial"))
    monkeypatch.setattr(fvf, "tenant_journeys", sentinel)

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    monkeypatch.setattr(db, "get_connection", lambda: conn)

    resp = _run(admin_api._get_first_value_journeys(_request(_RAW_PROJECT)))
    assert resp.status_code == 404
    sentinel.assert_not_called()


def test_journeys_endpoint_requires_auth(monkeypatch):
    from core import admin_api

    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(False, "")))
    resp = _run(admin_api._get_first_value_journeys(_request(_RAW_PROJECT)))
    assert resp.status_code == 401


def test_journeys_route_is_registered():
    from core import admin_api

    paths = {getattr(r, "path", None) for r in admin_api.router.routes}
    assert "/api/projects/{project_id}/first-value/journeys" in paths
