"""Accepting out-of-plan spend as unplanned -- story 61.2, arbitrage A3 (a).

THE MODULE UNDER TEST IS `core/plan_spend_decisions.py`, and the name of this
file is the one story 61.2's test plan prescribed. The two differ on one word and
that word is the arbitrage: the plan (`epic-61:36`) calls the fourth state
`Unmapped source placement`, and A4 measured that `placement` wrong. The decision
is taken at the CAMPAIGN grain because `list_unmapped_actuals` returns
`{connector, campaign_ref, spend, reason}` -- the only shape of this reading that
carries an amount. `_read_observed_placements` beside it returns a `row_count`,
which is not money, so a decision offered per placement could not state what it
is about.

THE SIX THINGS THIS FILE EXISTS TO HOLD:

  * the acceptance is WRITTEN -- a row, keyed on (plan_id, connector,
    campaign_ref), with a reason and an author, because the 10 media-plan audit
    actions carried no acceptance and the out-of-plan reading is derived at every
    read, so there was nothing to decorate;
  * it is READ BACK on the tab, and an accepted row STAYS listed;
  * it is IDEMPOTENT, and the FIRST decision is the one that stands: a second
    click never rewrites who decided and when;
  * it is REFUSED with no reason, and refused outside the scope
    (plan_id, connector, campaign_ref) -- including a plan of another Project,
    which is refused as not found;
  * it is REFUSED when the capability is off, by the same reader the tab uses;
  * it changes NO amount -- proved against the two files that would have to
    change for it to.

In-memory doubles, on the pattern of `test_datastream_workbench_placements.py`:
what is proved here is who decides and what is refused.
"""

from __future__ import annotations

import pytest
from core.plan_matching_states import MATCHING_STATE_ACCEPTED
from core.plan_spend_decisions import (
    ACTION_MEDIA_PLAN_SPEND_ACCEPTED,
    MAX_REASON_LENGTH,
    REASON_REQUIRED_MESSAGE,
    SpendDecisionNotFoundError,
    SpendDecisionValidationError,
    accept_unmatched_spend,
    list_decisions,
    plan_belongs_to_project,
)

_PROJECT = "proj_EXAMPLE"
_OTHER_PROJECT = "proj_EXAMPLE_2"
_PLAN = "1b6bd5c0-0000-4000-8000-000000000001"
_CAMPAIGN = "camp_EXAMPLE_9"
_ACTOR = "owner@example.com"
_REASON = "Brand takeover bought outside the plan cycle."


class _Cursor:
    def __init__(self, owner: "_Connection") -> None:
        self._owner = owner
        self._rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self._owner.statements.append((sql, params))
        stripped = sql.strip()
        if "FROM app.media_plans" in sql:
            self._rows = [(1,)] if self._owner.plan_in_project else []
        elif stripped.startswith("INSERT INTO app.plan_unmatched_spend_decisions"):
            # `ON CONFLICT DO NOTHING RETURNING` returns NOTHING on conflict --
            # the shape the store has to survive, and the reason it re-reads.
            self._rows = [] if self._owner.already_decided else [
                ("7c2a0000-0000-4000-8000-00000000000a", params[3], params[4],
                 "2026-08-09T10:00:00+00:00")
            ]
        elif stripped.startswith("SELECT id, reason, decided_by, decided_at"):
            self._rows = (
                [(
                    "7c2a0000-0000-4000-8000-00000000000a",
                    self._owner.stored_reason,
                    self._owner.stored_actor,
                    "2026-08-01T09:00:00+00:00",
                )]
                if self._owner.already_decided
                else []
            )
        elif stripped.startswith("SELECT campaign_ref"):
            self._rows = list(self._owner.decisions)
        elif "INSERT INTO app.audit_log" in sql:
            self._rows = []
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
        plan_in_project: bool = True,
        already_decided: bool = False,
        stored_reason: str = "The first reason, taken first.",
        stored_actor: str = "first@example.com",
        decisions=None,
    ):
        self.plan_in_project = plan_in_project
        self.already_decided = already_decided
        self.stored_reason = stored_reason
        self.stored_actor = stored_actor
        self.decisions = decisions if decisions is not None else []
        self.statements: list[tuple] = []

    def cursor(self):
        return _Cursor(self)


def _accept(conn, **overrides):
    kwargs = {
        "project_id": _PROJECT,
        "plan_id": _PLAN,
        "connector": "cm360",
        "campaign_ref": _CAMPAIGN,
        "reason": _REASON,
        "actor": _ACTOR,
    }
    kwargs.update(overrides)
    return accept_unmatched_spend(conn, **kwargs)


# ---------------------------------------------------------------------------
# Written, and read back.
# ---------------------------------------------------------------------------


def test_accepting_writes_a_dated_row_keyed_on_plan_connector_and_campaign() -> None:
    conn = _Connection()
    result = _accept(conn)

    assert result["created"] is True
    assert result["state"] == MATCHING_STATE_ACCEPTED
    assert (result["plan_id"], result["connector"], result["campaign_ref"]) == (
        _PLAN, "cm360", _CAMPAIGN,
    )
    assert result["reason"] == _REASON
    assert result["decided_by"] == _ACTOR

    insert = next(
        (sql, params) for sql, params in conn.statements
        if sql.strip().startswith("INSERT INTO app.plan_unmatched_spend_decisions")
    )
    # THE WHOLE KEY IS IN THE STATEMENT, and nothing else is: no amount, no
    # weight, no line -- the decision is about a campaign, not about a share of it.
    assert insert[1] == (_PLAN, "cm360", _CAMPAIGN, _REASON, _ACTOR)
    columns = insert[0].split("(", 1)[1].split(")", 1)[0]
    assert [name.strip() for name in columns.split(",")] == [
        "plan_id", "connector", "campaign_ref", "reason", "decided_by",
    ]


def test_the_acceptance_is_journalled_as_its_own_action() -> None:
    conn = _Connection()
    _accept(conn)

    assert any("app.audit_log" in sql for sql, _ in conn.statements)
    # Not `media_plan.mapping.set`: the 10 actions that existed all say something
    # moved between a line and a campaign, and this one says the opposite.
    assert ACTION_MEDIA_PLAN_SPEND_ACCEPTED == "media_plan.spend.accepted"


def test_the_decisions_of_a_plan_are_read_back_keyed_by_campaign() -> None:
    conn = _Connection(
        decisions=[(_CAMPAIGN, _REASON, _ACTOR, "2026-08-09T10:00:00+00:00")]
    )
    decisions = list_decisions(conn, plan_id=_PLAN, connector="cm360")

    assert set(decisions) == {_CAMPAIGN}
    assert decisions[_CAMPAIGN]["decided_by"] == _ACTOR
    assert decisions[_CAMPAIGN]["reason"] == _REASON
    sql, params = conn.statements[0]
    assert "plan_id = %s" in sql and "connector = %s" in sql
    assert params == (_PLAN, "cm360")


# ---------------------------------------------------------------------------
# Idempotent, and the first decision stands.
# ---------------------------------------------------------------------------


def test_accepting_twice_writes_one_row_and_keeps_the_first_author_and_date() -> None:
    """A dated human act whose author the second clicker can overwrite is not one."""
    conn = _Connection(already_decided=True)
    result = _accept(conn, actor="second@example.com", reason="A different reason.")

    assert result["created"] is False
    assert result["decided_by"] == "first@example.com"
    assert result["reason"] == "The first reason, taken first."
    # And the statement that could have rewritten them does not exist.
    insert = next(
        sql for sql, _ in conn.statements
        if sql.strip().startswith("INSERT INTO app.plan_unmatched_spend_decisions")
    )
    assert "DO NOTHING" in insert
    assert "DO UPDATE" not in insert


# ---------------------------------------------------------------------------
# Refused.
# ---------------------------------------------------------------------------


def test_an_acceptance_with_no_reason_is_refused_before_anything_is_written() -> None:
    conn = _Connection()
    with pytest.raises(SpendDecisionValidationError) as raised:
        _accept(conn, reason="   ")

    assert str(raised.value) == REASON_REQUIRED_MESSAGE
    # Refused BEFORE the plan is even read: a row with no reason is not written
    # and then regretted.
    assert conn.statements == []


def test_a_reason_over_the_bound_is_refused_and_the_bound_is_named() -> None:
    conn = _Connection()
    with pytest.raises(SpendDecisionValidationError) as raised:
        _accept(conn, reason="x" * (MAX_REASON_LENGTH + 1))

    assert str(MAX_REASON_LENGTH) in str(raised.value)
    assert conn.statements == []


@pytest.mark.parametrize("blank", ["plan_id", "connector", "campaign_ref"])
def test_a_blank_term_of_the_scope_is_refused(blank) -> None:
    """The key IS the scope. A decision keyed on a blank is a decision about everything."""
    conn = _Connection()
    with pytest.raises(SpendDecisionValidationError):
        _accept(conn, **{blank: "   "})
    assert conn.statements == []


def test_a_plan_of_another_project_is_not_found_rather_than_refused() -> None:
    """`plan_id` comes from the request body, so this is the scope of the route.

    Not found and not forbidden: telling a caller that a plan exists somewhere
    else is telling them something about another Project.
    """
    conn = _Connection(plan_in_project=False)
    with pytest.raises(SpendDecisionNotFoundError):
        _accept(conn, project_id=_OTHER_PROJECT)

    # It stopped at the scope read; nothing was inserted.
    assert not any(
        sql.strip().startswith("INSERT INTO app.plan_unmatched_spend_decisions")
        for sql, _ in conn.statements
    )


def test_the_project_scope_read_names_the_project_and_the_plan_and_skips_archives() -> None:
    conn = _Connection()
    assert plan_belongs_to_project(conn, plan_id=_PLAN, project_id=_PROJECT) is True

    sql, params = conn.statements[0]
    assert "project_id = %s" in sql and "archived_at IS NULL" in sql
    assert params == (_PLAN, _PROJECT)


def test_an_acceptance_with_no_identity_is_refused() -> None:
    conn = _Connection()
    with pytest.raises(SpendDecisionValidationError):
        _accept(conn, actor="")
    assert conn.statements == []


# ---------------------------------------------------------------------------
# THE CROSSING, THROUGH THE ROUTE -- and it is the only thing that proves the
# shared guard.
# ---------------------------------------------------------------------------
#
# WHY THE TESTS ABOVE DO NOT COVER IT, measured by mutation. Neutralising
# `plan_belongs_to_project` FOR THE ROUTE ONLY -- `datastream_workbench_api.py:403`
# -- left every assertion of this file and of
# `test_datastream_workbench_placements.py` green. The reason is that
# `accept_unmatched_spend` re-takes the same check inside itself
# (`plan_spend_decisions.py`), so `test_a_plan_of_another_project_is_not_found…`
# proves the STORE and not the shared guard; and `attach` / `detach` were only
# ever exercised at the store grain, where the plan is already scoped by the
# caller. The route was the hole.
#
# WHAT THESE THREE TESTS DO INSTEAD: they build the real ASGI app over the real
# route table and cross it. A member of Project A, from a Datastream of Project A,
# naming a plan of Project B. The doubled cursor answers as the LEAK would -- the
# parent mapping and the attachment BOTH exist on B's plan -- so if the guard is
# removed the calls succeed with 201 and 200 instead of 404, and these tests go
# red. `test_the_crossing_guard_is_not_vacuous` pins that same fixture succeeding
# when the plan really is Project A's, so the 404 can only come from the guard.
#
# 404 AND NOT 403, and that is the assertion that matters: a plan that exists
# somewhere else and a plan that never existed must be indistinguishable from
# here, which is what `_placement_scope`'s docstring promises. The two
# responses are compared byte for byte rather than by status alone.

_PROJECT_B_PLAN = "1b6bd5c0-0000-4000-8000-0000000000bb"
_PLAN_THAT_NEVER_EXISTED = "1b6bd5c0-0000-4000-8000-0000000000ff"
_DATASTREAM = "ds_EXAMPLE"
_PLACEMENT_ID = "3f0e0000-0000-4000-8000-00000000000a"


class _RouteCursor:
    """Answers as the LEAK would: everything on the far plan exists and is reachable."""

    def __init__(self, owner: "_RouteConnection") -> None:
        self._owner = owner
        self._rows: list[tuple] = []

    def __enter__(self) -> "_RouteCursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self._owner.statements.append((sql, params))
        stripped = sql.strip()
        if "FROM app.datastreams" in sql:
            # The Datastream IS this Project's -- the caller is at home. Only the
            # plan is somebody else's, which is exactly the crossing.
            self._rows = [("cm360",)]
        elif "FROM app.media_plans" in sql:
            # `SELECT 1 … WHERE id = %s AND project_id = %s` -- the ONE statement
            # the guard rests on. Empty for a plan of Project B and empty for a
            # plan that never existed, which is why the two are indistinguishable.
            self._rows = [(1,)] if str(params[0]) == self._owner.plan_of_this_project else []
        elif "FROM app.plan_line_mappings" in sql:
            # The parent exists ON THE FAR PLAN. Without this the attach would
            # 404 for the wrong reason and the test would pass on an accident.
            self._rows = [(1,)]
        elif stripped.startswith("INSERT INTO app.plan_line_placement_mappings"):
            self._rows = [("3f0e0000-0000-4000-8000-00000000000c", True)]
        elif stripped.startswith("DELETE FROM app.plan_line_placement_mappings"):
            self._rows = [("line_display_q3", "camp_EXAMPLE_1", "placement_id", "plc_EXAMPLE_feed")]
        elif "INSERT INTO app.audit_log" in sql:
            self._rows = []
        else:  # pragma: no cover -- an unexpected read must not answer silently
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _RouteConnection:
    def __init__(self, *, plan_of_this_project: str = _PLAN) -> None:
        self.plan_of_this_project = plan_of_this_project
        self.statements: list[tuple] = []

    def cursor(self):
        return _RouteCursor(self)

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _client(conn: "_RouteConnection"):
    """The real route table, over a doubled connection and a doubled identity.

    `_authorize` is replaced rather than bypassed at a lower level: what is under
    test is the PLAN scope, and leaving the role check in place would only prove
    that a fake bearer is refused.
    """
    import contextlib
    from unittest.mock import patch

    from core.datastream_workbench_api import datastream_workbench_routes
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    async def _actor(_request, _role: str = "viewer"):
        return _ACTOR

    stack = contextlib.ExitStack()
    stack.enter_context(patch("core.datastream_workbench_api._authorize", new=_actor))
    stack.enter_context(
        patch("core.db.get_connection", new=lambda *_a, **_k: conn)
    )
    stack.enter_context(
        patch("core.project_capability_states.read_capability_state", return_value="ready")
    )
    return stack, TestClient(Starlette(routes=list(datastream_workbench_routes)))


def _attach_over_the_route(client, plan_id: str):
    return client.post(
        f"/api/projects/{_PROJECT}/datastreams/{_DATASTREAM}/workbench/placements/attachments",
        json={
            "plan_id": plan_id,
            "line_key": "line_display_q3",
            "campaign_ref": "camp_EXAMPLE_1",
            "breakdown_dimension": "placement_id",
            "breakdown_value": "plc_EXAMPLE_feed",
        },
    )


def _detach_over_the_route(client, plan_id: str):
    return client.delete(
        f"/api/projects/{_PROJECT}/datastreams/{_DATASTREAM}"
        f"/workbench/placements/attachments/{_PLACEMENT_ID}?plan_id={plan_id}"
    )


def test_attaching_onto_another_projects_plan_is_refused_at_the_route() -> None:
    conn = _RouteConnection()
    stack, client = _client(conn)
    with stack:
        response = _attach_over_the_route(client, _PROJECT_B_PLAN)

    assert response.status_code == 404, response.text
    assert response.json() == {
        "code": "not_found",
        "message": "Datastream Workbench not found",
    }
    # AND NOTHING WAS WRITTEN. The parent mapping of the far plan was reachable --
    # the doubled cursor says so -- so a missing guard would have inserted here.
    assert not any(
        sql.strip().startswith("INSERT INTO app.plan_line_placement_mappings")
        for sql, _ in conn.statements
    )
    assert not any("app.audit_log" in sql for sql, _ in conn.statements)


def test_detaching_from_another_projects_plan_is_refused_at_the_route() -> None:
    conn = _RouteConnection()
    stack, client = _client(conn)
    with stack:
        response = _detach_over_the_route(client, _PROJECT_B_PLAN)

    assert response.status_code == 404, response.text
    assert response.json() == {
        "code": "not_found",
        "message": "Datastream Workbench not found",
    }
    # A guessed UUID must not delete another Project's row through this address.
    assert not any(
        sql.strip().startswith("DELETE FROM app.plan_line_placement_mappings")
        for sql, _ in conn.statements
    )


def test_a_plan_of_another_project_and_a_plan_that_never_existed_answer_identically() -> None:
    """The sentence `_placement_scope` promises, pinned byte for byte.

    Telling a caller that a plan exists somewhere else is telling them something
    about another Project. Status AND body, on both gestures.
    """
    elsewhere_stack, elsewhere_client = _client(_RouteConnection())
    with elsewhere_stack:
        elsewhere_attach = _attach_over_the_route(elsewhere_client, _PROJECT_B_PLAN)
        elsewhere_detach = _detach_over_the_route(elsewhere_client, _PROJECT_B_PLAN)

    nowhere_stack, nowhere_client = _client(_RouteConnection())
    with nowhere_stack:
        nowhere_attach = _attach_over_the_route(nowhere_client, _PLAN_THAT_NEVER_EXISTED)
        nowhere_detach = _detach_over_the_route(nowhere_client, _PLAN_THAT_NEVER_EXISTED)

    assert (elsewhere_attach.status_code, elsewhere_attach.text) == (
        nowhere_attach.status_code, nowhere_attach.text,
    )
    assert (elsewhere_detach.status_code, elsewhere_detach.text) == (
        nowhere_detach.status_code, nowhere_detach.text,
    )
    # Not 403: a refusal that names a resource is a refusal that reveals it.
    assert elsewhere_attach.status_code == 404


def test_the_crossing_guard_is_not_vacuous() -> None:
    """THE TEETH. The same fixture SUCCEEDS when the plan really is this Project's.

    Without this, the three 404s above would pass on a fixture that refuses
    everything, and removing `datastream_workbench_api.py:403` would leave them
    green. Here both gestures go all the way through, on the identical doubles,
    with only the plan changed.
    """
    conn = _RouteConnection(plan_of_this_project=_PLAN)
    stack, client = _client(conn)
    with stack:
        attached = _attach_over_the_route(client, _PLAN)
        detached = _detach_over_the_route(client, _PLAN)

    assert attached.status_code == 201, attached.text
    assert detached.status_code == 200, detached.text
    assert any(
        sql.strip().startswith("INSERT INTO app.plan_line_placement_mappings")
        for sql, _ in conn.statements
    )
    assert any(
        sql.strip().startswith("DELETE FROM app.plan_line_placement_mappings")
        for sql, _ in conn.statements
    )


def test_the_accept_route_crosses_the_same_guard_and_answers_the_same_404() -> None:
    """The third write of the tab, through the same shared reader.

    `accept_unmatched_spend` re-takes the check inside itself, so this asserts the
    SHARED one fires FIRST: the refusal carries the Workbench's sentence
    ("Datastream Workbench not found"), not the store's ("This media plan does not
    exist on this Project"). Two sentences here would be two authorities on one
    scope.
    """
    conn = _RouteConnection()
    stack, client = _client(conn)
    with stack:
        response = client.post(
            f"/api/projects/{_PROJECT}/datastreams/{_DATASTREAM}"
            "/workbench/placements/spend-decisions",
            json={
                "plan_id": _PROJECT_B_PLAN,
                "campaign_ref": _CAMPAIGN,
                "reason": _REASON,
            },
        )

    assert response.status_code == 404, response.text
    assert response.json() == {
        "code": "not_found",
        "message": "Datastream Workbench not found",
    }


# ---------------------------------------------------------------------------
# The capability decides, and no amount moves.
# ---------------------------------------------------------------------------


def test_the_route_refuses_the_write_when_the_capability_is_off() -> None:
    """The same reader the tab uses, and it runs BEFORE the connector is read.

    « Éteinte, la capacité n'apparaît nulle part » is not only about pixels: a
    write that succeeds while its tab does not exist is a second authority on
    whether the capability is on.
    """
    from unittest.mock import patch

    from core.datastream_workbench import WorkbenchNotFound
    from core.datastream_workbench_api import _placement_scope

    conn = _Connection()
    with (
        patch("core.project_capability_states.read_capability_state", return_value="disabled"),
        pytest.raises(WorkbenchNotFound),
    ):
        _placement_scope(
            conn, project_id=_PROJECT, datastream_id="ds_EXAMPLE", plan_id=_PLAN
        )
    # Not one statement: neither the Datastream nor the plan was read.
    assert conn.statements == []


def test_the_accept_route_is_mounted_at_one_address_and_takes_member() -> None:
    """A control with no route is the defect this epic was rejected for seven times."""
    import inspect

    from core.datastream_workbench_api import (
        _accept_unmatched_spend,
        datastream_workbench_routes,
    )

    accepts = [
        route for route in datastream_workbench_routes
        if getattr(route, "path", "").endswith("/placements/spend-decisions")
    ]
    assert len(accepts) == 1
    assert accepts[0].methods == {"POST"}
    assert accepts[0].endpoint is _accept_unmatched_spend
    # `member`, like every other write of this Workbench: reading a plan is a
    # viewer's right, closing a spend decision is not.
    assert '_authorize(request, "member")' in inspect.getsource(_accept_unmatched_spend)
    # And the connector is never taken from the body -- the scope's middle term is
    # read from `app.datastreams`.
    assert "connector=connector" in inspect.getsource(_accept_unmatched_spend)


def test_accepting_moves_no_money_and_the_two_files_that_would_have_to_change() -> None:
    """Acceptance 5, and it is checkable rather than promised."""
    import pathlib

    from core.mirror_sync import __file__ as mirror_file

    root = pathlib.Path(__file__).resolve().parents[3]
    mart = (root / "dbt" / "models" / "marts" / "plan_vs_actual_daily.sql").read_text(
        encoding="utf-8"
    )
    assert "WHERE m.status = 'active'" in mart
    assert "plan_unmatched_spend_decisions" not in mart
    assert "plan_unmatched_spend_decisions" not in pathlib.Path(mirror_file).read_text(
        encoding="utf-8"
    )


def test_the_migration_declares_the_same_shape_as_the_module() -> None:
    """The header may not claim a guard the SQL does not make -- or the reverse."""
    import pathlib

    migration = (
        pathlib.Path(__file__).resolve().parents[3]
        / "infra" / "nango" / "migrations"
        / "245_a_spend_outside_the_plan_is_accepted_by_a_person.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS app.plan_unmatched_spend_decisions" in migration
    assert "(plan_id, connector, campaign_ref)" in migration
    assert "ON DELETE RESTRICT" in migration
    # No `status` column: the row's EXISTENCE is the decision, which is the same
    # finding that took `plan_line_placement_mappings.status` off the wire.
    assert "status" not in migration.split("CREATE TABLE", 1)[1].split(");", 1)[0]
    # The two NOT NULL the module refuses in Python are also refused by the
    # database, so a caller that bypassed the module cannot write a blank act.
    assert "CHECK (btrim(reason) <> '')" in migration
    assert "CHECK (btrim(decided_by) <> '')" in migration


def test_the_guard_is_not_vacuous() -> None:
    """Without these, three assertions above would pass on a degenerate double."""
    conn = _Connection(already_decided=True)
    assert conn.stored_actor != _ACTOR
    assert conn.stored_reason != _REASON
    assert len(_REASON) < MAX_REASON_LENGTH
