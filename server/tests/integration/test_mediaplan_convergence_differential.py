"""toorow -- the DIFFERENTIAL of the mediaplan convergence (chantier 67-25b).

`file-source-ingestion.md` ratified ONE ingestion engine, with a media plan as a
template PROFILE of it. This file carries the proof the convergence owes: a
representative workbook must land the SAME plan through the one engine as the
pre-convergence path landed -- the same lines, the same line keys, the same
amounts, the same candidate version lifecycle.

That equality is the whole argument. Everything else about the fusion is a
design claim; this is the only thing that can falsify it, because a plan is not
a pile of rows -- it is a versioned object every plan reader
(`mediaplan_store`, `mediaplan_mapping`, `datastream_workbench_placements`,
`plan_actual_alignment`) reads by `line_key`. If the paths disagreed by one cent
or one key, the convergence would silently rewrite what those readers see.

THE SECOND PATH IS GONE, AND THE EXPECTATION IS NOW FROZEN (2026-08-24).
Until today this file RAN both paths and compared them. `core.mediaplan_import`
was retired with the three addresses that were its only door -- see the
amendment « the second xlsx engine is retired » -- so there is no second walk
left to execute.

What survives is what the comparison was FOR. The retired path's output on this
exact workbook is recorded below as `_RETIRED_PATH_LANDED`, cell for cell, as it
was measured on 2026-08-18 when both paths still ran. The engine is asserted
against that record. This is a weaker instrument than two live paths and the
docstring says so rather than pretending otherwise: a frozen expectation cannot
notice a defect the retired path ALSO had. It remains the strongest available
one, because it still compares landed plan CONTENT -- keys, labels, channels,
spans, amounts -- and not counts, which is what this surface's `Incomplete if`
demands. Loosening it to counts, or deleting it, reopens that criterion.

DO NOT "update" the frozen list to match a new engine result. It is a record of
what a retired path produced, not a snapshot of current behaviour: rewriting it
to make a red run green destroys the only evidence that the convergence kept the
plan intact. A genuine intended change to plan content is a change to the
Template contract, and it is argued in the document first.

The engine path here is NOT a shortcut into the plan store: it goes through
`run_import()`, so the ledger, the rejection gate and the money invariant all run
exactly as they do for a warehouse landing. Only the LANDING TARGET differs, and
the template declares it.
"""

from __future__ import annotations

import io
import os
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="platform Postgres not reachable",
)

openpyxl = pytest.importorskip("openpyxl")

_EXECUTABLE_PLAN = {"executable": True, "grain": ["date"]}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


# ---------------------------------------------------------------------------
# THE workbook -- the exact bytes the retired path was measured on.
# ---------------------------------------------------------------------------


def _workbook() -> bytes:
    """A representative plan sheet -- the shapes a real workbook actually has.

    Three things are here on purpose, because each is a way the two paths could
    have disagreed when both still ran -- and therefore each is a way the engine
    could now drift away from the frozen record:

      * plain lines, to fix labels, channels, spans and amounts;
      * a MERGED budget cell over three sub-rows (E4:E6), which must collapse
        into ONE line spanning the whole group -- min start to max end -- with the
        money landing exactly once. This is the case that made the engine land
        3600 for 1200 before d2b94e16, and the case the two walks are most likely
        to resolve differently;
      * a TOTAL row whose budget is READABLE but whose dates are absent, so the
        rejected money is visible on both sides and melts on neither.

    Four lines against one rejected row is also what a plan sheet really looks
    like. It matters: the rejection gate reads rejected/(accepted+rejected)
    against a 25% default, and on a three-row toy sheet a single structural TOTAL
    row alone breaches it.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Digital"
    ws.append(["Libelle", "Support", "Debut", "Fin", "Budget"])
    ws.append(["Meta prospecting", "Meta", "2026-03-01", "2026-03-31", 3000])
    ws.append(["Google Search", "Google", "2026-03-01", "2026-03-31", 2000])
    ws.append(["Display FR", "Display", "2026-03-01", "2026-03-15", 1200])
    ws.append(["Display DE", "Display", "2026-03-05", "2026-03-20", None])
    ws.append(["Display IT", "Display", "2026-03-03", "2026-03-25", None])
    ws.append(["LinkedIn B2B", "LinkedIn", "2026-03-01", "2026-03-31", 800])
    ws.append(["TOTAL", None, None, None, 7000])
    ws.merge_cells("E4:E6")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


#: THE FROZEN RECORD -- what the retired `mediaplan_import` walk landed for the
#: workbook above, measured 2026-08-18 while both paths still ran, in the shape
#: `_comparable` produces. Read the module docstring before touching it.
#:
#: The per-sheet contract it was produced from, kept so the record can be read
#: against the agreement that produced it:
#:     {"header_row": 1, "line_key": "auto",
#:      "columns": {"Libelle": "label", "Support": "channel",
#:                  "Debut": "start_date", "Fin": "end_date",
#:                  "Budget": "budget"}}
_RETIRED_PATH_LANDED = [
    # The merged group: ONE line, 1200 once, spanning min..max of the three
    # sub-rows, labelled and keyed the way the plan store expects.
    (
        "digital/display-fr-groupe-de-3", "Display FR (groupe de 3)",
        "Display", "2026-03-01", "2026-03-25", "1200.00", False,
    ),
    (
        "digital/google-search", "Google Search", "Google",
        "2026-03-01", "2026-03-31", "2000.00", False,
    ),
    (
        "digital/linkedin-b2b", "LinkedIn B2B", "LinkedIn",
        "2026-03-01", "2026-03-31", "800.00", False,
    ),
    (
        "digital/meta-prospecting", "Meta prospecting", "Meta",
        "2026-03-01", "2026-03-31", "3000.00", False,
    ),
]

#: And the money it accounted for, the same run. The TOTAL row's 7000 is
#: READABLE and rejected for its dates, so it is visible rather than melted; the
#: merged 1200 is counted ONCE in the file total.
_RETIRED_PATH_SUMS = {"file": "14000.00", "imported": "7000.00", "rejected": "7000.00"}


def _engine_contract() -> dict:
    """The SAME agreement, expressed as a file-source Template contract.

    Every plan-specific treatment is a declared capability of the template:
    the line grain, the composed row identity, the landing target. Nothing here
    is a mediaplan branch inside the engine.
    """
    return {
        "kind": "catalog",
        "required_fields": ["budget", "end_date", "label", "start_date"],
        "optional_fields": ["channel"],
        "grain": "line",
        "class": "planned",
        "landing_target": "plan_store",
        "placement": {"metric": "budget", "period": "start_date"},
        "sheet_name": "Digital",
        "header_row": 1,
        "reshape": {
            "fields": {
                "label": "Libelle",
                "channel": "Support",
                "start_date": "Debut",
                "end_date": "Fin",
                "budget": "Budget",
            },
            "amount_field": "budget",
            "start_field": "start_date",
            "end_field": "end_date",
            "label_field": "label",
            "sheet_name": "Digital",
            "header_row": 1,
            "grain": "line",
            "line_key": "auto",
            # The pre-convergence path has no subtotal-marker rule: it rejects the
            # TOTAL row for its missing dates. Declaring no markers keeps the two
            # walks reaching the same verdict for the same reason.
            "total_markers": [],
        },
    }


def _producer(conn, *, project_id: str, org_id: str = "org_test_fixture"):
    """A producer over a REAL template row, created through the production path.

    Not a hand-built dict: the ledger refuses an `import_contract_id` whose row
    does not exist (migration 213), and that refusal is right -- a governed import
    must name a contract someone can go and read. Creating it here also exercises
    the plan-line vocabulary check, which is the other half of declaring a
    plan-store landing.
    """
    from core.file_source_resolution import FileSourceProducer
    from core.file_source_template import create_file_source_template

    template = create_file_source_template(
        conn,
        project_id=project_id,
        org_id=org_id,
        template_code=f"PLAN_{uuid.uuid4().hex[:8].upper()}",
        contract=_engine_contract(),
        created_by="tester",
    )
    conn.commit()
    return FileSourceProducer(
        {"contract": template["contract"], "content_hash": template["content_hash"]},
        mapping={},
        template_id=template["id"],
        # A catalog Template is platform-governed and immutable per (code,
        # version): its own record IS the confirmation evidence.
        catalog_governed=True,
    )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_scope(conn) -> dict:
    """A project + datastream + plan/mapping versions the ledger FKs require."""
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_version_id, mapping_id = _id("dsp_"), _id("dmap_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, %s, %s, 'active', 'test', 'org_test_fixture')
            """,
            (project_id, project_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled,
                 created_by, org_id)
            VALUES (%s, %s, 'Plan feed', NULL, 'managed_feed', FALSE, 'test',
                    'org_test_fixture')
            """,
            (ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'managed_feed', 'toorow', 'managed_raw',
                    '{}'::jsonb, repeat('a', 64), repeat('b', 64), 'test')
            """,
            (plan_version_id, ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number,
                 mapping_contract_version, source_schema_hash, plan_version_id,
                 content_hash, ossie_spec_version, toorow_extension_version,
                 executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64),
                    '0.1.1', '1', TRUE, '{}'::jsonb, '{}'::jsonb,
                    repeat('d', 64), 'test')
            """,
            (mapping_id, ds_id, project_id, plan_version_id),
        )
    conn.commit()
    return {
        "project_id": project_id,
        "datastream_id": ds_id,
        "plan_version_id": plan_version_id,
        "mapping_version_id": mapping_id,
    }


def _comparable(lines: list[dict]) -> list[tuple]:
    """The plan content that must be identical, in a stable order."""
    return sorted(
        (
            line["line_key"],
            line["label"],
            line["channel"],
            str(line["start_date"]),
            str(line["end_date"]),
            str(line["budget"]),
            bool(line["is_plan_only"]),
        )
        for line in lines
    )


def _drop(conn, project_id: str) -> None:
    # THE WHOLE TREE, THROUGH THE PRODUCTION ERASURE PATH -- never a hand-written
    # DELETE list, which loses the race to the next governed table.
    from tests.conftest import purge_fixture_project  # noqa: PLC0415

    purge_fixture_project(conn, project_id)
    conn.commit()


# ---------------------------------------------------------------------------
# THE DIFFERENTIAL
# ---------------------------------------------------------------------------


def test_the_engine_lands_exactly_the_plan_the_retired_path_landed():
    """The non-regression proof of the whole fusion.

    One workbook, one ingestion path, and a frozen record of what the retired one
    produced from the same bytes. What lands must be indistinguishable from that
    record: same keys, same labels, same spans, same amounts, and a CANDIDATE
    version (an import is never destructive).
    """
    from core.import_runner import run_import
    from core.mediaplan_store import create_plan, list_lines

    conn = _connect()
    scope = _seed_scope(conn)
    data = _workbook()
    try:
        engine_plan = create_plan(
            conn, project_id=scope["project_id"], name="Engine", created_by="tester"
        )
        result = run_import(
            data,
            datastream_id=scope["datastream_id"],
            project_id=scope["project_id"],
            plan_version_id=scope["plan_version_id"],
            mapping_version_id=scope["mapping_version_id"],
            projection_plan=_EXECUTABLE_PLAN,
            actor="tester",
            idempotency_key=_id("imp_"),
            source_metadata={
                "filename": "plan.xlsx",
                # The template declares the KIND of target; the run supplies its
                # coordinates. A template is reusable across plans.
                "plan_id": engine_plan["id"],
            },
            contract={},
            conn=conn,
            producer=_producer(conn, project_id=scope["project_id"]),
        )
        conn.commit()

        assert result["blocked"] is False
        assert result["landing"]["backend"] == "plan_store"

        engine_version = result["landing"]["plan_version"]

        # 1. A CANDIDATE: an import never publishes, and never did on either path.
        assert engine_version["status"] == "candidate"
        assert engine_version["is_active"] is False

        # 2. THE CONTENT IS THE RECORDED ONE, line for line. Comparing the whole
        #    list rather than a count is the point: a failure names WHICH line
        #    moved, and by how much.
        engine_lines = list_lines(conn, version_id=engine_version["id"])
        assert _comparable(engine_lines) == _RETIRED_PATH_LANDED

        # 3. The money adds up the way it added up then. `sums` was the retired
        #    report's shape; the engine states the same three facts through the
        #    run result and the ledger, so they are checked in that form.
        assert result["rejected_count"] == 1
        assert result["row_count"] == 4
        assert result["landed_row_count"] == 4
        # The four landed lines are the retired path's `imported` total, to the
        # cent -- derived from the record, never restated as a literal, so the
        # two halves of this file cannot drift apart.
        landed = sum(Decimal(line[5]) for line in _RETIRED_PATH_LANDED)
        assert landed == Decimal(_RETIRED_PATH_SUMS["imported"])
        assert (
            Decimal(_RETIRED_PATH_SUMS["file"])
            == landed + Decimal(_RETIRED_PATH_SUMS["rejected"])
        ), "the frozen record must itself satisfy file == imported + rejected"
    finally:
        _drop(conn, scope["project_id"])
        conn.close()


def test_the_plan_landing_is_recorded_on_the_one_ledger():
    """A plan import is a governed import, not a side door into Postgres.

    The doc's `Incomplete if` says a mediaplan must not reach
    `app.media_plan_lines` by a path that is not `run_import()`. The ledger row is
    what makes that checkable after the fact.
    """
    from core.import_runner import run_import
    from core.mediaplan_store import create_plan

    conn = _connect()
    scope = _seed_scope(conn)
    try:
        plan = create_plan(
            conn, project_id=scope["project_id"], name="Ledger", created_by="tester"
        )
        result = run_import(
            _workbook(),
            datastream_id=scope["datastream_id"],
            project_id=scope["project_id"],
            plan_version_id=scope["plan_version_id"],
            mapping_version_id=scope["mapping_version_id"],
            projection_plan=_EXECUTABLE_PLAN,
            actor="tester",
            idempotency_key=_id("imp_"),
            source_metadata={"filename": "plan.xlsx", "plan_id": plan["id"]},
            contract={},
            conn=conn,
            producer=_producer(conn, project_id=scope["project_id"]),
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT landing_relation, outcome, row_count, rejected_row_count "
                "FROM app.managed_feed_import_ledger WHERE id = %s",
                (result["ledger"]["id"],),
            )
            landing_relation, outcome, row_count, rejected = cur.fetchone()

        # The landing names the exact plan version, so the ledger's evidence is as
        # specific for a plan as `schema.table` is for a warehouse relation.
        assert landing_relation == (
            f"app.media_plan_versions:{result['landing']['plan_version']['id']}"
        )
        assert outcome == "written"
        assert row_count == 4
        assert rejected == 1
    finally:
        _drop(conn, scope["project_id"])
        conn.close()


def test_a_plan_import_refuses_the_warehouse_publication_cycle():
    """ONE publication lifecycle per act.

    A plan version is published by the plan store's own explicit gesture. Driving
    the warehouse pointer-swap state machine over it would be a second cycle for
    the same publication -- and the pointer it swaps does not exist for a plan.
    """
    from core.import_runner import run_import
    from core.mediaplan_store import create_plan
    from core.tabular_types import CsvExcelImportError

    conn = _connect()
    scope = _seed_scope(conn)
    try:
        plan = create_plan(
            conn, project_id=scope["project_id"], name="NoPub", created_by="tester"
        )
        with pytest.raises(CsvExcelImportError) as exc:
            run_import(
                _workbook(),
                datastream_id=scope["datastream_id"],
                project_id=scope["project_id"],
                plan_version_id=scope["plan_version_id"],
                mapping_version_id=scope["mapping_version_id"],
                projection_plan=_EXECUTABLE_PLAN,
                actor="tester",
                idempotency_key=_id("imp_"),
                source_metadata={"filename": "plan.xlsx", "plan_id": plan["id"]},
                contract={},
                conn=conn,
                producer=_producer(conn, project_id=scope["project_id"]),
                publish_candidate=True,
            )
        assert exc.value.code == "plan_store_publication_is_governed_by_the_plan"
        conn.rollback()
    finally:
        _drop(conn, scope["project_id"])
        conn.close()


def test_a_plan_store_template_run_without_a_plan_refuses_before_the_ledger():
    """No plan, no landing -- and no ledger row minted for an import that cannot land."""
    from core.import_runner import run_import
    from core.tabular_types import CsvExcelImportError

    conn = _connect()
    scope = _seed_scope(conn)
    try:
        with pytest.raises(CsvExcelImportError) as exc:
            run_import(
                _workbook(),
                datastream_id=scope["datastream_id"],
                project_id=scope["project_id"],
                plan_version_id=scope["plan_version_id"],
                mapping_version_id=scope["mapping_version_id"],
                projection_plan=_EXECUTABLE_PLAN,
                actor="tester",
                idempotency_key=_id("imp_"),
                source_metadata={"filename": "plan.xlsx"},
                contract={},
                conn=conn,
                producer=_producer(conn, project_id=scope["project_id"]),
            )
        assert exc.value.code == "plan_target_missing"
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.managed_feed_import_ledger "
                "WHERE project_id = %s",
                (scope["project_id"],),
            )
            assert cur.fetchone()[0] == 0
    finally:
        _drop(conn, scope["project_id"])
        conn.close()
