"""The Query Spec collection, REPLAYED against a live PostgreSQL.

WHY THIS FILE IS PG-GATED RATHER THAN MOCKED. Everything worth proving about a
list route is a property of the WHERE clause: that a second Project's rows do not
appear, that a page boundary neither skips nor repeats, that an empty Project
answers an empty list. A `MagicMock` cursor returns whatever the fixture hands
it, so a mocked version of these four tests would prove that the author of the
fixture and the author of the assertion agree -- and would stay green the day the
`project_id` predicate is dropped from the SQL. That is exactly the failure this
route must not have: it feeds a picker whose whole job is to offer only what the
caller may pin.

`live_postgres` rolls back on teardown, so each test builds its own chain and
nothing here depends on another test's rows.
"""

from __future__ import annotations

import pytest
from core.query_specs import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, list_query_specs
from ulid import ULID

psycopg = pytest.importorskip("psycopg")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _project(conn) -> tuple[str, str]:
    org_id = _uid("org")
    project_id = _uid("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, "Query Spec list", org_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (project_id, org_id, "Query Spec list", project_id.lower(), "tester"),
        )
    return org_id, project_id


def _semantic_view(conn, project_id: str) -> tuple[str, str]:
    """A published Semantic View version -- `fk_query_spec_versions_semantic_scope`
    requires one, and a Query Spec version with no view to pin is not a thing this
    product allows."""
    view_id = _uid("sv")
    version_id = _uid("svv")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s, %s, %s, 'tester')",
            (view_id, project_id, f"list_fixture_{view_id.lower()}"),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', %s, 'Query Spec list fixture',
                    %s, %s, 'tester')
            """,
            (
                version_id, view_id, project_id, f"list_fixture_{view_id.lower()}",
                "a" * 64, "a" * 64,
            ),
        )
    return view_id, version_id


def _spec(conn, org_id: str, project_id: str, *, name: str | None, versions: int = 1) -> dict:
    """One Query Spec head and `versions` immutable versions, wired as the store
    writes them: the head's `current_version_id` advances to the newest."""
    spec_id = _uid("qs")
    view_id, view_version_id = _semantic_view(conn, project_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_specs
                (id, org_id, project_id, semantic_view_id, name, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (spec_id, org_id, project_id, view_id, name, "tester"),
        )
        version_id = None
        predecessor = None
        for number in range(1, versions + 1):
            version_id = _uid("qsv")
            cur.execute(
                """
                INSERT INTO app.query_spec_versions
                    (id, query_spec_id, org_id, project_id, version_number,
                     semantic_view_id, semantic_view_version_id, spec, content_hash,
                     predecessor_version_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (
                    version_id, spec_id, org_id, project_id, number, view_id,
                    view_version_id, "{}", f"{number:064d}", predecessor, "tester",
                ),
            )
            predecessor = version_id
        if version_id is not None:
            cur.execute(
                "UPDATE app.query_specs SET current_version_id = %s WHERE id = %s",
                (version_id, spec_id),
            )
    return {"id": spec_id, "current_version_id": version_id}


def test_the_list_carries_what_a_picker_needs_to_pin(live_postgres):
    org_id, project_id = _project(live_postgres)
    written = _spec(live_postgres, org_id, project_id, name="Weekly clicks", versions=3)

    payload = list_query_specs(live_postgres, org_id=org_id, project_id=project_id)

    assert payload["next_cursor"] is None
    (row,) = payload["query_specs"]
    assert row["id"] == written["id"]
    assert row["name"] == "Weekly clicks"
    # The PINNABLE identity, and the number that lets a person see WHICH version
    # they are about to pin. Three versions exist; the head is v3.
    assert row["current_version_id"] == written["current_version_id"]
    assert row["current_version_number"] == 3
    assert row["created_at"] is not None


def test_an_unnamed_spec_keeps_its_null_rather_than_being_given_a_title(live_postgres):
    """`name` is nullable in the schema. Deriving one here would invent a label
    the author never wrote, and no other surface would agree with it."""
    org_id, project_id = _project(live_postgres)
    _spec(live_postgres, org_id, project_id, name=None)

    (row,) = list_query_specs(
        live_postgres, org_id=org_id, project_id=project_id
    )["query_specs"]

    assert row["name"] is None


def test_a_head_with_no_version_is_listed_with_nothing_to_pin(live_postgres):
    """Listed, because it exists. `current_version_id` is null, because nothing
    can be pinned to it -- which is a different fact from "no such Query Spec"."""
    org_id, project_id = _project(live_postgres)
    written = _spec(live_postgres, org_id, project_id, name="Drafted", versions=0)

    (row,) = list_query_specs(
        live_postgres, org_id=org_id, project_id=project_id
    )["query_specs"]

    assert row["id"] == written["id"]
    assert row["current_version_id"] is None
    assert row["current_version_number"] is None


def test_only_the_asked_projects_specs_come_back(live_postgres):
    """The property a mocked cursor cannot hold: the neighbour's rows are absent
    because the WHERE excludes them, not because a fixture omitted them."""
    org_a, project_a = _project(live_postgres)
    org_b, project_b = _project(live_postgres)
    mine = _spec(live_postgres, org_a, project_a, name="Mine")
    theirs = _spec(live_postgres, org_b, project_b, name="Theirs")

    ids = {
        row["id"]
        for row in list_query_specs(
            live_postgres, org_id=org_a, project_id=project_a
        )["query_specs"]
    }

    assert ids == {mine["id"]}
    assert theirs["id"] not in ids
    # And the same Project asked under a foreign org is not a partial match: it
    # is nothing at all.
    assert list_query_specs(
        live_postgres, org_id=org_b, project_id=project_a
    )["query_specs"] == []


def test_an_empty_project_answers_an_empty_list(live_postgres):
    org_id, project_id = _project(live_postgres)

    payload = list_query_specs(live_postgres, org_id=org_id, project_id=project_id)

    assert payload == {"query_specs": [], "next_cursor": None}


def test_a_page_boundary_neither_skips_nor_repeats_a_row(live_postgres):
    org_id, project_id = _project(live_postgres)
    written = {_spec(live_postgres, org_id, project_id, name=f"Q{n}")["id"] for n in range(5)}

    first = list_query_specs(live_postgres, org_id=org_id, project_id=project_id, limit=2)
    assert len(first["query_specs"]) == 2
    assert first["next_cursor"] == first["query_specs"][-1]["id"]

    second = list_query_specs(
        live_postgres, org_id=org_id, project_id=project_id, limit=2,
        cursor=first["next_cursor"],
    )
    third = list_query_specs(
        live_postgres, org_id=org_id, project_id=project_id, limit=2,
        cursor=second["next_cursor"],
    )

    walked = [r["id"] for page in (first, second, third) for r in page["query_specs"]]
    assert len(walked) == len(set(walked)) == 5, "a boundary repeated or skipped a row"
    assert set(walked) == written
    # Newest first, and the last page says it is the last.
    assert walked == sorted(written, reverse=True)
    assert third["next_cursor"] is None


def test_the_page_is_bounded_whatever_the_caller_asks(live_postgres):
    """A picker that asks for everything still gets a page. The clamp is in the
    store, so an MCP adapter reaching this function directly is bounded too."""
    org_id, project_id = _project(live_postgres)
    for n in range(3):
        _spec(live_postgres, org_id, project_id, name=f"Q{n}")

    import re

    executed: list[tuple] = []
    real_cursor = live_postgres.cursor

    class _Watched:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def execute(self, sql, params=None):
            executed.append((sql, params))
            return self._inner.execute(sql, params)

        def fetchall(self):
            return self._inner.fetchall()

    live_postgres.cursor = lambda *a, **k: _Watched(real_cursor(*a, **k))
    try:
        list_query_specs(
            live_postgres, org_id=org_id, project_id=project_id, limit=10_000
        )
        list_query_specs(live_postgres, org_id=org_id, project_id=project_id, limit=0)
    finally:
        live_postgres.cursor = real_cursor

    # `limit + 1` is what the store sends: it is how "there is another page" is
    # learned without a second COUNT.
    sent = [params[-1] for _sql, params in executed]
    assert sent == [MAX_LIST_LIMIT + 1, DEFAULT_LIST_LIMIT + 1]
    # And the scope is in the statement, not applied afterwards in Python.
    normalized = " ".join(re.sub(r"\s+", " ", executed[0][0]).split())
    assert "WHERE s.org_id = %s AND s.project_id = %s" in normalized
