"""Story 60.1 -- where a value table applies, and what it says before it changes.

The subject of this file is ONE distinction: "I could not check" and "nothing
depends on this" are different facts (`master_data.py:745-749`). An impact read
that fails RAISES; it never returns an empty tuple, and no caller ever renders a
zero it did not measure.

Offline (no DB): the failing read, through a connection whose cursor raises --
the only way to prove the refusal without breaking a real database.

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): the triplet
`(table, datastream, source_field)` and its unicity, the counted impact, and the
refusal of a change nobody acknowledged.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import value_mapping_tables as store  # noqa: E402

from tests.core.test_value_mapping_tables import fixture_org, pg_available  # noqa: E402,F401


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ===========================================================================
# Offline -- the read that fails
# ===========================================================================


class _BrokenCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_args, **_kwargs):
        raise RuntimeError("the assignment store is unreachable")


class _BrokenConnection:
    def cursor(self):
        return _BrokenCursor()


def test_an_unreadable_impact_RAISES_instead_of_answering_an_empty_list():
    with pytest.raises(store.ValueMappingUnavailable) as refused:
        store.assess_table_impact(_BrokenConnection(), table_id="vmt_EXAMPLE")
    # The reason is named, so the surface can say WHY it does not know.
    assert "vmt_EXAMPLE" in str(refused.value)
    assert "RuntimeError" in str(refused.value)


def test_the_refusal_is_not_a_TableImpact_with_zero_assignments():
    """The failure mode this test exists for.

    An `except` that returned `TableImpact(table_id, ())` would make a broken
    read indistinguishable from a table nothing uses -- and every screen above
    would print `0 Datastreams` about a question that was never answered.
    """
    try:
        store.assess_table_impact(_BrokenConnection(), table_id="vmt_EXAMPLE")
    except store.ValueMappingUnavailable:
        return
    pytest.fail("a failed impact read returned a value instead of raising")


def test_an_empty_impact_is_a_MEASUREMENT_and_says_so():
    impact = store.TableImpact(table_id="vmt_EXAMPLE", assignments=())
    assert impact.is_clear is True
    assert impact.datastream_count == 0
    # `impact_state` is carried in the payload, so a reader never has to infer
    # "did the read happen" from "is the list empty".
    assert impact.as_dict()["impact_state"] == "known"
    assert impact.as_dict()["datastream_count"] == 0


def test_the_impact_counts_DATASTREAMS_and_not_assignment_rows():
    """Two fields of one Datastream are ONE affected Datastream.

    Counting rows would report a dependency load that does not exist, and the
    confirmation sentence would name a number nobody can verify on screen.
    """
    impact = store.TableImpact(
        table_id="vmt_EXAMPLE",
        assignments=(
            {"assignment_id": "vmasg_1", "datastream_id": "ds_1", "source_field": "column_a"},
            {"assignment_id": "vmasg_2", "datastream_id": "ds_1", "source_field": "column_b"},
            {"assignment_id": "vmasg_3", "datastream_id": "ds_2", "source_field": "column_a"},
        ),
    )
    assert impact.datastream_count == 2
    assert impact.as_dict()["assignment_count"] == 3
    assert impact.describe() == "2 Datastreams"
    assert store.TableImpact("vmt_EXAMPLE", (dict(impact.assignments[0]),)).describe() == (
        "1 Datastream"
    )


# ===========================================================================
# Live Postgres
# ===========================================================================


def _table(conn, fixture) -> dict:
    return store.create_table(
        conn,
        org_id=fixture["org_id"],
        project_id=fixture["project_id"],
        scope_level="PROJECT",
        name=f"Vocabulary {uuid.uuid4().hex[:6]}",
        description=None,
        identity="owner@example.com",
    )


@pg_available
def test_a_table_is_assigned_to_a_datastream_on_a_named_raw_field(fixture_org):  # noqa: F811
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        assignment = store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="raw_column_name",
            identity="owner@example.com",
        )
        conn.commit()
        impact = store.assess_table_impact(conn, table_id=table["id"])

    assert assignment["assignment_id"].startswith("vmasg_")
    assert impact.datastream_count == 1
    assert impact.assignments[0]["source_field"] == "raw_column_name"
    # The Datastream is NAMED, not only referenced: a confirmation that says
    # "6 Datastreams" without naming them cannot be checked by the person.
    assert impact.assignments[0]["datastream_name"]


@pg_available
def test_the_assigned_field_needs_no_mapping_to_exist(fixture_org):  # noqa: F811
    """Arbitrage 2, and the reason `122_derived_columns.sql:62-64` states it.

    The field named here is a raw collected column that no `datastream_mappings`
    row mentions. A foreign key would make a value table unusable on its main
    case -- normalising a column BEFORE anything maps it.
    """
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="a_column_nothing_maps",
            identity="owner@example.com",
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.datastream_mappings WHERE datastream_id = %s",
                (fixture_org["datastreams"][0],),
            )
            mapped = cur.fetchone()[0]
        impact = store.assess_table_impact(conn, table_id=table["id"])

    assert mapped == 0
    assert impact.datastream_count == 1


@pg_available
def test_the_triplet_is_unique_and_a_second_field_is_a_second_assignment(fixture_org):  # noqa: F811
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="column_a",
            identity="owner@example.com",
        )
        conn.commit()
        with pytest.raises(store.ValueMappingConflict):
            store.create_assignment(
                conn,
                table_id=table["id"],
                org_id=fixture_org["org_id"],
                datastream_id=fixture_org["datastreams"][0],
                source_field="column_a",
                identity="owner@example.com",
            )
        conn.rollback()
        store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="column_b",
            identity="owner@example.com",
        )
        conn.commit()
        impact = store.assess_table_impact(conn, table_id=table["id"])

    assert len(impact.assignments) == 2
    assert impact.datastream_count == 1


@pg_available
def test_one_table_serves_several_datastreams_which_is_the_whole_point(fixture_org):  # noqa: F811
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        for datastream_id in fixture_org["datastreams"]:
            store.create_assignment(
                conn,
                table_id=table["id"],
                org_id=fixture_org["org_id"],
                datastream_id=datastream_id,
                source_field="column_a",
                identity="owner@example.com",
            )
        conn.commit()
        listed = store.list_tables(
            conn, org_id=fixture_org["org_id"], project_id=fixture_org["project_id"]
        )

    row = next(entry for entry in listed if entry["id"] == table["id"])
    assert row["datastream_count"] == 2
    assert row["assignment_count"] == 2


@pg_available
def test_a_change_without_acknowledgement_is_REFUSED_and_names_the_count(fixture_org):  # noqa: F811
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="column_a",
            identity="owner@example.com",
        )
        conn.commit()

        with pytest.raises(store.ImpactNotAcknowledged) as refused:
            store.update_table(
                conn,
                table_id=table["id"],
                org_id=fixture_org["org_id"],
                name="Renamed",
                description=None,
                identity="owner@example.com",
            )
        conn.rollback()

        # The refusal CARRIES the impact it read -- the surface shows the same
        # evidence the guard used, not a second reading taken afterwards.
        assert refused.value.impact["datastream_count"] == 1
        assert refused.value.impact["impact_state"] == "known"
        assert "1 Datastream" in str(refused.value)

        # And the same call goes through once the impact is acknowledged.
        renamed = store.update_table(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            name="Renamed",
            description=None,
            identity="owner@example.com",
            acknowledge_impact=True,
        )
        conn.commit()
    assert renamed["name"] == "Renamed"


@pg_available
def test_a_table_nothing_uses_is_changed_without_any_acknowledgement(fixture_org):  # noqa: F811
    """The guard fires on a real dependency, never on the act of changing."""
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        conn.commit()
        renamed = store.update_table(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            name="Renamed",
            description=None,
            identity="owner@example.com",
        )
        conn.commit()
    assert renamed["name"] == "Renamed"


@pg_available
def test_a_deletion_is_refused_then_records_what_was_acknowledged(fixture_org):  # noqa: F811
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="column_a",
            identity="owner@example.com",
        )
        conn.commit()
        with pytest.raises(store.ImpactNotAcknowledged):
            store.delete_table(
                conn,
                table_id=table["id"],
                org_id=fixture_org["org_id"],
                identity="owner@example.com",
            )
        conn.rollback()

        result = store.delete_table(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            identity="owner@example.com",
            acknowledge_impact=True,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT before FROM app.metric_semantics_audit "
                "WHERE entity_id = %s AND action = 'value_mapping_table.deleted'",
                (table["id"],),
            )
            recorded = cur.fetchone()[0]

    assert result["impact"]["datastream_count"] == 1
    # What was acknowledged at command time is written down, not re-read later.
    assert recorded["assignments"][0]["source_field"] == "column_a"


@pg_available
def test_removing_an_assignment_lowers_the_impact_it_was_counted_in(fixture_org):  # noqa: F811
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        assignment = store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="column_a",
            identity="owner@example.com",
        )
        conn.commit()
        store.delete_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            assignment_id=assignment["assignment_id"],
            identity="owner@example.com",
        )
        conn.commit()
        impact = store.assess_table_impact(conn, table_id=table["id"])

    assert impact.is_clear is True
    assert impact.as_dict()["impact_state"] == "known"


@pg_available
def test_a_deleted_datastream_takes_its_assignment_with_it(fixture_org):  # noqa: F811
    """The assignment is about a Datastream that exists; it never outlives it."""
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        store.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][1],
            source_field="column_a",
            identity="owner@example.com",
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.datastreams WHERE id = %s", (fixture_org["datastreams"][1],)
            )
        conn.commit()
        impact = store.assess_table_impact(conn, table_id=table["id"])

    assert impact.is_clear is True
