"""Archiving a Report or a Notebook — the gesture `archived_at` never had.

WHY THIS FILE EXISTS. Migration 154 gave both stable heads an `archived_at`, a
partial index assuming a live-only list (`idx_analysis_reports_live ... WHERE
archived_at IS NULL`), and a header stating both heads "may advance their
current-version pointer AND BE ARCHIVED". Two refusals read the column -- "an
archived Report does not accept new versions", "an archived Notebook does not
accept new versions" -- and the notebook scheduler filtered on it.

**Nothing ever wrote it.** Measured 2026-08-17 (audit reports 02 and 11): zero
`SET archived_at` in the whole repository. So the refusals were unreachable code,
the index had no predicate to serve, the lists showed an `archived` boolean that
was permanently false, and archiving was modelled and inatteignable. Deletion is
not the alternative either: migration 154's trigger refuses DELETE on these heads
outright -- *"archive % rather than deleting it: its versions and runs are
evidence"*.

WHAT THIS PROVES:

  1. the writer exists, and is TIMESTAMPED AND ATTRIBUTED -- `archived_by` is
     added by migration 285 because `created_by` recorded who made an artifact
     and nothing recorded who retired it;
  2. THE READ<->WRITE LOOP CLOSES: the archived artifact LEAVES the default list.
     An archive that leaves the row in place is a flag, not a retirement -- and
     that is the half that makes the column worth writing;
  3. it stays REACHABLE -- `include_archived` brings it back, because the schema
     refuses to delete these heads precisely so their evidence survives;
  4. the refusals that were unreachable code now fire -- an archived Report takes
     no new version, and an archived Notebook does not run. That last one closes
     a real disagreement: the scheduler already refused an archived Notebook
     while a manual run accepted it;
  5. it is idempotent, because a confirmation dialog can be double-submitted.

Skipped without TEST_POSTGRES_DSN:
    python scripts/disposable_postgres.py up
"""

from __future__ import annotations

import os

import psycopg
import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")

_ACTOR = "owner@example.com"


@pytest.fixture()
def chain():
    """The Story 50.3 identity chain, reused rather than rebuilt here."""
    from tests.core.test_analyze_artifacts_pg import Chain

    connection = psycopg.connect(_DSN)
    try:
        yield Chain(connection).build()
    finally:
        connection.rollback()
        connection.close()


def _labels(rows) -> set[str]:
    return {row["id"] for row in rows}


# ---------------------------------------------------------------------------
# Reports.
# ---------------------------------------------------------------------------


def test_archiving_a_report_records_when_and_by_whom(chain):
    from core import analyze_artifacts as svc

    report_id, _version = chain.report()
    archived = svc.archive_report(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        report_id=report_id,
        actor=_ACTOR,
    )

    assert archived["archived"] is True
    assert archived["archived_at"], "an archive with no instant is not a record"
    assert archived["archived_by"] == _ACTOR, (
        "`created_by` said who made it and nothing said who retired it -- that is "
        "the column migration 285 adds"
    )
    assert archived["already_archived"] is False


def test_an_archived_report_leaves_the_default_list_and_can_be_asked_back(chain):
    """The loop: the write empties the list the screen reads, and only that list."""
    from core import analyze_artifacts as svc

    kept, _v1 = chain.report()
    retired, _v2 = chain.report()
    svc.archive_report(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        report_id=retired,
        actor=_ACTOR,
    )

    live = svc.list_reports(chain.conn, org_id=chain.org_id, project_id=chain.project_id)
    assert retired not in _labels(live), (
        "the archived Report is still in the default list -- an archive that does "
        "not empty the list is a flag, not a retirement"
    )
    assert kept in _labels(live), "archiving one Report hid another"

    everything = svc.list_reports(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        include_archived=True,
    )
    assert retired in _labels(everything), (
        "the archived Report is unreachable, which is deletion wearing another "
        "word -- and this schema refuses to delete these heads at all"
    )
    row = next(r for r in everything if r["id"] == retired)
    assert row["archived"] is True
    assert row["archived_by"] == _ACTOR


def test_an_archived_report_accepts_no_new_version(chain):
    """The refusal that was unreachable code until something wrote the column."""
    from core import analyze_artifacts as svc

    report_id, _version = chain.report()
    svc.archive_report(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        report_id=report_id, actor=_ACTOR,
    )

    with pytest.raises(svc.ArtifactRefused) as refused:
        svc.create_report_version(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            report_id=report_id,
            actor=_ACTOR,
            query_spec_version_id=chain.query_spec_version_id,
        )
    assert refused.value.as_dict()["code"] == "archived"


def test_archiving_twice_is_not_an_error(chain):
    """A confirmation dialog can be double-submitted, and two people can click it.

    The second call answers the state the caller asked for rather than inventing
    a conflict -- but it SAYS it changed nothing, which a bare rowcount could not
    distinguish from "not yours".
    """
    from core import analyze_artifacts as svc

    report_id, _version = chain.report()
    first = svc.archive_report(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        report_id=report_id, actor=_ACTOR,
    )
    second = svc.archive_report(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        report_id=report_id, actor="someone.else@example.com",
    )
    assert second["already_archived"] is True
    assert second["archived_at"] == first["archived_at"], (
        "the second archive moved the instant -- the first retirement is the one "
        "that happened"
    )
    assert second["archived_by"] == _ACTOR, "attribution was overwritten by a no-op"


def test_a_report_of_another_project_is_not_found(chain):
    """Non-disclosure, uniform with every other read on this surface."""
    from core import analyze_artifacts as svc

    report_id, _version = chain.report()
    with pytest.raises(svc.ArtifactNotFound):
        svc.archive_report(
            chain.conn,
            org_id=chain.org_id,
            project_id="proj_SOMEONE_ELSE",
            report_id=report_id,
            actor=_ACTOR,
        )


# ---------------------------------------------------------------------------
# Notebooks — the same class, which is why they share a writer.
# ---------------------------------------------------------------------------


def test_archiving_a_notebook_retires_it_from_the_list(chain):
    from core import analyze_artifacts as svc

    notebook_id, _version = chain.notebook()
    archived = svc.archive_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook_id, actor=_ACTOR,
    )
    assert archived["archived_by"] == _ACTOR

    live = svc.list_notebooks(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id
    )
    assert notebook_id not in _labels(live)

    everything = svc.list_notebooks(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        include_archived=True,
    )
    assert notebook_id in _labels(everything)


def test_an_archived_notebook_does_not_run(chain):
    """The two doors agree now. They did not before, and nobody could tell.

    `dispatch_due_notebook_schedules` filtered `archived_at IS NULL`; the manual
    `run_notebook` SELECTed `archived_at` and never tested it. With no writer for
    the column the disagreement was invisible -- the archive gesture is what makes
    it reachable, so it is closed in the same change.
    """
    from core import analyze_artifacts as svc

    notebook_id, _version = chain.notebook()
    svc.archive_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook_id, actor=_ACTOR,
    )

    with pytest.raises(svc.ArtifactRefused) as refused:
        svc.run_notebook(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            notebook_id=notebook_id,
            actor=_ACTOR,
            idempotency_key="idem_archived_example",
        )
    body = refused.value.as_dict()
    assert body["code"] == "archived"
    assert "Restore it" in body["message"], (
        "an error must name the gesture that repairs it, not the database state"
    )


def test_the_pair_cannot_be_half_written(chain):
    """`archived_at` and `archived_by` travel together, enforced by the schema.

    An artifact archived by nobody, or attributed to someone while still live,
    are both states the gesture cannot produce -- and migration 285 makes them
    unstorable rather than merely unwritten.
    """
    report_id, _version = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE app.analysis_reports SET archived_at = NOW() WHERE id = %s",
            (report_id,),
        )
    chain.conn.rollback()
