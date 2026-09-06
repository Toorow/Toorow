"""The SQL a report envelope runs, against a real schema -- AI-305 side-find.

WHY THIS FILE EXISTS, and it is the whole point. Two enrichment modules of the
report envelope carried queries that HAD NEVER BEEN EXECUTED, and their unit
tests could not have seen it: they patch `core.db.get_connection` with a
`MagicMock`, and a mocked cursor accepts any string. Measured 2026-08-23 by
driving the eval loop against a migrated database:

  * `branding._BRANDING_SQL` named TWO columns that do not exist -- `o.org_id`
    on `app.organizations` (which keys on `id`) and `p.project_id` on
    `app.projects` (which keys on `id` too);
  * `dimension_lineage._org_of_project` named `project_id` on the same table.

Both degrade by contract, so both failed in silence. Every other call site on
these two tables spells the columns correctly, which is why a grep found
nothing to worry about and only an execution did.

WHAT THAT COST, and why it left no mark. `resolve_org_branding` contracts to
degrade: "ANY resolution error -> None ... Branding NEVER fails a render". So
Postgres raised `UndefinedColumn`, the `except` swallowed it, and every report
was served with the DEFAULT THEME while a client's colors sat in the database.
The only trace was a WARNING. A degradation contract turns a broken query into a
silence, which is exactly why the query needs a test that runs it.

The second error only appeared after the first was fixed -- the parser stops at
the first unknown column -- so a test that merely asserted "no exception" on a
half-fixed query would have gone green on the way through. This one reads a row
back.

Every write happens inside the caller's transaction; `live_postgres` rolls back.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")

from core import branding, dimension_lineage  # noqa: E402

from tests.integration.epic66_fixtures import make_project  # noqa: E402

_PRIMARY = "#123456"
_SECONDARY = "#654321"
_ACCENT = "#ABCDEF"
_LOGO = "https://example.com/logo.svg"


def _connection_yielding(conn):
    """A `get_connection` stand-in that hands back the TEST transaction.

    It must not close: `live_postgres` owns the connection and rolls it back, and
    a helper that closed it would take the fixture's isolation with it. The
    modules under test import `get_connection` INSIDE their function bodies, so
    the patch target is `core.db` itself, never the module attribute.
    """

    @contextmanager
    def _factory():
        yield conn

    return _factory


def _run_branding_sql(conn, project_id: str):
    """Execute the module's OWN query string -- never a copy of it.

    A test that re-typed the SQL would prove that the test's SQL parses, which is
    the failure mode this file exists to close.
    """
    with conn.cursor() as cur:
        cur.execute(branding._BRANDING_SQL, (project_id,))
        return cur.fetchone()


def test_the_branding_sql_parses_and_binds_against_the_live_schema(live_postgres):
    """The minimum: Postgres accepts it. This alone would have been red.

    A project always has an org, so the row exists even before a color is set --
    which is what `resolve_org_branding` turns into `None` one layer up ("org
    exists but defined no branding -- same as absent"). The query's job here is
    to RUN and to bind five fields.
    """
    org_id, project_id = make_project(live_postgres, "Branding parse")

    row = _run_branding_sql(live_postgres, project_id)

    assert row is not None and len(row) == 5
    assert row[0] == org_id
    assert row[1:] == (None, None, None, None)


def test_it_returns_the_org_colors_of_the_project_five_fields_in_order(live_postgres):
    """Reading a row back is what proves the SECOND column name too.

    `resolve_org_branding` unpacks exactly five positional fields and refuses any
    other arity, so the order asserted here is the contract, not a preference.
    """
    conn = live_postgres
    org_id, project_id = make_project(conn, "Branding read")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.organizations SET brand_primary=%s, brand_secondary=%s, "
            "brand_accent=%s, logo_url=%s WHERE id=%s",
            (_PRIMARY, _SECONDARY, _ACCENT, _LOGO, org_id),
        )

    row = _run_branding_sql(conn, project_id)

    assert row is not None, "the project's org was not joined -- the JOIN column is wrong"
    assert len(row) == 5, "resolve_org_branding refuses any arity but five"
    assert row == (org_id, _PRIMARY, _SECONDARY, _ACCENT, _LOGO)


def test_a_project_of_another_org_does_not_borrow_its_colors(live_postgres):
    """The JOIN must be a join, not a cross product that any org would satisfy."""
    conn = live_postgres
    org_a, _project_a = make_project(conn, "Branding A")
    _org_b, project_b = make_project(conn, "Branding B")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.organizations SET brand_primary=%s WHERE id=%s", (_PRIMARY, org_a)
        )

    row = _run_branding_sql(conn, project_b)

    assert row is not None, "project B has an org of its own; the join must find it"
    assert row[0] != org_a
    assert row[1] is None, "B's org defined no color -- A's must not leak into it"


def test_dimension_lineage_resolves_the_org_of_a_project(live_postgres, monkeypatch):
    """The label cascade starts here, and it returned None on every report.

    `governance.md` ("A client label reaches every surface that shows the
    number") is answered by `resolve_report_dimension_labels`, whose FIRST act is
    this lookup. With the wrong column it raised, the handler logged, and every
    dimension fell back to its stable identifier -- a ratified criterion broken
    by a query nobody ran.
    """
    conn = live_postgres
    org_id, project_id = make_project(conn, "Lineage org")
    from core import db as core_db

    monkeypatch.setattr(core_db, "get_connection", _connection_yielding(conn))

    assert dimension_lineage._org_of_project(project_id) == org_id


def test_dimension_lineage_returns_none_for_a_project_that_does_not_exist(
    live_postgres, monkeypatch
):
    """None must mean "no such project", not "the query is broken"."""
    from core import db as core_db

    monkeypatch.setattr(core_db, "get_connection", _connection_yielding(live_postgres))

    assert dimension_lineage._org_of_project("proj_does_not_exist") is None
