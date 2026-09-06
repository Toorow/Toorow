"""Canonical Workbench boundary proofs for Story 47.5."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from core import admin_api

# Story 59.2 -- the fleet's own seed, reused rather than copied. The header and
# the list must agree about the SAME rows, and a second fixture written here
# would be a second world for one fact. `dq_fleet` itself is a fixture of
# `tests/core/conftest.py`, so it is requested by name and not imported.
from tests.core.conftest import open_dq_issue, publish_dq_monitor

# AI-317: a fake cursor that recognises SQL by text derives its description from
# the statement and raises on a statement it was never taught.
from tests.support.statement_router import StatementInventory, UnknownStatement, describe

ROOT = Path(__file__).resolve().parents[3]


def test_migration_138_adds_append_only_stage_and_output_evidence() -> None:
    sql = (ROOT / "infra/nango/migrations/138_datastream_workbench_evidence.sql").read_text(
        encoding="utf-8"
    )

    assert "app.datastream_execution_stage_evidence" in sql
    assert "app.datastream_execution_phase_evidence" in sql
    assert "app.datastream_outputs" in sql
    assert "app.datastream_output_versions" in sql
    assert "reject_datastream_workbench_evidence_mutation" in sql
    assert "safe_preconfiguration_evidence" in sql


def test_workbench_mounts_base_and_six_project_scoped_read_routes() -> None:
    paths = {route.path for route in admin_api.router.routes if "GET" in route.methods}
    base = "/api/projects/{project_id}/datastreams/{datastream_id}/workbench"

    assert base in paths
    # THE REGISTRY, NOT A COPY OF IT: `cost` is mounted by the same loop, and a
    # hand-written list here would have gone on proving six of the seven.
    from core.datastream_workbench import TABS

    assert "cost" in TABS
    for tab in TABS:
        assert f"{base}/{tab}" in paths


def test_tab_payloads_have_distinct_server_owned_schemas() -> None:
    # ONE SCHEMA PER TAB, DISTINCT -- which is the claim. The count was pinned at
    # six on the clause the amendment « Une capacité activée AJOUTE son onglet »
    # reverses, so it went red on a tab that document REQUIRES; what has to hold
    # is the bijection, not the cardinal.
    from core.datastream_workbench import TAB_SCHEMAS, TABS, compose_tab_payload

    assert len(TAB_SCHEMAS) == len(TABS)
    assert len(set(TAB_SCHEMAS.values())) == len(TABS)
    for tab, schema in TAB_SCHEMAS.items():
        payload = compose_tab_payload(
            tab,
            datastream_id="ds_1",
            project_id="proj_1",
            evidence={"state": "unavailable", "reason": "No evidence"},
        )
        assert payload["schema"] == schema
        assert payload["tab"] == tab
        assert payload["datastream_id"] == "ds_1"
        assert payload["project_id"] == "proj_1"


def test_core_workbench_boundary_is_source_agnostic_and_safe() -> None:
    from core import datastream_workbench

    source = inspect.getsource(datastream_workbench).lower()
    for provider_word in (
        "google analytics",
        "meta ads",
        "tiktok",
        "shopify",
        "stripe",
        "klaviyo",
    ):
        assert provider_word not in source
    assert "raw_rows" not in source
    assert "credential_secret" not in source


def test_header_axes_and_primary_action_are_composed_server_side() -> None:
    from core.datastream_workbench import compose_header

    header = compose_header(
        {
            "id": "ds_1",
            "project_id": "proj_1",
            "name": "Daily performance",
            "source_kind": "connector_pull",
            "data_role": "Performance",
            "lifecycle_state": "draft",
            "current_plan_version_id": None,
            "current_mapping_version_id": None,
            "current_published_execution_id": None,
        }
    )
    assert set(header["axes"]) == {"lifecycle", "configuration", "operations", "publication"}
    assert header["primary_action"]["kind"] == "finish_setup"
    assert header["primary_action"]["reason"]


# ---------------------------------------------------------------------------
# Operations axis: derived from SCHEDULE evidence, not from the latest run alone
# (`datastream-workbench-and-wizard.md:87`). The `Stale` branch used to be
# unreachable -- it tested an `is_stale` key no query produced -- so a Datastream
# that had stopped running read `Healthy` forever.
# ---------------------------------------------------------------------------


def _live_record(**overrides):
    base = {
        "id": "ds_1",
        "project_id": "proj_1",
        "name": "Daily performance",
        "source_kind": "connector_pull",
        "lifecycle_state": "active",
        "current_plan_version_id": "plan_1",
        "current_mapping_version_id": "map_1",
        "current_published_execution_id": "exec_1",
        "latest_run_state": "published",
        "schedule_state_known": True,
        "schedule_next_run_at": "2099-01-01T00:00:00+00:00",
        "schedule_missed_run_count": 0,
        "schedule_overdue": False,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("overrides", "expected", "reason"),
    [
        ({}, "Healthy", "on schedule, next run in the future"),
        ({"schedule_overdue": True}, "Stale", "next_run_at is in the past"),
        ({"schedule_missed_run_count": 3}, "Stale", "the scheduler recorded misses"),
        (
            {"schedule_state_known": False, "schedule_next_run_at": None,
             "schedule_missed_run_count": None},
            "Unknown",
            "active on a current plan but the schedule was never armed",
        ),
        ({"latest_run_state": "failed"}, "Blocked", "failure outranks schedule"),
        ({"latest_run_state": "partial"}, "Degraded", "partial outranks schedule"),
    ],
)
def test_operations_axis_reads_schedule_evidence(overrides, expected, reason) -> None:
    from core.datastream_workbench import compose_header

    header = compose_header(_live_record(**overrides))
    assert header["axes"]["operations"] == expected, reason


def test_a_successful_run_on_an_unarmed_schedule_is_never_healthy() -> None:
    """The regression this axis exists to prevent.

    A Datastream whose last run succeeded, whose schedule was never armed, and
    which will therefore never receive another row, must not present as Healthy.
    `README.md:123` (invariant 8): unknown is never healthy.
    """
    from core.datastream_workbench import compose_header

    header = compose_header(
        _live_record(
            schedule_state_known=False,
            schedule_next_run_at=None,
            schedule_missed_run_count=None,
        )
    )
    assert header["axes"]["operations"] != "Healthy"
    assert header["operations_evidence"]["schedule_state_known"] is False


def test_operations_evidence_names_why_the_axis_says_stale() -> None:
    """An axis that cannot be inspected is an assertion, not evidence."""
    from core.datastream_workbench import compose_header

    header = compose_header(_live_record(schedule_overdue=True, schedule_missed_run_count=2))
    assert header["axes"]["operations"] == "Stale"
    assert sorted(header["operations_evidence"]["late_reasons"]) == ["missed_runs", "overdue"]
    assert header["operations_evidence"]["missed_run_count"] == 2


def test_the_declared_cadence_reaches_the_screen_that_denies_it() -> None:
    """`Next run` printed "No cadence set" under every Datastream that has one.

    `WorkbenchOverviewPage.tsx` reads `schedule.mode` and falls back to that
    sentence; `_read_base_record` never selected `app.datastreams.schedule_mode`,
    so the fallback was the ONLY branch reachable -- including for the 742
    `nightly` rows of the fixture base. A sentence that denies a setting the row
    carries is worse than a blank, because it answers instead of declining.

    `None` stays `None`: the sentence is correct when the column really is unset,
    and this test pins both halves so a future read cannot substitute a default.
    """
    from core.datastream_workbench import compose_header

    nightly = compose_header(_live_record(schedule_mode="nightly"))
    assert nightly["operations_evidence"]["mode"] == "nightly"

    unset = compose_header(_live_record(schedule_mode=None))
    assert unset["operations_evidence"]["mode"] is None


def test_change_prepare_freezes_exact_active_versions_and_diff() -> None:
    """A MOCK, and it proves only the wiring of the two pointers into the review.

    `cur.fetchone` returns whatever this test says it returns, so the real
    query's `FOR UPDATE`, its lifecycle test and its head-of-ledger fallback are
    never evaluated here and no refusal of this engine can be measured from this
    file. Everything that actually guards the change -- the AD-27 secret, single
    use, expiry, binding revalidation, the idempotent replay, and since
    2026-08-12 the head-version base and its optimistic lock -- is proven against
    a real schema in `tests/integration/test_datastream_change_engine_pg.py`. Do
    not add a guard assertion here; it would be green whatever the engine does.
    """
    from unittest.mock import MagicMock

    from core.datastream_change import prepare_change

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    # FOUR reads now, in order: the Datastream row, then each axis resolved
    # against its ledger (`resolve_base`), then the retained-import count. The
    # single-row shape this test carried was the INNER JOIN the engine no longer
    # does -- it could not reach a Datastream with no pointer, which is 6 of the
    # 8 on the live base.
    cur.fetchone.side_effect = [
        (
            "org_1",
            "active",  # lifecycle_state
            None,  # archived_at
            "dsp_1",  # current_plan_version_id
            "dmap_1",  # current_mapping_version_id
            # `config` -- the Template binding lives under `source_owner`
            # (story 38.17 AC1). Empty here: this fixture is a connector-pull shape.
            {},
        ),
        ({"schedule": {"mode": "daily"}},),
        ({"fields": [{"field_id": "date"}]},),
        (0,),
    ]

    result = prepare_change(
        conn,
        project_id="proj_1",
        datastream_id="ds_1",
        kind="processing",
        proposed_payload={"schedule": {"mode": "hourly"}},
        actor="person_1",
        idempotency_key="change-1",
    )

    assert result["review"]["expected_plan_version_id"] == "dsp_1"
    assert result["review"]["expected_mapping_version_id"] == "dmap_1"
    # A pointer was in force on both axes, so the review says so rather than
    # leaving two ids that would read the same either way.
    assert result["review"]["base_versions"] == {
        "plan": {"state": "in_force", "version_id": "dsp_1"},
        "mapping": {"state": "in_force", "version_id": "dmap_1"},
    }
    assert result["review"]["diff"] == [
        {
            "path": "$.schedule",
            "before_hash": result["review"]["diff"][0]["before_hash"],
            "after_hash": result["review"]["diff"][0]["after_hash"],
        }
    ]
    assert result["confirmation_secret"]
    insert = next(
        call
        for call in cur.execute.call_args_list
        if "datastream_change_preparations" in call.args[0]
    )
    assert "dsp_1" in insert.args[1]
    assert "dmap_1" in insert.args[1]


def test_change_confirmation_is_non_live_and_dispatches_one_candidate() -> None:
    from core import datastream_change

    source = inspect.getsource(datastream_change)
    assert "advance_pointer=False" in source
    assert source.count("create_execution(") == 1
    assert source.count("enqueue_activation_work(") == 1
    assert "current_plan_version_id,current_mapping_version_id" in source
    assert "state='confirmed'" in source


def test_workbench_persists_stage_and_output_version_evidence() -> None:
    from core import datastream_activation, datastream_workbench

    stage_source = inspect.getsource(datastream_workbench.append_stage_evidence)
    phase_source = inspect.getsource(datastream_workbench.append_phase_evidence)
    publish_source = inspect.getsource(datastream_activation.publish_activate_mutation)
    assert "datastream_execution_stage_evidence" in stage_source
    assert "datastream_execution_phase_evidence" in phase_source
    assert "Secret-bearing stage evidence is forbidden" in stage_source
    assert "datastream_outputs" in publish_source
    assert "datastream_output_versions" in publish_source
    assert 'stage="published"' in publish_source
    assert 'phase="publication"' in publish_source


# ---------------------------------------------------------------------------
# Story 48.1: the capability projection reaches the three tabs that own its
# subjects, through the server read model -- and adds no seventh tab.
# ---------------------------------------------------------------------------


def test_migration_139_adds_the_immutable_capability_proposal_contract() -> None:
    sql = (ROOT / "infra/nango/migrations/139_project_capability_proposals.sql").read_text(
        encoding="utf-8"
    )

    assert "app.datastream_capability_proposals" in sql
    assert "app.project_configuration_owner_references" in sql
    assert "app.capability_exceptions" in sql
    # Immutable after preparation, and Project-scoped through composite keys.
    assert "reject_capability_proposal_mutation" in sql
    assert "reject_configuration_owner_reference_mutation" in sql
    assert "UNIQUE (project_id, change_set_id, datastream_id, capability_key)" in sql
    # Applicability and the denominator cannot disagree in stored data.
    assert (
        "CHECK ((applicability = 'not_applicable') = (coverage_state = 'not_applicable'))" in sql
    )
    # The complete impact shape is enforced, not merely documented.
    for block in (
        "detected_support_selection",
        "grain_before_after",
        "cardinality_scan",
        "quota_cost",
        "recent_history",
        "historical_coverage",
        "backfill",
        "fan_out",
    ):
        assert f"impact ? '{block}'" in sql


def test_the_capability_projection_narrows_per_tab_and_adds_no_new_tab() -> None:
    from core.datastream_workbench import TABS, _capability_projection

    projection = {
        "schema": "datastream_capabilities.v1",
        "capabilities": [
            {
                "capability_key": "country",
                "coverage_state": "partial",
                "applicability": "applicable",
                "impact": {
                    "grain_before_after": {"added": ["country"]},
                    "cardinality_scan": {"state": "estimated"},
                    "quota_cost": {"state": "declared"},
                    "fan_out": {"downstream_consumers": 2},
                },
            }
        ],
        "primary_action": {"capability_key": "country", "kind": "review"},
    }

    class _Stub:
        pass

    import core.capability_proposals as proposals

    original = proposals.read_datastream_capabilities
    proposals.read_datastream_capabilities = lambda *_a, **_k: projection
    try:
        mapping = _capability_projection(
            _Stub(), "proj_EXAMPLE", "ds_EXAMPLE",
            blocks=("grain_before_after", "cardinality_scan"),
        )
        processing = _capability_projection(
            _Stub(),
            "proj_EXAMPLE",
            "ds_EXAMPLE",
            blocks=("grain_before_after", "cardinality_scan", "quota_cost", "fan_out"),
        )
        overview = _capability_projection(_Stub(), "proj_EXAMPLE", "ds_EXAMPLE")
    finally:
        proposals.read_datastream_capabilities = original

    # Mapping sees schema and grain; Processing sees cost and reach. Handing every
    # block to every tab is how a workbench stops meaning anything.
    assert set(mapping["capabilities"][0]["impact"]) == {
        "grain_before_after",
        "cardinality_scan",
    }
    assert set(processing["capabilities"][0]["impact"]) == {
        "grain_before_after",
        "cardinality_scan",
        "quota_cost",
        "fan_out",
    }
    assert len(overview["capabilities"][0]["impact"]) == 4
    # THE PROJECTION STILL ADDS NO TAB OF ITS OWN, and that is what this asserts.
    #
    # It used to assert `len(TABS) == 6`, on the clause « the capability is a
    # projection inside the six tabs that exist ». The amendment « Une capacité
    # activée AJOUTE son onglet » of `datastream-workbench-and-wizard.md` reverses
    # that clause: `tax_fees` opens `Cost` (story 58.6) and `Placement mapping`
    # will open `Placements`. A count is the wrong guard for it either way -- it
    # went red on a tab the amendment REQUIRES. What must stay true is that this
    # projection is not itself a tab.
    assert "capabilities" not in TABS
    assert "cost" in TABS


# ---------------------------------------------------------------------------
# Story 63.1 -- the tabs read a run that is still moving, not only stopped runs.
# ---------------------------------------------------------------------------

_PROGRESS_COLUMNS = (
    "step",
    "day_in_progress",
    "days_done",
    "days_total",
    "rows_written",
    "started_at",
    "progress_updated_at",
)


def _read_tab_source() -> str:
    from core import datastream_workbench

    return inspect.getsource(datastream_workbench.read_tab)


def _tab_query(tab: str) -> str:
    """The execution SELECT the named tab issues, isolated from its neighbours."""
    source = _read_tab_source()
    marker = f'elif tab == "{tab}":' if tab != "overview" else 'if tab == "overview":'
    body = source.split(marker, 1)[1]
    for other in ("elif tab ==", "\n    return "):
        body = body.split(other)[0]
    return body


@pytest.mark.parametrize("tab", ["overview", "runs"])
def test_the_run_a_tab_shows_carries_where_it_is(tab: str) -> None:
    """A collecting run was readable as `loading` and nothing else, on every surface."""
    query = _tab_query(tab)
    assert "app.datastream_executions" in query
    for column in _PROGRESS_COLUMNS:
        assert column in query, f"{tab} tab drops {column}"


def test_the_two_tabs_show_the_same_progress_and_not_two_versions_of_it() -> None:
    """63.5 puts this state in three places; two column lists would become two truths."""
    overview = _tab_query("overview")
    runs = _tab_query("runs")
    assert all(c in overview and c in runs for c in _PROGRESS_COLUMNS)


def test_the_frozen_evidence_tables_still_refuse_a_second_write() -> None:
    """Story 63.1 writes progress on the execution row and NOWHERE near these two.

    THE GUARD NAMES THE TWO TABLES, and since story 58.10 it has to. It banned
    the substring `evidence` anywhere in `execution_progress`, which is wider
    than the invariant it protects: migration 223 gave the four ratified steps
    their own MUTABLE span table (`app.datastream_execution_step_evidence`),
    written exactly beside the statement that moves `step`, and a run entering a
    step twice must update a row rather than append one. What must stay true is
    that the two FROZEN, append-only tables are still untouched here -- that is
    what is asserted, by name.
    """
    from core.datastream_workbench import append_phase_evidence, append_stage_evidence

    for fn in (append_phase_evidence, append_stage_evidence):
        source = inspect.getsource(fn)
        assert "already exists with different facts" in source
    from core import execution_progress

    module_source = inspect.getsource(execution_progress)
    for frozen in (
        "datastream_execution_phase_evidence",
        "datastream_execution_stage_evidence",
    ):
        assert frozen not in module_source
    # And the mutable one it DOES write is the third table, upserted -- never
    # appended, which is why it carries no append-only trigger (migration 223).
    assert "datastream_execution_step_evidence" in module_source
    assert "ON CONFLICT (execution_id, step) DO NOTHING" in module_source


def test_the_workbench_renders_temporals_through_the_safe_encoder() -> None:
    """The progress columns are date-like; the stock JSONResponse raises on them."""
    from core import datastream_workbench_api

    source = inspect.getsource(datastream_workbench_api)
    assert "from core.json_encoding import SafeJSONResponse as JSONResponse" in source


# ---------------------------------------------------------------------------
# Story 63.1 -- what a nightly run must NOT do to the Workbench.
# ---------------------------------------------------------------------------
#
# From 63.1 the latest execution of a scheduled Datastream is its nightly
# retrieval, every night. Four readers assumed the latest run was a publication
# candidate, and each broke differently the moment that stopped being true.


def _axis(**record):
    from core.datastream_workbench import _operations_axis

    return _operations_axis({"schedule_state_known": True, **record})


def test_health_is_read_from_the_last_run_that_finished():
    """A pull in flight says nothing about health, and must not erase it.

    `latest_run_state` is `loading` for the whole duration of every nightly
    run. Driving the axis from it would flip a Healthy Datastream to `Unknown`
    every night, for the length of its own pull.
    """
    assert _axis(latest_run_state="loading", latest_terminal_run_state="published") == "Healthy"
    assert _axis(latest_run_state="loading", latest_terminal_run_state="failed") == "Blocked"
    # Nothing has ever finished: Unknown is the honest answer, not Healthy.
    assert _axis(latest_run_state="loading") == "Unknown"
    assert _axis() == "Unknown"


def test_every_terminal_state_maps_to_a_ratified_axis_value():
    """Named by no state: a new one without an axis reddens here."""
    from core import execution_states

    ratified = {"Unknown", "Healthy", "Degraded", "Stale", "Blocked"}
    for state in execution_states.STORED_STATES:
        assert _axis(latest_terminal_run_state=state) in ratified, state


def test_a_finished_collection_is_healthy_not_unknown():
    """The one that would have hit EVERY scheduled Datastream from tomorrow."""
    assert _axis(latest_terminal_run_state="collected") == "Healthy"


def test_the_success_rate_counts_a_collection_as_a_success():
    """A run that collected its windows succeeded, even though it published nothing.

    Counting only `published` made the denominator include failed collections
    and exclude successful ones, so the 30-day rate of any Datastream that
    collects without publishing tended to 0%.
    """
    from core import execution_states
    from core.datastream_workbench import read_tab

    assert "collected" in execution_states.SUCCESS_STATES
    assert "collected" in execution_states.TERMINAL_STATES
    source = inspect.getsource(read_tab)
    # The KEY on the wire, not the word: the comment above it necessarily names
    # the old one, and a guard that trips on its own explanation is useless.
    assert '"succeeded_count":' in source
    assert '"published_count":' not in source


def test_a_run_in_flight_is_not_offered_as_a_candidate_to_review():
    """A nightly collection publishes nothing: nobody is asked to review it."""
    from core.datastream_workbench import _read_base_record
    from core.execution_progress import COLLECTION_PLAN_KIND

    source = inspect.getsource(_read_base_record)
    assert "candidate_states" in source
    assert "collection_kind" in source
    assert COLLECTION_PLAN_KIND == "recurring_collection"


def test_the_run_list_is_bounded_and_says_so():
    """63.1 grows this table by one row per Datastream per night."""
    from core.datastream_workbench import RUN_LIST_LIMIT, read_tab

    assert RUN_LIST_LIMIT == 200
    source = inspect.getsource(read_tab)
    assert "LIMIT %s" in source
    # The cap is on the wire: a silently truncated list reads as a full history.
    assert '"runs_limit"' in source
    assert '"runs_truncated"' in source
    # AMENDED 2026-08-18: and the cap now has a door. `runs_truncated` said there
    # was more and gave nobody a way to it, which is the half of this contract
    # that was missing for a year.
    assert '"runs_next_cursor"' in source
    assert '"runs_matching"' in source


# ---------------------------------------------------------------------------
# AMENDED 2026-08-18 -- THE NARROWING IS THE SERVER'S, AND THE PAGE IS WALKABLE.
#
# The ratified sentence « le filtre d'état est côté client ... sans `?state=` sur
# une route partagée » described a tab drawing one uncapped list. Applied to a
# CAPPED one it produced a filter over a sample: `failed` answered "no run in
# this state" on a Datastream whose last failure sat below the cut. These prove
# the predicate is composed once, parameterised, and shared by the page and the
# count -- two spellings of it is how "12 runs match" ends up above eleven rows.
# ---------------------------------------------------------------------------


def test_a_narrowing_is_parameterised_and_never_interpolated():
    from core.datastream_workbench import _run_where

    where, params = _run_where(
        "proj_1", "ds_1", {"state": "failed", "q": "'; DROP TABLE app.datastreams --"}
    )
    assert "DROP TABLE" not in where
    assert where.count("%s") == len(params)
    # The search reaches the two strings somebody arrives holding, and no third.
    assert "id ILIKE %s OR error_code ILIKE %s" in where
    assert params[-1] == params[-2] == "%'; DROP TABLE app.datastreams --%"


def test_the_upper_bound_of_a_date_range_includes_its_own_day():
    """`to = the 17th` means the whole of the 17th, not up to its midnight.

    `created_at < '2026-08-17'` would drop every run of the day a person named,
    which is the day they are asking about.
    """
    from core.datastream_workbench import _run_where

    where, _ = _run_where("proj_1", "ds_1", {"to": "2026-08-17"})
    assert "created_at < (%s::date + 1)" in where


def test_an_empty_narrowing_is_the_same_request_as_an_absent_one():
    """A console clearing a field sends `""` and means "do not narrow"."""
    from core.datastream_workbench import _run_filters

    assert _run_filters({"state": "", "q": "   ", "origin": None}) == {}
    assert _run_filters(None) == {}
    assert _run_filters({"state": " failed "}) == {"state": "failed"}


def test_the_page_and_the_count_read_one_predicate():
    """One composer, two statements -- and the cursor belongs to the page alone.

    The count answers "how many match", which is a question about the COLLECTION;
    adding the cursor to it would make it answer "how many are left below where I
    am standing", and the screen would report a total that shrank as somebody
    paged.
    """
    from core.datastream_workbench import read_tab

    source = inspect.getsource(read_tab)
    assert "page_where, page_params = where, list(params)" in source
    # The count reads `where`, never `page_where`: the cursor belongs to the page.
    assert 'f"SELECT COUNT(*) FROM app.datastream_executions WHERE {where}"' in source
    assert 'WHERE {page_where}' in source


def test_the_page_is_keyset_paged_on_the_id_and_reads_one_row_over():
    """`id DESC` is creation order (`dse_` + ULID, migration 042 CHECKs it).

    An OFFSET on the one table that grows a row every night AT THE TOP would skip
    or repeat a run at every page boundary; a keyset cannot. And "is there a next
    page" is answered by reading one row past the limit rather than by a second
    count that could disagree with the page it describes.
    """
    from core.datastream_workbench import read_tab

    source = inspect.getsource(read_tab)
    assert "ORDER BY id DESC LIMIT %s" in source
    # The word appears in the comment that says why it is refused, and nowhere
    # in a statement.
    assert "OFFSET %s" not in source
    assert "RUN_LIST_LIMIT + 1" in source
    assert "has_more = len(runs) > RUN_LIST_LIMIT" in source


def test_the_chips_are_read_from_the_collection_and_not_from_the_page(monkeypatch):
    """A state present on page three must have a chip on page one.

    Derived from the rows in hand, the chip that could bring somebody back to a
    state appears only once they have already reached it -- and clearing it is
    then the only way back. The two DISTINCT reads are UNFILTERED on purpose:
    "which states exist" is a different question from "which states survived the
    current narrowing".
    """
    from core.datastream_workbench import read_tab

    source = inspect.getsource(read_tab)
    assert "DISTINCT state FROM app.datastream_executions" in source
    assert '"run_states_present"' in source
    assert '"run_origins_present"' in source
    # Unfiltered: the two reads take the scope and nothing else.
    assert (
        """SELECT DISTINCT state FROM app.datastream_executions
                    WHERE project_id=%s AND datastream_id=%s"""
        in source
    )
    del monkeypatch


# ---------------------------------------------------------------------------
# Story 63.7 -- the Runs list says WHY each run exists, and nothing more of the
# projection plan than that.
# ---------------------------------------------------------------------------

# THE FOUR STATEMENTS THE RUNS BRANCH MAKES. Named here rather than spelled out
# inside `execute`, so the reader can see the whole inventory at once -- and so a
# fifth read added to the branch cannot be answered by accident (AI-317).
_RUNS = StatementInventory(
    "_RunsCursor",
    total=("from app.datastream_executions", "count(*)"),
    states=("from app.datastream_executions", "distinct state"),
    origins=("from app.datastream_executions", "distinct projection_plan_ref"),
    page=("from app.datastream_executions", "order by id desc"),
)


class _RunsCursor:
    """Answers the four statements the runs branch makes, and REFUSES any other.

    AMENDED 2026-08-18. It used to answer one: the page of runs. Since the tab
    was paged and narrowed the branch also asks HOW MANY match over the whole
    collection, and which states and origins EXIST on it -- the last two being
    what keeps a chip from appearing and disappearing as somebody pages. Each
    statement is recognised by its own SQL rather than by a call counter, so a
    reordering of the branch cannot silently feed one answer to another question.

    AMENDED 2026-08-25 (AI-317). Two lies removed. (1) The projection was
    restated here as a 21-name tuple, which is a second copy of the SELECT and
    the copy nothing reads -- it is now DERIVED from the statement, so a column
    added to the branch reaches the fixture by itself. (2) The `else` answered
    the run page to anything it did not recognise, including a statement this
    fake was never taught; it now raises with the SQL in the message.
    """

    def __init__(self, runs):
        # Runs are held BY COLUMN NAME, never as a positional tuple: the order
        # of the product's projection is the product's business.
        self._runs = runs
        self._answer: list[tuple] = []
        self._one = None
        self.description = None
        self.executed: list[str] = []

    def execute(self, sql, params=None):  # noqa: ARG002
        self.executed.append(sql)
        self._one = None
        statement = _RUNS.match(sql)
        if statement == "page":
            self.description = describe(sql)
            self._answer = [
                tuple(run.get(name) for (name,) in self.description) for run in self._runs
            ]
        elif statement == "total":
            self.description = [("count",)]
            self._answer = []
            self._one = (len(self._runs),)
        elif statement == "states":
            self.description = [("state",)]
            self._answer = [(run.get("state"),) for run in self._runs]
        else:  # origins -- what the collection HAS, unfiltered by the page
            self.description = [("origin",)]
            self._answer = sorted(
                {
                    (plan or {}).get("origin")
                    for plan in (run.get("projection_plan_ref") for run in self._runs)
                    if isinstance(plan, dict) and plan.get("origin")
                }
            )
            self._answer = [(origin,) for origin in self._answer]

    def fetchall(self):
        return self._answer

    def fetchone(self):
        return self._one

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RunsConnection:
    def __init__(self, rows):
        self._cursor = _RunsCursor(rows)

    def cursor(self):
        return self._cursor


def _runs_payload(
    monkeypatch, plans, *, extra=None, spans=None, anomalies=None, without_run=0
):
    """The whole `runs` tab payload, over executions carrying the given plans."""
    from core import datastream_workbench as workbench

    rows = []
    for index, plan in enumerate(plans):
        values = {"id": f"dse_EXAMPLE_{index}", "state": "loading", "projection_plan_ref": plan}
        values.update((extra or {}).get(index, {}))
        rows.append(values)

    monkeypatch.setattr(workbench, "_read_base_record", lambda *a, **k: {"id": "ds_1"})
    monkeypatch.setattr(workbench, "_recovery_options", lambda *a, **k: {"eligible": False})
    monkeypatch.setattr(workbench, "_phase_evidence", lambda *a, **k: [])
    # Story 58.10: the two reads the runs branch now makes beside the run list.
    # Stubbed here for the same reason the two above are -- this fake cursor
    # answers the executions SELECT and nothing else.
    monkeypatch.setattr(workbench, "_step_evidence", lambda *a, **k: spans or {})
    # Story 59.1 widened this reader: it answers the per-run entries AND the
    # Datastream-grain count of the evaluations that could name no run.
    monkeypatch.setattr(
        workbench,
        "_run_anomalies",
        lambda *a, **k: {"runs": anomalies or {}, "evaluations_without_run": without_run},
    )
    return workbench.read_tab(
        _RunsConnection(rows), project_id="proj_1", datastream_id="ds_1", tab="runs"
    )["evidence"]


def _runs_evidence(monkeypatch, plans, **kwargs):
    """The `runs` list of that payload."""
    return _runs_payload(monkeypatch, plans, **kwargs)["runs"]


def test_the_runs_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317: the fixture's own guarantee, asserted rather than assumed.

    Every branch of `_RunsCursor` recognises its statement by a fragment of the
    product's SQL, so any rewrite of the runs branch can stop matching. When it
    did, the old `else` answered the run page -- and the suite went on proving
    the paging, the chips and the origins against a query that no longer ran.
    """
    cursor = _RunsCursor([])
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute("SELECT id FROM app.datastream_execution_stage_evidence")
    # The statement is in the message, and so is what this fake does know.
    assert "app.datastream_execution_stage_evidence" in str(raised.value)
    assert "origins" in str(raised.value)


def test_the_runs_fake_reports_the_columns_the_product_asked_for() -> None:
    """The description is DERIVED: a column added to the branch needs no edit here."""
    cursor = _RunsCursor([{"id": "dse_EXAMPLE_0", "state": "loading"}])
    cursor.execute(
        "SELECT id,state,made_up_column FROM app.datastream_executions "
        "WHERE project_id=%s ORDER BY id DESC LIMIT %s"
    )
    assert cursor.description == [("id",), ("state",), ("made_up_column",)]
    assert cursor.fetchall() == [("dse_EXAMPLE_0", "loading", None)]


def test_each_run_of_the_list_says_which_path_created_it(monkeypatch) -> None:
    """A tab that lists treatments without their origin lists anomalies.

    Measured 2026-08-06: `datastream_workbench` read `projection_plan_ref` for
    the recovery interval and then popped it off the payload, so the ONE path
    that already named itself (`open_collection_run`) never reached a screen
    either.
    """
    from core import run_origins

    runs = _runs_evidence(
        monkeypatch,
        [
            {"executable": True, "origin": run_origins.SCHEDULER_NIGHTLY},
            {"executable": True, "origin": run_origins.MAPPING_CHANGE},
            # A run minted before this story: no origin was ever recorded, and
            # nothing can work out afterwards which path created it.
            {"executable": True},
        ],
    )

    assert [run["origin"] for run in runs] == [
        run_origins.SCHEDULER_NIGHTLY,
        run_origins.MAPPING_CHANGE,
        None,
    ]


def test_the_runs_list_never_carries_the_projection_plan_itself(monkeypatch) -> None:
    """One derived field leaves; the plan does not.

    The plan holds the compiled projection, the recovery scope and the retained
    execution reference. Story 63.7 adds a reason to read it and does not widen
    what is published because of that.
    """
    from core import run_origins

    runs = _runs_evidence(
        monkeypatch, [{"executable": True, "origin": run_origins.PLAN_CHANGE,
                       "retained_execution_id": "dse_SECRET"}]
    )

    assert "projection_plan_ref" not in runs[0]
    assert "dse_SECRET" not in repr(runs[0])
    assert runs[0]["origin"] == run_origins.PLAN_CHANGE


def test_an_origin_the_build_does_not_know_is_not_replaced(monkeypatch) -> None:
    """A server ahead of this build is shown as it answered, never folded."""
    from core import run_origins

    runs = _runs_evidence(monkeypatch, [{"origin": "an_origin_no_build_knows"}])
    assert runs[0]["origin"] == "an_origin_no_build_knows"
    assert run_origins.label_for(runs[0]["origin"]) is None


# ---------------------------------------------------------------------------
# Story 58.10 -- the run's OWN reading: its duration, the span of each of its
# four steps, and the anomalies found on it.
# ---------------------------------------------------------------------------


def test_the_four_steps_travel_with_the_payload_and_are_not_the_seven_phases(
    monkeypatch,
) -> None:
    """The console keeps no second list of step names.

    `datastream-workbench-and-wizard.md` ratifies that the four steps and the
    seven phases of `app.datastream_execution_phase_evidence` are two
    vocabularies and stay two. The payload therefore carries the four, from
    `execution_progress.PROGRESS_STEPS`, and never the seven folded onto them.
    """
    from core.datastream_workbench import _PHASES
    from core.execution_progress import PROGRESS_STEPS

    evidence = _runs_payload(monkeypatch, [{"executable": True}])
    assert evidence["step_vocabulary"] == list(PROGRESS_STEPS)
    assert set(evidence["step_vocabulary"]).isdisjoint(_PHASES)


def test_a_run_carries_the_span_of_each_step_it_entered(monkeypatch) -> None:
    """The track's numbers, on the payload, with the running step still open."""
    runs = _runs_evidence(
        monkeypatch,
        [{"executable": True}],
        spans={
            "dse_EXAMPLE_0": [
                {
                    "step": "Collect",
                    "started_at": "T0",
                    "ended_at": "T1",
                    "duration_seconds": 120.0,
                },
                # Still running: `ended_at` is NULL and so is the duration. A
                # `0` here would print `0 s` under a step that is working.
                {"step": "Map", "started_at": "T1", "ended_at": None, "duration_seconds": None},
            ]
        },
    )
    assert [span["step"] for span in runs[0]["steps"]] == ["Collect", "Map"]
    assert runs[0]["steps"][0]["duration_seconds"] == 120.0
    assert runs[0]["steps"][1]["ended_at"] is None
    assert runs[0]["steps"][1]["duration_seconds"] is None


def test_an_open_span_is_running_only_while_the_run_is(monkeypatch) -> None:
    """The read-side guarantee -- second review, and it covers every writer.

    An open span means "still working" ONLY while the run itself is running.
    When this was written, `commit_publication` and `advance_state` closed a
    span while `datastream_activation`, `reconcile_execution` and
    `_reconcile_fail_closed` set a terminal state with their own UPDATE and
    closed nothing -- there was no seam they all crossed. AI-223 built the seam
    (every writer now calls `advance_state`), and the derivation stays: it
    answers for the runs written before it, and for a crash between the state
    write and the close. The answer is derived HERE, from the state, where no
    writer can bypass it.
    """
    from core import execution_states

    open_span = [{"step": "Collect", "started_at": "T0", "ended_at": None,
                  "duration_seconds": None}]
    runs = _runs_evidence(
        monkeypatch,
        [{"executable": True}, {"executable": True}],
        extra={0: {"state": "published"}, 1: {"state": "loading"}},
        spans={"dse_EXAMPLE_0": list(open_span), "dse_EXAMPLE_1": list(open_span)},
    )

    assert execution_states.is_terminal("published") is True
    assert execution_states.is_terminal("loading") is False
    # Same span, same missing end -- two different answers, and the state is
    # what decides. `False` is what makes the screen say "entered, not timed".
    assert runs[0]["steps"][0]["still_running"] is False
    assert runs[1]["steps"][0]["still_running"] is True
    # And neither invents a duration.
    assert runs[0]["steps"][0]["duration_seconds"] is None
    assert runs[1]["steps"][0]["duration_seconds"] is None


def test_a_run_with_no_span_carries_an_absence_and_never_four_zeroes(monkeypatch) -> None:
    """319 of 428 runs on the disposable base have no span at all.

    The empty list is what lets the screen say "no step of this run was timed".
    Four entries at `0` would be four measurements nobody made.
    """
    runs = _runs_evidence(monkeypatch, [{"executable": True}])
    assert runs[0]["steps"] == []
    assert all(span != 0 for span in runs[0]["steps"])


def test_the_duration_is_measured_only_when_both_ends_exist(monkeypatch) -> None:
    """Arbitrage 3: `state_changed_at - started_at`, on 103 runs of 428."""
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    runs = _runs_evidence(
        monkeypatch,
        [{"executable": True}, {"executable": True}, {"executable": True}],
        extra={
            0: {"started_at": start, "state_changed_at": start + timedelta(minutes=4)},
            # Started and never ended -- still running, not "took no time".
            1: {"started_at": start, "state_changed_at": None},
            # Minted before migration 218: no `started_at` was ever written.
            2: {"started_at": None, "state_changed_at": start},
        },
    )
    assert runs[0]["duration_seconds"] == 240.0
    assert runs[1]["duration_seconds"] is None
    assert runs[2]["duration_seconds"] is None


def test_every_run_says_how_many_anomalies_were_found_on_it(monkeypatch) -> None:
    """Two counts, because "not checked" and "checked and clean" are two facts.

    Readable at all because migration 222 gave `app.dq_issues` and
    `app.dq_evaluations` an `execution_id`. Both tables hold 0 rows on preprod
    AND on the disposable base, so today every run reads the first of the two --
    which the payload says rather than fills.
    """
    runs = _runs_evidence(
        monkeypatch,
        [{"executable": True}, {"executable": True}],
        anomalies={"dse_EXAMPLE_0": {"anomalies": 2, "evaluations": 3, "issues": []}},
    )
    assert runs[0]["anomalies"] == {"anomalies": 2, "evaluations": 3, "issues": []}
    # A run nothing evaluated: zero anomalies AND zero evaluations, which the
    # screen turns into "no monitor has been evaluated on this run".
    assert runs[1]["anomalies"] == {"anomalies": 0, "evaluations": 0, "issues": []}


def test_the_evaluations_that_could_name_no_run_are_counted_once_on_the_flux(
    monkeypatch,
) -> None:
    """Story 59.1, arbitrage 7 -- and the grain is the whole point.

    An evaluation with `execution_id IS NULL` belongs to NO run, so "no monitor
    has been evaluated on this run" stays literally true of every run. Rendering
    that evaluation under one of them would attribute to a collection something
    that named none. It travels at the grain it has: the Datastream's, once.
    """
    evidence = _runs_payload(
        monkeypatch, [{"executable": True}], anomalies={}, without_run=4
    )
    assert evidence["evaluations_without_run"] == 4
    assert all(run["anomalies"]["evaluations"] == 0 for run in evidence["runs"])


def test_the_anomaly_read_is_bound_to_the_run_and_to_the_project(monkeypatch) -> None:
    """The columns migration 222 added are the ones this reads -- both of them."""
    import inspect

    from core.datastream_workbench import _run_anomalies

    source = inspect.getsource(_run_anomalies)
    assert "dq_issues" in source and "dq_evaluations" in source
    assert "execution_id IS NOT NULL" in source
    assert "project_id=%s AND datastream_id=%s" in source


# ---------------------------------------------------------------------------
# Story 58.9 -- the FIVE capabilities travel on the header, and two of them can
# carry no switch because the database refuses to turn them off.
# ---------------------------------------------------------------------------


class _CapabilityCursor:
    """A cursor that answers `app.project_capabilities` and counts its statements.

    The count is the point: the header of this surface is loaded by every tab, so
    a reader that costs one round trip per capability costs five on the screen a
    person opens most.
    """

    def __init__(self, rows):
        self._rows = rows
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        self._params = params

    def fetchall(self):
        keys = set(self._params[1])
        return [row for row in self._rows if row[0] in keys]


class _CapabilityConnection:
    def __init__(self, rows):
        self.cursor_object = _CapabilityCursor(rows)

    def cursor(self):
        return self.cursor_object


def test_the_header_carries_the_seven_capabilities_and_the_tabs_they_open() -> None:
    from core.datastream_workbench import CAPABILITY_TABS, _capability_tabs
    from core.project_capability_states import PROJECT_CAPABILITY_KEYS

    # The registry, not a copy of it: the keys are the CHECK of migration 131 as
    # migration 243 extended it.
    assert [key for key, _tab in CAPABILITY_TABS] == list(PROJECT_CAPABILITY_KEYS)

    connection = _CapabilityConnection(
        [
            ("country", "ready", "optional"),
            ("currency_fx", "draft", "always_present"),
            ("reporting_timezone", "draft", "always_present"),
            ("tax_fees", "degraded", "optional"),
            ("competitors", "disabled", "optional"),
            ("placement_mapping", "disabled", "optional"),
            ("analytics_alignment", "disabled", "optional"),
        ]
    )
    tabs = _capability_tabs(connection, "proj_EXAMPLE")

    assert [entry["capability_key"] for entry in tabs] == list(PROJECT_CAPABILITY_KEYS)
    # TWO capabilities name a tab, and only those two. The other five say `None`
    # rather than being absent: "this capability adds no tab" is an answer the
    # screen prints, and an absent key would make it guess. `analytics_alignment`
    # is the newest of them -- story 70.3 adds three COLUMNS, not a tab.
    assert [entry["tab"] for entry in tabs] == [
        None, None, None, "cost", None, "placements", None,
    ]
    # `ready` and `degraded` are both active -- hiding evidence already collected
    # is worse than showing it diminished.
    assert [entry["open"] for entry in tabs] == [
        True, False, False, True, False, False, False,
    ]
    # The two the database refuses to disable carry their availability, which is
    # what tells a screen it may not draw a switch at all.
    assert {
        entry["capability_key"]: entry["availability"] for entry in tabs
    } == {
        "country": "optional",
        "currency_fx": "always_present",
        "reporting_timezone": "always_present",
        "tax_fees": "optional",
        "competitors": "optional",
        "placement_mapping": "optional",
        "analytics_alignment": "optional",
    }
    # ONE statement for the seven.
    assert len(connection.cursor_object.statements) == 1


def test_a_project_the_control_plane_never_wrote_reads_unset_not_disabled() -> None:
    """`unset` is not a decision anybody took, and `disabled` is.

    `app.seed_project_capabilities` writes one row per declared capability --
    seven since migration 309 -- so a missing row means the seed never ran.
    Reporting it as `disabled` would present the absence of the control plane as
    an operator's choice, including for `analytics_alignment`, whose row a
    Project created before 309 only receives from that migration's backfill.
    """
    from core.datastream_workbench import _capability_tabs
    from core.project_capability_states import (
        CAPABILITY_AVAILABILITY,
        PROJECT_CAPABILITY_KEYS,
    )

    tabs = _capability_tabs(_CapabilityConnection([]), "proj_EXAMPLE")

    # DERIVED, not spelled: a count written as a literal has to be edited by
    # every story that declares a capability, and the one that forgets makes this
    # test assert a cardinality nobody holds.
    assert len(tabs) == len(PROJECT_CAPABILITY_KEYS)
    assert {entry["state"] for entry in tabs} == {"unset"}
    assert all(entry["open"] is False for entry in tabs)
    # Availability still comes from the pairing CHECK, which is what the database
    # will hold the moment the seed runs.
    assert [entry["availability"] for entry in tabs] == [
        CAPABILITY_AVAILABILITY[key] for key in PROJECT_CAPABILITY_KEYS
    ]


def test_the_check_stage_reads_placed_monitors_and_never_stage_coverage() -> None:
    """Story 58.9, arbitrage 4 -- the object placed, not the evidence table.

    `app.datastream_execution_stage_evidence` is empty across preprod, so a stage
    coloured from it printed `0 / 4` for every Datastream. The `Check` stage reads
    a count of `app.dq_monitors` targeting this Datastream instead, and that count
    is a payload key rather than a route.
    """
    import inspect

    from core import datastream_workbench

    source = inspect.getsource(datastream_workbench.read_tab)
    assert "app.dq_monitors" in source
    assert "target_kind='datastream'" in source
    assert '"dq_monitors": {' in source


# ---------------------------------------------------------------------------
# Story 59.2 -- the header badge, on the same predicate as the fleet's
# ---------------------------------------------------------------------------


def test_the_header_count_is_the_fleets_own_reader_and_not_a_second_query() -> None:
    """One reader, two mounts. Two `SELECT`s would diverge at the first clause.

    `read_workbench` and `data_surface._merge_open_issues` both go through
    `dq_governance.read_open_issue_counts`, with the same
    `include_acknowledged=False` question. A header that asked it differently
    would show one number on the list and another on the object -- for the same
    flux, in the same second.

    AND NEITHER OF THEM SWALLOWS THE ERROR ITSELF. The fail-soft is one function,
    because it is not a `try/except` -- it is a SAVEPOINT and a rollback to it,
    and a caller that wrote its own `except` would leave the transaction aborted
    for whatever it read next.
    """
    import re

    from core import data_surface, datastream_workbench

    for module in (datastream_workbench, data_surface):
        source = inspect.getsource(module)
        assert source.count("read_open_issue_counts(") == 1, module.__name__
        assert "include_acknowledged=False" in source, module.__name__
        # The raw reader is reached through the fail-soft one, never directly.
        assert "open_issue_counts_by_datastream(" not in source, module.__name__
        # No copy of the predicate, and no private swallow, in either reader.
        assert not re.search(r"status\s*NOT\s*IN\s*\('closed'", source), module.__name__
        assert "except Exception" not in source, module.__name__


def test_the_header_badge_is_independent_of_the_run_that_found_the_issue() -> None:
    """`_run_anomalies` stays per-run, and this story neither reads nor changes it.

    Its `WHERE` is the exact predicate of `idx_dq_issues_execution` (migration
    222), which is partial on `execution_id IS NOT NULL`. The badge counts the
    issues that name no run -- the majority case -- so deriving one from the
    other would silently report zero on most Datastreams.
    """
    from core.datastream_workbench import _open_issue_summary, _run_anomalies

    per_run = inspect.getsource(_run_anomalies)
    assert "execution_id IS NOT NULL" in per_run
    assert per_run.count("FROM app.dq_issues") == 1

    badge = inspect.getsource(_open_issue_summary)
    assert "execution_id" not in badge.split('"""')[-1]
    assert "FROM app.dq_issues" not in badge


def test_a_header_whose_issue_store_cannot_be_read_carries_no_count(monkeypatch) -> None:
    """The FOURTH state on the header too: an absent key, never a `0`.

    The Workbench header is loaded by all six tabs; taking it down because one
    aggregate failed would blank the object. What it must not do is answer `0`,
    which the console would render as a clean bill of health nobody measured.
    """
    from core import datastream_workbench

    def _explode(*_args, **_kwargs):
        raise RuntimeError("issue store unavailable")

    monkeypatch.setattr(
        datastream_workbench, "compose_header", lambda record: {"identity": dict(record)}
    )
    monkeypatch.setattr(datastream_workbench, "_read_base_record", lambda *a: {"id": "ds_1"})
    monkeypatch.setattr(datastream_workbench, "_capability_tabs", lambda *a: [])
    monkeypatch.setattr("core.dq_governance.open_issue_counts_by_datastream", _explode)

    header = datastream_workbench.read_workbench(
        MagicMock(), project_id="proj_EXAMPLE", datastream_id="ds_1"
    )
    assert "open_issues" not in header


def test_a_datastream_the_aggregate_never_named_carries_a_measured_zero(monkeypatch) -> None:
    """Absent from the grouped result means "nothing watches it", not "unknown".

    The grouped read returns a row per Datastream that carries a monitor or an
    issue; every other flux of the Project is a MEASURED zero, and the reader
    supplies it here rather than letting the key go missing -- because a missing
    key is the state above and the two must never render for each other.
    """
    from core import datastream_workbench

    monkeypatch.setattr(
        "core.dq_governance.open_issue_counts_by_datastream",
        lambda *a, **k: {"ds_other": {"monitored": True, "count": 3}},
    )
    summary = datastream_workbench._open_issue_summary(MagicMock(), "proj_EXAMPLE", "ds_1")

    assert summary == {
        "monitored": False,
        "count": 0,
        "by_severity": {"blocking": 0, "degrading": 0, "informational": 0},
        "highest_severity": None,
        "faulty_execution_id": None,
    }


def test_the_header_and_the_fleet_agree_on_the_same_seeded_flux(pg_conn, dq_fleet) -> None:
    """The same object, the same numbers, read through the two real entry points.

    Seeded because `app.dq_issues` is 0 on both databases: the "watched, nothing
    open" case is reachable on preprod as it stands, and a count with a severity
    exists nowhere until a writer makes one.
    """
    from core.data_surface import compose_data_surface
    from core.datastream_workbench import read_workbench

    monitor_id = publish_dq_monitor(pg_conn, dq_fleet, dq_fleet["watched"])
    open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="degrading", seed="header-a")
    open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="blocking", seed="header-b")

    header = read_workbench(
        pg_conn, project_id=dq_fleet["project_id"], datastream_id=dq_fleet["watched"]
    )
    envelope = compose_data_surface(dq_fleet["project_id"], "datastreams", pg_conn)
    fleet = {item["object_ref"]["id"]: item for item in envelope["items"]}

    assert header["open_issues"] == fleet[dq_fleet["watched"]]["evidence"]["open_issues"]
    assert header["open_issues"]["count"] == 2
    assert header["open_issues"]["highest_severity"] == "blocking"

    # And the Datastream nobody watches carries the third sentence on both.
    unwatched = read_workbench(
        pg_conn, project_id=dq_fleet["project_id"], datastream_id=dq_fleet["bare"]
    )
    assert unwatched["open_issues"] == fleet[dq_fleet["bare"]]["evidence"]["open_issues"]
    assert unwatched["open_issues"]["monitored"] is False


# ---------------------------------------------------------------------------
# THE SENTENCE THAT SAYS NO SAMPLE CAN BE DRAWN -- finding D-7 of the visual
# review #69, ratified in the `Data` cell of
# `docs/product-architecture/datastream-workbench-and-wizard.md`.
#
# What a person read on that tab was « No governed sample endpoint is mounted.
# Story 47.5 retired the consolidated one because it aliased stages and bound a
# sample to no exact version; its replacement is not delivered. » — a story
# number, a route and a deployment state. What replaced it here says two things
# and no third: what cannot be done on this tab, and where the reading that DOES
# exist is found instead.
# ---------------------------------------------------------------------------

#: Words that belong to this repository and to nobody reading the console.
_REPO_WORDS = (
    "story",
    "endpoint",
    "mart",
    "mounted",
    "delivered",
    "warehouse",
    "keyed by provider",
    "declares no connector",
)


def _data_tab(monkeypatch, *, source_kind: str, module_name=None) -> dict:
    """The `data` branch of `read_tab`, over a base record and no stage rows."""
    from core import datastream_workbench as workbench

    monkeypatch.setattr(
        workbench,
        "_read_base_record",
        lambda *a, **k: {"id": "ds_1", "source_kind": source_kind, "module_name": module_name},
    )
    monkeypatch.setattr(workbench, "_stage_evidence", lambda *a, **k: [])
    return workbench.read_tab(
        MagicMock(), project_id="proj_EXAMPLE", datastream_id="ds_1", tab="data"
    )["evidence"]


@pytest.mark.parametrize(
    ("source_kind", "where_the_reading_lives"),
    [
        ("managed_feed", "the last file that arrived, above"),
        ("external_bq", "its columns are read on Mapping"),
        ("connector_pull", "the day-by-day reading above"),
    ],
)
def test_a_refused_sample_costs_one_sentence_naming_where_the_reading_lives(
    monkeypatch, source_kind: str, where_the_reading_lives: str
) -> None:
    reason = _data_tab(monkeypatch, source_kind=source_kind)["sample_reason"]

    # One sentence: the em dash joins the two clauses, and no full stop closes a
    # first one before the end.
    assert reason.count(".") == 1 and reason.endswith(".")
    # What cannot be done here...
    assert reason.startswith("No sample and no export can be drawn here")
    # ...and where the reading that does exist is found.
    assert where_the_reading_lives in reason
    for word in _REPO_WORDS:
        assert word not in reason.lower(), f"{word!r} reached the screen"


def test_a_reachable_sample_carries_no_deployment_state(monkeypatch) -> None:
    """`reachable` renders the sample itself, so there is nothing to say.

    The key used to answer « Governed sample endpoint is mounted. » — a sentence
    about a deployment, on the wire of a user-facing tab.
    """
    evidence = _data_tab(monkeypatch, source_kind="connector_pull", module_name="meta-ads")

    assert evidence["sample_state"] == "reachable"
    assert evidence["sample_reason"] is None


# ---------------------------------------------------------------------------
# THE NARROWING AND THE CURSOR, OVER REAL ROWS -- amended 2026-08-18.
#
# Everything above proves the SQL is COMPOSED correctly by reading the source.
# That is worth having and it is not enough: a predicate can be composed
# perfectly and still be invalid Postgres, or bind its parameters in the wrong
# order. These run the statements the runs branch actually issues.
#
# SEEDED, AND THE SEED IS THE MEASUREMENT THAT FORCED IT: `app.project_flux`
# holds ZERO rows on the disposable base, and `_read_base_record` INNER JOINs it
# -- so not one of the 33 executions already in that cluster is reachable through
# the Workbench. Reading them would have proved nothing at all, and a test that
# quietly skips on an empty table is a test nobody notices going silent.
# ---------------------------------------------------------------------------


def _run_id(index: int) -> str:
    """`dse_` + 26 Crockford base32 (migration 042 CHECKs it), ASCENDING.

    The id IS the order the page is keyset on, so the seed's ids have to ascend
    with their instants exactly as a ULID's do. A random ULID per row would make
    "newest first" untestable without also asserting what a ULID is.
    """
    return "dse_" + "0" * 20 + f"{index:06d}"


def _seeded_runs(conn):
    """One Project, one Datastream, and FIVE runs a person could narrow between.

    Written through plain INSERTs rather than through the collection path on
    purpose: what is under test is a READ, and `open_collection_run` can mint at
    most one non-terminal execution per Datastream
    (`uq_datastream_executions_active`). Five runs across four states is exactly
    the history this tab exists to be read against, and no writer in the product
    produces it in one transaction.

    Nothing is committed -- `pg_conn` rolls back.
    """
    import uuid

    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    ds_id = f"ds_{suffix}"
    plan_id = f"dsp_{suffix}"
    mapping_id = f"dmap_{suffix}"

    runs = [
        (1, "published", "scheduler_nightly", "2026-08-01", None),
        (2, "failed", "scheduler_nightly", "2026-08-02", "collection_window_failed"),
        (3, "published", "refetch", "2026-08-03", None),
        (4, "cancelled", "refetch", "2026-08-04", None),
        (5, "loading", None, "2026-08-05", None),
    ]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'owner@example.com')",
            (org_id, f"Org {suffix}", f"org-{suffix}"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'owner@example.com', %s)",
            (project_id, f"Project {suffix}", f"project-{suffix}", org_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, module_name, source_kind, enabled, created_by) "
            "VALUES (%s, %s, %s, 'Run history', 'example_connector', 'connector_pull', "
            "TRUE, 'owner@example.com')",
            (ds_id, project_id, org_id),
        )
        # Without this row `_read_base_record` answers "not found" and every
        # assertion below would be measuring the fixture instead of the query.
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) VALUES (%s, %s, %s)",
            (project_id, ds_id, org_id),
        )
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
                   (id, datastream_id, project_id, version_number, contract_version,
                    source_kind, writer_kind, destination_policy, normalized_payload,
                    content_hash, idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, '1', 'connector_pull', 'toorow', 'managed_raw',
                       '{}'::jsonb, repeat('a', 64), repeat('b', 64), 'test')""",
            (plan_id, ds_id, project_id),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                   (id, datastream_id, project_id, version_number, mapping_contract_version,
                    source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                    toorow_extension_version, executable, mapping_payload, ossie_projection,
                    idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',
                       '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('d', 64), 'test')""",
            (mapping_id, ds_id, project_id, plan_id),
        )
        for index, state, origin, created_at, error_code in runs:
            plan_ref = "{}" if origin is None else '{"origin": "' + origin + '"}'
            cur.execute(
                """INSERT INTO app.datastream_executions
                       (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                        projection_plan_ref, state, error_code, created_at, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::timestamptz, 'test')""",
                (
                    _run_id(index),
                    ds_id,
                    project_id,
                    plan_id,
                    mapping_id,
                    plan_ref,
                    state,
                    error_code,
                    created_at,
                ),
            )
    return {
        "project_id": project_id,
        "datastream_id": ds_id,
        "ids": [_run_id(index) for index, *_ in runs],
    }


def _runs_tab(conn, seed, **options):
    from core.datastream_workbench import read_tab

    return read_tab(
        conn,
        project_id=seed["project_id"],
        datastream_id=seed["datastream_id"],
        tab="runs",
        options=options or None,
    )["evidence"]


def test_the_whole_history_is_newest_first_and_says_how_many_there_are(pg_conn) -> None:
    seed = _seeded_runs(pg_conn)
    evidence = _runs_tab(pg_conn, seed)

    assert [run["id"] for run in evidence["runs"]] == sorted(seed["ids"], reverse=True)
    assert evidence["runs_matching"] == 5
    assert evidence["runs_truncated"] is False
    assert evidence["runs_next_cursor"] is None
    # The chips are the collection's, and the origin a run never carried is NOT
    # among them: `Not measured` is an absence, and no selector entry may reach
    # it as though it were a value.
    assert sorted(evidence["run_states_present"]) == [
        "cancelled", "failed", "loading", "published",
    ]
    assert sorted(evidence["run_origins_present"]) == ["refetch", "scheduler_nightly"]


def test_a_state_asked_of_the_server_narrows_the_collection_and_not_the_page(pg_conn) -> None:
    """The defect: a chip over a capped list narrowed a SAMPLE.

    `failed` answered "no run in this state" on a Datastream whose only failure
    sat below the cut. The narrowing is now a predicate on the whole collection,
    and the count says so.
    """
    seed = _seeded_runs(pg_conn)
    narrowed = _runs_tab(pg_conn, seed, state="failed")

    assert [run["state"] for run in narrowed["runs"]] == ["failed"]
    assert narrowed["runs_matching"] == 1
    # THE CHIPS DO NOT SHRINK WITH THE NARROWING. They say what exists, which is
    # what makes them the control that undoes it.
    assert sorted(narrowed["run_states_present"]) == [
        "cancelled", "failed", "loading", "published",
    ]
    assert narrowed["runs_filters"] == {"state": "failed"}


def test_an_origin_a_date_range_and_a_search_each_reach_the_rows_they_name(pg_conn) -> None:
    seed = _seeded_runs(pg_conn)

    by_origin = _runs_tab(pg_conn, seed, origin="refetch")
    assert [run["origin"] for run in by_origin["runs"]] == ["refetch", "refetch"]
    assert by_origin["runs_matching"] == 2

    # THE UPPER BOUND INCLUDES ITS OWN DAY. `created_at < '2026-08-03'` would
    # drop every run of the 3rd, which is a day somebody asking for it means.
    ranged = _runs_tab(pg_conn, seed, **{"from": "2026-08-02", "to": "2026-08-03"})
    assert [run["id"] for run in ranged["runs"]] == [seed["ids"][2], seed["ids"][1]]
    assert ranged["runs_matching"] == 2

    # The two strings a person arrives holding: a run id out of a ticket...
    by_id = _runs_tab(pg_conn, seed, q=seed["ids"][3][-8:])
    assert [run["id"] for run in by_id["runs"]] == [seed["ids"][3]]
    # ...and an error code out of an alert.
    by_code = _runs_tab(pg_conn, seed, q="window_failed")
    assert [run["id"] for run in by_code["runs"]] == [seed["ids"][1]]

    # A range that ends before any run answers NOTHING, and answers it as a
    # measurement: `0` matching is a true reading, not a failure.
    empty = _runs_tab(pg_conn, seed, **{"from": "1990-01-01", "to": "1990-01-02"})
    assert empty["runs"] == []
    assert empty["runs_matching"] == 0
    assert empty["run_states_present"]


def test_the_cursor_walks_down_without_skipping_or_repeating_a_run(pg_conn) -> None:
    """The keyset, exclusive, on the one table that grows a row AT THE TOP.

    An OFFSET would repeat or skip a run at every boundary the moment a nightly
    collection is minted mid-walk -- a person reading one run twice and believing
    it ran twice.
    """
    seed = _seeded_runs(pg_conn)
    whole = _runs_tab(pg_conn, seed)
    first = whole["runs"][0]["id"]

    below = _runs_tab(pg_conn, seed, cursor=first)
    assert [run["id"] for run in below["runs"]] == [run["id"] for run in whole["runs"][1:]]
    assert all(run["id"] < first for run in below["runs"])
    # THE COUNT IS THE COLLECTION'S, so it does not fall as somebody pages: a
    # total that shrank with every Next would be a second, wrong measurement.
    assert below["runs_matching"] == whole["runs_matching"] == 5
    assert below["runs_cursor"] == first


def test_a_page_boundary_says_there_is_more_and_names_where_it_starts(
    pg_conn, monkeypatch
) -> None:
    """`runs_truncated` said there was more and gave nobody a way to it.

    The limit is dropped to two so the boundary is reachable without seeding two
    hundred runs; what is under test is the boundary, not the number.
    """
    from core import datastream_workbench

    seed = _seeded_runs(pg_conn)
    monkeypatch.setattr(datastream_workbench, "RUN_LIST_LIMIT", 2)

    page = _runs_tab(pg_conn, seed)
    assert len(page["runs"]) == 2
    assert page["runs_truncated"] is True
    # The cursor is the LAST run of the page, so the next one starts below it.
    assert page["runs_next_cursor"] == page["runs"][-1]["id"]
    assert page["runs_matching"] == 5

    second = _runs_tab(pg_conn, seed, cursor=page["runs_next_cursor"])
    assert [run["id"] for run in second["runs"]] == [seed["ids"][2], seed["ids"][1]]
    assert second["runs_truncated"] is True

    third = _runs_tab(pg_conn, seed, cursor=second["runs_next_cursor"])
    assert [run["id"] for run in third["runs"]] == [seed["ids"][0]]
    # The last page says so, and the pager's Next goes dead rather than looping.
    assert third["runs_truncated"] is False
    assert third["runs_next_cursor"] is None

    # AND NO RUN WAS SEEN TWICE OR MISSED across the whole walk.
    walked = [run["id"] for page_ in (page, second, third) for run in page_["runs"]]
    assert walked == sorted(seed["ids"], reverse=True)
