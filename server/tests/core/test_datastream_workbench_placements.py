"""The `Placements` tab -- stories 61.1 and 61.2, epic 61.

WHAT STORY 61.2 ADDED TO THIS FILE, and each line of it is one of its five:

  * ONE WORD for the states the tab used to compose out of a boolean, a count and
    a separate panel -- `core.plan_matching_states`, whose vocabulary is the one
    `file_source_recognizer` already writes for a source column;
  * `matched` is not "a campaign row exists": it is "a campaign is ACTIVELY
    ventilating", because `plan_vs_actual_daily.sql` gives an orphaned match no
    money and a line that receives none is a line still waiting;
  * TWO placements on one line produce NO ambiguity, and no line ever carries
    `ambiguous` at all -- the engine that could justify it has no caller;
  * `plan_line_placement_mappings.status` no longer crosses the wire: it has only
    ever held `active`;
  * the two French payload values are gone from `mediaplan_mapping`, and the
    sentence that explains each now travels beside it.

In-memory doubles, on the pattern `tests/core/test_datastream_workbench_cost_tab.py`
established for the other capability tab: what is proved here is WHO DECIDES and
WHAT IS REFUSED. That the addresses are mounted is proved by
`tests/conformance/test_route_inventory_is_current.py`.

THE SIX THINGS THIS FILE EXISTS TO HOLD:

  * `placements` is a tab of the generated contract, with its schema, and the
    capability state decides ALONE and FIRST whether a plan is read at all;
  * the reading is filtered by CONNECTOR and never by account, and it says how
    many Datastreams share the slice -- with `None`, never `0`, when nobody
    counted;
  * a plan line with no match of this connector is SHOWN, not hidden: it is the
    material of the workshop, and hiding it is what made 61.1 unbuildable;
  * a placement is `(breakdown_dimension, breakdown_value)` of a dimension the
    connector DECLARES -- 2 manifests of 39 do -- and the other 37 get a reason,
    not an empty table and not an invented identity;
  * an unreachable mart is a typed refusal, and "there is nothing" and "we could
    not look" share no word;
  * an attachment hangs from a match that exists, on the dimension the manifest
    declares, and it can be undone.

THE TWO CONNECTORS ARE MEASURED, NOT CHOSEN. Scan of the 39 `manifest.json` of
`server/modules` on 2026-08-09, key `canonical_dimension_mapping`:

    cm360  placement_id -> placement_id
    x-ads  placement    -> placement

and nothing else. `meta-ads` declares campaign, adset, ad and creative and no
placement at all, which is why it is the connector used below for the 37-case.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core.datastream_workbench import TAB_SCHEMAS, TABS
from core.datastream_workbench_placements import (
    COUNT_UNREADABLE_REASON,
    EMPTY_NO_LINE_MESSAGE,
    EMPTY_NO_LINE_NAMES_CONNECTOR,
    EMPTY_NO_PLAN,
    EMPTY_NO_PLAN_MESSAGE,
    EMPTY_OWNER,
    NO_CANDIDATE_MESSAGE,
    NO_UNMATCHED_SPEND_MESSAGE,
    NOTHING_AWAITING_DECISION_MESSAGE,
    PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE,
    STATE_AVAILABLE,
    STATE_CAPABILITY_INACTIVE,
    STATE_NO_PLAN,
    PlacementEvidenceUnavailable,
    PlacementMatchNotProposed,
    confirm_placement_match,
    read_placement_suggestions,
    read_placements_evidence,
)
from core.mediaplan_mapping import (
    REASON_OUT_OF_WINDOW,
    REASON_UNMAPPED,
    UNMAPPED_REASON_LABELS,
)
from core.money_policy import PolicyGap
from core.plan_line_placements import (
    PLACEMENT_CANONICAL_TARGETS,
    PlacementNotFoundError,
    PlacementValidationError,
    attach_placement,
    count_placements_on_campaign,
    detach_placement,
    placement_dimension_for,
    placement_dimension_index,
)
from core.plan_matching_states import (
    MAPPING_STATUS_LABELS,
    MATCH_METHOD_LABELS,
    MATCH_METHOD_UNRECORDED_LABEL,
    MATCH_METHOD_UNRECORDED_REASON,
    MATCHING_STATE_ACCEPTED,
    MATCHING_STATE_AMBIGUOUS,
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
    PLAN_MATCHING_STATES,
    SUGGEST_MATCHES_LABEL,
)
from core.project_capability_states import (
    CAPABILITY_ACTIVE_STATES,
    CAPABILITY_STATE_UNSET,
    PLACEMENT_MAPPING_CAPABILITY_KEY,
    PROJECT_CAPABILITY_KEYS,
)

_PROJECT = "proj_EXAMPLE"
_DATASTREAM = "ds_EXAMPLE"
_PLAN = "1b6bd5c0-0000-4000-8000-000000000001"
_OTHER_PLAN = "1b6bd5c0-0000-4000-8000-000000000002"

#: Three lines of one plan version. TWO of them carry no match of the connector
#: under test, which is the shape the whole "24 lines of which 6" reading rests
#: on: a fixture where every line is matched cannot fail the assertion that an
#: unmatched line is shown.
_LINES = [
    ("line_display_q3", "Display Q3", "display", "2026-07-01", "2026-09-30",
     "120000.00", "cpm", False, 1),
    ("line_video_q3", "Video Q3", "video", "2026-07-01", "2026-09-30",
     "80000.00", "cpv", False, 2),
    ("line_search_q3", "Search Q3", "search", "2026-07-01", "2026-09-30",
     "40000.00", "cpc", False, 3),
]

#: One match, on ONE of the three lines, for the connector under test.
#:
#: STORY 61.3 ADDED THE LAST TWO COLUMNS: how the match was obtained, and the
#: score it was judged on. `similarity` here on purpose -- it is the ONLY level
#: that carries a number to a screen, so a fixture built on `exact` could not fail
#: the assertion that the other three print none.
_MAPPINGS = [
    ("line_display_q3", "camp_EXAMPLE_1", "1.000000", "active", "similarity", 0.91)
]

#: One placement attached to that match. `placement_id` because the connector
#: under test is `cm360`, and its manifest declares that dimension and no other.
_PLACEMENTS = [
    (
        "3f0e0000-0000-4000-8000-00000000000a",
        "line_display_q3",
        "camp_EXAMPLE_1",
        "placement_id",
        "plc_EXAMPLE_feed",
        "active",
        "owner@example.com",
        None,
    )
]


class _Cursor:
    """A cursor answering by SQL SHAPE, so the ORDER of the reads is provable."""

    def __init__(self, owner: "_Connection") -> None:
        self._owner = owner
        self._rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self._owner.statements.append(sql)
        # THE CARRIERS -- amended 2026-08-24. `source_kind = 'managed_feed'` is
        # what tells this read from every other `app.datastreams` one: it asks
        # which Datastreams of the Project could carry a media plan, so the
        # `no_plan` emptiness can offer a door instead of naming a gesture with
        # no address. It is answered BEFORE the plan branch because the two
        # statements name different tables and matching on the table alone would
        # hand plan rows to a carrier read.
        if "source_kind = 'managed_feed'" in sql:
            self._rows = list(self._owner.carriers)
        elif "project_capabilities" in sql:
            self._rows = [] if self._owner.state is None else [(self._owner.state,)]
        elif "plan_unmatched_spend_decisions" in sql:
            self._rows = list(self._owner.decisions)
        elif "app.media_plans" in sql:
            self._rows = list(self._owner.plans)
        elif "media_plan_lines" in sql:
            self._rows = list(self._owner.lines)
        elif "plan_line_placement_mappings" in sql:
            self._rows = list(self._owner.placements)
        # BEFORE the connector-scoped read below, and the alias is what tells them
        # apart: story 61.3 counts a line's matches across EVERY connector, because
        # that is what a confirmation is about to rewrite.
        elif "match_count" in sql:
            counts: dict[str, int] = {}
            for row in self._owner.mappings:
                counts[row[0]] = counts.get(row[0], 0) + 1
            self._rows = sorted(counts.items())
        elif "plan_line_mappings" in sql:
            self._rows = list(self._owner.mappings)
        elif "project_preferences" in sql:
            # Story 61.4: the currency `fact_daily_kpi.value` was converted INTO.
            # An absent row is a Project that never chose -- Story 48.3 removed
            # the `'EUR'` DEFAULT -- and it must not read as euros by omission.
            self._rows = (
                [] if self._owner.canonical_currency is None
                else [(self._owner.canonical_currency,)]
            )
        elif "COUNT(*)" in sql:
            if self._owner.count_raises is not None:
                raise self._owner.count_raises
            self._rows = [(self._owner.datastreams_on_connector,)]
        else:  # pragma: no cover -- an unexpected read must not answer silently
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(
        self,
        *,
        state: str | None,
        plans=None,
        lines=None,
        mappings=None,
        placements=None,
        decisions=None,
        datastreams_on_connector: int = 1,
        count_raises: Exception | None = None,
        canonical_currency: str | None = "EUR",
        carriers=None,
    ):
        self.state = state
        #: Rows of the carrier read -- `(id, name, current_mapping_version_id)`.
        #: Empty by default: a Project whose file sources carry no plan template
        #: has no carrier, which is the state of every fixture in this file.
        self.carriers = carriers if carriers is not None else []
        #: Story 61.4. `app.project_preferences.canonical_currency` -- the currency
        #: the warehouse FX join converted the observed spend INTO, and therefore
        #: the ONLY currency that may label it on this tab.
        self.canonical_currency = canonical_currency
        #: Rows of `app.plan_unmatched_spend_decisions` -- story 61.2. Empty by
        #: default, which is the state of every plan in both databases today
        #: (measured: every plan table carries 0 rows).
        self.decisions = decisions if decisions is not None else []
        self.plans = (
            plans
            if plans is not None
            else [(_PLAN, "Brand Q3", "EUR", None), (_OTHER_PLAN, "Always-on", "EUR", None)]
        )
        self.lines = lines if lines is not None else _LINES
        self.mappings = mappings if mappings is not None else _MAPPINGS
        self.placements = placements if placements is not None else _PLACEMENTS
        self.datastreams_on_connector = datastreams_on_connector
        self.count_raises = count_raises
        self.statements: list[str] = []

    def cursor(self):
        return _Cursor(self)


_UNMAPPED = {
    "plan_id": _PLAN,
    "project_id": _PROJECT,
    "window": {"start": "2026-07-01", "end": "2026-09-30"},
    "unmapped": [
        {"connector": "cm360", "campaign_ref": "camp_EXAMPLE_9", "spend": 4210.5,
         "reason": REASON_UNMAPPED,
         "reason_label": UNMAPPED_REASON_LABELS[REASON_UNMAPPED]},
        # Another connector's unmatched spend, on the same plan. It must not
        # travel on this Datastream's wire.
        {"connector": "meta-ads", "campaign_ref": "camp_EXAMPLE_8", "spend": 990.0,
         "reason": REASON_UNMAPPED,
         "reason_label": UNMAPPED_REASON_LABELS[REASON_UNMAPPED]},
    ],
}

#: One acceptance, on the cm360 row above. Story 61.2: a decided row STAYS in the
#: panel -- accepting says "this spend was not planned, and we know", never "it
#: was planned after all".
_DECISIONS = [
    ("camp_EXAMPLE_9", "Brand takeover bought outside the plan cycle.",
     "owner@example.com", None),
]

_OBSERVED = [
    {"connector": "cm360", "breakdown_value": "plc_EXAMPLE_feed", "row_count": 92},
    {"connector": "cm360", "breakdown_value": "plc_EXAMPLE_sidebar", "row_count": 61},
]


def _read(
    conn,
    *,
    connector: str = "cm360",
    plan_id: str | None = None,
    unmapped=None,
    observed=None,
    unmapped_raises=None,
    observed_raises=None,
    money_policy_currency: str | None = "EUR",
):
    """`read_placements_evidence` with both warehouse reads and the policy doubled."""

    def _unmapped(*_args, **_kwargs):
        if unmapped_raises is not None:
            raise unmapped_raises
        return _UNMAPPED if unmapped is None else unmapped

    def _observed(*_args, **_kwargs):
        if observed_raises is not None:
            raise observed_raises
        return list(_OBSERVED if observed is None else observed)

    def _policy(_conn, *, project_id):  # noqa: ARG001 -- signature fidelity
        if money_policy_currency is None:
            raise PolicyGap(
                "money_policy_unconfirmed",
                "This Project has no confirmed Money Policy.",
                capability="currency_fx",
            )
        return SimpleNamespace(
            reporting_currency=money_policy_currency, version_id="rsv_EXAMPLE_money"
        )

    with (
        patch("core.mediaplan_mapping.list_unmapped_actuals", side_effect=_unmapped) as unmapped_fn,
        patch("core.warehouse.query_breakdown_values", side_effect=_observed) as observed_fn,
        patch("core.money_policy.resolve_money_policy", _policy),
    ):
        evidence = read_placements_evidence(
            conn,
            project_id=_PROJECT,
            datastream_id=_DATASTREAM,
            connector=connector,
            plan_id=plan_id,
        )
    return evidence, unmapped_fn, observed_fn


# ---------------------------------------------------------------------------
# The tab exists, and the capability decides first.
# ---------------------------------------------------------------------------


def test_placements_is_a_tab_of_the_generated_contract_with_its_schema() -> None:
    assert "placements" in TABS
    assert TAB_SCHEMAS["placements"] == "datastream_workbench.placements.v1"
    # Between `cost` and `processing` -- the ratified order of
    # `datastream-workbench-and-wizard.md`, amendment 3: `Overview`, `Mapping`,
    # `Data`, [`Cost`], [`Placements`], `Processing`, `Runs`, `Outputs`.
    assert TABS.index("cost") < TABS.index("placements") < TABS.index("processing")
    # And the key is the one the database accepts since migration 243.
    assert PLACEMENT_MAPPING_CAPABILITY_KEY in PROJECT_CAPABILITY_KEYS


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", None])
def test_an_inactive_capability_reads_no_plan_and_no_warehouse(state) -> None:
    conn = _Connection(state=state)
    evidence, unmapped_fn, observed_fn = _read(conn)

    assert evidence["state"] == STATE_CAPABILITY_INACTIVE
    assert evidence["capability"]["active"] is False
    assert evidence["lines"] is None and evidence["plans"] is None
    # THE POINT OF THE ORDER: not one warehouse round trip, and not one statement
    # beyond the switch itself.
    unmapped_fn.assert_not_called()
    observed_fn.assert_not_called()
    assert len(conn.statements) == 1
    assert "project_capabilities" in conn.statements[0]


def test_a_project_the_control_plane_never_wrote_reads_unset_not_disabled() -> None:
    conn = _Connection(state=None)
    evidence, _, _ = _read(conn)

    # `unset` is a row that was never written; `disabled` is a decision somebody
    # took. They are repaired at different doors.
    assert evidence["capability"]["state"] == CAPABILITY_STATE_UNSET


@pytest.mark.parametrize("state", CAPABILITY_ACTIVE_STATES)
def test_an_active_capability_reads_the_plan_after_the_switch(state) -> None:
    conn = _Connection(state=state)
    evidence, _, _ = _read(conn)

    assert evidence["state"] == STATE_AVAILABLE
    assert "project_capabilities" in conn.statements[0]


# ---------------------------------------------------------------------------
# All the lines, and which of them names this connector (arbitrage A2).
# ---------------------------------------------------------------------------


def test_every_line_of_the_plan_is_listed_and_says_whether_this_connector_is_attached() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    keys = [line["line_key"] for line in evidence["lines"]]
    # THE ASSERTION THE STORY EXISTS FOR: three lines in, three lines out. The
    # unmatched ones are the material of the workshop, not its blind spot.
    assert keys == ["line_display_q3", "line_video_q3", "line_search_q3"]
    # Story 61.2: ONE WORD, not the boolean the console used to turn into a badge.
    assert [line["matching_state"] for line in evidence["lines"]] == [
        MATCHING_STATE_MATCHED, MATCHING_STATE_UNMATCHED, MATCHING_STATE_UNMATCHED,
    ]
    assert [line["matching_state_label"] for line in evidence["lines"]] == [
        "Matched", "Nothing observed", "Nothing observed",
    ]
    assert "attached" not in evidence["lines"][0]
    # And the count says how many of how many -- the "6 lines out of 24" reading.
    assert evidence["line_counts"]["total"] == 3
    assert evidence["line_counts"]["matched"] == 1
    # Something IS waiting, so the "nothing is waiting" sentence is absent. A
    # sentence that is always there is a sentence nobody reads.
    assert evidence["line_counts"]["nothing_awaiting_message"] is None
    # The plan columns a person matches on travel with the line.
    display = evidence["lines"][0]
    assert display["label"] == "Display Q3"
    assert display["channel"] == "display"
    assert display["budget"] == "120000.00"
    assert (display["start_date"], display["end_date"]) == ("2026-07-01", "2026-09-30")


def test_a_line_carries_its_campaigns_and_each_campaign_its_placements() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    display = evidence["lines"][0]
    assert [campaign["campaign_ref"] for campaign in display["campaigns"]] == ["camp_EXAMPLE_1"]
    campaign = display["campaigns"][0]
    # The weight is the EXACT string of the NUMERIC(7,6) column, never a float:
    # `0.333333` read back as a float is not one third.
    assert campaign["split_weight"] == "1.000000"
    assert campaign["status"] == "active"
    # Story 61.2: the raw word never travels alone. `active`/`orphaned` are a
    # database lifecycle, and what reaches a person is what it costs them.
    assert campaign["status_label"] == "Ventilating spend"
    # The third level, which nothing carried before this story.
    assert [placement["breakdown_value"] for placement in campaign["placements"]] == [
        "plc_EXAMPLE_feed"
    ]
    assert campaign["placements"][0]["breakdown_dimension"] == "placement_id"
    # A line with no campaign carries an empty list and no invented one.
    assert evidence["lines"][1]["campaigns"] == []


def test_a_plan_no_line_of_which_names_this_connector_says_the_ratified_sentence() -> None:
    conn = _Connection(state="ready", mappings=[], placements=[])
    evidence, _, _ = _read(conn)

    # Still `available`: the plan WAS read, and its lines are the work to do.
    assert evidence["state"] == STATE_AVAILABLE
    assert evidence["empty_code"] == EMPTY_NO_LINE_NAMES_CONNECTOR
    assert evidence["reason"] == EMPTY_NO_LINE_MESSAGE
    assert evidence["line_counts"]["total"] == 3
    assert evidence["line_counts"]["matched"] == 0
    # The lines are still listed. Emptiness is a sentence, not a hidden table.
    assert len(evidence["lines"]) == 3


# ---------------------------------------------------------------------------
# Which plan (arbitrage A5).
# ---------------------------------------------------------------------------


def test_the_tab_names_every_plan_it_could_read_and_which_one_it_read() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    assert [plan["id"] for plan in evidence["plans"]] == [_PLAN, _OTHER_PLAN]
    # No Project designates an active plan -- only a VERSION carries `is_active` --
    # so the default is the newest and the payload SAYS which was read rather than
    # leaving a reader to assume.
    assert evidence["selected_plan"]["id"] == _PLAN
    assert evidence["selected_plan"]["name"] == "Brand Q3"


def test_a_chosen_plan_is_the_one_read_and_an_unknown_one_does_not_break_the_tab() -> None:
    conn = _Connection(state="ready")
    chosen, _, _ = _read(conn, plan_id=_OTHER_PLAN)
    assert chosen["selected_plan"]["id"] == _OTHER_PLAN

    # A stale bookmark naming a plan of another Project must not make the tab
    # unopenable, and it must not silently claim to have read it either.
    stale, _, _ = _read(_Connection(state="ready"), plan_id="1b6bd5c0-0000-4000-8000-0000000000ff")
    assert stale["selected_plan"]["id"] == _PLAN


def test_a_project_with_no_media_plan_says_so_and_names_who_creates_one() -> None:
    conn = _Connection(state="ready", plans=[])
    evidence, unmapped_fn, observed_fn = _read(conn)

    assert evidence["state"] == STATE_NO_PLAN
    assert evidence["empty_code"] == EMPTY_NO_PLAN
    assert evidence["reason"] == EMPTY_NO_PLAN_MESSAGE
    assert evidence["owner"] == EMPTY_OWNER
    # THE OWNER IS THE CARRIER'S WORKBENCH -- ratified 2026-08-24. It read
    # `"Governance"` until that day, and no Governance screen has ever created a
    # media plan; naming a real owner is what lets this emptiness carry a door
    # instead of a gesture with no address.
    assert "Governance" not in evidence["owner"]
    assert "Workbench" in evidence["owner"]
    # AND THE ADDRESS TRAVELS EVEN WHEN IT IS EMPTY. A console that had to guess
    # whether the server knew about carriers would guess wrong once.
    assert evidence["carriers"] == []
    # And nothing was asked of the warehouse for a plan that does not exist.
    unmapped_fn.assert_not_called()
    observed_fn.assert_not_called()


def test_the_no_plan_emptiness_carries_the_address_of_the_carrier_workbench(
    monkeypatch,
) -> None:
    """The door itself -- `analyze-and-test.md`, ratified 2026-08-24.

    THE STORE IS STUBBED AND THE PAYLOAD IS NOT. Which Datastreams carry a plan
    is `mediaplan_store.list_carrier_datastreams`, proven against the real schema
    in `test_mediaplan_store.py` -- it resolves each Datastream's sealed Template
    contract, which a fake cursor cannot honestly answer. What this asserts is
    the half that belongs here: that the `no_plan` emptiness puts that answer on
    the wire, so the tab can offer a door instead of naming a gesture with no
    address. `plan_id` being `None` is the difference between the two gestures --
    naming the plan, or importing its next dated revision.
    """
    from core import mediaplan_store

    carriers = [
        {
            "datastream_id": "ds_EXAMPLE_PLAN",
            "name": "Agency plan file",
            "plan_id": None,
            "plan_name": None,
        }
    ]
    monkeypatch.setattr(
        mediaplan_store, "list_carrier_datastreams", lambda _conn, *, project_id: carriers
    )

    conn = _Connection(state="ready", plans=[])
    evidence, _, _ = _read(conn)

    assert evidence["state"] == STATE_NO_PLAN
    assert evidence["carriers"] == carriers


# ---------------------------------------------------------------------------
# The connector, not the account (arbitrage A6).
# ---------------------------------------------------------------------------


def test_the_slice_is_the_connector_s_and_the_sentence_carries_the_number() -> None:
    conn = _Connection(state="ready", datastreams_on_connector=3)
    evidence, _, _ = _read(conn)

    assert evidence["grain"]["connector"] == "cm360"
    assert evidence["grain"]["datastream_grain"] is False
    assert evidence["grain"]["datastreams_on_connector"] == 3
    assert "3 Datastream(s)" in evidence["grain"]["reason"]


def test_a_count_that_could_not_be_read_is_absent_and_never_a_zero() -> None:
    conn = _Connection(state="ready", count_raises=RuntimeError("relation unavailable"))
    evidence, _, _ = _read(conn)

    # `0` here would report a Project with no Datastream on the very connector
    # whose Datastream is being looked at.
    assert evidence["grain"]["datastreams_on_connector"] is None
    assert evidence["grain"]["reason"] == COUNT_UNREADABLE_REASON
    assert "not a zero" in COUNT_UNREADABLE_REASON
    # And the tab still renders: an unread count is not a broken tab.
    assert evidence["state"] == STATE_AVAILABLE


def test_the_unmatched_spend_panel_carries_this_connector_and_no_other() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    rows = evidence["unmapped"]["rows"]
    assert [row["campaign_ref"] for row in rows] == ["camp_EXAMPLE_9"]
    assert {row["connector"] for row in rows} == {"cm360"}
    assert evidence["unmapped"]["window"] == {"start": "2026-07-01", "end": "2026-09-30"}


# ---------------------------------------------------------------------------
# What a placement IS, and the 37 connectors that have none (arbitrage A3).
# ---------------------------------------------------------------------------


def test_exactly_two_of_the_thirty_nine_manifests_declare_a_placement_dimension() -> None:
    index = placement_dimension_index()

    assert index == {
        "cm360": {"dimension": "placement_id", "source_field": "placement_id"},
        "x-ads": {"dimension": "placement", "source_field": "placement"},
    }
    # The vocabulary is closed rather than a substring test: `placement_group`
    # would otherwise be swept in silently the day a manifest declares one.
    assert set(PLACEMENT_CANONICAL_TARGETS) == {"placement", "placement_id"}


def test_a_connector_that_declares_no_placement_dimension_gets_a_reason_not_a_table() -> None:
    conn = _Connection(state="ready", placements=[])
    evidence, _, observed_fn = _read(conn, connector="meta-ads")

    assert evidence["placement_dimension"]["declared"] is False
    assert evidence["placement_dimension"]["dimension"] is None
    assert "declares no placement dimension" in evidence["placement_dimension"]["reason"]
    assert evidence["observed_placements"]["state"] == "not_applicable"
    assert evidence["observed_placements"]["values"] == []
    # And nothing was invented to compensate: the mart was never asked.
    observed_fn.assert_not_called()


def test_the_observed_placements_are_read_on_the_declared_dimension_and_the_plan_window() -> None:
    conn = _Connection(state="ready")
    evidence, _, observed_fn = _read(conn)

    assert evidence["observed_placements"]["state"] == "available"
    assert [value["breakdown_value"] for value in evidence["observed_placements"]["values"]] == [
        "plc_EXAMPLE_feed", "plc_EXAMPLE_sidebar",
    ]
    # The dimension is the manifest's, and the window is the PLAN's -- a placement
    # observed outside every line of the plan is not one this plan bought.
    assert observed_fn.call_args.args == (_PROJECT, "placement_id", "2026-07-01", "2026-09-30")
    # And the honest degrade contract is the one asked for, not the swallowing one.
    assert observed_fn.call_args.kwargs == {"strict": True}


def test_the_capability_verdict_of_the_thirty_seven_is_the_one_already_shipped() -> None:
    """61.1 does not fabricate a placement to make a screen look alive.

    `PlacementMappingCompiler` answers `not_applicable` with its reason, and this
    tab's answer for the same connector is the same absence with the same shape.
    Two surfaces contradicting each other about whether a connector can carry a
    placement is the defect this pins.
    """
    from core.capability_compilers import _NO_PLACEMENT_EVIDENCE

    assert "No placement dimension is declared" in _NO_PLACEMENT_EVIDENCE
    assert placement_dimension_for("meta-ads")["declared"] is False
    assert placement_dimension_for("ga4")["declared"] is False
    assert placement_dimension_for("cm360")["declared"] is True


# ---------------------------------------------------------------------------
# Broken is not empty.
# ---------------------------------------------------------------------------


def test_an_unreachable_mart_on_the_actuals_is_a_refusal_not_an_empty_panel() -> None:
    from core.warehouse import WarehouseUnavailable

    conn = _Connection(state="ready")
    with pytest.raises(PlacementEvidenceUnavailable):
        _read(conn, unmapped_raises=WarehouseUnavailable("marts absent"))


def test_an_unreachable_mart_on_the_observed_placements_is_a_refusal_too() -> None:
    from core.warehouse import WarehouseUnavailable

    conn = _Connection(state="ready")
    with pytest.raises(PlacementEvidenceUnavailable):
        _read(conn, observed_raises=WarehouseUnavailable("marts absent"))


def test_broken_and_empty_share_no_word_at_all() -> None:
    broken = set(PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE.lower().split())
    for empty in (
        EMPTY_NO_PLAN_MESSAGE,
        EMPTY_NO_LINE_MESSAGE,
        # Story 61.2's two emptinesses join the rule rather than sit beside it.
        NO_UNMATCHED_SPEND_MESSAGE,
        NOTHING_AWAITING_DECISION_MESSAGE,
    ):
        assert not broken & set(empty.lower().split()), empty


def test_the_two_empty_sentences_are_two_sentences() -> None:
    # "This Project has no plan" and "this plan has no line for you" send a person
    # to two different doors; one sentence for both would send them to the wrong one.
    assert EMPTY_NO_PLAN_MESSAGE != EMPTY_NO_LINE_MESSAGE


# ---------------------------------------------------------------------------
# Story 61.2 -- one word for the states, and the one that is not delivered.
# ---------------------------------------------------------------------------


def test_the_state_vocabulary_is_the_one_the_recognizer_already_writes() -> None:
    """Arbitrage A1: reuse, never a fifth set of words.

    `file_source_recognizer` has emitted `matched | ambiguous | unmatched` since
    before this epic, and `column_treatments` consumes them. Story 61.2 borrows
    them for the plan match and adds ONE word, `accepted`, which is the only one
    of the four that comes from a stored human act.
    """
    from core import file_source_recognizer

    assert "status ∈ {matched, ambiguous, unmatched}" in (
        file_source_recognizer.recognize_columns.__doc__ or ""
    )
    assert set(PLAN_MATCHING_STATES) == {
        MATCHING_STATE_MATCHED,
        MATCHING_STATE_AMBIGUOUS,
        MATCHING_STATE_UNMATCHED,
        MATCHING_STATE_ACCEPTED,
    }


def test_matched_means_ventilating_and_an_orphaned_match_is_not_one() -> None:
    """The one status that decides money decides this word too.

    `plan_vs_actual_daily.sql:137` reads `WHERE m.status = 'active'`. A line whose
    every match is `orphaned` receives nothing from the mart, so reading it as
    `matched` because a row exists would tell a person a budget is covered when
    it is not.
    """
    conn = _Connection(
        state="ready",
        mappings=[
            # No level at all: a match written before migration 246, which is what
            # all 49 of them are. It renders as a named absence, never `manual`.
            ("line_display_q3", "camp_EXAMPLE_1", "1.000000", "orphaned", None, None)
        ],
        placements=[],
    )
    evidence, _, _ = _read(conn)

    line = evidence["lines"][0]
    assert line["campaigns"][0]["status"] == "orphaned"
    assert line["campaigns"][0]["status_label"] == MAPPING_STATUS_LABELS["orphaned"]
    # The campaign is listed -- it is the work -- and the line is still waiting.
    assert line["matching_state"] == MATCHING_STATE_UNMATCHED
    assert evidence["line_counts"]["matched"] == 0
    # AND THE 61.1 SENTENCE STAYS TRUE. "No plan line names this connector yet"
    # would be false here: a line does name it, its match is simply orphaned.
    # `matched` and "names this connector" are two different questions.
    assert evidence["empty_code"] is None
    assert evidence["reason"] is None


def test_two_placements_on_one_line_produce_no_ambiguity_at_all() -> None:
    """« Feed + Marketplace + Search results sur une seule ligne est le cas normal ».

    `placement-mapping.md:146` makes it an `Incomplete if`: several placements on
    one line reported as an ambiguity to arbitrate. This is the fixture that would
    have tripped it.
    """
    conn = _Connection(
        state="ready",
        placements=[
            ("3f0e0000-0000-4000-8000-00000000000a", "line_display_q3", "camp_EXAMPLE_1",
             "placement_id", "plc_EXAMPLE_feed", "active", "owner@example.com", None),
            ("3f0e0000-0000-4000-8000-00000000000b", "line_display_q3", "camp_EXAMPLE_1",
             "placement_id", "plc_EXAMPLE_marketplace", "active", "owner@example.com", None),
        ],
    )
    evidence, _, _ = _read(conn)

    line = evidence["lines"][0]
    assert len(line["campaigns"][0]["placements"]) == 2
    assert line["matching_state"] == MATCHING_STATE_MATCHED
    # Not one `ambiguous` anywhere in the payload, at any depth.
    assert MATCHING_STATE_AMBIGUOUS not in repr(evidence["lines"])


def test_the_engine_has_a_production_importer_and_it_is_named() -> None:
    """THE 61.2 GUARD, TURNED OVER -- story 61.3, acceptance 1 and arbitrage A8.

    Until this story the same measurement was asserted the other way round:
    `importers == ["tests/core/test_plan_mapping_suggest.py"]`, a 384-line engine
    with no production caller at all. Turning it over rather than deleting it is
    the point -- the literal inverse of one assertion, so the class it closes
    stays closed. Delete it and a moving part of production can quietly become an
    orphan again, which is the pattern this batch met six times.

    AN IMPORT, NOT A MENTION. Several modules NAME `plan_mapping_suggest` in a
    comment; what is measured is whether anything CALLS it.
    """
    import pathlib
    import re

    imports = re.compile(
        r"^\s*(?:from\s+[\w.]*\bplan_mapping_suggest\b|"
        r"import\s+[\w.]*\bplan_mapping_suggest\b|"
        r"from\s+core\s+import\s+[^\n]*\bplan_mapping_suggest\b)",
        re.MULTILINE,
    )
    server_root = pathlib.Path(__file__).resolve().parents[2]
    importers = sorted(
        path.relative_to(server_root).as_posix()
        for path in server_root.rglob("*.py")
        if path.name != "plan_mapping_suggest.py"
        and imports.search(path.read_text(encoding="utf-8", errors="replace"))
    )
    # The production one is FIRST-CLASS in this assertion: a test-only importer
    # would leave the engine exactly as orphaned as it was.
    assert "core/datastream_workbench_placements.py" in importers, importers
    assert [path for path in importers if not path.startswith("tests/")] == [
        "core/datastream_workbench_placements.py"
    ], importers


def test_the_tab_says_the_fourth_state_is_one_gesture_away_and_names_it() -> None:
    """`ambiguous` is not drawn HERE, and the reason is a cost, not an absence.

    The 61.2 sentence measured that no engine called the module. That measurement
    expired with this story, so the sentence moved with it: what stays true is
    that the sweep is paid on request, and that a badge without candidates behind
    it is a decoration.
    """
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    assert evidence["ambiguity"]["available"] is False
    assert evidence["ambiguity"]["action_label"] == SUGGEST_MATCHES_LABEL
    assert "computed on request" in evidence["ambiguity"]["reason"]
    # And the expired claim is gone from the payload, not merely from the module.
    assert "no caller" not in evidence["ambiguity"]["reason"]
    # No line carries the word either: the candidates are not on this payload.
    assert MATCHING_STATE_AMBIGUOUS not in repr(evidence["lines"])


def test_the_placement_status_no_longer_crosses_the_wire() -> None:
    """Arbitrage A6: a field that has only ever held one value is not a state.

    It is written -- `DEFAULT 'active'` and `DO UPDATE SET status = 'active'` --
    and nothing writes `orphaned` at the placement grain, which migration 244's
    own `COMMENT ON COLUMN` says. Carrying it teaches the next reader it means
    something.
    """
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    placement = evidence["lines"][0]["campaigns"][0]["placements"][0]
    assert set(placement) == {"id", "breakdown_dimension", "breakdown_value"}
    assert "status" not in placement


# ---------------------------------------------------------------------------
# Story 61.2 -- out-of-plan spend, and the decision taken on it.
# ---------------------------------------------------------------------------


def test_out_of_plan_spend_nobody_decided_is_unmatched_and_says_why() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    row = evidence["unmapped"]["rows"][0]
    assert row["matching_state"] == MATCHING_STATE_UNMATCHED
    assert row["matching_state_label"] == "Outside the plan"
    assert row["decision"] is None
    # The value is English and the sentence travels beside it, so the console has
    # nothing left to compare literally.
    assert row["reason"] == REASON_UNMAPPED == "no_match"
    assert row["reason_label"] == "No plan line of this plan matches this campaign."
    assert evidence["unmapped"]["counts"] == {
        "total": 1, "accepted": 0, "awaiting_decision": 1,
    }


def test_an_accepted_spend_row_is_still_listed_and_names_who_decided() -> None:
    """Accepting closes a decision; it does not make unplanned money disappear."""
    conn = _Connection(state="ready", decisions=_DECISIONS)
    evidence, _, _ = _read(conn)

    rows = evidence["unmapped"]["rows"]
    assert [row["campaign_ref"] for row in rows] == ["camp_EXAMPLE_9"]
    assert rows[0]["matching_state"] == MATCHING_STATE_ACCEPTED
    assert rows[0]["matching_state_label"] == "Accepted as unplanned"
    assert rows[0]["decision"]["decided_by"] == "owner@example.com"
    assert rows[0]["decision"]["reason"].startswith("Brand takeover")
    # The spend is untouched: the panel still carries the amount it always did.
    assert rows[0]["spend"] == 4210.5
    assert evidence["unmapped"]["counts"] == {
        "total": 1, "accepted": 1, "awaiting_decision": 0,
    }


def test_the_two_reason_values_are_english_and_each_carries_its_sentence() -> None:
    assert REASON_UNMAPPED == "no_match"
    assert REASON_OUT_OF_WINDOW == "outside_matched_line_window"
    assert set(UNMAPPED_REASON_LABELS) == {REASON_UNMAPPED, REASON_OUT_OF_WINDOW}
    assert all(sentence.endswith(".") for sentence in UNMAPPED_REASON_LABELS.values())


def test_a_decision_of_another_connector_never_reaches_this_tab() -> None:
    """The store filters by connector; this proves the tab asked it to.

    `list_decisions` is scoped `WHERE plan_id = %s AND connector = %s`, so the
    doubled cursor answers the same rows whatever is asked -- what is checked here
    is that the STATEMENT carries both predicates, which is what the real database
    enforces.
    """
    conn = _Connection(state="ready", decisions=_DECISIONS)
    _read(conn)

    decision_sql = next(sql for sql in conn.statements if "plan_unmatched_spend_decisions" in sql)
    assert "plan_id = %s" in decision_sql and "connector = %s" in decision_sql


def test_the_capability_is_still_read_first_and_alone() -> None:
    """Story 61.2 added a read; it did not add one BEFORE the switch.

    The `Contrôle` line of both stories: `placement_mapping` is read first, in one
    statement, and an inactive capability costs no plan, no warehouse and no
    decision read at all.
    """
    conn = _Connection(state="disabled", decisions=_DECISIONS)
    evidence, unmapped_fn, observed_fn = _read(conn)

    assert evidence["state"] == STATE_CAPABILITY_INACTIVE
    assert evidence["ambiguity"] is None
    assert len(conn.statements) == 1
    unmapped_fn.assert_not_called()
    observed_fn.assert_not_called()


def test_every_line_matched_says_nothing_is_waiting_for_a_decision() -> None:
    conn = _Connection(
        state="ready",
        mappings=[
            ("line_display_q3", "camp_EXAMPLE_1", "1.000000", "active", "exact", 1.0),
            ("line_video_q3", "camp_EXAMPLE_2", "1.000000", "active", "normalized", 1.0),
            ("line_search_q3", "camp_EXAMPLE_3", "1.000000", "active", "manual", None),
        ],
        placements=[],
    )
    evidence, _, _ = _read(conn)

    assert evidence["line_counts"]["matched"] == 3
    assert evidence["line_counts"]["nothing_awaiting_message"] == (
        NOTHING_AWAITING_DECISION_MESSAGE
    )


def test_no_out_of_plan_spend_carries_its_own_sentence_and_the_window_it_read() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn, unmapped={
        "plan_id": _PLAN, "project_id": _PROJECT,
        "window": {"start": "2026-07-01", "end": "2026-09-30"}, "unmapped": [],
    })

    assert evidence["unmapped"]["rows"] == []
    assert evidence["unmapped"]["empty_message"] == NO_UNMATCHED_SPEND_MESSAGE
    # An empty state is a MEASUREMENT: the panel keeps the window it looked over.
    assert evidence["unmapped"]["window"] == {"start": "2026-07-01", "end": "2026-09-30"}
    assert evidence["unmapped"]["counts"]["total"] == 0


def test_accepting_reads_and_writes_nothing_any_mart_can_see() -> None:
    """Acceptance 5, pinned on the two files that would have to change.

    `plan_vs_actual_daily.sql` ventilates on `plan_line_mappings.status='active'`
    and on nothing else, and `mirror_sync` syncs an EXPLICIT list of tables the
    new one is not in -- so no dbt model can reference it even by accident.
    """
    import pathlib

    from core.mirror_sync import __file__ as mirror_file

    root = pathlib.Path(__file__).resolve().parents[3]
    mart = (root / "dbt" / "models" / "marts" / "plan_vs_actual_daily.sql").read_text(
        encoding="utf-8"
    )
    assert "WHERE m.status = 'active'" in mart
    assert "plan_unmatched_spend_decisions" not in mart

    mirror = pathlib.Path(mirror_file).read_text(encoding="utf-8")
    assert '"plan_line_mappings",' in mirror
    assert "plan_unmatched_spend_decisions" not in mirror

    dbt_models = root / "dbt" / "models"
    offenders = [
        path.relative_to(root).as_posix()
        for path in dbt_models.rglob("*")
        if path.is_file()
        and "plan_unmatched_spend_decisions"
        in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# Attach and detach -- the gestures the `Permet` line promises.
# ---------------------------------------------------------------------------


class _WriteCursor:
    def __init__(self, owner: "_WriteConnection") -> None:
        self._owner = owner
        self._rows: list[tuple] = []

    def __enter__(self) -> "_WriteCursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self._owner.statements.append((sql, params))
        if "FROM app.plan_line_mappings" in sql:
            self._rows = [(1,)] if self._owner.parent_exists else []
        elif sql.strip().startswith("INSERT INTO app.plan_line_placement_mappings"):
            self._rows = [("3f0e0000-0000-4000-8000-00000000000b", True)]
        elif sql.strip().startswith("DELETE FROM app.plan_line_placement_mappings"):
            self._rows = (
                [("line_display_q3", "camp_EXAMPLE_1", "placement_id", "plc_EXAMPLE_feed")]
                if self._owner.attachment_exists
                else []
            )
        elif "COUNT(*)" in sql:
            self._rows = [(self._owner.sibling_count,)]
        elif "INSERT INTO app.audit_log" in sql:
            self._rows = []
        else:  # pragma: no cover
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _WriteConnection:
    def __init__(self, *, parent_exists=True, attachment_exists=True, sibling_count=3):
        self.parent_exists = parent_exists
        self.attachment_exists = attachment_exists
        self.sibling_count = sibling_count
        self.statements: list[tuple] = []

    def cursor(self):
        return _WriteCursor(self)


def test_attaching_a_placement_writes_it_under_the_declared_dimension() -> None:
    conn = _WriteConnection()
    result = attach_placement(
        conn,
        plan_id=_PLAN,
        line_key="line_display_q3",
        connector="cm360",
        campaign_ref="camp_EXAMPLE_1",
        breakdown_dimension="placement_id",
        breakdown_value="plc_EXAMPLE_feed",
        actor="owner@example.com",
    )

    assert result["breakdown_dimension"] == "placement_id"
    assert result["breakdown_value"] == "plc_EXAMPLE_feed"
    assert result["created"] is True
    # The parent is checked BEFORE the insert, so the caller reads a sentence
    # rather than an integrity error.
    assert "FROM app.plan_line_mappings" in conn.statements[0][0]
    # And the decision is journalled.
    assert any("audit_log" in sql for sql, _ in conn.statements)


def test_attaching_carries_no_weight_and_therefore_ventilates_nothing_twice() -> None:
    """THE INVARIANT A4 REFUSED TO BREAK, pinned on the write itself.

    `SUM(split_weight) = 1.0` per (plan_id, connector, campaign_ref) lives on
    `app.plan_line_mappings`. A placement carrying a weight would make three rows
    where there was one and ventilate a campaign's spend three times.
    """
    conn = _WriteConnection()
    attach_placement(
        conn,
        plan_id=_PLAN,
        line_key="line_display_q3",
        connector="cm360",
        campaign_ref="camp_EXAMPLE_1",
        breakdown_dimension="placement_id",
        breakdown_value="plc_EXAMPLE_feed",
        actor="owner@example.com",
    )

    insert = next(
        sql for sql, _ in conn.statements
        if sql.strip().startswith("INSERT INTO app.plan_line_placement_mappings")
    )
    assert "split_weight" not in insert
    # Nor does the write touch the table that carries the invariant.
    assert not any("UPDATE app.plan_line_mappings" in sql for sql, _ in conn.statements)


def test_a_connector_with_no_declared_placement_dimension_cannot_attach_one() -> None:
    conn = _WriteConnection()
    with pytest.raises(PlacementValidationError) as raised:
        attach_placement(
            conn,
            plan_id=_PLAN,
            line_key="line_display_q3",
            connector="meta-ads",
            campaign_ref="camp_EXAMPLE_1",
            breakdown_dimension="platform_position",
            breakdown_value="feed",
            actor="owner@example.com",
        )

    assert "declares no placement dimension" in str(raised.value)
    # Refused BEFORE any statement: a free-text placement is not written and then
    # regretted.
    assert conn.statements == []


def test_a_dimension_the_connector_does_not_observe_on_is_refused() -> None:
    conn = _WriteConnection()
    with pytest.raises(PlacementValidationError) as raised:
        attach_placement(
            conn,
            plan_id=_PLAN,
            line_key="line_display_q3",
            connector="cm360",
            campaign_ref="camp_EXAMPLE_1",
            breakdown_dimension="placement",
            breakdown_value="plc_EXAMPLE_feed",
            actor="owner@example.com",
        )

    # `placement` is x-ads' dimension, not cm360's. A value stored under the wrong
    # dimension can never be matched against a fact again.
    assert "placement_id" in str(raised.value)


def test_a_placement_cannot_hang_from_a_campaign_this_line_does_not_buy() -> None:
    conn = _WriteConnection(parent_exists=False)
    with pytest.raises(PlacementNotFoundError):
        attach_placement(
            conn,
            plan_id=_PLAN,
            line_key="line_video_q3",
            connector="cm360",
            campaign_ref="camp_EXAMPLE_1",
            breakdown_dimension="placement_id",
            breakdown_value="plc_EXAMPLE_feed",
            actor="owner@example.com",
        )


def test_detaching_is_scoped_by_plan_and_connector_not_by_identifier_alone() -> None:
    conn = _WriteConnection()
    result = detach_placement(
        conn, plan_id=_PLAN, connector="cm360",
        placement_id="3f0e0000-0000-4000-8000-00000000000a", actor="owner@example.com",
    )

    assert result["breakdown_value"] == "plc_EXAMPLE_feed"
    delete_sql, params = next(
        (sql, params) for sql, params in conn.statements
        if sql.strip().startswith("DELETE FROM app.plan_line_placement_mappings")
    )
    # A guessed UUID must not reach another Project's plan through this address.
    assert "plan_id = %s" in delete_sql and "connector = %s" in delete_sql
    assert params == ("3f0e0000-0000-4000-8000-00000000000a", _PLAN, "cm360")
    assert any("audit_log" in sql for sql, _ in conn.statements)


def test_detaching_something_that_is_not_there_is_a_typed_refusal() -> None:
    conn = _WriteConnection(attachment_exists=False)
    with pytest.raises(PlacementNotFoundError):
        detach_placement(
            conn, plan_id=_PLAN, connector="cm360",
            placement_id="3f0e0000-0000-4000-8000-0000000000ff", actor="owner@example.com",
        )


def test_the_scope_a_detach_changes_is_counted_before_the_change() -> None:
    """A confirmation naming the count AFTER the deletion names a scope nobody saw."""
    conn = _WriteConnection(sibling_count=3)
    assert count_placements_on_campaign(
        conn, plan_id=_PLAN, connector="cm360",
        line_key="line_display_q3", campaign_ref="camp_EXAMPLE_1",
    ) == 3
    # No delete has run: this is a read taken before the decision is offered.
    assert not any("DELETE" in sql for sql, _ in conn.statements)


# ---------------------------------------------------------------------------
# The guard on the guards.
# ---------------------------------------------------------------------------


def test_the_fixture_really_carries_an_unmatched_line_and_a_second_plan() -> None:
    """Without these, four assertions above would pass on a degenerate fixture."""
    assert len(_LINES) == 3
    matched = {mapping[0] for mapping in _MAPPINGS}
    assert len({line[0] for line in _LINES} - matched) == 2
    assert len(_UNMAPPED["unmapped"]) == 2
    assert len({row["connector"] for row in _UNMAPPED["unmapped"]}) == 2
    # Story 61.2: the acceptance fixture really names a row the panel carries, or
    # `test_an_accepted_spend_row_is_still_listed…` would pass on a decision
    # attached to nothing.
    assert {decision[0] for decision in _DECISIONS} <= {
        row["campaign_ref"] for row in _UNMAPPED["unmapped"]
    }


# ---------------------------------------------------------------------------
# Story 61.3 -- HOW a match was obtained, and the fourth state it makes possible.
# ---------------------------------------------------------------------------


def test_each_match_says_how_it_was_obtained_with_the_word_a_person_reads() -> None:
    """The second axis, on the campaign row, in the closed vocabulary of 27.4.

    Until this story a match was a fact with no history -- `campaign_ref`,
    `split_weight`, `status` -- and an equality of codes was indistinguishable
    from a 0.89 resemblance, though the pacing built on both does not deserve the
    same confidence.
    """
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)

    campaign = evidence["lines"][0]["campaigns"][0]
    assert campaign["match_method"] == "similarity"
    assert campaign["match_method_label"] == MATCH_METHOD_LABELS["similarity"]
    # THE WORD THE PLAN USES AND THE CODE DOES NOT KEEP. `similarity` is difflib
    # at 0.88; calling it an intelligence is a promise nothing behind it honours.
    assert "AI" not in repr(evidence)


def test_a_score_travels_for_similarity_alone() -> None:
    """`exact` and `normalized` are 1.0 BY CONSTRUCTION, and 1.0 is not a measure.

    Printing it beside a real 0.89 would invite a comparison between a tautology
    and a measurement. `manual` has no score at all -- migration 246 refuses the
    pair in the database too.
    """
    conn = _Connection(state="ready")
    evidence, _, _ = _read(conn)
    assert evidence["lines"][0]["campaigns"][0]["match_score"] == 0.91

    conn = _Connection(
        state="ready",
        mappings=[
            ("line_display_q3", "camp_EXAMPLE_1", "1.000000", "active", "exact", 1.0),
            ("line_video_q3", "camp_EXAMPLE_2", "1.000000", "active", "normalized", 1.0),
            ("line_search_q3", "camp_EXAMPLE_3", "1.000000", "active", "manual", None),
        ],
        placements=[],
    )
    evidence, _, _ = _read(conn)
    for line in evidence["lines"]:
        assert line["campaigns"][0]["match_score"] is None, line["line_key"]
    assert [line["campaigns"][0]["match_method_label"] for line in evidence["lines"]] == [
        MATCH_METHOD_LABELS["exact"],
        MATCH_METHOD_LABELS["normalized"],
        MATCH_METHOD_LABELS["manual"],
    ]


def test_a_match_older_than_the_columns_is_an_absence_and_never_manual() -> None:
    """The 49 matches both databases carried were written before migration 246.

    Rendering them `Matched by hand` would state that a person typed them, which
    nothing measured. They say the level is not known, and the payload carries the
    reason once rather than on every row.
    """
    conn = _Connection(
        state="ready",
        mappings=[("line_display_q3", "camp_EXAMPLE_1", "1.000000", "active", None, None)],
        placements=[],
    )
    evidence, _, _ = _read(conn)

    campaign = evidence["lines"][0]["campaigns"][0]
    assert campaign["match_method"] is None
    assert campaign["match_method_label"] == MATCH_METHOD_UNRECORDED_LABEL
    assert campaign["match_score"] is None
    assert campaign["match_method_label"] != MATCH_METHOD_LABELS["manual"]
    assert evidence["match_level_unrecorded_reason"] == MATCH_METHOD_UNRECORDED_REASON


def test_a_database_word_nobody_named_renders_as_nothing() -> None:
    """A fifth level would leak onto a screen as itself. It renders as an absence."""
    conn = _Connection(
        state="ready",
        mappings=[("line_display_q3", "camp_EXAMPLE_1", "1.000000", "active", "psychic", 0.4)],
        placements=[],
    )
    evidence, _, _ = _read(conn)

    campaign = evidence["lines"][0]["campaigns"][0]
    assert campaign["match_method_label"] is None


# ---------------------------------------------------------------------------
# Story 61.3 -- the suggestion reading, and the ambiguity it finally computes.
# ---------------------------------------------------------------------------


def _suggestion(line_key, campaign_ref, method="similarity", score=0.93, connector="cm360"):
    return {
        "line_key": line_key,
        "label": line_key,
        "connector": connector,
        "campaign_ref": campaign_ref,
        "method": method,
        "score": score,
        "normalized_form": line_key,
    }


def _computed(suggestions, *, notes=None):
    return {
        "plan_id": _PLAN,
        "project_id": _PROJECT,
        "connector": "cm360",
        "window": {"start": "2026-07-01", "end": "2026-09-30"},
        "similarity_threshold": 0.88,
        "suggestions": list(suggestions),
        "payload_by_line_key": {},
        "notes": list(notes or []),
    }


def _read_suggestions(conn, *, computed=None, connector="cm360", belongs=True, raises=None):
    calls: list[tuple] = []

    def _suggest_fn(plan_id, scope):
        if raises is not None:
            raise raises
        calls.append((plan_id, scope))
        return computed if computed is not None else _computed([])

    _suggest_fn.calls = calls
    with patch("core.plan_spend_decisions.plan_belongs_to_project", return_value=belongs):
        evidence = read_placement_suggestions(
            conn,
            project_id=_PROJECT,
            datastream_id=_DATASTREAM,
            connector=connector,
            plan_id=_PLAN,
            suggest_fn=_suggest_fn,
        )
    return evidence, _suggest_fn, calls


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", None])
def test_a_suggestion_reading_refuses_before_it_computes_anything(state) -> None:
    """The capability decides FIRST here too, or the tab and its engine disagree."""
    conn = _Connection(state=state)
    evidence, _, calls = _read_suggestions(conn)

    assert evidence["state"] == STATE_CAPABILITY_INACTIVE
    assert evidence["lines"] is None
    assert calls == []
    assert len(conn.statements) == 1


def test_a_plan_of_another_project_is_not_found_from_this_datastream() -> None:
    """`plan_id` arrives from a query string, and a plan is the Project's or it is not.

    Refused BEFORE the engine runs: a sweep on another Project's plan would carry
    its line labels back even if the payload were discarded.
    """
    conn = _Connection(state="ready")
    evidence, _, calls = _read_suggestions(conn, belongs=False)

    assert evidence["state"] == STATE_NO_PLAN
    assert evidence["lines"] is None
    assert calls == []


def test_the_engine_is_called_with_this_connector_and_no_other() -> None:
    """Arbitrage A7: the whole reach of this surface is one connector."""
    conn = _Connection(state="ready")
    _, _, calls = _read_suggestions(conn)

    assert calls == [(_PLAN, "cm360")]


def test_a_candidate_of_another_connector_never_reaches_the_payload() -> None:
    """The filter is the engine's, and this reading does not undo it.

    The fixture hands back a candidate the scoped engine could not have produced;
    it is the one shape that would prove the filter is only decorative.
    """
    conn = _Connection(state="ready")
    evidence, _, _ = _read_suggestions(
        conn,
        computed=_computed(
            [
                _suggestion("line_video_q3", "camp_EXAMPLE_2"),
                _suggestion("line_video_q3", "camp_OTHER_1", connector="meta-ads"),
            ]
        ),
    )

    connectors = {
        candidate["connector"]
        for line in evidence["lines"]
        for candidate in line["candidates"]
    }
    assert connectors == {"cm360"}


def test_two_candidates_on_an_unmatched_line_is_the_fourth_state() -> None:
    """`ambiguous`, delivered -- with the candidates NAMED in the same payload.

    Acceptance 6: a badge whose candidates are not beside it is a decoration, and
    that is why story 61.2 refused to draw one.
    """
    conn = _Connection(state="ready")
    evidence, _, _ = _read_suggestions(
        conn,
        computed=_computed(
            [
                _suggestion("line_video_q3", "camp_EXAMPLE_2", score=0.93),
                _suggestion("line_video_q3", "camp_EXAMPLE_3", score=0.9),
            ]
        ),
    )

    line = next(line for line in evidence["lines"] if line["line_key"] == "line_video_q3")
    assert line["matching_state"] == MATCHING_STATE_AMBIGUOUS
    assert line["matching_state_label"] == "To arbitrate"
    assert [candidate["campaign_ref"] for candidate in line["candidates"]] == [
        "camp_EXAMPLE_2",
        "camp_EXAMPLE_3",
    ]
    assert evidence["counts"]["lines_to_arbitrate"] == 1
    assert evidence["ambiguity"]["available"] is True


def test_one_candidate_is_a_proposal_and_a_matched_line_is_not_an_arbitration() -> None:
    """Two ways the word would be cried wolf, and neither is an ambiguity."""
    conn = _Connection(state="ready")
    evidence, _, _ = _read_suggestions(
        conn,
        computed=_computed(
            [
                # One candidate on a line nothing ventilates: a proposal.
                _suggestion("line_video_q3", "camp_EXAMPLE_2"),
                # Two candidates on a line ALREADY matched: somebody arbitrated.
                _suggestion("line_display_q3", "camp_EXAMPLE_4"),
                _suggestion("line_display_q3", "camp_EXAMPLE_5"),
            ]
        ),
    )

    by_key = {line["line_key"]: line for line in evidence["lines"]}
    assert by_key["line_video_q3"]["matching_state"] == MATCHING_STATE_UNMATCHED
    assert by_key["line_display_q3"]["matching_state"] == MATCHING_STATE_MATCHED
    assert evidence["counts"]["lines_to_arbitrate"] == 0


def test_a_campaign_two_lines_claim_is_the_other_side_of_the_ambiguity() -> None:
    """Arbitrage A9, and the amendment this story writes.

    The engine is N:M in both directions, so this set is computed every time the
    other one is. Naming only the line side would have hidden half of the
    arbitrations behind a reading that looked complete.
    """
    conn = _Connection(state="ready")
    evidence, _, _ = _read_suggestions(
        conn,
        computed=_computed(
            [
                _suggestion("line_video_q3", "camp_EXAMPLE_2"),
                _suggestion("line_search_q3", "camp_EXAMPLE_2"),
            ]
        ),
    )

    candidates = [
        candidate for line in evidence["lines"] for candidate in line["candidates"]
    ]
    assert {candidate["campaign_state"] for candidate in candidates} == {
        MATCHING_STATE_AMBIGUOUS
    }
    assert candidates[0]["claimed_by_line_keys"] == ["line_search_q3", "line_video_q3"]
    assert candidates[0]["campaign_state_label"] == "Claimed by several plan lines"
    assert evidence["counts"]["contested_campaigns"] == 1
    # A campaign one line alone claims is not in a state: it is simply not
    # contested, and a reassurance nobody needs is not printed.
    evidence, _, _ = _read_suggestions(
        conn, computed=_computed([_suggestion("line_video_q3", "camp_EXAMPLE_2")])
    )
    only = evidence["lines"][1]["candidates"][0]
    assert only["campaign_state"] is None and only["campaign_state_label"] is None


def test_a_candidate_carries_its_level_and_a_score_only_for_similarity() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read_suggestions(
        conn,
        computed=_computed(
            [
                _suggestion("line_video_q3", "camp_EXAMPLE_2", method="exact", score=1.0),
                _suggestion("line_search_q3", "camp_EXAMPLE_3", method="similarity", score=0.9),
            ]
        ),
    )

    by_key = {line["line_key"]: line for line in evidence["lines"]}
    exact = by_key["line_video_q3"]["candidates"][0]
    similar = by_key["line_search_q3"]["candidates"][0]
    assert exact["match_method_label"] == MATCH_METHOD_LABELS["exact"]
    assert exact["match_score"] is None
    assert similar["match_method_label"] == MATCH_METHOD_LABELS["similarity"]
    assert similar["match_score"] == 0.9
    # Nothing on this payload lets a caller state a level: the confirmation
    # re-derives it, and a ready-made entry to post back would be the way around.
    assert "payload_by_line_key" not in evidence
    assert "entry" not in exact


def test_the_reading_says_it_writes_nothing_and_over_what_it_looked() -> None:
    conn = _Connection(state="ready")
    evidence, _, _ = _read_suggestions(conn)

    assert evidence["writes"]["any"] is False
    assert "writes nothing" in evidence["writes"]["reason"]
    # AN EMPTY ANSWER IS A MEASUREMENT: the threshold and the window say how close
    # things had to be and over what we looked.
    assert evidence["empty_message"] == NO_CANDIDATE_MESSAGE
    assert evidence["similarity_threshold"] == 0.88
    assert evidence["window"] == {"start": "2026-07-01", "end": "2026-09-30"}
    assert evidence["computed_at"] is not None
    # And it shares no meaningful word with the broken sentence: "there is nothing"
    # and "we could not look" are two different reports.
    stop = {"a", "an", "the", "of", "this", "to", "no", "not", "be", "could"}
    empty_words = set(NO_CANDIDATE_MESSAGE.lower().split()) - stop
    broken_words = set(PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE.lower().split()) - stop
    assert not (empty_words & broken_words)


def test_an_unreachable_mart_is_a_refusal_and_never_an_empty_candidate_list() -> None:
    """An empty list is the exact shape of "nothing resembles anything"."""
    from core.warehouse import WarehouseUnavailable

    conn = _Connection(state="ready")
    with pytest.raises(PlacementEvidenceUnavailable):
        _read_suggestions(conn, raises=WarehouseUnavailable("down"))


def test_the_count_a_confirmation_replaces_is_taken_across_every_connector() -> None:
    """`set_line_mappings` replaces a line's WHOLE set, not this connector's share.

    A line matched to two campaigns would otherwise be announced as carrying one,
    and the confirmation would name a scope nobody was asked about.
    """
    conn = _Connection(
        state="ready",
        mappings=[
            ("line_display_q3", "camp_EXAMPLE_1", "1.000000", "active", "exact", 1.0),
            ("line_display_q3", "camp_EXAMPLE_7", "1.000000", "active", None, None),
        ],
        placements=[],
    )
    evidence, _, _ = _read_suggestions(conn)

    line = next(line for line in evidence["lines"] if line["line_key"] == "line_display_q3")
    assert line["matches_today"] == 2


# ---------------------------------------------------------------------------
# Story 61.3 -- the confirmation, which is the only writer.
# ---------------------------------------------------------------------------


class _ConfirmConnection:
    """A connection that records what `confirm_placement_match` asks of the store."""

    def __init__(self, *, existing: list[dict]) -> None:
        self.existing = existing
        self.written: list[dict] | None = None
        self.actor: str | None = None

    def cursor(self):  # pragma: no cover -- the store itself is doubled below
        raise AssertionError("confirm_placement_match must go through the store")


def _confirm(conn, *, suggestions, campaign_ref="camp_EXAMPLE_2", line_key="line_video_q3"):
    def _suggest_fn(plan_id, scope):
        return _computed(suggestions)

    def _list_mappings(_conn, *, plan_id):
        return {"plan_id": plan_id, "lines": [{"line_key": line_key, "mappings": conn.existing}]}

    def _set_line_mappings(_conn, *, plan_id, line_key, entries, actor):
        conn.written = entries
        conn.actor = actor
        return {"plan_id": plan_id, "line_key": line_key, "mappings": entries}

    with (
        patch("core.mediaplan_mapping.list_mappings", side_effect=_list_mappings),
        patch("core.mediaplan_mapping.set_line_mappings", side_effect=_set_line_mappings),
    ):
        return confirm_placement_match(
            conn,
            connector="cm360",
            plan_id=_PLAN,
            line_key=line_key,
            campaign_ref=campaign_ref,
            actor="owner@example.com",
            suggest_fn=_suggest_fn,
        )


def test_a_confirmation_writes_the_level_the_engine_computed_not_the_caller_s() -> None:
    """The guard that keeps the number beside `Name similarity` worth reading.

    The body of the route names a plan, a line and a campaign, and no level at
    all: a caller able to state one could write `Exact code` over a 0.89
    resemblance.
    """
    conn = _ConfirmConnection(existing=[])
    result = _confirm(
        conn,
        suggestions=[
            _suggestion("line_video_q3", "camp_EXAMPLE_2", method="similarity", score=0.9)
        ],
    )

    assert conn.written == [
        {
            "connector": "cm360",
            "campaign_ref": "camp_EXAMPLE_2",
            "match_method": "similarity",
            "match_score": 0.9,
        }
    ]
    assert conn.actor == "owner@example.com"
    assert result["match_method_label"] == MATCH_METHOD_LABELS["similarity"]
    assert result["match_score"] == 0.9
    assert result["replaced"] == 0


def test_a_campaign_the_engine_never_proposed_is_refused_with_its_own_word() -> None:
    conn = _ConfirmConnection(existing=[])
    with pytest.raises(PlacementMatchNotProposed):
        _confirm(
            conn,
            suggestions=[_suggestion("line_video_q3", "camp_EXAMPLE_2")],
            campaign_ref="camp_INVENTED",
        )
    assert conn.written is None


def test_a_confirmation_re_sends_the_neighbours_with_their_own_levels() -> None:
    """A6 -- the level survives the delete+insert, or it dies where a person validates.

    A neighbour whose level was never recorded is re-sent as UNKNOWN, never as
    `manual`: rewriting it would claim somebody typed a row nobody can name.
    """
    conn = _ConfirmConnection(
        existing=[
            {
                "connector": "cm360",
                "campaign_ref": "camp_EXAMPLE_1",
                "split_weight": "0.500000",
                "match_method": "exact",
                "match_score": 1.0,
            },
            {
                "connector": "meta-ads",
                "campaign_ref": "camp_EXAMPLE_9",
                "split_weight": "1.000000",
                "match_method": None,
                "match_score": None,
            },
        ]
    )
    result = _confirm(
        conn,
        suggestions=[
            _suggestion("line_video_q3", "camp_EXAMPLE_2", method="normalized", score=1.0)
        ],
    )

    assert conn.written[0]["match_method"] == "exact"
    assert conn.written[0]["split_weight"] == "0.500000"
    # `None` and NOT `manual`, and the key is PRESENT so the store can tell
    # "unknown" from "nobody said anything, so a person typed it".
    assert "match_method" in conn.written[1] and conn.written[1]["match_method"] is None
    # A match of ANOTHER connector is re-sent too: the store replaces the line's
    # whole set, so dropping it here would silently detach it.
    assert conn.written[1]["connector"] == "meta-ads"
    assert conn.written[-1]["campaign_ref"] == "camp_EXAMPLE_2"
    # The count named to a person is the one taken BEFORE the write.
    assert result["replaced"] == 2


# ---------------------------------------------------------------------------
# STORY 61.4 -- EVERY AMOUNT ON THIS TAB SAYS WHICH CURRENCY IT IS IN.
#
# Measured before: `grep -n "currency" WorkbenchPlacementsPage.tsx` returned 0,
# while the tab drew two amounts of two different scales side by side -- a line
# budget exact to the cent in `media_plans.currency`, and a campaign spend the
# warehouse had already converted into the Project's currency. `selected_plan`
# carried the plan's currency the whole time and nothing read it.
# ---------------------------------------------------------------------------


def test_the_plan_currency_reaches_the_payload_and_so_does_the_spend_currency() -> None:
    """Both amounts the tab draws, each with the currency that produced it."""
    evidence, _unmapped_fn, _observed_fn = _read(_Connection(state="ready"))

    assert evidence["selected_plan"]["currency"] == "EUR"
    money = evidence["money"]
    assert money["plan_currency"] == "EUR"
    # The spend is `fact_daily_kpi.value`, converted into the PROJECT's currency.
    # That is the only currency that may label it -- never the plan's.
    assert money["spend_currency"] == "EUR"
    assert money["comparable"] is True


def test_a_plan_in_another_currency_is_refused_and_both_currencies_are_named() -> None:
    """The defect this story repairs, at the tab's own grain.

    Before 61.4 this plan's budget would have been drawn beside this connector's
    spend with no currency on either, and the pacing marts behind them would have
    served a figure composed of both and labelled `USD`.
    """
    conn = _Connection(
        state="ready",
        plans=[(_PLAN, "Brand Q3 US", "USD", None)],
        canonical_currency="EUR",
    )
    evidence, _unmapped_fn, _observed_fn = _read(conn)

    money = evidence["money"]
    assert money["state"] == "plan_currency_mismatch"
    assert money["comparable"] is False
    assert "USD" in money["message"] and "EUR" in money["message"]


def test_a_project_that_never_chose_a_currency_labels_no_observed_amount() -> None:
    """`canonical_currency` NULL is "nobody chose", not "euros by omission"."""
    conn = _Connection(state="ready", canonical_currency=None)
    evidence, _unmapped_fn, _observed_fn = _read(conn, money_policy_currency=None)

    assert evidence["money"]["spend_currency"] is None
    assert evidence["money"]["state"] == "reporting_currency_unresolved"


def test_the_money_frame_is_present_even_when_no_plan_has_been_imported() -> None:
    """The Project's currencies are a fact with or without a plan.

    And the door to Analyze is one too: a Project with no media plan is exactly
    when somebody needs to be told where the reading would live.
    """
    conn = _Connection(state="ready", plans=[])
    evidence, _unmapped_fn, _observed_fn = _read(conn)

    assert evidence["state"] == STATE_NO_PLAN
    assert evidence["money"]["plan_currency"] is None
    assert evidence["money"]["analyze_reference"]["workspace"] == "analyze"


def test_an_inactive_capability_carries_no_money_frame_and_reads_nothing() -> None:
    """The capability still decides FIRST, and 61.4 did not open a way around it."""
    conn = _Connection(state="disabled")
    evidence, _unmapped_fn, _observed_fn = _read(conn)

    assert evidence["state"] == STATE_CAPABILITY_INACTIVE
    assert evidence["money"] is None
    # Not one statement beyond the capability switch: no currency read either.
    assert all("project_preferences" not in sql for sql in conn.statements)


def test_the_connector_slice_is_still_counted_and_still_says_when_it_cannot() -> None:
    """A5 -- the reading names how many Datastreams share the connector's slice.

    Unchanged by 61.4 and asserted here beside the currency work, because the two
    are the same promise: a figure says what it is about, or it is not drawn.
    """
    evidence, _u, _o = _read(_Connection(state="ready", datastreams_on_connector=3))
    assert evidence["grain"]["datastreams_on_connector"] == 3

    unreadable = _Connection(state="ready", count_raises=RuntimeError("no"))
    evidence, _u, _o = _read(unreadable)
    # `None`, never `0` -- nobody counted.
    assert evidence["grain"]["datastreams_on_connector"] is None


def test_the_two_empty_sentences_and_the_broken_one_share_no_word() -> None:
    """61.1's rule, re-measured with the sentences 61.4 adds beside them.

    "There is nothing" and "we could not look" must never be rendered for one
    another, and neither may borrow a word from the money refusal.
    """
    def words(sentence: str) -> set[str]:
        return {
            word.strip(".,:'\"-").lower()
            for word in sentence.split()
            if len(word.strip(".,:'\"-")) > 3
        }

    broken = words(PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE)
    for empty in (EMPTY_NO_PLAN_MESSAGE, EMPTY_NO_LINE_MESSAGE, NO_UNMATCHED_SPEND_MESSAGE):
        assert not (broken & words(empty)), (broken & words(empty), empty)


# ---------------------------------------------------------------------------
# AI-268 -- a plan with no currency is not an aligned plan.
# ---------------------------------------------------------------------------


class _MoneyConn:
    """The two reads `_read_money_frame` performs, and nothing else.

    `canonical_currency` comes from a cursor; the Money Policy comes from
    `resolve_money_policy`, which is patched per test rather than faked here --
    a double that reimplements a resolver measures the double.
    """

    def __init__(self, *, conversion: str | None, reporting: str | None = None) -> None:
        self._conversion = conversion
        self._reporting = reporting

    def cursor(self):
        return _MoneyCursor(self._conversion)


class _MoneyCursor:
    def __init__(self, conversion: str | None) -> None:
        self._conversion = conversion

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _sql, _params=None):
        return None

    def fetchone(self):
        return (self._conversion,) if self._conversion is not None else None


def _NoPolicy(*_a, **_k):
    """No confirmed Money Policy -- the gap `money_policy` raises, not a None."""
    from core.money_policy import PolicyGap

    raise PolicyGap(
        "money_policy_unconfirmed",
        "No Money Policy is confirmed for this Project.",
        capability="money",
    )



def test_a_plan_with_no_currency_never_reads_as_aligned() -> None:
    """`aligned` is a statement ABOUT a plan; composing it from a missing one lies.

    The chain used to fall through to `aligned` whenever the plan carried no
    currency, and the message then read "This plan's budget and this connector's
    spend are both in no currency, so the two can be read against each other" --
    agreement asserted between one amount and nothing.
    """
    from core import datastream_workbench_placements as placements

    with patch("core.money_policy.resolve_money_policy", side_effect=_NoPolicy):
        frame = placements._read_money_frame(
            _MoneyConn(conversion="EUR"),
            project_id="proj_EXAMPLE",
            plan={"currency": None},
        )
    assert frame["state"] == placements.MONEY_STATE_NO_PLAN_CURRENCY
    assert frame["comparable"] is False
    # The sentence names the gesture, and never claims the two agree.
    assert "names no currency" in frame["message"]
    assert "can be read against each other" not in frame["message"]
    # The SPEND stays labelled: it is the plan side that is missing.
    assert frame["spend_currency"] == "EUR"


def test_a_plan_that_agrees_still_reads_as_aligned() -> None:
    """The other half: the new branch must not swallow the real aligned case."""
    from core import datastream_workbench_placements as placements

    policy = SimpleNamespace(reporting_currency="EUR", version_id="pcv_EXAMPLE")
    with patch("core.money_policy.resolve_money_policy", return_value=policy):
        frame = placements._read_money_frame(
            _MoneyConn(conversion="EUR", reporting="EUR"),
            project_id="proj_EXAMPLE",
            plan={"currency": "EUR"},
        )
    assert frame["state"] == placements.MONEY_STATE_ALIGNED
    assert frame["comparable"] is True
