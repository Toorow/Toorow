"""What each collected day was ASKED to carry -- amendment 14, lot C2.

Amendment 14 of `datastream-workbench-and-wizard.md` (ratified 2026-08-11):
« Ajouter une dimension déclare une DETTE D'HISTORIQUE, et l'écran la nomme »,
and the measure « se lit de ce que l'extrait a réellement rendu, jamais du plan
actif ».

The per-day per-field truth the amendment asks for does not exist in this
repository -- no table records the columns a pull returned -- so what this module
publishes is a DIFFERENT fact, stated as such: what the plan version each run
executed under asked the provider for. These tests hold the ways that
substitution could quietly become a lie:

  * the ACTIVE plan leaking into a past day, which is the one thing the amendment
    forbids by name;
  * a run of another Datastream naming the dimensions of this one's day;
  * an unreadable history reported as "no day is missing", which is the same
    payload a real zero would produce;
  * a day that landed nothing counted as owing a dimension, so a confirmation
    proposes to spend on days with no row;
  * `max_provider_backfill_days` defaulted instead of reported absent -- measured
    2026-08-12, 0 of the 140 declared reports fill it, so a default would promise
    a reach no provider committed to.

A REAL POSTGRES, because the whole subject is a three-table join over immutable
ledgers. A stubbed connection would prove the dict shape and none of the chain.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, timedelta

import pytest
from ulid import ULID

psycopg = pytest.importorskip("psycopg")

from core.datastream_dimension_history import (  # noqa: E402
    MEASURED,
    NO_HISTORY,
    NOT_APPLICABLE,
    UNREADABLE,
    read_dimension_history,
)

DSN = os.environ.get("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_DSN not set -- skipping live Postgres test"
)

TODAY = date(2026, 8, 12)
#: Inside the 92-day window, which ends yesterday.
DAY_OLD = date(2026, 7, 1)
DAY_NEW = date(2026, 8, 1)


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as connection:
        yield connection
        # Nothing this test writes survives it.
        connection.rollback()


def _plan(cur, *, plan_id, datastream_id, project_id, number, dimensions, hashes):
    payload = {
        "source": {
            "kind": "connector_pull",
            "module": "example_connector",
            "report_id": "catalog_daily",
            "selection": {
                "metrics": ["clicks"],
                "dimensions": dimensions,
                "grain": ["date"],
            },
        }
    }
    cur.execute(
        """INSERT INTO app.datastream_plan_versions
             (id, datastream_id, project_id, version_number, contract_version,
              source_kind, writer_kind, destination_policy, normalized_payload,
              content_hash, idempotency_key_hash, created_by)
           VALUES (%s, %s, %s, %s, 'v1', 'connector_pull', 'toorow', 'managed_raw',
                   %s::jsonb, %s, %s, 'system')""",
        (
            plan_id,
            datastream_id,
            project_id,
            number,
            json.dumps(payload),
            hashes[0],
            hashes[1],
        ),
    )


@pytest.fixture
def flux(conn):
    """One Datastream, two plan versions, and two collected days.

    Version 1 asks for `date` alone; version 2 adds `campaign_id`. One day was
    collected by a run pinned to version 1, the other by a run pinned to version
    2. Every identifier is fictional and rolled back with the connection.
    """
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    datastream_id = f"ds_{suffix}"
    plan_v1 = f"dpv1_{suffix}"
    plan_v2 = f"dpv2_{suffix}"
    mapping_id = f"dmv_{suffix}"
    run_old = f"dse_{ULID()}"
    run_new = f"dse_{ULID()}"

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
            "(id, project_id, org_id, name, module_name, report_profile_id, enabled) "
            "VALUES (%s, %s, %s, %s, 'example_connector', 'rp_example', TRUE)",
            (datastream_id, project_id, org_id, f"Flux {suffix}"),
        )
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) VALUES (%s, %s, %s)",
            (project_id, datastream_id, org_id),
        )
        _plan(
            cur,
            plan_id=plan_v1,
            datastream_id=datastream_id,
            project_id=project_id,
            number=1,
            dimensions=["date"],
            hashes=("a" * 64, "b" * 64),
        )
        _plan(
            cur,
            plan_id=plan_v2,
            datastream_id=datastream_id,
            project_id=project_id,
            number=2,
            dimensions=["date", "campaign_id"],
            hashes=("c" * 64, "d" * 64),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, executable, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', %s, %s, %s, '1.0', '1.0', TRUE,
                       '{"grain": ["date"]}'::jsonb, '{}'::jsonb, %s, 'system')""",
            (mapping_id, datastream_id, project_id, "e" * 64, plan_v1, "f" * 64, "ab" * 32),
        )
        # THE POINTER IS SET TO VERSION 2 ON PURPOSE. The active plan asks for
        # `campaign_id`; the OLD day's run did not. A measure that read the
        # pointer would report the old day as carrying it, which is precisely
        # what the amendment forbids.
        cur.execute(
            "UPDATE app.datastreams SET current_plan_version_id = %s, "
            "current_mapping_version_id = %s WHERE id = %s",
            (plan_v2, mapping_id, datastream_id),
        )
        for run_id, plan_id in ((run_old, plan_v1), (run_new, plan_v2)):
            cur.execute(
                """INSERT INTO app.datastream_executions
                     (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                      state, created_by)
                   VALUES (%s, %s, %s, %s, %s, 'published', 'system')""",
                (run_id, datastream_id, project_id, plan_id, mapping_id),
            )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "(id, provider, project_id, owner_org_id, owner_identity, nango_connection_id) "
            "VALUES (%s, 'example_connector', %s, %s, 'owner@example.com', %s)",
            (f"conn_{suffix}", project_id, org_id, f"nango_{suffix}"),
        )
        for index, (day, run_id) in enumerate(((DAY_OLD, run_old), (DAY_NEW, run_new))):
            cur.execute(
                """INSERT INTO app.pull_jobs
                     (id, pull_id, connection_ref_id, date_from, date_to, state,
                      requested_by, datastream_id, execution_id, completed_at)
                   VALUES (%s, %s, %s, %s, %s, 'done', 'system', %s, %s, %s)""",
                (
                    f"pj{index}_{suffix}",
                    f"pull{index}_{suffix}",
                    f"conn_{suffix}",
                    day,
                    day,
                    datastream_id,
                    run_id,
                    f"{day.isoformat()}T06:00:00+00:00",
                ),
            )
    return {
        "project_id": project_id,
        "datastream_id": datastream_id,
        "plan_v1": plan_v1,
        "plan_v2": plan_v2,
        "run_old": run_old,
        "run_new": run_new,
        "suffix": suffix,
        "org_id": org_id,
    }


def _read(conn, flux, **kwargs):
    return read_dimension_history(
        conn,
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        source_kind="connector_pull",
        today=TODAY,
        **kwargs,
    )


def _day(history, day: date):
    return next(entry for entry in history["days"] if entry["date"] == day.isoformat())


def test_a_past_day_resolves_to_the_plan_version_its_own_run_pinned(conn, flux):
    """The whole point: the ACTIVE plan never answers for a day it did not run.

    Version 2 is `current_plan_version_id` and declares `campaign_id`; the July
    day was collected by a run pinned to version 1, which does not. Reading the
    pointer would have said the day carries it.
    """
    history = _read(conn, flux)
    assert history["state"] == MEASURED
    assert _day(history, DAY_OLD)["plan_version_id"] == flux["plan_v1"]
    assert _day(history, DAY_NEW)["plan_version_id"] == flux["plan_v2"]
    assert history["plan_dimensions"][flux["plan_v1"]] == ["date"]
    assert history["plan_dimensions"][flux["plan_v2"]] == ["date", "campaign_id"]


def test_a_dimension_no_version_ever_declared_is_absent_from_every_declaration(conn, flux):
    """The case of the gesture itself, and the only one that needs no run.

    `campaign_name` is in no version, so `first_declared` cannot name one -- and
    a field absent from that map is a field no run can have asked for. It is the
    exact answer amendment 14 wants in the confirmation.
    """
    history = _read(conn, flux)
    assert "campaign_name" not in history["first_declared"]
    assert set(history["first_declared"]) == {"date", "campaign_id"}
    assert history["first_declared"]["campaign_id"]["plan_version_id"] == flux["plan_v2"]
    assert history["first_declared"]["date"]["plan_version_id"] == flux["plan_v1"]


def test_a_run_of_another_datastream_never_names_this_one_s_dimensions(conn, flux):
    """`pull_jobs.execution_id` carries no composite key to (stream, project).

    A window pointed at a foreign run must resolve to NO plan version rather than
    to that run's. The day then reads as unattributed, which is an honest answer;
    borrowing the other stream's dimensions would not be.
    """
    other = f"dse_{ULID()}"
    with conn.cursor() as cur:
        # A second Datastream in the SAME project, with its own run.
        second = f"ds2_{flux['suffix']}"
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, module_name, report_profile_id, enabled) "
            "VALUES (%s, %s, %s, 'Second', 'example_connector', 'rp_example', TRUE)",
            (second, flux["project_id"], flux["org_id"]),
        )
        _plan(
            cur,
            plan_id=f"dpv3_{flux['suffix']}",
            datastream_id=second,
            project_id=flux["project_id"],
            number=1,
            dimensions=["date", "campaign_name"],
            hashes=("1" * 64, "2" * 64),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, executable, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', %s, %s, %s, '1.0', '1.0', TRUE,
                       '{}'::jsonb, '{}'::jsonb, %s, 'system')""",
            (
                f"dmv2_{flux['suffix']}",
                second,
                flux["project_id"],
                "3" * 64,
                f"dpv3_{flux['suffix']}",
                "4" * 64,
                "5" * 64,
            ),
        )
        cur.execute(
            """INSERT INTO app.datastream_executions
                 (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                  state, created_by)
               VALUES (%s, %s, %s, %s, %s, 'published', 'system')""",
            (
                other,
                second,
                flux["project_id"],
                f"dpv3_{flux['suffix']}",
                f"dmv2_{flux['suffix']}",
            ),
        )
        cur.execute(
            "UPDATE app.pull_jobs SET execution_id = %s WHERE datastream_id = %s "
            "AND date_from = %s",
            (other, flux["datastream_id"], DAY_OLD),
        )

    history = _read(conn, flux)
    day = _day(history, DAY_OLD)
    assert day["execution_id"] == other
    assert day["plan_version_id"] is None
    assert other not in history["plan_dimensions"]


def test_a_day_that_landed_nothing_is_not_a_day_that_owes_a_dimension(conn, flux):
    """Five buckets, never one "missing" total.

    A failed window landed no row, so no row of it lacks a dimension. Counting it
    would put days with nothing on them into a confirmation that names a spend.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.pull_jobs SET state = 'failed' WHERE datastream_id = %s "
            "AND date_from = %s",
            (flux["datastream_id"], DAY_OLD),
        )
    history = _read(conn, flux)
    assert _day(history, DAY_OLD)["state"] == "not_landed"
    assert history["counts"]["not_landed"] == 1
    assert history["counts"]["landed"] == 1
    # The window is 92 days; everything else was never fetched at all.
    assert history["counts"]["never_collected"] == history["window"]["days"] - 2


def test_a_stream_with_no_landed_day_says_so_and_never_reads_as_no_debt(conn, flux):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.pull_jobs SET state = 'failed' WHERE datastream_id = %s",
            (flux["datastream_id"],),
        )
    history = _read(conn, flux)
    assert history["state"] == NO_HISTORY
    assert "days" not in history
    assert "no history" in history["reason"]


def test_an_unreadable_history_is_not_an_empty_one(conn, flux):
    """A read that fails says so. It never answers the payload of a real zero."""

    class _Broken:
        def cursor(self, *args, **kwargs):
            raise RuntimeError("relation unavailable")

    history = read_dimension_history(
        _Broken(),
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        source_kind="connector_pull",
        today=TODAY,
    )
    assert history["state"] == UNREADABLE
    assert "days" not in history
    assert "could not be read" in history["reason"]


def test_a_mode_that_pulls_nothing_owes_nothing_and_says_which(conn, flux):
    for mode in ("managed_feed", "external_bq"):
        history = read_dimension_history(
            conn,
            project_id=flux["project_id"],
            datastream_id=flux["datastream_id"],
            source_kind=mode,
            today=TODAY,
        )
        assert history["state"] == NOT_APPLICABLE
        assert "asks a provider for nothing" in history["reason"]


def test_the_provider_bound_is_reported_absent_and_never_defaulted(conn, flux):
    """Measured 2026-08-12: 0 of the 140 declared reports fill the key.

    An absent bound must stay UNKNOWN -- `None`, `unavailable`, no earliest day.
    Defaulting it would promise that every day of the window can be asked for
    again, which no provider has committed to.
    """
    history = _read(conn, flux)
    assert history["max_provider_backfill_days"] is None
    assert history["backfill_bound_evidence"] == "unavailable"
    assert history["earliest_recoverable"] is None

    bounded = _read(conn, flux, max_provider_backfill_days=30)
    assert bounded["max_provider_backfill_days"] == 30
    assert bounded["backfill_bound_evidence"] == "provider_capability"
    assert bounded["earliest_recoverable"] == (TODAY - timedelta(days=30)).isoformat()

    # `True` is an `int` in Python and is not a number of days.
    assert _read(conn, flux, max_provider_backfill_days=True)["earliest_recoverable"] is None
    assert _read(conn, flux, max_provider_backfill_days=0)["earliest_recoverable"] is None


def test_the_window_ends_yesterday_so_today_is_never_counted_as_missing(conn, flux):
    history = _read(conn, flux)
    assert history["window"]["to"] == (TODAY - timedelta(days=1)).isoformat()
    assert history["window"]["days"] == 92
    assert history["window"]["from"] == (TODAY - timedelta(days=92)).isoformat()
    assert all(entry["date"] < TODAY.isoformat() for entry in history["days"])


def test_the_payload_states_what_it_measures_and_what_it_cannot(conn, flux):
    """The sentence that makes the substitution honest rather than silent."""
    history = _read(conn, flux)
    assert "asked the provider for" in history["basis"]
    assert "never from the plan in force today" in history["basis"]
    assert "recorded nowhere" in history["unmeasurable"]
    assert history["refetch_path"] == (
        f"/api/projects/{flux['project_id']}/datastreams/{flux['datastream_id']}/refetch"
    )


def test_no_project_means_no_refetch_address_rather_than_a_broken_one(conn):
    history = read_dimension_history(
        conn,
        project_id="",
        datastream_id="ds_EXAMPLE",
        source_kind="managed_feed",
        today=TODAY,
    )
    assert history["refetch_path"] is None
