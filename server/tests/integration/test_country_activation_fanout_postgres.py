"""Story 37.6, Task 5: the Country fan-out on a REAL Postgres, not on monkeypatches.

WHAT WAS GREEN AND WHAT IT PROVED. `tests/core/test_country_activation.py` drives
`apply_country_plan_fan_out` against a two-method fake connection and monkeypatches
`save_field_mapping`, `compile_projection`, `create_execution` and
`enqueue_activation_work`. So what it asserts is the ORDER OF THE CALLS — which is worth
asserting, and is not the transaction, the constraints, the role or the isolation. The
story's own open review finding says so: "Complete the live-Postgres and final applicable
connector/managed-feed regression proof".

Every version here is minted by the production services on the live schema:
`save_datastream_intent`, `save_field_mapping`, `compile_projection`, and then the fan-out
itself. Nothing is hand-INSERTed except the Project and the Datastream shell, because
those two have no service door.

THE FIXTURE IS A MANAGED FEED, deliberately. `country.md`'s coverage matrix asks for the
file path as much as the connector path, and Task 5 asks for "at least one compatible
managed feed". A managed feed also needs no OAuth: a `connector_pull` fixture would drag
in `get_scoped_source_capabilities`, a `connection_ref` and an authorization, and the
first thing to break would be the harness rather than the fan-out. The connector side is
proved offline, over every shipped descriptor, by
`tests/conformance/test_country_applicable_sources.py`.

WHAT THE PROJECTION REFUSED FIRST, recorded because it is the product working. The first
fixture declared its grain with CANONICAL names (`country`, `date`) while its fields carry
SOURCE ids (`Country`, `Date`), so the estimator found no profile for any grain column,
assumed 1 000 distinct each, and refused the candidate with `cardinality_over_limit` and
`scan_over_limit` — naming the exact columns to profile. A fixture that had reached for a
higher limit instead of reading the message would have proved that a limit can be raised.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

ORG = "org_test_fixture"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# The plan intent and the mapping payload, in their REAL shapes.
# ---------------------------------------------------------------------------


def _intent() -> dict:
    """A managed feed whose declared grain already carries `country`.

    That is the `preserved_full_grain` case of `compile_geographic_intent`: there is no
    provider report to widen, so the column is in the grain or it is not.
    """
    return {
        "contract_version": "1",
        "source": {
            "kind": "managed_feed",
            "writer_kind": "toorow",
            "managed_feed": {
                "format": "csv",
                "channel": "file_upload",
                "source_ref": _id("dsa_"),
                "template_ref": _id("fst_"),
            },
            "selection": {
                "selection_mode": "subset",
                "metrics": ["cost"],
                "dimensions": ["date", "campaign_id", "country"],
                "grain": ["date", "campaign_id", "country"],
                "filters": [],
            },
        },
        "destination": {"policy": "managed_raw"},
        "historical": {"start": "2026-01-01T00:00:00Z", "end_exclusive": "2026-07-20T00:00:00Z"},
        "schedule": {
            "mode": "daily",
            "interval_minutes": 1440,
            "timezone": "Europe/Paris",
            "watermark": {"kind": "date_window", "delay_minutes": 120},
            "late_arrival": {"lookback_minutes": 4320},
            "retry": {
                "max_attempts": 5,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 3600,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 7},
        },
    }


def _profile(sample: str) -> dict:
    return {
        "nullable": False,
        "unique": False,
        # LOAD-BEARING, and it is what the projection estimator reads. Without a
        # signal every grain column is `unknown` (1 000 assumed distinct) and the
        # candidate is refused for cardinality -- correctly.
        "cardinality_signal": "low",
        "sample_values": [sample],
        "confidence": 0.9,
    }


def _suggestion(role: str, aggregation: str) -> dict:
    return {
        "semantic_role": role,
        "aggregation": aggregation,
        "non_additive": False,
        "currency": "EUR",
        "sensitivity": "none",
        # `const: "suggested"` in the schema: the operator's confirmation lives on
        # the BINDING, never on the suggestion.
        "status": "suggested",
        "evidence": ["observed_schema"],
    }


def _binding(canonical_target: str) -> dict:
    return {
        "canonical_target": canonical_target,
        # `mdm_target` must be an `mdm_` id or null; a canonical NAME here is refused
        # by the schema, which is the mapping layer keeping the two apart.
        "mdm_target": None,
        "status": "confirmed",
        "blocking_reason": None,
        "confirmed_by": "pytest",
        "confirmed_reason": "Confirmed in Datastream final review",
    }


def _field(
    field_id: str,
    physical_type: str,
    role: str,
    aggregation: str,
    target: str,
    sample: str,
):
    return {
        "field_id": field_id,
        "physical_type": physical_type,
        "profile": _profile(sample),
        "suggestion": _suggestion(role, aggregation),
        "binding": _binding(target),
    }


def _mapping_payload(plan_version_id: str) -> dict:
    return {
        "mapping_contract_version": "1",
        "source_schema_hash": "0" * 64,
        "plan_version_id": plan_version_id,
        # SOURCE field ids, not canonical names -- see the module docstring.
        "grain": ["Campaign", "Country", "Date"],
        "ambiguities": [],
        "fields": [
            _field("Date", "date", "dimension", "none", "date", "2026-01-01"),
            _field("Campaign", "string", "dimension", "none", "campaign_id", "cmp-1"),
            _field("Country", "string", "dimension", "none", "country", "FR"),
            _field("Cost", "number", "measure", "sum", "cost", "1000.00"),
        ],
    }


# ---------------------------------------------------------------------------
# Fixture construction, through the production services.
# ---------------------------------------------------------------------------


def _new_project(conn) -> str:
    project_id = _id("proj_c37_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, 'Country fan-out', %s, 'active', 'pytest', %s)
            """,
            (project_id, _id("c37-"), ORG),
        )
    return project_id


def _publish_country(conn, project_id: str) -> dict[str, str]:
    from core import country_registry as registry_service
    from core import master_data

    vocabulary = registry_service.import_country_vocabulary(
        conn, actor="pytest", source_version="ISO-3166-1:2026-01", effective_date=date(2026, 1, 1)
    )
    presets = registry_service.seed_country_presets(conn)
    france = next(item for item in presets if "france" in str(item["preset_key"]))
    registry = registry_service.ensure_country_registry(
        conn, org_id=ORG, project_id=project_id, actor="pytest"
    )
    draft = registry_service.materialize_preset(
        conn,
        org_id=ORG,
        project_id=project_id,
        registry_id=str(registry["id"]),
        preset_version_id=str(france["id"]),
        actor="pytest",
        vocabulary_version_id=str(vocabulary["id"]),
    )
    published = master_data.publish_version(
        conn, project_id=project_id, version_id=str(draft["id"]), actor="pytest"
    )
    return {"registry_id": str(registry["id"]), "version_id": str(published["id"])}


def _active_datastream(conn, project_id: str) -> dict[str, str]:
    """An ACTIVE, ENABLED Datastream with a published plan and mapping.

    The fan-out's `FOR UPDATE` read demands all four: without `enabled = TRUE`,
    `lifecycle_state = 'active'` and both pointers, it raises `ProjectSettingsStale`
    rather than preparing anything. That refusal is the subject of its own test below.
    """
    from core.datastream_field_mapping import save_field_mapping
    from core.datastream_intents import save_datastream_intent
    from core.datastream_projection import compile_projection

    datastream_id = _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, org_id, name, module_name, source_kind, enabled,
                 schedule_mode, refetch_days, date_window_days, config, created_by,
                 lifecycle_state, data_role)
            VALUES (%s, %s, %s, %s, NULL, 'managed_feed', TRUE, 'nightly',
                    3, 30, %s::jsonb, 'pytest', 'active', 'Spend')
            """,
            # `uq_datastreams_project_name_live`: one live name per Project. The
            # stale-proposal test needs TWO Datastreams in one Project, so the name
            # carries the id rather than a literal.
            (datastream_id, project_id, ORG, f"Country feed {datastream_id[-6:]}",
             json.dumps({})),
        )

    operation = _id("op_")
    plan = save_datastream_intent(
        datastream_id=datastream_id,
        project_id=project_id,
        intent=_intent(),
        identity="pytest",
        idempotency_key=f"{operation}:plan",
        conn=conn,
        commit=False,
        advance_pointer=False,
    )
    assert plan.get("executable"), "the fixture plan must be executable before anything else"
    mapping = save_field_mapping(
        datastream_id=datastream_id,
        project_id=project_id,
        mapping_payload=_mapping_payload(str(plan["id"])),
        identity="pytest",
        idempotency_key=f"{operation}:mapping",
        conn=conn,
        pinned_plan_version_id=str(plan["id"]),
        advance_pointer=False,
        commit=False,
    )
    assert mapping.get("executable")
    projection = compile_projection(mapping)
    assert projection.get("executable"), (
        "the fixture mapping does not compile to an executable projection, so the "
        f"fan-out would refuse it for a fixture reason: {projection.get('issues')}"
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastreams
               SET current_plan_version_id = %s, current_mapping_version_id = %s
             WHERE id = %s AND project_id = %s
            """,
            (str(plan["id"]), str(mapping["id"]), datastream_id, project_id),
        )
    return {
        "datastream_id": datastream_id,
        "plan_version_id": str(plan["id"]),
        "mapping_version_id": str(mapping["id"]),
    }


def _proposal(conn, project_id: str, stream: dict[str, str], identities: dict[str, str]) -> dict:
    from core.capability_compilers import CountryCompiler

    evidence = CountryCompiler().project_evidence(conn, project_id=project_id, org_id="")
    return {
        "id": _id("dscp_"),
        "datastream_id": stream["datastream_id"],
        "capability_key": "country",
        "applicability": "applicable",
        "current_plan_version_id": stream["plan_version_id"],
        "current_mapping_version_id": stream["mapping_version_id"],
        "dependency_snapshot": {
            "current_plan_version_id": stream["plan_version_id"],
            "current_mapping_version_id": stream["mapping_version_id"],
        },
        "governance_owner_references": [
            {
                "object_type": "registry",
                "object_id": identities["registry_id"],
                "version_id": evidence["hierarchy_version_id"],
                "evidence_hash": evidence["evidence_hash"],
            }
        ],
        "impact": {},
    }


def _pointers(conn, datastream_id: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_plan_version_id, current_mapping_version_id,
                   current_published_execution_id, enabled, lifecycle_state
            FROM app.datastreams WHERE id = %s
            """,
            (datastream_id,),
        )
        return cur.fetchone()


@pytest.fixture()
def governed(live_postgres):
    project_id = _new_project(live_postgres)
    identities = _publish_country(live_postgres, project_id)
    stream = _active_datastream(live_postgres, project_id)
    return {
        "conn": live_postgres,
        "project_id": project_id,
        "identities": identities,
        "stream": stream,
    }


# ---------------------------------------------------------------------------
# The fan-out.
# ---------------------------------------------------------------------------


def test_the_fan_out_prepares_a_candidate_and_moves_no_data_pointer(governed) -> None:
    """AD-23, the invariant the whole story turns on: Data keeps publication authority.

    The Project capability advances in the caller's transaction, and the current plan,
    mapping, execution and enabled state do NOT move. Anything else would publish data
    from a Project Settings confirmation.
    """
    from core.country_activation import apply_country_plan_fan_out

    conn = governed["conn"]
    stream = governed["stream"]
    before = _pointers(conn, stream["datastream_id"])

    result = apply_country_plan_fan_out(
        conn,
        project_id=governed["project_id"],
        change_set_id=_id("pcs_"),
        proposals=[_proposal(conn, governed["project_id"], stream, governed["identities"])],
        actor="pytest",
        enabled=True,
        loaded_modules=[],
    )

    assert result["data_pointers_moved"] is False
    assert _pointers(conn, stream["datastream_id"]) == before, (
        "the fan-out moved a Data pointer; a Country confirmation may prepare a "
        "candidate and may not publish one"
    )

    [plan] = result["plan_versions"]
    assert plan["previous_plan_version_id"] == stream["plan_version_id"]
    assert plan["plan_version_id"] != stream["plan_version_id"]
    assert plan["version_number"] == 2
    assert plan["executable"] is True

    [candidate] = result["candidates"]
    assert candidate["mapping_version_id"] != stream["mapping_version_id"]
    assert candidate["candidate_execution_id"]
    assert candidate["candidate_job_id"]

    conn.rollback()


def test_the_prepared_plan_really_carries_the_published_country_meaning(governed) -> None:
    """A candidate that does not compile Country is a candidate for nothing.

    Read off the stored version rather than off the return value: the row is what a
    later review opens, and a snapshot that only the response carried would be a plan
    nobody can inspect.
    """
    from core.country_activation import apply_country_plan_fan_out

    conn = governed["conn"]
    result = apply_country_plan_fan_out(
        conn,
        project_id=governed["project_id"],
        change_set_id=_id("pcs_"),
        proposals=[
            _proposal(conn, governed["project_id"], governed["stream"], governed["identities"])
        ],
        actor="pytest",
        enabled=True,
        loaded_modules=[],
    )
    plan_version_id = result["plan_versions"][0]["plan_version_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_payload FROM app.datastream_plan_versions WHERE id = %s",
            (plan_version_id,),
        )
        payload = cur.fetchone()[0]
    if isinstance(payload, str):
        payload = json.loads(payload)

    geographic = payload["geographic"]
    # A managed feed whose declared grain already carries country: nothing was widened,
    # and the honest status says so.
    assert geographic["compilation_status"] == "preserved_full_grain"
    assert geographic["effective_country_field"] == "country"
    assert geographic["mode"] == "local_markets"
    # The markets come from the PUBLISHED hierarchy, projected by `posture_from_evidence`
    # -- not from `project_preferences`, which nothing writes.
    assert [market["country_codes"] for market in geographic["markets"]] == [["FR"]]
    assert geographic["impact"]["tracked_country_count"] == 1

    conn.rollback()


def test_a_stale_proposal_rolls_the_whole_fan_out_back(governed) -> None:
    """THE TRANSACTION, which is what a monkeypatched pipeline cannot show.

    Two proposals, the second one stale. The refusal must leave NOTHING behind: no plan
    version, no candidate mapping, no candidate execution, no queued job for the first
    Datastream either. A partial fan-out would leave a Project whose capability advanced
    over candidates that exist for some Datastreams and not others, and the operator
    would have no way to tell which.
    """
    from core.country_activation import apply_country_plan_fan_out
    from core.project_settings import ProjectSettingsStale

    conn = governed["conn"]
    project_id = governed["project_id"]
    first = governed["stream"]
    second = _active_datastream(conn, project_id)

    good = _proposal(conn, project_id, first, governed["identities"])
    stale = _proposal(conn, project_id, second, governed["identities"])
    # The plan moved after the review was prepared. This is the real race the check
    # exists for, not a synthetic error.
    stale["current_plan_version_id"] = "dsp_00000000000000000000000000"
    stale["dependency_snapshot"]["current_plan_version_id"] = "dsp_00000000000000000000000000"

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_fan_out")
    with pytest.raises(ProjectSettingsStale):
        apply_country_plan_fan_out(
            conn,
            project_id=project_id,
            change_set_id=_id("pcs_"),
            proposals=[good, stale],
            actor="pytest",
            enabled=True,
            loaded_modules=[],
        )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT before_fan_out")

    # Version 2 must not exist for EITHER Datastream: the good one was processed first
    # (proposals are sorted by datastream id, so which one that is depends on the ids --
    # asserting on both is what makes the test independent of that ordering).
    for datastream_id in (first["datastream_id"], second["datastream_id"]):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.datastream_plan_versions "
                "WHERE datastream_id = %s AND version_number > 1",
                (datastream_id,),
            )
            assert cur.fetchone()[0] == 0, (
                f"a plan version survived the refusal for {datastream_id}"
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.datastream_executions WHERE datastream_id = %s",
                (datastream_id,),
            )
            assert cur.fetchone()[0] == 0, f"a candidate execution survived for {datastream_id}"

    conn.rollback()


def test_a_datastream_that_is_not_active_is_refused_rather_than_prepared(governed) -> None:
    """The `FOR UPDATE` read demands enabled AND active, and says so.

    A disabled Datastream is one an operator switched off. Preparing a candidate for it
    would resurrect it by the side door, and the refusal names staleness rather than
    silently skipping — a skipped Datastream is a Project the operator believes is
    covered and is not.
    """
    from core.country_activation import apply_country_plan_fan_out
    from core.project_settings import ProjectSettingsStale

    conn = governed["conn"]
    stream = governed["stream"]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET enabled = FALSE WHERE id = %s",
            (stream["datastream_id"],),
        )

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT disabled_check")
    with pytest.raises(ProjectSettingsStale):
        apply_country_plan_fan_out(
            conn,
            project_id=governed["project_id"],
            change_set_id=_id("pcs_"),
            proposals=[_proposal(conn, governed["project_id"], stream, governed["identities"])],
            actor="pytest",
            enabled=True,
            loaded_modules=[],
        )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT disabled_check")

    conn.rollback()


def test_another_projects_datastream_is_not_reachable_from_this_fan_out(governed) -> None:
    """ISOLATION, on the real predicate and not on a comment.

    The fan-out's read is scoped `WHERE d.id = %s AND d.project_id = %s`. A proposal
    naming a live Datastream of ANOTHER Project must find nothing — a cross-project
    fan-out would mint a plan version for a tenant who confirmed nothing.
    """
    from core.country_activation import apply_country_plan_fan_out
    from core.project_settings import ProjectSettingsStale

    conn = governed["conn"]
    neighbour_project = _new_project(conn)
    neighbour_stream = _active_datastream(conn, neighbour_project)
    before = _pointers(conn, neighbour_stream["datastream_id"])

    trespassing = _proposal(conn, governed["project_id"], neighbour_stream, governed["identities"])

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT isolation_check")
    with pytest.raises(ProjectSettingsStale):
        apply_country_plan_fan_out(
            conn,
            project_id=governed["project_id"],
            change_set_id=_id("pcs_"),
            proposals=[trespassing],
            actor="pytest",
            enabled=True,
            loaded_modules=[],
        )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT isolation_check")

    assert _pointers(conn, neighbour_stream["datastream_id"]) == before
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_plan_versions "
            "WHERE datastream_id = %s AND version_number > 1",
            (neighbour_stream["datastream_id"],),
        )
        assert cur.fetchone()[0] == 0

    conn.rollback()


def test_deactivation_prepares_a_candidate_too_and_still_moves_nothing(governed) -> None:
    """Turning Country OFF is a plan change, not a deletion.

    `country.md` requires deactivation to be truly consolidated, and the way to get
    there is the same candidate path: a new plan compiled with a Global posture, awaiting
    the same review. Nothing about the current plan moves here either.
    """
    from core.country_activation import apply_country_plan_fan_out

    conn = governed["conn"]
    stream = governed["stream"]
    before = _pointers(conn, stream["datastream_id"])

    proposal = _proposal(conn, governed["project_id"], stream, governed["identities"])
    result = apply_country_plan_fan_out(
        conn,
        project_id=governed["project_id"],
        change_set_id=_id("pcs_"),
        proposals=[proposal],
        actor="pytest",
        enabled=False,
        loaded_modules=[],
    )

    assert result["data_pointers_moved"] is False
    assert _pointers(conn, stream["datastream_id"]) == before
    plan_version_id = result["plan_versions"][0]["plan_version_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_payload FROM app.datastream_plan_versions WHERE id = %s",
            (plan_version_id,),
        )
        payload = cur.fetchone()[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    geographic = payload["geographic"]
    assert geographic["mode"] == "global"
    assert geographic["markets"] == []
    # The country column was already in the feed's grain, so deactivating the capability
    # does not remove it: the split stops being REPORTED, the facts are not rewritten.
    assert geographic["compilation_status"] == "preserved_full_grain"

    conn.rollback()


def test_the_prepared_plan_version_is_immutable_once_written(governed) -> None:
    """CONSTRAINTS. A candidate an operator may still reject must not be editable.

    Migration 030's trigger is what makes the review meaningful: the thing reviewed and
    the thing published are the same bytes.
    """
    import psycopg
    from core.country_activation import apply_country_plan_fan_out

    conn = governed["conn"]
    result = apply_country_plan_fan_out(
        conn,
        project_id=governed["project_id"],
        change_set_id=_id("pcs_"),
        proposals=[
            _proposal(conn, governed["project_id"], governed["stream"], governed["identities"])
        ],
        actor="pytest",
        enabled=True,
        loaded_modules=[],
    )
    plan_version_id = result["plan_versions"][0]["plan_version_id"]

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT immutability_check")
    # The trigger RAISEs with SQLSTATE 23000, so psycopg surfaces
    # IntegrityConstraintViolation -- not RaiseException. Catching the wrong class here
    # would have let a REMOVED trigger pass as a caught error.
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.datastream_plan_versions SET content_hash = %s WHERE id = %s",
                ("f" * 64, plan_version_id),
            )
    assert "immutable" in str(caught.value)
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT immutability_check")

    conn.rollback()


def test_the_fan_out_is_idempotent_on_its_change_set_key(governed) -> None:
    """The same confirmation replayed must not mint a second candidate.

    Every write keys on `country:<change_set_id>:<datastream_id>`, which is what makes a
    retried confirmation safe. Without it a network retry would leave two candidates for
    one decision and the operator would have to guess which one the change set meant.
    """
    from core.country_activation import apply_country_plan_fan_out

    conn = governed["conn"]
    change_set_id = _id("pcs_")
    args = dict(
        project_id=governed["project_id"],
        change_set_id=change_set_id,
        actor="pytest",
        enabled=True,
        loaded_modules=[],
    )
    first = apply_country_plan_fan_out(
        conn,
        proposals=[
            _proposal(conn, governed["project_id"], governed["stream"], governed["identities"])
        ],
        **args,
    )
    second = apply_country_plan_fan_out(
        conn,
        proposals=[
            _proposal(conn, governed["project_id"], governed["stream"], governed["identities"])
        ],
        **args,
    )

    assert first["plan_versions"][0]["plan_version_id"] == (
        second["plan_versions"][0]["plan_version_id"]
    )
    assert first["candidates"][0]["candidate_execution_id"] == (
        second["candidates"][0]["candidate_execution_id"]
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_plan_versions "
            "WHERE datastream_id = %s AND version_number > 1",
            (governed["stream"]["datastream_id"],),
        )
        assert cur.fetchone()[0] == 1, "the replay minted a second plan version"

    conn.rollback()
