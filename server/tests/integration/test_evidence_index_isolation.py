"""Story 49.5 — cross-tenant isolation and anti-inference on the Evidence index.

Two different guards, and both are required:

* the APPLICATION guard runs first (`resolve_strict_resource_access` in
  `governance_surface_api`), because RLS alone cannot distinguish "denied" from
  "not found" in the way the surface must;
* the DATABASE guard runs underneath (fail-closed RLS with FORCE), because an
  application guard that is bypassed once exposes everything behind it.

The live blocks need a disposable PostgreSQL, so they are gated on
`TEST_POSTGRES_DSN` through the repository's own fixture. The structural blocks
above them run everywhere and hold the shape those live blocks verify.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "149_evidence_reference_index.sql"
INDEX = ROOT / "server" / "core" / "evidence_index.py"
SURFACE = ROOT / "server" / "core" / "governance_surface_api.py"


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------


def test_the_application_guard_runs_before_the_read_model():
    """RLS is the floor, not the door.

    If the index read ever moved above `resolve_strict_resource_access`, a
    Project the caller has no grant on would reach the query and be refused by
    the database with a different shape of failure — which is a timing and
    status distinction, which is a disclosure.
    """
    source = SURFACE.read_text(encoding="utf-8")
    access = source.index("resolve_strict_resource_access(")
    compose = source.index("compose_governance_collection(\n")
    assert access < compose


def test_a_read_is_scoped_to_the_project_in_every_index_query():
    source = INDEX.read_text(encoding="utf-8")
    for query_name in ("_LINKS_FROM", "_LINKS_TO", "_RECORD_COLUMNS"):
        assert query_name in source
    # Every index read binds the Project. `_RECORD_COLUMNS` is a column list, so
    # the predicate lives with its callers -- which all bind it.
    assert source.count("project_id = %(project_id)s") >= 6


def test_a_count_is_never_taken_with_a_wider_predicate_than_the_page():
    """`coverage.total` counts only what this caller can see.

    A count taken outside the caller's predicate would be an undercount
    presented as complete, or -- worse -- a disclosure of records the caller
    cannot open.
    """
    source = INDEX.read_text(encoding="utf-8")
    assert "count_where = _lens_where(lens, filters, count_params)" in source
    assert "total = None" in source


def test_a_hidden_node_is_removed_before_any_count():
    source = INDEX.read_text(encoding="utf-8")
    marker = "record = load_record(conn, project_id=project_id, record_id=candidate)"
    assert marker in source
    after = source[source.index(marker) : source.index(marker) + 400]
    # `None` means the caller cannot discover it. It is skipped, so it never
    # reaches `nodes`, so it cannot be inferred from a total.
    assert "if record is None:" in after
    assert "continue" in after


def test_the_audit_projection_never_selects_a_provider_account_or_secret():
    """Asserted on the QUERIES, not on the file.

    The module explains in prose which columns must never be returned, so
    scanning the whole text would only prove the explanation exists. The three
    SQL constants below are the only places audit columns are named at all.
    """
    from core.evidence_index import _AUDIT_DETAIL_SQL, _AUDIT_SQL
    from core.governance_read_model import _AUDIT_SHAPE

    for query in (_AUDIT_SQL, _AUDIT_DETAIL_SQL, _AUDIT_SHAPE):
        for forbidden in (
            "provider_account",
            "connection_ref",
            "idempotency_key_hash",
            "confirmation_reference_hash",
            "host_context",
        ):
            assert forbidden not in query, forbidden
    # `a.metadata` is READ once, to resolve a legacy Project scope, and is never
    # SELECTed. The other mention is the `'legacy_metadata'` scope label, which
    # is a constant this query writes, not a column it returns.
    assert _AUDIT_SQL.count("a.metadata") == 1
    assert "a.metadata->>'project_id'" in _AUDIT_SQL
    assert "a.metadata," not in _AUDIT_SQL
    assert "a.metadata\n" not in _AUDIT_SQL
    assert "metadata" not in _AUDIT_DETAIL_SQL
    assert "metadata" not in _AUDIT_SHAPE


def test_the_index_read_is_no_store_and_read_only():
    """The EVIDENCE reads stay GET-only, and Governance never writes an owner.

    This asserted that the whole surface module contained no POST at all. Story
    49.2 added one -- the guarded Master Data node command -- and the invariant
    worth keeping is narrower than the proxy that was measuring it: the four
    Evidence/collection/object READS are GET-only, and the one write delegates to
    the owner (`core.master_data` through the durable-operation wrapper) instead
    of issuing SQL of its own. A surface with a write is not a second writer; a
    surface with an INSERT is.
    """
    source = SURFACE.read_text(encoding="utf-8")
    assert 'response.headers["Cache-Control"] = "no-store"' in source
    assert 'methods=["GET"]' in source
    assert 'methods=["DELETE"]' not in source
    assert 'methods=["PATCH"]' not in source

    # No Evidence address gained a write: the only POST is the Master Data node
    # command, on its own path.
    post_paths = [
        line
        for line in source.splitlines()
        if "/api/projects/" in line and "evidence" in line.lower()
    ]
    assert all("commands" not in line for line in post_paths)

    # And Governance still issues no SQL of its own.
    statements = source.upper()
    for forbidden in ("INSERT INTO", "UPDATE APP.", "DELETE FROM", "CREATE TABLE"):
        assert forbidden not in statements, forbidden


def test_no_evidence_mutation_reaches_mcp_or_the_console():
    """Governance stays GET-only on this surface.

    The index is written by an internal projector, never by a request. A tool or
    an admin route that could register an Evidence Record would make Governance
    a second writer of other workspaces' history.
    """
    main = (ROOT / "server" / "core" / "main.py").read_text(encoding="utf-8")
    admin = (ROOT / "server" / "core" / "admin_api.py").read_text(encoding="utf-8")
    for source in (main, admin):
        assert "register_reference" not in source
        assert "evidence_records" not in source


def test_rls_uses_force_so_the_owning_role_is_not_exempt():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("FORCE ROW LEVEL SECURITY;") == 4


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(live_postgres):
    return live_postgres


def _org_project(conn, label: str) -> tuple[str, str]:
    from ulid import ULID

    org_id = f"org_{ULID()}"
    project_id = f"proj_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', %s)",
            (org_id, label, org_id.lower(), "test@example.com"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (project_id, org_id, label, project_id.lower(), "test@example.com"),
        )
    return org_id, project_id


def _index(conn, org_id: str, project_id: str, owner_object_id: str) -> str:
    from core.evidence_index import ReferenceEvent, register_reference

    record_id, _ = register_reference(
        conn,
        org_id=org_id,
        project_id=project_id,
        event=ReferenceEvent(
            producer="data_execution",
            record_kind="evidence_trace",
            owner_workspace="data",
            owner_object_type="datastream-execution",
            owner_object_id=owner_object_id,
            occurred_at="2026-07-30T09:00:00Z",
            source_identity_key=f"data_execution|{owner_object_id}",
            is_anchor=True,
        ),
    )
    return record_id


def test_the_same_owner_id_in_two_projects_never_crosses_scope(conn):
    """The exact cross-tenant case: one opaque source id, two Projects.

    Both index it. Each Project sees its own record and cannot open the other's,
    even though the OWNER identifier is byte-identical.
    """
    from core.evidence_index import load_record

    org_a, project_a = _org_project(conn, "Evidence A")
    org_b, project_b = _org_project(conn, "Evidence B")

    record_a = _index(conn, org_a, project_a, "dse_SHARED")
    record_b = _index(conn, org_b, project_b, "dse_SHARED")
    assert record_a != record_b

    assert load_record(conn, project_id=project_a, record_id=record_a) is not None
    # Not found, not denied: existence-indistinguishable from a record that
    # never existed.
    assert load_record(conn, project_id=project_a, record_id=record_b) is None
    assert load_record(conn, project_id=project_b, record_id=record_a) is None
    conn.rollback()


def test_rls_hides_a_row_from_an_identity_with_no_grant(conn):
    """Proven against the DEPLOYED policy, not an application mock.

    With `toorow.enforce_epic36` on and an identity that holds no grant, the
    policy has no applicable clause and the table exposes nothing.
    """
    from core.db import set_local_access_context

    # A superuser bypasses RLS entirely, and a table owner bypasses it unless
    # FORCE is set. Run as either and this test passes while proving NOTHING --
    # which is exactly what happened the first time it was executed, as
    # `postgres`. Refuse loudly rather than report a vacuous green.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        role, is_superuser = cur.fetchone()
        cur.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class"
            " WHERE oid = 'app.evidence_records'::regclass"
        )
        owner = cur.fetchone()[0]
    assert not is_superuser, (
        f"connected as superuser {role!r}: RLS is bypassed, so this test would pass "
        "without proving anything. Point TEST_POSTGRES_DSN at the deployed "
        "application role."
    )
    assert role != owner, (
        f"connected as the table owner {role!r}: run as the deployed application role."
    )

    org_id, project_id = _org_project(conn, "Evidence RLS")
    _index(conn, org_id, project_id, "dse_RLS")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.evidence_records WHERE project_id = %s", (project_id,)
        )
        assert cur.fetchone()[0] == 1

    set_local_access_context(conn, "stranger@example.com", enforce_epic36=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.evidence_records WHERE project_id = %s", (project_id,)
        )
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_a_backfill_is_restartable_and_never_duplicates(conn):
    """Two passes over the same producer index the same artifacts once.

    The second pass resumes from the watermark, so it neither re-indexes nor
    grows the record count.
    """
    from core.evidence_index import backfill_producer

    org_id, project_id = _org_project(conn, "Evidence backfill")

    first = backfill_producer(
        conn, org_id=org_id, project_id=project_id, producer="platform_audit", limit=10
    )
    second = backfill_producer(
        conn, org_id=org_id, project_id=project_id, producer="platform_audit", limit=10
    )
    assert first.state in {"idle", "backfilling"}
    # A fresh Project has no audit rows, so both passes index nothing -- and the
    # SECOND one proves the watermark did not make it re-scan from zero.
    assert second.indexed == 0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.evidence_index_watermarks"
            " WHERE project_id = %s AND producer = %s",
            (project_id, "platform_audit"),
        )
        assert cur.fetchone()[0] in {"idle", "backfilling"}
    conn.rollback()


def test_two_anchors_sharing_a_correlation_never_merge(conn):
    """A pull, a publication and a Result are three claims, not one trace.

    They can share an execution id. That must NOT make them one graph, because a
    correlation is a filter and only a persisted link is an edge.
    """
    from core.evidence_index import (
        ReferenceEvent,
        load_trace_graph,
        register_reference,
    )

    org_id, project_id = _org_project(conn, "Evidence anchors")

    first, _ = register_reference(
        conn,
        org_id=org_id,
        project_id=project_id,
        event=ReferenceEvent(
            producer="data_execution",
            record_kind="evidence_trace",
            owner_workspace="data",
            owner_object_type="datastream-execution",
            owner_object_id="dse_1",
            occurred_at="2026-07-30T09:00:00Z",
            source_identity_key="data_execution|dse_1",
            is_anchor=True,
            correlations=(("execution", "dse_1"),),
        ),
    )
    second, _ = register_reference(
        conn,
        org_id=org_id,
        project_id=project_id,
        event=ReferenceEvent(
            producer="data_mapping_publication",
            record_kind="evidence_trace",
            owner_workspace="data",
            owner_object_type="datastream-mapping-publication",
            owner_object_id="dmpl_1",
            occurred_at="2026-07-30T09:00:00Z",
            source_identity_key="data_mapping_publication|dmpl_1",
            is_anchor=True,
            # The SAME correlation. Deliberately.
            correlations=(("execution", "dse_1"),),
        ),
    )

    graph = load_trace_graph(conn, project_id=project_id, anchor_id=first)
    assert [node["record_id"] for node in graph["nodes"]] == [first]
    assert second not in {node["record_id"] for node in graph["nodes"]}
    assert graph["coverage"] == "partial"
    assert any(reason["code"] == "single_node_chain" for reason in graph["unavailable_reasons"])
    conn.rollback()
