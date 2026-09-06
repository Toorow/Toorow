"""AI-260 -- the bridge that resolves a value table assignment onto a connector key.

The subject of this file is ONE ratified sentence, transposed from
`fee_tax_country_resolution.sql:304-305`: two Datastreams of one connector that
do not resolve to the SAME table on the named field is an AMBIGUITY -- a gap,
never a pick. The resolver never invents the correspondence between a raw
collected column and a breakdown value: an assignment applies by string equality
on the field, or not at all.

Offline (no DB): a scripted connection serves the three reads the bridge makes
(the dim the mirror is made from, the assignments, the pairs), so every rung of
the classification is proved without a database -- including the refusal to
report an outage as "no table".

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): the same rungs against
the real stores, on the fixture whose two Datastreams share one connector.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import value_mapping_tables as store  # noqa: E402
from core import value_table_resolution as bridge  # noqa: E402

from tests.core.test_value_mapping_tables import fixture_org, pg_available  # noqa: E402,F401

# ===========================================================================
# Offline -- a scripted connection, one result set per relation named.
# ===========================================================================


class _ScriptedCursor:
    """Serves (columns, rows) for the first script key found in the SQL."""

    def __init__(self, script):
        self._script = script
        self._columns: list[str] = []
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, _params=None):
        for key, (columns, rows) in self._script.items():
            if key in sql:
                self._columns = list(columns)
                self._rows = list(rows)
                return
        self._columns, self._rows = [], []

    @property
    def description(self):
        return [(name,) for name in self._columns]

    def fetchall(self):
        return list(self._rows)


class _ScriptedConn:
    def __init__(self, script):
        self._script = script

    def cursor(self):
        return _ScriptedCursor(self._script)


def _conn(*, datastreams, assignments, entries=()):
    """A connection scripted with the three relations the bridge reads.

    SAVEPOINT statements match no key and harmlessly return nothing.
    """
    return _ScriptedConn(
        {
            "app.datastreams_dim_v": (["datastream_id"], [(d,) for d in datastreams]),
            "app.value_mapping_assignments": (
                ["datastream_id", "table_id", "table_name", "datastream_name"],
                list(assignments),
            ),
            "app.value_mapping_entries": (
                [
                    "id",
                    "table_id",
                    "source_value",
                    "canonical_value",
                    "created_by",
                    "created_at",
                    "updated_at",
                ],
                [
                    (f"vment_{i}", "vmt_A", source, canonical, None, None, None)
                    for i, (source, canonical) in enumerate(entries)
                ],
            ),
        }
    )


def _resolve(conn):
    return bridge.resolve_value_table(
        conn, project_id="proj_EXAMPLE", connector="example_connector",
        source_field="campaign_id",
    )


def test_one_datastream_one_assignment_resolves_the_table_and_its_pairs():
    conn = _conn(
        datastreams=["ds_1"],
        assignments=[("ds_1", "vmt_A", "Client vocabulary", "Stream one")],
        entries=[("camp-raw", "Campaign Canonical")],
    )
    resolution = _resolve(conn)
    assert resolution.state == bridge.STATE_APPLIED
    assert resolution.table_id == "vmt_A"
    assert resolution.table_name == "Client vocabulary"
    assert resolution.answer("camp-raw") == (True, "Campaign Canonical")


def test_on_the_assigned_field_the_table_answers_ALONE_a_precedence_not_a_merge():
    """AI_238_PRECEDENCE: a value the winning table does not name stays
    unconformed on that field. handled=True with None means '052 is NOT asked'."""
    conn = _conn(
        datastreams=["ds_1"],
        assignments=[("ds_1", "vmt_A", "Client vocabulary", "Stream one")],
        entries=[("camp-raw", "Campaign Canonical")],
    )
    assert _resolve(conn).answer("value-the-table-never-named") == (True, None)


def test_two_datastreams_agreeing_on_one_table_resolve_it():
    """Same table on every Datastream of the connector = agreement, not ambiguity
    -- the mirror rung counts DISTINCT values, and so does this."""
    conn = _conn(
        datastreams=["ds_1", "ds_2"],
        assignments=[
            ("ds_1", "vmt_A", "Client vocabulary", "Stream one"),
            ("ds_2", "vmt_A", "Client vocabulary", "Stream two"),
        ],
        entries=[("camp-raw", "Campaign Canonical")],
    )
    resolution = _resolve(conn)
    assert resolution.state == bridge.STATE_APPLIED
    assert resolution.table_id == "vmt_A"


def test_two_datastreams_disagreeing_is_an_ambiguity_NAMED_never_a_pick():
    conn = _conn(
        datastreams=["ds_1", "ds_2"],
        assignments=[
            ("ds_1", "vmt_A", "Vocabulary A", "Stream one"),
            ("ds_2", "vmt_B", "Vocabulary B", "Stream two"),
        ],
    )
    resolution = _resolve(conn)
    assert resolution.state == bridge.STATE_AMBIGUOUS
    assert resolution.reason == bridge.REASON_TABLES_DISAGREE
    assert resolution.table_id is None  # no winner was invented
    # Both sides are NAMED, checkable by the person reading the state.
    named = {(d["datastream_id"], d["table_id"]) for d in resolution.datastreams}
    assert named == {("ds_1", "vmt_A"), ("ds_2", "vmt_B")}
    # And the field is a gap for every value: never a pick between the tables.
    assert resolution.answer("camp-raw") == (True, None)


def test_a_table_missing_on_one_datastream_of_the_connector_is_the_same_gap():
    """Connector-keyed rows merge every Datastream's rows; applying a table one
    of them never assigned would be a pick. Mirrors the mart's
    `declared_count < datastream_count` rung."""
    conn = _conn(
        datastreams=["ds_1", "ds_2"],
        assignments=[("ds_1", "vmt_A", "Vocabulary A", "Stream one")],
    )
    resolution = _resolve(conn)
    assert resolution.state == bridge.STATE_AMBIGUOUS
    assert resolution.reason == bridge.REASON_PARTIAL_ASSIGNMENT
    unassigned = [d for d in resolution.datastreams if d["table_id"] is None]
    assert [d["datastream_id"] for d in unassigned] == ["ds_2"]
    assert resolution.answer("camp-raw") == (True, None)


def test_no_assignment_hands_the_field_to_the_governed_store():
    conn = _conn(datastreams=["ds_1"], assignments=[])
    resolution = _resolve(conn)
    assert resolution.state == bridge.STATE_NONE
    assert resolution.reason == bridge.REASON_NO_ASSIGNMENT
    assert resolution.answer("camp-raw") == (False, None)


def test_no_datastream_for_the_connector_is_none_with_the_marts_own_word():
    conn = _conn(datastreams=[], assignments=[])
    resolution = _resolve(conn)
    assert resolution.state == bridge.STATE_NONE
    assert resolution.reason == bridge.REASON_NO_DATASTREAM


def test_an_outage_is_UNAVAILABLE_never_reported_as_no_table():
    """'I could not check' and 'no table names this field' are different facts,
    and only one of them may hand the field to 052 in silence."""

    class _BrokenCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_a, **_k):
            raise RuntimeError("the store is unreachable")

    class _BrokenConn:
        def cursor(self):
            return _BrokenCursor()

    resolution = bridge.resolve_value_table(
        _BrokenConn(), project_id="proj_EXAMPLE", connector="example_connector",
        source_field="campaign_id",
    )
    assert resolution.state == bridge.STATE_UNAVAILABLE
    assert "RuntimeError" in resolution.reason
    # Not handled: the caller decides what an outage renders, and NAMES it.
    assert resolution.answer("camp-raw") == (False, None)


def test_the_resolution_serialises_for_the_read_contract():
    conn = _conn(
        datastreams=["ds_1"],
        assignments=[("ds_1", "vmt_A", "Client vocabulary", "Stream one")],
        entries=[("camp-raw", "Campaign Canonical")],
    )
    payload = _resolve(conn).as_dict()
    assert payload["state"] == "applied"
    assert payload["connector"] == "example_connector"
    assert payload["source_field"] == "campaign_id"
    assert payload["datastreams"][0]["datastream_id"] == "ds_1"
    assert "pairs" not in payload  # the contract carries states, not vocabularies


# ===========================================================================
# Live Postgres -- the same rungs against the real stores.
# ===========================================================================


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


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


def _assign(conn, fixture, table, datastream_id, field="campaign_id"):
    return store.create_assignment(
        conn,
        table_id=table["id"],
        org_id=fixture["org_id"],
        datastream_id=datastream_id,
        source_field=field,
        identity="owner@example.com",
    )


@pg_available
def test_pg_a_table_assigned_on_every_datastream_of_the_connector_applies(fixture_org):  # noqa: F811
    """The fixture's two Datastreams share module_name='example_connector', which
    is exactly the bridge's case: the fact would key them as ONE connector."""
    from core.db import get_connection

    with get_connection() as conn:
        table = _table(conn, fixture_org)
        store.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="camp-raw",
            canonical_value="Campaign Canonical",
            identity="owner@example.com",
        )
        for datastream_id in fixture_org["datastreams"]:
            _assign(conn, fixture_org, table, datastream_id)
        conn.commit()

        resolution = bridge.resolve_value_table(
            conn,
            project_id=fixture_org["project_id"],
            connector="example_connector",
            source_field="campaign_id",
        )

    assert resolution.state == bridge.STATE_APPLIED
    assert resolution.table_id == table["id"]
    assert resolution.answer("camp-raw") == (True, "Campaign Canonical")
    assert resolution.answer("never-named") == (True, None)  # precedence, not a merge


@pg_available
def test_pg_partial_assignment_and_disagreement_are_both_named_gaps(fixture_org):  # noqa: F811
    from core.db import get_connection

    ds_1, ds_2 = fixture_org["datastreams"]
    with get_connection() as conn:
        table_a = _table(conn, fixture_org)
        _assign(conn, fixture_org, table_a, ds_1)
        conn.commit()

        partial = bridge.resolve_value_table(
            conn,
            project_id=fixture_org["project_id"],
            connector="example_connector",
            source_field="campaign_id",
        )
        assert partial.state == bridge.STATE_AMBIGUOUS
        assert partial.reason == bridge.REASON_PARTIAL_ASSIGNMENT

        table_b = _table(conn, fixture_org)
        _assign(conn, fixture_org, table_b, ds_2)
        conn.commit()

        disagreeing = bridge.resolve_value_table(
            conn,
            project_id=fixture_org["project_id"],
            connector="example_connector",
            source_field="campaign_id",
        )

    assert disagreeing.state == bridge.STATE_AMBIGUOUS
    assert disagreeing.reason == bridge.REASON_TABLES_DISAGREE
    named = {(d["datastream_id"], d["table_id"]) for d in disagreeing.datastreams}
    assert named == {(ds_1, table_a["id"]), (ds_2, table_b["id"])}


@pg_available
def test_pg_another_field_or_no_assignment_stays_with_the_governed_store(fixture_org):  # noqa: F811
    """Exact string equality on the field: an assignment naming a DIFFERENT raw
    column does not reach this read -- the correspondence is never guessed."""
    from core.db import get_connection

    with get_connection() as conn:
        none_yet = bridge.resolve_value_table(
            conn,
            project_id=fixture_org["project_id"],
            connector="example_connector",
            source_field="campaign_id",
        )
        assert none_yet.state == bridge.STATE_NONE
        assert none_yet.reason == bridge.REASON_NO_ASSIGNMENT

        table = _table(conn, fixture_org)
        for datastream_id in fixture_org["datastreams"]:
            _assign(conn, fixture_org, table, datastream_id, field="utm_campaign")
        conn.commit()

        other_field = bridge.resolve_value_table(
            conn,
            project_id=fixture_org["project_id"],
            connector="example_connector",
            source_field="campaign_id",
        )

    assert other_field.state == bridge.STATE_NONE
    assert other_field.reason == bridge.REASON_NO_ASSIGNMENT
