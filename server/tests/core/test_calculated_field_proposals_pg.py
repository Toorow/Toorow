"""Story 75-1 -- the promotion rail, proved against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. Every property below is a row, a CHECK, a
partial UNIQUE index or a transaction: the inferred type stored beside the
formula, the exact pins collected by the walk, `applied_ref` naming a change-set
that stands in `prepared` and NOT in `confirmed`, a declined proposal that
touched nothing else, and -- the load-bearing one -- a refused piece leaving no
row behind. A mocked cursor accepts all of that happily.

Every test rolls back: the fixture's connection is never committed.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from core import calculated_field_proposals as proposals
from core.semantic_expressions import ALLOWED_OPERATIONS
from ulid import ULID

from tests.core.test_analyze_artifacts_pg import Chain

pytestmark = pytest.mark.usefixtures("live_postgres")

ACTOR = "owner@example.com"
REVIEWER = "reviewer@example.com"
_HASH = "c" * 64


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _metric(chain: Chain, name: str, value_type: str, unit: str | None = None) -> tuple[str, str]:
    """One published metric Concept and its version 1. Returns the EXACT pair."""
    concept_id, version_id = _uid("sc"), _uid("scv")
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts (id, project_id, kind, name, "
            "lifecycle_status, created_by) VALUES (%s, %s, 'metric', %s, 'published', 'test')",
            (concept_id, chain.project_id, name),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name, label,
                 value_type, unit, expression, aggregation, additivity_class,
                 content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'metric', %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, 'additive', %s, 'test')
            """,
            (
                version_id, concept_id, chain.project_id, name, name, value_type, unit,
                json.dumps({"op": "source_measure", "concept": name}),
                json.dumps({"function": "sum"}),
                _HASH,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (version_id, concept_id),
        )
    return concept_id, version_id


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


@pytest.fixture()
def pins(chain):
    """Two exact Concept versions a formula may reference: a count and money."""
    return {
        "clicks": _metric(chain, "clicks", "integer"),
        "spend": _metric(chain, "spend", "money", unit="EUR"),
    }


def _ref(pin: tuple[str, str]) -> dict:
    return {"op": "concept_ref", "concept_id": pin[0], "version_id": pin[1]}


def _ratio(pins) -> dict:
    """`spend / clicks` -- money over a count, with the zero case declared."""
    return {
        "op": "ratio",
        "numerator": _ref(pins["spend"]),
        "denominator": _ref(pins["clicks"]),
        "zero_denominator": "null",
    }


def _file(chain, pins, **overrides):
    payload = {
        "org_id": chain.org_id,
        "project_id": chain.project_id,
        "name": "cost_per_click",
        "expression": _ratio(pins),
        "provenance": {"result_id": chain.result_id},
        "origin": "human",
        "requested_by": ACTOR,
    }
    payload.update(overrides)
    return proposals.propose(chain.conn, **payload)


def _count(conn, table: str, project_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = %s", (project_id,))  # noqa: S608
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# A real INSERT, with the type INFERRED and every reference PINNED
# ---------------------------------------------------------------------------


def test_a_proposal_is_a_row_whose_type_and_pins_the_server_inferred(chain, pins):
    filed = _file(chain, pins)

    assert filed["id"].startswith("cfp_")
    assert filed["status"] == "open"
    assert filed["origin"] == "human"
    assert filed["resolved_by"] is None and filed["resolved_at"] is None
    assert filed["applied_ref"] is None

    # INFERRED, not declared: money over a count is a RATIO, not money, and the
    # caller never said so. `_RATIOISH` is its own family in the contract, so
    # summing it later is refused by name -- which is the whole point of
    # inferring instead of believing.
    assert filed["value_type"] == "ratio"

    # The exact pins the walk collected, both of them, with their role.
    dependencies = filed["dependencies"]
    assert {(d["concept_id"], d["version_id"]) for d in dependencies} == {
        pins["spend"], pins["clicks"]
    }

    # The provenance was VERIFIED, and the query spec version was derived from
    # the Result rather than taken from the caller.
    assert filed["provenance"]["origin"] == "exploration"
    assert filed["provenance"]["result_id"] == chain.result_id
    assert filed["provenance"]["query_spec_version_id"] == chain.query_spec_version_id

    # One append-only trace row for the act.
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT event, actor FROM app.calculated_field_proposal_events "
            "WHERE proposal_id = %s",
            (filed["id"],),
        )
        assert cur.fetchall() == [("proposed", ACTOR)]

    queue = proposals.list_open(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id
    )
    assert [row["id"] for row in queue] == [filed["id"]]


# ---------------------------------------------------------------------------
# Refused BY NAME -- and nothing written
# ---------------------------------------------------------------------------


def test_an_operation_outside_the_allowlist_is_refused_by_name(chain, pins):
    assert "raw_sql" not in ALLOWED_OPERATIONS
    with pytest.raises(proposals.CalculatedFieldProposalError) as excinfo:
        _file(chain, pins, expression={"op": "raw_sql", "sql": "SELECT 1"})

    assert excinfo.value.code == "invalid_expression"
    assert [r["code"] for r in excinfo.value.refusals] == ["unknown_operation"]
    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0


def test_a_reference_without_its_version_is_refused_rather_than_followed(chain, pins):
    with pytest.raises(proposals.CalculatedFieldProposalError) as excinfo:
        _file(
            chain,
            pins,
            expression={
                "op": "ratio",
                "numerator": {"op": "concept_ref", "concept_id": pins["spend"][0]},
                "denominator": _ref(pins["clicks"]),
                "zero_denominator": "null",
            },
        )

    assert excinfo.value.code == "invalid_expression"
    assert "malformed_node" in {r["code"] for r in excinfo.value.refusals}
    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0


def test_a_name_carried_instead_of_a_pin_can_be_parsed_and_never_promoted(chain, pins):
    with pytest.raises(proposals.CalculatedFieldProposalError) as excinfo:
        _file(
            chain,
            pins,
            expression={
                "op": "ratio",
                "numerator": {"op": "concept_name", "name": "spend"},
                "denominator": _ref(pins["clicks"]),
                "zero_denominator": "null",
            },
        )

    assert excinfo.value.code == "unresolved_reference"
    assert "spend" in excinfo.value.message
    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0


def test_adding_a_count_to_an_amount_of_money_is_refused_not_summed(chain, pins):
    with pytest.raises(proposals.CalculatedFieldProposalError) as excinfo:
        _file(
            chain,
            pins,
            name="clicks_plus_spend",
            expression={
                "op": "add",
                "operands": [_ref(pins["clicks"]), _ref(pins["spend"])],
            },
        )

    assert excinfo.value.code == "invalid_expression"
    assert "incompatible_types" in {r["code"] for r in excinfo.value.refusals}
    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0


def test_a_pin_this_project_cannot_read_is_refused_as_unknown(chain, pins):
    with pytest.raises(proposals.CalculatedFieldProposalError) as excinfo:
        _file(
            chain,
            pins,
            expression={
                "op": "ratio",
                "numerator": {
                    "op": "concept_ref",
                    "concept_id": _uid("sc"),
                    "version_id": _uid("scv"),
                },
                "denominator": _ref(pins["clicks"]),
                "zero_denominator": "null",
            },
        )

    assert "unknown_reference" in {r["code"] for r in excinfo.value.refusals}
    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0


def test_a_provenance_this_project_does_not_hold_is_refused(chain, pins):
    with pytest.raises(proposals.CalculatedFieldProposalError) as excinfo:
        _file(chain, pins, provenance={"result_id": _uid("qr")})
    assert excinfo.value.code == "unknown_provenance"

    with pytest.raises(proposals.CalculatedFieldProposalError) as missing:
        _file(chain, pins, provenance={})
    assert missing.value.code == "missing_provenance"

    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0


def test_a_name_no_concept_could_ever_carry_is_refused_at_proposal_time(chain, pins):
    with pytest.raises(proposals.CalculatedFieldProposalError) as excinfo:
        _file(chain, pins, name="Cost Per Click")
    assert excinfo.value.code == "invalid_name"
    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0


def test_a_refused_piece_leaves_neither_a_proposal_nor_a_trace_row(chain, pins):
    """One transaction. The provenance is checked AFTER the formula, so a formula
    that passed and a Result that does not exist must still write nothing --
    including no audit row and no event row."""
    before_audit = _audit_count(chain.conn)
    with pytest.raises(proposals.CalculatedFieldProposalError):
        _file(chain, pins, provenance={"result_id": _uid("qr")})

    assert _count(chain.conn, "app.calculated_field_proposals", chain.project_id) == 0
    assert _count(chain.conn, "app.calculated_field_proposal_events", chain.project_id) == 0
    assert _audit_count(chain.conn) == before_audit


def _audit_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.audit_log WHERE action IN (%s, %s)",
            (
                proposals.ACTION_CALCULATED_FIELD_PROPOSED,
                proposals.ACTION_CALCULATED_FIELD_RESOLVED,
            ),
        )
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Accepting PREPARES. It never publishes.
# ---------------------------------------------------------------------------


def test_accepting_leaves_a_prepared_change_set_and_names_it_in_applied_ref(chain, pins):
    filed = _file(chain, pins)

    resolved = proposals.resolve(
        chain.conn,
        proposal_id=filed["id"],
        org_id=chain.org_id,
        status="accepted",
        resolved_by=REVIEWER,
    )

    assert resolved["status"] == "accepted"
    assert resolved["resolved_by"] == REVIEWER
    assert resolved["resolved_at"] is not None
    change_set_id = resolved["applied_ref"]
    assert change_set_id and change_set_id.startswith("scs_")

    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT state, object_type, object_id, intent, diff, validation, "
            "       result_version_id, created_by "
            "  FROM app.semantic_change_sets WHERE id = %s",
            (change_set_id,),
        )
        row = cur.fetchone()

    assert row is not None
    # PREPARED, and nothing beyond it: no version was published.
    assert row[0] == proposals.PREPARED_STATE
    assert row[1] == "semantic-concept"
    assert row[2] is None
    assert row[6] is None
    assert row[7] == REVIEWER

    intent = row[3]
    assert intent["action"] == "create_concept"
    assert intent["concept"]["name"] == "cost_per_click"
    assert intent["concept"]["kind"] == "metric"
    assert intent["concept"]["value_type"] == "ratio"
    assert intent["concept"]["expression"] == _ratio(pins)
    # The exploration mark rides at the TOP level of the intent, where
    # `create_change_set` stores it verbatim.
    assert intent["provenance"]["origin"] == "exploration"
    assert intent["provenance"]["proposal_id"] == filed["id"]
    assert intent["provenance"]["result_id"] == chain.result_id

    # PRE-FILLED: the diff and the validation were computed, not left empty.
    assert row[4]["kind"] == "creation"
    assert row[5]["action"] == "create_concept"
    assert row[5]["expression"]["ok"] is True
    assert row[5]["test_gate"]["state"] in {"unverifiable", "fail", "pass"}

    # Nothing was published: no Concept and no Concept version was created.
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.semantic_concepts "
            "WHERE project_id = %s AND name = 'cost_per_click'",
            (chain.project_id,),
        )
        assert int(cur.fetchone()[0]) == 0

    # And the queue no longer offers it.
    assert proposals.list_open(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id
    ) == []


def test_the_prepared_change_set_says_what_a_person_still_has_to_declare(chain, pins):
    """A promotion carries the formula, not the aggregation declaration. The
    refusal is VISIBLE rather than derived on someone's behalf."""
    filed = _file(chain, pins)
    resolved = proposals.resolve(
        chain.conn,
        proposal_id=filed["id"],
        org_id=chain.org_id,
        status="accepted",
        resolved_by=REVIEWER,
    )
    assert "undeclared_aggregation" in {
        item["code"] for item in resolved["prepare_refusals"]
    }
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT validation FROM app.semantic_change_sets WHERE id = %s",
            (resolved["applied_ref"],),
        )
        validation = cur.fetchone()[0]
    assert validation["publishable"] is False


def test_declining_writes_the_verdict_and_touches_nothing_else(chain, pins):
    filed = _file(chain, pins)

    resolved = proposals.resolve(
        chain.conn,
        proposal_id=filed["id"],
        org_id=chain.org_id,
        status="declined",
        resolved_by=REVIEWER,
    )

    assert resolved["status"] == "declined"
    assert resolved["resolved_by"] == REVIEWER
    assert resolved["applied_ref"] is None
    # No change-set was opened at all.
    assert _count(chain.conn, "app.semantic_change_sets", chain.project_id) == 0
    # Everything else the proposal carried is byte for byte what was filed.
    for column in ("name", "expression", "value_type", "dependencies", "provenance"):
        assert resolved[column] == filed[column]


@pytest.mark.parametrize("seam", ["create_change_set", "prepare_change_set"])
def test_an_acceptance_whose_change_set_fails_leaves_the_proposal_open(chain, pins, seam):
    """THE RESOLUTION HALF of the transaction claim, which nothing measured.

    `resolve` promises "both halves are ONE transaction: a promotion whose
    change-set is refused leaves the proposal open, because a proposal marked
    accepted with nothing behind it is the one state nobody can act on". The
    proposal half of that claim is proved by
    `test_a_refused_piece_leaves_neither_a_proposal_nor_a_trace_row`; this half
    was a sentence. If the UPDATE stood while the change-set seam failed, the row
    would read `accepted` with an empty `applied_ref` -- gone from the queue,
    having opened nothing, and unreachable by a second verdict because the
    partial UNIQUE index and the `open` predicate both consider it decided.

    Both seams are forced, because they are two statements of the same
    transaction and only the second one runs after a change-set row exists.
    """
    import core.semantic_model

    filed = _file(chain, pins)
    events_before = _count(
        chain.conn, "app.calculated_field_proposal_events", chain.project_id
    )
    audit_before = _audit_count(chain.conn)

    with patch.object(
        core.semantic_model, seam, side_effect=RuntimeError("the change-set seam is down")
    ):
        with pytest.raises(RuntimeError):
            proposals.resolve(
                chain.conn,
                proposal_id=filed["id"],
                org_id=chain.org_id,
                status="accepted",
                resolved_by=REVIEWER,
            )

    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT status, applied_ref, resolved_by, resolved_at "
            "FROM app.calculated_field_proposals WHERE id = %s",
            (filed["id"],),
        )
        status, applied_ref, resolved_by, resolved_at = cur.fetchone()
    assert status == proposals.OPEN_STATUS
    assert applied_ref is None
    assert resolved_by is None
    assert resolved_at is None

    # Not one row of the verdict survived: no trace event, no audit row, no
    # half-opened change-set.
    assert (
        _count(chain.conn, "app.calculated_field_proposal_events", chain.project_id)
        == events_before
    )
    assert _audit_count(chain.conn) == audit_before
    assert _count(chain.conn, "app.semantic_change_sets", chain.project_id) == 0

    # AND THE QUEUE STILL HOLDS IT. A verdict that failed is a verdict not given,
    # so the person who tried can try again.
    still_open = proposals.list_open(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id
    )
    assert [row["id"] for row in still_open] == [filed["id"]]


def test_a_second_verdict_is_a_conflict_and_not_an_absence(chain, pins):
    filed = _file(chain, pins)
    proposals.resolve(
        chain.conn,
        proposal_id=filed["id"],
        org_id=chain.org_id,
        status="declined",
        resolved_by=REVIEWER,
    )
    with pytest.raises(proposals.ProposalAlreadyResolvedError) as excinfo:
        proposals.resolve(
            chain.conn,
            proposal_id=filed["id"],
            org_id=chain.org_id,
            status="accepted",
            resolved_by=REVIEWER,
        )
    assert excinfo.value.code == "proposal_already_resolved"

    # An id that belongs to no one answers absence, not conflict.
    assert proposals.resolve(
        chain.conn,
        proposal_id=_uid("cfp"),
        org_id=chain.org_id,
        status="accepted",
        resolved_by=REVIEWER,
    ) is None


def test_a_neighbours_organization_cannot_resolve_this_proposal(chain, pins):
    filed = _file(chain, pins)
    assert proposals.resolve(
        chain.conn,
        proposal_id=filed["id"],
        org_id=_uid("org"),
        status="accepted",
        resolved_by=REVIEWER,
    ) is None
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM app.calculated_field_proposals WHERE id = %s",
            (filed["id"],),
        )
        assert cur.fetchone()[0] == "open"


# ---------------------------------------------------------------------------
# The schema's own promises
# ---------------------------------------------------------------------------


def test_the_same_name_cannot_sit_open_twice_and_a_verdict_frees_it(chain, pins):
    import psycopg

    _file(chain, pins)
    # `pytest.raises` OUTSIDE the transaction block: the savepoint must see the
    # exception to roll back to itself. Swallowing it first leaves an aborted
    # transaction and the RELEASE fails with InFailedSqlTransaction.
    with pytest.raises(psycopg.errors.UniqueViolation), chain.conn.transaction():
        _file(chain, pins)
        raise AssertionError("a second open proposal for one name must be refused")


def test_the_trace_is_append_only(chain, pins):
    """TWO LAYERS, AND THEY REFUSE DIFFERENTLY -- which is the point.

    UPDATE is REVOKED from the application role (migration 316's lesson: a
    narrow GRANT alone only says otherwise), so a rewrite never reaches the
    trigger: it is `InsufficientPrivilege`. DELETE is granted, because it is the
    RGPD hatch, so a deletion outside a flagged erasure reaches the trigger and
    is refused there. Asserting one exception for both would have hidden
    whichever layer was missing.
    """
    import psycopg

    filed = _file(chain, pins)
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.calculated_field_proposal_events WHERE proposal_id = %s",
            (filed["id"],),
        )
        event_id = cur.fetchone()[0]

    refusals = (
        (
            "UPDATE app.calculated_field_proposal_events SET actor = 'someone else' "
            "WHERE id = %s",
            psycopg.errors.InsufficientPrivilege,
        ),
        (
            "DELETE FROM app.calculated_field_proposal_events WHERE id = %s",
            # SQLSTATE 23000, which the guard raises deliberately -- psycopg maps
            # it to IntegrityConstraintViolation, not to RaiseException.
            psycopg.errors.IntegrityConstraintViolation,
        ),
    )
    for statement, expected in refusals:
        with pytest.raises(expected), chain.conn.transaction():
            with chain.conn.cursor() as cur:
                cur.execute(statement, (event_id,))
            raise AssertionError("the trace accepted a rewrite")


def test_the_trace_yields_to_a_flagged_organization_erasure(chain, pins):
    """The hatch of migration 099: without it, `core/org_purge.py` fails here."""
    filed = _file(chain, pins)
    with chain.conn.transaction():
        with chain.conn.cursor() as cur:
            cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
            cur.execute(
                "DELETE FROM app.calculated_field_proposal_events WHERE proposal_id = %s",
                (filed["id"],),
            )
            assert cur.rowcount == 1
