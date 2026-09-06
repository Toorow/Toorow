"""The day-grain read of a Datastream -- story 58.1, epic 58.

In-memory doubles, on the pattern of `tests/core/test_extract_ledger.py`: what is
proved here is the ARITHMETIC and the REFUSALS. That the address exists, that the
guard is called with the stream id and that a real `app.pull_jobs` row reaches the
payload are proved against Postgres in
`tests/integration/test_datastream_daily_breakdown_api.py` -- a mocked cursor
answers whatever the fixture felt like and cannot prove a route is mounted.

THE FIVE THINGS THIS FILE EXISTS TO HOLD:

  * a day nothing covered says `never_fetched`, and never `0`;
  * a day somebody STOPPED does not read as a day nobody asked for (arbitrage 7);
  * a window's row total is not published as a day's (arbitrage 9);
  * the header and the rows are two blocks, and the response SAYS they do not
    join -- a pairing invented on matching names would be a join that does not
    exist, served as a fact;
  * each refusal is its own code, its own status and its own sentence.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core import pull_job_states
from core.datastream_daily_breakdown_api import (
    COLUMN_ROW_JOIN_REASON,
    COLUMNS_FROM_FLAT_TABLE,
    COLUMNS_FROM_MAPPING_VERSION,
    COLUMNS_NO_MAPPING,
    COUNTRY_CAPABILITY_NOT_ACTIVE,
    COUNTRY_CONNECTOR_REPORTS_NONE,
    COUNTRY_NO_VALUE_IN_WINDOW,
    DAILY_BREAKDOWN_SCHEMA,
    DQ_NO_MONITOR,
    MAX_COUNTRY_VALUES_PER_DAY,
    MAX_WINDOW_DAYS,
    REASON_NO_RUN_IN_WINDOW,
    ROWS_AMBIGUOUS_MATERIALIZATION,
    ROWS_CONNECTOR_NOT_IN_MART,
    ROWS_WAREHOUSE_UNAVAILABLE,
    DatastreamNotFound,
    RequestRefused,
    _read_daily_breakdown,
    parse_window,
    read_daily_breakdown,
    resolve_view_mode,
)
from core.extract_ledger import (
    PROVENANCE_PRE_8_2,
    ROW_COUNT_MEASURED_PER_WINDOW,
    ROW_COUNT_NOT_VERIFIED,
)

_NOW = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)

#: The column list of the ledger's batch query, in its own SQL order.
_PULL_COLS = [
    "job_id", "pull_id", "datastream_id", "connection_ref_id",
    "date_from", "date_to", "state", "execution_id", "completed_at", "enqueued_at",
    "error_detail",
    "verdict", "actual_rows", "expected_rows", "completeness_ratio",
]


def _pull(
    *,
    pull_id="pull_001",
    datastream_id="ds_001",
    date_from="2026-07-10",
    date_to="2026-07-10",
    state=pull_job_states.DONE,
    execution_id="dse_001",
    error_detail=None,
    verdict="ok",
    actual_rows=150,
    expected_rows=150,
    completeness_ratio=1.0,
):
    return (
        "job_001", pull_id, datastream_id, "conn_001",
        date_from, date_to, state, execution_id, _NOW, _NOW,
        error_detail,
        verdict, actual_rows, expected_rows, completeness_ratio,
    )


class _Cursor:
    """A cursor that answers by the SHAPE of the statement it is given.

    Keyed on the statement rather than on call order: the reads of this route are
    reordered every time a block moves, and a fixture that counts calls turns a
    refactor into a red test about nothing.
    """

    def __init__(self, state: dict) -> None:
        self._state = state
        self._rows: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self._state["statements"].append(text)
        if "d.module_name" in text:
            self._rows = self._state["facts"]
            self.description = None
        elif "COUNT(*) FROM app.datastreams" in text:
            self._rows = [(self._state["same_connector"],)]
        elif "v.mapping_payload" in text:
            payload = self._state["mapping_version"]
            self._rows = [(payload,)] if payload is not None else []
        elif "canonical_name" in text:
            wanted = set(params[0]) if params else set()
            self._rows = [
                (identity, name)
                for identity, name in self._state["mdm_names"].items()
                if identity in wanted
            ]
        elif "m.source_field" in text:
            self._rows = self._state["columns"]
        elif "v.value_type = 'money'" in text:
            # Story 58.7. The ONE authority allowed to say "this column is an
            # amount", and the default below is the measured estate: it answers
            # nothing on every project of this product.
            self._rows = [(name,) for name in self._state["monetary_concepts"]]
        elif "capability_key = ANY" in text:
            # Lot B3. The route reads every capability it projects in ONE
            # statement; the singular read below is the one `country_split` still
            # falls back to when nobody hands it a state.
            wanted = list(params[1]) if params else []
            states = self._state["capability_states"]
            self._rows = [
                (key, states[key], "optional")
                for key in wanted
                if states.get(key) is not None
            ]
        elif "app.project_capabilities" in text:
            state = self._state["country_capability_state"]
            self._rows = [(state,)] if state is not None else []
        elif "SELECT connection_ref_id FROM app.datastreams" in text:
            self._rows = [("conn_001",)]
        elif "FROM app.pull_jobs pj" in text:
            self._rows = list(self._state["pulls"])
            self.description = [(c,) for c in _PULL_COLS]
        elif "FROM app.pull_jobs j" in text:
            self._rows = [(1,)] if self._state["ever_collected"] else []
        else:  # pragma: no cover -- an unmodelled read must be loud
            raise AssertionError(f"unexpected statement: {text}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _conn(
    *,
    pulls=(),
    columns=(("spend", "cost", False),),
    mapping_version=None,
    mdm_names=None,
    facts=(("meta-ads", "dsp_1", "dmap_1", "campaign_daily"),),
    same_connector=1,
    ever_collected=True,
    # THE FIXTURE FABRICATES THE ON STATE, AND IT SAYS SO -- story 58.5.
    # `country` is `disabled` on 1891 projects out of 1891 (measured on the
    # disposable cluster 2026-08-07), so no project of this estate exercises the
    # branch that SHOWS the split. The default here is the measured reality; a test
    # that wants the column has to ask for a state no real project holds.
    country_capability_state="disabled",
    # LOT B3, AND THE DEFAULT IS THE MEASURED ESTATE AGAIN. Measured 2026-08-12 over
    # `app.project_capabilities`: 31 projects, 6 keys each, ZERO in `ready` or
    # `degraded` -- four `disabled`, `currency_fx` and `reporting_timezone` `draft`.
    # So NO capability projects anything onto a reading today. A test that wants an
    # effect on the data has to name a state no project of this product holds --
    # which is the point: the switch is read, never derived.
    currency_fx_capability_state="draft",
    reporting_timezone_capability_state="draft",
    # STORY 58.7, AND THE DEFAULT IS AGAIN THE MEASURED ESTATE. `select value_type,
    # count(*) from app.semantic_concept_versions group by 1` answers
    # `integer 6, decimal 3, string 3, date 1` -- ZERO `money` over 13 published
    # versions -- so no reading of this product designates an amount today. A test
    # that wants one has to declare it, which is the point: the authority is the
    # only thing that can.
    monetary_concepts=(),
):
    state = {
        "statements": [],
        "country_capability_state": country_capability_state,
        "capability_states": {
            "country": country_capability_state,
            "currency_fx": currency_fx_capability_state,
            "reporting_timezone": reporting_timezone_capability_state,
        },
        "monetary_concepts": list(monetary_concepts),
        "pulls": list(pulls),
        "columns": list(columns),
        # 328 of the 372 Datastreams keep their fields here and NOT in the flat
        # table; the default is `None` so every test written before story 58.2
        # keeps exercising the fallback it was written against.
        "mapping_version": mapping_version,
        "mdm_names": dict(mdm_names or {}),
        "facts": list(facts),
        "same_connector": same_connector,
        "ever_collected": ever_collected,
    }
    connection = MagicMock()
    connection.cursor.side_effect = lambda *a, **k: _Cursor(state)
    connection.statements = state["statements"]
    return connection


def _no_mart():
    """The warehouse answers "this connector is not in the mart" -- 23 of 39 are."""
    return patch(
        "core.cache_warehouse.read_daily_row_counts",
        return_value={"connector_present": False, "counts": {}},
    )


def _mart(counts):
    return patch(
        "core.cache_warehouse.read_daily_row_counts",
        return_value={"connector_present": True, "counts": counts},
    )


def _country(counts, *, dimension_present=True):
    """The country grouping of the mart -- story 58.5.

    A SECOND GROUPING OVER THE SAME RELATION, and `dimension_present` is the probe
    that keeps two different absences apart: a window that carried no country row,
    and a connector that has never carried one at all (5 of the 39 can).
    """
    return patch(
        "core.cache_warehouse.read_daily_country_counts",
        return_value={"dimension_present": dimension_present, "counts": counts},
    )


def _no_country():
    """The connector is in the mart and has never carried a country row."""
    return _country({}, dimension_present=False)


def _described(collected=None, mapped=None):
    """What the warehouse KNOWS of the two relations -- story 58.3, defect 6.

    The availability of `Collected` and `Mapped` is decided on this and never on
    the relation's name: 7 of the 52 declared relations carry no `date` column, so
    a verdict read off the manifest alone would enable a control that refuses once
    pressed. These tests are DB-free, so the description is the double -- the real
    one is executed against DuckDB in `tests/core/test_collected_mapped_reader.py`.
    """
    def _side(relation, over):
        base = {
            "relation": relation,
            "zone": "main",
            "prefix": "main.",
            "mode": "duckdb",
            "columns": ["project_id", "date"],
            "readable": True,
            "reason": None,
            "message": None,
        }
        base.update(over or {})
        return base

    return patch(
        "core.collected_mapped_reader.describe_pair",
        return_value={
            "collected": _side("raw_meta_ads_daily", collected),
            "mapped": _side("stg_meta_ads_daily", mapped),
        },
    )


def _undescribable():
    """The two sides as they come back for a flux whose pair does not resolve."""
    from core.collected_mapped_reader import _undeclared

    def _describe(*, project_id, pair):  # noqa: ARG001
        return {"collected": _undeclared(pair), "mapped": _undeclared(pair)}

    return patch("core.collected_mapped_reader.describe_pair", new=_describe)


def _breakdown(
    conn, *, start="2026-07-10", end="2026-07-10", span=None,
    view_mode="processed", day=None,
):
    return read_daily_breakdown(
        conn,
        project_id="proj_a",
        datastream_id="ds_001",
        start=start,
        end=end,
        span=span if span is not None else 1,
        view_mode=view_mode,
        day=day,
    )


# ---------------------------------------------------------------------------
# The window is mandatory, and its bound is said.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start,end,missing", [(None, "2026-07-10", "start"),
                                               ("2026-07-10", "", "end")])
def test_an_unbounded_window_names_the_parameter_it_lacks(start, end, missing) -> None:
    with pytest.raises(RequestRefused) as refused:
        parse_window(start, end)
    assert refused.value.code == "missing_param"
    assert refused.value.status == 400
    assert refused.value.extra["parameter"] == missing


def test_a_date_outside_the_iso_form_is_refused_by_name() -> None:
    with pytest.raises(RequestRefused) as refused:
        parse_window("2026/07/10", "2026-07-12")
    assert refused.value.code == "invalid_date"
    assert refused.value.extra["parameter"] == "start"


def test_a_window_wider_than_the_bound_says_the_bound() -> None:
    """A list that is silently narrowed reads as the whole answer."""
    with pytest.raises(RequestRefused) as refused:
        parse_window("2026-01-01", "2026-12-31")
    assert refused.value.code == "window_too_wide"
    assert refused.value.extra["bounded_at"] == MAX_WINDOW_DAYS
    assert refused.value.extra["requested_days"] == 365
    assert str(MAX_WINDOW_DAYS) in refused.value.message


def test_the_widest_accepted_window_is_exactly_the_bound() -> None:
    start, end, span = parse_window("2026-01-01", "2026-04-02")
    assert (start, end, span) == ("2026-01-01", "2026-04-02", MAX_WINDOW_DAYS)


def test_an_inverted_window_is_refused_rather_than_silently_emptied() -> None:
    with pytest.raises(RequestRefused) as refused:
        parse_window("2026-07-12", "2026-07-10")
    assert refused.value.code == "invalid_range"


def test_the_bound_travels_on_the_payload_whether_or_not_it_was_reached() -> None:
    with _no_mart():
        narrow = _breakdown(_conn(), span=1)
        wide = _breakdown(_conn(), start="2026-01-01", end="2026-04-02",
                          span=MAX_WINDOW_DAYS)
    assert narrow["window"]["bounded_at"] == MAX_WINDOW_DAYS
    assert narrow["window"]["bound_reached"] is False
    assert wide["window"]["bound_reached"] is True


# ---------------------------------------------------------------------------
# THE AVAILABILITY OF EVERY MODE IS ON THE `200`, WITH ITS REASON -- story 58.3,
# arbitrage 2.
#
# What changed and why: `resolve_view_mode` used to answer `422` for `collected`
# and `mapped` unconditionally, before a connection was opened. It cannot any
# more -- whether those two can be served is a fact about THIS Datastream, read
# from the relation its report profile declares. The vocabulary check stays here;
# the availability is decided once the stream's facts are known, and it travels on
# the `200` so a screen can grey a position WITH its reason before anyone clicks.
# ---------------------------------------------------------------------------


def test_the_vocabulary_is_the_four_ratified_stages_and_nothing_else() -> None:
    assert resolve_view_mode(None) == "processed"
    assert resolve_view_mode("collected") == "collected"
    assert resolve_view_mode(" mapped ") == "mapped"


def test_a_view_mode_that_is_not_a_stage_at_all_is_a_400() -> None:
    """`side_by_side` is a READING of the two stages, never a fifth stage."""
    with pytest.raises(RequestRefused) as refused:
        resolve_view_mode("side_by_side")
    assert refused.value.code == "invalid_stage"
    assert refused.value.status == 400


def test_the_two_hundred_carries_the_availability_of_the_four_modes() -> None:
    with _no_mart(), _described():
        payload = _breakdown(_conn())

    available = payload["view_mode"]["available"]
    assert [entry["mode"] for entry in available] == [
        "collected", "mapped", "processed", "published"
    ]
    assert {entry["mode"]: entry["available"] for entry in available} == {
        "collected": True, "mapped": True, "processed": True, "published": False,
    }
    # And each position names the relation it reads, so the screen shows an
    # address rather than a word.
    assert available[0]["relation"] == "raw_meta_ads_daily"
    assert available[1]["relation"] == "stg_meta_ads_daily"


def test_a_flux_with_no_report_profile_offers_the_two_modes_disabled_with_a_reason() -> None:
    """773 of the 842 live Datastreams. The greyed control has to say WHY."""
    from core.stage_relation_resolver import REPORT_PROFILE_NOT_SET, message_for

    with _no_mart():
        payload = _breakdown(_conn(facts=[("meta-ads", "dsp_1", "dmap_1", None)]))

    available = {entry["mode"]: entry for entry in payload["view_mode"]["available"]}
    assert available["collected"]["available"] is False
    assert available["mapped"]["available"] is False
    assert available["collected"]["reason"] == message_for(REPORT_PROFILE_NOT_SET)
    assert payload["stage_relations"]["reason"] == REPORT_PROFILE_NOT_SET


def test_the_published_mode_is_refused_for_every_flux_in_the_warehouses_words() -> None:
    from core.cache_warehouse import _sample_stage_note

    with _no_mart():
        payload = _breakdown(_conn())

    published = payload["view_mode"]["available"][3]
    assert published["available"] is False
    assert published["reason"] == _sample_stage_note("published")


@pytest.mark.parametrize("stage", ["collected", "mapped"])
def test_a_forced_call_on_a_mode_this_flux_cannot_serve_is_422_in_the_same_words(
    stage,
) -> None:
    """Two doors, ONE wording: the `422` quotes the sentence the greyed control had."""
    from core.stage_relation_resolver import message_for

    with _no_mart(), pytest.raises(RequestRefused) as refused:
        _breakdown(
            _conn(facts=[("meta-ads", "dsp_1", "dmap_1", None)]), view_mode=stage
        )
    assert refused.value.code == "stage_not_materialized"
    assert refused.value.status == 422
    assert refused.value.message == message_for("report_profile_not_set")
    assert refused.value.extra["available_view_modes"] == ["processed"]


def test_a_forced_call_on_a_mode_this_flux_can_serve_is_served(stage="collected") -> None:
    with _no_mart(), _described():
        payload = _breakdown(_conn(), view_mode=stage)
    assert payload["view_mode"]["requested"] == "collected"
    assert payload["view_mode"]["served"] == "collected"


def test_a_relation_with_no_date_column_greys_its_mode_BEFORE_the_click() -> None:
    """Defect measured on 14 report profiles over 7 connectors.

    `raw_x_ads_daily`, `raw_taboola_history`, `raw_strava_club_daily`,
    `raw_monday_board_snapshot`, `raw_gbp_review`,
    `raw_gbp_search_keyword_monthly` and `raw_linkedin_company_pages_daily` carry
    no `date` column. Reading availability off the manifest offered those an
    ENABLED `Collected` that refused once pressed -- the reason arriving after the
    click, which arbitrage 2 forbids in as many words.
    """
    from core.collected_mapped_reader import DATE_COLUMN_ABSENT, message_for

    refusal = {
        "readable": False,
        "reason": DATE_COLUMN_ABSENT,
        "message": message_for(DATE_COLUMN_ABSENT),
        "columns": ["project_id", "interval_start"],
    }
    with _no_mart(), _described(collected=refusal):
        payload = _breakdown(_conn())

    available = {entry["mode"]: entry for entry in payload["view_mode"]["available"]}
    assert available["collected"]["available"] is False
    assert available["collected"]["reason"] == message_for(DATE_COLUMN_ABSENT)
    # The ADDRESS is still published: the flux has one, it simply cannot be read
    # a day at a time. Two different facts, both said.
    assert available["collected"]["relation"] == "raw_meta_ads_daily"
    assert payload["stage_relations"]["collected_relation"] == "raw_meta_ads_daily"
    assert payload["stage_relations"]["reason"] is None


def test_that_same_flux_refuses_a_forced_collected_in_the_very_same_words() -> None:
    from core.collected_mapped_reader import DATE_COLUMN_ABSENT, message_for

    refusal = {
        "readable": False,
        "reason": DATE_COLUMN_ABSENT,
        "message": message_for(DATE_COLUMN_ABSENT),
        "columns": ["project_id", "interval_start"],
    }
    with _no_mart(), _described(collected=refusal), pytest.raises(RequestRefused) as refused:
        _breakdown(_conn(), view_mode="collected")
    assert refused.value.status == 422
    assert refused.value.message == message_for(DATE_COLUMN_ABSENT)
    assert refused.value.extra["available_view_modes"] == ["mapped", "processed"]


def test_the_pair_of_relations_travels_on_the_payload() -> None:
    with _no_mart():
        payload = _breakdown(_conn())
    assert payload["stage_relations"] == {
        "report_profile_id": "campaign_daily",
        "collected_relation": "raw_meta_ads_daily",
        "mapped_relation": "stg_meta_ads_daily",
        "reason": None,
        "message": None,
    }


# ---------------------------------------------------------------------------
# The days.
# ---------------------------------------------------------------------------


def test_one_row_per_date_ordered_ascending() -> None:
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(date_from="2026-07-10", date_to="2026-07-10")]),
            start="2026-07-10", end="2026-07-14", span=5,
        )
    assert payload["schema"] == DAILY_BREAKDOWN_SCHEMA
    assert [day["date"] for day in payload["days"]] == [
        "2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13", "2026-07-14"
    ]


def test_a_day_nothing_covered_is_never_fetched_and_never_a_zero() -> None:
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(date_from="2026-07-10", date_to="2026-07-10")]),
            start="2026-07-10", end="2026-07-11", span=2,
        )
    uncovered = payload["days"][1]
    assert uncovered["extract_status"] == pull_job_states.LEDGER_NEVER_FETCHED
    assert uncovered["row_count"] is None
    assert uncovered["extract_count"] == 0
    assert uncovered["pull_id"] is None


def test_a_stopped_day_does_not_read_as_a_day_nobody_asked_for() -> None:
    """Arbitrage 7. `cancelled` and `superseded` both report `never_fetched`.

    Without the window's own state on the row, a person who stopped a run at
    10:02 opens the strip at 10:03 and reads that the day was never requested.
    """
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(state=pull_job_states.CANCELLED, verdict=None,
                               actual_rows=None)])
        )
    day = payload["days"][0]
    assert day["extract_status"] == pull_job_states.LEDGER_NEVER_FETCHED
    assert day["job_state"] == pull_job_states.CANCELLED
    # And it is distinguishable from a day nothing ever covered.
    assert day["extract_count"] == 1
    assert day["pull_id"] == "pull_001"


def test_a_failed_day_carries_its_error_class_and_the_action_it_asks_for() -> None:
    detail = json.dumps({"error_class": "auth_expired", "user_action": "reconnect"})
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(state=pull_job_states.FAILED, verdict=None,
                               actual_rows=None, error_detail=detail)])
        )
    day = payload["days"][0]
    assert day["extract_status"] == pull_job_states.LEDGER_FAILED
    assert day["error_class"] == "auth_expired"
    assert day["user_action"] == "reconnect"


def test_a_day_that_is_not_failed_carries_no_error_pair_at_all() -> None:
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    assert "error_class" not in payload["days"][0]


def test_a_prevented_day_publishes_the_gesture_the_connector_named() -> None:
    """AI-307, the LAST metre: the sentence has to leave the server.

    The state, the row and the ledger all shipped before this route did, and the
    console still said « did not allow this collection » and named nothing to do:
    measured 2026-08-21, `grep -rn "prevented_message" ui/ web/` returned 0. The
    day grid reads THIS payload and nothing else, so if the pair stops here it
    stops everywhere a person looks.
    """
    detail = json.dumps({
        "prevented_reason": "reviews_access_pending",
        "prevented_message": (
            "Request the reviews allowlist for this project, then re-ask these dates."
        ),
    })
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(state=pull_job_states.PREVENTED, verdict=None,
                               actual_rows=None, error_detail=detail)])
        )
    day = payload["days"][0]
    # The day grain says the day was not fetched -- which is true and useless on
    # its own -- and the window says who refused and what releases it.
    assert day["extract_status"] == pull_job_states.LEDGER_NEVER_FETCHED
    assert day["job_state"] == pull_job_states.PREVENTED
    assert day["prevented_reason"] == "reviews_access_pending"
    assert day["prevented_message"].startswith("Request the reviews allowlist")
    # NEVER a count: a window that was not allowed to run took none.
    assert day["row_count"] is None


def test_a_day_that_was_not_prevented_carries_no_prevented_pair_at_all() -> None:
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    assert "prevented_reason" not in payload["days"][0]
    assert "prevented_message" not in payload["days"][0]


def test_the_run_that_produced_the_day_is_named() -> None:
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull(execution_id="dse_42")]))
    assert payload["days"][0]["execution_id"] == "dse_42"
    assert payload["days"][0]["loaded_at"] == _NOW.isoformat()


def test_a_day_served_by_the_pre_8_2_fallback_says_so_on_the_row() -> None:
    """Arbitrage 5: the branch stays, and the ambiguity is stated, not guessed.

    An orphan pull carries no `datastream_id`, so both Datastreams of one
    connection claim it. Switching the branch off would erase days that WERE
    collected; leaving it silent makes two streams show the same day as theirs
    with nothing to say which.
    """
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull(datastream_id=None)]))
    assert payload["days"][0]["provenance"] == PROVENANCE_PRE_8_2


def test_a_day_of_this_streams_own_pull_carries_no_provenance_caveat() -> None:
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull(datastream_id="ds_001")]))
    assert payload["days"][0]["provenance"] is None


# ---------------------------------------------------------------------------
# The volume of a day, which is the number that was wrong (arbitrage 9).
# ---------------------------------------------------------------------------


def test_a_single_day_pull_publishes_the_volume_it_measured() -> None:
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(date_from="2026-07-10", date_to="2026-07-10",
                               actual_rows=150)])
        )
    day = payload["days"][0]
    assert day["row_count"] == 150
    assert day["row_count_reason"] is None


def test_a_backfill_never_publishes_its_own_total_on_each_of_its_days() -> None:
    """The measured defect: 450 rows over three days were reported as 450 a day.

    `pull_verifications.actual_rows` is counted per `pull_id`, so it is the
    volume of the WINDOW. Copied onto each covered day it multiplied every
    backfill by its own width, on `/ledger` and on `CoverageStrip`.
    """
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(date_from="2026-07-10", date_to="2026-07-12",
                               actual_rows=450)]),
            start="2026-07-10", end="2026-07-12", span=3,
        )
    assert [day["row_count"] for day in payload["days"]] == [None, None, None]
    assert {day["row_count_reason"] for day in payload["days"]} == {
        ROW_COUNT_MEASURED_PER_WINDOW
    }
    # `expected_rows` and `completeness_ratio` are written per `pull_id` by the
    # SAME line of `verification.py` as `actual_rows`, so they carry the SAME
    # window grain. Silencing only `row_count` would leave each of these three
    # days saying `row_count: null` beside `expected_rows: 150` and a ratio of
    # 1.0 -- a window's expectation and a window's ratio read as a day's. The
    # whole measured trio goes silent together, under one reason.
    assert [day["expected_rows"] for day in payload["days"]] == [None, None, None]
    assert [day["completeness_ratio"] for day in payload["days"]] == [None, None, None]


def test_a_pull_with_no_verification_says_why_it_has_no_volume() -> None:
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull(verdict=None, actual_rows=None, expected_rows=None,
                               completeness_ratio=None)])
        )
    day = payload["days"][0]
    assert day["row_count"] is None
    assert day["row_count_reason"] == ROW_COUNT_NOT_VERIFIED


# ---------------------------------------------------------------------------
# The header, the rows, and the join that does not exist.
# ---------------------------------------------------------------------------


def test_the_header_comes_from_the_mapping_and_key_columns_come_first() -> None:
    with _no_mart():
        payload = _breakdown(
            _conn(columns=[("date", "date", True), ("spend", None, False)])
        )
    assert payload["columns"] == [
        {"source_field": "date", "target_field": "date", "is_key_column": True,
         "binding_status": None, "sensitivity": "unknown"},
        {"source_field": "spend", "target_field": None, "is_key_column": False,
         "binding_status": None, "sensitivity": "unknown"},
    ]
    assert payload["columns_reason"] is None
    assert payload["columns_source"] == COLUMNS_FROM_FLAT_TABLE


def test_a_versioned_flux_renders_its_fields_and_resolves_the_mdm_identity() -> None:
    """Arbitrage 2, and it is the majority case, not the exotic one.

    Measured on the disposable cluster: 44 Datastreams carry a flat row, 328
    carry a mapping VERSION, and none carries both. Reading only the flat store
    -- what story 58.1 shipped -- left 328 of 372 fluxes with an empty header
    and no way to tell that it was the reader and not the flux.

    `binding.mdm_target` is an identity (`mdm_<ULID>`), so it is resolved to its
    `canonical_name`: rendering the id would put an opaque string where a person
    reads a field name, and dropping it would report a bound field as unmapped.
    """
    with _no_mart():
        payload = _breakdown(
            _conn(
                columns=[],
                mapping_version={
                    "grain": ["media_date"],
                    "fields": [
                        {"field_id": "clicks",
                         "binding": {"status": "confirmed",
                                     "mdm_target": "mdm_" + "A" * 26}},
                        {"field_id": "media_date",
                         "binding": {"status": "confirmed",
                                     "mdm_target": "mdm_" + "B" * 26}},
                        # 105 of the 708 measured bindings name no target at
                        # all. That is a state of the mapping, and the field
                        # stays in the header -- it is the line a person opens
                        # this tab to repair.
                        {"field_id": "audience_label",
                         "binding": {"status": "suggested", "mdm_target": None}},
                    ],
                },
                mdm_names={
                    "mdm_" + "A" * 26: "clicks_total",
                    "mdm_" + "B" * 26: "media_date_98245b",
                },
            )
        )
    assert payload["columns"] == [
        # The grain field first: "key column" means the same thing in both
        # stores, and the versioned one states its grain rather than a flag.
        {"source_field": "media_date", "target_field": "media_date_98245b",
         "is_key_column": True, "binding_status": "confirmed", "sensitivity": "unknown"},
        {"source_field": "audience_label", "target_field": None,
         "is_key_column": False, "binding_status": "suggested", "sensitivity": "unknown"},
        {"source_field": "clicks", "target_field": "clicks_total",
         "is_key_column": False, "binding_status": "confirmed", "sensitivity": "unknown"},
    ]
    assert payload["columns_source"] == COLUMNS_FROM_MAPPING_VERSION
    assert payload["columns_reason"] is None


def test_the_identity_under_canonical_target_is_resolved_and_never_shown_raw() -> None:
    """105 of the 708 measured bindings put the identity under the OTHER key.

    `binding.mdm_target` is null on them and `binding.canonical_target` carries
    `mdm_<ULID>` -- and all 105 resolve in `app.mdm_canonical_fields`. Reading
    only the first key and falling back to the raw string is how the canonical
    pill of a field renders `Cout net -> mdm_6D13WZ6E18GSZTTEH1JPZBACYW`: an
    identity shown where a person reads a name.
    """
    identity = "mdm_" + "C" * 26
    with _no_mart():
        payload = _breakdown(
            _conn(
                columns=[],
                mapping_version={
                    "grain": [],
                    "fields": [
                        {"field_id": "net_cost",
                         "binding": {"status": "confirmed", "mdm_target": None,
                                     "canonical_target": identity}},
                    ],
                },
                mdm_names={identity: "net_media_cost_micros"},
            )
        )
    assert payload["columns"] == [
        {"source_field": "net_cost", "target_field": "net_media_cost_micros",
         "is_key_column": False, "binding_status": "confirmed", "sensitivity": "unknown"}
    ]


def test_an_identity_the_registry_cannot_resolve_is_unmapped_not_an_id() -> None:
    """The fallback that showed the raw identity made `Unmapped` unreachable.

    Measured on the versioned store: 0 columns out of 708 ever rendered
    `Unmapped`, because every unresolved binding fell through to its own opaque
    string. An absence that can never be displayed is an absence nobody repairs.
    """
    with _no_mart():
        payload = _breakdown(
            _conn(
                columns=[],
                mapping_version={
                    "grain": [],
                    "fields": [
                        {"field_id": "net_cost",
                         "binding": {"status": "suggested",
                                     "mdm_target": "mdm_" + "D" * 26}},
                    ],
                },
                mdm_names={},
            )
        )
    assert payload["columns"][0]["target_field"] is None


def test_a_canonical_name_that_is_not_an_identity_passes_through() -> None:
    """Older payloads carry a readable name there; it is not re-resolved."""
    conn = _conn(
        columns=[],
        mapping_version={
            "grain": [],
            "fields": [
                {"field_id": "spend_micros",
                 "binding": {"status": "bound", "mdm_target": None,
                             "canonical_target": "media_cost_micros"}},
            ],
        },
    )
    with _no_mart():
        payload = _breakdown(conn)
    assert payload["columns"][0]["target_field"] == "media_cost_micros"
    # And a readable name asks the MDM registry nothing.
    assert not any("canonical_name" in statement for statement in conn.statements)


def test_a_version_that_answers_makes_the_flat_table_unnecessary() -> None:
    """The fallback is a fallback: the two stores never both answer for one flux."""
    conn = _conn(
        columns=[("spend", "cost", False)],
        mapping_version={"grain": [], "fields": [{"field_id": "spend",
                                                  "binding": {"status": "confirmed"}}]},
    )
    with _no_mart():
        payload = _breakdown(conn)
    assert [column["source_field"] for column in payload["columns"]] == ["spend"]
    assert payload["columns_source"] == COLUMNS_FROM_MAPPING_VERSION
    assert not any("m.source_field" in statement for statement in conn.statements)


def test_a_version_with_no_binding_at_all_asks_the_mdm_registry_nothing() -> None:
    conn = _conn(
        columns=[],
        mapping_version={"grain": [], "fields": [{"field_id": "spend", "binding": {}}]},
    )
    with _no_mart():
        payload = _breakdown(conn)
    assert payload["columns"] == [
        {"source_field": "spend", "target_field": None, "is_key_column": False,
         "binding_status": None, "sensitivity": "unknown"}
    ]
    assert not any("canonical_name" in statement for statement in conn.statements)


def test_a_stream_with_no_mapping_gets_an_empty_header_and_its_reason() -> None:
    """Neither store answers -- and the payload says which one was believed.

    `columns_source: null` beside the reason is what tells a reader that BOTH
    were asked. Without it, an empty header and a header read from the wrong
    store render identically.
    """
    with _no_mart():
        payload = _breakdown(_conn(columns=[]))
    assert payload["columns"] == []
    assert payload["columns_reason"] == COLUMNS_NO_MAPPING
    assert payload["columns_source"] is None


def test_the_response_states_that_the_header_and_the_rows_do_not_join() -> None:
    """`datastream_mappings.target_field` has no key to `fact_daily_kpi.metric`.

    The mart's `metric` is a literal typed into the dbt model. Pairing the two on
    a name that happens to match would be a join this product does not have,
    published as a fact -- and it is exactly what story 58.3 exists to build.
    """
    with _mart({"2026-07-10": 4}):
        payload = _breakdown(_conn(pulls=[_pull()]))
    assert payload["column_row_join_available"] is False
    assert payload["column_row_join_reason"] == COLUMN_ROW_JOIN_REASON


def test_the_mart_volume_of_a_day_travels_beside_the_extract_volume() -> None:
    with _mart({"2026-07-10": 4}):
        payload = _breakdown(_conn(pulls=[_pull()]))
    day = payload["days"][0]
    assert day["rows"] == 4
    assert day["rows_reason"] is None


def test_a_connector_the_mart_does_not_model_answers_an_absence_not_a_zero() -> None:
    """16 of the 39 connectors reach `fact_daily_kpi`; `0` for the other 23 is a lie."""
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    day = payload["days"][0]
    assert day["rows"] is None
    assert day["rows_reason"] == ROWS_CONNECTOR_NOT_IN_MART
    # And the response names WHICH connector the absence is about.
    assert payload["connector"] == "meta-ads"


def test_a_warehouse_outage_leaves_the_registry_served() -> None:
    """The half a person came for lives in Postgres; the mart is the other half."""
    from core.cache_warehouse import SampleReadError

    with patch(
        "core.cache_warehouse.read_daily_row_counts",
        side_effect=SampleReadError("warehouse_unavailable", "backend down"),
    ):
        payload = _breakdown(_conn(pulls=[_pull()]))
    day = payload["days"][0]
    assert day["extract_status"] == "ok"
    assert day["rows"] is None
    assert day["rows_reason"] == ROWS_WAREHOUSE_UNAVAILABLE


# ---------------------------------------------------------------------------
# The two verdicts, and the versions.
# ---------------------------------------------------------------------------


def test_the_quality_verdict_exists_empty_rather_than_borrowing_the_extract_one() -> None:
    """Arbitrage 6. 0 monitors, 0 evaluations measured -- and they are keyed by
    monitor x window, never by a day of a Datastream. Renaming a PULL verdict into
    a QUALITY verdict would have to be un-taught on every reader that believed it.
    """
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    day = payload["days"][0]
    assert day["extract_status"] == "ok"
    assert day["dq_verdict"] is None
    assert day["dq_reason"] == DQ_NO_MONITOR


def test_the_versions_travel_and_the_missing_binding_is_stated() -> None:
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    assert payload["versions"] == {
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "version_binding_available": False,
    }


# ---------------------------------------------------------------------------
# Empty, and broken.
# ---------------------------------------------------------------------------


def test_nothing_covering_the_window_keeps_the_strip_and_says_why() -> None:
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[], ever_collected=True),
            start="2026-07-10", end="2026-07-12", span=3,
        )
    assert payload["reason"] == REASON_NO_RUN_IN_WINDOW
    assert len(payload["days"]) == 3
    assert all(day["row_count"] is None for day in payload["days"])
    assert all(
        day["extract_status"] == pull_job_states.LEDGER_NEVER_FETCHED
        for day in payload["days"]
    )


def test_a_stream_that_never_collected_anything_answers_no_days_at_all() -> None:
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[], ever_collected=False),
            start="2026-07-10", end="2026-07-12", span=3,
        )
    assert payload["days"] == []
    assert payload["reason"] == REASON_NO_RUN_IN_WINDOW


def test_a_delivered_feed_has_no_day_grain_here_and_the_absence_is_pinned() -> None:
    """A NAMED LIMIT, frozen as it is so the story that closes it starts from a fact.

    The day grain of this route is `app.pull_jobs`, and only a
    `source_kind = 'connector_pull'` flux enqueues a window there. A
    `managed_feed` -- a file drop, an inbound email -- has `module_name IS NULL`
    by CHECK (migration 030, `ck_datastreams_source_kind`) and no entry in the
    pull registry at all; `dq_monitors._check_timeliness` says so in those words
    and routes those Datastreams to an arrival monitor instead.

    So its `Data` tab answers `days: []`, `connector: null`, `rows: null`. That
    is honest -- it has no extract registry to read, not an empty one -- and it
    is NOT a complete tab for those fluxes. Their day grain has to come from the
    import ledger, which is its own story. This test exists so that story finds
    the current behaviour written down instead of discovering it.
    """
    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[], facts=[(None, None, None, None)], ever_collected=False),
            start="2026-07-10", end="2026-07-12", span=3,
        )

    assert payload["days"] == []
    assert payload["reason"] == REASON_NO_RUN_IN_WINDOW
    assert payload["connector"] is None
    # No connector means no mart slice to even ask about -- and no warehouse call.
    assert payload["rows_note"] is None


def test_a_delivered_feed_asks_the_warehouse_nothing() -> None:
    with patch("core.cache_warehouse.read_daily_row_counts") as measured:
        _breakdown(_conn(pulls=[], facts=[(None, None, None, None)], ever_collected=False))
    assert measured.call_count == 0


def test_a_stream_of_another_project_is_not_found() -> None:
    with pytest.raises(DatastreamNotFound):
        _breakdown(_conn(facts=[]))


def test_an_unattributable_mart_slice_refuses_the_rows_and_not_the_days() -> None:
    """CHANGED, not preserved: this used to assert a `409` for the whole payload.

    `fact_daily_kpi` carries `(project_id, connector)` and no Datastream
    discriminator, so two live streams on one connector make that slice
    unattributable. That is a fact about the MART. `app.pull_jobs` carries its
    own `datastream_id`, so the extract registry of each stream is separable and
    was never in doubt -- withholding it was the route punishing a person for a
    warehouse's shape. Measured on the disposable cluster: 17 ambiguous
    (project, connector) groups, so 34 Datastreams whose `Data` tab would have
    had no strip of days at all.
    """
    from core.datastream_sample_api import AMBIGUOUS_MATERIALIZATION_MESSAGE  # noqa: PLC0415

    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull()], same_connector=2),
            start="2026-07-10", end="2026-07-11", span=2,
        )

    # The half that never depended on the mart is served in full.
    assert [day["date"] for day in payload["days"]] == ["2026-07-10", "2026-07-11"]
    assert payload["days"][0]["extract_status"] == "ok"
    assert payload["days"][0]["pull_id"] == "pull_001"
    assert payload["columns"] == [
        {"source_field": "spend", "target_field": "cost", "is_key_column": False,
         "binding_status": None, "sensitivity": "unknown"}
    ]
    # And the half that did says exactly why, in the sentence already written.
    assert all(day["rows"] is None for day in payload["days"])
    assert all(
        day["rows_reason"] == ROWS_AMBIGUOUS_MATERIALIZATION
        for day in payload["days"]
    )
    assert payload["rows_note"] == AMBIGUOUS_MATERIALIZATION_MESSAGE


def test_an_ambiguous_slice_never_reads_the_warehouse_at_all() -> None:
    """Nothing may be counted from a slice that cannot be attributed."""
    with patch("core.cache_warehouse.read_daily_row_counts") as measured:
        _breakdown(_conn(pulls=[_pull()], same_connector=2))
    assert measured.call_count == 0


# ---------------------------------------------------------------------------
# THE COUNTRY SPLIT -- story 58.5.
#
# The payload gains the STATE of a project capability, which nothing on this route
# carried before (`grep -c "project_capabilities\|country"` answered 0 on 2026-08-07).
# What these tests hold is the difference between the four things that all render as
# "no country column" and must never be confused:
#
#   * the project never turned the capability on -- and then there is no block at all,
#     not an empty one;
#   * this connector reports no country -- the capability is ON and the flux cannot
#     use it, which is a different switch;
#   * the mart refused the rows -- the same three refusals as the volume beside it;
#   * the window carried no country row -- a measurement, and the only one of the four
#     that is empty rather than absent.
# ---------------------------------------------------------------------------


def test_the_payload_carries_the_state_of_the_country_capability() -> None:
    """The seam 58.6 reuses. Before this story nothing on this route read it."""
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    assert payload["country"]["capability_state"] == "disabled"
    assert payload["country"]["active"] is False


def test_a_capability_nobody_turned_on_leaves_no_country_MEASURE_AT_ALL() -> None:
    """Not a list, not a `null`, not a `0` -- nothing that reads as a measurement.

    An empty list says "we looked and there were none", which is a claim about a
    project that never asked the question. The state travels and the numbers do not
    exist.
    """
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    block = payload["country"]
    assert block["reason"] == COUNTRY_CAPABILITY_NOT_ACTIVE
    assert "days" not in block
    assert "bounded_at" not in block
    assert block.get("values") is None


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", "unset"])
def test_only_ready_and_degraded_show_the_split(state) -> None:
    """Arbitrage 5. `draft` and `blocked` are OFF, and `unset` is a project the
    control plane never wrote -- three different words, one behaviour, and none of
    them holds a column open."""
    with _mart({"2026-07-10": 4}), _country({"2026-07-10": {"FR": 3}}):
        payload = _breakdown(
            _conn(pulls=[_pull()], country_capability_state=None if state == "unset" else state)
        )
    assert payload["country"]["capability_state"] == state
    assert payload["country"]["active"] is False
    assert "days" not in payload["country"]


def test_an_inactive_capability_never_reads_the_warehouse_for_a_country() -> None:
    """A grouping nobody asked for is a warehouse round trip nobody asked for."""
    with _mart({"2026-07-10": 4}), patch(
        "core.cache_warehouse.read_daily_country_counts"
    ) as measured:
        _breakdown(_conn(pulls=[_pull()]))
    assert measured.call_count == 0


def test_an_active_capability_splits_each_day_by_country_value() -> None:
    with _mart({"2026-07-10": 9}), _country({"2026-07-10": {"FR": 5, "DE": 3}}):
        payload = _breakdown(_conn(pulls=[_pull()], country_capability_state="ready"))

    block = payload["country"]
    assert block["active"] is True
    assert block["reason"] is None
    day = block["days"]["2026-07-10"]
    # Ordered by volume, descending -- the reading order of the answer itself.
    assert [entry["value"] for entry in day["values"]] == ["FR", "DE"]
    assert [entry["rows"] for entry in day["values"]] == [5, 3]
    assert day["country_count"] == 2


def test_the_no_country_bucket_is_named_by_its_own_kind_and_placed_last() -> None:
    """Arbitrage 1 and 2. It is not a country, so it is neither ranked nor rendered
    as one: it carries its own kind, its own label, and it comes after the countries
    however large it is."""
    from core.geographic_semantics import (
        COUNTRY_ABSENT_BUCKET_ID,
        COUNTRY_ABSENT_BUCKET_KIND,
        COUNTRY_ABSENT_BUCKET_LABEL,
    )

    with _mart({"2026-07-10": 9}), _country(
        {"2026-07-10": {"FR": 2, COUNTRY_ABSENT_BUCKET_ID: 40}}
    ):
        payload = _breakdown(_conn(pulls=[_pull()], country_capability_state="ready"))

    values = payload["country"]["days"]["2026-07-10"]["values"]
    assert [entry["kind"] for entry in values] == ["country", COUNTRY_ABSENT_BUCKET_KIND]
    assert values[-1]["label"] == COUNTRY_ABSENT_BUCKET_LABEL
    assert values[-1]["label"] != COUNTRY_ABSENT_BUCKET_ID
    # And it is NOT counted among the countries -- it is not one.
    assert payload["country"]["days"]["2026-07-10"]["country_count"] == 1


def test_a_day_whose_rows_ALL_lack_a_country_counts_no_country_and_says_so() -> None:
    """THE CASE THAT PRINTED A ZERO, and it is the likely one in production.

    34 connectors of the 39 report no country at all, so a day where every mart row
    lands in the absence bucket is not an edge -- it is what most fluxes would show
    the moment somebody turned the capability on. The count of NAMED countries is
    zero and forty rows exist, so a payload that published `0` beside a non-empty
    list would have a screen print `0` over forty rows.

    The number is therefore called what it counts (`country_count`), the absence
    carries its own rows, and the two can never be read as one.
    """
    from core.geographic_semantics import COUNTRY_ABSENT_BUCKET_ID

    with _mart({"2026-07-10": 40}), _country({"2026-07-10": {COUNTRY_ABSENT_BUCKET_ID: 40}}):
        payload = _breakdown(_conn(pulls=[_pull()], country_capability_state="ready"))

    day = payload["country"]["days"]["2026-07-10"]
    assert day["country_count"] == 0
    # The rows exist and are NOT zero -- that is the whole defect.
    assert [entry["rows"] for entry in day["values"]] == [40]
    assert day["values"][0]["kind"] != "country"
    # And the block does not claim an absence of data: the day was measured.
    assert payload["country"]["reason"] is None


def test_the_unfolding_states_its_bound_and_never_hides_the_absence_behind_it() -> None:
    """A truncated list that stated its own length would read as the whole answer."""
    from core.geographic_semantics import COUNTRY_ABSENT_BUCKET_ID

    counts = {f"C{index:02d}": 100 - index for index in range(20)}
    counts[COUNTRY_ABSENT_BUCKET_ID] = 1
    with _mart({"2026-07-10": 9}), _country({"2026-07-10": counts}):
        payload = _breakdown(_conn(pulls=[_pull()], country_capability_state="ready"))

    block = payload["country"]
    day = block["days"]["2026-07-10"]
    assert block["bounded_at"] == MAX_COUNTRY_VALUES_PER_DAY
    # The bound applies to the countries; the absence bucket is appended past it.
    assert len([entry for entry in day["values"] if entry["kind"] == "country"]) == (
        MAX_COUNTRY_VALUES_PER_DAY
    )
    assert day["values"][-1]["value"] == COUNTRY_ABSENT_BUCKET_ID
    # The COUNT is what was measured, not what was shown.
    assert day["country_count"] == 20


def test_a_connector_that_reports_no_country_says_so_in_its_own_word() -> None:
    """Arbitrage 7. The project is ON and this flux cannot use it -- and that is not
    "the capability is off". Confusing the two sends a person to flip a switch that
    is already flipped. Measured: 5 connectors of the 39 carry a country at all."""
    with _mart({"2026-07-10": 4}), _no_country():
        payload = _breakdown(_conn(pulls=[_pull()], country_capability_state="ready"))

    block = payload["country"]
    assert block["active"] is True
    assert block["reason"] == COUNTRY_CONNECTOR_REPORTS_NONE
    assert block["reason"] != COUNTRY_CAPABILITY_NOT_ACTIVE
    assert "days" not in block


def test_a_window_with_no_country_row_is_EMPTY_and_not_absent() -> None:
    """The fourth answer, and the only one that is a measurement: the mart was read,
    it carries country rows for this connector, and this window has none."""
    with _mart({"2026-07-10": 4}), _country({}):
        payload = _breakdown(_conn(pulls=[_pull()], country_capability_state="ready"))

    block = payload["country"]
    assert block["reason"] == COUNTRY_NO_VALUE_IN_WINDOW
    assert block["days"] == {}
    assert block["bounded_at"] == MAX_COUNTRY_VALUES_PER_DAY


@pytest.mark.parametrize(
    ("rows_patch", "expected"),
    [
        (_no_mart, ROWS_CONNECTOR_NOT_IN_MART),
        (
            lambda: patch(
                "core.cache_warehouse.read_daily_row_counts",
                side_effect=__import__(
                    "core.cache_warehouse", fromlist=["SampleReadError"]
                ).SampleReadError("warehouse_unavailable", "backend down"),
            ),
            ROWS_WAREHOUSE_UNAVAILABLE,
        ),
    ],
)
def test_the_country_block_inherits_the_refusals_of_the_volume_it_decomposes(
    rows_patch, expected
) -> None:
    """A split cannot be safer than the total it decomposes -- and the DAYS survive
    both, exactly as they do for the volume."""
    with rows_patch(), patch("core.cache_warehouse.read_daily_country_counts") as measured:
        payload = _breakdown(
            _conn(pulls=[_pull()], country_capability_state="ready"),
            start="2026-07-10", end="2026-07-11", span=2,
        )

    assert payload["country"]["reason"] == expected
    assert "days" not in payload["country"]
    # Nothing is counted from a mart that already refused the number above it.
    assert measured.call_count == 0
    # And the half that lives in Postgres is served in full.
    assert [day["date"] for day in payload["days"]] == ["2026-07-10", "2026-07-11"]


def test_an_unattributable_slice_refuses_the_split_in_the_words_already_written() -> None:
    from core.datastream_sample_api import AMBIGUOUS_MATERIALIZATION_MESSAGE  # noqa: PLC0415

    with _no_mart():
        payload = _breakdown(
            _conn(pulls=[_pull()], same_connector=2, country_capability_state="ready")
        )
    assert payload["country"]["reason"] == ROWS_AMBIGUOUS_MATERIALIZATION
    assert payload["country"]["note"] == AMBIGUOUS_MATERIALIZATION_MESSAGE


def test_a_degraded_capability_shows_the_split_AND_says_it_is_degraded() -> None:
    """Arbitrage 5: hiding data already collected is worse than showing it diminished."""
    with _mart({"2026-07-10": 4}), _country({"2026-07-10": {"FR": 4}}):
        payload = _breakdown(_conn(pulls=[_pull()], country_capability_state="degraded"))
    assert payload["country"]["active"] is True
    assert payload["country"]["degraded"] is True
    assert payload["country"]["days"]["2026-07-10"]["values"][0]["value"] == "FR"


def test_the_split_costs_one_grouping_and_not_a_query_per_day() -> None:
    """Asserted on 1 day AND on 92: the cost of this block must not scale with the
    window. The same claim the volume beside it makes, made again because a second
    grouping is exactly where a per-day loop would have crept in."""
    with _mart({}), _country({}) as measured:
        one = _conn(pulls=[_pull()], country_capability_state="ready")
        _breakdown(one, span=1)
        first = measured.call_count
        wide = _conn(pulls=[_pull()], country_capability_state="ready")
        _breakdown(wide, start="2026-01-01", end="2026-04-02", span=MAX_WINDOW_DAYS)
        second = measured.call_count - first

    assert (first, second) == (1, 1)
    # And ONE statement more against Postgres, whatever the width of the window.
    # (The two calls do not have the SAME total: a window nothing covered pays for
    # `stream_has_ever_collected`, which is 58.1's branch and not this one. What is
    # asserted is what this story added.)
    for connection in (one, wide):
        reads = [text for text in connection.statements if "app.project_capabilities" in text]
        assert len(reads) == 1


# ---------------------------------------------------------------------------
# The handler: what reaches the wire.
# ---------------------------------------------------------------------------


def _request(query=None):
    request = MagicMock()
    request.path_params = {"project_id": "proj_a", "datastream_id": "ds_001"}
    request.query_params = query or {}
    return request


def _run(coro):
    return asyncio.run(coro)


def _authenticated():
    return patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))
    )


def _connection(conn):
    class _Once:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, *exc):
            return False

    return patch("core.db.get_connection", side_effect=lambda *a, **k: _Once())


def test_an_unauthenticated_call_is_401_and_opens_no_connection() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))),
        patch("core.db.get_connection", side_effect=AssertionError("must not open")),
    ):
        response = _run(_read_daily_breakdown(_request({"start": "2026-07-10",
                                                        "end": "2026-07-10"})))
    assert response.status_code == 401
    assert json.loads(response.body)["code"] == "unauthorized"


def test_a_malformed_window_is_refused_before_a_connection_is_opened() -> None:
    """A request with nothing to read must not pay for a round trip."""
    with (
        _authenticated(),
        patch("core.db.get_connection", side_effect=AssertionError("must not open")),
    ):
        response = _run(_read_daily_breakdown(_request({"end": "2026-07-10"})))
    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["code"] == "missing_param"
    assert body["parameter"] == "start"


def test_the_guard_receives_the_stream_id_and_claims_no_proof_of_its_own() -> None:
    """The ratchet on `pair_proven_by_read` is at ONE, and it is the polled route."""
    conn = _conn(pulls=[_pull()])
    with (
        _authenticated(),
        _connection(conn),
        patch("core.admin_api._require_datastream_role", return_value=None) as guard,
        _no_mart(),
    ):
        response = _run(_read_daily_breakdown(_request({"start": "2026-07-10",
                                                        "end": "2026-07-10"})))
    assert response.status_code == 200
    assert guard.call_args.kwargs["datastream_id"] == "ds_001"
    assert guard.call_args.args[2] == "viewer"
    assert guard.call_args.kwargs.get("pair_proven_by_read") is not True


def test_a_refused_role_reads_nothing_at_all() -> None:
    from starlette.responses import JSONResponse

    denied = JSONResponse({"code": "not_found", "message": "x"}, status_code=404)
    conn = _conn(pulls=[_pull()])
    with (
        _authenticated(),
        _connection(conn),
        patch("core.admin_api._require_datastream_role", return_value=denied),
    ):
        response = _run(_read_daily_breakdown(_request({"start": "2026-07-10",
                                                        "end": "2026-07-10"})))
    assert response.status_code == 404
    assert conn.statements == []


def test_no_status_of_this_route_withholds_the_days_for_a_mart_reason() -> None:
    """On the wire: `200`, the strip served, the rows refused with their sentence.

    CHANGED, not preserved: this asserted `409` and cemented the defect. This
    route has no `409` at all any more -- nothing about the warehouse's shape is
    a reason to refuse a registry that lives in Postgres.
    """
    from core.datastream_sample_api import AMBIGUOUS_MATERIALIZATION_MESSAGE  # noqa: PLC0415

    conn = _conn(pulls=[_pull()], same_connector=2)
    with (
        _authenticated(),
        _connection(conn),
        patch("core.admin_api._require_datastream_role", return_value=None),
        _no_mart(),
    ):
        response = _run(_read_daily_breakdown(_request({"start": "2026-07-10",
                                                        "end": "2026-07-10"})))
    assert response.status_code == 200
    body = json.loads(response.body)
    assert len(body["days"]) == 1
    assert body["days"][0]["extract_status"] == "ok"
    assert body["days"][0]["rows_reason"] == ROWS_AMBIGUOUS_MATERIALIZATION
    assert body["rows_note"] == AMBIGUOUS_MATERIALIZATION_MESSAGE


def test_the_route_declares_no_409_anywhere() -> None:
    """A ratchet, because `409` is one `raise` away from coming back.

    The refusal it used to carry is now a field on a `200`. Re-introducing the
    status would silently take the strip away again from the 34 Datastreams the
    story measured, and every test above would still pass on the ones that are
    not ambiguous.
    """
    import ast
    from pathlib import Path

    import core.datastream_daily_breakdown_api as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    # THE CODE, NOT THE PROSE. The module docstring explains why the status is
    # gone, and an explanation must not be able to fail the test that the rule
    # is implemented -- nor to satisfy it.
    statuses = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in (409,)
    ]
    assert not statuses, (
        "the day-grain route answers 409 again: a mart that cannot attribute its "
        "rows is not a reason to withhold an extract registry that is "
        "per-Datastream by construction"
    )


def test_a_read_that_fails_is_503_and_says_nothing_about_the_database(caplog) -> None:
    conn = _conn(pulls=[_pull()])
    with (
        _authenticated(),
        _connection(conn),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch(
            "core.datastream_daily_breakdown_api.read_stream_facts",
            side_effect=RuntimeError("connection reset by peer"),
        ),
        caplog.at_level("ERROR", logger="core.datastream_daily_breakdown_api"),
    ):
        response = _run(_read_daily_breakdown(_request({"start": "2026-07-10",
                                                        "end": "2026-07-10"})))
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body == {"code": "unavailable", "message": "Daily breakdown is unavailable"}
    # The operator gets the type; the caller gets none of it.
    assert "RuntimeError" in caplog.text
    assert "connection reset" not in response.body.decode()


def test_a_stage_refusal_reaches_the_wire_as_422_with_its_alternatives() -> None:
    """CHANGED by 58.3, arbitrage 2, and the change is the point.

    The refusal used to be decided before a connection was opened, because every
    stage but `processed` was refused for every flux. It is now a fact about THIS
    Datastream -- the flux below names no report profile -- so the wire carries the
    resolver's sentence and the list of modes that would be served.
    """
    from core.stage_relation_resolver import message_for

    conn = _conn(pulls=[_pull()], facts=[("meta-ads", "dsp_1", "dmap_1", None)])
    with (
        _authenticated(),
        _connection(conn),
        patch("core.admin_api._require_datastream_role", return_value=None),
        _no_mart(),
    ):
        response = _run(_read_daily_breakdown(_request({
            "start": "2026-07-10", "end": "2026-07-10", "view_mode": "collected",
        })))
    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["code"] == "stage_not_materialized"
    assert body["message"] == message_for("report_profile_not_set")
    assert body["available_view_modes"] == ["processed"]
    # And the four positions, each with its own verdict, so the caller that was
    # refused can render the control it should have rendered.
    assert [entry["mode"] for entry in body["available"]] == [
        "collected", "mapped", "processed", "published"
    ]


def test_a_view_mode_outside_the_vocabulary_still_costs_no_connection() -> None:
    with (
        _authenticated(),
        patch("core.db.get_connection", side_effect=AssertionError("must not open")),
    ):
        response = _run(_read_daily_breakdown(_request({
            "start": "2026-07-10", "end": "2026-07-10", "view_mode": "side_by_side",
        })))
    assert response.status_code == 400
    assert json.loads(response.body)["code"] == "invalid_stage"


# ---------------------------------------------------------------------------
# The reading of ONE day (story 58.3). Bounded, asked for, never volunteered.
# ---------------------------------------------------------------------------


def test_no_day_asked_for_means_no_reading_and_no_warehouse_row_read() -> None:
    with _no_mart(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped"
    ) as reader:
        payload = _breakdown(_conn(pulls=[_pull()]))
    assert payload["reading"] is None
    reader.assert_not_called()


def test_a_day_asked_for_carries_BOTH_readings_from_one_call() -> None:
    """One call, two sides: switching position on the screen then costs nothing,
    which is how the window and the opened day cannot move when it happens."""
    reading = {
        "window": {"start": "2026-07-10", "end": "2026-07-10"},
        "collected": {"relation": "raw_meta_ads_daily", "rows": [], "reason": None},
        "mapped": {"relation": "stg_meta_ads_daily", "rows": [], "reason": None},
    }
    with _no_mart(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped", return_value=reading
    ) as reader:
        payload = _breakdown(_conn(pulls=[_pull()]), day="2026-07-10")

    assert payload["reading"]["day"] == "2026-07-10"
    assert payload["reading"]["collected"]["relation"] == "raw_meta_ads_daily"
    assert payload["reading"]["mapped"]["relation"] == "stg_meta_ads_daily"
    assert reader.call_count == 1
    assert reader.call_args.kwargs["start"] == reader.call_args.kwargs["end"]


def test_the_classifications_handed_to_the_reader_mask_by_default() -> None:
    """Arbitrage 6, at the seam: only a field declared `none` may be shown."""
    version = {
        "grain": [],
        "fields": [
            {"field_id": "clicks", "suggestion": {"sensitivity": "none"},
             "binding": {"status": "confirmed", "mdm_target": None}},
            {"field_id": "usr_mail", "suggestion": {"sensitivity": "pii"},
             "binding": {"status": "confirmed", "mdm_target": None}},
            # No declared sensitivity at all -- `unknown`, therefore masked.
            {"field_id": "depense", "binding": {"status": "suggested"}},
        ],
    }
    with _no_mart(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped", return_value={}
    ) as reader:
        _breakdown(_conn(mapping_version=version), day="2026-07-10")

    classifications = reader.call_args.kwargs["classifications"]
    assert classifications["clicks"] == "none"
    assert classifications["usr_mail"] == "pii"
    assert classifications["depense"] == "unknown"


# ---------------------------------------------------------------------------
# The money provenance travels with the reading (story 58.7).
#
# What is held here is the SEAM, not the designation: that the key crosses
# `read_day_reading` intact, that it is never asked for when no day is opened, and
# that its absence cannot take the days down. The designation itself is proved
# against a real relation in `tests/core/test_collected_mapped_reader.py` and
# against the estate in `tests/integration/test_datastream_daily_breakdown_api.py`.
# ---------------------------------------------------------------------------


def _reading_double(collected_columns=(), mapped_columns=()):
    """What the reader answers -- the shape, with the columns the sides carried."""
    return {
        "window": {"start": "2026-07-10", "end": "2026-07-10"},
        "collected": {
            "relation": "raw_meta_ads_daily", "columns": list(collected_columns),
            "rows": [], "reason": None,
        },
        "mapped": {
            "relation": "stg_meta_ads_daily", "columns": list(mapped_columns),
            "rows": [], "reason": None,
        },
    }


def _described_money():
    """Both relations readable, the mapped one carrying the converting columns."""
    return _described(
        collected={"columns": ["date", "spend", "cost_source_currency"]},
        mapped={
            "columns": [
                "date", "cost_source_value", "cost_source_currency", "cost",
                "fx_rate", "fx_as_of_date", "fx_source", "fx_tier",
            ]
        },
    )


def test_the_provenance_key_is_on_BOTH_sides_of_every_reading() -> None:
    """Present always, empty with its reason -- never missing.

    A key a screen cannot find and a key that came back empty are two different
    sentences, and the screen says both. If this ever becomes conditional, one of
    them starts standing in for the other.

    THE CAPABILITY IS ON HERE, and it has to be said (lot B3): since amendment 11
    the empty designation is what a project sees once it turned `currency_fx` on
    and published no monetary Concept. With the capability off the answer is a
    different sentence, held by the test below.
    """
    from core.money_provenance_columns import NO_MONETARY_CONCEPT_DECLARED

    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(
            _conn(pulls=[_pull()], currency_fx_capability_state="ready"),
            day="2026-07-10",
        )

    for zone in ("collected", "mapped"):
        designation = payload["reading"][zone]["money_provenance"]
        assert designation["columns"] == []
        # The estate: no Concept version declares an amount, so nothing is
        # designated and the answer names the authority that would change that.
        assert designation["reason"] == NO_MONETARY_CONCEPT_DECLARED
        assert designation["message"]
        assert designation["reporting_currency"] is None


def test_a_declared_amount_crosses_the_seam_with_what_explains_it() -> None:
    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(
            _conn(
                pulls=[_pull()],
                monetary_concepts=("cost",),
                currency_fx_capability_state="ready",
            ),
            day="2026-07-10",
        )

    mapped = payload["reading"]["mapped"]["money_provenance"]
    assert [entry["column"] for entry in mapped["columns"]] == ["cost"]
    assert mapped["columns"][0]["fx_as_of_date"] == "fx_as_of_date"
    # And the raw zone designates nothing, for the reason that is TRUE of it:
    # `raw_meta_ads_daily` carries `spend`, which no Concept declares. The other
    # raw refusal -- a raw relation that DOES carry the declared name and no rate
    # column, `fx_columns_absent_in_raw_zone` -- is exercised over a real relation
    # in `tests/core/test_collected_mapped_reader.py`, where a warehouse exists to
    # carry one.
    collected = payload["reading"]["collected"]["money_provenance"]
    assert collected["columns"] == []
    assert collected["reason"] == "no_monetary_column_in_relation"


def test_the_authority_is_not_asked_when_no_day_is_opened() -> None:
    """One statement, and only when there are rows for it to qualify."""
    conn = _conn(pulls=[_pull()])
    with _no_mart():
        _breakdown(conn)
    assert not any("value_type = 'money'" in text for text in conn.statements)


def test_a_reading_that_carries_no_provenance_does_not_take_the_days_down() -> None:
    """The reader is doubled with a shape that has no side dicts at all.

    A payload half nobody could annotate is not a reason to withhold an extract
    registry that is per-Datastream by construction -- the same rule the mart
    absence follows one block up.
    """
    with _no_mart(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped", return_value={}
    ):
        payload = _breakdown(_conn(pulls=[_pull()]), day="2026-07-10")

    assert payload["reading"]["day"] == "2026-07-10"
    assert "money_provenance" not in payload["reading"]
    assert len(payload["days"]) == 1


def test_the_fx_columns_inherit_the_classification_of_the_amount_they_explain() -> None:
    """Masking stays a refusal by default, and the provenance stops being blind.

    No mapping names `fx_rate` or `fx_as_of_date`, so on their own authority they
    are unclassified and therefore masked -- which would have printed
    `[MASKED] · [MASKED]` under an amount the same policy had just decided to show.
    They inherit, and only downward: a masked amount keeps a masked rate.
    """
    version = {
        "grain": [],
        "fields": [
            {"field_id": "cost", "suggestion": {"sensitivity": "none"},
             "binding": {"status": "confirmed", "mdm_target": None}},
        ],
    }
    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ) as reader:
        _breakdown(
            _conn(
                mapping_version=version,
                monetary_concepts=("cost",),
                currency_fx_capability_state="ready",
            ),
            day="2026-07-10",
        )

    classifications = reader.call_args.kwargs["classifications"]
    assert classifications["cost"] == "none"
    assert classifications["fx_rate"] == "none"
    assert classifications["fx_as_of_date"] == "none"
    assert classifications["cost_source_currency"] == "none"


def test_a_masked_amount_keeps_a_masked_provenance() -> None:
    version = {
        "grain": [],
        "fields": [
            {"field_id": "cost", "suggestion": {"sensitivity": "confidential"},
             "binding": {"status": "confirmed", "mdm_target": None}},
        ],
    }
    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ) as reader:
        _breakdown(
            _conn(
                mapping_version=version,
                monetary_concepts=("cost",),
                currency_fx_capability_state="ready",
            ),
            day="2026-07-10",
        )

    classifications = reader.call_args.kwargs["classifications"]
    assert classifications["fx_rate"] == "confidential"


def test_a_day_outside_the_window_is_refused_by_name() -> None:
    with (
        _authenticated(),
        patch("core.db.get_connection", side_effect=AssertionError("must not open")),
    ):
        response = _run(_read_daily_breakdown(_request({
            "start": "2026-07-10", "end": "2026-07-12", "day": "2026-07-20",
        })))
    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["code"] == "day_outside_window"
    assert body["parameter"] == "day"


# ---------------------------------------------------------------------------
# The pairing crosses the seam with the mapping that keys it -- amendment 12.
#
# What is held here is the HAND-OVER, not the join: that the route gives the
# reader the mapping's own `source -> target` and gives it nothing else to pair
# on. The join itself is proved against a real DuckDB warehouse in
# `tests/core/test_collected_mapped_reader.py`, where two relations exist to be
# paired.
# ---------------------------------------------------------------------------


def test_the_mapping_is_what_the_reader_is_given_to_pair_the_two_sides_with() -> None:
    """The header the route already read, handed down -- never a second read, and
    never the mart's `metric`, which keys nothing between these two relations."""
    version = {
        "grain": ["media_date"],
        "fields": [
            {"field_id": "media_date", "suggestion": {"sensitivity": "none"},
             "binding": {"status": "confirmed", "canonical_target": "date"}},
            {"field_id": "depense", "suggestion": {"sensitivity": "none"},
             "binding": {"status": "confirmed", "canonical_target": "cost"}},
        ],
    }
    with _no_mart(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped", return_value={}
    ) as reader:
        _breakdown(_conn(mapping_version=version), day="2026-07-10")

    fields = reader.call_args.kwargs["fields"]
    assert [(f["source_field"], f["target_field"]) for f in fields] == [
        ("media_date", "date"),
        ("depense", "cost"),
    ]
    # The grain of the payload IS what keys a row, and it travels with the pair.
    assert [f["is_key_column"] for f in fields] == [True, False]


def test_the_mart_join_flag_is_untouched_and_says_nothing_about_the_reading() -> None:
    """Two questions, two answers. `column_row_join_available` answers "can the
    fields be drawn over the day grid" and its answer is still no; the reading's
    pairing answers a question about two other relations entirely, and reading the
    first as the second is what replaced the epic's promise with a paragraph."""
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()]))
    assert payload["column_row_join_available"] is False
    assert payload["column_row_join_reason"] == COLUMN_ROW_JOIN_REASON
