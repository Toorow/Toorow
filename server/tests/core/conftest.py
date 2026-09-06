"""Fixtures visible from ``tests/core/`` -- and the one that was missing.

WHY THIS FILE EXISTS. Nine files under ``tests/core/`` request a fixture named
``pg_conn``. It was defined in three places -- ``tests/isolation/conftest.py``,
``tests/core/test_warehouse_write_pg.py`` and
``tests/integration/test_external_bq_registration_constraints.py`` -- and none of
them is visible from ``tests/core/`` unless the requesting test happens to be
the file that defines it. Every other test asking for it therefore ended in::

    ERROR ... fixture 'pg_conn' not found

regardless of whether a PostgreSQL was available. Recorded as AI-93 on
2026-07-31, with the repair named there: "soit deplacer la fixture dans un
conftest visible depuis tests/core/, soit deplacer le fichier de test la ou elle
vit". This is the first option, chosen because it fixes the CLASS -- every file
in the directory -- rather than the one file that happened to be under
inspection.

What those tests were unable to prove is not incidental: DELETE being refused on
inbound receipts, and no raw recipient address existing as a column. They read
like a confidentiality guarantee and could not execute.

``pg_conn`` is deliberately a thin alias over the shared ``live_postgres``
fixture rather than a second connection helper. ``live_postgres`` already
carries the parts that must not be re-implemented per directory: the refusal
when ``TEST_POSTGRES_DSN`` points at a non-disposable database (these tests
COMMIT rows), the skip when no DSN is configured, and the declared-role
enforcement. A private ``psycopg.connect`` here would silently opt out of all
three -- which is how the polluted production fixtures happened in the first
place.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pg_conn(live_postgres):
    """A live psycopg connection, under the shared safety rules.

    Alias of ``live_postgres`` (defined in ``tests/conftest.py``). Files that
    define their own ``pg_conn`` shadow this one, which is fine: a local fixture
    is more specific and pytest resolves it first.
    """
    return live_postgres


# ---------------------------------------------------------------------------
# Real referents for the inbound pg-gated tests.
#
# The five inbound pg tests that could finally run, once `pg_conn` resolved,
# turned out never to have been correct either. They inserted
# `effective_org_id='platform'` (no such organization), an
# `idempotency_key_hash` of `"g" * 64` (the column CHECKs for hex) and a
# `datastream_id` that exists in no table (the FK is real). Three constraint
# violations that had simply never been reached.
#
# So the fixture below builds ACTUAL referents -- org, project, Datastream --
# instead of plausible-looking strings. It is here rather than duplicated per
# file because all of them need the same tree, and a private copy per file is
# how the three wrong literals survived in the first place.
# ---------------------------------------------------------------------------


@pytest.fixture
def inbound_pg_scope(pg_conn, test_org):
    """Commit a real org/project/Datastream tree and yield its identifiers.

    Yields ``{"org_id", "project_id", "datastream_id"}``.

    Committed rather than left in the transaction: the tests it serves commit
    their own writes to exercise constraints, and a FK cannot be satisfied by a
    parent row that is still invisible to the statement checking it. The
    end-of-session scrub in ``tests/conftest.py`` removes project-scoped rows.
    """
    import ulid as _ulid

    suffix = str(_ulid.ULID())
    project_id = f"proj_test_inb_{suffix}"[:40]
    datastream_id = f"ds_test_inb_{suffix}"[:40]

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, 'Inbound pg fixture', %s, 'active', 'system') "
            "ON CONFLICT (id) DO NOTHING",
            (project_id, test_org, f"inbound-pg-{suffix}"[:60]),
        )
        # `source_kind` is NOT optional here, even though the column allows NULL.
        # The BEFORE INSERT trigger `sync_external_dispatch_excluded` computes
        #     NEW.external_dispatch_excluded := (NEW.source_kind = 'external_bq')
        # and a NULL source_kind makes that comparison NULL, which then trips the
        # NOT NULL on external_dispatch_excluded -- an error naming a column the
        # caller never touched. `managed_feed` is the right value here regardless:
        # it is the ownership mode every inbound delivery lands under.
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, created_by, source_kind, enabled, "
            "schedule_mode, refetch_days, date_window_days, lifecycle_state) "
            "VALUES (%s, %s, %s, 'Inbound pg fixture datastream', 'system', "
            "'managed_feed', TRUE, 'manual', 3, 30, 'draft') "
            "ON CONFLICT (id) DO NOTHING",
            (datastream_id, project_id, test_org),
        )
    pg_conn.commit()

    yield {
        "org_id": test_org,
        "project_id": project_id,
        "datastream_id": datastream_id,
    }


@pytest.fixture
def insert_operation(pg_conn, inbound_pg_scope):
    """Return a callable inserting a valid ``app.operations`` row, and its id.

    Every hash it writes is real hex of the right length, derived from a counter
    so two calls never collide. The previous inline versions hardcoded letters
    past 'f', which the column CHECK rejects -- a detail invisible for as long
    as the tests could not run.
    """
    import hashlib
    import json as _json

    import ulid as _ulid

    counter = {"n": 0}

    def _insert(
        command_type: str = "inbound.test.operation",
        *,
        resource_path: list[str] | None = None,
    ) -> str:
        counter["n"] += 1
        op_id = f"op_{_ulid.ULID()}"
        seed = f"{op_id}:{counter['n']}"
        request_hash = hashlib.sha256(f"req:{seed}".encode()).hexdigest()
        idem_hash = hashlib.sha256(f"idem:{seed}".encode()).hexdigest()
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.operations "
                "(id, effective_org_id, command_type, actor, resource_path, "
                "host_context, versions, request_hash, provider_references, "
                "confirmation_mode, idempotency_key_hash, state) "
                "VALUES (%s, %s, %s, 'test', %s::jsonb, "
                "'{}'::jsonb, '{}'::jsonb, %s, '{}'::jsonb, 'server', %s, "
                "'pending')",
                (
                    op_id,
                    inbound_pg_scope["org_id"],
                    command_type,
                    # LA PROVENANCE QUE LE TRIGGER EXIGE, et pas un jeton d'essai.
                    # `protect_inbound_receipt` (migration 184) refuse un recu dont
                    # l'operation ne porte pas `datastream:<id>` dans son
                    # `resource_path`. La fixture ecrivait `["ds:test"]`, ce qui ne
                    # peut jamais correspondre -- cinq erreurs invisibles tant que
                    # les tests pg-gated skippaient faute de DSN.
                    _json.dumps(
                        resource_path
                        or [f"datastream:{inbound_pg_scope['datastream_id']}"]
                    ),
                    request_hash,
                    idem_hash,
                ),
            )
        return op_id

    return _insert


# ---------------------------------------------------------------------------
# Story 59.2 -- one seeded fleet for the two readers of the same fact.
#
# The Datastream fleet lens and the Workbench header both draw the open issues of
# a Datastream, and they must never disagree about one. Two fixtures would be two
# worlds: this one lives here so both test files ask for the SAME rows, for the
# same reason `pg_conn` and `inbound_pg_scope` were centralised above.
#
# It is seeded because nothing can be measured instead: `app.dq_issues` is 0 on
# the disposable base and 0 on preprod, and `app.dq_monitors` is 0 and 2. The
# governed rows are written by their real writers (`ensure_monitor`,
# `publish_version`, `open_issue`) -- a hand-rolled INSERT can satisfy a read the
# writer would never produce.
# ---------------------------------------------------------------------------


def dq_fingerprint(seed: str) -> str:
    """`ck` on `root_cause_fingerprint` is 64 lowercase hex, and nothing else."""
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@pytest.fixture
def dq_fleet(pg_conn):
    """One Project and TWO Datastreams -- the two the `Issues` column tells apart.

    `watched` is the one a monitor is published against; `bare` is watched by
    nothing. Both are linked through `app.project_flux`, which the fleet lens
    INNER JOINs -- a Datastream nobody linked to a Project is invisible to it,
    which is why 1421 rows of the disposable base project to at most one.

    Nothing is committed: the caller's connection is rolled back, and
    `app.dq_issues` is append-only under a trigger anyway.
    """
    import uuid

    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    watched = f"ds_{suffix}a"
    bare = f"ds_{suffix}b"

    with pg_conn.cursor() as cur:
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
        for datastream_id, name in ((watched, "Watched flux"), (bare, "Unwatched flux")):
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, org_id, name, module_name, enabled) "
                "VALUES (%s, %s, %s, %s, 'example_connector', TRUE)",
                (datastream_id, project_id, org_id, name),
            )
            cur.execute(
                "INSERT INTO app.project_flux (project_id, flux_id, org_id) "
                "VALUES (%s, %s, %s)",
                (project_id, datastream_id, org_id),
            )
    yield {"org_id": org_id, "project_id": project_id, "watched": watched, "bare": bare}
    pg_conn.rollback()


def publish_dq_monitor(conn, fleet, target: str) -> str:
    """A PUBLISHED monitor on one Datastream, through the writers of 59.3."""
    import uuid

    from core.dq_governance import ensure_monitor, publish_version

    head = ensure_monitor(
        conn,
        org_id=fleet["org_id"],
        project_id=fleet["project_id"],
        name=f"null_rate_{uuid.uuid4().hex[:10]}",
        label=f"Null rate: {target}",
        target_kind="datastream",
        target_id=target,
        actor="system",
    )
    publish_version(
        conn,
        project_id=fleet["project_id"],
        monitor_id=str(head["id"]),
        check_profile="null_rate",
        severity="degrading",
        actor="system",
        parameters={"thresholds": {"null_rate": 0.0}},
        window_days=1,
    )
    return str(head["id"])


def open_dq_issue(
    conn,
    fleet,
    monitor_id: str,
    *,
    severity: str,
    seed: str,
    datastream_id: str | None = None,
    execution_id: str | None = None,
) -> str:
    """One open issue, minted by `open_issue` and never by a hand-written row."""
    from core.dq_governance import open_issue

    issue = open_issue(
        conn,
        project_id=fleet["project_id"],
        monitor_id=monitor_id,
        root_cause_fingerprint=dq_fingerprint(seed),
        severity=severity,
        actor="system",
        datastream_id=datastream_id or fleet["watched"],
        execution_id=execution_id,
    )
    return str(issue["id"])
