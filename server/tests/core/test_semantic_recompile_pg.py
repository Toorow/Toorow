"""AI-346 -- a compiled artifact is re-derived when the compiler moves, and a
member's role is the Concept's kind.

WHY PG-GATED. Everything worth proving here is a property of rows: that the
newest artifact is the one the read side takes, that the version row is not
touched, that `uq_semantic_compiled_artifact_version` makes the sweep idempotent,
that the attempt record is what the refusal reads. A mocked cursor would prove
that the fixture and the assertion agree.

`live_postgres` rolls back on teardown; every test seeds its own Project.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core import semantic_model
from core.query_specs import QuerySpecRefused, _load_pinned_view
from core.semantic_compiler import COMPILER_VERSION
from core.semantic_model import (
    SemanticRefused,
    _apply_view,
    _compute_diff,
    _members_for_compilation,
    list_stale_view_versions,
    recompile_stale_artifacts,
)
from ulid import ULID

psycopg = pytest.importorskip("psycopg")

STALE_COMPILER = "semantic-compiler.v1"
ZERO_HASH = "0" * 64
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "336_a_member_role_is_the_concepts_kind_and_an_artifact_is_rederived.sql"
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


# ---------------------------------------------------------------------------
# Seeding -- a Project, two published Concepts, one published View version whose
# newest artifact was compiled by an EARLIER compiler and whose stored member
# roles are all `metric` (the shape measured in production on 2026-09-01).
# ---------------------------------------------------------------------------


def _project(conn) -> tuple[str, str]:
    org_id, project_id = _uid("org"), _uid("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s, %s, %s, %s)",
            (org_id, "AI-346 fixture", org_id.lower(), "owner@example.com"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (project_id, org_id, "AI-346 fixture", project_id.lower(), "owner@example.com"),
        )
    return org_id, project_id


def _concept(conn, project_id: str, *, kind: str, name: str) -> tuple[str, str]:
    concept_id, version_id = _uid("sc"), _uid("scv")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts (id, project_id, kind, name, lifecycle_status, "
            "created_by) VALUES (%s, %s, %s, %s, 'published', 'owner@example.com')",
            (concept_id, project_id, kind, name),
        )
        if kind == "metric":
            cur.execute(
                """
                INSERT INTO app.semantic_concept_versions
                    (id, concept_id, project_id, version_number, status, kind, name, label,
                     value_type, expression, aggregation, additivity_class, content_hash,
                     created_by)
                VALUES (%s, %s, %s, 1, 'published', 'metric', %s, %s, 'integer',
                        %s::jsonb, '{"function": "sum"}'::jsonb, 'additive', %s,
                        'owner@example.com')
                """,
                (
                    version_id, concept_id, project_id, name, name.title(),
                    json.dumps({"op": "source_measure", "concept": name}), ZERO_HASH,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO app.semantic_concept_versions
                    (id, concept_id, project_id, version_number, status, kind, name, label,
                     value_type, semantic_type, content_hash, created_by)
                VALUES (%s, %s, %s, 1, 'published', 'dimension', %s, %s, 'string',
                        'geographic', %s, 'owner@example.com')
                """,
                (version_id, concept_id, project_id, name, name.title(), ZERO_HASH),
            )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (version_id, concept_id),
        )
    return concept_id, version_id


def _projection(metric: tuple[str, str], dimension: tuple[str, str], *, datasets: bool = True):
    """The Ossie projection the v1 compiler wrote: datasets with the per-member
    dataset inside the toorow extension. `datasets=False` is a projection that
    names none -- the composition that cannot be recompiled."""
    if not datasets:
        return {"datasets": [], "metrics": []}
    return {
        "datasets": [
            {
                "name": "fact",
                "source": "fact_table",
                "fields": [
                    {
                        "name": "country",
                        "custom_extensions": [
                            {
                                "vendor_name": "COMMON",
                                "data": json.dumps({"toorow": {"concept_id": dimension[0]}}),
                            }
                        ],
                    }
                ],
            }
        ],
        "metrics": [
            {
                "name": "clicks",
                "custom_extensions": [
                    {
                        "vendor_name": "COMMON",
                        "data": json.dumps(
                            {"toorow": {"concept_id": metric[0], "dataset": "fact"}}
                        ),
                    }
                ],
            }
        ],
    }


def _stale_view(conn, project_id: str, *, datasets: bool = True) -> dict:
    metric = _concept(conn, project_id, kind="metric", name=f"clicks_{ULID()}".lower())
    dimension = _concept(conn, project_id, kind="dimension", name=f"country_{ULID()}".lower())
    view_id, version_id, artifact_id = _uid("sv"), _uid("svv"), _uid("sca")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by) "
            "VALUES (%s, %s, %s, 'published', 'owner@example.com')",
            (view_id, project_id, f"view_{view_id.lower()}"),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', %s, 'Stale fixture', %s, %s,
                    'owner@example.com')
            """,
            (version_id, view_id, project_id, f"view_{view_id.lower()}", ZERO_HASH, ZERO_HASH),
        )
        # The production shape: EVERY member stored as `metric`.
        for ordinal, (concept_id, concept_version_id) in enumerate((metric, dimension)):
            cur.execute(
                "INSERT INTO app.semantic_view_version_concepts "
                "(view_version_id, ordinal, concept_id, concept_version_id, role) "
                "VALUES (%s, %s, %s, %s, 'metric')",
                (version_id, ordinal, concept_id, concept_version_id),
            )
        cur.execute(
            """
            INSERT INTO app.semantic_compiled_artifacts
                (id, project_id, view_version_id, compiler_version, content_hash,
                 queryability_matrix, ossie_projection, ossie_spec_version,
                 toorow_extension_version, dbt_relation_refs, created_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, '0.1.1', '2', '[]'::jsonb,
                    NOW() - INTERVAL '1 hour')
            """,
            (
                artifact_id, project_id, version_id, STALE_COMPILER, ZERO_HASH,
                json.dumps({"metrics": [], "dimensions": [], "cells": [], "summary": {}}),
                json.dumps(_projection(metric, dimension, datasets=datasets)),
            ),
        )
        cur.execute(
            "UPDATE app.semantic_views SET current_version_id = %s WHERE id = %s",
            (version_id, view_id),
        )
    return {
        "view_id": view_id,
        "version_id": version_id,
        "artifact_id": artifact_id,
        "metric": metric,
        "dimension": dimension,
    }


def _artifacts(conn, version_id: str) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, compiler_version FROM app.semantic_compiled_artifacts "
            "WHERE view_version_id = %s ORDER BY created_at",
            (version_id,),
        )
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]


def _load(conn, project_id: str, seeded: dict):
    return _load_pinned_view(
        conn,
        project_id=project_id,
        semantic_view_id=seeded["view_id"],
        semantic_view_version_id=seeded["version_id"],
    )


# ---------------------------------------------------------------------------
# (a) stale -> refused; sweep -> fresh artifact; same load succeeds, roles from kind
# ---------------------------------------------------------------------------


def test_a_stale_artifact_is_refused_then_rederived_and_the_same_load_succeeds(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)

    with pytest.raises(QuerySpecRefused) as before:
        _load(live_postgres, project_id, seeded)
    assert before.value.code == "stale_compiled_artifact"
    # Not reached by the sweep yet: the sentence says the product recompiles,
    # and still names the gesture that does it now.
    assert "at start and every night" in str(before.value)
    assert "publish it again" in str(before.value)

    report = recompile_stale_artifacts(live_postgres, project_id=project_id, actor="tester")

    assert [e["version_id"] for e in report["recompiled"]] == [seeded["version_id"]]
    assert report["refused"] == []
    assert report["recompiled"][0]["composition_source"] == "stored_rows"

    version, matrix = _load(live_postgres, project_id, seeded)
    assert version["id"] == seeded["version_id"]
    # THE ROLE IS THE KIND. The stored rows said `metric` twice; the compiled
    # matrix puts the dimension where the Concept says it belongs.
    assert [m["concept_id"] for m in matrix["metrics"]] == [seeded["metric"][0]]
    assert [d["concept_id"] for d in matrix["dimensions"]] == [seeded["dimension"][0]]
    assert matrix["dimensions"][0]["name"]  # the v2 key the language guard judges on
    assert matrix["summary"] == {
        "metrics": 1, "dimensions": 1, "pairs": 1, "accepted": 1, "refused": 0
    }

    # A NEW row, the old one untouched, the version row untouched.
    rows = _artifacts(live_postgres, seeded["version_id"])
    assert rows[0] == (seeded["artifact_id"], STALE_COMPILER)
    assert rows[1][1] == COMPILER_VERSION and rows[1][0] == report["recompiled"][0]["artifact_id"]
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT status, version_number FROM app.semantic_view_versions WHERE id = %s",
            (seeded["version_id"],),
        )
        assert cur.fetchone() == ("published", 1)
        cur.execute(
            "SELECT outcome, artifact_id, composition_source FROM app.semantic_recompile_attempts "
            "WHERE view_version_id = %s AND compiler_version = %s",
            (seeded["version_id"], COMPILER_VERSION),
        )
        assert cur.fetchone() == ("recompiled", rows[1][0], "stored_rows")
        cur.execute(
            "SELECT count(*) FROM app.audit_log "
            "WHERE action = %s AND metadata->>'view_version_id' = %s",
            (semantic_model.ACTION_SEMANTIC_MODEL_RECOMPILE_ARTIFACT, seeded["version_id"]),
        )
        assert cur.fetchone()[0] == 1


def test_the_confirmed_change_set_is_read_first_and_its_roles_are_not_trusted(live_postgres):
    """The change set carried `role: metric` for the dimension -- the defect. The
    sweep reads its datasets and strips its roles."""
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id, datasets=False)  # projection is useless
    intent = {
        "action": "create_view",
        "view": {
            "name": f"view_{seeded['view_id'].lower()}",
            "label": "Stale fixture",
            "concepts": [
                {"concept_id": c, "concept_version_id": v, "dataset": "fact", "role": "metric"}
                for c, v in (seeded["metric"], seeded["dimension"])
            ],
            "datasets": [{"name": "fact", "source": "fact_table"}],
            "relationships": [],
        },
    }
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_change_sets
                (id, project_id, object_type, object_id, intent, state, test_gate_state,
                 confirmation_used_at, result_version_id, idempotency_key_hash, created_by)
            VALUES (%s, %s, 'semantic-view', %s, %s::jsonb, 'confirmed', 'pass', NOW(), %s,
                    %s, 'owner@example.com')
            """,
            (_uid("scs"), project_id, seeded["view_id"], json.dumps(intent),
             seeded["version_id"], ZERO_HASH),
        )

    report = recompile_stale_artifacts(live_postgres, project_id=project_id, actor="tester")

    assert report["refused"] == []
    assert report["recompiled"][0]["composition_source"] == "change_set"
    _, matrix = _load(live_postgres, project_id, seeded)
    assert [d["concept_id"] for d in matrix["dimensions"]] == [seeded["dimension"][0]]


# ---------------------------------------------------------------------------
# (b) idempotent
# ---------------------------------------------------------------------------


def test_the_sweep_is_idempotent(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)

    first = recompile_stale_artifacts(live_postgres, project_id=project_id, actor="tester")
    second = recompile_stale_artifacts(live_postgres, project_id=project_id, actor="tester")

    assert len(first["recompiled"]) == 1
    assert second["stale"] == [] and second["recompiled"] == [] and second["refused"] == []
    assert list_stale_view_versions(live_postgres, project_id=project_id) == []
    assert len(_artifacts(live_postgres, seeded["version_id"])) == 2


def test_a_dry_run_lists_and_writes_nothing(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)

    report = recompile_stale_artifacts(
        live_postgres, project_id=project_id, actor="tester", dry_run=True
    )

    assert [e["version_id"] for e in report["stale"]] == [seeded["version_id"]]
    assert report["stale"][0]["compiled_by"] == STALE_COMPILER
    assert len(_artifacts(live_postgres, seeded["version_id"])) == 1


def test_a_superseded_version_is_not_swept_because_it_cannot_execute(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)
    with live_postgres.cursor() as cur:
        # Detach the pointer, then supersede: the trigger admits this transition.
        cur.execute(
            "UPDATE app.semantic_views SET current_version_id = NULL WHERE id = %s",
            (seeded["view_id"],),
        )
        cur.execute(
            "UPDATE app.semantic_view_versions SET status = 'superseded' WHERE id = %s",
            (seeded["version_id"],),
        )
    assert list_stale_view_versions(live_postgres, project_id=project_id) == []


# ---------------------------------------------------------------------------
# (c) a refused recompilation is reported, and the read names the attempt
# ---------------------------------------------------------------------------


def test_a_version_that_cannot_be_recompiled_stays_refused_and_the_refusal_names_the_attempt(
    live_postgres,
):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id, datasets=False)

    report = recompile_stale_artifacts(live_postgres, project_id=project_id, actor="tester")

    assert report["recompiled"] == []
    (refused,) = report["refused"]
    assert refused["version_id"] == seeded["version_id"]
    assert "unknown_dataset" in refused["codes"]
    assert "publish it again" in refused["gesture"]
    assert len(_artifacts(live_postgres, seeded["version_id"])) == 1

    with pytest.raises(QuerySpecRefused) as exc:
        _load(live_postgres, project_id, seeded)
    assert exc.value.code == "stale_compiled_artifact"
    message = str(exc.value)
    assert "tried to recompile it on" in message
    assert datetime.now(UTC).date().isoformat() in message
    assert "unknown_dataset" in message
    assert "publish it again" in message
    assert {r.code for r in exc.value.refusals} >= {"stale_compiled_artifact", "unknown_dataset"}

    # Still stale, still listed: the sweep hides nothing it could not repair.
    assert [v["id"] for v in list_stale_view_versions(live_postgres, project_id=project_id)] == [
        seeded["version_id"]
    ]


# ---------------------------------------------------------------------------
# (d) the writer: role from kind, never from the payload
# ---------------------------------------------------------------------------


def _view_row(seeded: dict, concepts: list[dict]) -> dict:
    return {
        "object_id": None,
        "intent": {
            "action": "create_view",
            "view": {
                "name": f"written_{ULID()}".lower(),
                "label": "Writer fixture",
                "concepts": concepts,
                "datasets": [{"name": "fact", "source": "fact_table"}],
            },
        },
        "validation": {"queryability_matrix": {"summary": {}}, "ossie_projection": {}},
        "dependency_fingerprint": ZERO_HASH,
    }


def test_the_writer_stores_the_kind_when_the_payload_omits_the_role(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)
    row = _view_row(
        seeded,
        [
            {"concept_id": c, "concept_version_id": v}
            for c, v in (seeded["metric"], seeded["dimension"])
        ],
    )

    version_id = _apply_view(live_postgres, project_id, row, actor="owner@example.com")

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT concept_id, role FROM app.semantic_view_version_concepts "
            "WHERE view_version_id = %s ORDER BY ordinal",
            (version_id,),
        )
        assert cur.fetchall() == [
            (seeded["metric"][0], "metric"),
            (seeded["dimension"][0], "dimension"),
        ]


def test_the_writer_refuses_a_role_that_contradicts_the_kind(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)
    row = _view_row(
        seeded,
        [
            {"concept_id": seeded["metric"][0], "concept_version_id": seeded["metric"][1]},
            {
                "concept_id": seeded["dimension"][0],
                "concept_version_id": seeded["dimension"][1],
                "role": "metric",
            },
        ],
    )

    with pytest.raises(SemanticRefused) as exc:
        _apply_view(live_postgres, project_id, row, actor="owner@example.com")
    assert exc.value.code == "member_role_mismatch"


def test_prepare_refuses_the_same_contradiction_by_name(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)

    members, refusals = _members_for_compilation(
        live_postgres,
        project_id,
        [
            {"concept_id": seeded["metric"][0], "concept_version_id": seeded["metric"][1]},
            {
                "concept_id": seeded["dimension"][0],
                "concept_version_id": seeded["dimension"][1],
                "role": "metric",
                "dataset": "fact",
            },
        ],
    )

    assert [r.code for r in refusals] == ["member_role_mismatch"]
    assert refusals[0].path == "$.concepts[1].role"
    assert [m.role for m in members] == ["metric"]


def test_the_diff_sees_a_member_change_and_never_the_derived_role(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)
    base = {"base_version_id": seeded["version_id"], "object_type": "semantic-view"}
    same_members_other_roles = {
        "view": {
            "concepts": [
                {"concept_id": c, "concept_version_id": v, "role": "dimension"}
                for c, v in (seeded["metric"], seeded["dimension"])
            ]
        }
    }
    fewer_members = {
        "view": {
            "concepts": [
                {"concept_id": seeded["metric"][0], "concept_version_id": seeded["metric"][1]}
            ]
        }
    }

    unchanged = _compute_diff(live_postgres, project_id, base, same_members_other_roles)
    changed = _compute_diff(live_postgres, project_id, base, fewer_members)

    assert [c["field"] for c in unchanged["changed"]] == []
    (member_change,) = changed["changed"]
    assert member_change["field"] == "concepts"
    assert [m["concept_id"] for m in member_change["from"]] == [
        seeded["metric"][0], seeded["dimension"][0]
    ]
    assert [m["concept_id"] for m in member_change["to"]] == [seeded["metric"][0]]


# ---------------------------------------------------------------------------
# (e) the migration's UPDATE corrects a seeded mismatch, idempotently
# ---------------------------------------------------------------------------


def _migration_update() -> str:
    text = MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"UPDATE app\.semantic_view_version_concepts.*?;", text, re.DOTALL)
    assert match, "migration 336 carries the corrective UPDATE"
    return match.group(0)


def test_the_migration_sets_every_stored_role_to_the_concepts_kind(live_postgres):
    _, project_id = _project(live_postgres)
    seeded = _stale_view(live_postgres, project_id)
    statement = _migration_update()

    with live_postgres.cursor() as cur:
        cur.execute(statement)
        first = cur.rowcount
        cur.execute(statement)
        second = cur.rowcount
        cur.execute(
            "SELECT m.role, c.kind FROM app.semantic_view_version_concepts m "
            "JOIN app.semantic_concepts c ON c.id = m.concept_id "
            "WHERE m.view_version_id = %s ORDER BY m.ordinal",
            (seeded["version_id"],),
        )
        rows = cur.fetchall()

    assert first >= 1 and second == 0
    assert rows == [("metric", "metric"), ("dimension", "dimension")]
