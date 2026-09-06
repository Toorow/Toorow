"""Story 49.4: the control lifecycle, proved against a live PostgreSQL.

The properties here are database properties -- a partial unique index, a CHECK
constraint, an append-only trigger -- so a mocked test would prove nothing about
them. Gated on `TEST_POSTGRES_DSN`, and it refuses to run against anything that
looks like production, because Story 48.3's own memory of this repository is that
a test suite once wrote 268 rows into it.

What each block guards, in one line:

* recurrence never overwrites history, and the rule is one documented sentence;
* a decision whose owner command FAILED does not advance the case;
* a DQ pass over zero eligible members is unstorable;
* a baseline never advances itself;
* every append-only record refuses UPDATE and DELETE.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest


@pytest.fixture()
def conn(live_postgres):
    """The repository's own gated connection, not a second one.

    `server/tests/conftest.py` already refuses a DSN that is not demonstrably
    disposable, and it FAILS rather than skips when a test that would write is
    pointed at a real database -- a skip there would read as "the pg gate ran".
    Re-implementing that guard here would have been a second copy of the one rule
    this repository has already been burned by getting wrong.
    """
    return live_postgres


@pytest.fixture()
def scope(conn):
    """An organization and a project that exist only for this test, rolled back."""
    from ulid import ULID

    org_id = f"org_{ULID()}"
    project_id = f"proj_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, "Controls test org", org_id.lower(), "controls-test"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (project_id, org_id, "Controls test project", project_id.lower(), "controls-test"),
        )
    yield {"org_id": org_id, "project_id": project_id}
    conn.rollback()


# ---------------------------------------------------------------------------
# Control Case recurrence.
# ---------------------------------------------------------------------------


def _observe(conn, scope, *, detail="a", severity="degrading", discriminators=None):
    from core.control_cases import CaseObservation, observe

    return observe(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        case_type="mapping_conflict",
        subject_kind="datastream",
        subject_id="ds_subject",
        severity=severity,
        discriminators=discriminators or {"field": "revenue"},
        observation=CaseObservation(detected_by="test", observation={"detail": detail}),
    )


def test_the_same_root_cause_appends_and_never_forks(conn, scope):
    first = _observe(conn, scope, detail="run-1")
    second = _observe(conn, scope, detail="run-2")

    assert first["recurrence"] == "opened"
    assert second["recurrence"] == "appended"
    assert second["id"] == first["id"], "the same root cause must reach the same case"

    from core.control_cases import case_history

    history = case_history(conn, project_id=scope["project_id"], case_id=first["id"])
    assert len(history["occurrences"]) == 2, "both sightings are retained"


def test_an_identical_observation_is_replayed_not_duplicated(conn, scope):
    first = _observe(conn, scope, detail="same")
    replay = _observe(conn, scope, detail="same")
    assert replay["recurrence"] == "replayed"
    assert replay["occurrence_id"] == first["occurrence_id"]


def test_a_different_root_cause_is_a_different_case(conn, scope):
    first = _observe(conn, scope, discriminators={"field": "revenue"})
    other = _observe(conn, scope, discriminators={"field": "cost"})
    assert other["id"] != first["id"]


def test_a_closed_case_that_recurs_opens_a_linked_successor(conn, scope):
    first = _observe(conn, scope, detail="before")
    with conn.cursor() as cur:
        cur.execute("UPDATE app.control_cases SET status = 'closed' WHERE id = %s", (first["id"],))

    again = _observe(conn, scope, detail="after")
    assert again["id"] != first["id"], "a closed case is history, not a slot to reuse"
    assert again["supersedes_case_id"] == first["id"], "the return must be readable as a return"


def test_two_open_cases_for_one_root_cause_are_unstorable(conn, scope):
    """The recurrence rule is an index, not a convention."""
    import psycopg

    first = _observe(conn, scope)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.control_cases
                        (id, org_id, project_id, case_type, subject_kind, subject_id,
                         root_cause_fingerprint, severity, created_by)
                    SELECT 'ctc_01ARZ3NDEKTSV4RRFFQ69G5FA0', org_id, project_id, case_type,
                           subject_kind, subject_id, root_cause_fingerprint, severity, 'test'
                    FROM app.control_cases WHERE id = %s
                    """,
                    (first["id"],),
                )


# ---------------------------------------------------------------------------
# Decisions.
# ---------------------------------------------------------------------------


def test_a_failed_owner_handoff_does_not_advance_the_case(conn, scope):
    from core.control_cases import decide, fetch_case

    case = _observe(conn, scope)
    decide(
        conn,
        project_id=scope["project_id"],
        case_id=case["id"],
        decision_kind="approve_mapping",
        actor="operator@example.com",
        reason="Approved, but the Data owner command failed.",
        effective_from=date.today(),
        owner_outcome="failed",
        owner_result={"error": "publication refused"},
    )
    after = fetch_case(conn, project_id=scope["project_id"], case_id=case["id"])
    assert after["status"] == "open", (
        "a case that looks decided while the owner command failed is how a problem "
        "gets closed without being fixed"
    )


def test_a_successful_decision_advances_and_is_retained(conn, scope):
    from core.control_cases import case_history, decide, fetch_case

    case = _observe(conn, scope)
    decide(
        conn,
        project_id=scope["project_id"],
        case_id=case["id"],
        decision_kind="approve_mapping",
        actor="operator@example.com",
        reason="Reviewed against the published mapping version.",
        effective_from=date.today(),
        owner_outcome="succeeded",
    )
    decided = fetch_case(conn, project_id=scope["project_id"], case_id=case["id"])
    assert decided["status"] == "decided"
    history = case_history(conn, project_id=scope["project_id"], case_id=case["id"])
    assert len(history["decisions"]) == 1
    assert history["decisions"][0]["reason"]


def test_decisions_and_occurrences_refuse_update_and_delete(conn, scope):
    import psycopg
    from core.control_cases import decide

    case = _observe(conn, scope)
    decision_id = decide(
        conn,
        project_id=scope["project_id"],
        case_id=case["id"],
        decision_kind="exclude",
        actor="operator@example.com",
        reason="Excluded for this period.",
        effective_from=date.today(),
    )
    for sql, params in (
        ("UPDATE app.control_case_decisions SET reason = 'edited' WHERE id = %s", (decision_id,)),
        ("DELETE FROM app.control_case_decisions WHERE id = %s", (decision_id,)),
        (
            "UPDATE app.control_case_occurrences SET detected_by = 'x' WHERE id = %s",
            (case["occurrence_id"],),
        ),
    ):
        # A SAVEPOINT, not a rollback. `conn.rollback()` here discarded the case
        # this test had just created, so the third statement matched zero rows and
        # the trigger never fired -- a green assertion about nothing.
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="append-only"):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql, params)


# ---------------------------------------------------------------------------
# DQ monitors and evaluations.
# ---------------------------------------------------------------------------


@pytest.fixture()
def monitor(conn, scope):
    from core.dq_governance import ensure_monitor, publish_version

    head = ensure_monitor(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name="schema_drift",
        label="Schema drift",
        target_kind="datastream",
        target_id="ds_subject",
        actor="test",
    )
    version = publish_version(
        conn,
        project_id=scope["project_id"],
        monitor_id=head["id"],
        check_profile="schema",
        severity="degrading",
        baseline={"columns": ["date", "revenue"]},
        actor="test",
    )
    return {"monitor_id": head["id"], "version_id": version["id"]}


def test_a_pass_over_zero_eligible_members_is_refused(conn, scope, monitor):
    from core.dq_governance import DqGovernanceError, EvaluationCounts, record_evaluation

    with pytest.raises(DqGovernanceError, match="not a pass"):
        record_evaluation(
            conn,
            project_id=scope["project_id"],
            monitor_id=monitor["monitor_id"],
            monitor_version_id=monitor["version_id"],
            outcome="pass",
            window_start=date.today() - timedelta(days=1),
            window_end=date.today(),
            counts=EvaluationCounts(total_eligible=0),
            dependency_refs={},
        )


def test_the_database_refuses_an_empty_pass_even_without_the_service(conn, scope, monitor):
    """The Python check is the message; the constraint is the guarantee."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.dq_evaluations
                        (id, project_id, monitor_id, monitor_version_id, outcome,
                         window_start, window_end, dependency_fingerprint, total_eligible,
                         evaluator_version, content_hash)
                    VALUES ('dqe_01ARZ3NDEKTSV4RRFFQ69G5FA0', %s, %s, %s, 'pass',
                            CURRENT_DATE, CURRENT_DATE, %s, 0, 'test', %s)
                    """,
                    (
                        scope["project_id"],
                        monitor["monitor_id"],
                        monitor["version_id"],
                        "0" * 64,
                        "1" * 64,
                    ),
                )


def test_an_unverifiable_evaluation_is_unavailable_never_healthy(conn, scope, monitor):
    from core.dq_governance import EvaluationCounts, record_evaluation, runtime_state_for

    record_evaluation(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        monitor_version_id=monitor["version_id"],
        outcome="unverifiable",
        window_start=date.today(),
        window_end=date.today(),
        counts=EvaluationCounts(total_eligible=0),
        dependency_refs={"reason": "target not published"},
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT runtime_state FROM app.dq_monitors WHERE id = %s", (monitor["monitor_id"],)
        )
        assert cur.fetchone()[0] == "unavailable"
    assert runtime_state_for("error") == "unavailable"
    assert runtime_state_for("pass") == "healthy"
    assert runtime_state_for("fail") == "failing"


def test_a_baseline_change_is_proposed_and_never_applied(conn, scope, monitor):
    """The replacement for `_write_dq_baseline`, which overwrote the baseline after
    a drift so the next run agreed with the drift."""
    from core.dq_governance import propose_baseline_change

    proposal = propose_baseline_change(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        observed_baseline={"columns": ["date", "revenue", "surprise"]},
    )
    assert proposal["applied"] is False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT baseline FROM app.dq_monitor_versions WHERE id = %s", (monitor["version_id"],)
        )
        assert cur.fetchone()[0] == {"columns": ["date", "revenue"]}, "the frozen baseline stands"


def test_a_published_monitor_version_is_immutable(conn, scope, monitor):
    import psycopg

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="immutable"):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.dq_monitor_versions SET baseline = '{}'::jsonb WHERE id = %s",
                    (monitor["version_id"],),
                )


# ---------------------------------------------------------------------------
# Issues.
# ---------------------------------------------------------------------------


def test_an_issue_reopens_rather_than_duplicating(conn, scope, monitor):
    from core.dq_governance import open_issue

    fingerprint = "a" * 64
    first = open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        root_cause_fingerprint=fingerprint,
        severity="degrading",
    )
    again = open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        root_cause_fingerprint=fingerprint,
        severity="degrading",
    )
    assert again["id"] == first["id"]
    assert again["recurrence"] == "appended"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.dq_issue_events WHERE issue_id = %s", (first["id"],)
        )
        assert cur.fetchone()[0] == 2, "both sightings are events, not one overwritten row"


def test_a_suppression_without_an_end_date_is_refused(conn, scope, monitor):
    from core.dq_governance import DqGovernanceError, open_issue, transition_issue

    issue = open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        root_cause_fingerprint="b" * 64,
        severity="degrading",
    )
    with pytest.raises(DqGovernanceError, match="silent hole"):
        transition_issue(
            conn,
            project_id=scope["project_id"],
            issue_id=issue["id"],
            event_kind="suppressed",
            actor="operator@example.com",
            reason="Known and accepted.",
        )


def test_an_expired_suppression_brings_the_issue_back(conn, scope, monitor):
    from core.dq_governance import open_issue, open_issues, transition_issue

    issue = open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        root_cause_fingerprint="c" * 64,
        severity="blocking",
    )
    transition_issue(
        conn,
        project_id=scope["project_id"],
        issue_id=issue["id"],
        event_kind="suppressed",
        actor="operator@example.com",
        reason="Accepted for one day.",
        suppressed_until=date.today() - timedelta(days=1),
    )
    listed = {item["id"] for item in open_issues(conn, project_id=scope["project_id"])}
    assert issue["id"] in listed, "an end date that quietly extends itself is a silent hole"


# ---------------------------------------------------------------------------
# Template materialization -- AC3's other half, and the cutover's precondition.
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeds(tmp_path):
    """A two-metric platform template, written locally so the test states its input."""
    (tmp_path / "metric_source_priority.csv").write_text(
        "metric,connector,priority\n"
        "conversions,alpha,1\n"
        "conversions,beta,2\n"
        "revenue,gamma,1\n"
        "revenue,delta,2\n",
        encoding="utf-8",
    )
    return tmp_path


def _concept(conn, scope, name):
    """A published Concept the template can resolve *name* to.

    `additivity_class` IS NOT DECORATION HERE, and the next reader must not
    delete it as noise. Until story 60.2 this fixture wrote a metric version
    carrying an aggregation and no additivity class, which migration 142 allowed
    (`142:189-193`: an aggregation OR `non_additive` satisfied it) and which
    migration 237 now refuses outright — a stored metric names one of
    `additive` / `semi_additive` / `non_additive`.

    So this line is not "a column the constraint wants filled": it is the fixture
    saying what it means. These Concepts carry `{"function": "sum"}` over a
    `decimal`, which is the definition of `additive` — summable across every
    dimension. A fixture that declined to say so was writing exactly the row this
    story exists to forbid: a metric nobody can safely roll up, stored in silence.
    """
    from ulid import ULID

    concept_id = f"sc_{ULID()}"
    version_id = f"scv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts "
            "(id, project_id, kind, name, lifecycle_status, created_by) "
            "VALUES (%s, %s, 'metric', %s, 'published', 'test')",
            (concept_id, scope["project_id"], name),
        )
        cur.execute(
            "INSERT INTO app.semantic_concept_versions "
            "(id, concept_id, project_id, version_number, status, kind, name, label, "
            " value_type, expression, aggregation, additivity_class, content_hash, created_by) "
            "VALUES (%s, %s, %s, 1, 'published', 'metric', %s, %s, 'decimal', "
            " '{\"op\":\"source_measure\"}'::jsonb, '{\"function\":\"sum\"}'::jsonb, "
            " 'additive', %s, 'test')",
            (version_id, concept_id, scope["project_id"], name, name, "e" * 64),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (version_id, concept_id),
        )
    return concept_id


def _digest(value: str) -> str:
    """A distinct 64-hex hash per fixture row.

    Constants collided: the plan idempotency key is unique per Project, so two
    Datastreams in one test could not both exist.
    """
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _datastream(conn, scope, module_name, *, with_mapping=True):
    from ulid import ULID

    datastream_id = f"ds_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, module_name, created_by) "
            "VALUES (%s, %s, %s, %s, %s, 'test')",
            (
                datastream_id,
                scope["project_id"],
                scope["org_id"],
                f"stream-{module_name}-{datastream_id[-6:]}",
                module_name,
            ),
        )
        if with_mapping:
            # A real plan -> mapping chain. `datastreams.current_mapping_version_id`
            # is guarded by a composite FK, and pinning a fabricated id would prove
            # the materializer works against data the product cannot produce.
            plan_id = f"dpv_{ULID()}"
            mapping_id = f"dmv_{ULID()}"
            cur.execute(
                "INSERT INTO app.datastream_plan_versions "
                "(id, datastream_id, project_id, version_number, contract_version, "
                " source_kind, writer_kind, destination_policy, normalized_payload, "
                " content_hash, idempotency_key_hash, created_by) "
                "VALUES (%s, %s, %s, 1, '1', 'connector_pull', 'toorow', "
                " 'managed_raw', '{}'::jsonb, %s, %s, 'test')",
                (
                    plan_id,
                    datastream_id,
                    scope["project_id"],
                    _digest(plan_id),
                    # Unique per Datastream: the idempotency key is unique per
                    # Project, so a constant would let only one stream exist.
                    _digest(f"plan:{plan_id}"),
                ),
            )
            cur.execute(
                "INSERT INTO app.datastream_mapping_versions "
                "(id, datastream_id, project_id, version_number, mapping_contract_version, "
                " source_schema_hash, plan_version_id, content_hash, ossie_spec_version, "
                " toorow_extension_version, mapping_payload, ossie_projection, "
                " idempotency_key_hash, created_by) "
                "VALUES (%s, %s, %s, 1, '1', %s, %s, %s, '0.1.1', '1', "
                " '{}'::jsonb, '{}'::jsonb, %s, 'test')",
                (
                    mapping_id,
                    datastream_id,
                    scope["project_id"],
                    _digest(f"schema:{mapping_id}"),
                    plan_id,
                    _digest(mapping_id),
                    _digest(f"mapping:{mapping_id}"),
                ),
            )
            cur.execute(
                "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
                (mapping_id, datastream_id),
            )
    return datastream_id


def _materialize(conn, scope, seeds):
    from core.controls_quality import materialize_reconciliation_template

    return materialize_reconciliation_template(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor="operator@example.com",
        seeds_dir=seeds,
    )


def test_materializing_the_template_produces_a_draft_and_publishes_nothing(conn, scope, seeds):
    """A template is not a cascade: it becomes an editable draft, in force nowhere."""
    _concept(conn, scope, "conversions")
    _datastream(conn, scope, "alpha")
    _datastream(conn, scope, "beta")

    result = _materialize(conn, scope, seeds)
    assert result["published"] is False
    assert result["draft_version_id"], "a resolvable template must yield a draft"
    assert result["rule_count"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.current_version_id, v.status FROM app.governance_rule_sets s "
            "JOIN app.governance_rule_set_versions v ON v.rule_set_id = s.id "
            "WHERE s.id = %s",
            (result["rule_set_id"],),
        )
        current, status = cur.fetchone()
    assert current is None, "materializing must not put anything in force"
    assert status == "draft"


def test_two_datastreams_for_one_connector_are_refused_not_guessed(conn, scope, seeds):
    """The Implementation Gate's refusal, where it actually bites.

    The template says which PROVIDER comes first. It says nothing about which of
    that provider's two Datastreams, and inventing an answer is exactly the
    equal-label guess the gate forbids.
    """
    _concept(conn, scope, "conversions")
    _datastream(conn, scope, "alpha")
    _datastream(conn, scope, "alpha")
    _datastream(conn, scope, "beta")

    result = _materialize(conn, scope, seeds)
    assert result["rule_count"] == 0, "an ambiguous row must not become a rule"
    assert {item["reason"] for item in result["ambiguous"]} == {"several_datastreams"}
    assert len(result["ambiguous"][0]["datastream_ids"]) == 2


def test_a_connector_this_project_does_not_have_is_reported_not_invented(conn, scope, seeds):
    _concept(conn, scope, "conversions")
    _datastream(conn, scope, "alpha")

    result = _materialize(conn, scope, seeds)
    assert result["rule_count"] == 0, "one resolved source is not a reconciliation"
    assert "no_datastream" in {item["reason"] for item in result["unresolved"]}


def test_a_metric_with_no_published_concept_is_reported(conn, scope, tmp_path):
    """A rule about a metric no Concept defines would be a rule about a string.

    The metric name is deliberately one nothing defines. `conversions` and
    `revenue` are seeded as platform Concepts by migration 142, so using them here
    would have tested the opposite of what this test is named for -- and did, on
    the first run.
    """
    from core.controls_quality import materialize_reconciliation_template

    (tmp_path / "metric_source_priority.csv").write_text(
        "metric,connector,priority\n"
        "undefined_metric_for_this_test,alpha,1\n"
        "undefined_metric_for_this_test,beta,2\n",
        encoding="utf-8",
    )
    _datastream(conn, scope, "alpha")
    _datastream(conn, scope, "beta")

    result = materialize_reconciliation_template(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor="operator@example.com",
        seeds_dir=tmp_path,
    )
    assert result["rule_count"] == 0
    assert {item["reason"] for item in result["unresolved"]} == {"no_published_concept"}


def test_a_published_draft_becomes_the_runtime_rule(conn, scope, seeds):
    """The cutover's precondition, end to end.

    Materialize, publish, and the governed resolver answers where it previously
    returned None -- which is what has to be true before the PROJECT > ORG >
    PLATFORM cascade can be taken away.
    """
    from core.controls_quality import (
        RECONCILIATION_RULE_SET_NAME,
        resolve_reconciliation_rule,
    )
    from core.governance_rule_sets import active_version, publish_version

    concept_id = _concept(conn, scope, "conversions")
    _datastream(conn, scope, "alpha")
    _datastream(conn, scope, "beta")

    result = _materialize(conn, scope, seeds)
    assert (
        resolve_reconciliation_rule(
            conn, project_id=scope["project_id"], concept_id=concept_id
        )
        is None
    ), "a draft is not in force"

    publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=result["rule_set_id"],
        version_id=result["draft_version_id"],
        actor="operator@example.com",
    )
    rule = resolve_reconciliation_rule(
        conn, project_id=scope["project_id"], concept_id=concept_id
    )
    assert rule is not None
    assert rule["method"] == "PRIORITY"
    assert [source["kind"] for source in rule["sources"]] == ["datastream", "datastream"]
    assert all(source["version_id"] for source in rule["sources"]), "every source pins a version"

    head, version = active_version(
        conn,
        project_id=scope["project_id"],
        family="metric_reconciliation",
        name=RECONCILIATION_RULE_SET_NAME,
    )
    assert version["status"] == "published"
    assert head["current_version_id"] == version["id"]


# ---------------------------------------------------------------------------
# The Overview attention projection (AC10).
# ---------------------------------------------------------------------------


def test_a_case_and_the_issue_it_escalated_from_collapse_to_one_item(conn, scope, monitor):
    """The deduplication that makes the projection worth having.

    A DQ issue escalates into a Control Case. They are two owner records and ONE
    problem, and they collapse because both carry the SAME root-cause fingerprint
    -- the owner's, not one minted here. Keying on the row id would show the same
    problem twice, which is how an attention list stops being read.
    """
    from core.control_cases import CaseObservation, observe, root_cause_fingerprint
    from core.controls_attention import control_attention_items
    from core.dq_governance import open_issue
    from core.project_overview import rank_attention_items

    fingerprint = root_cause_fingerprint(
        case_type="dq_escalation",
        subject_kind="datastream",
        subject_id="ds_subject",
        discriminators={"check": "schema"},
    )
    case = observe(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        case_type="dq_escalation",
        subject_kind="datastream",
        subject_id="ds_subject",
        severity="blocking",
        discriminators={"check": "schema"},
        observation=CaseObservation(detected_by="dq", observation={"check": "schema"}),
    )
    open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        root_cause_fingerprint=fingerprint,
        severity="blocking",
        control_case_id=case["id"],
    )

    items = control_attention_items(conn, project_id=scope["project_id"])
    assert len(items) == 2, "two owner records"
    assert {item["root_cause_key"] for item in items} == {fingerprint}
    assert len(rank_attention_items(items)) == 1, "one problem"


def test_an_informational_case_is_not_attention(conn, scope):
    """A list that shows everything is a list nobody reads."""
    from core.control_cases import CaseObservation, observe
    from core.controls_attention import control_attention_items

    observe(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        case_type="note",
        subject_kind="datastream",
        subject_id="ds_quiet",
        severity="informational",
        observation=CaseObservation(detected_by="test", observation={}),
    )
    assert control_attention_items(conn, project_id=scope["project_id"]) == []


def test_a_failed_owner_handoff_is_promoted_in_the_cause(conn, scope):
    """The one state worth saying out loud: decided, and the owner command failed."""
    from datetime import date

    from core.control_cases import CaseObservation, decide, observe
    from core.controls_attention import control_attention_items

    case = observe(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        case_type="mapping_conflict",
        subject_kind="datastream",
        subject_id="ds_subject",
        severity="blocking",
        observation=CaseObservation(detected_by="test", observation={}),
    )
    decide(
        conn,
        project_id=scope["project_id"],
        case_id=case["id"],
        decision_kind="approve_mapping",
        actor="operator@example.com",
        reason="Approved; the Data owner refused the publication.",
        effective_from=date.today(),
        owner_outcome="failed",
    )
    items = control_attention_items(conn, project_id=scope["project_id"])
    assert len(items) == 1
    assert "owner command failed" in items[0]["cause"]


def test_an_expired_suppression_returns_to_attention(conn, scope, monitor):
    """An end date that quietly extends itself is a silent hole."""
    from datetime import date, timedelta

    from core.controls_attention import control_attention_items
    from core.dq_governance import open_issue, transition_issue

    issue = open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        root_cause_fingerprint="9" * 64,
        severity="blocking",
    )
    transition_issue(
        conn,
        project_id=scope["project_id"],
        issue_id=issue["id"],
        event_kind="suppressed",
        actor="operator@example.com",
        reason="Accepted for a day.",
        suppressed_until=date.today() + timedelta(days=7),
    )
    assert control_attention_items(conn, project_id=scope["project_id"]) == []

    transition_issue(
        conn,
        project_id=scope["project_id"],
        issue_id=issue["id"],
        event_kind="suppressed",
        actor="operator@example.com",
        reason="The window closed.",
        suppressed_until=date.today() - timedelta(days=1),
    )
    assert len(control_attention_items(conn, project_id=scope["project_id"])) == 1


def test_an_unreadable_control_store_does_not_take_the_page_down(conn, scope):
    """Overview is a summary: one failed projection must not hide the other five."""
    from unittest.mock import MagicMock

    from core.controls_attention import control_attention_items

    broken = MagicMock()
    broken.cursor.side_effect = RuntimeError("control store unreadable")
    assert control_attention_items(broken, project_id=scope["project_id"]) == []


# ---------------------------------------------------------------------------
# The Controls & Quality command family (AC7).
# ---------------------------------------------------------------------------


@pytest.fixture()
def rule_set(conn, scope):
    """A published Rule Set to prepare changes against."""
    from core.controls_quality import FAMILY_DQ, PROFILE_DQ
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_DQ,
        name="project_policy",
        label="DQ policy",
        actor="test",
    )
    draft = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        profile=PROFILE_DQ,
        label="v1",
        payload={"check": "volume", "baseline": {"median": 100}},
        actor="test",
    )
    publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        version_id=draft["id"],
        actor="test",
    )
    return head["id"]


def _open(conn, scope, rule_set, key="k1"):
    from core.controls_change_sets import create_change_set

    return create_change_set(
        conn,
        project_id=scope["project_id"],
        object_type="rule-set",
        object_id=rule_set,
        intent={"action": "publish_new_version"},
        actor="operator@example.com",
        idempotency_key=key,
    )


def test_the_same_idempotency_key_opens_one_change_set(conn, scope, rule_set):
    first = _open(conn, scope, rule_set)
    again = _open(conn, scope, rule_set)
    assert again["id"] == first["id"]


def test_prepare_returns_the_token_once_and_stores_only_its_hash(conn, scope, rule_set):
    from core.controls_change_sets import prepare_change_set, read_change_set

    record = _open(conn, scope, rule_set)
    prepared, token = prepare_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        actor="operator@example.com",
    )
    assert prepared["state"] == "prepared"
    assert prepared["dependency_fingerprint"]
    assert token and len(token) > 20

    # The read surface can never hand the token back.
    readable = read_change_set(
        conn, project_id=scope["project_id"], change_set_id=record["id"]
    )
    assert "confirmation_token_hash" not in readable
    assert token not in str(readable)


def test_the_impact_is_server_derived_and_the_intent_cannot_supply_it(conn, scope, rule_set):
    """An impact a caller supplied describes what the caller claimed."""
    from core.controls_change_sets import create_change_set, prepare_change_set

    record = create_change_set(
        conn,
        project_id=scope["project_id"],
        object_type="rule-set",
        object_id=rule_set,
        intent={"action": "publish_new_version", "impact": {"active_exceptions": 999}},
        actor="operator@example.com",
        idempotency_key="claimed",
    )
    prepared, _token = prepare_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        actor="operator@example.com",
    )
    assert prepared["impact"]["active_exceptions"] == 0, "the caller's number is ignored"
    assert prepared["impact"]["family"] == "dq_policy"


def test_a_wrong_token_is_refused_indistinguishably_from_a_missing_one(conn, scope, rule_set):
    from core.controls_change_sets import (
        ControlsChangeSetError,
        confirm_change_set,
        prepare_change_set,
    )

    record = _open(conn, scope, rule_set)
    prepare_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        actor="operator@example.com",
    )
    with pytest.raises(ControlsChangeSetError, match="no confirmation to consume"):
        confirm_change_set(
            conn,
            project_id=scope["project_id"],
            change_set_id=record["id"],
            confirmation_token="not-the-token",
            actor="operator@example.com",
        )


def test_confirming_twice_replays_instead_of_minting_a_second_result(conn, scope, rule_set):
    from core.controls_change_sets import confirm_change_set, prepare_change_set

    record = _open(conn, scope, rule_set)
    _prepared, token = prepare_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        actor="operator@example.com",
    )
    first = confirm_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        confirmation_token=token,
        actor="operator@example.com",
        apply=lambda _conn, _record: "grsv_RESULT",
    )
    assert first["state"] == "confirmed"
    assert first["replayed"] is False

    # A second call with the SAME token: the token is gone, and the replay path
    # returns the first result rather than applying anything again.
    applied_again = []
    second = confirm_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        confirmation_token=token,
        actor="operator@example.com",
        apply=lambda _conn, _record: applied_again.append(1),
    )
    assert second["replayed"] is True
    assert second["result_version_id"] == first["result_version_id"]
    assert applied_again == [], "a replay must not re-run the owner command"


def test_drift_between_prepare_and_confirm_is_refused(conn, scope, rule_set):
    """A prepare that was correct ten minutes ago is not evidence it is correct now."""
    from core.controls_change_sets import (
        ControlsChangeSetStale,
        confirm_change_set,
        prepare_change_set,
    )
    from core.controls_quality import PROFILE_DQ
    from core.governance_rule_sets import draft_version, publish_version

    record = _open(conn, scope, rule_set)
    _prepared, token = prepare_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        actor="operator@example.com",
    )

    # Someone else publishes a new version in between.
    moved = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=rule_set,
        profile=PROFILE_DQ,
        label="v2",
        payload={"check": "volume", "baseline": {"median": 200}},
        actor="someone-else",
    )
    publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=rule_set,
        version_id=moved["id"],
        actor="someone-else",
    )

    with pytest.raises(ControlsChangeSetStale, match="changed since"):
        confirm_change_set(
            conn,
            project_id=scope["project_id"],
            change_set_id=record["id"],
            confirmation_token=token,
            actor="operator@example.com",
            apply=lambda _conn, _record: "grsv_SHOULD_NOT_HAPPEN",
        )


def test_an_expired_confirmation_window_refuses_and_is_marked(conn, scope, rule_set):
    from core.controls_change_sets import (
        ControlsChangeSetStale,
        confirm_change_set,
        prepare_change_set,
        read_change_set,
    )

    record = _open(conn, scope, rule_set)
    _prepared, token = prepare_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        actor="operator@example.com",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.controls_change_sets SET expires_at = NOW() - INTERVAL '1 minute' "
            "WHERE id = %s",
            (record["id"],),
        )

    with pytest.raises(ControlsChangeSetStale, match="window has closed"):
        confirm_change_set(
            conn,
            project_id=scope["project_id"],
            change_set_id=record["id"],
            confirmation_token=token,
            actor="operator@example.com",
        )
    after = read_change_set(
        conn, project_id=scope["project_id"], change_set_id=record["id"]
    )
    assert after["state"] == "expired", "an expired window is recorded, not left prepared"


def test_a_change_set_from_another_project_is_not_found(conn, scope, rule_set):
    """Cross-Project reads are indistinguishable from missing ones."""
    from core.controls_change_sets import ControlsChangeSetNotFound, read_change_set

    record = _open(conn, scope, rule_set)
    with pytest.raises(ControlsChangeSetNotFound):
        read_change_set(conn, project_id="proj_someone_else", change_set_id=record["id"])


# ---------------------------------------------------------------------------
# Manual evaluation is durable, replayable, and runs the SCHEDULER's check.
#
# The route this replaces submitted to a request-scoped ThreadPoolExecutor and
# answered 202 before anything ran, so "did my evaluation happen?" had no answer
# and two identical clicks ran it twice.
# ---------------------------------------------------------------------------


def _stub_evaluator(calls):
    from core.dq_governance import EvaluationCounts

    def evaluator(conn, **kwargs):
        calls.append(kwargs)
        return "pass", EvaluationCounts(total_eligible=3, evaluated=3, passed=3), {}, {}

    return evaluator


def test_a_manual_evaluation_leaves_a_durable_operation_and_names_it_in_the_evidence(
    conn, scope, monitor
):
    from core.dq_governance import evaluate_monitor

    calls: list = []
    result = evaluate_monitor(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        monitor_id=monitor["monitor_id"],
        actor="operator@example.com",
        idempotency_key="manual-1",
        window_start=date(2026, 7, 1),
        window_end=date(2026, 7, 1),
        evaluator=_stub_evaluator(calls),
    )
    assert result["outcome"] == "succeeded"
    assert len(calls) == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT command_type, state FROM app.operations WHERE id = %s",
            (result["operation_id"],),
        )
        assert cur.fetchone()[0] == "dq.monitor.evaluate"
        # The evidence row points BACK at the operation, so the run is traceable
        # from the verdict rather than by correlating two tables on a timestamp.
        cur.execute(
            "SELECT operation_id FROM app.dq_evaluations WHERE id = %s",
            (result["result"]["evaluation_id"],),
        )
        assert cur.fetchone()[0] == result["operation_id"]


def test_the_same_manual_evaluation_replays_instead_of_running_twice(conn, scope, monitor):
    from core.dq_governance import evaluate_monitor

    calls: list = []
    evaluator = _stub_evaluator(calls)
    kwargs = {
        "project_id": scope["project_id"],
        "org_id": scope["org_id"],
        "monitor_id": monitor["monitor_id"],
        "actor": "operator@example.com",
        "idempotency_key": "manual-same",
        "window_start": date(2026, 7, 1),
        "window_end": date(2026, 7, 1),
        "evaluator": evaluator,
    }
    first = evaluate_monitor(conn, **kwargs)
    second = evaluate_monitor(conn, **kwargs)

    assert second["replayed"] is True
    assert second["operation_id"] == first["operation_id"]
    # The point of the replay: the check did NOT run a second time.
    assert len(calls) == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.dq_evaluations WHERE monitor_id = %s",
            (monitor["monitor_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_a_monitor_with_no_published_version_is_refused_not_evaluated(conn, scope):
    from core.dq_governance import DqMonitorNotFound, ensure_monitor, evaluate_monitor

    head = ensure_monitor(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name="unpublished",
        label="Never published",
        target_kind="datastream",
        target_id="ds_subject",
        actor="test",
    )
    with pytest.raises(DqMonitorNotFound):
        evaluate_monitor(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            monitor_id=head["id"],
            actor="test",
            idempotency_key="manual-unpublished",
            window_start=date(2026, 7, 1),
            window_end=date(2026, 7, 1),
            evaluator=_stub_evaluator([]),
        )


def test_manual_and_scheduled_work_go_through_one_dispatch_table(conn, scope):
    """AC7 asks for "the same evaluator"; this makes both callers prove it.

    Both are made to observe the SAME substituted entry. If either one kept its
    own hand-written call to `_check_schema`, one of these lists stays empty.
    """
    from core import dq_monitors

    observed: list[str] = []

    def spy(ds, conn_, yesterday, now_utc, version):
        observed.append(ds["id"])
        return False

    datastream_id = _datastream(conn, scope, "ga4", with_mapping=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE app.datastreams SET enabled = TRUE WHERE id = %s", (datastream_id,))
    ds = {
        "id": datastream_id,
        "project_id": scope["project_id"],
        "module_name": "ga4",
        "name": "spy-stream",
        "config": None,
    }

    original = dict(dq_monitors.CHECK_PROFILES)
    dq_monitors.CHECK_PROFILES["schema"] = spy
    try:
        dq_monitors._run_monitors_for_datastream(ds, conn, date(2026, 7, 1))
        scheduled = list(observed)

        from core.dq_governance import ensure_monitor, publish_version

        head = ensure_monitor(
            conn,
            org_id=scope["org_id"],
            project_id=scope["project_id"],
            name="shared_dispatch",
            label="Shared dispatch",
            target_kind="datastream",
            target_id=datastream_id,
            actor="test",
        )
        version = publish_version(
            conn,
            project_id=scope["project_id"],
            monitor_id=head["id"],
            check_profile="schema",
            severity="degrading",
            baseline={"columns": ["date", "revenue"]},
            actor="test",
        )
        observed.clear()
        outcome, counts, _refs, _observed = dq_monitors.governed_evaluator(
            conn,
            project_id=scope["project_id"],
            monitor_id=head["id"],
            monitor_version_id=version["id"],
            window_start=date(2026, 7, 1),
            window_end=date(2026, 7, 1),
        )
    finally:
        dq_monitors.CHECK_PROFILES.clear()
        dq_monitors.CHECK_PROFILES.update(original)

    assert scheduled == [datastream_id], "the scheduled path bypassed the dispatch table"
    assert observed == [datastream_id], "the governed path bypassed the dispatch table"
    assert (outcome, counts.total_eligible, counts.passed) == ("pass", 1, 1)


def test_a_target_that_is_absent_is_not_applicable_and_never_a_pass(conn, scope):
    from core.dq_governance import ensure_monitor, publish_version
    from core.dq_monitors import governed_evaluator

    head = ensure_monitor(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name="absent_target",
        label="Absent target",
        target_kind="datastream",
        target_id="ds_that_does_not_exist",
        actor="test",
    )
    version = publish_version(
        conn,
        project_id=scope["project_id"],
        monitor_id=head["id"],
        check_profile="schema",
        severity="degrading",
        baseline={"columns": ["date", "revenue"]},
        actor="test",
    )
    outcome, counts, _refs, _observed = governed_evaluator(
        conn,
        project_id=scope["project_id"],
        monitor_id=head["id"],
        monitor_version_id=version["id"],
        window_start=date(2026, 7, 1),
        window_end=date(2026, 7, 1),
    )
    # Nothing was measured. A green dashboard here would be a lie about a
    # Datastream that is not even enabled.
    assert (outcome, counts.total_eligible) == ("not_applicable", 0)


def test_a_check_that_raises_is_unavailable_and_never_a_pass(conn, scope):
    from core import dq_monitors
    from core.dq_governance import ensure_monitor, publish_version

    datastream_id = _datastream(conn, scope, "ga4", with_mapping=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE app.datastreams SET enabled = TRUE WHERE id = %s", (datastream_id,))

    def explode(ds, conn_, yesterday, now_utc, version):
        raise RuntimeError("the warehouse is unreachable")

    head = ensure_monitor(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name="raising_check",
        label="Raising check",
        target_kind="datastream",
        target_id=datastream_id,
        actor="test",
    )
    version = publish_version(
        conn,
        project_id=scope["project_id"],
        monitor_id=head["id"],
        check_profile="schema",
        severity="degrading",
        baseline={"columns": ["date", "revenue"]},
        actor="test",
    )
    original = dict(dq_monitors.CHECK_PROFILES)
    dq_monitors.CHECK_PROFILES["schema"] = explode
    try:
        outcome, counts, _refs, observed = dq_monitors.governed_evaluator(
            conn,
            project_id=scope["project_id"],
            monitor_id=head["id"],
            monitor_version_id=version["id"],
            window_start=date(2026, 7, 1),
            window_end=date(2026, 7, 1),
        )
    finally:
        dq_monitors.CHECK_PROFILES.clear()
        dq_monitors.CHECK_PROFILES.update(original)

    assert outcome == "unverifiable"
    assert (counts.unavailable, counts.passed, counts.failed) == (1, 0, 0)
    assert "RuntimeError" in observed["errors"][0]


# ---------------------------------------------------------------------------
# Used-by is MEASURED. Governance "Incomplete if": *a governed object lacks
# stable identity, version, owner, used-by or evidence*.
#
# All three governed types reported `used_by: empty` from a constant, which is
# not a missing facet -- it is a confident wrong answer, and a screen renders it
# as "safe to retire".
# ---------------------------------------------------------------------------


#: The lens each governed type is published under. There is no "all" lens on
#: Controls & Quality, and asking for one would have tested a collection the
#: product does not serve.
_LENS_FOR = {
    "control-case": "conflicts",
    "dq-monitor": "data-quality",
    "rule-set": "rule-sets",
}


def _governed_objects(conn, scope, object_type):
    from core.governance_read_model import compose_governance_collection

    payload = compose_governance_collection(
        scope["project_id"],
        "controls-quality",
        conn,
        lens=_LENS_FOR[object_type],
        org_id=scope["org_id"],
    )
    items = payload.get("items") or []
    return [item for item in items if item["object_ref"]["type"] == object_type]


def test_an_object_nothing_references_reports_a_measured_zero(conn, scope, monitor):
    """`empty` is only honest when it came from a COUNT.

    Same code path as the test below, opposite data. Without this pair, a
    projector that hardcoded `empty` and one that hardcoded `available` would
    each pass one of the two tests and neither would be caught.
    """
    monitors = _governed_objects(conn, scope, "dq-monitor")
    watched = next(item for item in monitors if item["object_ref"]["id"] == monitor["monitor_id"])
    assert (watched["used_by"]["state"], watched["used_by"]["count"]) == ("empty", 0)


def test_a_case_with_an_escalated_issue_does_not_report_that_nothing_uses_it(
    conn, scope, monitor
):
    from core.dq_governance import open_issue

    case = _observe(conn, scope, detail="used-by")
    issue = open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor["monitor_id"],
        # The column is a 64-hex CHECK: a fingerprint is a hash, not a label.
        root_cause_fingerprint=_digest("used-by"),
        severity="degrading",
        control_case_id=case["id"],
        actor="test",
    )
    assert issue["recurrence"] == "opened"

    cases = _governed_objects(conn, scope, "control-case")
    subject = next(item for item in cases if item["object_ref"]["id"] == case["id"])
    # ONE, AND NOT LISTED HERE (49-2, 2026-09-01). This lens counts its dependent
    # issues and composes no list, so the facet is `unavailable` WITH its count:
    # "available with an empty refs list" was the state the workbench rendered as
    # "nothing depends on this object". What the test is about -- that a case with
    # an escalated issue does NOT report a zero -- is unchanged and is the count.
    assert (subject["used_by"]["state"], subject["used_by"]["count"]) == ("unavailable", 1)

    # The other direction: the monitor is used by the case its issue escalated to.
    monitors = _governed_objects(conn, scope, "dq-monitor")
    watched = next(item for item in monitors if item["object_ref"]["id"] == monitor["monitor_id"])
    assert (watched["used_by"]["state"], watched["used_by"]["count"]) == ("unavailable", 1)


def test_a_rule_set_reports_the_monitors_pinned_to_its_published_version(conn, scope):
    from core.dq_governance import ensure_monitor
    from core.dq_governance import publish_version as publish_monitor
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family="dq_policy",
        name="used_by_policy",
        label="Used-by policy",
        actor="test",
    )
    draft = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        profile="dq_policy_v1",
        label="v1",
        payload={"check": "schema", "severity": "degrading", "baseline": {"columns": ["date"]}},
        actor="test",
    )
    published = publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        version_id=draft["id"],
        actor="test",
    )

    rule_sets = _governed_objects(conn, scope, "rule-set")
    subject = next(item for item in rule_sets if item["object_ref"]["id"] == head["id"])
    assert (subject["used_by"]["state"], subject["used_by"]["count"]) == ("empty", 0)

    monitor_head = ensure_monitor(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name="pinned_monitor",
        label="Pinned monitor",
        target_kind="datastream",
        target_id="ds_pinned",
        actor="test",
    )
    publish_monitor(
        conn,
        project_id=scope["project_id"],
        monitor_id=monitor_head["id"],
        check_profile="schema",
        severity="degrading",
        baseline={"columns": ["date"]},
        rule_set_id=head["id"],
        rule_set_version_id=published["id"],
        actor="test",
    )

    rule_sets = _governed_objects(conn, scope, "rule-set")
    subject = next(item for item in rule_sets if item["object_ref"]["id"] == head["id"])
    # Counted from the VERSION the monitor was published against, not from the
    # head: "the latest policy" is not a policy. Counted and not listed, so the
    # facet is `unavailable` with its one -- see the case test above.
    assert (subject["used_by"]["state"], subject["used_by"]["count"]) == ("unavailable", 1)


def test_a_governed_schema_check_is_judged_against_its_frozen_baseline(conn, scope, monkeypatch):
    """The parallel evidence store loses its say over a governed verdict.

    `app.dq_baselines` WAS a mutable side table: `_write_dq_baseline` overwrote it
    the moment a drift was seen, so the second run after a drift agreed with
    whatever the source had become. A governed monitor freezes its baseline
    inside its immutable version; this proves the check reads THAT one and
    consults no other store.

    Retired by migration 298 -- the table no longer exists, and the reader and the
    writer left `dq_monitors` with it. What this test still holds is the part that
    does not depend on the table being there: a governed verdict is judged against
    the version it was PUBLISHED with, and the only baseline lookup on any other
    path (`dq_monitor_bridge.published_baseline`) is not reached at all when the
    caller already supplied the version's own.
    """
    from core import dq_monitors
    from core.dq_governance import ensure_monitor, publish_version

    datastream_id = _datastream(conn, scope, "ga4", with_mapping=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE app.datastreams SET enabled = TRUE WHERE id = %s", (datastream_id,))

    # The warehouse now has a column the frozen baseline does not know about.
    monkeypatch.setattr(
        dq_monitors, "_fetch_raw_columns", lambda *a, **k: ["date", "revenue", "surprise"]
    )
    from core import dq_monitor_bridge

    read_calls: list = []
    write_calls: list = []
    monkeypatch.setattr(
        dq_monitor_bridge,
        "published_baseline",
        lambda *a, **k: read_calls.append(a) or ({"columns": ["surprise"]}, None),
    )
    monkeypatch.setattr(
        dq_monitor_bridge, "derive_monitor", lambda *a, **k: write_calls.append(a)
    )
    fired: list = []

    head = ensure_monitor(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name="frozen_baseline",
        label="Frozen baseline",
        target_kind="datastream",
        target_id=datastream_id,
        actor="test",
    )
    version = publish_version(
        conn,
        project_id=scope["project_id"],
        monitor_id=head["id"],
        check_profile="schema",
        severity="degrading",
        baseline={"columns": ["date", "revenue"]},
        actor="test",
    )

    import core.infra_alerts as infra_alerts

    monkeypatch.setattr(infra_alerts, "write_infra_firing", lambda **kw: fired.append(kw))

    outcome, counts, _refs, _observed = dq_monitors.governed_evaluator(
        conn,
        project_id=scope["project_id"],
        monitor_id=head["id"],
        monitor_version_id=version["id"],
        window_start=date(2026, 7, 1),
        window_end=date(2026, 7, 1),
    )

    # Drift against the FROZEN baseline. The fallback lookup was stubbed to say
    # ["surprise"], which would have reported agreement -- so a check that fell
    # through to it would PASS here, and this assertion would go red.
    assert (outcome, counts.failed) == ("fail", 1)
    assert read_calls == [], "the governed check looked a baseline up a second time"
    assert write_calls == [], "the governed check published a version of its own"


def test_the_retired_baseline_table_is_gone_from_the_schema(conn):
    """Migration 298 applied, and nothing re-creates the table.

    Asserted against the LIVE schema and not against the migration text, because
    a `CREATE TABLE IF NOT EXISTS` in any later migration would put it back and a
    text check would not notice.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('app.dq_baselines')")
        assert cur.fetchone()[0] is None, (
            "app.dq_baselines is back. It holds one mutable column set per "
            "Datastream, with no monitor, no version and no decision date -- "
            "migration 145 refuses to adopt such a row. The baseline belongs in "
            "the published `schema` DQ Monitor version."
        )


# ---------------------------------------------------------------------------
# A confirmed change set RUNS an owner command.
#
# Everything around the confirmation was already real -- single-use token,
# expiry, drift recheck -- and it guarded an operation that never happened:
# `confirm_change_set` takes an `apply` callable and the route passed none, so a
# confirmed change set left `result_version_id` NULL and published nothing. That
# is worse than an unbuilt command, because it looks delivered.
# ---------------------------------------------------------------------------


def _change_set(conn, scope, object_type, object_id, intent, key):
    from core.controls_change_sets import create_change_set

    return create_change_set(
        conn,
        project_id=scope["project_id"],
        object_type=object_type,
        object_id=object_id,
        intent=intent,
        actor="operator@example.com",
        idempotency_key=key,
    )


def _confirm(conn, scope, record, *, actor="operator@example.com"):
    """Prepare, then confirm WITH the owner command the route now passes."""
    from core.controls_change_sets import confirm_change_set, prepare_change_set
    from core.controls_owner_commands import owner_command

    _prepared, token = prepare_change_set(
        conn, project_id=scope["project_id"], change_set_id=record["id"], actor=actor
    )
    return confirm_change_set(
        conn,
        project_id=scope["project_id"],
        change_set_id=record["id"],
        confirmation_token=token,
        actor=actor,
        apply=owner_command(actor=actor, org_id=scope["org_id"]),
    )


def test_confirming_a_rule_set_change_set_publishes_a_version(conn, scope, rule_set):
    """The one that unblocks Story 48.4: without it no Project can publish a ladder."""
    record = _change_set(
        conn,
        scope,
        "rule-set",
        rule_set,
        {
            "profile": "dq_policy_v1",
            "label": "Published by a change set",
            "payload": {
                "check": "schema",
                "severity": "degrading",
                "baseline": {"columns": ["date"]},
            },
        },
        "apply-rule-set",
    )
    confirmed = _confirm(conn, scope, record)

    assert confirmed["state"] == "confirmed"
    version_id = confirmed["result_version_id"]
    assert version_id, "a confirmed change set that points at nothing published nothing"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.governance_rule_sets WHERE id = %s",
            (rule_set,),
        )
        # The head advanced to exactly the version the change set reports.
        assert cur.fetchone()[0] == version_id
        cur.execute(
            "SELECT status FROM app.governance_rule_set_versions WHERE id = %s", (version_id,)
        )
        assert cur.fetchone()[0] == "published"


def test_confirming_a_dq_monitor_change_set_publishes_a_monitor_version(conn, scope, monitor):
    record = _change_set(
        conn,
        scope,
        "dq-monitor",
        monitor["monitor_id"],
        {
            "check_profile": "schema",
            "severity": "blocking",
            "baseline": {"columns": ["date", "revenue", "cost"]},
        },
        "apply-monitor",
    )
    confirmed = _confirm(conn, scope, record)

    version_id = confirmed["result_version_id"]
    assert version_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.dq_monitors WHERE id = %s",
            (monitor["monitor_id"],),
        )
        assert cur.fetchone()[0] == version_id
        # `last_known_good` keeps the version that was current BEFORE, not the
        # one that just replaced it -- otherwise a rollback rolls back to itself.
        cur.execute(
            "SELECT last_known_good_version_id FROM app.dq_monitors WHERE id = %s",
            (monitor["monitor_id"],),
        )
        assert cur.fetchone()[0] == monitor["version_id"]


def test_confirming_a_control_case_change_set_records_a_decision(conn, scope):
    case = _observe(conn, scope, detail="apply")
    record = _change_set(
        conn,
        scope,
        "control-case",
        case["id"],
        {
            "decision_kind": "accept_as_is",
            "reason": "Reviewed and accepted.",
            "effective_from": "2026-07-01",
        },
        "apply-case",
    )
    confirmed = _confirm(conn, scope, record)

    decision_id = confirmed["result_version_id"]
    assert decision_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision_kind, owner_outcome FROM app.control_case_decisions WHERE id = %s",
            (decision_id,),
        )
        # `not_applicable`, not `applied`: no owner was asked, and saying
        # otherwise would claim a handoff that never happened.
        assert cur.fetchone() == ("accept_as_is", "not_applicable")


def test_an_intent_the_profile_refuses_publishes_nothing_and_keeps_the_change_set(
    conn, scope, rule_set
):
    """A refusal must not spend the confirmation.

    The token is verified before `apply` runs and nulled only after it succeeds,
    so a refused intent can be corrected and confirmed again. If the refusal ate
    the token, every typo would cost a full prepare cycle.
    """
    from core.controls_change_sets import read_change_set
    from core.controls_owner_commands import OwnerCommandError

    record = _change_set(
        conn,
        scope,
        "rule-set",
        rule_set,
        # A drift check with no frozen baseline: the profile refuses it, and
        # this is the profile's own sentence reaching the caller.
        {"profile": "dq_policy_v1", "payload": {"check": "schema", "severity": "degrading"}},
        "apply-refused",
    )
    with pytest.raises(OwnerCommandError):
        _confirm(conn, scope, record)

    after = read_change_set(conn, project_id=scope["project_id"], change_set_id=record["id"])
    assert after["state"] == "prepared", "a refused owner command must not confirm the change set"
    assert after["result_version_id"] is None


def test_a_change_set_with_no_intent_is_refused_rather_than_publishing_an_empty_version(
    conn, scope, rule_set
):
    from core.controls_owner_commands import OwnerCommandError

    # `create_change_set` refuses an empty intent, so the reachable gap is an
    # intent that carries something but not the key the owner needs.
    record = _change_set(
        conn, scope, "rule-set", rule_set, {"label": "no profile named"}, "apply-empty"
    )
    with pytest.raises(OwnerCommandError, match="profile"):
        _confirm(conn, scope, record)


def test_a_mapping_approval_whose_owner_refuses_is_recorded_as_failed(conn, scope, monkeypatch):
    """The handoff Story 49.4 AC7 names, and the rule it must not soften.

    Data refusing the publication is a real recorded result: the decision is
    written with `owner_outcome='failed'` and the case does NOT advance. Raising
    instead would roll the confirmation back and leave no trace that the owner
    was ever asked.
    """
    import core.governed_publication as governed_publication

    def refuse(*args, **kwargs):
        # The owner's real refusal type, with its real stable code -- not a
        # stand-in, so the mapping from code to recorded reason is exercised.
        raise governed_publication.PublicationConfirmationRefused(
            "stale_versions", "the reviewed versions moved"
        )

    monkeypatch.setattr(governed_publication, "confirm_and_publish", refuse)

    case = _observe(conn, scope, detail="handoff")
    record = _change_set(
        conn,
        scope,
        "control-case",
        case["id"],
        {
            "decision_kind": "approve_mapping",
            "reason": "Approved by review.",
            "effective_from": "2026-07-01",
            "data_confirmation_id": "pc_example",
            "data_confirmation_secret": "not-a-real-secret",
        },
        "apply-handoff",
    )
    confirmed = _confirm(conn, scope, record)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner_outcome, owner_result FROM app.control_case_decisions WHERE id = %s",
            (confirmed["result_version_id"],),
        )
        outcome, owner_result = cur.fetchone()
        assert outcome == "failed"
        assert owner_result["refusal"] == "stale_versions"
        # THE SENTENCE, WHOLE. Measured 2026-08-31: this function stored the code
        # and DROPPED the message, while `CaseDecisionDialog` reads a `message`
        # key -- so every refusal rendered "the Data owner refused the
        # publication and gave no reason", and `governance.md`'s rule that the
        # owner's refusal is quoted rather than paraphrased could not be met by
        # any screen, because the words never left this process.
        assert owner_result["message"] == "the reviewed versions moved"
        # The secret is never echoed back, anywhere.
        assert "not-a-real-secret" not in json.dumps(owner_result)

        cur.execute("SELECT status FROM app.control_cases WHERE id = %s", (case["id"],))
        assert cur.fetchone()[0] != "decided", "a failed handoff advanced the case"


def test_a_publication_data_performed_is_recorded_in_the_owners_own_vocabulary(
    conn, scope, monkeypatch
):
    """A SUCCESS THAT COULD NOT BE STORED, measured 2026-08-31.

    `_hand_off_to_data` returned `"applied"`. `control_cases.OWNER_OUTCOMES` is
    `("succeeded", "failed", "not_applicable")`, so `decide` refused the word,
    the refusal became an `OwnerCommandError` -> 422, and the console said
    *"The decision was refused. Nothing was recorded."* about a publication Data
    had ALREADY performed -- the pointer moved, the decision did not.

    MUTATION: return `"applied"` again -> `OwnerCommandError` here and this is red.
    """
    import core.governed_publication as governed_publication
    from core.control_cases import OWNER_OUTCOMES

    def publish(*args, **kwargs):
        return governed_publication.PublicationResult(
            confirmation_id="pc_example",
            operation_id="op_example",
            outcome="succeeded",
            replayed=False,
            current_mapping_version_id="dmv_example_new",
            prior_mapping_version_id="dmv_example_old",
            result={},
        )

    monkeypatch.setattr(governed_publication, "confirm_and_publish", publish)

    case = _observe(conn, scope, detail="handoff-succeeded")
    record = _change_set(
        conn,
        scope,
        "control-case",
        case["id"],
        {
            "decision_kind": "approve_mapping",
            "reason": "Approved by review.",
            "effective_from": "2026-07-01",
            "data_confirmation_id": "pc_example",
            "data_confirmation_secret": "not-a-real-secret",
        },
        "apply-handoff-ok",
    )
    confirmed = _confirm(conn, scope, record)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner_outcome, owner_result FROM app.control_case_decisions WHERE id = %s",
            (confirmed["result_version_id"],),
        )
        outcome, owner_result = cur.fetchone()
    assert outcome in OWNER_OUTCOMES, f"{outcome!r} is not a word the owner admits"
    assert outcome == "succeeded"
    # The pointer Data moved, and the one it can roll back to, both public.
    assert owner_result["current_mapping_version_id"] == "dmv_example_new"
    assert owner_result["prior_mapping_version_id"] == "dmv_example_old"
    assert "not-a-real-secret" not in json.dumps(owner_result)


def test_an_owner_outage_is_a_failed_handoff_that_names_the_repair(conn, scope, monkeypatch):
    """An outage is not a refusal, and neither is silent.

    The exception's own text is a stack-trace sentence; what reaches the person
    is a sentence naming what to do next. Both are stored as `failed`, because
    the mapping was not published either way.
    """
    import core.governed_publication as governed_publication

    def explode(*args, **kwargs):
        raise TimeoutError("connection to the owner timed out at 0x7f")

    monkeypatch.setattr(governed_publication, "confirm_and_publish", explode)

    case = _observe(conn, scope, detail="handoff-outage")
    record = _change_set(
        conn,
        scope,
        "control-case",
        case["id"],
        {
            "decision_kind": "approve_mapping",
            "reason": "Approved by review.",
            "effective_from": "2026-07-01",
            "data_confirmation_id": "pc_example",
            "data_confirmation_secret": "not-a-real-secret",
        },
        "apply-handoff-outage",
    )
    confirmed = _confirm(conn, scope, record)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner_outcome, owner_result FROM app.control_case_decisions WHERE id = %s",
            (confirmed["result_version_id"],),
        )
        outcome, owner_result = cur.fetchone()
    assert outcome == "failed"
    assert owner_result["refusal"] == "TimeoutError"
    assert "0x7f" not in owner_result["message"], "a stack-trace sentence is not a repair"
    assert "again" in owner_result["message"]


# ---------------------------------------------------------------------------
# Story 48.4, re-entry checklist step 1: draft and publish ONE `tax_fee` ladder
# version through the route that Story 49.4 owns.
#
# 48.4 stopped here deliberately (Amendment 1, authorized by Jean): there was no
# REST surface for `governance_rule_sets`, so no Project could have a published
# ladder and the user's path could not be executed end to end. Both halves now
# exist -- the confirm route runs an owner command, and the profile registry is
# populated when that route loads -- so this is the proof, not a claim.
# ---------------------------------------------------------------------------


def _ladder_source_evidence(**overrides):
    base = {
        "issuer": "Agency",
        "reference": "Master services agreement, schedule B",
        "reference_version": "v3",
        "document_ref": "MSA-2026-B",
        "published_on": "2026-01-01",
    }
    base.update(overrides)
    return base


def _ladder_rule(**overrides):
    """One jurisdiction-independent agency fee: the simplest legal ladder rule."""
    rule = {
        "rule_key": "agency_fee",
        "label": "Agency fee",
        "scope_kind": "project",
        "scope_ref": None,
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": "0.15",
        "base_target": "NET_MEDIA",
        "cascade_phase": 5,
        "sequence_order": 10,
        "effective_from": "2026-01-01",
        "authority_kind": "agency_contract",
        "source_evidence": _ladder_source_evidence(),
    }
    rule.update(overrides)
    return rule


@pytest.fixture()
def currency_vocabulary_version(conn):
    """The bottom of the pin chain, imported rather than invented.

    The Money Policy profile REQUIRES a pinned `currency_vocabulary_version`, so
    a Project with no imported ISO snapshot cannot publish a money policy, and
    therefore cannot publish a tax ladder either. Building the chain from a
    fabricated id would have hidden that this onboarding step is load-bearing.
    """
    from core.currency_vocabulary import import_currency_vocabulary

    stored = import_currency_vocabulary(
        conn,
        actor="test",
        source_version="ISO-4217:2026-01",
        effective_date="2026-01-01",
    )
    return stored


@pytest.fixture()
def money_policy_version(conn, scope, currency_vocabulary_version):
    """A REAL published money policy version for the ladder to pin.

    Fabricating an id would have proved the transport works against data the
    product cannot produce, and the ladder's whole point is that its arithmetic
    is pinned to an exact money policy rather than to "the latest".
    """
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.money_policy import PROFILE_MONEY

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family="money_policy",
        name="project_money_policy",
        label="Money policy",
        actor="test",
    )
    draft = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        profile=PROFILE_MONEY,
        label="v1",
        payload={
            "reporting_currency": "EUR",
            "rounding": "half_even",
            "max_staleness_days": 7,
            "rate_source_priority": ["ecb"],
        },
        requires=[
            {
                "kind": "currency_vocabulary_version",
                "object_id": str(currency_vocabulary_version["vocabulary_key"]),
                "version_id": str(currency_vocabulary_version["id"]),
            }
        ],
        actor="test",
    )
    published = publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        version_id=draft["id"],
        actor="test",
    )
    return {"object_id": head["id"], "version_id": published["id"]}


@pytest.fixture()
def tax_fee_ladder(conn, scope):
    """The Rule Set head only. The VERSION is what the change set must produce."""
    from core.governance_rule_sets import ensure_rule_set
    from core.tax_fee_rule_set import FAMILY_TAX_FEE, LADDER_NAME

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_TAX_FEE,
        name=LADDER_NAME,
        label="Tax and fee ladder",
        actor="test",
    )
    return head["id"]


def _pins(money_policy_version):
    return [
        {
            "kind": "money_policy_version",
            "object_id": money_policy_version["object_id"],
            "version_id": money_policy_version["version_id"],
        }
    ]


def test_a_tax_fee_ladder_is_published_through_the_governed_change_set(
    conn, scope, tax_fee_ladder, money_policy_version
):
    """Story 48.4 Definition-of-Done point 4, and the reason it was not met.

    Nothing Tax-shaped is involved in the transport: the same change set, the
    same single-use token, the same owner command that publishes a DQ policy.
    That is the whole point of the family being one more on the generic
    lifecycle rather than a fifth lifecycle of its own.
    """
    from core.tax_fee_rule_set import PROFILE_TAX_FEE, resolve_tax_fee_ladder

    record = _change_set(
        conn,
        scope,
        "rule-set",
        tax_fee_ladder,
        {
            "profile": PROFILE_TAX_FEE,
            "label": "Ladder v1",
            "payload": {"rounding": "half_even", "default_money_basis": "native_source"},
            "ordered_rules": [_ladder_rule()],
            "requires": _pins(money_policy_version),
        },
        "tax-ladder-v1",
    )
    confirmed = _confirm(conn, scope, record)

    version_id = confirmed["result_version_id"]
    assert version_id, "no Project can have a published ladder if this is NULL"

    # Read it back through the Tax capability's own resolver -- the code the
    # compiler and the Datastream panel use. A version that only the change set
    # can see is not a published ladder.
    ladder = resolve_tax_fee_ladder(conn, project_id=scope["project_id"])
    assert ladder.version_id == version_id
    assert [rule["rule_key"] for rule in ladder.rules] == ["agency_fee"]
    # The evidence the capability's `Incomplete if` list requires on every rule.
    evidence = ladder.rules[0]["source_evidence"]
    assert evidence["issuer"] and evidence["reference_version"]


def test_a_ladder_rule_with_no_source_evidence_is_refused_by_the_route_not_stored(
    conn, scope, tax_fee_ladder, money_policy_version
):
    """The profile's refusals reach the caller through the generic transport.

    `tax-fees.md` lists "a rule lacks source, jurisdiction, effective date or
    version evidence" as an incompleteness criterion with no exemption. The
    refusal must therefore survive the trip through a transport that knows
    nothing about tax.
    """
    from core.controls_owner_commands import OwnerCommandError
    from core.tax_fee_rule_set import PROFILE_TAX_FEE

    rule = _ladder_rule()
    del rule["source_evidence"]
    record = _change_set(
        conn,
        scope,
        "rule-set",
        tax_fee_ladder,
        {
            "profile": PROFILE_TAX_FEE,
            "payload": {},
            "ordered_rules": [rule],
            "requires": _pins(money_policy_version),
        },
        "tax-ladder-no-evidence",
    )
    with pytest.raises(OwnerCommandError, match="source_evidence"):
        _confirm(conn, scope, record)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.governance_rule_set_versions WHERE rule_set_id = %s",
            (tax_fee_ladder,),
        )
        assert cur.fetchone()[0] == 0, "a refused ladder must leave no version behind"


def test_the_ladder_family_travels_the_same_road_as_every_other_family(
    conn, scope, tax_fee_ladder, rule_set, money_policy_version
):
    """AC1's real claim, asserted rather than described.

    A Tax ladder and a DQ policy are published by the SAME code path. If Tax had
    kept its own activation authority -- the defect 48.4 removed -- these two
    would have to differ somewhere, and the assertion below is where it would
    show.
    """
    from core.controls_owner_commands import _HANDLERS
    from core.tax_fee_rule_set import PROFILE_TAX_FEE

    tax = _change_set(
        conn,
        scope,
        "rule-set",
        tax_fee_ladder,
        {
            "profile": PROFILE_TAX_FEE,
            "payload": {},
            "ordered_rules": [_ladder_rule()],
            "requires": _pins(money_policy_version),
        },
        "same-road-tax",
    )
    dq = _change_set(
        conn,
        scope,
        "rule-set",
        rule_set,
        {
            "profile": "dq_policy_v1",
            "payload": {
                "check": "schema",
                "severity": "degrading",
                "baseline": {"columns": ["date"]},
            },
        },
        "same-road-dq",
    )
    assert _confirm(conn, scope, tax)["result_version_id"]
    assert _confirm(conn, scope, dq)["result_version_id"]
    # One handler for the whole object type, not one per family.
    assert set(_HANDLERS) == {"rule-set", "dq-monitor", "control-case"}
