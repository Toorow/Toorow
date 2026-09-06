"""Story 71.4 -- the composition, and the refusals it owes, without a database.

What is proved here is what a connection cannot help with: that the block bounds
itself and says what it withheld, that a disabled capability names NO column
anywhere in its payload, that an unconverted currency comes back as the answer
rather than as an exception, that the observed and the ventilated blocks are
disjoint, and that a breakdown by a dimension no governed grain relates to the
metric is REFUSED BY NAME.

The store, the real grains and the real pairs are proved in
``test_analytics_alignment_read_pg.py``.

No real identifier appears: ``proj_EXAMPLE``, ``ds_EXAMPLE_*``, ``owner@example.com``.
"""

from __future__ import annotations

import json

import pytest
from core import analytics_alignment_read as reader
from core import metric_dimensions
from core.analytics_alignment import (
    ADDED_COLUMNS,
    AlignmentCurrencies,
    AlignmentDecision,
    AlignmentDependencies,
    AlignmentSide,
    MissingDependency,
)
from core.analytics_ventilation import (
    VENTILATION_COLUMNS,
    VentilationBasis,
    VentilationCandidate,
    VentilationGroup,
)

PROJECT = "proj_EXAMPLE"
LEFT = "ds_EXAMPLE_media"
RIGHT = "ds_EXAMPLE_analytics"
KEY_VERSION = "mckv_EXAMPLE"
ACTOR = "owner@example.com"

_PAIR = {
    "left_datastream_id": LEFT,
    "right_datastream_id": RIGHT,
    "common_key_version_id": KEY_VERSION,
    "relationship_name": "media_to_analytics",
    "view_version_id": "svv_EXAMPLE",
}


class _NoConn:
    """A connection nothing in this file is allowed to open. Every read is stubbed."""

    def cursor(self):  # pragma: no cover -- reaching it is the failure
        raise AssertionError("this test must not touch a database")


#: What the registry answers when neither side has been attached to a node. The
#: DEFAULT of the fixture below, because it is the state of every Project that has
#: not used story 70.2's attachment command -- which today is all of them.
_NO_ATTACHMENTS = {
    "state": "unavailable",
    "code": reader.NO_ATTACHMENTS,
    "datastreams_without_attachment": [LEFT, RIGHT],
    "reason": "carries no live observed-entity attachment",
    "gesture": reader.ATTACH_GESTURE,
    "columns_designating_an_object_kind": {LEFT: 0, RIGHT: 0},
}


@pytest.fixture
def stubbed(monkeypatch):
    """The database reads `read_alignment` composes, each replaced by a value.

    ``registry`` is the observed-entity registry's answer -- the pair of values
    `registry_reading` returns. Its DEFAULT is "neither side is attached", so a
    test that says nothing about the registry measures the state a Project is
    actually in; the real registry read is proved against Postgres in
    `test_analytics_alignment_read_pg.py`.
    """

    def _install(
        *,
        pairs=(_PAIR,),
        missing=(),
        decisions=(),
        shares=(),
        registry=(None, _NO_ATTACHMENTS),
    ):
        monkeypatch.setattr(
            reader, "registry_reading", lambda conn, *, project_id, pair: registry
        )
        monkeypatch.setattr(
            reader, "alignable_pairs", lambda conn, *, project_id: [dict(p) for p in pairs]
        )
        monkeypatch.setattr(
            reader,
            "resolve_dependencies",
            lambda conn, *, project_id, left_datastream_id, right_datastream_id: (
                AlignmentDependencies(
                    left_datastream_id=left_datastream_id,
                    right_datastream_id=right_datastream_id,
                    common_key_version_ids=(KEY_VERSION,),
                    relationships=(),
                    currency_fx_state="ready",
                    missing=tuple(missing),
                )
            ),
        )
        monkeypatch.setattr(
            reader,
            "list_alignment_decisions",
            lambda conn, **kwargs: list(decisions),
        )
        monkeypatch.setattr(
            reader, "list_ventilation_weights", lambda conn, **kwargs: list(shares)
        )

    return _install


def _reading(**overrides) -> reader.ObservedReading:
    """One pair with a matched row, an ambiguous row and an unmatched row."""
    base = {
        "left": (
            AlignmentSide(row_key="L1", entity_id="e1"),
            AlignmentSide(row_key="L2", entity_name="Alpha Campaign"),
            AlignmentSide(row_key="L3", entity_id="nobody_publishes_this"),
        ),
        "right": (
            AlignmentSide(row_key="R1", entity_id="e1", entity_name="Direct"),
            AlignmentSide(row_key="R2", entity_name="alpha campaign"),
            AlignmentSide(row_key="R3", entity_name="ALPHA-CAMPAIGN"),
        ),
        "observed_by_row": {"L1": {"spend": "100"}, "L2": {"spend": "200"}},
        "groups": (
            VentilationGroup.of(
                "L2",
                "2026-08-01",
                [VentilationCandidate("R2", 30), VentilationCandidate("R3", 70)],
            ),
        ),
        "basis": VentilationBasis(volume_name="clicks", declared_version="clicks_v1"),
        "day": "2026-08-01",
    }
    base.update(overrides)
    return reader.ObservedReading(**base)


# ---------------------------------------------------------------------------
# Off names nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", "unset", ""])
def test_an_inactive_capability_names_no_column_and_counts_nothing(state) -> None:
    """« Eteinte, la capacite n'apparait nulle part » -- checkable, not asserted in prose."""
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state=state, breakdown={"metric": "m"}
    )
    payload = json.dumps(block)
    assert block["active"] is False
    for column in (*ADDED_COLUMNS, *VENTILATION_COLUMNS):
        assert column not in payload
    assert "alignment_counts" not in payload
    assert "pair" not in block
    # The gesture, not only the state: a person is told what turns it on.
    assert "Project Settings" in block["gesture"]


# ---------------------------------------------------------------------------
# The bounded block.
# ---------------------------------------------------------------------------


def test_a_capped_list_states_the_true_count_and_what_it_withheld() -> None:
    head, withheld = reader.bounded(list(range(30)))
    assert len(head) == reader.MAX_LISTED_ROWS
    assert withheld == 30 - reader.MAX_LISTED_ROWS


def test_the_production_shaped_block_fits_the_model_channel_budget(stubbed) -> None:
    """The bound is MEASURED, not asserted in prose.

    Forty pairs and sixty decisions is more than any Project this product has, and
    the block it produces must still be something a model READS rather than a
    descriptor the shared guard moved wholesale into the app channel. The number is
    `model_channel.MODEL_CHANNEL_MAX_BYTES`, which is the guard's, not a second one.
    """
    from core.model_channel import MODEL_CHANNEL_MAX_BYTES  # noqa: PLC0415

    stubbed(
        pairs=[
            {
                "left_datastream_id": f"ds_EXAMPLE_l{index}",
                "right_datastream_id": f"ds_EXAMPLE_r{index}",
                "common_key_version_id": f"mckv_EXAMPLE{index}",
                "relationship_name": "media_to_analytics",
                "view_version_id": "svv_EXAMPLE",
            }
            for index in range(40)
        ],
        decisions=[
            AlignmentDecision(
                left_row_key=f"L{index}",
                decision="arbitrated",
                right_row_key=f"R{index}",
                reason="a reason of a plausible length",
                decided_by=ACTOR,
                decided_at="2026-08-27T10:00:00+00:00",
            )
            for index in range(60)
        ],
    )
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    measured = len(json.dumps(block, separators=(",", ":")).encode("utf-8"))
    assert measured < MODEL_CHANNEL_MAX_BYTES, measured
    # And the caps are STATED, so the reader is never shown a cap as a total.
    assert block["alignable_pairs"]["count"] == 40
    assert block["alignable_pairs"]["withheld"] == 40 - reader.MAX_LISTED_ROWS
    assert block["decisions"]["count"] == 60


def test_a_block_that_ran_the_cascade_LOSES_its_structure_to_the_shared_guard(
    stubbed,
) -> None:
    """The honest half of the budget, measured on the envelope the TOOL RETURNS.

    An earlier version of this test partitioned the alignment block ALONE -- a
    shape `read_project_capability` never returns. On the real envelope
    (`{**projection, "foundation": block, "console": ...}`) `partition_envelope`
    moves the WHOLE `foundation` key and leaves `{"withheld": ..., "bytes": N}`:
    NO count survives in `structuredContent`. That is the true behaviour, so it is
    what is pinned, and the headline is what actually carries the answer.

    AC1 requires every `unmatched` row listed, every `ambiguous` row with its
    candidates and every `matched` row with its method; an aligned row is ~250
    bytes, so no cap fits three such lists into 4 KB. Nothing is discarded -- the
    block is in the app channel, whole -- but a model reading only the structured
    channel sees a descriptor, and that is why the headline carries the counts and
    the attribution warning.
    """
    from core.model_channel import (  # noqa: PLC0415
        MODEL_CHANNEL_MAX_BYTES,
        partition_envelope,
    )

    stubbed(
        pairs=[
            {
                "left_datastream_id": f"ds_EXAMPLE_l{index}",
                "right_datastream_id": f"ds_EXAMPLE_r{index}",
                "common_key_version_id": f"mckv_EXAMPLE{index}",
                "relationship_name": "media_to_analytics",
                "view_version_id": "svv_EXAMPLE",
            }
            for index in range(40)
        ],
        decisions=[
            AlignmentDecision(
                left_row_key=f"L{index}",
                decision="arbitrated",
                right_row_key=f"R{index}",
                reason="a reason of a plausible length",
                decided_by=ACTOR,
                decided_at="2026-08-27T10:00:00+00:00",
            )
            for index in range(60)
        ],
        registry=(
            reader.ObservedReading(
                left=tuple(
                    AlignmentSide(
                        row_key=f"oe1|example_ads|l1|c{index}||",
                        entity_id=f"c{index}",
                        entity_name=f"campaign number {index}",
                    )
                    for index in range(40)
                ),
                right=tuple(
                    AlignmentSide(
                        row_key=f"oe1|example_analytics|l1|c{index}||",
                        entity_id=f"c{index}",
                        entity_name=f"campaign number {index}",
                    )
                    for index in range(25)
                ),
            ),
            {"rows_from": reader.ROWS_FROM_REGISTRY},
        ),
    )
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    # The envelope `_read_project_capability` actually builds.
    envelope = {
        "capability_key": "analytics_alignment",
        "coverage": {"label": "ready", "applicable": 2, "not_applicable": 0},
        "foundation": block,
        "console": {"kind": "console", "project_id": PROJECT},
    }
    model_visible, app_payload = partition_envelope(
        envelope, tool_name="read_project_capability"
    )
    measured = len(json.dumps(model_visible, separators=(",", ":")).encode("utf-8"))
    assert measured <= MODEL_CHANNEL_MAX_BYTES, measured
    # THE WHOLE `foundation` KEY LEFT, and it left with a stated descriptor.
    assert "foundation" in app_payload
    assert "withheld" in model_visible["foundation"]
    assert "alignment_counts" not in json.dumps(model_visible)
    # Nothing was discarded: what left the model channel is intact in the app one.
    assert app_payload["foundation"] == block
    # And the answer a person needs is in the TEXT channel, which is not
    # partitioned: the counts, and the attribution warning when there is one.
    assert "40 matched, 0 ambiguous, 0 unmatched, 0 accepted" in block["headline"]
    # The headline says ONE thing about the reading, never two contradictory ones.
    assert "No aligned reading is available" not in block["headline"]


def test_no_alignable_pair_says_why_and_names_the_gesture(stubbed) -> None:
    stubbed(pairs=())
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    assert block["gap_code"] == "no_alignable_pair"
    assert "common key" in block["gesture"]
    assert "alignment_counts" not in json.dumps(block)


def test_the_default_pair_is_the_first_and_it_says_so(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    assert block["pair"]["left_datastream_id"] == LEFT
    assert block["pair_is_the_default"] is True


def test_a_named_pair_is_not_the_default(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        left_datastream_id=LEFT,
        right_datastream_id=RIGHT,
    )
    assert block["pair_is_the_default"] is False


def test_a_pair_no_relationship_crosses_is_refused_with_the_act_that_opens_it(
    stubbed,
) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        left_datastream_id="ds_EXAMPLE_other",
        right_datastream_id=RIGHT,
    )
    assert block["gap_code"] == "pair_not_alignable"
    assert "Semantic View" in block["gesture"]


def test_every_unmet_dependency_is_named_never_the_first(stubbed) -> None:
    stubbed(
        missing=(
            MissingDependency(code="common_key", reason="no key", gesture="declare it"),
            MissingDependency(code="currency_fx", reason="no fx", gesture="land a batch"),
        )
    )
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    codes = [item["code"] for item in block["dependencies"]["missing"]]
    assert codes == ["common_key", "currency_fx"]
    assert all(item["gesture"] for item in block["dependencies"]["missing"])


def test_a_decision_carries_its_author_and_its_date(stubbed) -> None:
    stubbed(
        decisions=(
            AlignmentDecision(
                left_row_key="L2",
                decision="arbitrated",
                right_row_key="R2",
                decided_by=ACTOR,
                decided_at="2026-08-27T10:00:00+00:00",
            ),
        )
    )
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    decided = block["decisions"]["items"][0]
    assert decided["decided_by"] == ACTOR
    assert decided["decided_at"] == "2026-08-27T10:00:00+00:00"


# ---------------------------------------------------------------------------
# No reading -- an absence with its cause, and never four zeros.
# ---------------------------------------------------------------------------


def test_a_datastream_with_no_attachment_is_NAMED_and_no_state_is_counted(stubbed) -> None:
    """The true reason, and the Datastream that carries it.

    An earlier version of this module claimed the rows could not be derived at all.
    They can: story 70.2's observed-entity registry is the source. What is really
    absent on a fresh Project is the ATTACHMENT, and that is a gesture somebody can
    take -- so the refusal names the Datastream and the act, not an impossibility.
    """
    stubbed()
    reading = block_reading = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready"
    )["reading"]
    assert reading["state"] == "unavailable"
    assert reading["code"] == reader.NO_ATTACHMENTS
    assert reading["datastreams_without_attachment"] == [LEFT, RIGHT]
    assert "attach" in reading["gesture"].lower()
    # THE POINT: no four-state count over a population nobody attached.
    assert "alignment_counts" not in json.dumps(block_reading)


def test_an_unreadable_registry_is_not_an_empty_one(stubbed) -> None:
    """"I could not look" is not "nobody attached anything"."""
    stubbed(
        registry=(
            None,
            {
                "state": "unavailable",
                "code": reader.REGISTRY_UNREADABLE,
                "reason": "the store did not answer",
                "gesture": "read again",
            },
        )
    )
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    assert block["reading"]["code"] == reader.REGISTRY_UNREADABLE
    assert block["reading"]["code"] != reader.NO_ATTACHMENTS


def test_a_registry_built_reading_runs_the_cascade_and_says_where_it_came_from(
    stubbed,
) -> None:
    stubbed(
        registry=(
            _reading(basis=None, groups=(), observed_by_row={}),
            {
                "rows_from": reader.ROWS_FROM_REGISTRY,
                "attachment_counts": {LEFT: 3, RIGHT: 3},
                "unreadable_attachments": {LEFT: 0, RIGHT: 0},
                "entity_key": reader.ENTITY_KEY_NOT_DERIVABLE,
                "entity_key_reason": reader.ENTITY_KEY_NOT_DERIVABLE_REASON,
                "columns_designating_an_object_kind": {LEFT: 1, RIGHT: 1},
            },
        )
    )
    block = reader.read_alignment(_NoConn(), project_id=PROJECT, capability_state="ready")
    reading = block["reading"]
    assert reading["state"] == "available"
    assert reading["rows_from"] == reader.ROWS_FROM_REGISTRY
    assert reading["alignment_counts"]["matched"] == 1
    assert reading["alignment_counts"]["unmatched"] == 1
    # The declared-key stage had NOTHING TO COMPARE, and that is not "no match".
    assert reading["entity_key"] == reader.ENTITY_KEY_NOT_DERIVABLE
    assert "carries no value of a common-key component" in reading["entity_key_reason"]
    # The wire the attachment rode in on, counted rather than asserted in prose.
    assert reading["columns_designating_an_object_kind"] == {LEFT: 1, RIGHT: 1}


def test_a_caller_supplied_reading_is_used_and_labelled_as_such(
    stubbed, monkeypatch
) -> None:
    """A surface that HAS fuller rows keeps them: the registry branch is skipped."""
    stubbed()

    def _boom(conn, *, project_id, pair):  # pragma: no cover -- reaching it is the failure
        raise AssertionError("the registry was read although the caller supplied rows")

    monkeypatch.setattr(reader, "registry_reading", _boom)
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready", reading=_reading()
    )
    assert block["reading"]["rows_from"] == reader.ROWS_FROM_CALLER
    assert block["reading"]["state"] == "available"
    # The caller's reading carried metrics and a basis, so it DID ventilate.
    assert block["reading"]["ventilation"]["ran"] is True


# ---------------------------------------------------------------------------
# The cascade, over a reading a caller supplied.
# ---------------------------------------------------------------------------


def test_the_cascade_counts_all_four_states_and_lists_the_unmatched(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready", reading=_reading()
    )
    reading = block["reading"]
    assert reading["state"] == "available"
    assert reading["alignment_counts"] == {
        "matched": 1,
        "ambiguous": 1,
        "unmatched": 1,
        "accepted": 0,
    }
    # `unmatched` is the WORK: returned, never counted away.
    assert [row["row_key"] for row in reading["unmatched"]["items"]] == ["L3"]
    assert reading["unmatched"]["count"] == 1


def test_an_ambiguous_row_carries_its_candidates_beside_it(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready", reading=_reading()
    )
    ambiguous = block["reading"]["ambiguous"]["items"][0]
    assert ambiguous["row_key"] == "L2"
    assert sorted(ambiguous["candidates"]) == ["R2", "R3"]


def test_a_matched_row_names_the_method_that_resolved_it(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready", reading=_reading()
    )
    matched = block["reading"]["matched"]["items"][0]
    assert matched["row_key"] == "L1"
    assert matched["method"] == "id_exact"
    assert matched["method_label"] == "Same entity id"


def test_an_unconverted_currency_is_the_ANSWER_and_it_names_the_side(stubbed) -> None:
    """Refused, not warned -- and returned rather than raised out of the block.

    A bounded block that swallowed this would draw a pair with no rows and no
    reason, which reads as an alignment that found nothing.
    """
    stubbed()
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        reading=_reading(currencies=AlignmentCurrencies(left="USD", right="EUR", reporting="EUR")),
    )
    assert block["reading"]["state"] == "refused"
    assert block["reading"]["refusal"]["code"] == "unconverted_currency"
    assert "USD" in block["reading"]["refusal"]["reason"]


# ---------------------------------------------------------------------------
# Observed and ventilated: two blocks, never summed.
# ---------------------------------------------------------------------------


def test_the_observed_and_ventilated_blocks_are_disjoint(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready", reading=_reading()
    )
    ventilation = block["reading"]["ventilation"]
    assert ventilation["ran"] is True
    assert ventilation["basis"] == {"volume_name": "clicks", "declared_version": "clicks_v1"}
    observed_keys = {row["row_key"] for row in ventilation["observed"]["items"]}
    ventilated_keys = {row["row_key"] for row in ventilation["ventilated"]["items"]}
    assert observed_keys & ventilated_keys == set()
    # The ambiguous key was split across its two candidates; the matched row was not.
    assert ventilated_keys == {"L2"}
    assert "L1" in observed_keys


def test_every_share_names_its_volume_and_the_version_that_declared_it(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready", reading=_reading()
    )
    shares = [row["share"] for row in block["reading"]["ventilation"]["ventilated"]["items"]]
    assert shares
    for share in shares:
        assert share["volume_name"] == "clicks"
        assert share["volume_version"] == "clicks_v1"
        assert share["volume_total"] == "100"
    # A weight is a Decimal on the wire, never a float.
    assert all(isinstance(share["weight"], str) for share in shares)


def test_a_reading_that_declared_no_volume_does_not_split_and_says_so(stubbed) -> None:
    """No default and no equal parts: a run that did not happen reads as one."""
    stubbed()
    block = reader.read_alignment(
        _NoConn(), project_id=PROJECT, capability_state="ready", reading=_reading(basis=None)
    )
    ventilation = block["reading"]["ventilation"]
    assert ventilation["ran"] is False
    assert "declared" in ventilation["reason"]
    assert ventilation["columns"] == []


def test_a_refused_key_is_counted_with_its_code_never_hidden(stubbed) -> None:
    stubbed()
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        reading=_reading(
            groups=(
                VentilationGroup.of(
                    "L2",
                    "2026-08-01",
                    [VentilationCandidate("R2", None), VentilationCandidate("R3", 70)],
                ),
            )
        ),
    )
    ventilation = block["reading"]["ventilation"]
    assert ventilation["counts"]["volume_not_declared"] == 1
    refusal = ventilation["refusals"]["items"][0]
    assert refusal["code"] == "volume_not_declared"
    assert refusal["gesture"]


# ---------------------------------------------------------------------------
# The observed-entity registry, read as the source of the two sides.
# ---------------------------------------------------------------------------


def _attachment(datastream_id, *, platform="example_ads", level="l1", l1="c1", node="mdn_1",
                provenance_reference=None, evidence=None):
    """One row shaped exactly as `observed_entities.ATTACHMENT_COLUMNS` yields it."""
    from core.observed_entities import ObservedEntityPath  # noqa: PLC0415

    path = ObservedEntityPath(platform=platform, level=level, l1_id=l1)
    return {
        "id": f"mdali_{l1}",
        "org_id": "org_EXAMPLE",
        "project_id": PROJECT,
        "node_id": node,
        "namespace": f"observed_entity:{PROJECT}",
        "raw_value": path.canonical_key,
        "normalized_value": path.canonical_key,
        "relation": "exact",
        "provenance": "datastream_observation",
        "provenance_reference": (
            datastream_id if provenance_reference is None else provenance_reference
        ),
        "evidence": (
            {"datastream_id": datastream_id, **path.as_dict()} if evidence is None else evidence
        ),
        "conflict_state": None,
        "created_by": ACTOR,
        "created_at": None,
        "retired_at": None,
    }


@pytest.fixture
def registry(monkeypatch):
    """The registry's three public reads, replaced by values. No database."""
    from core import master_data, observed_entities  # noqa: PLC0415

    def _install(attachments=(), labels=None, registry_row={"id": "mdreg_1"}):
        monkeypatch.setattr(
            observed_entities, "list_attachments", lambda conn, **k: list(attachments)
        )
        monkeypatch.setattr(
            observed_entities,
            "fetch_observed_entity_registry",
            lambda conn, *, project_id: registry_row,
        )
        monkeypatch.setattr(
            master_data,
            "list_nodes",
            lambda conn, *, project_id, registry_id, include_archived=False: [
                {"id": node_id, "label": label} for node_id, label in (labels or {}).items()
            ],
        )
        monkeypatch.setattr(
            reader, "_designating_column_counts", lambda conn, **k: {LEFT: 1, RIGHT: 1}
        )

    return _install


def test_an_attachment_is_routed_by_the_datastream_COLUMN(registry) -> None:
    """`provenance_reference` is where `_insert_attachment` writes it, verbatim."""
    registry(
        attachments=[_attachment(LEFT, l1="c1"), _attachment(RIGHT, l1="c2")],
        labels={"mdn_1": "Alpha Campaign"},
    )
    indexed = reader._attachments_by_datastream(
        _NoConn(), project_id=PROJECT, datastream_ids=(LEFT, RIGHT)
    )
    assert [row["raw_value"] for row in indexed[LEFT]] == [indexed[LEFT][0]["raw_value"]]
    assert len(indexed[LEFT]) == 1 and len(indexed[RIGHT]) == 1


def test_the_evidence_is_the_fallback_when_the_column_is_empty(registry) -> None:
    """A JSON key is easier to lose than a column, so the column is read first."""
    registry(attachments=[_attachment(LEFT, provenance_reference="")])
    indexed = reader._attachments_by_datastream(
        _NoConn(), project_id=PROJECT, datastream_ids=(LEFT, RIGHT)
    )
    assert len(indexed[LEFT]) == 1
    assert indexed[RIGHT] == []


def test_an_attachment_of_another_datastream_is_not_borrowed(registry) -> None:
    registry(attachments=[_attachment("ds_EXAMPLE_third")])
    indexed = reader._attachments_by_datastream(
        _NoConn(), project_id=PROJECT, datastream_ids=(LEFT, RIGHT)
    )
    assert indexed == {LEFT: [], RIGHT: []}


def test_a_side_carries_the_paths_own_level_id_and_the_GOVERNED_label(registry) -> None:
    """The label is Master Data's, never re-derived beside the one it stores."""
    rows = [_attachment(LEFT, l1="c1", node="mdn_1")]
    sides, unreadable, platforms = reader._sides_from_attachments(
        rows, {"mdn_1": "Alpha Campaign"}
    )
    assert platforms == frozenset({"example_ads"})
    assert unreadable == 0
    (side,) = sides
    assert side.entity_id == "c1"
    assert side.entity_name == "Alpha Campaign"
    # The identity is the PATH, which is why it is the row key and the id is not.
    assert side.row_key.startswith("oe1|")
    # An attachment carries no common-key component value.
    assert side.entity_key is None


def test_an_undecodable_attachment_is_COUNTED_never_dropped_in_silence(registry) -> None:
    broken = _attachment(LEFT, evidence={"datastream_id": LEFT, "platform": "", "level": ""})
    sides, unreadable, platforms = reader._sides_from_attachments([broken], {})
    assert platforms == frozenset()
    assert sides == ()
    assert unreadable == 1


def test_both_sides_attached_gives_a_reading_and_names_its_source(registry) -> None:
    registry(
        attachments=[
            _attachment(LEFT, l1="c1", node="mdn_1"),
            _attachment(RIGHT, l1="c1", node="mdn_1"),
        ],
        labels={"mdn_1": "Alpha Campaign"},
    )
    reading, provenance = reader.registry_reading(_NoConn(), project_id=PROJECT, pair=_PAIR)
    assert reading is not None
    assert provenance["rows_from"] == reader.ROWS_FROM_REGISTRY
    assert provenance["attachment_counts"] == {LEFT: 1, RIGHT: 1}
    assert provenance["entity_key"] == reader.ENTITY_KEY_NOT_DERIVABLE
    assert provenance["columns_designating_an_object_kind"] == {LEFT: 1, RIGHT: 1}


def test_one_empty_side_names_THAT_datastream_and_the_gesture(registry) -> None:
    registry(attachments=[_attachment(LEFT)], labels={"mdn_1": "Alpha"})
    reading, provenance = reader.registry_reading(_NoConn(), project_id=PROJECT, pair=_PAIR)
    assert reading is None
    assert provenance["code"] == reader.NO_ATTACHMENTS
    # THE side, named. Aligning against an empty one would report every row of the
    # other as unmatched and call that the work.
    assert provenance["datastreams_without_attachment"] == [RIGHT]
    assert "attach_observed_entity" in provenance["gesture"]


def test_both_empty_sides_are_named_together_never_one_at_a_time(registry) -> None:
    registry(attachments=[])
    _reading_out, provenance = reader.registry_reading(_NoConn(), project_id=PROJECT, pair=_PAIR)
    assert provenance["datastreams_without_attachment"] == [LEFT, RIGHT]


def test_a_registry_read_that_FAILED_is_not_an_empty_registry(monkeypatch) -> None:
    from core import observed_entities  # noqa: PLC0415

    def _boom(conn, **kwargs):
        raise RuntimeError("the alias store is unreachable")

    monkeypatch.setattr(observed_entities, "list_attachments", _boom)
    reading, provenance = reader.registry_reading(_NoConn(), project_id=PROJECT, pair=_PAIR)
    assert reading is None
    assert provenance["code"] == reader.REGISTRY_UNREADABLE
    assert provenance["code"] != reader.NO_ATTACHMENTS


def test_the_registry_read_writes_nothing(registry, monkeypatch) -> None:
    """A read never writes -- and the only way to prove it is to forbid the verbs.

    `monkeypatch` and never a bare `setattr`: a doublure left standing on a shared
    module poisons every later test in the session. This file did exactly that for
    one run, and the pg suite two files away failed on a `None` nobody had written.
    """
    written: list[str] = []
    from core import observed_entities  # noqa: PLC0415

    registry(
        attachments=[_attachment(LEFT, node="mdn_1"), _attachment(RIGHT, node="mdn_1")],
        labels={"mdn_1": "Alpha"},
    )
    for verb in ("attach_observed_entity", "reattach_observed_entity", "release_observed_entity"):
        monkeypatch.setattr(
            observed_entities,
            verb,
            lambda *a, _verb=verb, **k: written.append(_verb),  # pragma: no cover
        )
    reader.registry_reading(_NoConn(), project_id=PROJECT, pair=_PAIR)
    assert written == []


# ---------------------------------------------------------------------------
# The grain sanctions the breakdown -- epic 71's first caller.
# ---------------------------------------------------------------------------


def _grain(grain_id, version_id, head, members, *, status="active", name="Spend grain"):
    return {
        "id": grain_id,
        "name": name,
        "status": status,
        "current_version": {
            "id": version_id,
            "version_number": 1,
            "content_hash": "0" * 64,
            "head": {"canonical_field_id": head, "canonical_name": "spend"},
            "members": [
                {"canonical_field_id": item, "canonical_name": item, "ordinal": index}
                for index, item in enumerate(members)
            ],
        },
    }


@pytest.fixture
def grains(monkeypatch):
    def _install(catalogue, coverage=None):
        monkeypatch.setattr(
            metric_dimensions,
            "list_measurement_grains",
            lambda conn, *, project_id: list(catalogue),
        )
        monkeypatch.setattr(
            metric_dimensions,
            "derived_coverage",
            lambda conn, *, project_id, head, members: coverage
            or {
                "state": "available",
                "counts": {"full": 1, "partial": 1, "absent": 0, "unknown": 0},
                "datastreams": [
                    {
                        "datastream_id": LEFT,
                        "coverage": "full",
                        "missing_dimensions": [],
                    },
                    {
                        "datastream_id": RIGHT,
                        "coverage": "partial",
                        "missing_dimensions": [
                            {"canonical_field_id": "cf_publisher", "canonical_name": "publisher"}
                        ],
                    },
                ],
                "unknown_datastreams": [],
            },
        )

    return _install


def test_a_metric_that_heads_no_live_grain_is_refused_with_the_act_that_fixes_it(
    grains,
) -> None:
    grains([])
    answer = reader.sanction_breakdown(
        _NoConn(), project_id=PROJECT, metric_field_id="cf_spend", dimension_field_ids=["cf_day"]
    )
    assert answer["sanctioned"] is False
    assert answer["refusal"]["code"] == reader.REFUSAL_NO_GOVERNED_GRAIN
    assert "Governance > Master Data" in answer["refusal"]["gesture"]


def test_an_archived_grain_does_not_sanction_anything(grains) -> None:
    grains([_grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day"], status="archived")])
    answer = reader.sanction_breakdown(
        _NoConn(), project_id=PROJECT, metric_field_id="cf_spend", dimension_field_ids=["cf_day"]
    )
    assert answer["refusal"]["code"] == reader.REFUSAL_NO_GOVERNED_GRAIN


def test_a_dimension_no_grain_relates_to_the_metric_is_refused_by_name(grains) -> None:
    """The clause of `governance.md` § Amendment 2026-08-27 this module answers."""
    grains([_grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day", "cf_campaign"])])
    answer = reader.sanction_breakdown(
        _NoConn(),
        project_id=PROJECT,
        metric_field_id="cf_spend",
        dimension_field_ids=["cf_day", "cf_creative"],
    )
    assert answer["sanctioned"] is False
    refusal = answer["refusal"]
    assert refusal["code"] == reader.REFUSAL_DIMENSION_NOT_IN_GRAIN
    # The metric, the dimension AND the grain versions that DO exist.
    assert refusal["metric"] == "cf_spend"
    assert refusal["dimensions_not_in_any_grain"] == ["cf_creative"]
    assert "mgrv_1" in refusal["reason"]
    assert "sliced_by" not in answer


def test_a_breakdown_spread_over_two_grains_is_refused_rather_than_unioned(grains) -> None:
    """Two grains, each governing half the cut. Their union is a grain nobody declared."""
    grains(
        [
            _grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day"], name="Spend by day"),
            _grain("mgr_2", "mgrv_2", "cf_spend", ["cf_campaign"], name="Spend by campaign"),
        ]
    )
    answer = reader.sanction_breakdown(
        _NoConn(),
        project_id=PROJECT,
        metric_field_id="cf_spend",
        dimension_field_ids=["cf_day", "cf_campaign"],
    )
    assert answer["refusal"]["code"] == reader.REFUSAL_BREAKDOWN_SPANS_GRAINS
    assert "sliced_by" not in answer


def test_a_sanctioned_breakdown_pins_the_grain_AND_its_version(grains) -> None:
    grains([_grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day", "cf_campaign"])])
    answer = reader.sanction_breakdown(
        _NoConn(),
        project_id=PROJECT,
        metric_field_id="cf_spend",
        dimension_field_ids=["cf_campaign"],
    )
    assert answer["sanctioned"] is True
    # A consumer never pins a grain without a version.
    assert answer["sliced_by"] == {"measurement_grain_id": "mgr_1", "version_id": "mgrv_1"}


def test_a_grain_of_zero_dimensions_sanctions_a_total(grains) -> None:
    grains([_grain("mgr_total", "mgrv_total", "cf_spend", [])])
    answer = reader.sanction_breakdown(
        _NoConn(), project_id=PROJECT, metric_field_id="cf_spend", dimension_field_ids=[]
    )
    assert answer["sanctioned"] is True
    assert answer["sliced_by"]["version_id"] == "mgrv_total"


def test_a_partial_coverage_is_shown_as_the_partial_it_is(grains) -> None:
    grains([_grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day"])])
    answer = reader.sanction_breakdown(
        _NoConn(), project_id=PROJECT, metric_field_id="cf_spend", dimension_field_ids=["cf_day"]
    )
    coverage = answer["coverage"]
    assert coverage["counts"]["partial"] == 1
    named = coverage["partial_datastreams"]["items"]
    assert [row["datastream_id"] for row in named] == [RIGHT]
    assert named[0]["missing_dimensions"][0]["canonical_name"] == "publisher"


def test_a_coverage_that_could_not_be_read_is_not_a_zero(grains) -> None:
    grains(
        [_grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day"])],
        coverage={"state": "unavailable", "counts": None, "datastreams": []},
    )
    answer = reader.sanction_breakdown(
        _NoConn(), project_id=PROJECT, metric_field_id="cf_spend", dimension_field_ids=["cf_day"]
    )
    assert answer["coverage"]["state"] == "unavailable"
    assert "counts" not in answer["coverage"]


# ---------------------------------------------------------------------------
# The breakdown's figures come from the aligned reading, or say `unavailable`.
# ---------------------------------------------------------------------------


def test_a_sanctioned_breakdown_with_no_reading_says_unavailable_never_zero(
    stubbed, grains
) -> None:
    stubbed()
    grains([_grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day"])])
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        breakdown={"metric": "cf_spend", "dimensions": ["cf_day"]},
    )
    values = block["breakdown"]["values"]
    assert values["state"] == "unavailable"
    assert values["code"] == reader.VALUES_UNAVAILABLE
    assert "0" != values.get("total")
    assert block["breakdown"]["sliced_by"]["version_id"] == "mgrv_1"


def test_a_sanctioned_breakdown_takes_its_figures_from_the_two_blocks(
    stubbed, grains
) -> None:
    """No new aggregation engine: the observed and ventilated blocks, pointed at."""
    stubbed()
    grains([_grain("mgr_1", "mgrv_1", "spend", ["cf_day"])])
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        reading=_reading(),
        breakdown={"metric": "spend", "dimensions": ["cf_day"]},
    )
    values = block["breakdown"]["values"]
    assert values["state"] == "available"
    assert values["metric"] == "spend"
    assert values["observed"]["count"] >= 1
    assert values["ventilated"]["count"] == 2
    assert "never summed" in values["note"]


def test_a_metric_the_reading_does_not_carry_is_unavailable_with_the_reason(
    stubbed, grains
) -> None:
    stubbed()
    grains([_grain("mgr_1", "mgrv_1", "conversions", ["cf_day"])])
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        reading=_reading(),
        breakdown={"metric": "conversions", "dimensions": ["cf_day"]},
    )
    assert block["breakdown"]["values"]["state"] == "unavailable"
    assert "conversions" in block["breakdown"]["values"]["reason"]


def test_a_refused_breakdown_carries_no_figures_at_all(stubbed, grains) -> None:
    stubbed()
    grains([_grain("mgr_1", "mgrv_1", "spend", ["cf_day"])])
    block = reader.read_alignment(
        _NoConn(),
        project_id=PROJECT,
        capability_state="ready",
        reading=_reading(),
        breakdown={"metric": "spend", "dimensions": ["cf_creative"]},
    )
    assert block["breakdown"]["sanctioned"] is False
    assert "values" not in block["breakdown"]
    assert "Breakdown refused" in block["headline"]


# ---------------------------------------------------------------------------
# Review round 1 -- the refusals a decision must pass, proved without a database.
# ---------------------------------------------------------------------------


def test_an_empty_breakdown_over_several_grains_no_longer_pins_the_first(grains) -> None:
    """Finding 5, at its root: `all([])` is true of every grain."""
    grains(
        [
            _grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day"], name="a first by name"),
            _grain("mgr_2", "mgrv_2", "cf_spend", ["cf_campaign"], name="b second by name"),
        ]
    )
    answer = reader.sanction_breakdown(
        _NoConn(), project_id=PROJECT, metric_field_id="cf_spend", dimension_field_ids=[]
    )
    assert answer["sanctioned"] is False
    assert answer["refusal"]["code"] == reader.REFUSAL_TOTAL_NOT_DECLARED
    assert "sliced_by" not in answer


def test_a_declared_total_is_chosen_over_a_grain_that_sorts_first(grains) -> None:
    grains(
        [
            _grain("mgr_1", "mgrv_1", "cf_spend", ["cf_day"], name="a first by name"),
            _grain("mgr_total", "mgrv_total", "cf_spend", [], name="z total"),
        ]
    )
    answer = reader.sanction_breakdown(
        _NoConn(), project_id=PROJECT, metric_field_id="cf_spend", dimension_field_ids=[]
    )
    assert answer["sliced_by"]["measurement_grain_id"] == "mgr_total"


def test_a_same_platform_pair_is_detected_from_the_decoded_paths(registry) -> None:
    """Finding 2: the platform is the first component of the path, and it is read."""
    registry(
        attachments=[
            _attachment(LEFT, platform="example_ads", l1="c1", node="mdn_1"),
            _attachment(RIGHT, platform="example_ads", l1="c2", node="mdn_2"),
        ],
        labels={"mdn_1": "Left", "mdn_2": "Right"},
    )
    _reading_out, provenance = reader.registry_reading(
        _NoConn(), project_id=PROJECT, pair=_PAIR
    )
    risk = provenance["attribution_risk"]
    assert risk["code"] == reader.SAME_PLATFORM_PAIR
    assert risk["platforms"] == ["example_ads"]
    assert risk["shared_platform_count"] == 1


def test_two_platforms_carry_no_attribution_risk(registry) -> None:
    registry(
        attachments=[
            _attachment(LEFT, platform="example_ads", l1="c1", node="mdn_1"),
            _attachment(RIGHT, platform="example_analytics", l1="c1", node="mdn_2"),
        ],
        labels={"mdn_1": "Left", "mdn_2": "Right"},
    )
    _reading_out, provenance = reader.registry_reading(
        _NoConn(), project_id=PROJECT, pair=_PAIR
    )
    assert "attribution_risk" not in provenance
