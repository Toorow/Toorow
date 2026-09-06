"""Focused lifecycle proofs for Story 47.4."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core import admin_api, datastreams_api, scheduler
from core.datastream_activation import (
    ActivationValidationError,
    PreviewValidationError,
    _promote_managed_candidate,
    build_candidate_review,
    build_safe_preview,
    candidate_schema_hash,
    compile_materialization_contract,
    complete_candidate_from_adapter,
    execute_confirmed_operation,
    freeze_final_review,
    preview_dispatch_request,
    publish_activate_mutation,
    read_candidate_review,
)
from core.datastream_field_mapping import save_field_mapping
from core.datastream_intents import save_datastream_intent

# AI-317: the fake cursor names every statement it answers, derives its
# description from the SELECT, and raises on anything else.
from tests.support.statement_router import StatementInventory, UnknownStatement, describe

ROOT = Path(__file__).resolve().parents[3]


def test_connector_pull_publication_promotes_with_its_frozen_typed_columns() -> None:
    columns = [
        {"name": "date", "type": "DATE"},
        {"name": "pull_id", "type": "STRING"},
    ]
    with (
        patch("core.managed_feed_ledger.fetch_ledger_for_execution", return_value=None),
        patch("core.raw_landing.promote_candidate", return_value={"rows": 2}) as promote,
    ):
        result = _promote_managed_candidate(
            MagicMock(),
            execution_id="dse_typed",
            project_id="proj_1",
            row_count=2,
            artifact_ref="raw_example__cand_dse_typed",
            candidate_columns=columns,
        )

    assert result == {"rows": 2}
    promote.assert_called_once_with(
        "raw_example",
        "dse_typed",
        columns=[("date", "DATE"), ("pull_id", "STRING")],
        project_id="proj_1",
        idempotency_column="execution_id",
        idempotency_value="dse_typed",
        expected_rows=2,
    )


def test_plan_and_mapping_versions_can_be_inserted_without_active_pointer_changes() -> None:
    intent = inspect.signature(save_datastream_intent).parameters
    mapping = inspect.signature(save_field_mapping).parameters

    assert intent["advance_pointer"].default is True
    assert mapping["advance_pointer"].default is True
    assert mapping["pinned_plan_version_id"].default is None


def test_setup_state_migration_separates_preview_review_materialization_and_lifecycle() -> None:
    sql = (ROOT / "infra/nango/migrations/137_datastream_setup_activation.sql").read_text(
        encoding="utf-8"
    )

    assert "app.datastream_setup_previews" in sql
    assert "app.datastream_setup_final_reviews" in sql
    assert "app.datastream_setup_materializations" in sql
    assert "lifecycle_state IN ('draft', 'active', 'paused', 'archived')" in sql
    assert "next_run_at = NULL" in sql
    assert "reject_datastream_setup_evidence_mutation" in sql


def _pins() -> dict:
    return {
        "draft_revision_id": "dsdr_1",
        "proposal_id": "dspp_1",
        "observation_id": "dsobs_1",
        "connector_contract_version_id": "ccv_1",
        "project_configuration_version_id": "pcv_1",
        "capability_version_ids": ["pcap_1"],
        "mapping_hash": "a" * 64,
        "processing_hash": "b" * 64,
    }


def test_safe_preview_is_deterministic_bounded_and_masks_deny_by_default() -> None:
    rows = [
        {"date": f"2026-07-{day:02d}", "spend": day, "email": f"u{day}@example.com"}
        for day in range(30, 0, -1)
    ]
    kwargs = {
        "mode": "connector_pull",
        "pins": _pins(),
        "adapter_evidence": {
            "adapter_verified": True,
            "requested_interval": {"start": "2026-07-01", "end": "2026-08-01"},
            "observed_interval": {"start": "2026-07-01", "end": "2026-07-31"},
            "quota_cost": {"read_points": 1},
            "schema": {"fields": ["date", "spend", "email"]},
            "rows": rows,
            "coverage": {"state": "covered"},
            "dq": {"blocking": []},
            "processing": ["land", "map", "validate"],
            "outputs": [{"kind": "full_grain"}],
        },
        "classifications": {"date": "none", "spend": "financial"},
    }

    first = build_safe_preview(**kwargs)
    second = build_safe_preview(**kwargs)

    assert first == second
    assert len(first["sample"]) == 25
    assert first["sample"][0]["date"] == "2026-07-01"
    assert {row["spend"] for row in first["sample"]} == {"[MASKED]"}
    assert {row["email"] for row in first["sample"]} == {"[MASKED]"}
    assert "example.com" not in str(first)


@pytest.mark.parametrize(
    ("mode", "evidence"),
    [
        ("external_bq", {"adapter_verified": True, "read_only": False}),
        ("managed_feed", {"adapter_verified": False, "detected_format": "csv"}),
        ("connector_pull", {"adapter_verified": True, "placeholder": True}),
    ],
)
def test_unverified_unavailable_or_write_capable_preview_cannot_pass(
    mode: str, evidence: dict
) -> None:
    with pytest.raises(PreviewValidationError):
        build_safe_preview(mode=mode, pins=_pins(), adapter_evidence=evidence, classifications={})


def test_connector_preview_work_uses_shared_queue_and_revision_correlation() -> None:
    dispatched: list[dict] = []
    payload = preview_dispatch_request(
        mode="connector_pull",
        project_id="proj_1",
        draft_id="dsd_1",
        pins=_pins(),
        dispatch=dispatched.append,
    )

    assert dispatched == [payload]
    assert payload["kind"] == "datastream_setup_preview"


def test_final_review_refuses_blockers_and_requires_every_warning_acknowledgement() -> None:
    proposal = {
        "proposal_ref": "dspp_1",
        "content_hash": "c" * 64,
        "confirmed_intent_bundle": {"joint_grain": ["date", "country"]},
        "sections": [
            {
                "items": [
                    {
                        "key": "schedule.policy",
                        "status": "warning",
                        "warnings": [{"cause": "review"}],
                        "blockers": [],
                    }
                ]
            }
        ],
    }
    preview = {
        "status": "ready_for_review",
        "preview_ref": "dspv_1",
        "dependency_hash": "d" * 64,
        "evidence_hash": "e" * 64,
    }

    with pytest.raises(ActivationValidationError):
        freeze_final_review(
            project_id="proj_1",
            draft_id="dsd_1",
            proposal=proposal,
            preview=preview,
            acknowledged_warning_ids=[],
        )
    review = freeze_final_review(
        project_id="proj_1",
        draft_id="dsd_1",
        proposal=proposal,
        preview=preview,
        acknowledged_warning_ids=["schedule.policy"],
    )
    assert review["acknowledged_warning_ids"] == ["schedule.policy"]
    assert len(review["content_hash"]) == 64


def _candidate() -> dict:
    return {
        "project_id": "proj_1",
        "datastream_id": "ds_1",
        "execution_id": "dse_01",
        "state": "ready",
        "adapter_verified": True,
        "placeholder": False,
        "artifact_ref": "candidate/dse_01/output",
        "content_hash": "f" * 64,
        "row_count": 10,
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "projection_hash": "a" * 64,
        "output_plan": {"kind": "full_grain"},
        "schedule": {"next_run_at": "2026-07-30T02:00:00Z"},
        "expected_current_execution_id": None,
        "dq": {"blocking": []},
    }


# THE FIVE STATEMENTS THE PUBLISH-ACTIVATE ACT MAKES. Named here so that a sixth
# one cannot be answered by an `else` that returned `None` to everything -- which
# is how `fetchone()` says "no such row", so an unrecognised read used to reach
# the product as a MISSING ROW and fail it for the wrong reason (AI-317).
_PUBLISH = StatementInventory(
    "_PublishCursor",
    advance_state=("update app.datastream_executions", "set state = %s"),
    output_id="select id from app.datastream_outputs",
    # FOUND BY THE CONVERSION, 2026-08-25. Story 68.2 re-verifies the entity
    # designations of the pinned mapping under the same FOR UPDATE, and the fake
    # never modelled that read: it answered `None`, which the product reads as
    # "no such mapping version" and turns into an empty payload. Every publish
    # test therefore walked the ONE branch where there is nothing to re-verify,
    # and would have kept walking it if the re-verification query had moved.
    mapping_payload=("select mapping_payload", "from app.datastream_mapping_versions"),
    # The seven writes of the act. They were answered by the same silent `else`
    # as everything else, so `conn.executed` was the ONLY record that they had
    # happened at all -- and a write that stopped being issued would have left
    # that list shorter without any test noticing which one went missing.
    publication_log="insert into app.datastream_publication_log",
    pointer_swap=("update app.datastreams", "set current_published_execution_id"),
    schedule_state="update app.datastream_schedule_state",
    arrival_monitor="insert into app.datastream_arrival_monitors",
    output_insert="insert into app.datastream_outputs",
    output_version="insert into app.datastream_output_versions",
    audit_log="insert into app.audit_log",
    # `advance_state` keeps the step ledger: it opens the step it is entering and
    # closes the ones it is leaving. Both were silent here too.
    step_open="insert into app.datastream_execution_step_evidence",
    step_close="update app.datastream_execution_step_evidence",
    lock_state=("select state, datastream_id, project_id", "from app.datastream_executions"),
    execution_row=(
        "select id, datastream_id, project_id, plan_version_id",
        "from app.datastream_executions",
    ),
    # Stage evidence: this fixture records none, and the ABSENCE is now declared
    # rather than falling out of an `else` that answered `None` to everything.
    stage_evidence=("from app.datastream_execution_stage_evidence",),
    stage_evidence_write=("insert into app.datastream_execution_stage_evidence",),
    phase_evidence=("from app.datastream_execution_phase_evidence",),
    phase_evidence_write=("insert into app.datastream_execution_phase_evidence",),
    execution_versions=(
        "select plan_version_id,mapping_version_id",
        "from app.datastream_executions",
    ),
    locked_join=("select e.state", "from app.datastream_executions e"),
)

_PUBLISH_WRITES = frozenset(
    {
        "publication_log",
        "pointer_swap",
        "schedule_state",
        "arrival_monitor",
        "output_insert",
        "output_version",
        "audit_log",
        "step_open",
        "step_close",
        "stage_evidence_write",
        "phase_evidence_write",
    }
)

# The execution row this fixture stands for, BY COLUMN NAME. `state` is not here:
# it lives on the connection, because the point of this double is that the state
# machine moves.
_EXECUTION_VALUES = {
    "id": "dse_01",
    "datastream_id": "ds_1",
    "project_id": "proj_1",
    "plan_version_id": "dsp_1",
    "mapping_version_id": "dmap_1",
    "projection_plan_ref": {},
    "state_changed_at": None,
    "content_hash": "f" * 64,
    "row_count": 10,
    "error_code": None,
    "error_detail": None,
    "idempotency_key_hash": None,
    "created_by": "operator",
    "created_at": None,
    "updated_at": None,
    "artifact_ref": "candidate/dse_01/output",
    "current_published_execution_id": None,
    "org_id": "org_1",
    "enabled": False,
}


class _PublishCursor:
    """A cursor that can answer a STATE MACHINE, which a blanket mock cannot.

    AI-223: `publish_activate_mutation` no longer writes
    `app.datastream_executions.state` with two statements of its own -- it goes
    through `datastream_publication.advance_state`, which LOCKS AND RE-READS the
    row before every transition. A double whose `fetchone` answers one frozen
    tuple forever therefore reports `ready` a second time and the machine
    correctly refuses `ready -> published`. Carrying the state here is what lets
    the test watch the run cross `ready -> publishing -> published` instead of
    watching a mock.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self.description = None
        self._result = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        statement = _PUBLISH.match(sql)
        if statement == "advance_state":
            self.description = None
            self._result = None
            self._conn.state = params[0]
            self._conn.state_writes.append(params[0])
            return
        if statement in _PUBLISH_WRITES:
            # A write returns no result set, and `description = None` is exactly
            # how psycopg says so.
            self.description = None
            self._result = None
            return
        if statement == "output_id":
            self.description = [("id",)]
            self._result = ("dso_1",)
            return
        if statement in {"stage_evidence", "phase_evidence"}:
            self.description = describe(sql)
            self._result = None  # no stage evidence was recorded for this run
            return
        if statement == "mapping_payload":
            # The pinned mapping EXISTS and designates no entity type. Said out
            # loud, because "the row is missing" and "the row designates
            # nothing" reach the same `return` and are not the same fixture.
            self.description = describe(sql)
            self._result = (self._conn.mapping_payload,)
            return
        # Every read below is PROJECTED from the statement rather than restated:
        # the fixture holds values by column name and the SELECT says which ones
        # it wants, in which order (AI-317).
        self.description = describe(sql)
        values = self._conn.row_values(locked=statement == "locked_join")
        self._result = tuple(values.get(name) for (name,) in self.description)

    def fetchone(self):
        return self._result


class _PublishConn:
    def __init__(self, locked: dict | None = None, mapping_payload: dict | None = None) -> None:
        self.executed: list[tuple] = []
        self.state_writes: list[str] = []
        self.state = "ready"
        self.mapping_payload = {} if mapping_payload is None else mapping_payload
        # What the FOR UPDATE re-read finds, by column name. It used to be a
        # positional 4-tuple against a six-column join, which is why the product
        # still carries `if len(row) > 4` fallbacks it will never take against a
        # real database.
        self.locked = dict(locked or {})
        self.committed = False

    def row_values(self, *, locked: bool = False) -> dict:
        values = {**_EXECUTION_VALUES, "state": self.state}
        if locked:
            # The locked re-read is the SNAPSHOT the act took, so it does not
            # follow the state machine forward -- that is the whole point of it.
            values.update({"state": "ready", **self.locked})
        return values

    def cursor(self):
        return _PublishCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    @property
    def sql(self) -> str:
        return "\n".join(str(sql) for sql, _ in self.executed)


def test_candidate_review_requires_real_isolated_nonempty_passing_artifact() -> None:
    review = build_candidate_review(_candidate())
    assert review["execution_id"] == "dse_01"
    assert len(review["review_hash"]) == 64
    for candidate_change in (
        {"placeholder": True},
        {"row_count": 0},
        {"artifact_ref": "shared/latest"},
        {"dq": {"blocking": ["schema_drift"]}},
    ):
        with pytest.raises(ActivationValidationError):
            build_candidate_review({**_candidate(), **candidate_change})


def test_the_publish_fake_refuses_a_statement_the_act_never_declared() -> None:
    """AI-317: the inventory of `_PublishCursor` is a claim, so it is asserted.

    Before this, the fake answered `self._result = None` to anything it did not
    recognise -- and `fetchone() is None` is how psycopg says NO SUCH ROW. Six
    reads of the publish act arrived that way: the pinned mapping payload, the
    stage and phase evidence, the versions re-read. Every one of them made the
    product take its "nothing there" branch, and the suite called that a proof.
    """
    cursor = _PublishCursor(_PublishConn())
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute("SELECT enabled FROM app.datastream_trial_allowances WHERE org_id=%s")
    assert "app.datastream_trial_allowances" in str(raised.value)
    assert "locked_join" in str(raised.value)


def test_the_publish_fake_projects_exactly_what_the_statement_asked_for() -> None:
    """The description is DERIVED, so a column added to the act needs no edit here."""
    cursor = _PublishCursor(_PublishConn())
    cursor.execute(
        "SELECT e.state,e.content_hash,e.row_count,d.current_published_execution_id,"
        "d.org_id,d.enabled FROM app.datastream_executions e JOIN app.datastreams d "
        "ON d.id=e.datastream_id WHERE e.id=%s FOR UPDATE"
    )
    assert [name for (name,) in cursor.description] == [
        "state",
        "content_hash",
        "row_count",
        "current_published_execution_id",
        "org_id",
        "enabled",
    ]
    assert cursor.fetchone() == ("ready", "f" * 64, 10, None, "org_1", False)


def test_publish_activate_mutation_swaps_all_pointers_and_lifecycle_together() -> None:
    # The locked re-read answers the WHOLE join now (AI-317): it used to be a
    # four-item tuple against a six-column SELECT, which is why the product still
    # carries `if len(row) > 4` fallbacks no real database will ever take. The
    # Outputs block remains a different subject, proven in the test below and
    # against a real warehouse.
    conn = _PublishConn()

    # This test's subject is the POINTER SWAP. Promotion is a cross-store act
    # against the warehouse and is proven where a warehouse exists --
    # `tests/integration/test_recurring_retrieval_arming_pg.py`. A blanket-mocked
    # cursor cannot answer for it, and letting it try would only measure the mock.
    with patch("core.datastream_activation._promote_managed_candidate", return_value=None):
        result = publish_activate_mutation(
            conn,
            review=build_candidate_review(_candidate()),
            actor="operator",
            operation_id="op_1",
        )

    sql = conn.sql
    assert "current_published_execution_id" in sql
    assert "current_plan_version_id" in sql
    assert "current_mapping_version_id" in sql
    assert "lifecycle_state='active'" in sql
    assert "next_run_at" in sql
    assert result.outcome == "succeeded"
    assert "with conn.transaction()" in inspect.getsource(execute_confirmed_operation)


def test_publish_activate_moves_the_run_through_the_one_state_machine() -> None:
    """AI-223: this act writes no `state` of its own, and the machine stamps it.

    Two private UPDATEs used to live here -- `state='publishing'` then
    `state='published'` -- and NEITHER wrote `state_changed_at`. The table has no
    trigger (migration 042 says so in its own header), so only the `DEFAULT now()`
    of the insertion held and a run published through the wizard measured its
    duration up to `ready`. Going through `advance_state` is what stamps the
    instant, closes the open step spans, and audits the change.
    """
    conn = _PublishConn()

    with patch("core.datastream_activation._promote_managed_candidate", return_value=None):
        publish_activate_mutation(
            conn,
            review=build_candidate_review(_candidate()),
            actor="operator",
            operation_id="op_1",
        )

    # The run crossed the machine, in order, with no shortcut from ready.
    assert conn.state_writes == ["publishing", "published"]
    assert conn.state == "published"
    # And every one of those writes carries the instant the private ones lost.
    state_writes = [
        sql for sql, _ in conn.executed
        if "UPDATE app.datastream_executions" in " ".join(str(sql).split())
        and "SET state" in " ".join(str(sql).split())
    ]
    assert state_writes, "no state write reached the connection"
    for sql in state_writes:
        assert "state_changed_at" in sql
    # The promotion happens BETWEEN the two, which is what `publishing` means.
    order = [" ".join(str(sql).split()) for sql, _ in conn.executed]
    assert any("app.datastream_execution_step_evidence" in sql for sql in order), (
        "the terminal transition must close the run's open step spans"
    )


def test_publish_activate_refuses_a_candidate_that_left_ready_in_its_own_words() -> None:
    """The machine's refusal is translated, not leaked.

    `advance_state` raises `invalid_state_transition`; the person publishing a
    candidate is told what changed under them, in this module's vocabulary.
    """
    conn = _PublishConn()
    conn.state = "loading"  # someone else moved it between the lock and the write

    with patch("core.datastream_activation._promote_managed_candidate", return_value=None):
        with pytest.raises(ActivationValidationError, match="Candidate changed"):
            publish_activate_mutation(
                conn,
                review=build_candidate_review(_candidate()),
                actor="operator",
                operation_id="op_1",
            )
    assert conn.state_writes == []


def _frozen_review() -> dict:
    return {
        "project_id": "proj_1",
        "draft_id": "dsd_1",
        "proposal_ref": "dspp_1",
        "proposal_hash": "a" * 64,
        "preview_ref": "dspv_1",
        "preview_dependency_hash": "b" * 64,
        "preview_evidence_hash": "c" * 64,
        "acknowledged_warning_ids": [],
        "confirmed_intent_bundle": {
            "datastream_name": "Daily performance",
            "data_role": "Performance",
            "joint_grain": ["date"],
            "field_mappings": [
                {
                    "field_id": "date",
                    "semantic_type": "date",
                    "included": True,
                    "profile": {
                        "nullable": False,
                        "unique": True,
                        "cardinality_signal": "unique",
                        "sample_values": [],
                        "confidence": 0.9,
                    },
                    "suggestion": {
                        "semantic_role": "primary_date",
                        "aggregation": "none",
                        "non_additive": False,
                        "currency": "unknown",
                        "sensitivity": "none",
                        "status": "suggested",
                        "evidence": ["kind:date"],
                    },
                    "binding": {
                        "canonical_target": None,
                        "mdm_target": None,
                        "status": "suggested",
                        "blocking_reason": None,
                        "confirmed_by": None,
                        "confirmed_reason": None,
                    },
                },
                {
                    "field_id": "spend",
                    "semantic_type": "number",
                    "included": True,
                    "profile": {
                        "nullable": False,
                        "unique": False,
                        "cardinality_signal": "medium",
                        "sample_values": [],
                        "confidence": 0.9,
                    },
                    "suggestion": {
                        "semantic_role": "measure_spend",
                        "aggregation": "sum",
                        "non_additive": False,
                        "currency": "EUR",
                        "sensitivity": "financial",
                        "status": "suggested",
                        "evidence": ["kind:metric"],
                    },
                    "binding": {
                        "canonical_target": "spend",
                        "mdm_target": None,
                        "status": "suggested",
                        "blocking_reason": None,
                        "confirmed_by": None,
                        "confirmed_reason": None,
                    },
                },
            ],
        },
    }


@pytest.mark.parametrize(
    ("operator_input", "activation_kind"),
    [
        (
            {
                "mode": "external_bq",
                "source": {
                    "object_ref": "analytics.raw.events",
                    "declared_writer": "Agency ETL",
                },
                "configure": {},
            },
            "schedule",
        ),
        (
            {
                "mode": "managed_feed",
                "source": {"channel": "file_upload", "staged_asset_ref": "dsa_1"},
                "configure": {},
            },
            "manual",
        ),
        (
            {
                "mode": "managed_feed",
                "source": {"channel": "webhook", "template_ref": "fst_1"},
                "configure": {},
                "schedule": {"expected_interval_minutes": 90},
            },
            "arrival_monitor",
        ),
    ],
)
def test_materialization_contract_is_mode_exact_and_confirms_every_field(
    operator_input: dict, activation_kind: str
) -> None:
    from core.datastream_field_mapping import normalize_mapping
    from core.datastream_intents import normalize_intent

    result = compile_materialization_contract(
        frozen_review=_frozen_review(),
        operator_input=operator_input,
        observation={"schema_hash": "d" * 64, "safe_metadata": {"detected_format": "csv"}},
        org_id="org_1",
        actor="person_1",
        timezone_name="Europe/Paris",
    )

    normalize_intent(result["plan_intent"])
    normalize_mapping(result["mapping_payload"])
    assert result["schedule"]["activation_kind"] == activation_kind
    assert {field["binding"]["status"] for field in result["mapping_payload"]["fields"]} == {
        "confirmed"
    }
    assert all(
        field["binding"]["confirmed_by"] == "person_1"
        for field in result["mapping_payload"]["fields"]
    )


# ---------------------------------------------------------------------------
# Story 60.6 / AI-249 -- excluding ONE column used to make activation impossible.
#
# The compiler skipped every field whose `included` flag was not exactly True and
# then compared what it kept against the WHOLE list, so the first person to
# exclude a column got "Every included physical field must be reviewed" -- a
# refusal accusing them of not reviewing what they had just decided.
#
# The two cases are read SEPARATELY on purpose: a single test asserting "it
# activates" would pass just as well against a compiler that had lost the refusal
# altogether, and the refusal is what stops an unreviewed field from landing.
# ---------------------------------------------------------------------------


def _review_with_excluded(field_id: str) -> dict:
    """The frozen review, with one of its two columns EXCLUDED by its binding."""
    review = _frozen_review()
    for field in review["confirmed_intent_bundle"]["field_mappings"]:
        if field["field_id"] == field_id:
            field["binding"]["status"] = "excluded"
    return review


def test_an_excluded_column_activates_and_leaves_the_mapping_version() -> None:
    """Exclusion is a DECISION, and the compiler now accepts it.

    `spend` is excluded, `date` stays. The contract compiles, and the excluded
    column is absent from the mapping version -- which is what makes the three
    projection gates, the file import and the fail-closed MDM skip it downstream:
    they all read `binding.status == "excluded"` on this same payload.
    """
    from core.datastream_field_mapping import normalize_mapping

    result = compile_materialization_contract(
        frozen_review=_review_with_excluded("spend"),
        operator_input={
            "mode": "managed_feed",
            "source": {"channel": "file_upload", "staged_asset_ref": "dsa_1"},
            "configure": {},
        },
        observation={"schema_hash": "d" * 64, "safe_metadata": {"detected_format": "csv"}},
        org_id="org_1",
        actor="person_1",
        timezone_name="Europe/Paris",
    )

    normalize_mapping(result["mapping_payload"])
    assert [f["field_id"] for f in result["mapping_payload"]["fields"]] == ["date"]
    # And the excluded measure is not selected for collection either: the plan
    # asks the source for what will land, and nothing else.
    assert result["plan_intent"]["source"]["selection"]["metrics"] == []


def test_an_included_field_with_no_binding_decision_still_refuses_activation() -> None:
    """The refusal survives for exactly what it was meant to catch.

    A field entry carrying no binding at all is a column nobody said anything
    about -- not an intentional exclusion, and not a reviewed inclusion.
    """
    review = _frozen_review()
    review["confirmed_intent_bundle"]["field_mappings"].append(
        {"field_id": "impressions", "semantic_type": "number"}
    )

    with pytest.raises(ActivationValidationError) as raised:
        compile_materialization_contract(
            frozen_review=review,
            operator_input={
                "mode": "managed_feed",
                "source": {"channel": "file_upload", "staged_asset_ref": "dsa_1"},
                "configure": {},
            },
            observation={"schema_hash": "d" * 64, "safe_metadata": {"detected_format": "csv"}},
            org_id="org_1",
            actor="person_1",
            timezone_name="Europe/Paris",
        )

    assert "Every included physical field must be reviewed" in str(raised.value)


def test_the_included_flag_no_longer_decides_anything() -> None:
    """Arbitrage 2: one flag, and it is `binding.status`.

    A field flagged `included: False` while its binding says nothing of the kind
    is INCLUDED -- otherwise two keys would go on saying two different things,
    which is the state this story exists to end.
    """
    review = _frozen_review()
    for field in review["confirmed_intent_bundle"]["field_mappings"]:
        field["included"] = False

    result = compile_materialization_contract(
        frozen_review=review,
        operator_input={
            "mode": "managed_feed",
            "source": {"channel": "file_upload", "staged_asset_ref": "dsa_1"},
            "configure": {},
        },
        observation={"schema_hash": "d" * 64, "safe_metadata": {"detected_format": "csv"}},
        org_id="org_1",
        actor="person_1",
        timezone_name="Europe/Paris",
    )

    assert [f["field_id"] for f in result["mapping_payload"]["fields"]] == ["date", "spend"]


def test_candidate_adapter_proof_advances_only_after_isolated_verified_artifact() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = ("created", {"executable": True, "issues": []})
    cur.rowcount = 1
    result = {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": "external_bq.readonly.candidate.v1",
        "artifact_ref": "candidate/dse_01/relation",
        "artifact_hash": "a" * 64,
        "content_hash": "b" * 64,
        "validated_content_hash": "b" * 64,
        "row_count": 12,
        "schema_hash": "c" * 64,
        "dq": {"blocking": []},
    }
    with (
        patch("core.datastream_publication.advance_state") as advance,
        patch("core.datastream_publication.run_dq_gates", return_value=[]) as gates,
    ):
        completed = complete_candidate_from_adapter(
            conn,
            project_id="proj_1",
            datastream_id="ds_1",
            execution_id="dse_01",
            actor="person_1",
            adapter_result=result,
        )

    assert completed["state"] == "ready"
    assert [call.args[2] for call in advance.call_args_list] == ["loading", "validating", "ready"]
    assert gates.call_args.kwargs["validated_content_hash"] == "b" * 64
    persisted = next(
        call for call in cur.execute.call_args_list if "candidate_evidence" in call.args[0]
    )
    assert "candidate/dse_01/relation" not in str(persisted.args[1][3])


def test_candidate_schema_hash_reads_either_name_and_refuses_anything_else() -> None:
    """One fact, two names, and no near-values.

    The adapter path records it as `schema_hash`, the managed-file path as
    `candidate_schema_fingerprint`. Both are `sha256(...).hexdigest()`, and the
    column that receives it CHECKs `^[0-9a-f]{64}$` (migration 138) -- a
    violation aborts the whole publish transaction, so anything else must
    become None, not a best effort.
    """
    assert candidate_schema_hash({"schema_hash": "a" * 64}) == "a" * 64
    assert candidate_schema_hash({"candidate_schema_fingerprint": "b" * 64}) == "b" * 64
    assert candidate_schema_hash({}, {"schema_hash": "c" * 64}) == "c" * 64
    for refused in ("A" * 64, "a" * 63, "sha256:" + "a" * 64, "", None, 64):
        assert candidate_schema_hash({"schema_hash": refused}) is None


def test_published_output_version_carries_the_schema_hash_it_already_had() -> None:
    """The Outputs tab compares `v.schema_hash`; nothing ever wrote it.

    `complete_candidate_from_adapter` persists the fingerprint into
    `candidate_evidence`, but the `datastream_output_versions` INSERT listed
    fifteen columns and that was not one of them. So every published row had
    `schema_hash IS NULL`, both sides of the publication diff read
    "Unavailable", and the Schema line said "Not comparable" on the screen that
    decides a promotion.
    """
    # `row[4]` is the org_id: without it the whole Outputs block is skipped.
    conn = _PublishConn({"org_id": "org_1"})

    review = build_candidate_review({**_candidate(), "schema_hash": "d" * 64})
    assert review["schema_hash"] == "d" * 64

    with (
        patch("core.datastream_workbench.append_stage_evidence") as stage,
        patch("core.datastream_workbench.append_phase_evidence"),
        # Same reason as above: the subject here is the schema hash on the Output
        # row, and promotion is proven against a real warehouse.
        patch("core.datastream_activation._promote_managed_candidate", return_value=None),
    ):
        publish_activate_mutation(conn, review=review, actor="operator", operation_id="op_1")

    insert = next(
        (sql, params)
        for sql, params in conn.executed
        if "INSERT INTO app.datastream_output_versions" in str(sql)
    )
    assert "schema_hash" in insert[0]
    assert insert[0].count("%s") == len(insert[1])
    assert insert[1].count("d" * 64) == 1
    # The evidence blob must not relabel the CONTENT hash as a schema hash.
    evidence = json.loads(insert[1][-2])
    assert evidence["schema_hash"] == "d" * 64
    # And the published stage evidence carries the same fact.
    assert stage.call_args.kwargs["evidence"]["schema_hash"] == "d" * 64


def test_review_reads_the_schema_hash_from_whichever_table_recorded_it() -> None:
    """Adapter evidence or managed-file dispatch: the review carries both."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    def _row(evidence: dict, dispatch_hash: str | None) -> tuple:
        return (
            "ready",
            "candidate/dse_01/output",
            "f" * 64,
            10,
            "dsp_1",
            "dmap_1",
            {"kind": "full_grain"},
            {"adapter_verified": True, "placeholder": False, **evidence},
            None,
            {"schedule": {"schedule_mode": "nightly", "activation_kind": "schedule"}},
            "nightly",
            dispatch_hash,
        )

    cur.fetchone.return_value = _row({"schema_hash": "e" * 64}, None)
    adapter = read_candidate_review(
        conn, project_id="proj_1", datastream_id="ds_1", execution_id="dse_01"
    )
    assert adapter["schema_hash"] == "e" * 64

    cur.fetchone.return_value = _row({}, "9" * 64)
    managed = read_candidate_review(
        conn, project_id="proj_1", datastream_id="ds_1", execution_id="dse_01"
    )
    assert managed["schema_hash"] == "9" * 64

    cur.fetchone.return_value = _row({}, None)
    unknown = read_candidate_review(
        conn, project_id="proj_1", datastream_id="ds_1", execution_id="dse_01"
    )
    assert unknown["schema_hash"] is None, "an unknown hash stays unknown, never borrowed"


def test_materialization_persists_exact_source_project_link_role_and_cadence() -> None:
    activation = __import__("core.datastream_activation", fromlist=["x"])
    source = inspect.getsource(activation.materialize_draft_mutation)

    assert "connection_ref_id" in source
    assert "module_name" in source
    assert "INSERT INTO app.project_flux" in source
    assert "schedule_mode" in source
    assert '"Finance"' not in source
    assert '"Operations"' not in source


def test_connector_contract_freezes_exact_source_account_and_runtime_cadence() -> None:
    result = compile_materialization_contract(
        frozen_review=_frozen_review(),
        operator_input={
            "mode": "connector_pull",
            "source": {"report_ref": "daily", "account_ref": "account_7"},
            "configure": {"cadence_intent": "daily"},
        },
        observation={"schema_hash": "d" * 64},
        org_id="org_1",
        actor="person_1",
        timezone_name="Europe/Paris",
        connector_capabilities={
            "connection_ref_id": "conn_7",
            "selected_account_ref": "account_7",
            "module": {"name": "meta-ads"},
            "reports": [
                {
                    "id": "daily",
                    "selection_mode": "subset",
                    "availability": {"status": "selectable"},
                }
            ],
        },
    )

    source = result["plan_intent"]["source"]
    assert source["module"] == "meta-ads"
    assert source["connection_ref_id"] == "conn_7"
    assert source["selected_account_ref"] == "account_7"
    assert result["plan_intent"]["schedule"]["mode"] == "daily"


def test_runtime_activation_adapters_are_registered_for_every_supported_mode() -> None:
    from inbound.datastream_activation_worker import registered_activation_adapters

    registrations = registered_activation_adapters()
    for mode in ("connector_pull", "external_bq", "managed_feed"):
        assert ("setup_preview", mode, "*") in registrations
        assert ("candidate_materialization", mode, "*") in registrations


def test_scheduler_requires_active_lifecycle_and_coherent_active_versions() -> None:
    nightly = inspect.getsource(scheduler._dispatch_nightly_datastreams)
    hourly = inspect.getsource(scheduler._dispatch_hourly_datastreams)
    for source in (nightly, hourly):
        assert "lifecycle_state = 'active'" in source
        assert "current_plan_version_id IS NOT NULL" in source
        assert "current_mapping_version_id IS NOT NULL" in source
        assert "plan_version_id = ds.current_plan_version_id" in source


def test_unsafe_direct_publication_routes_are_not_mounted() -> None:
    paths = {route.path for route in admin_api.router.routes}

    assert "/api/projects/{project_id}/datastreams/{ds_id}/publish" not in paths
    assert "/api/datastreams/{id}/mapping/versions" not in {
        route.path for route in admin_api.router.routes if "POST" in route.methods
    }
    assert "/api/datastreams/{id}/executions/{exec_id}/publish" not in paths


def test_ai75_connector_pull_activates_nightly_scheduler_eligibility() -> None:
    """AI-75: pull and Sheets Datastreams default to daily without an explicit cadence."""
    operator_input = {
        "mode": "connector_pull",
        "source": {
            "account_ref": "account_7",
            "report_ref": "rep_1",
        },
        "selection": {"metrics": ["spend"], "dimensions": ["date"]},
        "joint_grain": ["date"],
        "field_mappings": [
            {
                "field_id": "date",
                "included": True,
                "suggestion": {"semantic_role": "dimension"},
                "binding": {"status": "suggested"},
            },
            {
                "field_id": "spend",
                "included": True,
                "suggestion": {"semantic_role": "measure_financial"},
                "binding": {"status": "suggested"},
            },
        ],
    }
    capabilities = {
        "connection_ref_id": "conn_7",
        "module": {"name": "meta-ads"},
        "reports": [{"id": "rep_1", "selection_mode": "subset"}],
    }
    contract = compile_materialization_contract(
        frozen_review=_frozen_review(),
        operator_input=operator_input,
        observation={"schema_hash": "a" * 64},
        org_id="org_test",
        actor="user_1",
        timezone_name="Europe/Paris",
        connector_capabilities=capabilities,
    )

    assert contract["plan_intent"]["schedule"]["mode"] == "daily"




# ---------------------------------------------------------------------------
# Story 57.3 -- the arrival expectation is DECLARED or it is absent. Never 1440.
# ---------------------------------------------------------------------------


def _inbound_review(declared: dict | None) -> dict:
    """A reviewed webhook feed whose channel contract declares `declared`."""
    source = {"channel": "webhook", "template_ref": "fst_1"}
    if declared is not None:
        source["channel_contract"] = declared
    return compile_materialization_contract(
        frozen_review=_frozen_review(),
        operator_input={"mode": "managed_feed", "source": source, "configure": {}},
        observation={"schema_hash": "d" * 64, "safe_metadata": {"detected_format": "csv"}},
        org_id="org_1",
        actor="person_1",
        timezone_name="Europe/Paris",
    )


def test_an_undeclared_arrival_expectation_is_absent_and_never_defaulted() -> None:
    """`or 1440` promised a daily delivery on the operator's behalf.

    `dq_monitors._check_arrival_timeliness` then fired -- or stayed silent --
    against a number nobody gave. The review now says the expectation is not
    declared, and carries no interval at all.
    """
    from core.datastream_intents import normalize_intent

    review = _inbound_review(None)
    schedule = review["schedule"]

    assert schedule["activation_kind"] == "arrival_monitor"
    assert schedule["expected_interval_minutes"] is None
    assert schedule["arrival_expectation"] == "not_declared"
    assert "1440" not in str(schedule)
    normalize_intent(review["plan_intent"])


def test_a_declared_arrival_expectation_travels_exactly_as_declared() -> None:
    """The declaration of step 1 is what arms the monitor -- to the minute."""
    from core.datastream_intents import normalize_intent

    review = _inbound_review({"expected_interval_minutes": 360})

    assert review["schedule"]["expected_interval_minutes"] == 360
    assert review["schedule"]["arrival_expectation"] == "declared"
    assert (
        review["plan_intent"]["source"]["managed_feed"]["channel_contract"][
            "expected_interval_minutes"
        ]
        == 360
    )
    normalize_intent(review["plan_intent"])


def test_the_declared_sender_list_reaches_the_plan_normalized() -> None:
    """A declaration stored nowhere would govern nothing.

    It travels to the plan so materialization can put it on the Datastream row,
    which is the only place `inbound_processing` can read it from.
    """
    from core.datastream_intents import normalize_intent

    review = _inbound_review(
        {"allowed_senders": ["  Reports@Agency.invalid ", "reports@agency.invalid"]}
    )
    contract = review["plan_intent"]["source"]["managed_feed"]["channel_contract"]

    assert contract["allowed_senders"] == ["reports@agency.invalid"]
    assert "expected_interval_minutes" not in contract
    normalize_intent(review["plan_intent"])


def test_the_arrival_monitor_row_is_written_only_when_an_interval_was_declared() -> None:
    """No declaration, NO ROW -- and the Datastream reports no lateness.

    This is the half that proves `1440` is gone from the WRITE as well as from
    the review. The publish path needs a live database, so the guard is read in
    the source that carries it -- the same way the adapter-coverage suite proves
    a closure is really built rather than merely declared.
    """
    import inspect
    from pathlib import Path

    import core.datastream_activation as activation_module

    module_source = Path(inspect.getfile(activation_module)).read_text(encoding="utf-8")
    insert_at = module_source.index("INSERT INTO app.datastream_arrival_monitors")
    guard = module_source[insert_at - 900 : insert_at]

    assert 'activation_kind == "arrival_monitor" and not schedule.get(' in guard, (
        "l'insertion du moniteur ne verifie plus qu'une attente a ete declaree"
    )
    assert "NOT armed" in guard, (
        "un moniteur non arme doit le DIRE ; un silence se lit comme une surveillance"
    )
    # `1440` reste legitime AILLEURS -- une cadence `daily` fait bien 1440
    # minutes. Ce qui ne doit jamais revenir, c'est un defaut d'ATTENTE
    # D'ARRIVEE : la ligne executable qui nomme les deux ensemble.
    executable = [
        line for line in module_source.splitlines() if not line.strip().startswith("#")
    ]
    assert not [
        line for line in executable if "expected_interval_minutes" in line and "1440" in line
    ], "le defaut `or 1440` est revenu sur l'attente d'arrivee"


# ---------------------------------------------------------------------------
# Story 57.3 -- the walk: a channel chosen at step 1 can actually RECEIVE.
#
# `config.channels` was never written by materialization, and nothing on this
# path read it, so nobody saw that it was empty. Story 57.3 made step 1 say "the
# address is issued against the Datastream, on its Overview, once this draft is
# created" -- and that sentence was false at the very next link:
# `_get_datastream_status` returns an EMPTY channel set for a row with no
# `config.channels`, and `_require_receivable` then raises "delivery channel is
# not configured". No address could ever be issued for a Datastream the wizard
# created.
#
# These tests walk it end to end rather than assert on a dict: the channel picked
# in the wizard, through the real compiler, the real config writer, the real
# status reader and the real receivability guard. Only the database row is a
# double, and it is built FROM the config the writer produced -- never typed by
# hand, which is how a walk stops proving anything.
# ---------------------------------------------------------------------------


class _DatastreamRowCursor:
    """The five columns `_get_datastream_status` really selects, no more."""

    def __init__(self, config: dict, *, lifecycle_state: str = "draft"):
        self._row = (
            config.get("connector_name") or "managed_feed",
            "org_1",
            False,
            lifecycle_state,
            config.get("channels", []),
        )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        assert "FROM app.datastreams" in sql

    def fetchone(self):
        return self._row


class _RowConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _materialized_config(channel: str, declared: dict | None = None) -> dict:
    """The config a materialization writes for a channel chosen at step 1."""
    from core.datastream_activation import datastream_config

    source: dict = {"channel": channel, "template_ref": "fst_1"}
    if declared is not None:
        source["channel_contract"] = declared
    review = compile_materialization_contract(
        frozen_review=_frozen_review(),
        operator_input={"mode": "managed_feed", "source": source, "configure": {}},
        observation={"schema_hash": "d" * 64, "safe_metadata": {"detected_format": "csv"}},
        org_id="org_1",
        actor="person_1",
        timezone_name="Europe/Paris",
    )
    contract = review["plan_intent"]["source"]
    return datastream_config(contract, source_owner={"selected_account_ref": None})


@pytest.mark.parametrize(
    ("wizard_channel", "credential_channel"),
    [("inbound_email", "email"), ("webhook", "webhook")],
)
def test_a_channel_chosen_in_the_wizard_can_actually_receive(
    wizard_channel: str, credential_channel: str
) -> None:
    """THE MARCH: step 1 -> plan -> config -> status -> receivable, no exception.

    Before this, `_require_receivable` raised "delivery channel is not
    configured" on every Datastream the wizard had ever created, so the address
    the screen promises could never be issued.
    """
    from core.inbound_credentials import _get_datastream_status, _require_receivable

    config = _materialized_config(wizard_channel)
    info = _get_datastream_status(
        _RowConn(_DatastreamRowCursor(config)), datastream_id="ds_1"
    )

    # The vocabulary that survives the reader's own translation, pinned so the
    # next rename breaks a test instead of breaking delivery in silence.
    assert config["channels"] == [credential_channel]
    assert info["channels"] == {credential_channel}
    _require_receivable(info, channel=credential_channel)


def test_a_channel_that_receives_nothing_declares_no_delivery_channel() -> None:
    """An upload and a sheet are read, not delivered to. Neither holds a credential.

    Listing them would promise an address that nothing can ever issue -- the
    mirror image of the defect above.
    """
    from core.inbound_credentials import (
        InboundCredentialUnavailable,
        _get_datastream_status,
        _require_receivable,
    )

    # AI-321 (2026-08-29): `file_upload` DOES declare `upload` -- it is the
    # ingress `inbound_ingest` admits by -- and that still issues NO address:
    # an email credential is refused on it exactly as before. Sheets declare
    # nothing, they are read.
    for channel, declared in (("file_upload", {"upload"}), ("google_sheets", set())):
        config = _materialized_config(channel)
        assert set(config.get("channels") or []) == declared
        info = _get_datastream_status(
            _RowConn(_DatastreamRowCursor(config)), datastream_id="ds_1"
        )
        # The CREDENTIAL reader keeps only deliverable channels: `upload` is an
        # ingress, not an address, so it sees none -- and refuses to issue one.
        assert info["channels"] == set()
        with pytest.raises(InboundCredentialUnavailable):
            _require_receivable(info, channel="email")


def test_the_declared_contract_and_the_delivery_channel_travel_together() -> None:
    """One row carries both halves: what may arrive, and who may send it.

    `inbound_processing` reads the allowlist from this same config, so a config
    that carried the channel without the contract would receive from anyone.
    """
    from core.inbound_sender_policy import read_allowed_senders

    config = _materialized_config(
        "inbound_email",
        {"expected_interval_minutes": 720, "allowed_senders": ["reports@agency.invalid"]},
    )

    assert config["channels"] == ["email"]
    assert config["channel_contract"]["allowed_senders"] == ["reports@agency.invalid"]
    assert config["channel_contract"]["expected_interval_minutes"] == 720
    assert read_allowed_senders(
        _RowConn(_ConfigOnlyCursor(config)), datastream_id="ds_1"
    ) == ["reports@agency.invalid"]


class _ConfigOnlyCursor:
    """The single `config` column `read_allowed_senders` selects."""

    def __init__(self, config: dict):
        self._config = config

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        assert "app.datastreams" in sql

    def fetchone(self):
        return (self._config,)


# ---------------------------------------------------------------------------
# AI-217 -- a weekly plan compiles, and reaches the stable row as `weekly`.
# ---------------------------------------------------------------------------


def _weekly_contract() -> dict:
    return compile_materialization_contract(
        frozen_review=_frozen_review(),
        operator_input={
            "mode": "connector_pull",
            "source": {"account_ref": "account_7", "report_ref": "rep_1"},
            "configure": {},
            "schedule": {"mode": "weekly", "arrival_hour": 6},
        },
        observation={"schema_hash": "a" * 64},
        org_id="org_test",
        actor="user_1",
        timezone_name="Europe/Paris",
        connector_capabilities={
            "connection_ref_id": "conn_7",
            "module": {"name": "meta-ads"},
            "reports": [{"id": "rep_1", "selection_mode": "subset"}],
        },
    )


def test_a_weekly_plan_compiles_instead_of_raising_on_its_own_window() -> None:
    """The review already accepted `weekly` and then died computing its window.

    `compile_materialization_contract` validated the mode, set 10080 minutes and
    kept the arrival hour -- then called `calculate_schedule_window`, which
    raised `ValueError: unsupported schedule mode: weekly`. Half the vocabulary
    was in place and the other half refused it.
    """
    contract = _weekly_contract()

    assert contract["plan_intent"]["schedule"]["mode"] == "weekly"
    assert contract["plan_intent"]["schedule"]["interval_minutes"] == 10080
    assert contract["schedule"]["activation_kind"] == "schedule"
    assert contract["schedule"]["next_run_at"] is not None


def test_the_cadence_reaches_the_stable_row_through_one_table() -> None:
    """`{"daily": "nightly", "nightly": "nightly", "hourly": "hourly"}.get(m, "manual")`.

    That dictionary existed in THREE copies -- `materialize_draft_mutation`,
    `read_candidate_review` and the activation route in `admin_api` -- and its
    default is the defect: an unlisted cadence became `manual`, so a weekly plan
    activated a Datastream that runs on demand only, the opposite of what the
    person chose, written into the column the dispatcher reads. Three copies is
    also why one omission became three: the vocabulary now lives in one place
    (`core.datastreams.schedule_mode_for_cadence`).
    """
    from core import admin_api, datastream_activation

    for module in (datastream_activation, admin_api):
        source = inspect.getsource(module)
        assert '"daily": "nightly"' not in source, (
            f"{module.__name__} still carries its own cadence table -- the next "
            "cadence added to the constraint will be forgotten in it again"
        )
    assert "schedule_mode_for_cadence" in inspect.getsource(datastream_activation)
    # AD-40 : la porte PATCH est partie avec la famille `/api/datastreams/…`.
    assert "schedule_mode_for_cadence" in inspect.getsource(datastreams_api)


def test_a_weekly_plan_keeps_its_arrival_hour() -> None:
    """A3 names `nightly` and `weekly` together, on every door but this one."""
    contract = _weekly_contract()

    assert contract["plan_intent"]["schedule"]["run_at_hour"] == 6
    assert contract["schedule"]["arrival_hour_local"] == 6


def test_a_feed_awaiting_its_first_delivery_materializes_without_versions(monkeypatch) -> None:
    """The Datastream exists and can receive; its plan and mapping do not exist yet.

    An inbound channel has no file until a delivery arrives, an address is only
    issued against a MATERIALIZED Datastream, and
    `inbound_credentials._require_receivable` already allows exactly that state:
    "Allow draft discovery and active intake". So materialization writes the row
    and stops -- no plan version, no mapping version, no candidate. Migration 224
    made those three columns sayable as absent, and its CHECK keeps them
    all-or-nothing so a half-versioned materialization stays impossible.

    What is NOT deferred is the plan intent: it carries the channel, the declared
    arrival expectation and the sender allowlist -- the tests above pin all
    three, and they are what an inbound Datastream is made of.
    """
    from core.datastream_activation import awaits_first_delivery

    assert awaits_first_delivery(
        {"mode": "managed_feed", "source": {"channel": "inbound_email"}}
    )
    assert awaits_first_delivery({"mode": "managed_feed", "source": {"channel": "webhook"}})
    # The deferral belongs to the CHANNEL: a staged upload has its file already.
    assert not awaits_first_delivery(
        {"mode": "managed_feed", "source": {"channel": "file_upload"}}
    )
    assert not awaits_first_delivery(
        {"mode": "connector_pull", "source": {"channel": "inbound_email"}}
    )


def test_a_materialized_inbound_datastream_names_the_connector_it_receives_through() -> None:
    """Writing the channel was half the hole 57.3 named; this is the other half.

    `inbound_credentials` resolves the connector as
    `COALESCE(config->>'connector_name', module_name)` (:363, :1262), and a
    managed feed has no `module_name`. So `datastream_matches_connector` answered
    False and `_require_receivable` refused a Datastream that was otherwise
    receivable -- measured 2026-08-07 on the first inbound Datastream the
    assistant ever materialised: `POST .../credentials` -> 500.

    `managed_feed` is what `app.connector_installations`,
    `app.connector_domain_configs` and `app.connector_activations` all carry, and
    what the other writer has always written. One value, one meaning.
    """
    from core.datastream_activation import datastream_config

    config = datastream_config(
        {"kind": "managed_feed", "managed_feed": {"channel": "inbound_email"}},
        source_owner={},
    )
    assert config["channels"] == ["email"]
    assert config["connector_name"] == "managed_feed"

    # AI-321 (2026-08-29): an uploaded feed declares `upload` -- the channel
    # `inbound_ingest` admits an ingress by, and the one the upload door has
    # always written. Without it the assistant's file Datastream refused the
    # very upload it was created for (`allowed: []`). Sheets stay undeclared.
    staged = datastream_config(
        {"kind": "managed_feed", "managed_feed": {"channel": "file_upload"}},
        source_owner={},
    )
    assert staged["channels"] == ["upload"]
    assert staged["connector_name"] == "managed_feed"
    sheets = datastream_config(
        {"kind": "managed_feed", "managed_feed": {"channel": "google_sheets"}},
        source_owner={},
    )
    assert "channels" not in sheets


def test_a_materialized_inbound_datastream_is_completed_not_duplicated() -> None:
    """Le raccord : un flux entrant recoit son mapping APRES sa materialisation.

    Une adresse entrante ne s'emet que contre un Datastream materialise, donc le
    flux existe avant que son fichier ait revele sa forme. Le geste qui pose plan
    et mapping vient plus tard -- et sans ce raccord il CREAIT un second
    Datastream a cote du premier : l'adresse emise, les evidences retenues et le
    nouveau mapping se retrouvaient sur deux lignes differentes, aucune complete.

    La condition est le couple de pointeurs VIDES. Un flux qui en a deja n'attend
    rien, et ecrire par-dessus serait une reprise silencieuse -- exactement ce
    qu'une porte de completion ne doit jamais devenir.
    """
    from core.datastream_activation import _deferred_datastream_awaiting_setup

    class _Conn:
        def __init__(self, rows):
            self._rows, self._n = rows, 0

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, _sql, _params=None):
            pass

        def fetchone(self):
            row = self._rows[self._n] if self._n < len(self._rows) else None
            self._n += 1
            return row

    review = {"draft_id": "dsd_1", "project_id": "proj_1"}

    # Materialise, et sans aucun pointeur : c'est lui qui attend.
    assert (
        _deferred_datastream_awaiting_setup(
            _Conn([("ds_waiting",), (None, None)]), review
        )
        == "ds_waiting"
    )
    # Deja des pointeurs : ce n'est pas un raccord, c'est une reprise. Refuse.
    assert (
        _deferred_datastream_awaiting_setup(
            _Conn([("ds_done",), ("dsp_1", "dsm_1")]), review
        )
        is None
    )
    # Un seul pointeur est un etat que rien ne produit : ne pas le masquer.
    assert (
        _deferred_datastream_awaiting_setup(
            _Conn([("ds_half",), ("dsp_1", None)]), review
        )
        is None
    )
    # Rien de materialise : creation ordinaire.
    assert _deferred_datastream_awaiting_setup(_Conn([(None,)]), review) is None
