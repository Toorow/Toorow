"""Story 49.3 — the Semantic Model command family, on the PRODUCTION app factory.

Mounted through `build_asgi_app()` rather than a hand-built Router, because a
route that exists only in a test fixture is exactly the "server capability with
no door" this repository keeps producing.

What these hold in place, in the order a request meets them:

* the four change-set routes are REACHABLE at their canonical addresses;
* authorization is resolved before anything else, from the authenticated
  identity — a client-supplied `org_id` is never authority;
* a foreign Project is a non-disclosing 404, indistinguishable from one that
  does not exist;
* reading needs `view`, drafting and publishing need `edit`;
* a stale or replayed confirmation is refused with 409, not applied;
* a refusal carries its named reasons rather than a generic failure.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from core.project_access import AccessDecision
from core.semantic_model import ChangeSet, SemanticNotFound, SemanticRefused, SemanticStale
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

ROOT = "/api/projects/proj_EXAMPLE/governance/semantic-model/change-sets"
CHANGE_SET = "scs_01EXAMPLE0000000000000000"


class _Connection:
    def __init__(self):
        self.commit = MagicMock()


@contextmanager
def _connection():
    yield _Connection()


def _client():
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _allowed(capability: str = "edit"):
    return AccessDecision(True, "explicit_grant", capability, "org_EXAMPLE")


def _denied(reason: str = "insufficient_capability"):
    return AccessDecision(False, reason, None, None)


def _change_set(**overrides) -> ChangeSet:
    base = dict(
        id=CHANGE_SET,
        project_id="proj_EXAMPLE",
        object_type="semantic-concept",
        object_id=None,
        base_version_id=None,
        intent={"action": "create_concept", "concept": {"name": "revenue", "kind": "metric"}},
        diff={},
        dependency_fingerprint=None,
        validation={},
        test_gate_state="unevaluated",
        state="open",
        expires_at=None,
        result_version_id=None,
        created_by="person@example.com",
        created_at="2026-07-30T09:00:00Z",
    )
    base.update(overrides)
    return ChangeSet(**base)


@contextmanager
def _seam(capability: str = "edit", decision: AccessDecision | None = None, **patches):
    """One authorized request through the production app, with every owner
    boundary replaced by a named double so the SEAM is what is under test."""
    resolved = decision if decision is not None else _allowed(capability)
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.semantic_model_api.resolve_strict_resource_access", return_value=resolved
        ) as access,
        patch("core.db.get_connection", side_effect=_connection),
    ):
        with patch.multiple("core.semantic_model_api", **patches):
            yield access


# ---------------------------------------------------------------------------
# The doors exist
# ---------------------------------------------------------------------------


def test_the_four_change_set_routes_are_reachable_on_the_production_app():
    """`build_asgi_app()` returns a middleware closure, not an introspectable
    router, so reachability is proven the only way that actually matters: by
    sending a request and checking a HANDLER answered.

    A path with no route returns Starlette's own 404 body. This surface's 404 is
    its own sentence, so the two are distinguishable — and a route that existed
    only in a test fixture would fail here.
    """
    client = _client()
    calls = {
        ("POST", ROOT): dict(create_change_set=MagicMock(return_value=_change_set())),
        ("GET", f"{ROOT}/{CHANGE_SET}"): dict(
            get_change_set=MagicMock(return_value=_change_set())
        ),
        ("POST", f"{ROOT}/{CHANGE_SET}/prepare"): dict(
            prepare_change_set=MagicMock(return_value=_change_set().as_dict())
        ),
        ("POST", f"{ROOT}/{CHANGE_SET}/confirm"): dict(
            confirm_change_set=MagicMock(return_value={"result_version_id": "scv_1"})
        ),
    }
    for (method, path), patches in calls.items():
        with _seam(**patches):
            response = client.request(
                method,
                path,
                json={
                    "object_type": "semantic-concept",
                    "intent": {"action": "create_concept"},
                    "idempotency_key": "k",
                    "confirmation_token": "t",
                },
            )
        assert response.status_code in {200, 201}, (method, path, response.status_code)
        assert "detail" not in response.json(), f"{method} {path} hit no handler"


def test_the_governance_read_surface_still_answers_semantic_model():
    """The command family shares a prefix with the read surface and is registered
    BEFORE it. Registering it first must not have swallowed the section route."""
    composed = MagicMock(return_value={"schema_version": "governance-collection.v1"})
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=_allowed("view"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.governance_surface_api.compose_governance_collection", composed),
    ):
        response = _client().get("/api/projects/proj_EXAMPLE/governance/semantic-model")
    assert response.status_code == 200
    # The READ surface answered, not the command family.
    composed.assert_called_once()
    assert composed.call_args.args[1] == "semantic-model"


def test_creating_a_change_set_returns_201_and_the_server_owned_envelope():
    with _seam(create_change_set=MagicMock(return_value=_change_set())) as access:
        response = _client().post(
            ROOT,
            json={
                "object_type": "semantic-concept",
                "intent": {"action": "create_concept", "concept": {}},
                "idempotency_key": "key-1",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["change_set_id"] == CHANGE_SET
    assert body["state"] == "open"
    assert body["policy_version"] == "semantic-model.policy.v1"
    assert access.call_args.kwargs["minimum_capability"] == "edit"


# ---------------------------------------------------------------------------
# Authorization comes first, and org scope is derived
# ---------------------------------------------------------------------------


def test_authorization_is_resolved_before_the_service_is_called():
    create = MagicMock(return_value=_change_set())
    with _seam(decision=_denied(), create_change_set=create):
        response = _client().post(
            ROOT,
            json={"object_type": "semantic-concept", "intent": {}, "idempotency_key": "k"},
        )
    assert response.status_code == 403
    create.assert_not_called()


def test_a_client_supplied_org_id_is_never_authority():
    create = MagicMock(return_value=_change_set())
    with _seam(create_change_set=create) as access:
        _client().post(
            ROOT,
            json={
                "object_type": "semantic-concept",
                "intent": {"action": "create_concept"},
                "idempotency_key": "k",
                # A caller naming another Organization. It must reach nothing.
                "org_id": "org_SOMEONE_ELSE",
            },
        )
    # The Organization used downstream is the one the AUTHORIZED Project resolved
    # to, never the one the body claimed.
    assert access.call_args.kwargs["project_id"] == "proj_EXAMPLE"
    assert create.call_args.args[1] == "proj_EXAMPLE"


def test_a_foreign_project_is_a_non_disclosing_404():
    with _seam(decision=_denied("not_found"), create_change_set=MagicMock()):
        response = _client().post(
            ROOT,
            json={"object_type": "semantic-concept", "intent": {}, "idempotency_key": "k"},
        )
    assert response.status_code == 404
    # Nothing about the Project, its Organization or its existence.
    assert response.json() == {"code": "not_found", "message": "Semantic Model object not found"}


def test_reading_a_change_set_needs_only_view():
    with _seam(capability="view", get_change_set=MagicMock(return_value=_change_set())) as access:
        response = _client().get(f"{ROOT}/{CHANGE_SET}")
    assert response.status_code == 200
    assert access.call_args.kwargs["minimum_capability"] == "view"


def test_publishing_needs_edit_and_a_viewer_is_denied():
    confirm = MagicMock()
    with _seam(decision=_denied(), confirm_change_set=confirm) as access:
        response = _client().post(
            f"{ROOT}/{CHANGE_SET}/confirm", json={"confirmation_token": "t"}
        )
    assert response.status_code == 403
    assert access.call_args.kwargs["minimum_capability"] == "edit"
    confirm.assert_not_called()


def test_an_unreadable_access_owner_is_unavailable_not_denied():
    with _seam(decision=_denied("access_unavailable"), get_change_set=MagicMock()):
        response = _client().get(f"{ROOT}/{CHANGE_SET}")
    assert response.status_code == 503
    assert response.json()["code"] == "semantic_model_access_unavailable"


# ---------------------------------------------------------------------------
# Prepare and confirm
# ---------------------------------------------------------------------------


def test_prepare_returns_the_single_use_token_once():
    prepared = {
        **_change_set(state="prepared").as_dict(),
        "confirmation_token": "one-time-secret",
        "confirmation_expires_at": "2026-07-30T09:15:00Z",
        "refusals": [],
    }
    with _seam(prepare_change_set=MagicMock(return_value=prepared)):
        response = _client().post(f"{ROOT}/{CHANGE_SET}/prepare", json={})
    assert response.status_code == 200
    assert response.json()["confirmation_token"] == "one-time-secret"


def test_confirming_without_a_token_is_refused_before_the_service():
    confirm = MagicMock()
    with _seam(confirm_change_set=confirm):
        response = _client().post(f"{ROOT}/{CHANGE_SET}/confirm", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "missing_confirmation"
    confirm.assert_not_called()


def test_a_drifted_dependency_is_409_and_nothing_is_published():
    stale = SemanticStale(
        "dependency_drift",
        "A Concept version this change set was prepared against has changed.",
    )
    with _seam(confirm_change_set=MagicMock(side_effect=stale)):
        response = _client().post(
            f"{ROOT}/{CHANGE_SET}/confirm", json={"confirmation_token": "t"}
        )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "dependency_drift"
    assert "changed" in body["message"]


def test_a_replayed_confirmation_is_refused_rather_than_applied_twice():
    used = SemanticStale("confirmation_already_used", "This confirmation was already consumed.")
    with _seam(confirm_change_set=MagicMock(side_effect=used)):
        response = _client().post(
            f"{ROOT}/{CHANGE_SET}/confirm", json={"confirmation_token": "t"}
        )
    assert response.status_code == 409
    assert response.json()["code"] == "confirmation_already_used"


def test_an_expired_confirmation_is_refused():
    expired = SemanticStale("confirmation_expired", "This confirmation expired.")
    with _seam(confirm_change_set=MagicMock(side_effect=expired)):
        response = _client().post(
            f"{ROOT}/{CHANGE_SET}/confirm", json={"confirmation_token": "t"}
        )
    assert response.status_code == 409
    assert response.json()["code"] == "confirmation_expired"


def test_an_unsafe_formula_refusal_carries_its_named_reasons():
    from core.semantic_expressions import Refusal

    refused = SemanticRefused(
        "not_publishable",
        "This change set did not pass preparation.",
        [
            Refusal("incompatible_currency", "Money in EUR cannot be added to money in USD.", "$"),
            Refusal("unsafe_sum", "Summing a ratio adds rates together.", "$.aggregation"),
        ],
    )
    with _seam(prepare_change_set=MagicMock(side_effect=refused)):
        response = _client().post(f"{ROOT}/{CHANGE_SET}/prepare", json={})
    assert response.status_code == 422
    codes = [entry["code"] for entry in response.json()["refusals"]]
    assert codes == ["incompatible_currency", "unsafe_sum"]


def test_a_missing_change_set_is_the_same_404_as_a_foreign_one():
    with _seam(get_change_set=MagicMock(side_effect=SemanticNotFound("nope"))):
        missing = _client().get(f"{ROOT}/{CHANGE_SET}")
    with _seam(decision=_denied("not_found"), get_change_set=MagicMock()):
        foreign = _client().get(f"{ROOT}/{CHANGE_SET}")
    assert missing.status_code == foreign.status_code == 404
    assert missing.json() == foreign.json()


def test_every_response_is_no_store():
    with _seam(get_change_set=MagicMock(return_value=_change_set())):
        response = _client().get(f"{ROOT}/{CHANGE_SET}")
    assert response.headers["cache-control"] == "no-store"


def test_an_unexpected_owner_failure_fails_closed_without_disclosing():
    with _seam(get_change_set=MagicMock(side_effect=RuntimeError("connection reset"))):
        response = _client().get(f"{ROOT}/{CHANGE_SET}")
    assert response.status_code == 503
    assert "connection reset" not in response.text
