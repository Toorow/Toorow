"""The Epic-36 RLS floor under the Analyze surfaces, proved by reading rows.

WHY THIS FILE EXISTS. `test_analyze_artifacts_pg.py` asserts `relrowsecurity`
and `relforcerowsecurity` on the eleven Analyze tables. Those flags were TRUE the
whole time the floor was doing nothing: the policy predicate is

    current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)

so with the GUC unset the first disjunct is TRUE and every row is visible. A test
that asserts the flag proves the flag. This file inserts a row into project A and
tries to READ IT BACK as a subject granted only project B, which is the only
thing that distinguishes an armed policy from a decorative one.

IT NEVER SKIPS INTO GREEN. A superuser and a BYPASSRLS role both ignore RLS
entirely, so an isolation assertion under either passes while proving nothing.
This file FAILS in that case instead of skipping, and it asserts the negative
control -- the same SELECT with the GUC unset must return the row -- so a query
that returns nothing for an unrelated reason cannot be mistaken for isolation.
"""

from __future__ import annotations

import pytest
from core.query_specs_api import analyze_connection, arm_access_floor
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

_HASH = "a" * 64


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _assert_rls_can_bite(cur) -> None:
    """A superuser or BYPASSRLS role makes every assertion below vacuous."""
    cur.execute(
        "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
        "WHERE rolname = current_user"
    )
    user, is_super, bypasses = cur.fetchone()
    assert not is_super, (
        f"connected as superuser {user!r}: RLS is bypassed and this test would "
        "pass without proving anything"
    )
    assert not bypasses, f"role {user!r} has BYPASSRLS: this test proves nothing"


class TwoProjects:
    """One org, two projects, and a subject granted exactly one of them."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.project_a = _uid("proj")
        self.project_b = _uid("proj")
        self.subject = f"person_{ULID()}"
        self.view_id = _uid("sv")
        self.spec_a = _uid("qs")

    def build(self) -> TwoProjects:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'RLS floor fixture', %s, 'active', 'test')",
                (self.org_id, self.org_id.replace("_", "-")),
            )
            for project_id in (self.project_a, self.project_b):
                cur.execute(
                    "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                    "VALUES (%s, %s, 'RLS floor fixture', %s, 'test')",
                    (project_id, self.org_id, project_id.replace("_", "-")),
                )
            # An ACTIVE MEMBER, not an owner: an owner short-circuits
            # `epic36_has_resource_access` and would see both projects, which
            # would make the isolation assertion untestable.
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status) "
                "VALUES (%s, %s, %s, 'member', 'active')",
                (_uid("om"), self.org_id, self.subject),
            )
            # Granted project B ONLY. Project A is the one we will fail to read.
            cur.execute(
                "INSERT INTO app.resource_grants "
                "(id, org_id, identity, scope_type, scope_id, capability, granted_by) "
                "VALUES (%s, %s, %s, 'project', %s, 'view', 'test')",
                (_uid("rg"), self.org_id, self.subject, self.project_b),
            )
            cur.execute(
                "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
                "VALUES (%s, %s, 'fixture_view', 'test')",
                (self.view_id, self.project_a),
            )
            # The row that must become invisible. Written with the floor DOWN,
            # which is how every row in this database was written today.
            cur.execute(
                "INSERT INTO app.query_specs "
                "(id, org_id, project_id, semantic_view_id, created_by) "
                "VALUES (%s, %s, %s, %s, 'test')",
                (self.spec_a, self.org_id, self.project_a, self.view_id),
            )
        return self


@pytest.fixture()
def two_projects(live_postgres):
    return TwoProjects(live_postgres).build()


def test_analyze_row_is_unreadable_by_a_subject_granted_another_project(
    live_postgres, two_projects
):
    """Insert into project A; read it back as a subject granted only project B.

    Deliberately NO `WHERE org_id = ... AND project_id = ...`. The application
    adds that predicate to every statement and the reviewer confirmed it is
    present -- which is exactly why it cannot be what is under test here. This
    query asks the database, and nothing else, to withhold the row.
    """
    with live_postgres.cursor() as cur:
        _assert_rls_can_bite(cur)

        # Negative control: with the floor down the row IS there. Without this,
        # an empty result later would prove nothing about the policy.
        cur.execute("SELECT set_config('toorow.enforce_epic36', 'off', true)")
        cur.execute("SELECT count(*) FROM app.query_specs WHERE id = %s", (two_projects.spec_a,))
        assert cur.fetchone()[0] == 1, "fixture row absent: the test below would be vacuous"

        # Now the subject the product would authenticate.
        arm_access_floor(live_postgres, two_projects.subject)
        cur.execute("SELECT count(*) FROM app.query_specs WHERE id = %s", (two_projects.spec_a,))
        assert cur.fetchone()[0] == 0, (
            "a subject granted only project B read a Query Spec belonging to "
            "project A: the RLS floor is not armed"
        )

        # ... and the project it WAS granted stays readable, so the assertion
        # above is isolation and not a blanket denial.
        cur.execute(
            "SELECT count(*) FROM app.projects WHERE id = %s", (two_projects.project_b,)
        )
        assert cur.fetchone()[0] == 1, "the granted project became unreadable too"


def test_arming_is_what_changes_visibility_not_the_predicate(live_postgres, two_projects):
    """The same statement, twice, differing only in the GUC. 1 row then 0 rows."""
    with live_postgres.cursor() as cur:
        _assert_rls_can_bite(cur)
        statement = "SELECT count(*) FROM app.query_specs WHERE id = %s"

        cur.execute("SELECT set_config('toorow.identity', %s, true)", (two_projects.subject,))
        cur.execute("SELECT set_config('toorow.enforce_epic36', 'off', true)")
        cur.execute(statement, (two_projects.spec_a,))
        unset = cur.fetchone()[0]

        cur.execute("SELECT set_config('toorow.enforce_epic36', 'on', true)")
        cur.execute(statement, (two_projects.spec_a,))
        armed = cur.fetchone()[0]

    assert (unset, armed) == (1, 0), (
        f"expected 1 row unarmed and 0 armed, got {unset} and {armed}"
    )


def test_analyze_connection_arms_the_connection_it_yields(monkeypatch):
    """`analyze_connection` is the seam; it must arm the connection it hands out.

    Arming any OTHER connection buys nothing: `set_config(..., true)` is local to
    the transaction of the connection it ran on. This is the defect the four
    modules had -- `_authorize` opened, used and CLOSED its own connection, and
    every handler then opened a fresh unarmed one.

    `PLATFORM_DB_URL` is pointed at the SAME disposable database the rest of this
    file uses, and only for the duration of this test, so `analyze_connection`
    opens a real connection and we read the GUC back off the real thing.
    """
    import os as _os

    monkeypatch.setenv("PLATFORM_DB_URL", _os.environ["TEST_POSTGRES_DSN"])
    subject = f"person_{ULID()}"
    with analyze_connection(subject) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT current_setting('toorow.identity', true), "
            "current_setting('toorow.enforce_epic36', true)"
        )
        identity, enforce = cur.fetchone()
    assert identity == subject
    assert enforce == "on"


def test_the_anonymous_local_operator_is_the_only_unarmed_subject(live_postgres, monkeypatch):
    """Auth-disabled self-host keeps working; every other mode arms the floor.

    `anonymous` has no `app.org_members` row to resolve against and
    `set_local_access_context` refuses the identity outright, so arming it would
    turn the local developer database into a permanent 404. That carve-out is
    the same one `admin_api._strict_project_capability_allowed` and
    `rendus_api` already make -- it is not a new policy invented here. The
    moment auth is enabled, the same identity is armed.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    arm_access_floor(live_postgres, "anonymous")
    with live_postgres.cursor() as cur:
        cur.execute("SELECT current_setting('toorow.enforce_epic36', true)")
        assert cur.fetchone()[0] in (None, "", "off"), (
            "the auth-disabled local operator was armed: every local read becomes a 404"
        )

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    subject = f"person_{ULID()}"
    arm_access_floor(live_postgres, subject)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_setting('toorow.identity', true), "
            "current_setting('toorow.enforce_epic36', true)"
        )
        assert cur.fetchone() == (subject, "on")


def test_every_analyze_handler_opens_its_connection_through_the_seam():
    """A structural guard, so the next handler cannot reopen the hole quietly.

    Not a style rule. Any `get_connection()` in a handler is a connection with
    the floor down, which is precisely the shape the four modules shipped with.
    The only legitimate bare uses are inside `analyze_connection` itself and
    inside each module's `_authorize`, which resolves authorization and reads no
    Analyze table.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "core"
    modules = [
        "query_specs_api.py",
        "analyze_workbench_api.py",
        "analyze_artifacts_api.py",
        "visualization_specs_api.py",
    ]
    offenders = []
    for name in modules:
        text = (root / name).read_text(encoding="utf-8")
        # Everything from the first handler onward: `_authorize` and
        # `analyze_connection` are both declared above it in all four files.
        marker = text.find("\nasync def _", text.find("async def _authorize") + 1)
        body = text[marker:] if marker != -1 else ""
        for number, line in enumerate(body.splitlines(), start=1):
            if "with get_connection()" in line:
                offenders.append(f"{name}: {line.strip()}")
    assert not offenders, (
        "handler(s) opening an unarmed connection, so the RLS floor is down for "
        "their statements:\n  " + "\n  ".join(offenders)
    )
