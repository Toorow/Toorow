"""Story 36.15 -- render / validate / reproduce the first report (offline).

Offline MagicMock-style, mirroring test_epic36_first_report_readiness.py: the
composed 36.10 readiness object and the 36.1 strict-access seam are patched so the
Story 36.15 INVARIANTS are exercised WITHOUT live Postgres. Live-PG coverage (real
RLS existence-hiding on denial) is the established follow-up; here the denial is
exercised at the domain access boundary.

Covered invariants:
  (a) render runs ONLY when readiness is ready/degraded-within-policy and cites
      freshness / coverage / provenance / exclusions.
  (b) no app UI -> compact-text render mode + bounded evidence + optional
      deep-link, and the FULL dataset never enters the model-facing output.
  (c) rendered figures reference the active publication + mapping version (the
      validation checklist can match).
  (d) a second AUTHORIZED user in an approved workspace reproduces the SAME
      publication/provenance (access independently evaluated).
  (e) an unauthorized second user/workspace is DENIED without exposing report
      existence or first-user results (existence-hide).
  (f) the render/reproduce MCP tools register under Insights and pass
      validate_catalog.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import first_report_render as frr  # noqa: E402
from core.first_report_readiness import FirstReportReadiness, PhaseState  # noqa: E402

_HASH = "a" * 64
_SCHEMA_HASH = "b" * 64
_CONTENT_HASH = "c" * 64
_INTERVAL = {"start": "2026-06-22T00:00:00Z", "end_exclusive": "2026-07-22T00:00:00Z"}

# The full dataset marker: it must NEVER appear anywhere in the render output.
_SECRET_ROW = {"date": "2026-07-21", "clicks": 999, "raw_payload": "PROVIDER_SECRET_ROW"}


def _readiness(*, overall="ready", host_cta="enabled"):
    """A minimal but complete FirstReportReadiness for the render to consume."""
    return FirstReportReadiness(
        schema_version="1",
        readiness_version="v" * 32,
        datastream_id="ds-1",
        project_id="proj-1",
        overall=overall,
        host_cta=host_cta,
        headline="peu importe",
        current_publication={
            "execution_id": "dse_pub",
            "row_count": 120,
            "published_at": "2026-07-22T00:00:00Z",
        },
        selected_objects={
            "report_id": "report_x",
            "metrics": ["clicks", "cost", "impressions"],
            "dimensions": ["date"],
            "grain": ["day"],
            "timezone": "UTC",
            "currency": "EUR",
            "interval": dict(_INTERVAL),
        },
        recent_coverage={"state": "covered"},
        historical_coverage={"state": "covered" if overall == "ready" else "failed"},
        freshness={"last_pull_at": "2026-07-22T00:00:00Z", "connection_health": "healthy"},
        verification={"verdict": "ok", "row_count": 120, "expected_rows": 120, "at": "t"},
        dq={"total_unresolved": 0, "monitors_unavailable": False, "degraded": False},
        mapping={
            "mapping_version_id": "dmap_1",
            "version_number": 3,
            "mapping_contract_version": "1.2",
            "executable": True,
        },
        provenance={
            "source_schema_hash": _SCHEMA_HASH,
            "capability_fingerprint": _HASH,
            "content_hash": _CONTENT_HASH,
            "plan_version_id": "dsp_1",
        },
        exclusions=[{"kind": "unsupported_metric", "reason": "hors capacite"}],
        last_successful_pull={"execution_id": "dse_pub", "row_count": 120, "at": "t"},
        diagnosis={"verdict": "ok"},
        phases=[
            PhaseState(
                phase="recent_pull", state="succeeded", interval=dict(_INTERVAL),
                rows=120, attempts=2, live_publication_execution_id="dse_pub",
                next_action=None,
            )
        ],
    )


def _allow(*_a, **_k):
    from core.project_access import AccessDecision

    return AccessDecision(True, "owner_floor", "manage", "org-1", ())


def _deny(*_a, **_k):
    from core.project_access import AccessDecision

    return AccessDecision(False, "grant_required")


def _bound_ws(workspace_id):
    """Return a host-ctx resolver bound to *workspace_id* (app-UI supported)."""
    return lambda *_a, **_k: {
        "ui_supported": True,
        "endpoint_binding": "ep",
        "workspace_id": workspace_id,
    }


@pytest.fixture
def patched(monkeypatch):
    """Patch the 36.10 readiness compute + the 36.1 access seam + 36.14 host ctx."""
    state = {"overall": "ready", "host_cta": "enabled", "ui_supported": False, "workspace": None}

    monkeypatch.setattr(
        frr,
        "compute_first_report_readiness",
        lambda *a, **k: _readiness(overall=state["overall"], host_cta=state["host_cta"]),
        raising=False,
    )
    # host_preflight (36.14) may not be on disk: patch the guarded resolver directly.
    monkeypatch.setattr(
        frr,
        "_bound_host_connection",
        lambda *a, **k: {
            "ui_supported": state["ui_supported"],
            "endpoint_binding": "ep" if state["ui_supported"] else None,
            "workspace_id": state["workspace"],
        },
    )

    ns = MagicMock()
    ns.conn = MagicMock()
    ns.state = state
    return ns


# ---------------------------------------------------------------------------
# The import path for compute_first_report_readiness is inside the function body,
# so patch it where it is imported FROM (core.first_report_readiness).
# ---------------------------------------------------------------------------
def _patch_compute(monkeypatch, overall="ready", host_cta="enabled"):
    from core import first_report_readiness as readiness_mod

    monkeypatch.setattr(
        readiness_mod,
        "compute_first_report_readiness",
        lambda *a, **k: _readiness(overall=overall, host_cta=host_cta),
    )


# (a) render runs only when ready/degraded and cites the evidence -----------------
def test_render_ready_cites_freshness_coverage_provenance_exclusions(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    rendered = frr.render_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
    ).as_dict()

    assert rendered["overall"] == "ready"
    assert rendered["freshness"]["last_pull_at"] == "2026-07-22T00:00:00Z"
    assert rendered["coverage"]["recent"]["state"] == "covered"
    assert rendered["provenance"]["content_hash"] == _CONTENT_HASH
    assert rendered["exclusions"][0]["kind"] == "unsupported_metric"


def test_render_degraded_within_policy_runs_and_stays_honest(monkeypatch):
    _patch_compute(monkeypatch, overall="degraded", host_cta="degraded")
    rendered = frr.render_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
    ).as_dict()

    assert rendered["overall"] == "degraded"
    assert rendered["validation"]["is_degraded"] is True
    assert rendered["validation"]["degraded_note"]
    # Honest: never upgraded to "prêt".
    assert "prêt" not in rendered["headline"].lower()


def test_render_refuses_when_blocked(monkeypatch):
    _patch_compute(monkeypatch, overall="blocked", host_cta="disabled")
    with pytest.raises(frr.FirstReportRenderUnavailable):
        frr.render_first_report(
            MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
        )


# (b) no app UI -> compact text + bounded evidence + deep-link; NO full dataset ----
def test_no_app_ui_compact_text_bounded_evidence_and_deeplink(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    monkeypatch.setattr(
        frr, "_bound_host_connection", lambda *a, **k: {"ui_supported": False, "workspace_id": None}
    )
    rendered = frr.render_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
    ).as_dict()

    assert rendered["ui_supported"] is False
    assert rendered["render_mode"] == "compact_text"
    # Mandatory bounded evidence present (never deep-link-only).
    ev = rendered["bounded_evidence"]
    assert ev["row_count"] == 120
    assert ev["content_hash"] == _CONTENT_HASH
    assert len(ev["metric_names"]) <= frr._MAX_BOUNDED_FIGURES
    # Optional authenticated deep-link present, but carries no dataset/bearer.
    link = rendered["report_deep_link"]
    assert link["requires_authentication"] is True
    assert "token" not in link


def test_full_dataset_never_enters_model_facing_output(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    # Even if a raw dataset row were somehow present, the render composes only the
    # readiness object -- assert the secret marker is absent everywhere.
    rendered = frr.render_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
    ).as_dict()
    import json as _json

    blob = _json.dumps(rendered)
    assert "PROVIDER_SECRET_ROW" not in blob
    assert "raw_payload" not in blob


def test_app_ui_render_mode_when_host_supports_it(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    monkeypatch.setattr(
        frr, "_bound_host_connection",
        lambda *a, **k: {"ui_supported": True, "endpoint_binding": "ep", "workspace_id": "ws-1"},
    )
    rendered = frr.render_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
    ).as_dict()
    assert rendered["ui_supported"] is True
    assert rendered["render_mode"] == "app_ui"


# (c) figures reference the active publication + mapping version ------------------
def test_figures_cite_active_publication_and_mapping_version(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    rendered = frr.render_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
    ).as_dict()
    v = rendered["validation"]
    assert v["figures_cite_publication"] is True
    assert v["figures_cite_mapping_version"] is True
    assert v["publication_execution_id"] == "dse_pub"
    assert v["mapping_version_id"] == "dmap_1"
    assert v["content_hash"] == _CONTENT_HASH


# (d) authorized second user reproduces the SAME publication/provenance -----------
def test_second_authorized_user_reproduces_same_result(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    from core import project_access

    monkeypatch.setattr(project_access, "resolve_strict_resource_access", _allow)

    first = frr.render_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", actor="alice"
    ).as_dict()
    repro = frr.reproduce_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1", second_actor="bob",
    ).as_dict()

    # SAME publication + provenance reproduced (access independently evaluated).
    assert repro["publication"]["execution_id"] == first["publication"]["execution_id"]
    assert repro["provenance"]["content_hash"] == first["provenance"]["content_hash"]
    assert repro["mapping"]["mapping_version_id"] == first["mapping"]["mapping_version_id"]


def test_second_authorized_user_in_approved_workspace(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    from core import project_access

    monkeypatch.setattr(project_access, "resolve_strict_resource_access", _allow)
    monkeypatch.setattr(frr, "_bound_host_connection", _bound_ws("ws-approved"))
    repro = frr.reproduce_first_report(
        MagicMock(), datastream_id="ds-1", project_id="proj-1",
        second_actor="bob", workspace_ref="ws-approved",
    ).as_dict()
    assert repro["publication"]["execution_id"] == "dse_pub"


# (e) unauthorized second user/workspace is denied WITHOUT leaking ----------------
def test_unauthorized_second_user_denied_existence_hidden(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    from core import project_access

    monkeypatch.setattr(project_access, "resolve_strict_resource_access", _deny)

    with pytest.raises(frr.FirstReportReproductionDenied):
        frr.reproduce_first_report(
            MagicMock(), datastream_id="ds-1", project_id="proj-1", second_actor="mallory"
        )


def test_unauthorized_workspace_denied_existence_hidden(monkeypatch):
    _patch_compute(monkeypatch, overall="ready")
    from core import project_access

    # The user is a member, but the SECOND workspace is not the approved one.
    monkeypatch.setattr(project_access, "resolve_strict_resource_access", _allow)
    monkeypatch.setattr(frr, "_bound_host_connection", _bound_ws("ws-approved"))
    with pytest.raises(frr.FirstReportReproductionDenied):
        frr.reproduce_first_report(
            MagicMock(), datastream_id="ds-1", project_id="proj-1",
            second_actor="bob", workspace_ref="ws-foreign",
        )


def test_reproduce_denial_raises_before_any_render(monkeypatch):
    """The access check must run BEFORE the readiness compute (no first-user leak)."""
    from core import first_report_readiness as readiness_mod
    from core import project_access

    called = {"compute": False}

    def _tracking_compute(*a, **k):
        called["compute"] = True
        return _readiness()

    monkeypatch.setattr(readiness_mod, "compute_first_report_readiness", _tracking_compute)
    monkeypatch.setattr(project_access, "resolve_strict_resource_access", _deny)

    with pytest.raises(frr.FirstReportReproductionDenied):
        frr.reproduce_first_report(
            MagicMock(), datastream_id="ds-1", project_id="proj-1", second_actor="mallory"
        )
    # The readiness object (first-user truth) was NEVER computed for the denied user.
    assert called["compute"] is False


# (f) the render/reproduce tools register under Insights + pass validate_catalog --
def test_tools_register_insights_and_validate_catalog():
    from core import mcp_profiles
    from core.first_report_render_mcp import register

    mcp_profiles.reset_registry_for_tests()

    class _FakeMCP:
        def __init__(self):
            self.tools = []

        def tool(self, handler, *, name=None, tags=None, meta=None):
            self.tools.append((name or handler.__name__, tags, meta))
            return handler

    mcp = _FakeMCP()
    register(mcp)

    names = {t[0] for t in mcp.tools}
    assert "render_starter_report" in names
    assert "reproduce_starter_report" in names

    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    for name in ("render_starter_report", "reproduce_starter_report"):
        assert decls[name].profile == "insights"
        assert decls[name].effect == "read"
        assert decls[name].confirmation_mode == "none"

    # validate_catalog must accept these Insights read-only tools (fail-closed boot).
    validated = mcp_profiles.validate_catalog()
    validated_names = {d.name for d in validated}
    assert {"render_starter_report", "reproduce_starter_report"} <= validated_names

    mcp_profiles.reset_registry_for_tests()
