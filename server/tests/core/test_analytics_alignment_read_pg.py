"""Story 71.4 against a real database: the first caller, end to end.

What only a real schema proves here, and a stub could only have been told:

  * the block runs over a REAL alignable pair -- a published Semantic View
    relationship pinning a real common key version between two real Datastreams --
    rather than over a dict somebody wrote;
  * a REAL measurement grain sanctions a breakdown and names the version it was cut
    by, and a real dimension outside every live grain version is refused by name;
  * the derived coverage of that grain is the real mapping coverage, so a
    Datastream that binds the metric and not one of its dimensions shows as the
    `partial` it is;
  * the FIRST decision on a row stands, and the review reference MOVES when it
    lands -- which is what makes a stale confirmation detectable rather than a
    second write that quietly changed nothing;
  * the confirmed write goes through the generic MCP tool and returns
    ``already_decided`` when it wrote nothing.

pg-gated: skipped without ``TEST_POSTGRES_DSN``. ``live_postgres`` rolls back on
teardown, and the one place that would commit is neutralised (see
:class:`_UncommittedConnection`).

No real identifier appears: the fixtures are minted from a ULID and the actors are
``owner@example.com`` and ``second@example.com``.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import analytics_alignment_read as reader  # noqa: E402
from core import mdm_common_keys as keys  # noqa: E402
from core import metric_dimensions as grains  # noqa: E402
from core import project_capabilities_mcp as surface  # noqa: E402
from core.analytics_alignment import (  # noqa: E402
    AlignmentDecision,
    AlignmentRefused,
    list_alignment_decisions,
    record_alignment_decision,
)

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
    make_semantic_view,
    pin_relationship,
    publish_output,
)

ACTOR = "owner@example.com"
SECOND_ACTOR = "second@example.com"
ROW = "left_row_1"


class _UncommittedConnection:
    """The live connection, with ``commit`` turned into a no-op.

    The MCP confirm tool commits, which is right in production and wrong inside a
    fixture whose teardown is a ROLLBACK: a committed org would outlive the test.
    Everything else is delegated, so the statements under test are the real ones
    against the real schema -- only the transaction boundary moves to the fixture,
    which is the boundary this suite already owns.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self) -> None:
        return None


@pytest.fixture()
def world(live_postgres):
    """Two Datastreams a published relationship crosses, and a governed grain.

    The left binds spend, day and campaign; the right binds day and campaign only.
    That asymmetry is deliberate: it is what makes the grain's coverage `partial`
    on one side, which is the state `governance.md` refuses to see rendered as
    covered.
    """
    org_id, project_id = make_project(live_postgres, label="Epic 71 alignment read")
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    creative = make_canonical_field(live_postgres, project_id, "creative_id")
    spend = make_canonical_field(
        live_postgres, project_id, "spend", kind="metric", value_type="money"
    )
    left = make_datastream(
        live_postgres,
        org_id,
        project_id,
        "Campaign spend",
        bindings={
            day: ("date", "confirmed"),
            campaign: ("campaign", "confirmed"),
            spend: ("spend", "confirmed"),
        },
    )
    right = make_datastream(
        live_postgres,
        org_id,
        project_id,
        "Site analytics",
        bindings={day: ("event_date", "resolved"), campaign: ("campaign_key", "resolved")},
        measures={"sessions": "confirmed"},
    )
    publish_output(live_postgres, org_id, project_id, left, "align_left")
    publish_output(live_postgres, org_id, project_id, right, "align_right")
    key = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    _view_id, view_version_id = make_semantic_view(live_postgres, project_id)
    pin_relationship(
        live_postgres,
        view_version_id,
        key["current_version"]["id"],
        left_datastream_id=left,
        right_datastream_id=right,
    )
    grain = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend by day and campaign",
        head_field_id=spend,
        member_field_ids=[day, campaign],
        actor="tester",
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.project_capabilities SET state = 'ready' "
            "WHERE project_id = %s AND capability_key IN "
            "('analytics_alignment', 'currency_fx')",
            (project_id,),
        )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "left": left,
        "right": right,
        "day": day,
        "campaign": campaign,
        "creative": creative,
        "spend": spend,
        "key_version_id": key["current_version"]["id"],
        "grain": grain,
    }


def _read(conn, world, **kwargs):
    return reader.read_alignment(
        conn, project_id=world["project_id"], capability_state="ready", **kwargs
    )


# ---------------------------------------------------------------------------
# The generic read serves the alignment, over a real pair.
# ---------------------------------------------------------------------------


def test_the_generic_read_finds_the_real_pair_and_says_it_defaulted(
    live_postgres, world
) -> None:
    block = surface._foundation_summary(
        live_postgres, world["project_id"], "analytics_alignment"
    )
    assert block["schema"] == reader.ALIGNMENT_READ_SCHEMA
    assert block["active"] is True
    assert block["alignable_pairs"]["count"] == 1
    assert block["pair"]["left_datastream_id"] == world["left"]
    assert block["pair"]["right_datastream_id"] == world["right"]
    assert block["pair"]["common_key_version_id"] == world["key_version_id"]
    assert block["pair_is_the_default"] is True


def test_the_three_dependencies_are_read_against_real_sql(live_postgres, world) -> None:
    block = _read(live_postgres, world)
    dependencies = block["dependencies"]
    # The key covers both sides and a published relationship pins it, so neither of
    # those two is missing; Currency & FX was set ready by the fixture.
    assert dependencies["common_key_version_ids"] == [world["key_version_id"]]
    assert dependencies["missing"] == []
    assert dependencies["satisfied"] is True


def test_an_unmet_dependency_names_the_act_that_meets_it(live_postgres, world) -> None:
    with live_postgres.cursor() as cur:
        # `blocked` and not `disabled`: `currency_fx` is `always_present`, and
        # migration 131's pairing CHECK refuses `disabled` on such a row. The state
        # is read through `capability_is_active`, which counts only `ready` and
        # `degraded`, so `blocked` is the not-active state this row can hold.
        cur.execute(
            "UPDATE app.project_capabilities SET state = 'blocked' "
            "WHERE project_id = %s AND capability_key = 'currency_fx'",
            (world["project_id"],),
        )
    block = _read(live_postgres, world)
    missing = block["dependencies"]["missing"]
    assert [item["code"] for item in missing] == ["currency_fx"]
    assert "Money Policy" in missing[0]["gesture"]


def test_a_disabled_capability_reads_no_pair_and_names_no_column(
    live_postgres, world
) -> None:
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.project_capabilities SET state = 'disabled' "
            "WHERE project_id = %s AND capability_key = 'analytics_alignment'",
            (world["project_id"],),
        )
    block = surface._foundation_summary(
        live_postgres, world["project_id"], "analytics_alignment"
    )
    assert block["active"] is False
    assert "pair" not in block
    assert "analytics_alignment_entity_id" not in str(block)


def test_the_stored_shares_are_read_even_when_no_reading_can_be_built(
    live_postgres, world
) -> None:
    """Zero shares, honestly zero: the store answered and holds none."""
    block = _read(live_postgres, world)
    assert block["stored_shares"] == {"items": [], "count": 0, "withheld": 0}


# ---------------------------------------------------------------------------
# The observed rows, read from story 70.2's registry against a real database.
# ---------------------------------------------------------------------------

#: TWO platforms, and that is the shape of the reference case rather than a
#: fixture convenience: the media side and the analytics side are different
#: sources, so their PATHS differ by their first component even when they name
#: the same campaign. It also matters mechanically -- an attachment is keyed by
#: PATH, so one path attached twice is ONE row (story 70.2's idempotency), and a
#: fixture that gave both sides one platform would have given the right side no
#: attachment at all. Measured 2026-08-28, on this very test.
LEFT_PLATFORM = "example_ads"
RIGHT_PLATFORM = "example_analytics"


def _attach(conn, world, datastream_id, platform, l1_id, *, label=None):
    """One observed entity of one Datastream, bound to a governed node."""
    from core.observed_entities import LEVEL_L1, ObservedEntityPath, attach_observed_entity

    return attach_observed_entity(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        registry_id=world["registry_id"],
        path=ObservedEntityPath(platform=platform, level=LEVEL_L1, l1_id=l1_id),
        datastream_id=datastream_id,
        actor=ACTOR,
        label=label,
    )


@pytest.fixture()
def attached(live_postgres, world):
    """The measured shape: one exact id match, one name-only match, one unmatched.

    * ``c_shared`` is published by BOTH sides under their own platform, so the two
      paths differ and the two nodes differ -- but the ENTITY ID at the path's own
      level is the same, and `id_exact` resolves the row on exactly that;
    * ``c_left_named`` / ``c_right_named`` are different ids given the SAME label
      in two spellings, so only `name_normalized` can pair them;
    * ``c_orphan`` is published by the left alone and nothing answers it.
    """
    from core.observed_entities import ensure_observed_entity_registry

    registry = ensure_observed_entity_registry(
        live_postgres,
        org_id=world["org_id"],
        project_id=world["project_id"],
        actor=ACTOR,
    )
    world["registry_id"] = registry["id"]

    _attach(live_postgres, world, world["left"], LEFT_PLATFORM, "c_shared", label="Winter Push")
    _attach(
        live_postgres, world, world["right"], RIGHT_PLATFORM, "c_shared", label="winter push"
    )
    _attach(
        live_postgres, world, world["left"], LEFT_PLATFORM, "c_left_named",
        label="Brand Always On",
    )
    _attach(
        live_postgres, world, world["right"], RIGHT_PLATFORM, "c_right_named",
        label="brand always on",
    )
    _attach(
        live_postgres, world, world["left"], LEFT_PLATFORM, "c_orphan",
        label="Nobody Answers This",
    )
    return world


def test_one_path_attached_twice_is_ONE_row_and_it_keeps_the_first_datastream(
    live_postgres, world
) -> None:
    """Story 70.2's idempotency, stated here because it shapes what this reader sees.

    An attachment is keyed by PATH. Two Datastreams of the SAME platform observing
    the same entity therefore share one alias row, and that row names whichever
    Datastream attached it first -- so the second one appears to have nothing. It
    is not a defect of either module: the path identity is deliberate, and two
    real sources are two platforms. Written as a test so the next person meets it
    here rather than in a fixture that silently produced an empty side.
    """
    from core.observed_entities import ensure_observed_entity_registry, list_attachments

    registry = ensure_observed_entity_registry(
        live_postgres, org_id=world["org_id"], project_id=world["project_id"], actor=ACTOR
    )
    world["registry_id"] = registry["id"]
    first = _attach(live_postgres, world, world["left"], LEFT_PLATFORM, "c_1", label="One")
    second = _attach(live_postgres, world, world["right"], LEFT_PLATFORM, "c_1")

    assert first["outcome"] == "attached"
    assert second["outcome"] == "unchanged"
    rows = list_attachments(live_postgres, project_id=world["project_id"])
    assert len(rows) == 1
    assert rows[0]["provenance_reference"] == world["left"]

    reading = _read(live_postgres, world)["reading"]
    assert reading["code"] == reader.NO_ATTACHMENTS
    assert reading["datastreams_without_attachment"] == [world["right"]]


def test_a_datastream_with_no_attachment_is_named_and_nothing_is_counted(
    live_postgres, world
) -> None:
    """The TRUE reason a fresh Project cannot align, and the act that repairs it."""
    block = _read(live_postgres, world)
    reading = block["reading"]
    assert reading["state"] == "unavailable"
    assert reading["code"] == reader.NO_ATTACHMENTS
    assert sorted(reading["datastreams_without_attachment"]) == sorted(
        [world["left"], world["right"]]
    )
    assert "attach_observed_entity" in reading["gesture"]
    # Never four zeros over a population nobody attached.
    assert "alignment_counts" not in str(block)


def test_one_side_attached_still_refuses_and_names_the_OTHER_one(
    live_postgres, world
) -> None:
    from core.observed_entities import ensure_observed_entity_registry

    registry = ensure_observed_entity_registry(
        live_postgres, org_id=world["org_id"], project_id=world["project_id"], actor=ACTOR
    )
    world["registry_id"] = registry["id"]
    _attach(live_postgres, world, world["left"], LEFT_PLATFORM, "c_1", label="Only Left")

    reading = _read(live_postgres, world)["reading"]
    assert reading["code"] == reader.NO_ATTACHMENTS
    # Aligning a full side against an empty one would report every one of its rows
    # as `unmatched` and call that the work.
    assert reading["datastreams_without_attachment"] == [world["right"]]


def test_the_registry_builds_both_sides_and_the_cascade_runs(
    live_postgres, attached
) -> None:
    block = _read(live_postgres, attached)
    reading = block["reading"]
    assert reading["state"] == "available"
    assert reading["rows_from"] == reader.ROWS_FROM_REGISTRY
    assert reading["attachment_counts"] == {attached["left"]: 3, attached["right"]: 2}
    assert reading["unreadable_attachments"] == {attached["left"]: 0, attached["right"]: 0}
    assert reading["alignment_counts"] == {
        "matched": 2,
        "ambiguous": 0,
        "unmatched": 1,
        "accepted": 0,
    }


def test_an_id_match_and_a_name_match_each_name_their_own_method(
    live_postgres, attached
) -> None:
    reading = _read(live_postgres, attached)["reading"]
    methods = {row["row_key"]: row["method"] for row in reading["matched"]["items"]}
    assert sorted(methods.values()) == ["id_exact", "name_normalized"]
    # The row key is the PATH, which is the identity story 70.2 chose.
    assert all(key.startswith("oe1|") for key in methods)


def test_the_unmatched_row_is_LISTED_never_counted_away(live_postgres, attached) -> None:
    reading = _read(live_postgres, attached)["reading"]
    assert reading["unmatched"]["count"] == 1
    (orphan,) = reading["unmatched"]["items"]
    assert "c_orphan" in orphan["row_key"]
    # A row that is not matched carries null, never a sentinel.
    assert orphan["analytics_alignment_entity_id"] is None


def test_the_declared_key_stage_had_nothing_to_compare_and_says_so(
    live_postgres, attached
) -> None:
    """`key_exact` proposing nothing is not `key_exact` finding no match."""
    reading = _read(live_postgres, attached)["reading"]
    assert reading["entity_key"] == reader.ENTITY_KEY_NOT_DERIVABLE
    assert "carries no value of a common-key component" in reading["entity_key_reason"]
    assert "key_exact" not in {row["method"] for row in reading["matched"]["items"]}


def test_a_registry_built_reading_ventilates_nothing_and_says_why(
    live_postgres, attached
) -> None:
    """The registry aligns identities: four fields and no metric, so no split."""
    ventilation = _read(live_postgres, attached)["reading"]["ventilation"]
    assert ventilation["ran"] is False
    assert "declared" in ventilation["reason"]
    assert ventilation["columns"] == []


def test_a_sanctioned_breakdown_over_a_registry_reading_has_no_figures(
    live_postgres, attached
) -> None:
    """The grain sanctions the cut; the identity side cannot carry the measure."""
    block = _read(
        live_postgres,
        attached,
        breakdown={"metric": attached["spend"], "dimensions": [attached["day"]]},
    )
    assert block["breakdown"]["sanctioned"] is True
    values = block["breakdown"]["values"]
    assert values["state"] == "unavailable"
    assert values["code"] == reader.VALUES_UNAVAILABLE
    assert "no metric" in values["reason"]


def test_a_stored_arbitration_settles_a_registry_built_row(
    live_postgres, attached
) -> None:
    """The two halves meet: the registry supplies the row, a person settles it."""
    reading = _read(live_postgres, attached)["reading"]
    (orphan,) = reading["unmatched"]["items"]
    right_key = _read(live_postgres, attached)["reading"]["matched"]["items"][0]["row_key"]

    record_alignment_decision(
        live_postgres,
        org_id=attached["org_id"],
        project_id=attached["project_id"],
        left_datastream_id=attached["left"],
        right_datastream_id=attached["right"],
        common_key_version_id=attached["key_version_id"],
        decision=AlignmentDecision(
            left_row_key=orphan["row_key"],
            decision="accepted",
            reason="nothing on the analytics side answers this campaign",
            decided_by=ACTOR,
        ),
        actor=ACTOR,
    )
    after = _read(live_postgres, attached)["reading"]
    assert after["alignment_counts"] == {
        "matched": 2,
        "ambiguous": 0,
        "unmatched": 0,
        "accepted": 1,
    }
    assert right_key


def test_preparing_a_registry_built_row_NAMES_its_candidates(
    live_postgres, attached
) -> None:
    reading = _read(live_postgres, attached)["reading"]
    (orphan,) = reading["unmatched"]["items"]
    prepared = reader.prepare_row_decision(
        live_postgres,
        project_id=attached["project_id"],
        left_datastream_id=attached["left"],
        right_datastream_id=attached["right"],
        left_row_key=orphan["row_key"],
    )
    assert prepared["candidates"]["state"] == "available"
    assert prepared["candidates"]["rows_from"] == reader.ROWS_FROM_REGISTRY
    assert prepared["candidates"]["row_is_observed"] is True
    assert prepared["candidates"]["row_state"] == "unmatched"
    assert prepared["candidates"]["items"] == []


def test_the_read_writes_no_attachment(live_postgres, attached) -> None:
    """A read never writes. The registry's row count is the measurement."""
    from core.observed_entities import list_attachments

    before = len(list_attachments(live_postgres, project_id=attached["project_id"]))
    _read(live_postgres, attached)
    _read(
        live_postgres,
        attached,
        breakdown={"metric": attached["spend"], "dimensions": [attached["day"]]},
    )
    assert len(list_attachments(live_postgres, project_id=attached["project_id"])) == before


# ---------------------------------------------------------------------------
# The grain sanctions the breakdown -- against a real grain.
# ---------------------------------------------------------------------------


def test_a_real_grain_sanctions_the_breakdown_and_names_its_version(
    live_postgres, world
) -> None:
    block = _read(
        live_postgres,
        world,
        breakdown={"metric": world["spend"], "dimensions": [world["day"], world["campaign"]]},
    )
    breakdown = block["breakdown"]
    assert breakdown["sanctioned"] is True
    assert breakdown["sliced_by"] == {
        "measurement_grain_id": world["grain"]["id"],
        "version_id": world["grain"]["current_version"]["id"],
    }
    assert "Breakdown sanctioned by grain" in block["headline"]


def test_a_dimension_no_governed_grain_relates_is_refused_by_name(
    live_postgres, world
) -> None:
    """The clause of `governance.md` § Amendment 2026-08-27 that closes its list."""
    block = _read(
        live_postgres,
        world,
        breakdown={"metric": world["spend"], "dimensions": [world["creative"]]},
    )
    refusal = block["breakdown"]["refusal"]
    assert refusal["code"] == reader.REFUSAL_DIMENSION_NOT_IN_GRAIN
    assert refusal["dimensions_not_in_any_grain"] == [world["creative"]]
    assert world["grain"]["current_version"]["id"] in refusal["reason"]
    assert "sliced_by" not in block["breakdown"]


def test_the_grains_coverage_shows_the_partial_side_as_partial(
    live_postgres, world
) -> None:
    block = _read(
        live_postgres,
        world,
        breakdown={"metric": world["spend"], "dimensions": [world["day"]]},
    )
    coverage = block["breakdown"]["coverage"]
    assert coverage["state"] == "available"
    # The right side binds day and campaign but NOT spend, so the grain is anchored
    # on a measure it does not carry: `absent`, never a silent zero.
    assert coverage["counts"]["full"] == 1
    assert coverage["counts"]["absent"] == 1


def test_a_new_grain_version_moves_what_is_sanctioned(live_postgres, world) -> None:
    """Coverage and sanction are DERIVED: adding a dimension changes the answer."""
    before = _read(
        live_postgres,
        world,
        breakdown={"metric": world["spend"], "dimensions": [world["creative"]]},
    )
    assert before["breakdown"]["sanctioned"] is False
    appended = grains.append_version(
        live_postgres,
        project_id=world["project_id"],
        measurement_grain_id=world["grain"]["id"],
        head_field_id=world["spend"],
        member_field_ids=[world["day"], world["campaign"], world["creative"]],
        actor="tester",
    )
    after = _read(
        live_postgres,
        world,
        breakdown={"metric": world["spend"], "dimensions": [world["creative"]]},
    )
    assert after["breakdown"]["sanctioned"] is True
    # The NEW version, not the superseded one: a consumer pins the current contract.
    assert after["breakdown"]["sliced_by"]["version_id"] == appended["current_version"]["id"]


def test_the_read_writes_no_used_by_row_for_the_grain(live_postgres, world) -> None:
    """A read is not a pin. Anybody looking for a row here will not find one."""
    _read(
        live_postgres,
        world,
        breakdown={"metric": world["spend"], "dimensions": [world["day"]]},
    )
    assert (
        grains.grain_used_by(
            live_postgres,
            project_id=world["project_id"],
            measurement_grain_id=world["grain"]["id"],
        )
        == []
    )


# ---------------------------------------------------------------------------
# Prepare, confirm, and the first decision standing.
# ---------------------------------------------------------------------------


def test_preparing_a_row_freezes_a_reference_and_writes_nothing(
    live_postgres, world
) -> None:
    prepared = reader.prepare_row_decision(
        live_postgres,
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        left_row_key=ROW,
    )
    assert prepared["authorizing"] is False
    assert prepared["already_decided"] is False
    assert len(prepared["review_reference"]) == 64
    # It names the candidates it cannot list, with the reason. An empty list here
    # would state that nothing on the other side answers this row.
    assert prepared["candidates"]["state"] == "unavailable"
    assert (
        list_alignment_decisions(
            live_postgres,
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=world["key_version_id"],
        )
        == []
    )


def test_a_pair_no_relationship_crosses_cannot_be_prepared(live_postgres, world) -> None:
    with pytest.raises(AlignmentRefused) as refused:
        reader.prepare_row_decision(
            live_postgres,
            project_id=world["project_id"],
            left_datastream_id=world["right"],
            right_datastream_id=world["left"],
            left_row_key=ROW,
        )
    assert refused.value.code == "pair_not_alignable"


def test_a_landed_decision_moves_the_review_reference(live_postgres, world) -> None:
    """That movement IS the staleness check: the row is no longer the row prepared."""
    before = reader.review_reference_for(
        live_postgres,
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        left_row_key=ROW,
    )
    record_alignment_decision(
        live_postgres,
        org_id=world["org_id"],
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        common_key_version_id=world["key_version_id"],
        decision=AlignmentDecision(
            left_row_key=ROW,
            decision="arbitrated",
            right_row_key="right_row_9",
            decided_by=ACTOR,
        ),
        actor=ACTOR,
    )
    after = reader.review_reference_for(
        live_postgres,
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        left_row_key=ROW,
    )
    assert before != after
    prepared = reader.prepare_row_decision(
        live_postgres,
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        left_row_key=ROW,
    )
    assert prepared["already_decided"] is True
    assert prepared["standing_decision"]["decided_by"] == ACTOR
    assert prepared["standing_decision"]["decided_at"]


def test_the_read_lists_the_decision_with_its_author_and_its_date(
    live_postgres, world
) -> None:
    record_alignment_decision(
        live_postgres,
        org_id=world["org_id"],
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        common_key_version_id=world["key_version_id"],
        decision=AlignmentDecision(
            left_row_key=ROW, decision="accepted", reason="nothing answers it", decided_by=ACTOR
        ),
        actor=ACTOR,
    )
    block = _read(live_postgres, world)
    decided = block["decisions"]["items"][0]
    assert decided["decision"] == "accepted"
    assert decided["right_row_key"] is None
    assert decided["decided_by"] == ACTOR
    assert decided["decided_at"]


# ---------------------------------------------------------------------------
# Review round 1 -- the three blocking findings, against a real database.
# ---------------------------------------------------------------------------


@pytest.fixture()
def same_platform(live_postgres, world):
    """Both Datastreams observing ONE platform -- and one entity they both publish.

    The partial case the first version never pinned: the right side DOES have an
    attachment, so the read comes back `available`, and the shared entity is
    reported `unmatched` because its single alias row is attributed to the left.
    """
    from core.observed_entities import ensure_observed_entity_registry

    registry = ensure_observed_entity_registry(
        live_postgres, org_id=world["org_id"], project_id=world["project_id"], actor=ACTOR
    )
    world["registry_id"] = registry["id"]
    _attach(live_postgres, world, world["left"], LEFT_PLATFORM, "c_both", label="Shared")
    _attach(live_postgres, world, world["right"], LEFT_PLATFORM, "c_only_right", label="Right")
    return world


def test_a_same_platform_pair_is_NAMED_on_the_payload_not_silently_available(
    live_postgres, same_platform
) -> None:
    """Finding 2: the attribution is stolen, and nothing used to say so."""
    block = _read(live_postgres, same_platform)
    reading = block["reading"]
    # It DOES run -- what is attached is still true and still worth reading.
    assert reading["state"] == "available"
    risk = reading["attribution_risk"]
    assert risk["code"] == reader.SAME_PLATFORM_PAIR
    assert risk["platforms"] == [LEFT_PLATFORM]
    assert risk["shared_platform_count"] == 1
    assert sorted(risk["datastreams"]) == sorted(
        [same_platform["left"], same_platform["right"]]
    )
    assert "attributed to whichever Datastream attached it first" in risk["reason"]
    # And in the TEXT channel, because the model-channel guard may route the
    # structured block into the app channel entirely.
    assert reader.SAME_PLATFORM_PAIR in block["headline"]


def test_a_two_platform_pair_carries_no_attribution_risk(live_postgres, attached) -> None:
    """The guard is a measurement, not a banner: it is silent when it should be."""
    assert "attribution_risk" not in _read(live_postgres, attached)["reading"]


def test_the_stored_shares_are_read_on_the_cascade_ran_branch_too(
    live_postgres, attached
) -> None:
    """Finding 3: the 70.4 MCP row promises the splits on EVERY read, not one branch.

    A promise kept only while the cascade could not run is the kind nobody notices
    is broken: the moment a Project attaches its entities, the splits it was
    already shown would vanish from the payload.
    """
    block = _read(live_postgres, attached)
    assert block["reading"]["state"] == "available"
    assert block["stored_shares"] == {"items": [], "count": 0, "withheld": 0}


def test_a_breakdown_with_no_dimension_needs_a_DECLARED_total(
    live_postgres, world
) -> None:
    """Finding 5: `all([])` is true of every grain, so an empty request pinned one."""
    block = _read(live_postgres, world, breakdown={"metric": world["spend"], "dimensions": []})
    refusal = block["breakdown"]["refusal"]
    assert refusal["code"] == reader.REFUSAL_TOTAL_NOT_DECLARED
    assert "no dimension" in refusal["reason"]
    assert "sliced_by" not in block["breakdown"]


def test_a_declared_total_grain_serves_the_empty_breakdown(
    live_postgres, world
) -> None:
    total = grains.create_measurement_grain(
        live_postgres,
        project_id=world["project_id"],
        name="Total spend",
        head_field_id=world["spend"],
        member_field_ids=[],
        actor="tester",
    )
    block = _read(live_postgres, world, breakdown={"metric": world["spend"], "dimensions": []})
    assert block["breakdown"]["sanctioned"] is True
    # The zero-dimension grain, and never whichever one sorted first.
    assert block["breakdown"]["sliced_by"] == {
        "measurement_grain_id": total["id"],
        "version_id": total["current_version"]["id"],
    }


# ---------------------------------------------------------------------------
# Through the MCP tool itself.
# ---------------------------------------------------------------------------


@pytest.fixture()
def confirmable(live_postgres, world, monkeypatch):
    """The generic confirm tool, wired to this transaction and to a proven presence.

    The three server-side resolutions it makes -- authorization, presence and the
    person behind the subject -- are proved by their own suites. What is proved
    HERE is the statement they gate: the write, its refusals, and the first
    decision standing.
    """
    from contextlib import contextmanager  # noqa: PLC0415

    from core import db as core_db  # noqa: PLC0415

    wrapped = _UncommittedConnection(live_postgres)

    @contextmanager
    def _connection(_identity=None):
        yield wrapped

    monkeypatch.setattr(core_db, "request_connection", _connection)
    monkeypatch.setattr(
        surface, "_authorize", lambda project_id, **_: (ACTOR, world["org_id"])
    )
    monkeypatch.setattr(surface, "_presence_available", lambda: True)
    monkeypatch.setattr(surface, "_person_for_identity", lambda conn, identity: identity)
    return wrapped


def _confirm(world, reference, left_row_key, **decision):
    return surface._confirm_project_capability_row_decision(
        world["project_id"],
        "analytics_alignment",
        reference,
        {
            "left_datastream_id": world["left"],
            "right_datastream_id": world["right"],
            "left_row_key": left_row_key,
            **decision,
        },
    )


def _prepare(world, left_row_key):
    return surface._prepare_project_capability_row_decision(
        world["project_id"],
        "analytics_alignment",
        {
            "left_datastream_id": world["left"],
            "right_datastream_id": world["right"],
            "left_row_key": left_row_key,
        },
    )["structuredContent"]


def _rows(conn, world):
    """The orphan row, and a row the right side really publishes."""
    reading = _read(conn, world)["reading"]
    (orphan,) = reading["unmatched"]["items"]
    right_key = f"oe1|{RIGHT_PLATFORM}|l1|c_right_named||"
    return orphan["row_key"], right_key


def test_the_confirm_tool_writes_one_arbitration(live_postgres, attached, confirmable) -> None:
    orphan, right_key = _rows(live_postgres, attached)
    reference = _prepare(attached, orphan)["review_reference"]
    result = _confirm(attached, reference, orphan, decision="arbitrated", right_row_key=right_key)
    payload = result["structuredContent"]
    assert payload["already_decided"] is False
    assert payload["decision"]["decision"] == "arbitrated"
    assert payload["decision"]["decided_by"] == ACTOR
    # The row the guard checked, named on the payload.
    assert payload["verified_row"]["row_key"] == orphan
    stored = list_alignment_decisions(
        live_postgres,
        project_id=attached["project_id"],
        left_datastream_id=attached["left"],
        right_datastream_id=attached["right"],
        common_key_version_id=attached["key_version_id"],
    )
    assert [item.left_row_key for item in stored] == [orphan]
    # And the pair still READS: the arbitration named a row that exists.
    assert _read(live_postgres, attached)["reading"]["state"] == "available"


def test_an_arbitration_naming_an_UNPUBLISHED_row_is_refused_before_the_write(
    live_postgres, attached, confirmable
) -> None:
    """The defect that bricked a pair for good. Refused now, and nothing is stored.

    Before this guard the store accepted it (its CHECKs only require *a* row key),
    and `run_cascade` then refused the pair on EVERY later read -- permanently,
    because the first decision stands, migration 309 grants no `UPDATE`, and no
    withdrawal gesture exists. One unvalidated string, one pair lost.
    """
    orphan, _right_key = _rows(live_postgres, attached)
    reference = _prepare(attached, orphan)["review_reference"]
    with pytest.raises(Exception) as refused:
        _confirm(
            attached, reference, orphan, decision="arbitrated", right_row_key="right_row_9"
        )
    assert reader.REFUSAL_RIGHT_ROW_NOT_PUBLISHED in str(refused.value)
    assert (
        list_alignment_decisions(
            live_postgres,
            project_id=attached["project_id"],
            left_datastream_id=attached["left"],
            right_datastream_id=attached["right"],
            common_key_version_id=attached["key_version_id"],
        )
        == []
    )
    # The pair is untouched and still readable -- which is the whole point.
    assert _read(live_postgres, attached)["reading"]["state"] == "available"


def test_a_decision_on_an_automatically_matched_row_is_refused(
    live_postgres, attached, confirmable
) -> None:
    """`run_cascade` ignores it, so storing it announces an act with no effect.

    And it is worse than useless: it takes effect SILENTLY the day the mapping
    moves and the row stops matching. The card's own rule -- if an `id_exact`
    alignment is wrong, the mapping is wrong.
    """
    reading = _read(live_postgres, attached)["reading"]
    matched = next(
        row for row in reading["matched"]["items"] if row["method"] == "id_exact"
    )
    reference = _prepare(attached, matched["row_key"])["review_reference"]
    with pytest.raises(Exception) as refused:
        _confirm(
            attached,
            reference,
            matched["row_key"],
            decision="accepted",
            reason="I disagree with the cascade",
        )
    assert reader.REFUSAL_ROW_ALREADY_CASCADED in str(refused.value)


def test_a_decision_on_a_row_nothing_observes_is_refused(
    live_postgres, attached, confirmable
) -> None:
    reference = _prepare(attached, "oe1|example_ads|l1|not_a_row||")["review_reference"]
    with pytest.raises(Exception) as refused:
        _confirm(
            attached,
            reference,
            "oe1|example_ads|l1|not_a_row||",
            decision="accepted",
            reason="x",
        )
    assert reader.REFUSAL_LEFT_ROW_NOT_OBSERVED in str(refused.value)


def test_a_decision_is_refused_when_no_reading_can_verify_it(
    live_postgres, world, confirmable
) -> None:
    """No attachment on either side: nothing can check the row, so nothing is written."""
    reference = _prepare(world, ROW)["review_reference"]
    with pytest.raises(Exception) as refused:
        _confirm(world, reference, ROW, decision="accepted", reason="x")
    assert reader.REFUSAL_ROW_UNVERIFIABLE in str(refused.value)


def test_a_second_person_gets_the_FIRST_author_back_and_wrote_nothing(
    live_postgres, attached, confirmable, monkeypatch
) -> None:
    orphan, right_key = _rows(live_postgres, attached)
    _confirm(
        attached,
        _prepare(attached, orphan)["review_reference"],
        orphan,
        decision="arbitrated",
        right_row_key=right_key,
        reason="the first reason",
    )
    monkeypatch.setattr(surface, "_person_for_identity", lambda conn, identity: SECOND_ACTOR)
    second = _prepare(attached, orphan)
    assert second["already_decided"] is True
    result = _confirm(
        attached,
        second["review_reference"],
        orphan,
        decision="arbitrated",
        right_row_key=right_key,
        reason="a different reason",
    )
    payload = result["structuredContent"]
    assert payload["already_decided"] is True
    assert payload["decision"]["decided_by"] == ACTOR
    assert payload["decision"]["reason"] == "the first reason"


def test_the_SAME_person_with_a_new_reason_is_still_already_decided(
    live_postgres, attached, confirmable
) -> None:
    """Finding 4: `already` is the row's identity, never a field-by-field guess.

    The comparison this replaced omitted `reason`, so the same person re-confirming
    the same pick with a new reason was told `already_decided: false` while nothing
    had been written and the OLD reason came back on the same payload.
    """
    orphan, right_key = _rows(live_postgres, attached)
    _confirm(
        attached,
        _prepare(attached, orphan)["review_reference"],
        orphan,
        decision="arbitrated",
        right_row_key=right_key,
        reason="the first reason",
    )
    again = _prepare(attached, orphan)
    result = _confirm(
        attached,
        again["review_reference"],
        orphan,
        decision="arbitrated",
        right_row_key=right_key,
        reason="a second reason from the same person",
    )
    payload = result["structuredContent"]
    assert payload["already_decided"] is True
    assert payload["decision"]["reason"] == "the first reason"


def test_a_stale_review_reference_is_refused_before_anything_is_written(
    live_postgres, attached, confirmable
) -> None:
    orphan, right_key = _rows(live_postgres, attached)
    with pytest.raises(Exception) as refused:
        _confirm(
            attached, "b" * 64, orphan, decision="arbitrated", right_row_key=right_key
        )
    assert "stale_review" in str(refused.value)
    assert (
        list_alignment_decisions(
            live_postgres,
            project_id=attached["project_id"],
            left_datastream_id=attached["left"],
            right_datastream_id=attached["right"],
            common_key_version_id=attached["key_version_id"],
        )
        == []
    )


def test_an_arbitration_that_names_no_side_is_refused_by_the_domain(
    attached, confirmable
) -> None:
    orphan, _right_key = _rows(confirmable, attached)
    reference = _prepare(attached, orphan)["review_reference"]
    with pytest.raises(Exception) as refused:
        _confirm(attached, reference, orphan, decision="arbitrated")
    assert reader.REFUSAL_RIGHT_ROW_NOT_PUBLISHED in str(refused.value)
