"""Authoritative Project Overview derivation tests (Story 43.15)."""

from __future__ import annotations

from core.project_overview import (
    _ALERT_SIGNAL_LIMITATION,
    _ALERT_SIGNAL_NO_DESTINATION,
    _business_silence_explanation,
    _query_daily_insights,
    _query_latest_test,
    _query_project_evidence,
    _query_recent_alert_firings,
    _signal_items,
    compose_project_overview,
    derive_project_trust,
    rank_attention_items,
)
from core.project_readiness import compose_project_readiness_from_evidence


def _row(
    datastream_id: str,
    *,
    coverage_end_exclusive: str | None = "2026-07-23T00:00:00+00:00",
    latest_state: str = "done",
    latest_verdict: str | None = "ok",
    connection_status: str | None = "ok",
    published: bool = True,
    active_state: str | None = None,
    enabled: bool = True,
    coverage_state: str | None = "covered",
    coverage_execution_id: str | None = None,
    coverage_verdict: str | None = "ok",
    source_kind: str = "connector_pull",
) -> dict:
    current_execution_id = f"exec-{datastream_id}" if published else None
    return {
        "id": datastream_id,
        "name": f"Datastream {datastream_id}",
        "enabled": enabled,
        "source_kind": source_kind,
        "connection_ref_id": (f"conn-{datastream_id}" if source_kind == "connector_pull" else None),
        "connection_status": connection_status,
        "connection_health_current": True,
        "connection_ref_status": "active",
        "connection_enabled": True,
        "account_state": "ready",
        "account_available": True,
        "account_exposed": True,
        "current_published_execution_id": current_execution_id,
        "publication_state": "published" if published else None,
        "published_at": "2026-07-22T00:00:00Z" if published else None,
        "coverage_interval_start": "2026-07-01T00:00:00Z",
        "coverage_end_exclusive": coverage_end_exclusive,
        "verified_at": "2026-07-22T00:00:00Z",
        "latest_pull_state": latest_state,
        "latest_pull_completed_at": "2026-07-23T00:00:00Z",
        "coverage_state": coverage_state,
        "coverage_execution_id": (
            coverage_execution_id if coverage_execution_id is not None else current_execution_id
        ),
        "coverage_verdict": coverage_verdict,
        "latest_verdict": latest_verdict,
        "active_state": active_state,
    }


def test_trusted_complete_through_uses_oldest_required_verified_interval():
    summary, attention, active_work = derive_project_trust(
        [
            _row("one", coverage_end_exclusive="2026-07-23T00:00:00Z"),
            _row("two", coverage_end_exclusive="2026-07-20T00:00:00Z"),
        ]
    )

    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-19"
    assert summary["verified_datastreams"] == 2
    assert attention == []
    assert active_work == []


def test_failed_candidate_keeps_last_known_good_and_requires_attention():
    summary, attention, _active_work = derive_project_trust(
        [_row("search", latest_state="failed", latest_verdict=None)]
    )

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == [
        {
            "datastream_id": "search",
            "name": "Datastream search",
            "reason": "latest_run_failed",
            "detail": (
                "The latest run failed; the verified current publication remains available."
            ),
            "target": "runs",
        }
    ]


def test_missing_evidence_is_unknown_without_completion_date():
    summary, attention, _active_work = derive_project_trust(
        [
            _row(
                "unknown",
                coverage_end_exclusive=None,
                connection_status=None,
                published=False,
            )
        ]
    )

    assert summary["published_trust"] == "unknown"
    assert summary["complete_through"] is None
    assert summary["verified_datastreams"] == 0
    assert attention[0]["reason"] == "evidence_missing"
    assert "current published execution" in attention[0]["detail"]
    assert "connection-health evidence" in attention[0]["detail"]


def test_empty_project_is_not_trusted():
    summary, attention, active_work = derive_project_trust([])

    assert summary["published_trust"] == "no_data"
    assert summary["active_datastreams"] == 0
    assert summary["complete_through"] is None
    assert attention == []
    assert active_work == []
    assert summary["no_data_reason"] == "empty_project"


def test_only_disabled_datastreams_are_not_described_as_an_empty_project():
    summary, attention, active_work = derive_project_trust([_row("disabled", enabled=False)])

    assert summary["published_trust"] == "no_data"
    assert summary["no_data_reason"] == "no_active_datastreams"
    assert summary["evidence_message"].startswith("No Datastream is active")
    assert attention == []
    assert active_work == []


def test_active_work_is_reported_from_persisted_execution_state():
    summary, _attention, active_work = derive_project_trust(
        [_row("one", active_state="validating")]
    )

    assert summary["active_work_count"] == 1
    assert active_work == [
        {
            "datastream_id": "one",
            "name": "Datastream one",
            "state": "validating",
            "target": "runs",
        }
    ]


def test_old_coverage_cannot_verify_the_current_publication():
    summary, attention, _active_work = derive_project_trust(
        [_row("mismatch", coverage_execution_id="exec-previous")]
    )

    assert summary["published_trust"] == "unknown"
    assert summary["complete_through"] is None
    assert summary["verified_datastreams"] == 0
    assert attention[0]["reason"] == "evidence_missing"
    assert "current publication" in attention[0]["detail"]


def test_degraded_coverage_is_attention_without_a_completion_date():
    summary, attention, _active_work = derive_project_trust(
        [
            _row(
                "degraded",
                coverage_state="degraded",
                coverage_verdict=None,
            )
        ]
    )

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] is None
    assert {item["reason"] for item in attention} == {
        "coverage_incomplete",
        "evidence_missing",
    }


def test_non_connector_uses_source_agnostic_coverage_evidence():
    summary, attention, _active_work = derive_project_trust(
        [_row("warehouse", source_kind="external_bq", coverage_verdict=None)]
    )

    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == []


def test_exclusive_coverage_end_reports_previous_complete_day():
    summary, attention, _active_work = derive_project_trust(
        [_row("exclusive", coverage_end_exclusive="2026-07-23T00:00:00+00:00")]
    )
    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == []


def test_failed_coverage_candidate_preserves_current_publication_with_limitation():
    summary, attention, _active_work = derive_project_trust(
        [
            _row(
                "candidate",
                coverage_state="failed",
                coverage_execution_id="exec-candidate",
                coverage_end_exclusive=None,
                coverage_verdict=None,
                latest_state="failed",
                latest_verdict=None,
            )
        ]
    )

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] is None
    failure = next(item for item in attention if item["reason"] == "latest_run_failed")
    assert "current publication remains available" in failure["detail"]
    assert "coverage horizon cannot be verified" in failure["detail"]
    assert {item["target"] for item in attention} == {"runs"}


def test_failed_pull_older_than_current_publication_does_not_downgrade_trust():
    row = _row("ordered", latest_state="failed", latest_verdict=None)
    row["latest_pull_completed_at"] = "2026-07-20T00:00:00Z"
    row["published_at"] = "2026-07-22T00:00:00Z"

    summary, attention, _active_work = derive_project_trust([row])

    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == []


def test_stale_health_evidence_cannot_be_trusted():
    row = _row("stale-health")
    row["connection_health_current"] = False

    summary, attention, _active_work = derive_project_trust([row])

    assert summary["published_trust"] == "unknown"
    assert summary["complete_through"] is None
    assert attention[0]["target"] == "overview"
    assert "recent connection-health evidence" in attention[0]["detail"]


def test_revoked_account_keeps_published_horizon_but_requires_attention():
    row = _row("revoked")
    row["account_exposed"] = False

    summary, attention, _active_work = derive_project_trust([row])

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] == "2026-07-22"
    assert attention[0]["reason"] == "connection_unusable"
    assert attention[0]["target"] == "overview"


class _FakeTransaction:
    def __init__(self):
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.rolled_back = exc_type is not None
        return False


class _FakeCursor:
    def __init__(self, *, rows=None, fail=False, capture=None):
        self.rows = rows or []
        self.fail = fail
        self.capture = capture
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def execute(self, sql, params):
        if self.capture is not None:
            self.capture.append((sql, params))
        if self.fail:
            raise RuntimeError("optional relation unavailable")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, *, rows=None, fail=False, capture=None):
        self.rows = rows
        self.fail = fail
        self.capture = capture
        self.transactions = []

    def cursor(self):
        return _FakeCursor(rows=self.rows, fail=self.fail, capture=self.capture)

    def transaction(self):
        tx = _FakeTransaction()
        self.transactions.append(tx)
        return tx


def test_project_evidence_uses_ratified_project_flux_topology():
    captured = []
    _query_project_evidence("project-linked", _FakeConnection(capture=captured))

    sql, params = captured[0]
    assert "FROM app.project_flux pf" in sql
    assert "pf.project_id = %s" in sql
    assert "ds.project_id = %s" not in sql
    assert params[1] == "project-linked"


def test_the_module_offers_no_second_authorization_path():
    """`authorize_overview_project` and `build_project_overview` are GONE.

    They were tested and unreachable: the live route authorizes through
    `project_overview_api._strict_project_capability_allowed`, and the envelope is
    built by `compose_project_overview`. The old test here asserted, in detail, an
    authorization path no deployment ever walked -- a green result about nothing.

    Proving their ABSENCE is the useful assertion: a second entry point into this
    read model is exactly what let two authorization stories drift apart.
    """
    from core import project_overview

    assert not hasattr(project_overview, "authorize_overview_project")
    assert not hasattr(project_overview, "build_project_overview")
    # Story 46.4 removed the default-open helpers outright; they must stay gone.
    from core import project_access

    assert not hasattr(project_access, "identity_has_project_access")
    assert not hasattr(project_access, "resolve_project_role")


def test_optional_query_failure_rolls_back_its_savepoint():
    conn = _FakeConnection(fail=True)

    assert _query_latest_test("project-1", conn) == {
        "status": "unavailable",
        "value": None,
    }
    assert len(conn.transactions) == 1
    assert conn.transactions[0].rolled_back is True


def test_failed_latest_insight_run_does_not_render_stale_published_items():
    conn = _FakeConnection(rows=[("failed", None, None, None, None, None, "2026-07-21")])

    assert _query_daily_insights("project-1", conn) == {
        "status": "unavailable",
        "items": [],
        "silence": {
            "state": "failed",
            "insight_date": "2026-07-21",
            "explanation": "The daily insight run failed before it could report.",
            "reason": None,
        },
    }


# ---------------------------------------------------------------------------
# The silence of a daily-insight run is a STATE, and this surface says it
#
# `proactive-assertions.md` ("Silence is a state, and it is disclosed"): a
# surface that cannot speak says so. The publication path records WHY a run
# published nothing -- a `no_insight` declared on a blocked day is written as
# `blocked` with the server-measured reasons. Reading `status` and dropping
# `coverage` collapsed that back into "No persisted business signal is
# available", which reads as a quiet project rather than one nobody could look
# at.
# ---------------------------------------------------------------------------


def test_blocked_run_carries_its_measured_reason_out_of_the_query():
    conn = _FakeConnection(
        rows=[
            (
                "blocked",
                None,
                None,
                None,
                None,
                {"reason": "latest data 2026-07-19 is behind target 2026-07-21"},
                "2026-07-21",
            )
        ]
    )

    result = _query_daily_insights("project-1", conn)

    assert result["status"] == "unavailable"
    assert result["items"] == []
    assert result["silence"]["state"] == "blocked"
    assert "2026-07-19" in result["silence"]["explanation"]
    assert "not ready" in result["silence"]["explanation"]


def test_blocked_run_reason_survives_a_json_encoded_coverage_column():
    # `coverage` is JSONB; a driver that hands it back as text must not silently
    # cost the reader the reason.
    conn = _FakeConnection(
        rows=[("blocked", None, None, None, None, '{"reason": "gap in cost"}', "2026-07-21")]
    )

    assert "gap in cost" in _query_daily_insights("project-1", conn)["silence"]["explanation"]


def test_a_quiet_day_and_a_blocked_day_do_not_read_the_same():
    quiet = _FakeConnection(rows=[("no_insight", None, None, None, None, None, "2026-07-21")])
    blocked = _FakeConnection(
        rows=[("blocked", None, None, None, None, {"reason": "not complete"}, "2026-07-21")]
    )

    quiet_silence = _query_daily_insights("project-1", quiet)["silence"]
    blocked_silence = _query_daily_insights("project-1", blocked)["silence"]

    assert quiet_silence["explanation"] != blocked_silence["explanation"]
    assert "nothing worth reporting" in quiet_silence["explanation"]


# ---------------------------------------------------------------------------
# THE FIFTH STATE -- an absent run is not a quiet run
#
# `execution-substrate.md`, `Incomplete if` 11: "a unit of work scheduled in
# someone else's host is exempted from 2 because toorow does not own its clock --
# or, the opposite failure, its silence is read as a run that happened". The four
# recorded states are told apart by `_insight_silence`; the ABSENT row answered
# `empty`, which is the same word this function returns for `no_insight`. So a
# Project whose scheduled task was never installed read exactly like a Project
# whose task ran and found the day quiet.
# ---------------------------------------------------------------------------


def test_a_project_whose_task_never_ran_is_not_reported_as_a_quiet_day():
    never = _FakeConnection(rows=[])
    quiet = _FakeConnection(rows=[("no_insight", None, None, None, None, None, "2026-07-21")])

    never_read = _query_daily_insights("project-1", never)
    quiet_read = _query_daily_insights("project-1", quiet)

    # The STATUS itself has to differ: `empty` was carried by both.
    assert never_read["status"] == "never_ran"
    assert quiet_read["status"] == "empty"
    assert never_read["silence"]["state"] == "never_ran"
    assert (
        never_read["silence"]["explanation"] != quiet_read["silence"]["explanation"]
    )


def test_the_absent_run_names_the_gesture_that_repairs_it_and_infers_nothing():
    read = _query_daily_insights("project-1", _FakeConnection(rows=[]))
    silence = read["silence"]

    # The gesture is the recipe and the host's own schedule -- never a deployment,
    # an environment or a queue: toorow does not own this clock.
    assert "recipe" in silence["gesture"]
    assert "scheduled task" in silence["gesture"]
    for forbidden in ("deploy", "environment", "queue", "Cloud"):
        assert forbidden not in silence["explanation"]
    # Nothing is invented for a row that does not exist.
    assert silence["insight_date"] is None
    assert silence["reason"] is None


def test_the_empty_outcome_of_an_absent_run_reaches_the_posture_sentence():
    outcomes = _signal_items(
        _query_daily_insights("project-1", _FakeConnection(rows=[])),
        {"status": "empty", "items": []},
    )

    assert "scheduled task has not reported once" in (
        outcomes["insight_silence"]["explanation"]
    )


# ---------------------------------------------------------------------------
# A RETRACTED claim leaves the signals, and does NOT vanish
#
# `proactive-assertions.md` decision 4: a retraction is an audited state
# transition, never a delete. Overview is the surface that VOLUNTEERS claims, so
# a withdrawn one must stop being carried among them -- and dropping it silently
# would be the delete this rule refuses, one layer above the database.
# ---------------------------------------------------------------------------


def _retracted_row(title: str, *, insight_id: str, reason: str) -> tuple:
    return _published_insight_row(
        {"insight": {"title": title, "summary": "s", "confidence": "high"}},
        insight_id=insight_id,
        retracted_at="2026-07-23T11:00:00+00:00",
        retracted_by="owner@example.com",
        retracted_reason=reason,
    )


def test_a_retracted_insight_is_no_longer_asserted_but_is_still_reported():
    conn = _FakeConnection(
        rows=[_retracted_row("Spend spiked", insight_id="din-1", reason="wrong window")]
    )

    read = _query_daily_insights("project-1", conn)

    assert read["items"] == []
    assert [r["id"] for r in read["retractions"]] == ["din-1"]
    assert read["retractions"][0]["reason"] == "wrong window"
    assert read["retractions"][0]["retracted_by"] == "owner@example.com"
    # Not a quiet day, not a blocked day: a day that spoke and unsaid it.
    assert read["silence"]["state"] == "retracted"
    assert "wrong window" in read["silence"]["explanation"]


def test_a_standing_insight_and_a_withdrawn_one_are_kept_apart():
    payload = {"insight": {"title": "Cost drifted", "summary": "s", "confidence": "low"}}
    conn = _FakeConnection(
        rows=[
            _published_insight_row(payload, insight_id="din-live"),
            _retracted_row("Spend spiked", insight_id="din-gone", reason="misread"),
        ]
    )

    read = _query_daily_insights("project-1", conn)
    outcomes = _signal_items(read, {"status": "empty", "items": []})

    assert [item["id"] for item in read["items"]] == ["din-live"]
    assert [r["id"] for r in outcomes["insight_retractions"]] == ["din-gone"]
    # The withdrawal is NOT smuggled back among the signals with a label.
    assert all(item["id"] != "din-gone" for item in outcomes["items"])


# ---------------------------------------------------------------------------
# A projected CONFIDENCE names its author, or it is not projected
#
# `overview.md:277`: a surfaced signal must not state a cause, a comparison
# baseline or a confidence level that no server evidence backs.
# `insight.confidence` is the MODEL's own word -- its only check is enum
# membership -- and story 53.4 (CAV-07) made every published payload carry the
# `authorship` block that says so. This projection dropped the block and kept the
# word, so `outcomes.items[].confidence` reached every consumer of
# `GET /api/projects/{id}/overview` with nobody named as its author. Nothing
# attacked it, which is how it survived a "true" verdict.
# ---------------------------------------------------------------------------


def _published_insight_row(
    payload: dict,
    *,
    insight_id: str = "insight-1",
    retracted_at: str | None = None,
    retracted_by: str | None = None,
    retracted_reason: str | None = None,
) -> tuple:
    """One row of the latest-run join, in the column order the query selects.

    The three retraction columns arrived with migration 321; they are NULL on a
    standing claim, which is the only "still asserted" state there is.
    """
    return (
        "published",
        insight_id,
        payload,
        "2026-07-23T06:00:00+00:00",
        "snap-1",
        None,
        "2026-07-23",
        retracted_at,
        retracted_by,
        retracted_reason,
    )


def test_a_projected_confidence_travels_with_the_authorship_that_names_the_model():
    payload = {
        "insight": {
            "title": "Paid revenue recovered week over week",
            "summary": "Revenue is back above the prior week.",
            "confidence": "medium",
        },
        "evidenceRefs": ["metric:revenue"],
    }

    conn = _FakeConnection(rows=[_published_insight_row(payload)])
    item = _query_daily_insights("project-1", conn)["items"][0]

    # The top-level key is the READING -- this legacy payload predates the
    # derivation, so there is nothing measured to show and the projection says
    # so instead of borrowing the model's "medium" (re-review of ae60c22a: a
    # consumer reading only `confidence` must never receive an unlabelled
    # model-declared level).
    assert item["confidence"] == "unmeasurable"
    # Derived by the SAME function the Daily Insights routes call, so the two read
    # paths cannot drift on where the confidence a reader sees comes from.
    assert item["authorship"]["confidence"] == "derived"
    # The model's word is carried under its own name and decides nothing; this
    # legacy payload was published before the derivation, so there is no reading
    # to show and the surface says `unmeasurable` rather than borrowing "medium".
    assert item["authorship"]["declaredConfidence"] == "medium"
    assert item["authorship"]["derivedConfidence"] is None
    assert item["authorship"]["modelAuthored"] == ["insight.title", "insight.summary"]
    assert item["authorship"]["evidenceRefs"] == ["metric:revenue"]


def test_a_payload_that_persisted_its_own_authorship_is_projected_as_persisted():
    stored = {
        "modelAuthored": ["insight.title"],
        "confidence": "model_declared",
        "evidenceRefs": ["metric:cost"],
    }
    payload = {
        "insight": {"title": "Cost drifted", "summary": "Cost is above plan.", "confidence": "low"},
        "evidenceRefs": ["metric:cost"],
        "authorship": stored,
    }

    conn = _FakeConnection(rows=[_published_insight_row(payload)])
    item = _query_daily_insights("project-1", conn)["items"][0]

    # Rendered, never rewritten: the block the publication persisted is the block
    # the reader gets, even where deriving it would have listed one more field.
    assert item["authorship"] == stored


def test_no_signal_states_a_confidence_without_saying_who_declared_it():
    """The class guard: it holds for every item `_signal_items` emits, not one.

    This is the assertion that was missing on 2026-08-25 -- `grep confidence` on
    both Overview suites returned nothing, so the bare word could reach the
    envelope with every suite green.
    """
    payload = {
        "insight": {"title": "Spend spiked", "summary": "Spend is up.", "confidence": "high"},
        "evidenceRefs": ["metric:spend"],
    }
    conn = _FakeConnection(rows=[_published_insight_row(payload)])
    insights = _query_daily_insights("project-1", conn)

    outcomes = _signal_items(insights, {"status": "empty", "items": []})

    assert outcomes["items"], "the fixture must produce at least one signal to attack"
    for item in outcomes["items"]:
        if "confidence" not in item:
            continue
        authorship = item.get("authorship")
        assert isinstance(authorship, dict), (
            f"{item['kind']} states a confidence with no authorship"
        )
        assert authorship["confidence"] == "derived"


def test_business_posture_explains_the_silence_instead_of_only_stating_it():
    outcomes = _signal_items(
        {"status": "unavailable", "items": [], "silence": {"explanation": "Data was not ready."}},
        {"status": "empty", "items": []},
    )

    assert outcomes["items"] == []
    # Carried, never counted: a blocked run adds no signal and moves no state.
    assert outcomes["status"] != "ready"
    assert _business_silence_explanation(outcomes) == (
        "No persisted business signal is available. Data was not ready."
    )


def test_business_posture_says_only_the_fact_when_nothing_disclosed_a_reason():
    outcomes = _signal_items({"status": "empty", "items": []}, {"status": "empty", "items": []})

    assert _business_silence_explanation(outcomes) == (
        "No persisted business signal is available."
    )


def _owner(workspace: str, section: str, **extra) -> dict:
    return {
        "surface": "project",
        "workspace": workspace,
        "section": section,
        "global_surface": None,
        "global_section": None,
        "object_type": extra.get("object_type"),
        "object_id": extra.get("object_id"),
        "tab": extra.get("tab"),
        "action": extra.get("action"),
        "version_id": extra.get("version_id"),
        "evidence_id": extra.get("evidence_id"),
    }


def _projection(status: str, state: str, owner: dict, *, gaps=None) -> dict:
    return {
        "status": status,
        "state": state,
        "denominator": 1 if status == "ready" else 0,
        "complete": 1 if state == "ready" else 0,
        "gaps": gaps or [],
        "evidence_horizon": "2026-07-29" if status == "ready" else None,
        "owner": owner,
    }


def _settings(project_id: str = "project-1", *, editable: bool = True) -> dict:
    def capability(key: str, availability: str, state: str) -> dict:
        applicable = 1 if availability == "always_present" or state != "disabled" else 0
        return {
            "key": key,
            "availability": availability,
            "active": {"state": state, "version_id": f"{key}-v1" if state != "disabled" else None},
            "pending": None,
            "coverage": {
                "applicable": applicable,
                "complete": applicable,
                "partial": 0,
                "unavailable": 0,
                "excluded": 0,
                "pending": 0,
                "label": "100% complete" if applicable else "Not applicable",
                "percentage": 100.0 if applicable else None,
            },
            "exceptions": [],
            "blockers": [],
            "owner_links": [],
        }

    return {
        "project": {
            "id": project_id,
            "name": "Acme",
            "organization": {"id": "org-1", "name": "Acme Org"},
            "business_domains": [{"id": "domain-1", "name": "Commerce"}],
            "active_configuration_version_id": "cfg-3",
            "can_edit": editable,
        },
        "capabilities": [
            capability("country", "optional", "disabled"),
            capability("currency_fx", "always_present", "ready"),
            capability("reporting_timezone", "always_present", "ready"),
            capability("tax_fees", "optional", "disabled"),
            capability("competitors", "optional", "disabled"),
        ],
        "changes": [],
    }


def _patch_optional_projections(monkeypatch, project_overview) -> None:
    monkeypatch.setattr(
        project_overview,
        "_query_governance_projection",
        lambda *_args: _projection("ready", "ready", _owner("governance", "controls-quality")),
    )
    monkeypatch.setattr(
        project_overview,
        "_query_context_projection",
        lambda *_args: _projection(
            "empty",
            "unknown",
            _owner("context-hub", "knowledge-graph"),
            gaps=["No governed context evidence"],
        ),
    )
    monkeypatch.setattr(
        project_overview,
        "_query_test_projection",
        lambda *_args: _projection(
            "ready", "degraded", _owner("test", "regression-runs"), gaps=["One regression"]
        ),
    )
    monkeypatch.setattr(
        project_overview, "_query_daily_insights", lambda *_args: {"status": "empty", "items": []}
    )
    monkeypatch.setattr(
        project_overview, "_query_recent_renders", lambda *_args: {"status": "empty", "items": []}
    )
    # THE SHARED READINESS PATH. Overview no longer derives readiness from
    # `rows[0]` of its own fleet query; it calls the one selection in
    # `project_readiness`. Patching it here is what proves the call happens --
    # a composer that still derived its own would never touch this stub.
    monkeypatch.setattr(
        project_overview,
        "compose_project_readiness",
        lambda *_args: compose_project_readiness_from_evidence(
            active_configuration_version_id="cfg-3",
            source_id="conn-1",
            datastream_id="ds-1",
            first_value_id=None,
        ),
    )
    monkeypatch.setattr(
        project_overview, "_query_recent_changes", lambda *_args: {"status": "empty", "items": []}
    )
    monkeypatch.setattr(
        project_overview,
        "_query_recent_alert_firings",
        lambda *_args: {"status": "empty", "items": []},
    )


def test_attention_ranking_deduplicates_root_cause_and_keeps_exact_owner():
    owner = _owner(
        "data",
        "datastreams",
        object_type="datastream",
        object_id="ds-1",
        tab="runs",
        evidence_id="ev-1",
    )
    common = {
        "root_cause_key": "run:failed:ds-1",
        "cause": "The latest run failed.",
        "status": "blocked",
        "evidence_horizon": "2026-07-28",
        "owner": owner,
        "action": {"label": "Open Runs", "permitted": True},
    }
    items = rank_attention_items(
        [
            {
                **common,
                "id": "att-2",
                "priority_class": 2,
                "impact": "Publication is delayed.",
                "scope": ["Datastream ds-1"],
                "first_observed_at": "2026-07-28T09:00:00Z",
                "last_observed_at": "2026-07-29T09:00:00Z",
            },
            {
                **common,
                "id": "att-1",
                "priority_class": 1,
                "impact": "Trust is limited.",
                "scope": ["Project trust"],
                "first_observed_at": "2026-07-28T08:00:00Z",
                "last_observed_at": "2026-07-29T10:00:00Z",
            },
        ]
    )

    assert len(items) == 1
    assert items[0]["priority_class"] == 1
    assert items[0]["impact"] == ["Publication is delayed.", "Trust is limited."]
    assert items[0]["scope"] == ["Datastream ds-1", "Project trust"]
    assert items[0]["owner"] == owner


def test_transverse_composer_uses_settings_and_separates_posture(monkeypatch):
    from core import project_overview

    monkeypatch.setattr(
        project_overview, "_read_project_settings_projection", lambda *_args, **_kwargs: _settings()
    )
    monkeypatch.setattr(project_overview, "_query_project_evidence", lambda *_args: [_row("one")])
    _patch_optional_projections(monkeypatch, project_overview)

    result = compose_project_overview("project-1", object(), actor="person-1")

    assert result["schema_version"] == "project-overview.v1"
    assert result["project"]["active_configuration_version_id"] == "cfg-3"
    assert set(result["posture"]) == {
        "operational_health",
        "trust_readiness",
        "business_signals",
        "limiting_dimension",
    }
    capability_keys = [item["key"] for item in result["coverage"] if item["kind"] == "capability"]
    assert capability_keys == ["currency_fx", "reporting_timezone"]
    assert result["coverage"][0]["kind"] == "data"
    assert result["next_action"]["owner"]["section"] == "regression-runs"


def test_empty_project_next_action_is_the_single_data_owned_wizard(monkeypatch):
    from core import project_overview

    settings = _settings("project-empty")
    settings["project"]["active_configuration_version_id"] = None
    settings["project"]["business_domains"] = []
    settings["capabilities"] = []
    monkeypatch.setattr(
        project_overview, "_read_project_settings_projection", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(project_overview, "_query_project_evidence", lambda *_args: [])
    _patch_optional_projections(monkeypatch, project_overview)

    result = compose_project_overview("project-empty", object(), actor="person-1")

    assert result["next_action"]["label"] == "Add Datastream"
    # The one Data-owned Wizard entry is an action on the Datastreams COLLECTION.
    # It used to name an object id of "new", which asserted an object that does
    # not exist in a route model whose identifiers are opaque.
    assert result["next_action"]["owner"] == _owner("data", "datastreams", action="create")
    assert result["next_action"]["owner"]["object_id"] is None
    assert result["posture"]["operational_health"]["state"] == "unknown"


# ---------------------------------------------------------------------------
# Governed alert firings as business signals (AI-190).
#
# `overview.md:57` counts a governed alert among the evidence a business signal
# is made of, and the zone read none. Media-plan pacing is what made the gap
# measurable: its console screen went away with `ui/admin/src/mediaplans/`, so
# the firing is the only thing of a plan's pace a person can still reach outside
# the MCP App.
# ---------------------------------------------------------------------------


def _firing_row(
    firing_id: str = "fire_1",
    *,
    alert_type: str = "mediaplan_pace",
    metric: str = "mediaplan_pace_overrun",
    severity: str = "error",
    message: str | None = "Budget overrun on line Display FR | {\"plan_id\": \"plan-1\"}",
    observed_value: float | None = 0.31,
    threshold: float | None = 0.10,
    datastream_id: str | None = None,
) -> tuple:
    return (
        firing_id,
        alert_type,
        metric,
        severity,
        message,
        observed_value,
        threshold,
        "2026-08-03",
        "2026-08-04T06:00:00+00:00",
        datastream_id,
    )


def test_media_plan_pace_firing_reaches_the_business_signal_zone():
    alerts = _query_recent_alert_firings("project-1", _FakeConnection(rows=[_firing_row()]))

    assert alerts["status"] == "ready"
    signals = _signal_items(
        {"status": "empty", "items": []},
        {"status": "empty", "items": []},
        alerts,
    )
    item = signals["items"][0]

    assert signals["status"] == "ready"
    assert item["kind"] == "governed_alert"
    assert item["provenance"] == {"kind": "alert_firing:mediaplan_pace", "id": "fire_1"}
    assert item["period"] == "2026-08-03"
    assert item["freshness"] == "2026-08-04T06:00:00+00:00"
    assert item["observed_value"] == 0.31
    assert item["threshold"] == 0.10
    # The item keeps its caveat -- one observation against one threshold -- AND it
    # opens: `overview.md` (amendment of 2026-08-21) sends a plan-pace firing to
    # the `pacing` lens of Analyze > Reports, the same reference the Datastream
    # Workbench composes.
    assert item["limitations"] == [_ALERT_SIGNAL_LIMITATION]
    assert item["owner"]["surface"] == "project"
    assert item["owner"]["workspace"] == "analyze"
    assert item["owner"]["section"] == "reports"
    assert item["owner"]["lens"] == "pacing"


def test_quality_firing_opens_the_datastream_runs_evidence_it_accuses():
    """Gap 5 of the audit of 2026-08-17, and the branch production actually walks.

    Measured under the owner DSN on 2026-08-21: every project-scoped firing of the
    last seven days is `dq_timeliness`, and all 84 carry a `datastream_id`.
    """
    alerts = _query_recent_alert_firings(
        "project-1",
        _FakeConnection(
            rows=[
                _firing_row(
                    alert_type="dq_timeliness",
                    metric="arrival_delay_hours",
                    message="Feed arrived 9h late",
                    datastream_id="ds_01EXAMPLE",
                )
            ]
        ),
    )

    assert alerts["items"][0]["datastream_id"] == "ds_01EXAMPLE"
    item = _signal_items(
        {"status": "empty", "items": []}, {"status": "empty", "items": []}, alerts
    )["items"][0]

    assert item["owner"]["workspace"] == "data"
    assert item["owner"]["section"] == "datastreams"
    assert item["owner"]["object_type"] == "datastream"
    assert item["owner"]["object_id"] == "ds_01EXAMPLE"
    # `runs` is the `datastream` contract's declared evidence tab, and a lateness
    # is a fact about what arrived and when.
    assert item["owner"]["tab"] == "runs"
    assert item["limitations"] == [_ALERT_SIGNAL_LIMITATION]


def test_firing_with_no_destination_says_what_is_missing_instead_of_going_silent():
    """A Project-scoped finding has no Datastream and no list that contains it.

    Pointing it at the Data Quality collection would open a list of
    `app.dq_monitors` rows -- all Datastream-scoped -- without this finding in it.
    """
    alerts = _query_recent_alert_firings(
        "project-1",
        _FakeConnection(
            rows=[
                _firing_row(
                    alert_type="dq_geography",
                    metric="unresolved_geography_rate",
                    message="Unresolved geography above threshold",
                    datastream_id=None,
                )
            ]
        ),
    )
    item = _signal_items(
        {"status": "empty", "items": []}, {"status": "empty", "items": []}, alerts
    )["items"][0]

    # ABSENT, not null: the screen draws the button on the key's presence, so a
    # null owner would render a control that opens nothing.
    assert "owner" not in item
    assert item["limitations"] == [_ALERT_SIGNAL_LIMITATION, _ALERT_SIGNAL_NO_DESTINATION]


def test_alert_firings_are_read_by_project_not_by_one_family():
    captured: list = []
    _query_recent_alert_firings("project-7", _FakeConnection(capture=captured))

    sql, params = captured[0]

    # Filtering on `type` would repair one family and leave the other two -- and
    # the next writer would need an edit here to be seen at all.
    assert "type = " not in sql
    assert "project_id = %s" in sql
    assert params[0] == "project-7"
    assert params[1] == 168
    assert params[2] == 5


def test_unreachable_alert_table_is_unavailable_never_nothing_fired():
    alerts = _query_recent_alert_firings("project-1", _FakeConnection(fail=True))

    assert alerts == {"status": "unavailable", "items": []}
    signals = _signal_items(
        {"status": "empty", "items": []},
        {"status": "empty", "items": []},
        alerts,
    )
    assert signals["status"] == "unavailable"


def test_firing_metadata_blob_is_never_shown_as_the_title():
    alerts = _query_recent_alert_firings("project-1", _FakeConnection(rows=[_firing_row()]))

    assert alerts["items"][0]["title"] == "Budget overrun on line Display FR"


def test_firing_without_message_falls_back_to_its_metric_not_to_a_blank_label():
    alerts = _query_recent_alert_firings(
        "project-1", _FakeConnection(rows=[_firing_row(message=None)])
    )

    assert alerts["items"][0]["title"] == "mediaplan_pace_overrun"
