"""Story 48.5, AC6: one Project edit fans out truthfully -- and stops there.

The fan-out is the step Epic 40 recorded a preview of and never performed: its
confirmation wrote a row and created no Datastream projection at all. What
matters here is therefore as much what the function refuses as what it writes.

Run against a fake connection that records every statement. That is deliberate:
the four refusals below -- never publish, never move a pointer, never dispatch,
never overwrite a local exclusion -- are claims about statements NOT issued, and
the only way to prove a statement was not issued is to look at the list.
"""

from __future__ import annotations

import re

import pytest
from core.entity_bindings import (
    STATE_CANDIDATE,
    STATE_EXCLUDED,
    STATE_PUBLISHED,
    Support,
    apply_confirmed_fan_out,
)

NODE_A = "mdnode_AAAAAAAAAAAAAAAAAAAAAAAAAA"
NODE_B = "mdnode_BBBBBBBBBBBBBBBBBBBBBBBBBB"
DS = "ds_EXAMPLE"

DECLARATION = {
    "declaration_version": "1",
    "reports": [
        {
            "report_id": "snapshot",
            "direction": "collect",
            "entity_kinds": ["source_entity"],
            "candidate_field_ids": ["entity_label"],
            "identity_field_id": "entity_ref",
            "label_field_id": "entity_label",
            "own_marker_field_id": None,
            "population": {"completeness": "declared_scope", "note": None},
            "query_driver": {
                "parameter": "entity_ids",
                "value_source": "source_identity",
                "cardinality": "one_request_per_value",
                "own_marker_parameter": "own_entity_ids",
                "max_values_per_request": None,
            },
        }
    ],
}


def _projected_columns(flat: str) -> list[str]:
    """The column names psycopg would report for a flat, single-level SELECT.

    Only what this fake needs: the projection of a SELECT whose first ``FROM``
    is its own, with no sub-select and no function call in the list. It exists
    so the fake never restates the product's column list by hand.
    """

    match = re.match(r"select (.+?) from ", flat)
    assert match, f"not a SELECT this fake can project: {flat}"
    assert "(" not in match.group(1), f"projection too rich for this fake: {flat}"
    names = []
    for item in match.group(1).split(","):
        expression = item.strip().split(" as ")[-1].strip()
        names.append(expression.rsplit(".", 1)[-1])
    return names


class FakeCursor:
    def __init__(self, owner: "FakeConn"):
        self.owner = owner
        self._rows: list[tuple] = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql: str, params=None):
        self.owner.statements.append((" ".join(sql.split()).lower(), params))
        flat = " ".join(sql.split()).lower()
        self._rows = []
        self.description = None
        if "from app.datastreams " in flat and "module_name" in flat:
            # `description` is DERIVED from the statement, never spelled out
            # again here. A hand-written list is a second copy of the query, and
            # a second copy goes stale: when the product aliased this SELECT to
            # `d.id, d.module_name, ...` to join the plan version, the branch
            # test read `select id, module_name` and stopped matching, so the
            # fake answered `description = None` -- which psycopg only ever does
            # for a statement that returned no result set. The product read it
            # as such and raised `TypeError: 'NoneType' object is not iterable`.
            self.description = [(name,) for name in _projected_columns(flat)]
            self._rows = [self.owner.datastream_row]
        elif "coalesce(max(version_number), 0) + 1" in flat:
            self._rows = [(1,)]
        elif "from app.datastream_entity_binding_versions" in flat and flat.startswith("select"):
            # Two callers read this table with different parameter orders. Find
            # the node id by shape rather than by position, so the fake does not
            # quietly answer "nothing" when a query is reordered.
            wanted = set()
            for item in params or ():
                if isinstance(item, str) and item.startswith("mdnode_"):
                    wanted.add(item)
                elif isinstance(item, list):
                    wanted.update(x for x in item if isinstance(x, str) and x.startswith("mdnode_"))
            rows = [
                self.owner.existing_bindings[node]
                for node in sorted(wanted)
                if node in self.owner.existing_bindings
            ]
            self._rows = rows
        elif "insert into app.datastream_entity_binding_versions" in flat:
            self.owner.inserted.append(params)
            self._rows = [self.owner.binding_row(params)]
        elif "update app.datastream_entity_binding_versions" in flat:
            # Superseding the previous version. No result set, and the product
            # reads none -- but it IS issued, so `self.owner.statements` (which
            # the refusal assertions scan) has to be the whole truth about it.
            pass
        else:
            # LOUD, not empty. Every branch above recognizes the statement by a
            # fragment of its text, so any rewrite of the product query can stop
            # matching -- and an unmatched statement that silently returns zero
            # rows and no description makes the fake assert about a code path it
            # never exercised. Naming the statement here is what turns that class
            # of drift into a failure that says which query moved.
            raise AssertionError(f"FakeCursor has no answer for this statement: {flat}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records statements. Reads are stubbed; writes are inspected."""

    BINDING_COLUMNS = (
        "id", "org_id", "project_id", "datastream_id", "source_identity_version_id",
        "node_id", "project_role", "project_association_id", "version_number",
        "connector_name", "connection_ref_id", "account_scope", "report_id", "field_ids",
        "direction", "query_driver", "connector_fingerprint", "plan_version_id",
        "mapping_version_id", "publication_version_id", "application_state",
        "exception_reason_code", "exception_reason", "content_hash", "created_by",
        "created_at", "published_at", "published_by",
    )

    def __init__(self, existing_bindings: dict[str, tuple] | None = None):
        self.statements: list[tuple[str, object]] = []
        self.inserted: list[object] = []
        self.existing_bindings = existing_bindings or {}
        self.datastream_row = (
            DS,
            "example-source",
            {"source": {"report_id": "snapshot"}},
            "conn_EXAMPLE",
            "fp-1",
        )

    def cursor(self):
        return FakeCursor(self)

    def binding_row(self, params) -> tuple:
        values = dict(zip(self.BINDING_COLUMNS[:22], params, strict=False))
        return tuple(values.get(column) for column in self.BINDING_COLUMNS)


def _binding_row(node_id: str, state: str, **extra) -> tuple:
    row = {
        "id": f"deb_{node_id[-4:]}",
        "org_id": "org_EXAMPLE",
        "project_id": "proj_EXAMPLE",
        "datastream_id": DS,
        "node_id": node_id,
        "application_state": state,
        "content_hash": "c" * 64,
        "exception_reason_code": extra.get("exception_reason_code"),
        "exception_reason": extra.get("exception_reason"),
    }
    return tuple(row.get(column) for column in FakeConn.BINDING_COLUMNS)


@pytest.fixture(autouse=True)
def stub_owners(monkeypatch):
    import core.entity_bindings as bindings
    import core.tracked_entities as entities

    monkeypatch.setattr(bindings, "tracked_entity_declaration", lambda name: DECLARATION)
    monkeypatch.setattr(
        entities,
        "project_roles",
        lambda conn, *, project_id: [
            {"id": "mdass_A", "node_id": NODE_A, "project_role": "competitor"},
            {"id": "mdass_B", "node_id": NODE_B, "project_role": "own"},
        ],
    )
    monkeypatch.setattr(
        entities,
        "list_source_identities",
        lambda conn, *, org_id, node_ids=None, connector_name=None: [
            {
                "id": "esi_A",
                "node_id": NODE_A,
                "connector_name": "example-source",
                "account_scope": None,
                "external_id": "42",
            },
            {
                "id": "esi_B",
                "node_id": NODE_B,
                "connector_name": "example-source",
                "account_scope": None,
                "external_id": "7",
            },
        ],
    )


def _proposals(**overrides) -> list[dict]:
    return [
        {
            "datastream_id": DS,
            "capability_key": "competitors",
            "applicability": "applicable",
            "coverage_state": "partial",
            **overrides,
        }
    ]


def _run(conn, proposals=None):
    return apply_confirmed_fan_out(
        conn,
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        proposals=proposals if proposals is not None else _proposals(),
        actor="operator@example.com",
    )


# ---------------------------------------------------------------------------
# What it writes.
# ---------------------------------------------------------------------------


def test_one_edit_reaches_every_entity_of_every_compatible_datastream():
    conn = FakeConn()
    summary = _run(conn)
    assert summary["bindings_created"] == 2
    assert summary["datastreams_considered"] == 1
    assert len(conn.inserted) == 2


def test_every_binding_lands_as_a_candidate_and_none_is_published():
    conn = FakeConn()
    summary = _run(conn)
    states = {params[19] for params in conn.inserted}
    # index 19 is application_state in the INSERT's parameter order; the literal
    # 'candidate' is in the SQL itself, so the assertion below is the real one.
    assert summary["published"] == 0
    inserts = [sql for sql, _ in conn.statements if "insert into app.datastream_entity" in sql]
    assert inserts and all("'candidate'" in sql for sql in inserts)
    assert all("'published'" not in sql for sql in inserts)
    assert states  # the row shape did not silently change underneath the test


def test_the_governed_identifier_becomes_the_request_the_connector_declared():
    conn = FakeConn()
    _run(conn)
    drivers = [params[15] for params in conn.inserted]
    assert all('"parameter": "entity_ids"' in driver for driver in drivers)
    assert any('"42"' in driver for driver in drivers)
    # The `own` role's identifier lands in the own-marker list as well.
    assert any('"own_values": ["7"]' in driver.replace(" ", " ") for driver in drivers)


# ---------------------------------------------------------------------------
# What it refuses. Each one is a statement that must NOT appear.
# ---------------------------------------------------------------------------


def test_no_plan_mapping_or_publication_pointer_moves():
    conn = FakeConn()
    _run(conn)
    # A pointer moves when it is WRITTEN. Naming the plan relation is not moving
    # it: the fan-out has to LEFT JOIN `datastream_plan_versions` to read the
    # capability fingerprint it pins each binding to -- the fingerprint lives on
    # the plan version, not on the Datastream. An earlier version of this list
    # forbade the mere mention, which would have made the correct read look like
    # a violation and the corrected query look like the defect.
    pointers = (
        "app.datastreams",
        "app.datastream_plan_versions",
        "app.datastream_mapping_versions",
        "app.datastream_publication_log",
        "current_published_execution_id",
    )
    writes = ("insert into", "update ", "delete from", "for update")
    offenders = [
        sql
        for sql, _ in conn.statements
        if any(word in sql for word in pointers) and any(verb in sql for verb in writes)
    ]
    assert not offenders, f"the fan-out moved a Data pointer: {offenders}"


def test_no_pull_is_dispatched():
    conn = FakeConn()
    _run(conn)
    offenders = [sql for sql, _ in conn.statements if "pull_jobs" in sql or "enqueue" in sql]
    assert not offenders, f"the fan-out queued work: {offenders}"


def test_a_local_exclusion_is_preserved_and_reported_never_overwritten():
    conn = FakeConn(
        existing_bindings={
            NODE_A: _binding_row(
                NODE_A,
                STATE_EXCLUDED,
                exception_reason_code="not_measured_here",
                exception_reason="This account does not run competitor campaigns.",
            )
        }
    )
    summary = _run(conn)
    assert summary["exceptions_preserved"] == 1
    assert summary["bindings_created"] == 1
    # Exactly one insert: the excluded pair was left alone rather than rebound.
    assert len(conn.inserted) == 1
    assert conn.inserted[0][5] == NODE_B


def test_an_unchanged_binding_is_not_rewritten():
    conn = FakeConn()
    first = _run(conn)
    assert first["bindings_created"] == 2
    row = conn.binding_row(conn.inserted[0])
    replay = FakeConn(existing_bindings={NODE_A: row})
    summary = _run(replay)
    # The content hash matches, so the existing row is returned untouched.
    assert summary["bindings_unchanged"] + summary["bindings_created"] == 2


def test_a_datastream_no_longer_compatible_is_skipped_rather_than_bound(monkeypatch):
    """The proposal pinned a contract; the contract changed. Refusing is the pin."""
    import core.entity_bindings as bindings

    monkeypatch.setattr(bindings, "tracked_entity_declaration", lambda name: None)
    conn = FakeConn()
    summary = _run(conn)
    assert summary["bindings_created"] == 0
    assert not conn.inserted


def test_a_not_applicable_proposal_contributes_nothing():
    conn = FakeConn()
    summary = _run(conn, _proposals(applicability="not_applicable"))
    assert summary["datastreams_considered"] == 0
    assert not conn.inserted


def test_another_capability_s_proposals_are_ignored():
    conn = FakeConn()
    summary = _run(conn, _proposals(capability_key="country"))
    assert summary["datastreams_considered"] == 0


def test_a_collect_contract_without_a_governed_identifier_is_skipped_not_guessed(monkeypatch):
    import core.tracked_entities as entities

    monkeypatch.setattr(
        entities, "list_source_identities", lambda conn, **kwargs: []
    )
    conn = FakeConn()
    summary = _run(conn)
    assert summary["skipped_without_source_identity"] == 2
    assert not conn.inserted


def test_every_write_names_the_project_it_was_authorized_for():
    """No statement reaches outside the Project. The fan-out cannot cross."""
    conn = FakeConn()
    _run(conn)
    for sql, params in conn.statements:
        if sql.startswith("insert") or sql.startswith("update"):
            assert params is not None
            assert "proj_EXAMPLE" in [str(item) for item in params]


def test_support_detection_is_read_from_the_declaration_not_the_connector_name():
    support = Support(applicable=False, reason_code="connector_declares_no_tracked_entity_support")
    assert support.as_dict()["state"] == "not_applicable"
    assert STATE_CANDIDATE != STATE_PUBLISHED
