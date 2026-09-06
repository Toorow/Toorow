"""toorow -- A carrier Datastream's file becomes a dated version of ITS plan.

Chantier 67-25, against the amendment ratified 2026-08-24
(`docs/product-architecture/file-source-ingestion.md`, "a project carries one or
several media plans, each on its own carrier Datastream").

WHAT WAS MISSING AND WHY IT MATTERED. `import_runner` has routed a plan-store
Template since the one-engine fusion, and it refuses an import that brings no
`plan_id` (`plan_target_missing`). Nothing in the product could SUPPLY one: no
row said which plan a Datastream carried, so the converged path was reachable
only by a caller that knew a plan id by other means -- which is to say, by no
console surface at all. The carrier link (migration 303) is that sentence, and
this file proves the ingress reads it.

THE TWO HALVES ARE ONE FUNCTION, ON PURPOSE. `ingest_inbound_file` is where an
uploaded file and an e-mailed file meet; resolving the plan anywhere else would
leave one of the two ingresses refusing a plan the other accepts.

AND THE PUBLICATION CYCLE IS THE PLAN'S. A governed upload carries a
`raw_import_id`, which sets `publish_candidate` -- and `_resolve_landing_target`
refuses a plan-store import that publishes, because a plan version is published
by the plan's own explicit act. Without the exemption proven below, every plan
file uploaded from the Workbench would have met a dead end.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


#: A Template that lands in the plan store -- the literal shape
#: `file_source_template.create_file_source_template` seals, reduced to the two
#: keys the router reads. `grain: "line"` is not decoration: a plan-store target
#: REQUIRES it (`file_source_template.py`), and a fixture that omitted it would
#: prove a routing decision the product would never reach.
_PLAN_STORE_TEMPLATE = {
    "template": {
        "contract": {
            "landing_target": "plan_store",
            "grain": "line",
            "class": "planned",
        },
        "content_hash": "sha256:EXAMPLE",
    },
    "template_id": "fst_EXAMPLE",
}

_WAREHOUSE_TEMPLATE = {
    "template": {
        "contract": {"landing_target": "warehouse_relation", "grain": "daily"},
        "content_hash": "sha256:EXAMPLE",
    },
    "template_id": "fst_EXAMPLE_2",
}


def test_the_landing_is_read_from_the_template_and_from_nothing_else():
    """`_lands_in_plan_store` answers the Template's own declaration.

    THE SAME KEY `import_runner` ROUTES ON. Two readings of "where does this file
    land" is the pair of answers free to diverge that this chantier exists to
    end, so this asserts the shapes rather than a behaviour: a producer with no
    template, a warehouse one, and a plan-store one.
    """
    from core.csv_excel_import import FileSourceProducer
    from core.inbound_ingest import _lands_in_plan_store

    plan_producer = FileSourceProducer(
        _PLAN_STORE_TEMPLATE["template"], {}, _PLAN_STORE_TEMPLATE["template_id"]
    )
    warehouse_producer = FileSourceProducer(
        _WAREHOUSE_TEMPLATE["template"], {}, _WAREHOUSE_TEMPLATE["template_id"]
    )

    assert _lands_in_plan_store(plan_producer) is True
    assert _lands_in_plan_store(warehouse_producer) is False
    # A Datastream with no file-source Template at all is not a carrier, and the
    # question must not raise on it: every ordinary CSV import runs through here.
    assert _lands_in_plan_store(None) is False


def test_a_plan_store_import_that_publishes_is_refused_which_is_why_the_ingress_exempts_it():
    """The rule the ingress obeys, stated by the router itself.

    A governed upload sets `publish_candidate`. `_resolve_landing_target` refuses
    that for a plan-store landing, so the ingress must turn it off for this
    landing and only for it -- proven here rather than asserted in a comment,
    because a future change to either side would otherwise silently close the
    Workbench's import door again.
    """
    from core.csv_excel_import import CsvExcelImportError, FileSourceProducer
    from core.import_runner import _resolve_landing_target

    producer = FileSourceProducer(
        _PLAN_STORE_TEMPLATE["template"], {}, _PLAN_STORE_TEMPLATE["template_id"]
    )

    with pytest.raises(CsvExcelImportError) as publishing:
        _resolve_landing_target(
            producer,
            source_metadata={"plan_id": "plan_EXAMPLE"},
            publish_candidate=True,
        )
    assert publishing.value.code == "plan_store_publication_is_governed_by_the_plan"

    with pytest.raises(CsvExcelImportError) as unplanned:
        _resolve_landing_target(producer, source_metadata={}, publish_candidate=False)
    assert unplanned.value.code == "plan_target_missing"

    target, plan_id = _resolve_landing_target(
        producer,
        source_metadata={"plan_id": "plan_EXAMPLE"},
        publish_candidate=False,
    )
    assert (target, plan_id) == ("plan_store", "plan_EXAMPLE")


def _seed(conn) -> tuple[str, str]:
    """A project and a managed-feed Datastream that accepts an e-mailed file."""
    project_id = f"proj-carrier-{uuid.uuid4().hex[:8]}"
    datastream_id = f"ds-carrier-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, %s, %s, 'active', 'system', 'org_test_fixture')
            """,
            (project_id, "Carrier Test", project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, source_kind, enabled, config, created_by, org_id)
            VALUES (%s, %s, %s, 'managed_feed', TRUE,
                    '{"channels": ["email"]}'::jsonb, 'tester', 'org_test_fixture')
            """,
            (datastream_id, project_id, f"Plan file {uuid.uuid4().hex[:6]}"),
        )
    conn.commit()
    return project_id, datastream_id


def _bundle() -> dict:
    return {
        "datastream_id": None,  # filled by the caller
        "project_id": None,
        "plan_version_id": "dplan_EXAMPLE",
        "mapping_version_id": "dmap_EXAMPLE",
        "projection_plan": {},
        "mapping_payload": {},
        "governed_evidence": {},
        "template": _PLAN_STORE_TEMPLATE,
    }


@pg_available
def test_the_ingress_stamps_the_plan_this_datastream_carries(monkeypatch):
    """The file lands in the plan the CARRIER holds, and publishes nothing."""
    import core.csv_excel_import as csv_excel_import
    from core.inbound_ingest import ingest_inbound_file
    from core.mediaplan_store import create_plan

    conn = _connect()
    project_id, datastream_id = _seed(conn)
    try:
        plan = create_plan(
            conn,
            project_id=project_id,
            name="Q1 Brand",
            created_by="tester",
            carrier_datastream_id=datastream_id,
        )
        conn.commit()

        seen: dict = {}

        def _fake_run_import(_file_bytes, **kwargs):
            seen.update(kwargs)
            return {"outcome": "written_pending_publication"}

        monkeypatch.setattr(csv_excel_import, "run_import", _fake_run_import)

        bundle = _bundle()
        bundle["datastream_id"] = datastream_id
        bundle["project_id"] = project_id

        ingest_inbound_file(
            conn,
            datastream_id=datastream_id,
            project_id=project_id,
            file_bytes=b"plan",
            filename="february.xlsx",
            channel="email",
            message_id="msg-EXAMPLE",
            actor="inbound-worker",
            dispatch_bundle_override=bundle,
        )

        # THE PLAN IS THE CARRIER'S, resolved by the ingress and never sent by a
        # caller: `file_import_api` refuses a client-supplied `source_metadata`.
        assert seen["source_metadata"]["plan_id"] == plan["id"]
        # AND THE PUBLICATION STAYS THE PLAN'S OWN ACT.
        assert seen["publish_candidate"] is False
    finally:
        purge_fixture_project(conn, project_id)
        conn.commit()
        conn.close()


@pg_available
def test_a_carrier_with_no_plan_refuses_and_names_the_gesture_instead_of_provisioning(
    monkeypatch,
):
    """« importing a plan provisions a Datastream the person never asked for » --
    the second `Incomplete if`, refuted in the other direction too.

    NOTHING IS CREATED, not even the plan: a file arriving at a carrier that
    holds no plan is refused, and the refusal names the gesture -- create the
    plan in this Datastream's Workbench -- rather than minting one under a name
    nobody chose.
    """
    import core.csv_excel_import as csv_excel_import
    from core.inbound_ingest import DatastreamNotIngestable, ingest_inbound_file

    conn = _connect()
    project_id, datastream_id = _seed(conn)
    try:
        called: list[str] = []
        monkeypatch.setattr(
            csv_excel_import,
            "run_import",
            lambda *a, **k: called.append("ran") or {},
        )

        bundle = _bundle()
        bundle["datastream_id"] = datastream_id
        bundle["project_id"] = project_id

        with pytest.raises(DatastreamNotIngestable) as refused:
            ingest_inbound_file(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
                file_bytes=b"plan",
                filename="february.xlsx",
                channel="email",
                message_id="msg-EXAMPLE",
                actor="inbound-worker",
                dispatch_bundle_override=bundle,
            )
        message = str(refused.value)
        assert "carries no plan yet" in message
        # THE GESTURE, NOT THE CAUSE. A refusal naming `plan_target_missing`
        # would be the plumbing term CLAUDE.md forbids on a message meant to
        # tell somebody what to do.
        assert "Workbench" in message
        assert called == []

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.media_plans WHERE project_id = %s",
                (project_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.rollback()
        purge_fixture_project(conn, project_id)
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# The Workbench evidence -- what the console draws the two gestures from.
# ---------------------------------------------------------------------------


def _producer(template: dict):
    from core.csv_excel_import import FileSourceProducer

    return FileSourceProducer(template["template"], {}, template["template_id"])


def test_the_data_tab_says_nothing_about_plans_on_a_datastream_that_carries_none(
    monkeypatch,
):
    """`media_plan` is `None`, and the console then draws no panel at all.

    An empty block saying "this is not a media plan" would be a sentence nobody
    asked for on every file source of the product. The verdict is the SERVER'S
    and it comes from the Template's own declaration -- never from the mode,
    which most file sources share while landing warehouse rows.
    """
    from core import datastream_workbench, file_source_resolution

    monkeypatch.setattr(
        file_source_resolution,
        "resolve_file_source_producer",
        lambda *a, **k: _producer(_WAREHOUSE_TEMPLATE),
    )

    assert (
        datastream_workbench._media_plan_evidence(
            None, "proj_EXAMPLE", "ds_EXAMPLE", {"source_kind": "managed_feed"}
        )
        is None
    )
    # A Datastream that pulls from an API is not asked the question at all.
    assert (
        datastream_workbench._media_plan_evidence(
            None, "proj_EXAMPLE", "ds_EXAMPLE", {"source_kind": "connector_pull"}
        )
        is None
    )


def test_a_carrier_with_no_plan_yet_says_why_and_the_gesture_is_on_this_tab(monkeypatch):
    from core import datastream_workbench, file_source_resolution, mediaplan_store

    monkeypatch.setattr(
        file_source_resolution,
        "resolve_file_source_producer",
        lambda *a, **k: _producer(_PLAN_STORE_TEMPLATE),
    )
    monkeypatch.setattr(mediaplan_store, "get_carrier_plan", lambda *a, **k: None)

    evidence = datastream_workbench._media_plan_evidence(
        None, "proj_EXAMPLE", "ds_EXAMPLE", {"source_kind": "managed_feed"}
    )
    assert evidence["carrier"] is True
    assert evidence["plan"] is None
    # THE EMPTINESS SAYS WHY, in the words of the person: what the file becomes,
    # not a table name and not a deployment state.
    assert "carries no media plan yet" in evidence["empty_message"]
    assert "plan lines" in evidence["empty_reason"]
    assert "dated version" in evidence["empty_reason"]
