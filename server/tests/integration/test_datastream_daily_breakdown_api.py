"""The day-grain route, walked end to end on real rows -- story 58.1, epic 58.

THROUGH `build_asgi_app()` AND AGAINST POSTGRES, because the two things this file
exists to prove cannot be proved anywhere else: that the ADDRESS exists, and that
a real `app.pull_jobs` row joined to a real `app.pull_verifications` row comes out
of the payload as the day it describes. A mocked cursor answers whatever the
fixture felt like, so it can neither mount a route nor execute the ledger's own
SQL -- and that SQL is where the two defects of story 8.3's review lived.

WHAT THE FIXTURE DELIBERATELY DOES NOT DO. It never commits: everything happens
inside the connection's transaction and is rolled back, so the append-only version
tables are never asked to forgive a delete and the disposable cluster keeps the
shape the next run expects.
"""

from __future__ import annotations

import hashlib
import os
import random
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live daily-breakdown walk skipped",
)

IDENTITY = "reader@example.com"
AUTHOR = "story-58-1"

#: A window entirely in the past, so nothing a scheduler does can move it.
DAY = "2026-06-15"
WINDOW = {"start": "2026-06-14", "end": "2026-06-16"}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


#: Crockford base32 without I, L, O and U -- the alphabet the `ck_*_id` checks of
#: the Semantic Model spell out, and the reason ids there cannot be a hex slice.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(prefix: str) -> str:
    return prefix + "".join(random.choice(_CROCKFORD) for _ in range(26))


@pytest.fixture
def a_stream(live_postgres, monkeypatch):
    """One org, one owner, one project, one connection, one Datastream."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "daily-breakdown-local-token")
    # Required as soon as auth is not `disabled` (`analyze_feedback.py:91-93`):
    # it signs the feedback context, and an unsigned context is one nobody can
    # trust. This fixture turned auth on and left it out, so the refusal it met
    # was about the secret rather than about breakdowns.
    monkeypatch.setenv(
        "TOOROW_FEEDBACK_CONTEXT_SECRET", "test-fixture-feedback-secret-not-a-real-one"
    )
    conn = live_postgres
    org_id, project_id = _id("org_"), _id("proj_")
    ds_id, connection_ref_id = _id("ds_"), _id("cref_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (org_id, org_id, org_id, AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status)"
            " VALUES (%s, %s, %s, 'owner', 'active')",
            (_id("mem_"), org_id, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (project_id, project_id, project_id, AUTHOR, org_id),
        )
        cur.execute(
            "INSERT INTO app.connection_ref"
            " (id, provider, nango_connection_id, project_id, status, enabled,"
            "  owner_org_id, owner_identity)"
            " VALUES (%s, 'meta-ads', %s, %s, 'active', TRUE, %s, %s)",
            (connection_ref_id, connection_ref_id, project_id, org_id, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, connection_ref_id, enabled, created_by, org_id)"
            " VALUES (%s, %s, 'Stream', 'meta-ads', 'connector_pull', %s, TRUE, %s, %s)",
            (ds_id, project_id, connection_ref_id, AUTHOR, org_id),
        )
    try:
        yield {
            "conn": conn,
            "org_id": org_id,
            "project_id": project_id,
            "ds_id": ds_id,
            "connection_ref_id": connection_ref_id,
        }
    finally:
        conn.rollback()


def _pull(ids, *, date_from, date_to, state, verdict=None, actual_rows=None,
          expected_rows=None):
    """One window of this stream, with the verification that measured it."""
    from core import pull_job_states

    pull_id, job_id = _id("pull_"), _id("job_")
    with ids["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.pull_jobs"
            " (id, pull_id, datastream_id, connection_ref_id, date_from, date_to,"
            "  state, requested_by, completed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s,"
            "         CASE WHEN %s = %s THEN now() ELSE NULL END)",
            (job_id, pull_id, ids["ds_id"], ids["connection_ref_id"], date_from,
             date_to, state, AUTHOR, state, pull_job_states.DONE),
        )
        if verdict is not None:
            cur.execute(
                "INSERT INTO app.pull_verifications"
                " (id, pull_id, connection_ref_id, verdict, actual_rows,"
                "  expected_rows, completeness_ratio, verified_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, now())",
                (_id("pv_"), pull_id, ids["connection_ref_id"], verdict, actual_rows,
                 expected_rows,
                 None if not expected_rows else round(actual_rows / expected_rows, 4)),
            )
    return pull_id


def _mapping_version(ids, payload, *, current=True):
    """A real mapping VERSION of this stream, with its plan version under it.

    THIS IS THE MAJORITY PATH AND NOTHING REAL EXERCISED IT. 328 Datastreams keep
    their fields here and 44 in the flat table; the in-memory double routes on the
    substring `v.mapping_payload` and stays green on SQL Postgres would refuse.
    The `jsonb` traversal, the ordering on `current_mapping_version_id` and the
    join to `app.mdm_canonical_fields` are only proved here.
    """
    import json

    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    with ids["conn"].cursor() as cur:
        # THE ORDINAL AND THE HASHES ARE DERIVED, not spelled (2026-08-31). Both
        # were constants -- `version_number = 1`, `repeat('b', 64)` -- and a walk
        # that wants a SECOND version of the same stream (publish, then a run in
        # flight) died on `UNIQUE (datastream_id, version_number)` and on
        # `UNIQUE (project_id, idempotency_key_hash)`. A fixture callable once per
        # stream cannot walk a stream's life.
        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1"
            " FROM app.datastream_plan_versions WHERE datastream_id = %s",
            (ids["ds_id"],),
        )
        ordinal = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO app.datastream_plan_versions"
            " (id, datastream_id, project_id, version_number, contract_version,"
            "  source_kind, writer_kind, destination_policy, normalized_payload,"
            "  content_hash, idempotency_key_hash, created_by)"
            " VALUES (%s, %s, %s, %s, '1', 'connector_pull', 'toorow', 'managed_raw',"
            "         '{}'::jsonb, %s, %s, %s)",
            (plan_id, ids["ds_id"], ids["project_id"], ordinal,
             _digest(plan_id), _digest(plan_id, "plan-key"), AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.datastream_mapping_versions"
            " (id, datastream_id, project_id, version_number, mapping_contract_version,"
            "  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,"
            "  toorow_extension_version, executable, mapping_payload, ossie_projection,"
            "  idempotency_key_hash, created_by)"
            " VALUES (%s, %s, %s, %s, '1', %s, %s, %s, '0.1.1',"
            "         '1', TRUE, %s::jsonb, '{}'::jsonb, %s, %s)",
            (mapping_id, ids["ds_id"], ids["project_id"], ordinal,
             _digest(mapping_id, "schema"), plan_id, _digest(mapping_id),
             json.dumps(payload), _digest(mapping_id, "map-key"), AUTHOR),
        )
        if current:
            cur.execute(
                "UPDATE app.datastreams SET current_mapping_version_id = %s"
                " WHERE id = %s",
                (mapping_id, ids["ds_id"]),
            )
    return mapping_id


def _digest(*parts: str) -> str:
    """64 lowercase hex characters -- the shape every `*_hash` CHECK requires."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _publish(ids, payload=None) -> dict[str, str]:
    """Publish this Datastream the way the wizard's own writer publishes it.

    WHY THIS EXISTS (2026-08-31). The arming gate landed AFTER the re-collection
    walks below were written: `datastream_dispatch` refuses a `draft` Datastream
    by name -- "This Datastream has never been published" -- before it looks at
    anything else. Three of those walks therefore measured that refusal instead
    of the re-collection, and a fixture that flipped `lifecycle_state` alone
    would have built a row no gesture of this product produces.

    So this writes what the `completing` branch of
    `datastream_activation.materialize_draft_mutation` writes, in its order and
    into the same tables: a plan and a mapping version, a `published` execution,
    the append-only `app.datastream_publication_log` row, and only then the
    pointer swap that carries `lifecycle_state='active'` and `enabled=TRUE`.
    `published` is not one of the five states `uq_datastream_executions_active`
    holds, so a run may still be opened after this -- which is precisely what a
    publication makes possible.
    """
    mapping_id = _mapping_version(ids, payload if payload is not None else {"fields": []})
    execution_id = _ulid("dse_")
    log_id = _ulid("dplog_")
    content_hash = _digest(execution_id, "content")
    with ids["conn"].cursor() as cur:
        cur.execute(
            "SELECT plan_version_id FROM app.datastream_mapping_versions WHERE id = %s",
            (mapping_id,),
        )
        plan_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO app.datastream_executions (id, datastream_id, project_id,"
            " plan_version_id, mapping_version_id, state, content_hash, row_count,"
            " created_by)"
            " VALUES (%s, %s, %s, %s, %s, 'published', %s, 0, %s)",
            (execution_id, ids["ds_id"], ids["project_id"], plan_id, mapping_id,
             content_hash, AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.datastream_publication_log"
            " (id, execution_id, datastream_id, project_id, plan_version_id,"
            "  mapping_version_id, content_hash, row_count, published_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s)",
            (log_id, execution_id, ids["ds_id"], ids["project_id"], plan_id,
             mapping_id, content_hash, AUTHOR),
        )
        cur.execute(
            "UPDATE app.datastreams"
            "   SET current_published_execution_id = %s, current_plan_version_id = %s,"
            "       current_mapping_version_id = %s, lifecycle_state = 'active',"
            "       enabled = TRUE"
            " WHERE id = %s AND project_id = %s",
            (execution_id, plan_id, mapping_id, ids["ds_id"], ids["project_id"]),
        )
    return {
        "plan_version_id": plan_id,
        "mapping_version_id": mapping_id,
        "execution_id": execution_id,
    }


def _no_global_money_concept(ids) -> None:
    """Make the ABSENCE this walk asserts true, inside the walk's own transaction.

    THE DEFECT THIS CLOSES (measured 2026-08-31). `monetary_concept_names` reads
    `c.project_id = %s OR c.project_id IS NULL`, so a GLOBAL published money
    Concept is visible to every Project. The disposable cluster carries two,
    committed by another suite (`monetary_probe_*`, `created_by = 'system'`), and
    an absence test whose premise is a row no fixture owns is not measuring the
    route: it was green only because the capability gate short-circuited before
    the Concept read, and turning the capability on to reach that read is what
    exposed it.

    So the walk STATES its premise instead of hoping for it. The rows are set
    aside inside the fixture's transaction, which is rolled back whole at
    teardown, so nothing another suite committed is destroyed -- and the reading
    below is taken against the estate the test claims.
    """
    with ids["conn"].cursor() as cur:
        cur.execute(
            """UPDATE app.semantic_concepts c
                  SET lifecycle_status = 'draft'
                 FROM app.semantic_concept_versions v
                WHERE v.id = c.current_version_id
                  AND c.lifecycle_status = 'published'
                  AND c.project_id IS NULL
                  AND v.kind = 'metric'
                  AND v.value_type = 'money'"""
        )


def _activate_currency_fx(ids) -> None:
    """Turn `currency_fx` on for this Project, in the table the product reads.

    `datastream_reading_capabilities` answers every money designation with
    `currency_fx_not_active` until `app.project_capabilities.state` is `ready` or
    `degraded`, and that gate landed after the two money walks below. A
    disposable cluster carries no row at all, so those walks were asking the
    reading a question the Project had never opened -- and were answered by the
    refusal that says exactly that. `always_present` is the availability this
    key's own CHECK demands.
    """
    with ids["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_capabilities"
            " (project_id, capability_key, availability, state)"
            " VALUES (%s, 'currency_fx', 'always_present', 'ready')"
            " ON CONFLICT (project_id, capability_key)"
            " DO UPDATE SET state = 'ready'",
            (ids["project_id"],),
        )


def _canonical_field(ids, identity, name, *, project_scoped=True):
    """One row of the governed MDM registry -- what an identity resolves TO."""
    with ids["conn"].cursor() as cur:
        # `value_type` is NOT NULL since migration 241 and carries no default: a
        # canonical field states the half this table never had, without which it
        # could never become a Concept. These are all `metric ... sum`, so the
        # type they imply is a whole number.
        cur.execute(
            "INSERT INTO app.mdm_canonical_fields"
            " (id, project_id, concept_kind, canonical_name, aggregation,"
            "  value_type, created_by)"
            " VALUES (%s, %s, 'metric', %s, 'sum', 'integer', %s)",
            (identity, ids["project_id"] if project_scoped else None, name, AUTHOR),
        )


def _get(ids, *, project_id=None, params=None):
    """Drive the real application against the fixture's own transaction."""
    from core.main import build_asgi_app

    connection = ids["conn"]

    @contextmanager
    def _open(*_a, **_k):
        yield connection

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        patch("core.db.get_connection", side_effect=_open),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        return client.get(
            f"/api/projects/{project_id or ids['project_id']}"
            f"/datastreams/{ids['ds_id']}/daily-breakdown",
            params=params or WINDOW,
        )


def _no_mart():
    """The mart is not seeded on the disposable cluster, and that is a real state.

    23 of the 39 connectors can never reach `fact_daily_kpi`, so the branch this
    pins is the one most Datastreams take. The warehouse itself is exercised in
    `tests/core/test_cache_warehouse*`; what matters here is that its absence does
    not take the registry down with it.
    """
    return patch(
        "core.cache_warehouse.read_daily_row_counts",
        return_value={"connector_present": False, "counts": {}},
    )


# ---------------------------------------------------------------------------
# The address exists.
# ---------------------------------------------------------------------------


def test_the_route_is_mounted_on_the_application() -> None:
    from core import admin_api
    from core.datastream_daily_breakdown_api import DAILY_BREAKDOWN_ROUTE_PATH

    assert any(
        getattr(route, "path", "") == DAILY_BREAKDOWN_ROUTE_PATH
        for route in admin_api.router.routes
    ), "the day-grain read is not reachable from the application router"


# ---------------------------------------------------------------------------
# What a real pull becomes on the wire.
# ---------------------------------------------------------------------------


@_skip_without_dsn
def test_a_done_pull_with_an_ok_verdict_is_an_ok_day(a_stream) -> None:
    from core import pull_job_states

    pull_id = _pull(a_stream, date_from=DAY, date_to=DAY,
                    state=pull_job_states.DONE, verdict="ok",
                    actual_rows=1200, expected_rows=1200)

    with _no_mart():
        response = _get(a_stream)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema"] == "datastream_daily_breakdown.v1"
    assert payload["datastream_id"] == a_stream["ds_id"]
    assert [day["date"] for day in payload["days"]] == [
        "2026-06-14", "2026-06-15", "2026-06-16"
    ]
    day = payload["days"][1]
    assert day["extract_status"] == "ok"
    assert day["job_state"] == pull_job_states.DONE
    assert day["pull_id"] == pull_id
    assert day["extract_count"] == 1
    # A one-day window IS its day, so the measured volume is published.
    assert day["row_count"] == 1200
    assert day["row_count_reason"] is None
    assert day["loaded_at"] is not None


@_skip_without_dsn
def test_a_partial_verdict_reaches_the_day_as_partial(a_stream) -> None:
    from core import pull_job_states

    _pull(a_stream, date_from=DAY, date_to=DAY, state=pull_job_states.DONE,
          verdict="partial", actual_rows=30, expected_rows=1200)

    with _no_mart():
        payload = _get(a_stream).json()

    day = payload["days"][1]
    assert day["extract_status"] == "partial"
    assert day["row_count"] == 30
    assert day["expected_rows"] == 1200
    assert day["completeness_ratio"] == pytest.approx(0.025)


@_skip_without_dsn
def test_a_backfill_publishes_no_daily_volume_it_never_measured(a_stream) -> None:
    """Arbitrage 9, against the real join: one verification, three days, no total.

    `pull_verifications.actual_rows` is one row per `pull_id`. The ledger's own
    LEFT JOIN is what used to spread it across every covered day.
    """
    from core import pull_job_states
    from core.extract_ledger import ROW_COUNT_MEASURED_PER_WINDOW

    _pull(a_stream, date_from="2026-06-14", date_to="2026-06-16",
          state=pull_job_states.DONE, verdict="ok",
          actual_rows=3600, expected_rows=3600)

    with _no_mart():
        payload = _get(a_stream).json()

    assert [day["row_count"] for day in payload["days"]] == [None, None, None]
    assert {day["row_count_reason"] for day in payload["days"]} == {
        ROW_COUNT_MEASURED_PER_WINDOW
    }
    assert all(day["extract_status"] == "ok" for day in payload["days"])


@_skip_without_dsn
def test_a_stopped_window_is_not_reported_as_a_day_nobody_asked_for(a_stream) -> None:
    from core import pull_job_states

    _pull(a_stream, date_from=DAY, date_to=DAY, state=pull_job_states.CANCELLED)

    with _no_mart():
        day = _get(a_stream).json()["days"][1]

    assert day["extract_status"] == pull_job_states.LEDGER_NEVER_FETCHED
    assert day["job_state"] == pull_job_states.CANCELLED


@_skip_without_dsn
def test_the_mart_absence_is_named_and_never_answered_with_a_zero(a_stream) -> None:
    from core import pull_job_states
    from core.datastream_daily_breakdown_api import ROWS_CONNECTOR_NOT_IN_MART

    _pull(a_stream, date_from=DAY, date_to=DAY, state=pull_job_states.DONE,
          verdict="ok", actual_rows=10, expected_rows=10)

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["connector"] == "meta-ads"
    assert all(day["rows"] is None for day in payload["days"])
    assert all(
        day["rows_reason"] == ROWS_CONNECTOR_NOT_IN_MART for day in payload["days"]
    )


@_skip_without_dsn
def test_the_header_and_the_rows_are_published_without_a_join_between_them(
    a_stream,
) -> None:
    from core.datastream_daily_breakdown_api import (
        COLUMN_ROW_JOIN_REASON,
        COLUMNS_FROM_FLAT_TABLE,
        COLUMNS_NO_MAPPING,
    )

    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastream_mappings"
            " (datastream_id, source_field, target_field, is_key_column)"
            " VALUES (%s, 'spend', 'cost', FALSE), (%s, 'date', 'date', TRUE)",
            (a_stream["ds_id"], a_stream["ds_id"]),
        )

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["columns"] == [
        {"source_field": "date", "target_field": "date", "is_key_column": True,
         "binding_status": None, "sensitivity": "unknown"},
        {"source_field": "spend", "target_field": "cost", "is_key_column": False,
         "binding_status": None, "sensitivity": "unknown"},
    ]
    assert payload["columns_reason"] is None
    assert payload["columns_reason"] != COLUMNS_NO_MAPPING
    # This flux has no mapping VERSION, so the flat table is what answered --
    # and story 58.2 makes the payload say which of the two stores it was.
    assert payload["columns_source"] == COLUMNS_FROM_FLAT_TABLE
    # And the response says what it cannot do with them.
    assert payload["column_row_join_available"] is False
    assert payload["column_row_join_reason"] == COLUMN_ROW_JOIN_REASON


@_skip_without_dsn
def test_a_versioned_flux_reads_its_fields_and_resolves_the_mdm_identity(
    a_stream,
) -> None:
    """The path 328 of the 372 Datastreams take, on real rows.

    Everything here is what the in-memory double cannot answer: `jsonb`
    traversal of `mapping_payload -> fields[]`, the ordering that prefers the
    stream's CURRENT mapping version, and the join to `app.mdm_canonical_fields`
    that turns an `mdm_<ULID>` into a name a person can read.
    """
    from core.datastream_daily_breakdown_api import COLUMNS_FROM_MAPPING_VERSION

    clicks = "mdm_" + "0123456789ABCDEFGHJKMNPQRS"
    net_cost = "mdm_" + "TVWXYZ0123456789ABCDEFGHJK"
    _canonical_field(a_stream, clicks, "clicks_total")
    _canonical_field(a_stream, net_cost, "net_media_cost_micros")
    _mapping_version(a_stream, {
        "grain": ["media_date"],
        "fields": [
            {"field_id": "clicks",
             "binding": {"status": "confirmed", "mdm_target": clicks}},
            {"field_id": "media_date",
             "binding": {"status": "confirmed", "mdm_target": None,
                         "canonical_target": "media_date"}},
            # The identity under the OTHER key -- 105 of the 708 measured
            # bindings are shaped exactly like this.
            {"field_id": "net_cost",
             "binding": {"status": "confirmed", "mdm_target": None,
                         "canonical_target": net_cost}},
            # And one nothing binds, which must stay in the header.
            {"field_id": "audience_label",
             "binding": {"status": "suggested", "mdm_target": None}},
        ],
    })

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["columns_source"] == COLUMNS_FROM_MAPPING_VERSION
    assert payload["columns"] == [
        {"source_field": "media_date", "target_field": "media_date",
         "is_key_column": True, "binding_status": "confirmed", "sensitivity": "unknown"},
        {"source_field": "audience_label", "target_field": None,
         "is_key_column": False, "binding_status": "suggested", "sensitivity": "unknown"},
        {"source_field": "clicks", "target_field": "clicks_total",
         "is_key_column": False, "binding_status": "confirmed", "sensitivity": "unknown"},
        {"source_field": "net_cost", "target_field": "net_media_cost_micros",
         "is_key_column": False, "binding_status": "confirmed", "sensitivity": "unknown"},
    ]
    # NOT ONE IDENTITY REACHES THE WIRE. An `mdm_<ULID>` in the canonical pill is
    # an absence wearing a 30-character disguise, and the reader cannot tell it
    # apart from a field name.
    assert not any(
        str(column["target_field"]).startswith("mdm_") for column in payload["columns"]
    )


@_skip_without_dsn
def test_a_version_beats_the_flat_table_for_the_same_stream(a_stream) -> None:
    """The two stores hold disjoint populations -- and if one flux ever held both,
    the versioned store is the one the `Mapping` tab shows, so it is the one the
    `Data` tab must agree with."""
    from core.datastream_daily_breakdown_api import COLUMNS_FROM_MAPPING_VERSION

    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastream_mappings"
            " (datastream_id, source_field, target_field, is_key_column)"
            " VALUES (%s, 'spend', 'cost', FALSE)",
            (a_stream["ds_id"],),
        )
    _mapping_version(a_stream, {
        "grain": [],
        "fields": [{"field_id": "clicks", "binding": {"status": "confirmed"}}],
    })

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["columns_source"] == COLUMNS_FROM_MAPPING_VERSION
    assert [column["source_field"] for column in payload["columns"]] == ["clicks"]


@_skip_without_dsn
def test_an_identity_no_canonical_row_resolves_is_unmapped_on_the_wire(
    a_stream,
) -> None:
    """The state that could never be displayed, displayed.

    Every unresolved binding used to fall through to its own opaque string, so
    `Unmapped` was unreachable on the versioned store: 0 of 708. An absence that
    cannot be shown is an absence nobody repairs.
    """
    _mapping_version(a_stream, {
        "grain": [],
        "fields": [{"field_id": "net_cost",
                    "binding": {"status": "suggested",
                                "canonical_target": "mdm_" + "Z" * 26}}],
    })

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["columns"] == [
        {"source_field": "net_cost", "target_field": None, "is_key_column": False,
         "binding_status": "suggested", "sensitivity": "unknown"}
    ]


# ---------------------------------------------------------------------------
# Empty.
# ---------------------------------------------------------------------------


@_skip_without_dsn
def test_a_stream_that_never_collected_answers_no_days_and_says_why(a_stream) -> None:
    from core.datastream_daily_breakdown_api import REASON_NO_RUN_IN_WINDOW

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["days"] == []
    assert payload["reason"] == REASON_NO_RUN_IN_WINDOW


@_skip_without_dsn
def test_a_gap_inside_a_collected_stream_keeps_its_strip(a_stream) -> None:
    """The gap IS the information: a stream that has collected keeps its days."""
    from core import pull_job_states
    from core.datastream_daily_breakdown_api import REASON_NO_RUN_IN_WINDOW

    _pull(a_stream, date_from="2026-05-01", date_to="2026-05-01",
          state=pull_job_states.DONE, verdict="ok", actual_rows=5, expected_rows=5)

    with _no_mart():
        payload = _get(a_stream).json()

    assert len(payload["days"]) == 3
    assert payload["reason"] == REASON_NO_RUN_IN_WINDOW
    assert all(
        day["extract_status"] == pull_job_states.LEDGER_NEVER_FETCHED
        for day in payload["days"]
    )
    assert all(day["row_count"] is None for day in payload["days"])


# ---------------------------------------------------------------------------
# What it refuses, against the real guard.
# ---------------------------------------------------------------------------


@_skip_without_dsn
def test_a_stream_of_another_project_is_a_non_disclosing_404(a_stream) -> None:
    """AI-219: the guard proves the PAIR, and its refusal is the absence envelope."""
    other_org, other_project = _id("org_"), _id("proj_")
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (other_org, other_org, other_org, AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status)"
            " VALUES (%s, %s, %s, 'owner', 'active')",
            (_id("mem_"), other_org, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (other_project, other_project, other_project, AUTHOR, other_org),
        )

    with _no_mart():
        response = _get(a_stream, project_id=other_project)

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    # Nothing in the body confirms the stream exists in another project.
    assert a_stream["project_id"] not in response.text
    assert set(body) == {"code", "message"}


@_skip_without_dsn
def test_an_unbounded_window_is_refused_and_names_the_parameter(a_stream) -> None:
    response = _get(a_stream, params={"end": "2026-06-16"})
    assert response.status_code == 400
    assert response.json()["code"] == "missing_param"
    assert response.json()["parameter"] == "start"


@_skip_without_dsn
def test_a_window_wider_than_the_bound_is_refused_and_says_the_bound(a_stream) -> None:
    from core.datastream_daily_breakdown_api import MAX_WINDOW_DAYS

    response = _get(a_stream, params={"start": "2025-01-01", "end": "2026-12-31"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "window_too_wide"
    assert body["bounded_at"] == MAX_WINDOW_DAYS


@_skip_without_dsn
def test_a_stage_with_no_materialisation_is_refused_in_the_warehouse_words(
    a_stream,
) -> None:
    """`published` is the stage no flux can ever be served, and it says so.

    CHANGED BY 58.3: `mapped` used to be refused here for the same reason, which
    was a statement about the WAREHOUSE. It is now a statement about this flux --
    see the two tests below -- while `published` remains a Postgres pointer over
    the same mart rows, for every Datastream, so its sentence is still the
    warehouse's own.
    """
    from core.cache_warehouse import _sample_stage_note

    with _no_mart():
        response = _get(a_stream, params={**WINDOW, "view_mode": "published"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "stage_not_materialized"
    assert body["message"] == _sample_stage_note("published")
    assert body["available_view_modes"] == ["processed"]


# ---------------------------------------------------------------------------
# The pair of relations, and the availability of every mode -- story 58.3.
# ---------------------------------------------------------------------------


@_skip_without_dsn
def test_a_flux_with_no_report_profile_offers_the_two_readings_with_their_reason(
    a_stream,
) -> None:
    """The state of 773 of the 842 live Datastreams, walked on a real row.

    The greyed control and the forced call have to carry ONE wording, and this is
    where that is proved end to end: the `200` publishes the reason, and the `422`
    below quotes the same string.
    """
    from core.stage_relation_resolver import REPORT_PROFILE_NOT_SET, message_for

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["stage_relations"]["report_profile_id"] is None
    assert payload["stage_relations"]["reason"] == REPORT_PROFILE_NOT_SET
    available = {entry["mode"]: entry for entry in payload["view_mode"]["available"]}
    assert available["collected"]["available"] is False
    assert available["mapped"]["available"] is False
    assert available["collected"]["reason"] == message_for(REPORT_PROFILE_NOT_SET)


@_skip_without_dsn
def test_a_forced_reading_this_flux_cannot_serve_is_422_in_the_very_same_words(
    a_stream,
) -> None:
    from core.stage_relation_resolver import REPORT_PROFILE_NOT_SET, message_for

    with _no_mart():
        response = _get(a_stream, params={**WINDOW, "view_mode": "collected"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "stage_not_materialized"
    assert body["message"] == message_for(REPORT_PROFILE_NOT_SET)
    assert [entry["mode"] for entry in body["available"]] == [
        "collected", "mapped", "processed", "published"
    ]


@_skip_without_dsn
def test_a_flux_with_a_profile_resolves_the_pair_from_its_own_manifest(
    a_stream,
) -> None:
    """`report_profile_id` stops being decorative here: it becomes the address.

    The two names come from `server/modules/meta-ads/manifest.json` and from
    nowhere else -- no rule composes them, which is why the manifest declares them.
    """
    from core.collected_mapped_reader import RELATION_ABSENT, message_for

    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET report_profile_id = 'campaign_daily'"
            " WHERE id = %s",
            (a_stream["ds_id"],),
        )

    with _no_mart():
        payload = _get(a_stream).json()

    assert payload["stage_relations"] == {
        "report_profile_id": "campaign_daily",
        "collected_relation": "raw_meta_ads_daily",
        "mapped_relation": "stg_meta_ads_daily",
        "reason": None,
        "message": None,
    }
    # AND THE ADDRESS IS NOT THE AVAILABILITY. This cluster's warehouse holds
    # neither relation, so both modes come back refused WITH THE READER'S REASON
    # -- before any click, which is the whole of arbitrage 2. A verdict read off
    # the manifest alone would have answered `true` here and refused afterwards.
    available = {entry["mode"]: entry for entry in payload["view_mode"]["available"]}
    assert available["collected"]["relation"] == "raw_meta_ads_daily"
    assert available["collected"]["available"] is False
    assert available["collected"]["reason"] == message_for(RELATION_ABSENT)
    assert available["mapped"]["available"] is False
    assert available["processed"]["available"] is True
    assert available["published"]["available"] is False


@_skip_without_dsn
def test_a_reading_is_only_taken_when_a_day_is_asked_for(a_stream) -> None:
    """Nothing on this route reads warehouse ROWS uninvited."""
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET report_profile_id = 'campaign_daily'"
            " WHERE id = %s",
            (a_stream["ds_id"],),
        )

    with _no_mart():
        without = _get(a_stream).json()
        with_day = _get(a_stream, params={**WINDOW, "day": DAY}).json()

    assert without["reading"] is None
    assert with_day["reading"]["day"] == DAY
    # The warehouse of this cluster holds no such relation, and that is an absence
    # WITH ITS NAME -- never an empty table drawn as a day with no rows.
    assert with_day["reading"]["collected"]["relation"] == "raw_meta_ads_daily"
    assert with_day["reading"]["collected"]["rows"] is None
    assert with_day["reading"]["collected"]["reason"] == "relation_absent"


@_skip_without_dsn
def test_the_reading_says_which_of_its_columns_is_an_amount_and_it_says_none(
    a_stream,
) -> None:
    """Story 58.7, against the estate rather than against a fixture's opinion.

    Measured on this cluster: ``select value_type, count(*) from
    app.semantic_concept_versions group by 1`` -> ``date 1, decimal 3, integer 6,
    string 3`` over 13 published versions, and ZERO ``money``. So the honest answer
    on every reading of this product is an EMPTY designation with the name of what
    would fill it -- and the key is still there, on both sides, because a missing
    key and an empty one are the two different sentences the screen says.
    """
    from core.money_provenance_columns import NO_MONETARY_CONCEPT_DECLARED

    # `currency_fx` FIRST, and it is not decoration (2026-08-31). While the
    # capability is off the reading answers `currency_fx_not_active` for every
    # side -- deliberately, so nobody is sent to publish a Concept for a
    # projection the Project never opened. The sentence this walk is about only
    # exists on the far side of that switch.
    _activate_currency_fx(a_stream)
    _no_global_money_concept(a_stream)
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET report_profile_id = 'campaign_daily'"
            " WHERE id = %s",
            (a_stream["ds_id"],),
        )

    with _no_mart():
        payload = _get(a_stream, params={**WINDOW, "day": DAY}).json()

    for zone in ("collected", "mapped"):
        designation = payload["reading"][zone]["money_provenance"]
        assert designation["columns"] == []
        assert designation["reason"] == NO_MONETARY_CONCEPT_DECLARED
        assert "app.semantic_concept_versions" in designation["message"]
        assert designation["rows"] == []
        # Arbitrage 8: no reporting currency is named, and the door that confirms
        # one is. 0 project of 18 has confirmed one, so printing `EUR` would be a
        # column DEFAULT dressed as a decision.
        assert designation["reporting_currency"] is None
        assert designation["reporting_currency_gate"]
        assert "EUR" not in str(designation)


@_skip_without_dsn
def test_a_published_money_concept_is_what_the_route_really_reads(a_stream) -> None:
    """The one thing a mocked cursor cannot prove: that this SQL runs on this DDL.

    A Concept version is published with ``value_type = 'money'`` inside the
    fixture's own transaction, and the reading stops answering « nothing declares
    an amount ». The warehouse of this cluster holds neither relation, so what it
    then answers is `relation_not_read` -- which is the point: an amount IS
    declared and the relation still could not be opened, and those are two facts
    the payload keeps apart instead of collapsing into one silence.
    """
    from core.money_provenance_columns import (
        NO_MONETARY_CONCEPT_DECLARED,
        RELATION_NOT_READ,
    )

    concept_id, version_id = _ulid("sc_"), _ulid("scv_")
    _activate_currency_fx(a_stream)
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET report_profile_id = 'campaign_daily'"
            " WHERE id = %s",
            (a_stream["ds_id"],),
        )
        # THREE STATEMENTS, because the two tables point at each other: the concept
        # lands without a current version, the version lands, then the concept
        # names it. The same order the Semantic Model's own writer uses.
        cur.execute(
            "INSERT INTO app.semantic_concepts"
            " (id, project_id, kind, name, lifecycle_status, created_by)"
            " VALUES (%s, %s, 'metric', 'cost', 'published', %s)",
            (concept_id, a_stream["project_id"], AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.semantic_concept_versions"
            " (id, concept_id, project_id, version_number, status, kind, name,"
            "  label, value_type, expression, aggregation, additivity_class,"
            "  content_hash, created_by)"
            " VALUES (%s, %s, %s, 1, 'published', 'metric', 'cost', 'Cost',"
            "         'money', '{\"sql\": \"cost\"}', '{\"function\": \"sum\"}',"
            "         'additive', %s, %s)",
            (
                version_id,
                concept_id,
                a_stream["project_id"],
                hashlib.sha256(version_id.encode()).hexdigest(),
                AUTHOR,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (version_id, concept_id),
        )

    with _no_mart():
        payload = _get(a_stream, params={**WINDOW, "day": DAY}).json()

    for zone in ("collected", "mapped"):
        designation = payload["reading"][zone]["money_provenance"]
        assert designation["reason"] != NO_MONETARY_CONCEPT_DECLARED
        assert designation["reason"] == RELATION_NOT_READ
        assert payload["reading"][zone]["reason"] == "relation_absent"


@_skip_without_dsn
def test_a_day_outside_the_window_is_refused_by_name(a_stream) -> None:
    response = _get(a_stream, params={**WINDOW, "day": "2026-07-30"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "day_outside_window"
    assert body["parameter"] == "day"


@_skip_without_dsn
def test_a_reading_of_another_projects_stream_is_a_404_like_everything_else(
    a_stream,
) -> None:
    """The cross-project guard covers the reading too: it is the same address."""
    response = _get(a_stream, project_id=_id("proj_"), params={**WINDOW, "day": DAY})
    assert response.status_code in (403, 404)


@_skip_without_dsn
def test_two_streams_on_one_connector_lose_their_rows_and_keep_their_days(
    a_stream,
) -> None:
    """CHANGED, not preserved: this asserted `409` and cemented the defect.

    `fact_daily_kpi` carries `(project_id, connector)` and no stream id, so the
    mart slice of two twins is unattributable. `app.pull_jobs` carries
    `datastream_id`, so their extract registries never were. Refusing the whole
    payload took the strip away from 34 Datastreams of the disposable cluster for
    a reason that says nothing about them.
    """
    from core import pull_job_states
    from core.datastream_daily_breakdown_api import ROWS_AMBIGUOUS_MATERIALIZATION
    from core.datastream_sample_api import AMBIGUOUS_MATERIALIZATION_MESSAGE  # noqa: PLC0415

    _pull(a_stream, date_from=DAY, date_to=DAY, state=pull_job_states.DONE,
          verdict="ok", actual_rows=1200, expected_rows=1200)
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, connection_ref_id, enabled, created_by, org_id)"
            " VALUES (%s, %s, 'Twin', 'meta-ads', 'connector_pull', %s, TRUE, %s, %s)",
            (_id("ds_"), a_stream["project_id"], a_stream["connection_ref_id"],
             AUTHOR, a_stream["org_id"]),
        )

    with _no_mart():
        response = _get(a_stream)

    assert response.status_code == 200, response.text
    payload = response.json()
    # This stream's own day, read from its own pull job.
    assert [day["date"] for day in payload["days"]] == [
        "2026-06-14", "2026-06-15", "2026-06-16"
    ]
    assert payload["days"][1]["extract_status"] == "ok"
    assert payload["days"][1]["row_count"] == 1200
    # The mart half, refused with the sentence already written for it.
    assert all(
        day["rows_reason"] == ROWS_AMBIGUOUS_MATERIALIZATION
        for day in payload["days"]
    )
    assert payload["rows_note"] == AMBIGUOUS_MATERIALIZATION_MESSAGE


# ---------------------------------------------------------------------------
# Re-collecting one day, and the run it leaves on that day's row -- story 58.4.
# ---------------------------------------------------------------------------


class _NoCommit:
    """The fixture's connection with `commit` and `rollback` disarmed.

    THE WRITE PATH COMMITS AND THIS FIXTURE MUST NOT. `_open_refetch_run` commits
    the execution it mints, and every other test in this file relies on the
    transaction being rolled back whole at teardown. Without this proxy one run
    of this file would leave an execution, a pull job and an audit row in the
    disposable cluster, and the append-only tables would then be asked to forgive
    a delete. Everything the route writes still goes through real SQL -- only the
    boundary is neutralised.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        return None

    def rollback(self):
        return None


def _running_execution(ids, state):
    """A run in flight on this stream, in the shape 76 real rows have.

    IT DOES NOT SET `current_plan_version_id`, and that is the whole point.
    Measured on the disposable cluster 2026-08-06: 842 Datastreams, 76 hold an
    execution in an active state, and **all 76** name no current plan version.
    Setting one here would build a row this product does not have -- and would
    make the re-collection answer `422 dispatch_not_available`, because
    `datastreams._row_to_dict` reads `versioned` off exactly that column.
    """
    mapping_id = _mapping_version(ids, {"fields": []}, current=False)
    execution_id = f"dse_{uuid.uuid4().hex[:12].upper()}{'0' * 14}"
    with ids["conn"].cursor() as cur:
        cur.execute(
            "SELECT plan_version_id FROM app.datastream_mapping_versions WHERE id = %s",
            (mapping_id,),
        )
        plan_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO app.datastream_executions (id, datastream_id, project_id,"
            " plan_version_id, mapping_version_id, state, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (execution_id, ids["ds_id"], ids["project_id"], plan_id, mapping_id,
             state, AUTHOR),
        )
    return execution_id


def _refetch(ids, dates, *, project_id=None):
    """POST the re-collection through the real application and the real queue.

    THE ONE COLLABORATOR THAT IS STOOD DOWN, and why. `queue._topology_scope_refusal`
    is an authorization seam that opens its OWN connection through
    `core.db.request_connection` -- a connection this fixture cannot lend it,
    because its rows live in an uncommitted transaction. Left alone it raises,
    and uncertainty at an authorization seam is a denial by design, so every
    window came back `refused` and the walk measured that refusal instead of the
    re-collection. It has its own tests (`tests/core/test_queue*`); here it is
    asked to proceed so the subject of this file stays the subject.
    """
    from core.main import build_asgi_app

    connection = _NoCommit(ids["conn"])

    @contextmanager
    def _open(*_a, **_k):
        yield connection

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        patch("core.db.get_connection", side_effect=_open),
        patch("core.queue._topology_scope_refusal", return_value=None),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        return client.post(
            f"/api/projects/{project_id or ids['project_id']}"
            f"/datastreams/{ids['ds_id']}/refetch",
            json={"dates": dates},
        )


def test_the_re_collection_is_mounted_where_the_console_calls_it() -> None:
    """One address, and it is project-scoped like the rest of this surface.

    The un-scoped `/api/datastreams/{id}/refetch` is GONE (story 58.4, arbitrage
    4): two doors onto one gesture is what this lot refuses everywhere else, and
    the route had a single console caller so nothing forced the second one.
    """
    from core import admin_api, datastream_collection_api

    paths = {getattr(route, "path", "") for route in admin_api.router.routes}
    assert datastream_collection_api.REFETCH_ROUTE_PATH in paths
    assert "/api/datastreams/{id}/refetch" not in paths, (
        "the un-scoped re-collection address is still mounted beside the new one"
    )


def _inside_the_backfill_ceiling() -> str:
    """A day the trial ceiling does not move, and it has to be relative to today.

    `queue.enqueue_pull` raises `date_from` to `today - max_backfill_days` for a
    trial org (story 34.3), and the fixture's org is one. Measured 2026-08-06 on
    a day 52 days old: the enqueued row came back `date_from=2026-07-07,
    date_to=2026-06-15` -- a window whose start is AFTER its end. The clamp is
    computed from `date.today()`, so a fixed constant here would pass now and
    start clamping silently later.
    """
    from datetime import date, timedelta

    return (date.today() - timedelta(days=2)).isoformat()


@_skip_without_dsn
def test_a_day_older_than_the_trial_ceiling_is_refused_with_its_reason(
    a_stream,
) -> None:
    """CHANGED, NOT PRESERVED -- and it is the test that cemented the defect.

    Written earlier the same day, this case asserted that an old day comes back
    `202` with a `backfill_clamp` on its job: it froze "the window was moved" as
    the correct behaviour. It was not. `trial_enforcement` raised `date_from` to
    the ceiling without looking at `date_to`, so the row that landed in
    `app.pull_jobs` was `date_from=2026-07-07, date_to=2026-06-15` -- a start
    after its end, which nothing downstream refused and which no worker can
    honour. A test asserting the clamp was signalled made that row look inspected.

    What is true now: the ceiling reports an EMPTY window (no day both asked for
    and entitled), `enqueue_pull` refuses it by name, no job row is written, and
    the answer carries a sentence the person can act on -- their recovery window
    does not reach that day.

    AND IT PUBLISHES FIRST (2026-08-31). The arming gate refuses a `draft`
    Datastream before the ceiling is ever consulted, so without `_publish` this
    walk answered `422 not_armed` and proved nothing about the ceiling.
    """
    _publish(a_stream)

    response = _refetch(a_stream, [DAY])

    assert response.status_code == 202, response.text
    entry = response.json()["jobs"][0]
    assert entry["code"] == "window_before_backfill_ceiling"
    assert "reaches back to" in entry["message"]
    assert entry["message"].endswith("so no day of it can be collected")
    # Nothing was queued, and nothing was written.
    assert "job_id" not in entry
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "SELECT date_from, date_to FROM app.pull_jobs WHERE datastream_id = %s",
            (a_stream["ds_id"],),
        )
        rows = cur.fetchall()
    assert rows == [], f"a window was written anyway: {rows}"


@_skip_without_dsn
def test_no_pull_job_of_this_stream_can_start_after_it_ends(a_stream) -> None:
    """The invariant on the ROW, not on the helper -- and it is the whole point.

    `tests/core/test_trial_enforcement.py` proves the arithmetic; this proves
    that nothing between the console and the table re-opens it. Every date this
    walk asks for, old and recent, and not one row where `date_from > date_to`.
    """
    for day in (DAY, "2026-01-01", _inside_the_backfill_ceiling()):
        _refetch(a_stream, [day])

    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "SELECT date_from, date_to FROM app.pull_jobs"
            " WHERE datastream_id = %s AND date_from > date_to",
            (a_stream["ds_id"],),
        )
        inverted = cur.fetchall()
    assert inverted == [], f"windows whose start is after their end: {inverted}"


@_skip_without_dsn
def test_a_re_collected_day_lands_on_the_grid_as_the_window_it_created(
    a_stream,
) -> None:
    """The `202` and the grid agree on real rows, INCLUDING about the run.

    Arbitrage 6 says the row reloads the window after the `202` instead of
    writing the id the answer carried, and this walk holds the two to each other.

    THE PREVIOUS VERSION OF THIS DOCSTRING IS RETIRED (2026-08-31), because both
    facts it rested on are gone. It said a re-collection could never own a run:
    `_open_refetch_run` needs an active plan AND mapping version, and a stream
    naming an active plan was refused `422 dispatch_not_available` before the
    queue -- two conditions that excluded each other. That refusal was retired
    (`datastream_collection_api._run_datastream`, "The refusal is retired"), and
    the arming gate now makes publication a PRECONDITION of collecting anything:
    a `draft` stream is refused `not_armed`. So the only walkable path is the one
    below -- publish, then re-collect -- and on it the run is minted, carried in
    the answer, and found again on the day's row. What used to be excluded is now
    the only shape there is.
    """
    from core import pull_job_states

    published = _publish(a_stream)
    day = _inside_the_backfill_ceiling()

    response = _refetch(a_stream, [day])

    assert response.status_code == 202, response.text
    answer = response.json()
    assert len(answer["jobs"]) == 1
    assert (answer["jobs"][0]["date_from"], answer["jobs"][0]["date_to"]) == (day, day)
    assert answer["jobs"][0]["state"] == pull_job_states.QUEUED
    assert "backfill_clamp" not in answer["jobs"][0]
    # A RUN OF ITS OWN, and never the publication's: the id the answer carries is
    # freshly minted, so a console reading it is not shown the run that published.
    run_id = answer["execution_id"]
    assert run_id and run_id != published["execution_id"]

    with _no_mart():
        payload = _get(a_stream, params={"start": day, "end": day}).json()

    row = next(entry for entry in payload["days"] if entry["date"] == day)
    # The WINDOW is on the row -- the day is now visibly being collected.
    assert row["job_state"] == pull_job_states.QUEUED
    assert row["extract_count"] == 1
    # And arbitrage 6: the row was RELOADED, so the run it shows is the run the
    # queue really wrote, never the id the answer happened to carry.
    assert row["execution_id"] == run_id


@_skip_without_dsn
def test_a_held_datastream_is_collected_anyway_and_the_answer_names_the_run(
    a_stream,
) -> None:
    """Jean, 2026-08-06: the console SAYS it, and lets go. No `409` is added.

    `uq_datastream_executions_active` already holds one non-terminal execution
    per Datastream, so the re-collection gets no run of its own. The windows are
    still enqueued -- the pull is the point -- and the answer names the run that
    holds the lock, so the missing progress line has a reason a person can read
    instead of a silence.
    """
    from core import execution_states, pull_job_states
    from core.datastream_active_run import ACTIVE_RUN_MESSAGE

    # Published first, for the same reason as the walk above: the arming gate
    # refuses a `draft` stream before the lock is ever consulted, so this used to
    # measure `not_armed` and never reached the subject.
    _publish(a_stream)
    state = execution_states.ACTIVE_STATES[1]
    held = _running_execution(a_stream, state)

    # A day INSIDE the backfill ceiling: an older one is refused by the ceiling
    # before the lock is ever consulted, and this walk is about the lock.
    response = _refetch(a_stream, [_inside_the_backfill_ceiling()])

    assert response.status_code == 202, response.text
    answer = response.json()
    assert len(answer["jobs"]) == 1, "the pull was refused instead of being queued"
    assert answer["jobs"][0]["state"] == pull_job_states.QUEUED
    assert "execution_id" not in answer
    assert answer["active_run"]["execution_id"] == held
    assert answer["active_run"]["state"] == state
    assert answer["active_run"]["message"] == ACTIVE_RUN_MESSAGE


@_skip_without_dsn
def test_a_terminal_run_holds_nothing_and_the_answer_claims_nothing(a_stream) -> None:
    """A finished run is not a held Datastream, and the two must not read alike.

    `collected` is terminal since story 63.1 and holds no lock. If this module
    kept its own list of "active" states instead of reading
    `core.execution_states`, a terminal run would be reported as a collection in
    flight -- and the console would tell a person to wait for a run that ended.
    """
    from core import execution_states

    terminal = next(
        state.name for state in execution_states.EXECUTION_STATES
        if state.stored and state.terminal and state.success
    )
    _running_execution(a_stream, terminal)

    answer = _refetch(a_stream, [_inside_the_backfill_ceiling()]).json()

    assert "active_run" not in answer


@_skip_without_dsn
def test_a_re_collection_of_another_projects_stream_is_the_same_404(a_stream) -> None:
    response = _refetch(a_stream, [_inside_the_backfill_ceiling()], project_id=_id("proj_"))
    assert response.status_code in (403, 404)
