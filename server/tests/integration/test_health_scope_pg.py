"""Live-Postgres proof that the freshness verdict describes THIS project and THIS window.

Story 53.3, second pass. The completeness half of this story was scoped through
the governed chain (`pull_jobs.datastream_id` -> `datastreams.project_id`) and is
proven next door in `test_confidence_scope_pg.py`. The freshness half was not: it
filtered on `r.owner_org_id = (SELECT org_id FROM app.projects WHERE id = %s)` --
the ORGANIZATION -- and bound no date at all. A story titled "freshness and
completeness describe this project and this window" shipped a freshness read that
described neither.

WHY THESE ASSERTIONS NEED A DATABASE. The suite that covered this behaviour used
a double answering on `"connection_health" in statement`, which ignores the
`WHERE` entirely. Measured before this repair: replacing the scope predicate with
`WHERE (%s IS NOT NULL OR TRUE)` -- every organization's connections -- left the
file at its exact baseline, and so did deleting the provider filter. Only a real
database can tell "the query ran" from "the query answered about the right rows".

The fixture is the shape that makes the defect visible, and no smaller: ONE org,
TWO projects, TWO org-owned credentials -- because with one project every scope
looks correct, and with one credential every provider filter looks correct.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def one_org_two_projects_two_credentials(live_postgres):
    """Project A pulls a healthy connector; project B, next door, has a revoked one.

    Both credentials are owned by the same organization, which is the ordinary
    case: one authorization, several projects. A also has a SECOND credential,
    revoked, for a connector that contributed nothing to the report -- that is
    what the provider filter exists for.
    """
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES ('org_hs','Health Scope','health-scope','test') ON CONFLICT DO NOTHING"
        )
        for pid in ("proj_hs_a", "proj_hs_b"):
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, org_id, status, created_by) "
                "VALUES (%s,%s,%s,'org_hs','active','test') ON CONFLICT DO NOTHING",
                (pid, pid, pid),
            )
        # (credential, provider, health status)
        credentials = (
            ("cref_hs_a", "google-ads", "ok"),            # project A's contributor
            ("cref_hs_a_other", "meta-ads", "revoked"),   # project A, contributed nothing
            ("cref_hs_b", "google-ads", "revoked"),       # project B, another project
        )
        for cref, provider, status in credentials:
            cur.execute(
                "INSERT INTO app.connection_ref "
                "(id, project_id, provider, nango_connection_id, owner_org_id, "
                " owner_identity, auth_path) "
                "VALUES (%s,'proj_hs_a',%s,%s,'org_hs','test','nango') "
                "ON CONFLICT DO NOTHING",
                (cref, provider, "nango_" + cref),
            )
            cur.execute(
                "INSERT INTO app.connection_health "
                "(connection_ref_id, status, last_fetched_at, last_checked_at) "
                "VALUES (%s,%s,'2026-07-31 06:00+00', now()) "
                "ON CONFLICT (connection_ref_id) DO UPDATE SET status = EXCLUDED.status",
                (cref, status),
            )
        # (datastream, project, module, credential)
        datastreams = (
            ("ds_hs_a", "proj_hs_a", "google-ads", "cref_hs_a"),
            ("ds_hs_a_other", "proj_hs_a", "meta-ads", "cref_hs_a_other"),
            ("ds_hs_b", "proj_hs_b", "google-ads", "cref_hs_b"),
        )
        for ds, pid, module, cref in datastreams:
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, org_id, name, source_kind, module_name, "
                " connection_ref_id, lifecycle_state, created_by) "
                "VALUES (%s,%s,'org_hs',%s,'connector_pull',%s,%s,'active','test') "
                "ON CONFLICT DO NOTHING",
                (ds, pid, ds, module, cref),
            )
            cur.execute(
                "INSERT INTO app.pull_jobs (id, pull_id, connection_ref_id, date_from, "
                "date_to, state, requested_by, datastream_id) "
                "VALUES (%s,%s,%s,'2026-07-01','2026-07-31','done','test',%s) "
                "ON CONFLICT DO NOTHING",
                ("job_" + ds, "pull_" + ds, cref, ds),
            )
    conn.commit()
    return conn


def _envelope(contributor: str | None = "google-ads") -> dict:
    """A minimal canonical envelope, with or without provenance."""
    provenance = (
        [{"source_system": contributor, "source_field": "fact_daily_kpi", "pull_id": "p"}]
        if contributor
        else []
    )
    return {
        "meta": {"provenance": provenance, "freshness": {}, "alerts": []},
        "data": {"date_range": {"start": "2026-07-01", "end": "2026-07-31"}, "rows": []},
    }


def _alert_codes(envelope: dict) -> list[str]:
    return [a.get("code") for a in (envelope.get("meta") or {}).get("alerts", [])]


def test_freshness_describes_this_project_not_the_organization(
    one_org_two_projects_two_credentials, monkeypatch
):
    """Project A is healthy; project B's revoked credential is not A's business.

    Drop `ds.project_id = %s` and A inherits B's `auth_expired` banner -- the
    behaviour that shipped.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", one_org_two_projects_two_credentials.info.dsn)
    from core.health_enrichment import enrich_envelope_with_health

    a = enrich_envelope_with_health(
        _envelope(), "proj_hs_a", date_from="2026-07-01", date_to="2026-07-31"
    )
    b = enrich_envelope_with_health(
        _envelope(), "proj_hs_b", date_from="2026-07-01", date_to="2026-07-31"
    )

    assert a["meta"]["freshness"]["stale_since_evaluated"] is True, (
        "project A's health was never evaluated -- the governed chain found nothing"
    )
    assert "auth_expired" not in _alert_codes(a), (
        "project A carries the revoked banner of a credential it does not use: "
        f"{_alert_codes(a)}"
    )
    assert "auth_expired" in _alert_codes(b), (
        "project B's own revoked credential is missing from its report -- the "
        "scope is now too narrow, which is a different defect and just as wrong"
    )


def test_a_report_window_outside_the_pull_window_evaluates_nothing(
    one_org_two_projects_two_credentials, monkeypatch
):
    """January cannot be described by a connection that only ever served July.

    No date entered the rejected query. `stale_since_evaluated` stays False, which
    reads "nobody looked" -- the honest answer, and the one the builders already
    emit (`overview.md:38`: missing evidence is Unknown, never Healthy).
    """
    monkeypatch.setenv("PLATFORM_DB_URL", one_org_two_projects_two_credentials.info.dsn)
    from core.health_enrichment import enrich_envelope_with_health

    out = enrich_envelope_with_health(
        _envelope("meta-ads"), "proj_hs_a", date_from="2025-01-01", date_to="2025-01-31"
    )

    assert out["meta"]["freshness"].get("stale_since_evaluated") is not True
    assert "auth_expired" not in _alert_codes(out), (
        "a revoked credential from another period banners this report: "
        f"{_alert_codes(out)}"
    )


def test_a_connector_that_contributed_nothing_cannot_banner_the_report(
    one_org_two_projects_two_credentials, monkeypatch
):
    """The provider filter is the story's own addition and nothing tested it.

    Project A owns a revoked `meta-ads` credential in the same window. The report
    was built from `google-ads` alone, and `meta.provenance` says so. Delete
    `AND r.provider = ANY(%s)` and this goes red.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", one_org_two_projects_two_credentials.info.dsn)
    from core.health_enrichment import enrich_envelope_with_health

    out = enrich_envelope_with_health(
        _envelope("google-ads"), "proj_hs_a", date_from="2026-07-01", date_to="2026-07-31"
    )

    assert out["meta"]["freshness"]["stale_since_evaluated"] is True
    assert "auth_expired" not in _alert_codes(out), (
        "a connector absent from meta.provenance decided this report's health: "
        f"{_alert_codes(out)}"
    )


def test_without_provenance_the_fallback_stays_inside_the_project_and_window(
    one_org_two_projects_two_credentials, monkeypatch
):
    """The fallback widens by ONE clause -- the provider -- and by nothing else.

    The rejected version's fallback read org-wide across every provider, all time:
    the original defect, rewritten in the branch that runs when provenance is
    missing. Here project A's own revoked `meta-ads` DOES decide (it is a real
    contributor when nothing says otherwise, and the worst input wins), while
    project B's credential still cannot reach it.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", one_org_two_projects_two_credentials.info.dsn)
    from core.health_enrichment import enrich_envelope_with_health

    out = enrich_envelope_with_health(
        _envelope(None), "proj_hs_a", date_from="2026-07-01", date_to="2026-07-31"
    )

    assert "auth_expired" in _alert_codes(out), (
        "with no provenance the worst connection of this project must decide"
    )

    outside = enrich_envelope_with_health(
        _envelope(None), "proj_hs_a", date_from="2025-01-01", date_to="2025-01-31"
    )
    assert outside["meta"]["freshness"].get("stale_since_evaluated") is not True, (
        "the fallback ignored the window"
    )


def test_an_unreadable_window_is_not_evaluated_rather_than_defaulted(
    one_org_two_projects_two_credentials, monkeypatch
):
    """No window, no verdict. Today is not a substitute for the period asked about."""
    monkeypatch.setenv("PLATFORM_DB_URL", one_org_two_projects_two_credentials.info.dsn)
    from core.health_enrichment import enrich_envelope_with_health

    envelope = _envelope()
    envelope["data"]["date_range"] = {"start": "not-a-date", "end": "not-a-date"}
    out = enrich_envelope_with_health(envelope, "proj_hs_a")

    assert out["meta"]["freshness"].get("stale_since_evaluated") is not True
    assert out["meta"]["freshness"].get("stale_since") is None
