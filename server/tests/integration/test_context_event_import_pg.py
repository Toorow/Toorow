"""An events file becomes datastream-owned markers, on a real base (Story 68.4).

WHY THIS FILE EXISTS. `tests/core/test_context_event_route.py` proves the
routing rule and the row rules off base. What only a database can show is the
half the acceptance criteria are about:

  * the rows land in `app.context_events`, BOUND to the Datastream and to an
    active Event Configuration version -- the trigger
    `require_event_observation_binding` refuses anything else, and a managed
    feed has no Connector to derive that configuration from (AC2, AC5);
  * each marker names the execution that landed it -- the `execution_id`
    column has existed since migration 133 and no writer ever filled it (AD-7);
  * a re-import REPLACES the file's own window instead of doubling it (AC2);
  * a row that cannot be an event is rejected with per-row evidence in the
    ledger, and above the threshold the whole landing goes back (AC4);
  * the landed markers are the ones a report reads for that window (AC3).

Every write happens on the caller's transaction; `live_postgres` rolls back.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from core import context_event_import as cei  # noqa: E402
from core.import_runner import run_import  # noqa: E402
from core.tabular_types import CsvExcelImportError  # noqa: E402

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres events route test skipped",
)

ACTOR = "alice@example.com"
CONTRACT = {"format": "csv", "write_mode": "replace", "header_row": 1}


def _known_type() -> str:
    from core.report_dictionary import _load_event_type_dictionary

    types = sorted(_load_event_type_dictionary())
    assert types, "the canonical event dictionary is empty -- this test proves nothing"
    return types[0]


MAPPING_PAYLOAD = {
    "grain": ["day", "headline"],
    "fields": [
        {
            "field_id": "day",
            "physical_type": "string",
            "binding": {
                "status": "confirmed",
                "canonical_target": "day",
                "event_role": "date",
            },
        },
        {
            "field_id": "kind",
            "physical_type": "string",
            "binding": {
                "status": "confirmed",
                "canonical_target": "kind",
                "event_role": "type",
            },
        },
        {
            "field_id": "headline",
            "physical_type": "string",
            "binding": {
                "status": "confirmed",
                "canonical_target": "headline",
                "event_role": "label",
            },
        },
    ],
}
PROJECTION = {
    "executable": True,
    "grain": ["day", "headline"],
    "full_grain_relation": {
        "grain_columns": [{"field_id": "day"}, {"field_id": "headline"}],
        "source_fields": ["day", "kind", "headline"],
    },
}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _csv(rows) -> bytes:
    lines = ["day,kind,headline"] + [",".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture()
def world(live_postgres):
    """A Connector-less Datastream pinning a mapping that declares event roles."""
    from tests.integration.epic66_fixtures import make_project

    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 68.4")
    ds_id, plan_id, mapping_id = _id("ds_"), _id("dsp_"), _id("dmap_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'Marketing calendar', NULL, 'managed_feed', FALSE, %s, %s)
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
    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": ds_id,
        "plan_version_id": plan_id,
        "mapping_version_id": mapping_id,
    }


def _import(world, rows, *, key=None, **kwargs):
    return run_import(
        _csv(rows),
        datastream_id=world["datastream_id"],
        project_id=world["project_id"],
        plan_version_id=world["plan_version_id"],
        mapping_version_id=world["mapping_version_id"],
        projection_plan=PROJECTION,
        actor=ACTOR,
        idempotency_key=key or f"68-4-{uuid.uuid4().hex[:10]}",
        source_metadata={"filename": "calendar.csv"},
        contract=CONTRACT,
        conn=world["conn"],
        mapping_payload=MAPPING_PAYLOAD,
        **kwargs,
    )


def _events(world):
    with world["conn"].cursor() as cur:
        cur.execute(
            """
            SELECT event_date, type, label, source, datastream_id,
                   event_configuration_version_id, execution_id, binding_state
              FROM app.context_events
             WHERE project_id = %s
             ORDER BY event_date, label
            """,
            (world["project_id"],),
        )
        return cur.fetchall()


def _rejected(world, ledger_id):
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT row_number, field_name, rule FROM app.managed_feed_rejected_rows "
            "WHERE ledger_id = %s ORDER BY row_number",
            (ledger_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# AC2 + AC5: the markers land, BOUND, on a Datastream that has no Connector.
# ---------------------------------------------------------------------------


@requires_postgres
def test_an_events_file_lands_as_bound_datastream_markers(world):
    kind = _known_type()
    result = _import(
        world,
        [["2026-08-01", kind, "Spring push"], ["2026-08-05", kind, "Summer teaser"]],
    )

    assert result["route"] == cei.ROUTE_CONTEXT_EVENTS
    assert result["published"] is True
    assert result["landing"]["events_written"] == 2
    assert result["landing"]["table"].startswith(cei.LANDING_RELATION_PREFIX)

    rows = _events(world)
    assert [row[2] for row in rows] == ["Spring push", "Summer teaser"]
    for row in rows:
        assert row[3] == cei.EVENT_SOURCE
        # The trigger refuses an unbound observation; a managed feed could never
        # satisfy it before this story, because the binding query joined on a
        # `module_name` it does not have.
        assert row[4] == world["datastream_id"]
        assert row[5] == result["landing"]["event_configuration_version_id"]
        assert row[7] == "linked"
        # AD-7: the marker names the run that put it on the calendar.
        assert row[6] == result["execution"]["id"]


@requires_postgres
def test_the_armed_configuration_anchors_on_the_pinned_mapping_not_a_contract(world):
    result = _import(world, [["2026-08-01", _known_type(), "Spring push"]])
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT connector_contract_version_id, source_mapping, review_state "
            "FROM app.event_configuration_versions WHERE id = %s",
            (result["landing"]["event_configuration_version_id"],),
        )
        contract_id, source_mapping, review_state = cur.fetchone()
    # No Connector, so no contract row can exist -- and demanding one refused
    # every imported event stream before this story.
    assert contract_id is None
    assert source_mapping["module"] is None
    assert source_mapping["mapping_version_id"] == world["mapping_version_id"]
    assert review_state == "active"


@requires_postgres
def test_a_second_import_reuses_the_stream_never_arms_a_second_one(world):
    kind = _known_type()
    first = _import(world, [["2026-08-01", kind, "Spring push"]])
    second = _import(world, [["2026-08-01", kind, "Spring push (v2)"]])

    assert (
        second["landing"]["event_configuration_version_id"]
        == first["landing"]["event_configuration_version_id"]
    )
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.event_configurations WHERE datastream_id = %s",
            (world["datastream_id"],),
        )
        # A second configuration would collect the same markers under a second
        # identity, and every later count of them would be double.
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Idempotence: the file's own window is replaced, never doubled.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_re_import_replaces_the_files_window_instead_of_doubling_it(world):
    kind = _known_type()
    _import(world, [["2026-08-01", kind, "Spring push"]])
    result = _import(world, [["2026-08-01", kind, "Spring push renamed"]])

    rows = _events(world)
    assert [row[2] for row in rows] == ["Spring push renamed"]
    assert result["landing"]["events_replaced"] == 1


@requires_postgres
def test_the_window_cleared_is_the_files_own_span(world):
    kind = _known_type()
    _import(world, [["2026-07-01", kind, "Old news"], ["2026-08-01", kind, "Spring push"]])
    # A file talking only about August must not erase July.
    _import(world, [["2026-08-01", kind, "Spring push v2"]])

    labels = [row[2] for row in _events(world)]
    assert labels == ["Old news", "Spring push v2"]


# ---------------------------------------------------------------------------
# AC4: a row that cannot be an event is evidence, not a silence.
# ---------------------------------------------------------------------------


@requires_postgres
def test_an_unknown_event_type_is_rejected_with_its_line(world):
    kind = _known_type()
    rows = [[f"2026-08-0{n}", kind, f"Push {n}"] for n in range(1, 6)]
    rows.insert(2, ["2026-08-09", "vibes", "Unnameable"])
    result = _import(world, rows)

    assert result["blocked"] is False
    assert result["rejected_count"] == 1
    assert result["landing"]["events_written"] == 5
    rejected = _rejected(world, result["ledger"]["id"])
    assert [row[1] for row in rejected] == ["kind"]
    assert [row[2] for row in rejected] == [cei.RULE_EVENT_VALIDATION_FAILED]


@requires_postgres
def test_too_many_bad_rows_refuse_the_import_and_write_no_marker(world):
    kind = _known_type()
    rows = [["2026-08-01", kind, "Spring push"]] + [
        ["not-a-day", kind, f"Broken {n}"] for n in range(8)
    ]
    result = _import(world, rows)

    assert result["blocked"] is True
    assert result["outcome"] == "rejected"
    # The savepoint took the whole landing back: a refused import leaves no
    # marker on anybody's calendar.
    assert _events(world) == []


# ---------------------------------------------------------------------------
# AC3: what the report reads for that window is what landed.
# ---------------------------------------------------------------------------


@requires_postgres
def test_the_landed_markers_reach_the_table_the_report_read_mirrors(world):
    from core.mirror_sync import _DEFAULT_TABLES

    kind = _known_type()
    _import(world, [["2026-08-01", kind, "Spring push"], ["2026-09-15", kind, "Autumn"]])

    # `fetch_context_events` -- what `meta.context_events` of a report is built
    # from -- reads the DuckDB MIRROR, never Postgres (AD-12). So the read side
    # needed nothing built: the proof owed here is that the rows land in the
    # table the mirror copies by default, and that they are windowable there.
    assert "context_events" in _DEFAULT_TABLES
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT label FROM app.context_events "
            "WHERE project_id = %s AND event_date BETWEEN %s::date AND %s::date "
            "AND retired_at IS NULL ORDER BY event_date",
            (world["project_id"], "2026-08-01", "2026-08-31"),
        )
        assert [row[0] for row in cur.fetchall()] == ["Spring push"]


# ---------------------------------------------------------------------------
# The route refuses what it cannot honour, before minting anything.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_governed_dispatch_is_refused_naming_the_repair(world):
    with pytest.raises(CsvExcelImportError) as excinfo:
        _import(world, [["2026-08-01", _known_type(), "Spring push"]], publish_candidate=True)
    assert excinfo.value.code == "context_events_publication_is_the_import"
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.managed_feed_import_ledger WHERE datastream_id = %s",
            (world["datastream_id"],),
        )
        assert cur.fetchone()[0] == 0
