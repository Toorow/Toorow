"""AI-294 against real rows: publish -> a named Result -> the ONE Share mechanism.

The design (`proactive-assertions.md`, 2026-08-17) says publishing an insight
produces a Result by putting the card behind a governed Query Spec. This file
proves it on a live Postgres and a real DuckDB warehouse, end to end and with
nothing mocked but the clock of the world:

  1. the publication derives a Query Spec from the card contract, versions it
     through the EXISTING spec store, executes it through the EXISTING governed
     executor, and the item names `query_spec_version_id` + `result_id`;
  2. the Result is one the unique Share mechanism accepts: a Render is frozen
     over it and `render_shares.create_share` opens a share of that Render;
  3. a project whose Semantic View does not cover the card still publishes,
     with `result_unavailable_reason` naming the missing link and ZERO rows in
     `app.query_results` -- the honest refusal, not a broken control.

Needs migration 281 applied (lineage columns on app.daily_insights).

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import json

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

from core import daily_insights as store  # noqa: E402
from core import daily_insights_tools as dit  # noqa: E402
from core.daily_insight_result import produce_insight_result  # noqa: E402
from core.daily_insights_schema import evidence_universe  # noqa: E402
from core.semantic_compiler import COMPILER_VERSION  # noqa: E402
from ulid import ULID  # noqa: E402

# The pepper and the origin, for THIS module's tests only -- a module-level
# `os.environ.setdefault` wrote them for the whole session (AI-377).
from tests.support.render_share_env import render_share_env  # noqa: E402,F401

_HASH = "a" * 64
_RELATION = "main_marts.insight_daily"


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class _NoCommit:
    """`record_run` commits; the test transaction must not. Everything else passes.

    The same proxy idea as this suite's StatementCounter: behaviour is real, only
    the transaction boundary is held by the fixture so `live_postgres` can roll
    everything back on teardown.
    """

    def __init__(self, inner):
        self._inner = inner

    def commit(self):
        return None

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    """A real DuckDB relation behind the governed read.

    The date lands as TEXT: the single-source runner compares the window bounds
    as bound parameters, and ISO strings order correctly either way.
    """
    path = tmp_path / "ai294.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.insight_daily (date VARCHAR, conversions INTEGER)")
    con.execute(
        "INSERT INTO main_marts.insight_daily VALUES "
        "('2026-07-20', 12), ('2026-07-21', 19), ('2026-06-01', 999)"
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return str(path)


def _seed_governed_world(conn) -> dict:
    """Org -> project -> concepts -> published view + matrix -> bound Datastream.

    Every link `resolve_physical_plan` walks is real: removing one is how the
    refusal test below earns its name.
    """
    ids = {
        "org": _uid("org"),
        "project": _uid("proj"),
        "conv": f"sc_{ULID()}",
        "conv_v": f"scv_{ULID()}",
        "date": f"sc_{ULID()}",
        "date_v": f"scv_{ULID()}",
        "view": f"sv_{ULID()}",
        "view_v": f"svv_{ULID()}",
        "ds": _uid("ds"),
        "plan": _uid("dpv"),
        "mapping": _uid("dmv"),
    }
    matrix = {
        "metrics": [{"concept_id": ids["conv"], "version_id": ids["conv_v"]}],
        "dimensions": [
            {"concept_id": ids["date"], "version_id": ids["date_v"], "name": "reporting_date"}
        ],
        "cells": [
            {
                "metric_id": ids["conv"],
                "dimension_id": ids["date"],
                "queryable": True,
                "join_path": [],
            }
        ],
    }
    mapping_payload = {
        "grain": ["reporting_date"],
        "fields": [
            {
                "field_id": "date",
                "physical_type": "string",
                "binding": {"canonical_target": "reporting_date", "status": "confirmed"},
            },
            {
                "field_id": "conversions",
                "physical_type": "number",
                "binding": {"canonical_target": "conversions", "status": "confirmed"},
            },
        ],
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (ids["org"], "AI-294", ids["org"].lower().replace("_", "-"), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (ids["project"], ids["org"], "AI-294", ids["project"].lower().replace("_", "-"),
             "tester"),
        )
        cur.execute(
            """INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by)
               VALUES (%s,%s,'metric','conversions','tester'),
                      (%s,%s,'dimension','reporting_date','tester')""",
            (ids["conv"], ids["project"], ids["date"], ids["project"]),
        )
        cur.execute(
            """INSERT INTO app.semantic_concept_versions
                 (id, concept_id, project_id, version_number, status, kind, name, label,
                  value_type, expression, aggregation, additivity_class, semantic_type,
                  allowed_grains, content_hash, created_by)
               VALUES
                 (%s,%s,%s,1,'published','metric','conversions','Conversions','integer',
                  %s::jsonb,%s::jsonb,'additive',NULL,%s,%s,'tester'),
                 (%s,%s,%s,1,'published','dimension','reporting_date','Date','date',
                  NULL,NULL,NULL,'time',%s,%s,'tester')""",
            (
                ids["conv_v"], ids["conv"], ids["project"],
                json.dumps({"op": "sum", "field": "conversions"}),
                json.dumps({"type": "sum"}), [], _HASH,
                ids["date_v"], ids["date"], ids["project"], ["day"], _HASH,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (ids["conv_v"], ids["conv"]),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (ids["date_v"], ids["date"]),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s,%s,'insight_view','tester')",
            (ids["view"], ids["project"]),
        )
        cur.execute(
            """INSERT INTO app.semantic_view_versions
                 (id, view_id, project_id, version_number, status, name, label,
                  dependency_fingerprint, content_hash, created_by)
               VALUES (%s,%s,%s,1,'published','insight_view','Insight view',%s,%s,'tester')""",
            (ids["view_v"], ids["view"], ids["project"], _HASH, _HASH),
        )
        cur.execute(
            "UPDATE app.semantic_views SET current_version_id = %s WHERE id = %s",
            (ids["view_v"], ids["view"]),
        )
        cur.execute(
            """INSERT INTO app.semantic_compiled_artifacts
                 (id, project_id, view_version_id, compiler_version, content_hash,
                  queryability_matrix, ossie_projection, ossie_spec_version,
                  toorow_extension_version)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,'{}'::jsonb,'1','1')""",
            # The compiler version the derivation checks, never a literal: the
            # fixture said 'test' while the compiler is semantic-compiler.v2, so
            # every derivation refused query_spec_refused and only the refusal
            # branch was ever exercised (found 2026-08-30).
            (
                _uid("sca"), ids["project"], ids["view_v"], COMPILER_VERSION, _HASH,
                json.dumps(matrix),
            ),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, org_id, project_id, name, created_by, "
            "source_kind) VALUES (%s,%s,%s,'Insight source','tester','managed_feed')",
            (ids["ds"], ids["org"], ids["project"]),
        )
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
                 (id, datastream_id, project_id, version_number, contract_version,
                  source_kind, writer_kind, destination_policy, normalized_payload,
                  content_hash, idempotency_key_hash, created_by)
               VALUES (%s,%s,%s,1,'plan.v1','connector_pull','toorow','managed_raw',
                       '{}'::jsonb,%s,%s,'tester')""",
            (ids["plan"], ids["ds"], ids["project"], _HASH, _HASH),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s,%s,%s,1,'mapping.v1',%s,%s,%s,'0.1.1','2',%s::jsonb,'{}'::jsonb,
                       %s,'tester')""",
            (
                ids["mapping"], ids["ds"], ids["project"], _HASH, ids["plan"], _HASH,
                json.dumps(mapping_payload), _HASH,
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (ids["mapping"], ids["ds"]),
        )
        for ordinal, concept in enumerate((ids["conv"], ids["date"])):
            cur.execute(
                """INSERT INTO app.semantic_view_version_bindings
                     (view_version_id, ordinal, concept_id, datastream_id, project_id,
                      mapping_version_id, binding_state)
                   VALUES (%s,%s,%s,%s,%s,%s,'active')""",
                (ids["view_v"], ordinal, concept, ids["ds"], ids["project"], ids["mapping"]),
            )
        execution_id, output_id = _uid("dse"), _uid("dso")
        ids["output_version"] = _uid("dsov")
        cur.execute(
            """INSERT INTO app.datastream_executions
                 (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                  projection_plan_ref, state, created_by)
               VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'published','tester')""",
            (execution_id, ids["ds"], ids["project"], ids["plan"], ids["mapping"]),
        )
        cur.execute(
            """INSERT INTO app.datastream_outputs
                 (id, org_id, project_id, datastream_id, output_kind, stable_name, created_by)
               VALUES (%s,%s,%s,%s,'full_grain',%s,'tester')""",
            (output_id, ids["org"], ids["project"], ids["ds"], _RELATION),
        )
        cur.execute(
            """INSERT INTO app.datastream_output_versions
                 (id, output_id, org_id, project_id, datastream_id, execution_id,
                  plan_version_id, mapping_version_id, relation_ref, grain_evidence,
                  evidence, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,'tester')""",
            (
                ids["output_version"], output_id, ids["org"], ids["project"], ids["ds"],
                execution_id, ids["plan"], ids["mapping"], _RELATION,
            ),
        )
    return ids


def _payload(slot=0):
    return {
        "schemaVersion": "1",
        "slot": slot,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-15", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": "conversions", "metrics": ["conversions"]},
        "evidenceRefs": ["metric:conversions"],
    }


def _publish(conn, project_id: str, org_id: str) -> dict:
    metrics, dims = {"conversions"}, {"reporting_date"}
    return dit.publish(
        project_id=project_id,
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx={
            "available_metrics": metrics,
            "available_dimensions": dims,
            "available_templates": {"conversions"},
            "resolvable_evidence": evidence_universe(
                available_metrics=metrics, available_dimensions=dims
            ),
            "freshness_date": "2026-07-21",
            "has_project_access": True,
            "existing_slots": set(),
        },
        conn=_NoCommit(conn),
        identity="person_test",
        result_fn=lambda p: produce_insight_result(
            conn, org_id=org_id, project_id=project_id, payload=p, actor="person_test"
        ),
    )


def test_publishing_an_insight_produces_a_governed_result(live_postgres, warehouse):
    ids = _seed_governed_world(live_postgres)
    ack = _publish(live_postgres, ids["project"], ids["org"])
    assert ack["ok"] is True, ack
    lineage = ack["results"][0]
    assert lineage["resultId"], lineage
    assert lineage["outcome"] == "success", lineage

    # The Result is real: written by the ONE writer, over an immutable spec
    # version, with the window the card claimed and only its rows.
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT outcome, row_count, query_spec_version_id FROM app.query_results "
            "WHERE id = %s AND org_id = %s AND project_id = %s",
            (lineage["resultId"], ids["org"], ids["project"]),
        )
        result = cur.fetchone()
        assert result is not None
        assert result[0] == "success"
        assert result[1] == 2, "the period filter must keep July's two rows, not June's"
        assert result[2] == lineage["querySpecVersionId"]
        cur.execute(
            "SELECT spec FROM app.query_spec_versions WHERE id = %s AND project_id = %s",
            (lineage["querySpecVersionId"], ids["project"]),
        )
        spec = cur.fetchone()[0]
        assert spec["time"]["start"] == "2026-07-15" and spec["time"]["end"] == "2026-07-21"

    # And the item names its lineage (migration 281): the row, not the ack, is
    # what a surface reads tomorrow.
    insight = store.get_run(ids["project"], "2026-07-21", live_postgres)["insights"][0]
    assert insight["result_id"] == lineage["resultId"]
    assert insight["query_spec_version_id"] == lineage["querySpecVersionId"]
    assert insight["result_unavailable_reason"] is None


def test_the_result_names_the_exact_output_version_it_read(live_postgres, warehouse):
    """data.md [9]: a Result must resolve the exact physical version behind it.

    The Output version identity was already in the executor's hand --
    `resolve_physical_plan` SELECTs `id` off `app.datastream_output_versions` --
    and was thrown away, so `datastream_output_version_id` was an allowlisted
    manifest key with one reader, one allowlist entry and NO writer. Every Result
    in existence therefore projected `unavailable` about the Output it had just
    read.

    Proved here rather than over a hand-built plan dict on purpose: the version
    asserted below is the row this test INSERTED, so a manifest that carried some
    other version -- a head re-resolved at projection time, say -- fails.
    """
    from core.analyze_render_mcp import project_provenance  # noqa: PLC0415

    ids = _seed_governed_world(live_postgres)
    ack = _publish(live_postgres, ids["project"], ids["org"])
    result_id = ack["results"][0]["resultId"]

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT manifest FROM app.query_result_payloads WHERE result_id = %s",
            (result_id,),
        )
        manifest = cur.fetchone()[0]

    assert manifest["datastream_output_version_id"] == ids["output_version"], manifest

    provenance = project_provenance(manifest)
    assert provenance["datastream_output_version_id"] == ids["output_version"]
    # And the chain the same snapshot wrote, under the names the WRITER spells.
    assert provenance["lineage"]["datastream_id"] == ids["ds"]
    assert provenance["lineage"]["mapping_version_id"] == ids["mapping"]
    assert provenance["lineage"]["relation"] == _RELATION
    assert provenance["lineage"]["member_count"] == 2
    # Nothing predates anything here, so no reason is stated.
    assert provenance["lineage_unavailable_reason"] is None
    # The DQ owner answered; "nothing open" is not "we could not look".
    assert provenance["data_quality"]["evaluation_ids"] == []
    assert provenance["data_quality"]["unavailable_reason"] is None


def test_the_named_result_is_shareable_through_the_one_mechanism(live_postgres, warehouse):
    """Render over the publication's Result, Share over the Render. No new door."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from core import render_shares  # noqa: PLC0415

    ids = _seed_governed_world(live_postgres)
    ack = _publish(live_postgres, ids["project"], ids["org"])
    result_id = ack["results"][0]["resultId"]
    spec_version_id = ack["results"][0]["querySpecVersionId"]
    assert result_id

    with live_postgres.cursor() as cur:
        cur.execute("SELECT content_hash FROM app.query_results WHERE id = %s", (result_id,))
        content_hash = cur.fetchone()[0]
        cur.execute(
            "SELECT query_spec_id FROM app.query_spec_versions WHERE id = %s",
            (spec_version_id,),
        )
        query_spec_id = cur.fetchone()[0]
        visualization_id, viz_version_id, render_id = _uid("viz"), _uid("vsv"), _uid("rnd")
        cur.execute(
            "INSERT INTO app.visualizations (id, org_id, project_id, query_spec_id, name, "
            "created_by) VALUES (%s,%s,%s,%s,'Insight card','tester')",
            (visualization_id, ids["org"], ids["project"], query_spec_id),
        )
        cur.execute(
            "INSERT INTO app.visualization_spec_versions (id, visualization_id, org_id, "
            "project_id, version_number, query_spec_id, query_spec_version_id, "
            "spec_contract_version, schema_version, family, spec, content_hash, created_by) "
            "VALUES (%s,%s,%s,%s,1,%s,%s,'visualization-spec.v1',1,'table',%s::jsonb,%s,"
            "'tester')",
            (
                viz_version_id, visualization_id, ids["org"], ids["project"],
                query_spec_id, spec_version_id,
                json.dumps(
                    {
                        # The four pins ck_visualization_spec_versions_document_pins
                        # (migration 159) requires the document to repeat.
                        "spec_contract_version": "visualization-spec.v1",
                        "schema_version": 1,
                        "family": "table",
                        "accessibility": {"table_fallback": "required"},
                        "bindings": {"dimension": ["date"], "measure": ["conversions"]},
                    }
                ),
                _HASH,
            ),
        )
        cur.execute(
            "INSERT INTO app.renders (id, org_id, project_id, result_id, "
            "result_content_hash, visualization_spec_version_id, renderer_adapter, "
            "renderer_build_id, runtime_build_id, theme_version, formatter_version, "
            "responsive_profile, display_state, evidence_manifest, datum_evidence_keys, "
            "creation_surface, origin_kind, content_hash, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,'toorow-table','table/toorow-table@1.0.0',"
            "'@toorow/card-shell/viz@1.0.0+abc','theme-1','fmt-1','share','{}'::jsonb,"
            "%s::jsonb,'{}'::jsonb,'explore','explore',%s,'tester')",
            (
                render_id, ids["org"], ids["project"], result_id, content_hash,
                viz_version_id,
                json.dumps({"freshness": "2026-07-21", "grain": "day"}),
                _HASH,
            ),
        )

    # Migration 323: the exit is a project-scoped capability, default `forbidden`.
    # This fixture allows it explicitly, because what is being proved here is
    # that a published insight's Result reaches the ONE Share mechanism -- not
    # what the mechanism does when the Project has never authorized an exit.
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_preferences (project_id, external_sharing) "
            "VALUES (%s, 'allowed') ON CONFLICT (project_id) DO UPDATE "
            "SET external_sharing = 'allowed'",
            (ids["project"],),
        )
    requested = render_shares.create_share(
        live_postgres,
        org_id=ids["org"],
        project_id=ids["project"],
        render_id=render_id,
        actor="owner@example.com",
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        idempotency_key=f"share-{ULID()}",
    )
    assert requested.share_id
    assert requested.state == "pending_confirmation"
    # And the link exists only once a SECOND role holder authorizes the exit.
    created = render_shares.confirm_share(
        live_postgres,
        org_id=ids["org"],
        project_id=ids["project"],
        share_id=requested.share_id,
        actor="second.holder@example.com",
        idempotency_key=f"confirm-{requested.share_id}",
    )
    assert "#render=" in created.delivery_url


def test_a_card_no_view_covers_still_publishes_with_the_reason_named(
    live_postgres, warehouse
):
    """The honest refusal: publication survives, shareability says why not."""
    with live_postgres.cursor() as cur:
        org_id, project_id = _uid("org"), _uid("proj")
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "AI-294 bare", org_id.lower().replace("_", "-"), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, "AI-294 bare", project_id.lower().replace("_", "-"), "tester"),
        )

    ack = _publish(live_postgres, project_id, org_id)
    assert ack["ok"] is True, "a derivation refusal must never block publication"
    lineage = ack["results"][0]
    assert lineage["resultId"] is None
    assert lineage["resultUnavailableReason"]

    insight = store.get_run(project_id, "2026-07-21", live_postgres)["insights"][0]
    assert insight["result_id"] is None
    assert insight["result_unavailable_reason"] == lineage["resultUnavailableReason"]

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.query_results WHERE project_id = %s", (project_id,)
        )
        assert cur.fetchone()[0] == 0, "no Result may exist for a refused derivation"
