"""Bound key occurrences get persisted verdicts, on a real base (Story 68.3).

WHY THIS FILE EXISTS. `tests/core/test_entity_key_matching.py` proves the
taxonomy and the arithmetic off base. What only a database can show is the
half the acceptance criteria are about:

  * a landed pull writes ONE `current` verdict row per occurrence, with its
    evidence and its provenance (AC1);
  * a replayed pull writes NOTHING -- the coverage fraction must not inflate;
    the SAME occurrence answering DIFFERENTLY supersedes, append-only (AC1);
  * `matching_coverage` answers N of M per (datastream, entity type), reading
    only `current` rows (AC2);
  * an ambiguous occurrence persists its candidates and names no node (AC3);
  * and a Datastream that designates nothing writes nothing at all.

The warehouse read (`verification.distinct_raw_values`) is the ONE thing
stubbed: it is another module's contract, tested where it lives, and it needs
a warehouse this suite has no business standing up. Everything the story owns
-- resolution, persistence, supersession, coverage -- runs for real.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from core import entity_key_matching as ekm  # noqa: E402
from core import master_data  # noqa: E402
from core import object_kind_registry as okr  # noqa: E402

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres verdict test skipped",
)

ACTOR = "alice@example.com"
KIND = "video"
FIELD = "video_id"
WINDOW = ("2026-08-01", "2026-08-07")

MAPPING_PAYLOAD = {
    "grain": ["date", FIELD],
    "fields": [
        {"field_id": "date", "physical_type": "date", "binding": {"status": "confirmed"}},
        {
            "field_id": FIELD,
            "physical_type": "string",
            "binding": {"status": "confirmed", "designates_object_kind": KIND},
        },
        {"field_id": "views", "physical_type": "integer", "binding": {"status": "confirmed"}},
    ],
}
UNDESIGNATED_PAYLOAD = {
    "grain": ["date"],
    "fields": [
        {"field_id": "date", "physical_type": "date", "binding": {"status": "confirmed"}}
    ],
}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _observe(monkeypatch, values):
    """Stub the warehouse read: {field_id: {raw_value: occurrence_count}}."""

    def fake(pull_id, fields, **kwargs):
        return {FIELD: dict(values)} if FIELD in fields else {}

    monkeypatch.setattr("core.verification.distinct_raw_values", fake)


@pytest.fixture()
def world(live_postgres):
    """A Datastream pinning a designating mapping, and a declared entity type."""
    from tests.integration.epic66_fixtures import make_project

    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 68.3")
    ds_id, plan_id, mapping_id = _id("ds_"), _id("dsp_"), _id("dmap_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'Video pulls', NULL, 'managed_feed', FALSE, %s, %s)
            """,
            (ds_id, project_id, ACTOR, org_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'managed_feed', 'toorow', 'managed_raw',
                    '{}'::jsonb, repeat('a', 64), repeat('b', 64), %s)
            """,
            (plan_id, ds_id, project_id, ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',
                    '1', TRUE, %s::jsonb, '{}'::jsonb, repeat('d', 64), %s)
            """,
            (
                mapping_id,
                ds_id,
                project_id,
                plan_id,
                psycopg.types.json.Json(MAPPING_PAYLOAD),
                ACTOR,
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (mapping_id, ds_id),
        )
    declared = okr.declare_entity_type(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=KIND,
        canonical_key=FIELD,
        display_name="Videos",
        actor=ACTOR,
    )
    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "datastream": {"id": ds_id, "module_name": None, "report_profile_id": None},
        "datastream_id": ds_id,
        "mapping_version_id": mapping_id,
        "registry": declared["registry"],
    }


def _node(world, label, *, aliases=()):
    conn = world["conn"]
    node = master_data.create_node(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        registry_id=world["registry"]["id"],
        node_kind=KIND,
        label=label,
        actor=ACTOR,
    )
    for alias in aliases or (label,):
        master_data.record_alias(
            conn,
            org_id=world["org_id"],
            node_id=node["id"],
            namespace=f"ns_{uuid.uuid4().hex[:8]}",
            raw_value=alias,
            relation="exact",
            actor=ACTOR,
        )
    return str(node["id"])


def _run(world, pull_id="pull_1"):
    return ekm.record_verdicts_for_pull(
        world["conn"],
        datastream=world["datastream"],
        project_id=world["project_id"],
        pull_id=pull_id,
        date_from=WINDOW[0],
        date_to=WINDOW[1],
        actor=ACTOR,
        org_id=world["org_id"],
    )


def _verdicts(world, *, state="current"):
    clause = "" if state is None else " AND state = %s"
    params = [world["project_id"]] + ([] if state is None else [state])
    with world["conn"].cursor() as cur:
        cur.execute(
            """
            SELECT normalized_value, verdict, node_id, candidates, reason_code,
                   occurrence_count, execution_id, mapping_version_id, state
              FROM app.entity_key_match_verdicts
             WHERE project_id = %s
            """
            + clause
            + " ORDER BY normalized_value, created_at",
            params,
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# AC1: one verdict per occurrence, with evidence and provenance.
# ---------------------------------------------------------------------------


@requires_postgres
def test_every_occurrence_of_a_bound_key_gets_a_persisted_verdict(world, monkeypatch):
    _node(world, "v-1")
    _observe(monkeypatch, {"v-1": 12, "v-unknown": 3})

    assert _run(world) == 2

    rows = {row[0]: row for row in _verdicts(world)}
    assert set(rows) == {"v-1", "v-unknown"}
    assert rows["v-1"][1] == ekm.VERDICT_RESOLVED
    assert rows["v-1"][2] is not None
    assert rows["v-1"][5] == 12
    # AD-7: the verdict names the pull and the pinned mapping that bound it.
    assert rows["v-1"][6] == "pull_1"
    assert rows["v-1"][7] == world["mapping_version_id"]
    # An unmatched occurrence is a ROW, not an absence: "we looked and found
    # nothing" and "we never looked" must never read the same.
    assert rows["v-unknown"][1] == ekm.VERDICT_UNMATCHED
    assert rows["v-unknown"][2] is None


@requires_postgres
def test_a_datastream_that_designates_nothing_writes_nothing(world, monkeypatch):
    # Mapping versions are immutable (trigger), so this is a SECOND version
    # the Datastream pins instead -- the way a Project actually drops a
    # designation.
    plain_id = _id("dmap_")
    with world["conn"].cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            SELECT %s, datastream_id, project_id, 2, '1', source_schema_hash,
                   plan_version_id, repeat('e', 64), ossie_spec_version,
                   toorow_extension_version, TRUE, %s::jsonb, ossie_projection,
                   repeat('f', 64), created_by
              FROM app.datastream_mapping_versions WHERE id = %s
            """,
            (plain_id, psycopg.types.json.Json(UNDESIGNATED_PAYLOAD), world["mapping_version_id"]),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (plain_id, world["datastream_id"]),
        )
    _observe(monkeypatch, {"v-1": 1})
    assert _run(world) == 0
    assert _verdicts(world) == []


@requires_postgres
def test_a_draft_designation_is_not_read__only_the_pinned_mapping_counts(world, monkeypatch):
    # Un-pin the mapping: the designation still exists as a version, but the
    # Project has not decided on it, so nothing may resolve against it.
    with world["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = NULL WHERE id = %s",
            (world["datastream_id"],),
        )
    _observe(monkeypatch, {"v-1": 1})
    assert _run(world) == 0


# ---------------------------------------------------------------------------
# Replay vs supersession -- the coverage contract.
# ---------------------------------------------------------------------------


@requires_postgres
def test_replaying_a_pull_writes_no_second_row(world, monkeypatch):
    _node(world, "v-1")
    _observe(monkeypatch, {"v-1": 12})
    _run(world)

    # A replay gets a NEW pull id: the occurrence key is the window, not the
    # pull, precisely so a retried run cannot inflate the fraction.
    assert _run(world, pull_id="pull_2") == 0
    assert len(_verdicts(world, state=None)) == 1


@requires_postgres
def test_the_same_occurrence_answering_differently_supersedes(world, monkeypatch):
    _observe(monkeypatch, {"v-1": 4})
    _run(world)
    assert _verdicts(world)[0][1] == ekm.VERDICT_UNMATCHED

    # An alias is recorded: the same value now names an identity.
    _node(world, "v-1")
    assert _run(world, pull_id="pull_2") == 1

    current = _verdicts(world)
    assert len(current) == 1
    assert current[0][1] == ekm.VERDICT_RESOLVED
    # Append-only: the old answer is history, not an edit.
    everything = _verdicts(world, state=None)
    assert len(everything) == 2
    assert {row[8] for row in everything} == {ekm.STATE_CURRENT, ekm.STATE_SUPERSEDED}


# ---------------------------------------------------------------------------
# AC3: an ambiguity names its candidates and picks nobody.
# ---------------------------------------------------------------------------


@requires_postgres
def test_two_nodes_claiming_one_value_are_ambiguous_and_both_are_named(world, monkeypatch):
    first = _node(world, "Intro", aliases=["v-1"])
    second = _node(world, "Intro (re-cut)", aliases=["v-1"])
    _observe(monkeypatch, {"v-1": 5})

    _run(world)
    row = _verdicts(world)[0]

    assert row[1] == ekm.VERDICT_AMBIGUOUS
    # The machine picked nobody...
    assert row[2] is None
    # ...and said between whom, which is what a person repairs from.
    assert {c["node_id"] for c in row[3]} == {first, second}


# ---------------------------------------------------------------------------
# AC2: the coverage read.
# ---------------------------------------------------------------------------


@requires_postgres
def test_coverage_answers_n_of_m_and_counts_only_current_rows(world, monkeypatch):
    _node(world, "v-1")
    _node(world, "v-2")
    _observe(monkeypatch, {"v-1": 3, "v-2": 2, "v-nope": 1})
    _run(world)

    report = ekm.matching_coverage(world["conn"], project_id=world["project_id"])
    assert report["state"] == "available"
    assert report["coverage"] == {
        "bound": 2,
        "eligible": 3,
        "by_state": {"resolved": 2, "unmatched": 1},
    }
    assert report["rows"][0]["datastream_id"] == world["datastream_id"]
    assert report["rows"][0]["object_kind"] == KIND

    # Superseding an unmatched occurrence into a resolved one moves the
    # fraction, and does NOT add a fourth occurrence to it.
    _node(world, "v-nope")
    _run(world, pull_id="pull_2")
    after = ekm.matching_coverage(world["conn"], project_id=world["project_id"])
    assert after["coverage"]["bound"] == 3
    assert after["coverage"]["eligible"] == 3


@requires_postgres
def test_a_project_with_no_occurrence_reads_empty_never_unavailable(world):
    report = ekm.matching_coverage(world["conn"], project_id=world["project_id"])
    assert report["state"] == "empty"
    assert report["coverage"]["eligible"] == 0
    assert report["unavailable_reason"] is None


# ---------------------------------------------------------------------------
# An archived type resolves nothing, and says so rather than guessing.
# ---------------------------------------------------------------------------


@requires_postgres
def test_an_archived_type_yields_unmatched_naming_the_reason(world, monkeypatch):
    _node(world, "v-1")
    master_data.set_registry_state(
        world["conn"],
        project_id=world["project_id"],
        registry_id=world["registry"]["id"],
        lifecycle_state="disabled",
    )
    _observe(monkeypatch, {"v-1": 1})
    _run(world)

    row = _verdicts(world)[0]
    assert row[1] == ekm.VERDICT_UNMATCHED
    assert row[4] == ekm.REASON_TYPE_UNAVAILABLE
