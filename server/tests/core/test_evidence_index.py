"""Story 49.5 — the reference index, its projector, its cursors and its refusals.

These are the properties that hold WITHOUT a database: the shape of a normalized
reference event, the closed vocabularies, the cursor contract, the filter
allowlist and the registry's own completeness. The properties that are database
properties — immutability triggers, fail-closed RLS, the unique source identity —
live in `server/tests/integration/test_evidence_index_constraints.py` and
`..._isolation.py`, because a mocked cursor proves nothing about a trigger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from core.evidence_index import (
    CORRELATION_KINDS,
    LENS_RECORD_KIND,
    MAX_PAGE,
    PRODUCERS,
    RECORD_KINDS,
    EvidenceConflict,
    EvidenceCursorInvalid,
    EvidenceFilterInvalid,
    EvidenceIndexError,
    LinkSpec,
    ReferenceEvent,
    decode_cursor,
    encode_cursor,
    is_w3c_trace_id,
    normalize_filters,
    owner_route,
    producer_inventory,
    register_reference,
)

from tests.support.statement_router import (  # noqa: E402
    StatementInventory,
    UnknownStatement,
    projected_columns,
)

ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
MOMENT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)

# THE FIVE STATEMENTS THE PROJECTOR MAKES. Named here rather than recognised two
# at a time with an `else` for the rest: three of them are the ledgers this act
# also writes, and an `else` that answers them cannot tell a ledger that was
# written from a query that moved (AI-317).
_PROJECTOR = StatementInventory(
    "_Cursor",
    by_source=("select id, source_payload_hash", "from app.evidence_records"),
    insert_record="insert into app.evidence_records",
    availability="insert into app.evidence_availability_events",
    quarantine="insert into app.evidence_projection_quarantine",
    watermark="insert into app.evidence_index_watermarks",
)


def _event(**overrides) -> ReferenceEvent:
    base = {
        "producer": "data_execution",
        "record_kind": "evidence_trace",
        "owner_workspace": "data",
        "owner_object_type": "datastream-execution",
        "owner_object_id": "dse_1",
        "occurred_at": MOMENT,
        "source_identity_key": "data_execution|dse_1",
        "is_anchor": True,
    }
    base.update(overrides)
    return ReferenceEvent(**base)


class _Cursor:
    """Answers each query by the table it reads, and records every write."""

    def __init__(self, existing: list[dict] | None = None):
        self.existing = existing or []
        self.executed: list[tuple[str, dict]] = []
        self._rows: list[dict] = []
        # None until a statement says otherwise -- a cursor that has run nothing
        # has no description, exactly as psycopg reports it.
        self._columns: list[str] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query: str, params=None):
        self.executed.append((query, dict(params or {})))
        statement = _PROJECTOR.match(query)
        # The columns come from the statement, never from a list kept here: this
        # fake used to answer `["id"]` to anything it did not recognise, and
        # `_fetch` zips `cur.description` against the row STRICTLY -- so a
        # widened SELECT would have raised inside the fixture, naming the
        # product (AI-317).
        if statement in {"by_source", "insert_record"}:
            self._columns = projected_columns(query)
        else:
            # No result set at all -- which is what `description = None` means,
            # and the only honest answer for a write with no RETURNING.
            self._columns = None
        if statement == "by_source":
            key = (params or {}).get("key")
            self._rows = [row for row in self.existing if row["source_identity_key"] == key]
        elif statement == "insert_record":
            self._rows = [{"id": params["id"]}]
            self.existing.append(
                {
                    "id": params["id"],
                    "source_identity_key": params["source_identity_key"],
                    "source_payload_hash": params["source_payload_hash"],
                }
            )
        else:
            # The three side ledgers of the projector -- availability events,
            # quarantine, watermark. They are written, never read back here.
            self._rows = []

    @property
    def description(self):
        if self._columns is None:
            return None
        return [(name,) for name in self._columns]

    def fetchall(self):
        return [tuple(row[name] for name in self._columns or ()) for row in self._rows]


def _conn(existing: list[dict] | None = None) -> MagicMock:
    cursor = _Cursor(existing)
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.evidence_cursor = cursor
    return connection


def test_the_projector_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317: the `else` used to answer `[]` with a one-column description.

    `_fetch` zips `cur.description` against each row with `strict=True`, so a
    fake that answers the wrong column list does not return wrong data -- it
    raises inside the product's own helper, and the failure reads as a product
    bug. Refusing here names the query instead.
    """
    cursor = _Cursor()
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute("SELECT id FROM app.evidence_links WHERE project_id = %(project_id)s")
    assert "app.evidence_links" in str(raised.value)
    assert "quarantine" in str(raised.value)


def test_the_projector_fake_reports_the_columns_the_statement_asked_for() -> None:
    """Derived both ways: from a SELECT list and from a write's RETURNING."""
    cursor = _Cursor()
    cursor.execute(
        "SELECT id, source_payload_hash FROM app.evidence_records "
        "WHERE project_id = %(project_id)s AND source_identity_key = %(key)s",
        {"project_id": PROJECT, "key": "data_execution|dse_1"},
    )
    assert cursor.description == [("id",), ("source_payload_hash",)]
    cursor.execute(
        "INSERT INTO app.evidence_index_watermarks (project_id) VALUES (%(project_id)s)",
        {"project_id": PROJECT},
    )
    assert cursor.description is None


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_every_producer_is_indexed_unavailable_or_excluded_with_a_reason():
    """No hidden subset. That is the whole point of the registry.

    The previous Evidence surface read three tables and named none of the ones
    it did not read, so a Project with no execution evidence and a Project whose
    execution evidence was never wired looked identical.
    """
    for entry in producer_inventory():
        assert entry["disposition"] in {"indexed", "future_owner", "excluded"}
        if entry["disposition"] != "indexed":
            assert entry["reason_code"], entry["producer"]
            assert entry["reason"], entry["producer"]


def test_the_future_owners_are_named_and_none_of_them_is_indexed():
    """Two remain. `context_ai_path` left this set when Story 49.6 shipped its
    owner — see test_the_ai_path_adapter_is_active_now_that_its_owner_exists."""
    future = {e["producer"] for e in producer_inventory() if e["disposition"] == "future_owner"}
    assert future == {"analyze_render", "test_evaluation_run"}


def test_an_incumbent_table_is_never_relabelled_as_the_ratified_owner():
    """`eval_runs` is not an Evaluation Run, and `render_snapshots` is not a
    Render. The reasons say so in words, because the next person to read this
    registry is the one tempted to wire them up."""
    reasons = {e["producer"]: (e["reason"] or "") for e in producer_inventory()}
    assert "eval_runs" in reasons["test_evaluation_run"]
    assert "render_snapshots" in reasons["analyze_render"]
    # `context_ai_path` no longer carries a reason because it is indexed now.
    # That the resolution table STILL is not its source is asserted structurally
    # by test_the_resolution_table_is_still_not_wired_up_as_an_ai_path, which is
    # stronger than a sentence: it reads every registered query.


def test_every_indexed_producer_carries_a_bounded_project_scoped_backfill():
    for contract in PRODUCERS:
        if not contract.is_indexed:
            continue
        assert contract.backfill_sql and contract.row_to_event, contract.producer
        assert "%(project_id)s" in contract.backfill_sql, contract.producer
        assert "LIMIT %(limit)s" in contract.backfill_sql, contract.producer
        # Deterministic order, so a watermark means the same thing on a restart.
        assert "ORDER BY" in contract.backfill_sql, contract.producer


def test_each_lens_maps_to_exactly_one_record_kind():
    assert set(LENS_RECORD_KIND.values()) == set(RECORD_KINDS)
    assert len(set(LENS_RECORD_KIND.values())) == 3


# ---------------------------------------------------------------------------
# The normalized event
# ---------------------------------------------------------------------------


def test_only_a_trace_record_can_anchor_a_trace():
    with pytest.raises(EvidenceIndexError):
        _event(record_kind="object_version", is_anchor=True)


def test_an_unregistered_relation_correlation_or_workspace_is_refused():
    with pytest.raises(EvidenceIndexError):
        LinkSpec(
            relation="caused_by",
            to_owner_workspace="data",
            to_owner_object_type="x",
            to_owner_object_id="1",
        )
    with pytest.raises(EvidenceIndexError):
        _event(correlations=(("guess", "1"),))
    with pytest.raises(EvidenceIndexError):
        _event(owner_workspace="marketing")


def test_a_link_has_exactly_one_destination():
    with pytest.raises(EvidenceIndexError):
        LinkSpec(relation="derives_from")
    with pytest.raises(EvidenceIndexError):
        LinkSpec(
            relation="derives_from",
            to_source_identity_key="a",
            to_owner_workspace="data",
            to_owner_object_type="datastream",
            to_owner_object_id="ds_1",
        )


def test_the_payload_hash_ignores_when_it_was_indexed():
    """A replay an hour later describes the same fact. It must be a no-op, not a
    conflict, so `indexed_at` cannot participate in the identity."""
    first = _event()
    second = _event()
    assert first.payload_hash == second.payload_hash
    assert _event(occurred_at=datetime(2020, 1, 1, tzinfo=UTC)).payload_hash != first.payload_hash


# ---------------------------------------------------------------------------
# The projector
# ---------------------------------------------------------------------------


def test_registering_the_same_artifact_twice_is_a_no_op():
    conn = _conn()
    first_id, created = register_reference(conn, org_id=ORG, project_id=PROJECT, event=_event())
    assert created is True
    second_id, created_again = register_reference(
        conn, org_id=ORG, project_id=PROJECT, event=_event()
    )
    assert second_id == first_id
    assert created_again is False


def test_a_conflicting_payload_under_the_same_identity_fails_closed():
    """It never updates the immutable record, and never appends a second one.

    Both would be silent: one rewrites a claim, the other doubles the owner.
    """
    conn = _conn()
    register_reference(conn, org_id=ORG, project_id=PROJECT, event=_event())
    with pytest.raises(EvidenceConflict):
        register_reference(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            event=_event(occurred_at=datetime(2026, 7, 31, tzinfo=UTC)),
        )
    updates = [q for q, _ in conn.evidence_cursor.executed if "UPDATE app.evidence_records" in q]
    assert updates == []


def test_an_edge_to_an_unindexed_destination_is_dropped_not_guessed():
    """A missing edge stops the proven chain.

    Downgrading it to a fabricated owner reference would draw a step nobody
    observed, which is exactly the inference the contract forbids.
    """
    conn = _conn()
    register_reference(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        event=_event(
            source_identity_key="data_execution_stage|dsse_1",
            is_anchor=False,
            links=(
                LinkSpec(relation="anchored_by", to_source_identity_key="data_execution|absent"),
            ),
        ),
    )
    links = [q for q, _ in conn.evidence_cursor.executed if "INSERT INTO app.evidence_links" in q]
    assert links == []


def test_an_organization_or_project_is_required():
    with pytest.raises(EvidenceIndexError):
        register_reference(_conn(), org_id="", project_id=PROJECT, event=_event())
    with pytest.raises(EvidenceIndexError):
        register_reference(_conn(), org_id=ORG, project_id="", event=_event())


# ---------------------------------------------------------------------------
# Cursors and filters
# ---------------------------------------------------------------------------


def test_a_cursor_minted_elsewhere_is_refused_rather_than_reinterpreted():
    row = {"occurred_at": MOMENT, "id": "evr_1"}
    cursor = encode_cursor(PROJECT, "audit-activity", {}, row)
    assert decode_cursor(cursor, PROJECT, "audit-activity", {}) == ("2026-07-30T09:00:00Z", "evr_1")

    with pytest.raises(EvidenceCursorInvalid):
        decode_cursor(cursor, "proj_OTHER", "audit-activity", {})
    with pytest.raises(EvidenceCursorInvalid):
        decode_cursor(cursor, PROJECT, "lineage-provenance", {})
    with pytest.raises(EvidenceCursorInvalid):
        decode_cursor(cursor, PROJECT, "audit-activity", {"outcome": "success"})


def test_a_malformed_cursor_does_not_fall_back_to_page_one():
    for candidate in ("", "not-base64!!", "eyJhIjoxfQ"):
        with pytest.raises(EvidenceCursorInvalid):
            decode_cursor(candidate, PROJECT, "audit-activity", {})


def test_a_cursor_is_opaque():
    cursor = encode_cursor(PROJECT, "audit-activity", {}, {"occurred_at": MOMENT, "id": "evr_1"})
    assert PROJECT not in cursor
    assert "evr_1" not in cursor


def test_an_unknown_filter_is_refused_not_dropped():
    """A silently ignored filter renders a page that does not match the address
    that produced it."""
    with pytest.raises(EvidenceFilterInvalid):
        normalize_filters("audit-activity", {"project_id": "proj_OTHER"})
    with pytest.raises(EvidenceFilterInvalid):
        normalize_filters("audit-activity", {"owner_workspace": "marketing"})
    with pytest.raises(EvidenceFilterInvalid):
        normalize_filters("audit-activity", {"correlation_kind": "guess"})
    with pytest.raises(EvidenceFilterInvalid):
        normalize_filters("audit-activity", {"from": "last tuesday"})
    with pytest.raises(EvidenceFilterInvalid):
        normalize_filters("audit-activity", {"outcome": "x" * 201})


def test_a_lens_cannot_be_asked_to_show_another_lens_records():
    with pytest.raises(EvidenceFilterInvalid):
        normalize_filters("audit-activity", {"record_kind": "evidence_trace"})
    assert normalize_filters("audit-activity", {"record_kind": "audit_event"}) == {
        "record_kind": "audit_event"
    }


def test_the_collection_bound_stays_a_display_bound():
    assert MAX_PAGE == 200


# ---------------------------------------------------------------------------
# Correlations and owner routes
# ---------------------------------------------------------------------------


def test_a_w3c_trace_id_is_validated_but_nothing_is_derived_from_it():
    assert is_w3c_trace_id("a" * 32)
    assert not is_w3c_trace_id("0" * 32)  # all-zero is invalid by the spec
    assert not is_w3c_trace_id("A" * 32)  # lowercase hex only
    assert not is_w3c_trace_id("a" * 31)
    assert not is_w3c_trace_id("trace_1")


def test_every_correlation_kind_is_closed_and_names_a_real_identifier():
    assert "operation" in CORRELATION_KINDS
    assert "w3c_trace" in CORRELATION_KINDS
    # No catch-all: an "other" bucket is where an unvalidated identifier lands.
    assert "other" not in CORRELATION_KINDS
    assert "metadata" not in CORRELATION_KINDS


def test_an_unroutable_owner_type_yields_no_link_rather_than_a_guessed_address():
    assert owner_route("datastream-execution", "dse_1") is not None
    assert owner_route("some-future-object", "x_1") is None
    assert owner_route("datastream-execution", "") is None


def test_no_raw_url_is_ever_stored_or_emitted():
    route = owner_route("datastream-mapping-version", "ds_1", "dmap_2")
    assert route is not None
    assert "http" not in str(route)
    assert route["object_id"] == "ds_1"
    assert route["version_id"] == "dmap_2"


# ---------------------------------------------------------------------------
# The durable projector
# ---------------------------------------------------------------------------


def test_every_mapped_outbox_event_names_a_registered_indexed_producer():
    """An event type that mapped to nothing would index nothing, silently."""
    from core.evidence_index import OUTBOX_EVENT_PRODUCERS, PRODUCERS_BY_KEY

    for event_type, producer in OUTBOX_EVENT_PRODUCERS.items():
        contract = PRODUCERS_BY_KEY.get(producer)
        assert contract is not None, event_type
        assert contract.is_indexed, event_type


def test_the_projector_never_reads_the_outbox_payload():
    """The event says WHICH producer moved; the producer is then re-read.

    Consuming the payload would make the index a mirror of whatever shape the
    owner happens to publish — the generic event store the contract forbids —
    and would index a claim rather than the owner's current truth.
    """
    from core.evidence_index import _PENDING_OUTBOX

    assert "payload" not in _PENDING_OUTBOX
    assert "o.event_type" in _PENDING_OUTBOX
    # Scoped by the authorized organization, and bounded.
    assert "op.effective_org_id = %(org_id)s" in _PENDING_OUTBOX
    assert "LIMIT %(limit)s" in _PENDING_OUTBOX


def test_an_unmapped_event_type_is_ignored_rather_than_guessed():
    from core.evidence_index import OUTBOX_EVENT_PRODUCERS

    assert "some.future.event" not in OUTBOX_EVENT_PRODUCERS
    assert OUTBOX_EVENT_PRODUCERS.get("some.future.event") is None


def test_the_projector_keeps_its_own_position_without_posing_as_an_adapter():
    """It shares the watermark table under a reserved key.

    A projector that registered itself as a producer would appear in coverage as
    an Evidence source, which it is not: it is how the index moves, not what the
    index saw.
    """
    from core.evidence_index import _PROJECTION_KEY, PRODUCERS_BY_KEY

    assert _PROJECTION_KEY not in PRODUCERS_BY_KEY


# ---------------------------------------------------------------------------
# The AI Path adapter (activated once Story 49.6 landed its owner)
# ---------------------------------------------------------------------------


def test_the_ai_path_adapter_is_active_now_that_its_owner_exists():
    """`future_owner` was true until migration 150 and `core/ai_paths.py` landed.

    Leaving it declared unavailable after the owner shipped would be the mirror
    of fabricating one: reporting an absence that is no longer real.
    """
    inventory = {entry["producer"]: entry for entry in producer_inventory()}
    assert inventory["context_ai_path"]["disposition"] == "indexed"
    assert inventory["context_ai_path_step"]["disposition"] == "indexed"
    assert inventory["context_ai_path"]["creates_anchor"] is True


def test_the_two_remaining_future_owners_are_still_honestly_unavailable():
    future = {e["producer"] for e in producer_inventory() if e["disposition"] == "future_owner"}
    assert future == {"analyze_render", "test_evaluation_run"}


def test_only_a_finalized_ai_path_is_indexed():
    """A `recording` path is still being appended to.

    Its content hash is not frozen, so indexing it would register an identity
    whose payload changes underneath the record — exactly what the conflict rule
    exists to refuse, turned into a guaranteed conflict on the next pass.
    """
    from core.evidence_index import _CONTEXT_AI_PATH_STEPS_SQL, _CONTEXT_AI_PATHS_SQL

    assert "p.lifecycle = 'finalized'" in _CONTEXT_AI_PATHS_SQL
    # A step is only as frozen as its path, so it carries the same condition.
    assert "p.lifecycle = 'finalized'" in _CONTEXT_AI_PATH_STEPS_SQL
    assert "JOIN app.ai_paths p" in _CONTEXT_AI_PATH_STEPS_SQL


def test_the_resolution_table_is_still_not_wired_up_as_an_ai_path():
    """`context_path_resolutions` answers which path a question SHOULD take.

    An AI Path answers which path an execution DID take. Two different claims,
    and the whole reason the adapter waited for a real owner instead of pointing
    at the incumbent table.
    """
    from core.evidence_index import PRODUCERS

    for contract in PRODUCERS:
        assert "context_path_resolutions" not in (contract.backfill_sql or "")


def test_an_ai_path_step_carries_its_owner_proven_ordinal():
    from core.evidence_index import _context_ai_path_step_event

    event = _context_ai_path_step_event(
        {
            "id": "aps_1",
            "path_id": "aip_1",
            "ordinal": 3,
            "observed_at": MOMENT,
            "step_kind": "semantic_query",
            "owner_workspace": "governance",
            "owner_object_type": "semantic-view-version",
            "owner_object_id": "sv_1",
            "owner_version_id": "svv_2",
            "outcome": "succeeded",
        }
    )
    anchored = [link for link in event.links if link.relation == "anchored_by"]
    assert anchored[0].ordinal == 3
    assert anchored[0].to_source_identity_key == "context_ai_path|aip_1"
    # The step names an exact owner object, so the edge is typed and versioned.
    referenced = [link for link in event.links if link.relation == "references"]
    assert referenced[0].to_owner_object_id == "sv_1"
    assert referenced[0].to_owner_version_id == "svv_2"
    assert event.is_anchor is False


def test_a_step_naming_no_owner_object_emits_no_reference_edge():
    from core.evidence_index import _context_ai_path_step_event

    event = _context_ai_path_step_event(
        {
            "id": "aps_2",
            "path_id": "aip_1",
            "ordinal": 0,
            "observed_at": MOMENT,
            "step_kind": "tool_call",
            "owner_workspace": None,
            "owner_object_type": None,
            "owner_object_id": None,
            "owner_version_id": None,
            "outcome": "refused",
        }
    )
    assert [link.relation for link in event.links] == ["anchored_by"]


def test_the_context_hub_owner_link_is_emitted_now_that_a_screen_renders_one():
    """The instruction this test carried has been executed, so it flips.

    It used to read: "Register the route in the same change that mounts the
    screen." Story 49.6 lot 4 mounted the workbench half of the trace lens --
    the ordered steps now carry their exact owner links -- so the link BACK to
    Context Hub resolves and `owner_route` emits it. Leaving it unregistered
    after the screen shipped would be the mirror of a route without a door:
    a door with no route.

    NO TAB and NO VERSION TAIL: `navigation/contextHub.ts` declares
    `{ type: "ai-path" }` with no tabs, and a path has one content hash rather
    than a version history. A reference naming either would be refused by the
    console's own resolver.
    """
    route = owner_route("ai-path", "aip_1")
    assert route is not None
    assert route["surface"] == "project"
    assert route["workspace"] == "context-hub"
    assert route["section"] == "knowledge-graph"
    assert route["object_type"] == "ai-path"
    assert route["object_id"] == "aip_1"
    assert route["tab"] is None
    assert route["version_id"] is None


def test_a_path_step_still_has_no_screen_of_its_own():
    """A step is read INSIDE its path, and the path is the route.

    `ai-path-step` stays unregistered on purpose: no screen opens one, so a link
    to it would be an address nobody serves. The node keeps its identity and
    reports `owner_route_unavailable` -- an honest absence, not a broken link.
    """
    assert owner_route("ai-path-step", "aps_1") is None


def test_topic_and_procedure_steps_open_their_context_hub_screens() -> None:
    """AI-376 (Opus F3): the console declares context-topic and context-procedure; the route table knows them."""
    from core.evidence_index import owner_route

    topic = owner_route("topic", "topic_1", None)
    procedure = owner_route("procedure", "proc_1", None)
    assert topic and topic["object_type"] == "context-topic" and topic["section"] == "knowledge-library"
    assert procedure and procedure["object_type"] == "context-procedure" and procedure["section"] == "skills-registry"
    assert owner_route("schema-doc", "doc_1", None) is None  # no addressable screen: honest absence
