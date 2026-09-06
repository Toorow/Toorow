"""Live-Postgres proof that `meta.confidence` describes THIS project and THIS window.

Story 53.3 / CAV-06. These assertions need a real database for a reason a mock
would hide: the defect they close was a **column that does not exist**.

`core.confidence.compute_confidence` selected `r.connector_name` from
`app.connection_ref`, a table whose column is `provider`. Every call therefore
raised `UndefinedColumn`, the module's bare `except Exception: return None`
swallowed it, and `meta.confidence` was ABSENT from every envelope the function
has ever produced. A mocked cursor returns whatever the test hands it and proves
none of that; a dead feature is indistinguishable from a feature with no data yet,
which is why it survived. `core.mirror_sync:110` already carried the correct
mapping (`provider AS connector_name`), so the fix is verified against a peer.

The second defect is what the number MEANT once it ran. `app.pull_verifications`
has no project column (migration 007) and `connection_ref` is ORG-owned since
story 21.3, so the query returned the organization's most recent verification for
the connector -- whatever project requested it, over whatever period. The project
identity was reachable and unused: `pull_jobs.datastream_id` -> `datastreams.project_id`
(NOT NULL). `connection_ref.project_id` is deliberately NOT used: migration 037
keeps it "for compat (removed in a later dedicated story)".

The fixture is the exact shape that made the old behaviour wrong -- ONE org, ONE
shared authorization, TWO projects -- because with a single project every query
looks correct.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def two_projects_one_authorization(live_postgres):
    """One org, one credential, two projects, one pull each with different quality."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES ('org_cav','Cav','cav','test') ON CONFLICT DO NOTHING"
        )
        for pid in ("proj_cav_a", "proj_cav_b"):
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, org_id, status, created_by) "
                "VALUES (%s,%s,%s,'org_cav','active','test') ON CONFLICT DO NOTHING",
                (pid, pid, pid),
            )
        # ONE org-owned credential, used by BOTH projects. This is the shape that
        # made the org-wide query wrong.
        cur.execute(
            "INSERT INTO app.connection_ref "
            "(id, project_id, provider, nango_connection_id, owner_org_id, "
            " owner_identity, auth_path) "
            "VALUES ('cref_cav','proj_cav_a','google-ads','nango_cav','org_cav',"
            "'test','nango') ON CONFLICT DO NOTHING"
        )
        for pid, ds in (("proj_cav_a", "ds_cav_a"), ("proj_cav_b", "ds_cav_b")):
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, org_id, name, source_kind, module_name, "
                " lifecycle_state, created_by) "
                "VALUES (%s,%s,'org_cav',%s,'connector_pull','google-ads','active','test') "
                "ON CONFLICT DO NOTHING",
                (ds, pid, ds),
            )
        # Project B pulled MORE RECENTLY and BADLY. Under the old org-wide
        # "ORDER BY verified_at DESC LIMIT 1", project A would inherit B's 0.10.
        for jid, pull, ds, ratio, verified in (
            ("job_cav_a", "pull_cav_a", "ds_cav_a", 0.95, "2026-07-30 10:00+00"),
            ("job_cav_b", "pull_cav_b", "ds_cav_b", 0.10, "2026-07-31 10:00+00"),
        ):
            cur.execute(
                "INSERT INTO app.pull_jobs (id, pull_id, connection_ref_id, date_from, "
                "date_to, state, requested_by, datastream_id) "
                "VALUES (%s,%s,'cref_cav','2026-07-01','2026-07-31','done','test',%s) "
                "ON CONFLICT DO NOTHING",
                (jid, pull, ds),
            )
            cur.execute(
                "INSERT INTO app.pull_verifications (id, pull_id, connection_ref_id, "
                "expected_rows, actual_rows, completeness_ratio, verdict, verified_at) "
                "VALUES (%s,%s,'cref_cav',100,%s,%s,'ok',%s) ON CONFLICT DO NOTHING",
                ("ver_" + pull, pull, int(ratio * 100), ratio, verified),
            )
    conn.commit()
    return conn


def _rows(day: str = "2026-07-15"):
    return [{"date": day, "pull_id": "p", "loaded_at": f"{day}T00:00:00"}]


def test_completeness_describes_this_project_not_the_organization(
    two_projects_one_authorization, monkeypatch
):
    monkeypatch.setenv("PLATFORM_DB_URL", two_projects_one_authorization.info.dsn)
    from core.confidence import compute_confidence

    a = compute_confidence("proj_cav_a", ["google-ads"], rows=_rows(), date_to="2026-07-20")
    b = compute_confidence("proj_cav_b", ["google-ads"], rows=_rows(), date_to="2026-07-20")

    assert a is not None and b is not None, (
        "compute_confidence returned None -- the query failed and was swallowed"
    )
    # Each project reads its OWN pull. B's more recent, worse pull does not leak.
    assert a["completeness"] == 0.95
    assert b["completeness"] == 0.10


def test_a_report_window_outside_the_pull_window_borrows_nothing(
    two_projects_one_authorization, monkeypatch
):
    monkeypatch.setenv("PLATFORM_DB_URL", two_projects_one_authorization.info.dsn)
    from core.confidence import compute_confidence

    assert compute_confidence(
        "proj_cav_a", ["google-ads"], rows=_rows("2025-01-05"), date_to="2025-01-10"
    ) is None


def test_the_scope_disclosure_matches_what_the_query_actually_did(
    two_projects_one_authorization, monkeypatch
):
    """The disclosure is read by a human and a model; it must not outlive the query."""
    monkeypatch.setenv("PLATFORM_DB_URL", two_projects_one_authorization.info.dsn)
    from core.confidence import compute_confidence

    result = compute_confidence(
        "proj_cav_a", ["google-ads"], rows=_rows(), date_to="2026-07-20"
    )
    assert "this project's Datastreams" in result["completeness_scope"]
    assert "no Datastream carry no project identity" in result["completeness_scope"]


def test_an_unreadable_window_measures_nothing_rather_than_today(
    two_projects_one_authorization, monkeypatch
):
    """No window, no number -- and no query against a window nobody asked about.

    `_date_to` fell back to `datetime.now()`, so a report whose end date was
    absent or malformed was measured against TODAY: the completeness lookup ran
    over a period the caller never named, and freshness -- anchored on "now",
    which is always close to a recent load -- came out at 1.0. The unknown was
    absorbed as an answer instead of being stated, which is the defect the rest of
    this module was rewritten to stop.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", two_projects_one_authorization.info.dsn)
    from core.confidence import compute_confidence

    for unreadable in (None, "", "not-a-date"):
        result = compute_confidence(
            "proj_cav_a", ["google-ads"], rows=_rows(), date_to=unreadable
        )
        assert result is not None, f"date_to={unreadable!r}: nothing was said at all"
        assert result["completeness"] is None, (
            f"date_to={unreadable!r}: a completeness ratio was produced for a "
            f"window nobody named -- {result['completeness']}"
        )
        assert result["freshness"] is None, (
            f"date_to={unreadable!r}: freshness {result['freshness']} was measured "
            "against today"
        )
        assert "score" not in result, (
            "the compensating scalar is gone (`proactive-assertions.md`: a single "
            "score merges evidence of different natures) and must not return"
        )
        assert set(result["unknown_terms"]) >= {"completeness", "freshness"}
        assert "window could not be read" in result["completeness_scope"]


def test_the_window_that_is_named_is_still_measured(
    two_projects_one_authorization, monkeypatch
):
    """The guard above must refuse the unknown window, not every window."""
    monkeypatch.setenv("PLATFORM_DB_URL", two_projects_one_authorization.info.dsn)
    from core.confidence import compute_confidence

    result = compute_confidence(
        "proj_cav_a", ["google-ads"], rows=_rows(), date_to="2026-07-20"
    )
    assert result["completeness"] == 0.95
    assert result["freshness"] is not None


def test_connection_ref_has_provider_and_not_connector_name(live_postgres):
    """The structural guard: the column the old query named does not exist.

    Kept as a test rather than a comment because the failure mode was silent --
    `except Exception: return None` turned a schema drift into a missing field
    that looked like missing data.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='app' AND table_name='connection_ref'"
        )
        columns = {row[0] for row in cur.fetchall()}

    assert "provider" in columns
    assert "connector_name" not in columns
