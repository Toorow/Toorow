"""Capture the one server-owned Analyze render CallToolResult (Story 65.1, AI-337).

WHAT CHANGED ON 2026-08-31, AND WHY IT IS THE WHOLE POINT OF THIS FILE. Until
today this script AUTHORED the rows it captured: two literal dictionaries
(`{channel: organic, sessions: 1240}`) under a `qr_FIXTURE` result id, handed to
the real render tool through four patched seams (`_answer`, `_load_result`,
`_load_payload`, `load_visualization_spec_version`). The envelope was composed by
the product, and everything inside it was written here -- so criterion [20] of
`docs/product-architecture/visualization-and-rendering.md` stayed open: *no
Result a warehouse execution produced had ever been drawn through this runtime.*

It now CAPTURES. The script builds one governed chain in a real PostgreSQL,
validates a Query Spec against the pinned Semantic View version, runs it through
`core.query_execution` against the real local DuckDB warehouse, writes a
grammar-validated Visualization Spec version pinned to that exact Query Spec
version, and only then calls the real render tool over an in-process FastMCP
client. The rows in the fixture are the rows the warehouse returned; the
identities in it are the identities the run minted.

WHAT IS STILL SEEDED, said rather than implied: the governed chain itself -- the
org, the project, the Datastream and its published mapping, the Semantic View
version and its compiled queryability matrix. Those are DECLARATIONS a person
makes in the product, not measurements; the Result is the measurement, and the
Result is executed.

Prerequisites (both are real, and neither is mocked):

    python scripts/disposable_postgres.py up        # TEST_POSTGRES_DSN
    uv run python server/modules/google-analytics/seeds/run_local_loop.py

THE THIRD PRECONDITION, AND IT IS THE ONE THAT COST A SESSION (AI-349, measured
2026-09-01). The render tool resolves its caller through
`project_access.resolve_strict_resource_access`, which TRANSLATES the identity
through `identity_bridge.canonical_identity` before comparing it to
`app.org_members.identity`. This script used to seed that membership row under
the literal subject `owner@example.com`. On a database where any earlier suite had
registered that same subject -- `server/tests/conftest.py::canonical_person` does
it, and `app.person_identities` is append-only, so the row outlives every rollback
-- the bridge resolved the caller to `person_<ULID>` while the membership row still
said `owner@example.com`. The two halves named the same human two ways, exactly the
defect `identity_bridge` was written for, and the tool answered its one mute
`not_found`.

Two things follow, and both are in this file rather than in a reader's memory:

* `seed_governed_chain` seeds the membership under the CANONICAL identity, which
  is the identity the guard will compare. It therefore holds on a database that
  carries the registration and on one that does not.
* `assert_capture_identity_can_view` re-reads that decision before the render tool
  is called, so a chain that is somehow still unreachable is refused HERE, by a
  message that names the missing row and the gesture -- never by a `not_found`
  three frames deep in a FastMCP client.

Usage (from the repository root):

    uv run python scripts/generate_analyze_render_tool_result_fixture.py
    uv run python scripts/generate_analyze_render_tool_result_fixture.py --check

`--check` re-runs the whole capture and compares it to the committed fixture with
every run-minted identity (ULID ids, content hashes, timestamps) folded to its
shape. A fresh execution mints new ids by construction, so comparing them byte
for byte would only ever prove that nobody re-ran it; what must not drift is the
DATA -- the rows, the schema, the spec document, the pins and the answer text.

Stdout is ASCII-only (AI-03).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

#: Set INSIDE `_capture_seams` and restored on the way out, never at import. This
#: module is imported by
#: `server/tests/integration/test_mcp_analyze_app_payload.py` to reach the
#: fixture path and the wire normalizer; an import that mutated the auth mode or
#: a signing secret would reconfigure the whole pytest session from a file nobody
#: reads as configuration. It did, once: `TOOROW_AUTH_MODE=oauth` set here broke
#: the collection of `test_mcp_data_render_split.py`.
_CAPTURE_ENV = {
    # The render tool resolves the caller through the strict access guard, which
    # is the deployment's own mode. Without it the guard fails closed to the one
    # `not_found` the tool answers for everything.
    "TOOROW_AUTH_MODE": "oauth",
    "TOOROW_FEEDBACK_CONTEXT_SECRET": "analyze-render-capture-secret-at-least-32-bytes",
}

FIXTURE_PATH = ROOT / "ui/cards/shell/src/viz/__tests__/fixtures/analyzeRenderToolResult.json"
RENDERER_BUILDS = ROOT / "ui/cards/shell/src/viz/rendererBuilds.generated.json"
WAREHOUSE = ROOT / "server/modules/google-analytics/seeds/local.duckdb"

#: The landing the captured Query Spec reads. It is a real relation of the local
#: warehouse, built by the google-analytics seed loop (generate -> load -> dbt),
#: and it is named with its schema so the read needs no dataset qualifier.
RELATION = "main.raw_ga4_standard_daily"

#: The two members the captured question is made of, and the physical column each
#: one is mapped to. `(concept name, kind, value type, physical column)`.
MEMBERS = (
    ("device_category", "dimension", "string", "device_category"),
    ("sessions", "metric", "integer", "sessions"),
)

#: The visual the capture presents the Result through. Bindings are filled with
#: the MEMBER IDS the run mints -- a Visualization Spec binds concept ids, which
#: is what `visualization_specs.check_shape_compatibility` validates against.
FAMILY = "bar"
PROFILE = "mcp-inline"

#: Matched ANYWHERE in a string, not only as the whole of one: the answer's text
#: channel spells the Result id and the content hash inside a sentence, so an
#: anchored pattern folded the ids in the meta and left the same ids unfolded in
#: the prose -- and `--check` then reported drift on every run.
_ULID_ID = re.compile(r"\b([a-z]{2,5})_[0-9A-HJKMNP-TV-Z]{26}\b")
_HASH64 = re.compile(r"\b[0-9a-f]{64}\b")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)


def _dsn() -> str:
    dsn = os.environ.get("TEST_POSTGRES_DSN") or os.environ.get("PLATFORM_DB_URL") or ""
    if not dsn:
        raise SystemExit(
            "error: TEST_POSTGRES_DSN is required. Run: "
            "python scripts/disposable_postgres.py up"
        )
    database = _database_name(dsn)
    # THIS SCRIPT WRITES, and it falls back to `PLATFORM_DB_URL` -- the variable a
    # developer shell most often has pointed at the deployed database. It seeds an
    # organization, a project, a Datastream and a Semantic View, then EXECUTES a
    # Query Spec, which appends to `app.query_results`: append-only tables, so
    # nothing it inserted could be taken back. The same refusal
    # `disposable_postgres.py` encodes as its trap 3 therefore belongs here, on the
    # side that does the writing.
    if database.rsplit("_", 1)[-1] != "test":
        raise SystemExit(
            f"refusing: this capture SEEDS a governed chain and EXECUTES a Query Spec, "
            f"so it writes -- and the database it was pointed at is {database!r}, which "
            f"does not end in `_test`. Point it at a disposable cluster instead:\n"
            f"    python scripts/disposable_postgres.py up\n"
            f'    eval "$(python scripts/disposable_postgres.py env)"'
        )
    return dsn


def _database_name(dsn: str) -> str:
    """The database a DSN names, or `''` when it names none."""
    from urllib.parse import urlsplit  # noqa: PLC0415

    return urlsplit(dsn).path.lstrip("/").split("?", 1)[0]


# ---------------------------------------------------------------------------
# The runtime the capture pins to -- read from the shipped registry projection.
# ---------------------------------------------------------------------------


def runtime_manifest() -> dict[str, Any]:
    """The deployed-runtime manifest, DERIVED from the committed registry projection.

    `rendererBuilds.generated.json` is emitted by
    `pnpm --filter @toorow/card-shell generate:build-identity` and carries the one
    runtime identity the runtime's own `buildInfo.generated.ts` carries. Reading it
    here is what keeps the pins in this fixture equal to the build the parity suite
    mounts: a hand-typed pin, or the stale manifest of an older `dist/`, produces an
    envelope the runtime REFUSES ("deploy the build whose hash matches"), which is a
    green generator and a red screen.
    """
    registry = json.loads(RENDERER_BUILDS.read_text(encoding="utf-8"))
    renderers = {
        build["family"]: {
            "renderer_build": build["id"],
            "schema_versions": {"min": 1, "max": 1},
            "profiles": list(build["responsive_profiles"]),
        }
        for build in registry["builds"]
    }
    first = registry["builds"][0]
    return {
        "schema_version": 1,
        "bundle_sha256": "",  # filled by `_write_runtime`, which owns the bytes
        "runtime_build": registry["runtime_build"],
        "theme_version": first["theme_version"],
        "formatter_version": first["formatter_version"],
        "renderers": renderers,
    }


def _write_runtime(directory: Path) -> Path:
    """Stand the runtime resource up so the render tool's bundle check passes.

    The BYTES are a placeholder and nothing reads them: what the tool verifies is
    that a manifest exists beside the served HTML and hashes it. The identities in
    that manifest are the shipped ones (see `runtime_manifest`), which is the half
    that must be true.
    """
    bundle = directory / "mcp-app.html"
    bundle.write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    manifest = runtime_manifest()
    manifest["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    bundle.with_name("runtime-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return bundle


def _pins() -> dict[str, str]:
    """The four pins the captured envelope carries, resolved by the real resolver."""
    from core import render_app_payload

    manifest = runtime_manifest()
    return render_app_payload.resolve_runtime_pins(
        manifest, family=FAMILY, schema_version=1, profile=PROFILE
    )


#: Kept as a module attribute because the replay test
#: (`server/tests/integration/test_mcp_analyze_app_payload.py`) stands its own
#: runtime up from the same identities. It is DERIVED, never typed.
PINS = _pins()


# ---------------------------------------------------------------------------
# The governed chain. Declarations, seeded; the Result is executed below.
# ---------------------------------------------------------------------------


def _uid(prefix: str) -> str:
    from ulid import ULID

    return f"{prefix}_{ULID()}"


def _hash64() -> str:
    from ulid import ULID

    return hashlib.sha256(str(ULID()).encode("utf-8")).hexdigest()


#: The subject the capture calls as. A shareable placeholder, never a real address
#: (CLAUDE.md). It is what `adapter._identity` is made to return, and it is the
#: SUBJECT -- what the membership row must carry is its canonical translation, see
#: `capture_identity` below.
CAPTURE_SUBJECT = "owner@example.com"


def capture_identity(conn) -> str:
    """The identity the guard will COMPARE, which is not always the one we call as.

    `resolve_strict_resource_access` runs `identity_bridge.canonical_identity` on
    its caller before matching `app.org_members.identity`. So seeding the
    membership under the raw subject is only correct while nothing has registered
    that subject in `app.person_identities` -- and that registry is append-only,
    shared by every suite that ran against the same database, and written by
    `server/tests/conftest.py::canonical_person` for this very subject. Seeding
    what the guard will compare is the only form of this that holds either way.
    """
    from core.identity_bridge import canonical_identity

    return canonical_identity(CAPTURE_SUBJECT, conn)


def assert_capture_identity_can_view(conn, *, project_id: str, identity: str) -> None:
    """Refuse BEFORE the render tool does, and name what is missing. (AI-349)

    The render tool fails closed to one `not_found` for every denial -- which is
    correct on the wire and useless to a generator, because the message names
    neither the identity that was refused nor the row that would admit it. This
    asks the same question the tool will ask, with the same auth mode, and turns
    the denial back into the sentence a reader can act on.
    """
    from core.project_access import resolve_strict_resource_access

    decision = resolve_strict_resource_access(
        identity,
        conn,
        project_id=project_id,
        minimum_capability="view",
        auth_mode=_CAPTURE_ENV["TOOROW_AUTH_MODE"],
    )
    if decision.allowed:
        return
    resolved = capture_identity(conn)
    raise SystemExit(
        "error: the capture identity cannot read the project it just seeded, so the "
        "render tool would answer its mute `not_found`.\n"
        f"  refused        : {decision.reason}\n"
        f"  called as      : {CAPTURE_SUBJECT}\n"
        f"  compared as    : {resolved}\n"
        f"  seeded member  : {identity}\n"
        f"  project        : {project_id}\n"
        "The precondition is that `app.org_members` carries an active row for the "
        "COMPARED identity in this project's organization. "
        + (
            "The two differ, so `app.person_identities` maps the subject to a person "
            "and the membership was seeded under the other name -- re-run against a "
            "database whose registry is not shared, or see `capture_identity`."
            if resolved != identity
            else "It was seeded here and is not readable back: check that the "
            "organization and the project are `active` and that the connection role "
            "may read `app.org_members`."
        )
    )


def seed_governed_chain(conn) -> dict[str, Any]:
    """One org, one project, one published Datastream, one published Semantic View."""
    from core.query_specs import COMPILER_VERSION

    org_id, project_id = _uid("org"), _uid("proj")
    identity = capture_identity(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Analyze render capture", org_id.lower(), identity),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, "Analyze render capture", project_id.lower(), identity),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
            "VALUES (%s,%s,%s,'owner','active',NOW())",
            (_uid("omem"), org_id, identity),
        )

    mapping_fields: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for name, kind, value_type, physical in MEMBERS:
            field_id = _uid("mdm")
            cur.execute(
                """
                INSERT INTO app.mdm_canonical_fields
                    (id, project_id, concept_kind, canonical_name, value_type, aggregation,
                     non_additive, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,FALSE,%s)
                """,
                (
                    field_id,
                    project_id,
                    kind if kind == "dimension" else "metric",
                    name,
                    value_type,
                    "sum" if kind == "metric" else None,
                    identity,
                ),
            )
            mapping_fields.append(
                {
                    "field_id": physical,
                    "physical_type": "number" if kind == "metric" else "string",
                    "suggestion": {
                        "semantic_role": "measure" if kind == "metric" else "dimension"
                    },
                    # `canonical_target` is the name `resolve_physical_plan` joins
                    # the semantic concept on; `mdm_target` is the canonical field
                    # the mapping bound. Both, because both are read.
                    "binding": {
                        "canonical_target": name,
                        "mdm_target": field_id,
                        "status": "confirmed",
                    },
                }
            )

    datastream_id, plan_id, mapping_id = _uid("ds"), _uid("dpv"), _uid("dmv")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, org_id, project_id, name, created_by, source_kind, module_name) "
            "VALUES (%s,%s,%s,%s,%s,'connector_pull','google-analytics')",
            (datastream_id, org_id, project_id, "GA4 standard daily", identity),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'plan.v1','connector_pull','toorow','managed_raw',
                    '{}'::jsonb,%s,%s,%s)
            """,
            (plan_id, datastream_id, project_id, _hash64(), _hash64(), identity),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'mapping.v1',%s,%s,%s,'0.1.1','2',%s::jsonb,'{}'::jsonb,%s,%s)
            """,
            (
                mapping_id,
                datastream_id,
                project_id,
                _hash64(),
                plan_id,
                _hash64(),
                json.dumps({"fields": mapping_fields, "grain": ["date"]}),
                _hash64(),
                identity,
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_plan_version_id=%s, "
            "current_mapping_version_id=%s WHERE id=%s",
            (plan_id, mapping_id, datastream_id),
        )

    execution_id, output_id, output_version_id = _uid("dse"), _uid("dso"), _uid("dsov")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, created_by)
            VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'published',%s)
            """,
            (execution_id, datastream_id, project_id, plan_id, mapping_id, identity),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_outputs
                (id, org_id, project_id, datastream_id, output_kind, stable_name, created_by)
            VALUES (%s,%s,%s,%s,'full_grain',%s,%s)
            """,
            (output_id, org_id, project_id, datastream_id, RELATION, identity),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_output_versions
                (id, output_id, org_id, project_id, datastream_id, execution_id,
                 plan_version_id, mapping_version_id, relation_ref, grain_evidence,
                 evidence, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,%s)
            """,
            (
                output_version_id,
                output_id,
                org_id,
                project_id,
                datastream_id,
                execution_id,
                plan_id,
                mapping_id,
                RELATION,
                identity,
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id=%s WHERE id=%s",
            (execution_id, datastream_id),
        )

    view_id, view_version_id = _uid("sv"), _uid("svv")
    concepts: dict[str, str] = {}
    concept_versions: dict[str, str] = {}
    with conn.cursor() as cur:
        for name, kind, value_type, _physical in MEMBERS:
            concept_id, concept_version_id = _uid("sc"), _uid("scv")
            concepts[name], concept_versions[name] = concept_id, concept_version_id
            cur.execute(
                "INSERT INTO app.semantic_concepts "
                "(id, project_id, kind, name, lifecycle_status, created_by) "
                "VALUES (%s,%s,%s,%s,'published',%s)",
                (concept_id, project_id, kind, name, identity),
            )
            cur.execute(
                """
                INSERT INTO app.semantic_concept_versions
                    (id, concept_id, project_id, version_number, status, kind, name, label,
                     value_type, expression, aggregation, additivity_class, semantic_type,
                     content_hash, created_by)
                VALUES (%s,%s,%s,1,'published',%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
                """,
                (
                    concept_version_id,
                    concept_id,
                    project_id,
                    kind,
                    name,
                    name.replace("_", " ").capitalize(),
                    value_type,
                    json.dumps({"op": "column", "name": name}) if kind == "metric" else None,
                    json.dumps({"default": "sum"}) if kind == "metric" else None,
                    "additive" if kind == "metric" else None,
                    None if kind == "metric" else "categorical",
                    _hash64(),
                    identity,
                ),
            )
            cur.execute(
                "UPDATE app.semantic_concepts SET current_version_id=%s WHERE id=%s",
                (concept_version_id, concept_id),
            )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s,%s,%s,%s)",
            (view_id, project_id, "ga4_standard", identity),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s,%s,%s,1,'published','ga4_standard','GA4 standard',%s,%s,%s)
            """,
            (view_version_id, view_id, project_id, _hash64(), _hash64(), identity),
        )
        for ordinal, (name, _kind, _vt, _physical) in enumerate(MEMBERS):
            cur.execute(
                """
                INSERT INTO app.semantic_view_version_bindings
                    (view_version_id, ordinal, concept_id, datastream_id, project_id,
                     mapping_version_id, output_ref, binding_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,'active')
                """,
                (
                    view_version_id,
                    ordinal,
                    concepts[name],
                    datastream_id,
                    project_id,
                    mapping_id,
                    json.dumps({"relation": RELATION}),
                ),
            )
        matrix = {
            "metrics": [
                {"concept_id": concepts["sessions"], "version_id": concept_versions["sessions"]}
            ],
            "dimensions": [
                {
                    "concept_id": concepts["device_category"],
                    "version_id": concept_versions["device_category"],
                    "name": "device_category",
                }
            ],
            "cells": [
                {
                    "metric_id": concepts["sessions"],
                    "dimension_id": concepts["device_category"],
                    "queryable": True,
                    "join_path": [],
                }
            ],
        }
        cur.execute(
            """
            INSERT INTO app.semantic_compiled_artifacts
                (id, project_id, view_version_id, compiler_version, content_hash,
                 queryability_matrix, ossie_projection, ossie_spec_version,
                 toorow_extension_version)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,'{}'::jsonb,'0.1.1','2')
            """,
            (
                _uid("sca"),
                project_id,
                view_version_id,
                COMPILER_VERSION,
                _hash64(),
                json.dumps(matrix),
            ),
        )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "identity": identity,
        "view_id": view_id,
        "view_version_id": view_version_id,
        "measure_id": concepts["sessions"],
        "dimension_id": concepts["device_category"],
    }


def execute_query_spec(conn, chain: dict[str, Any]) -> dict[str, Any]:
    """Validate, accept and RUN one Query Spec against the local warehouse."""
    from core.query_execution import accept_execution, run_execution
    from core.query_specs import create_query_spec_version, validate_query_spec

    validated = validate_query_spec(
        conn,
        project_id=chain["project_id"],
        semantic_view_id=chain["view_id"],
        semantic_view_version_id=chain["view_version_id"],
        payload={
            "measures": [{"id": chain["measure_id"]}],
            "dimensions": [{"id": chain["dimension_id"]}],
            "filters": [],
            # Ranked by the server, never by the runtime: the compiler is
            # forbidden to sort, so the order in the fixture is the order the
            # warehouse returned.
            "sort": [{"member_id": chain["measure_id"], "direction": "desc"}],
            "row_limit": 100,
            "grain": "day",
        },
    )
    version = create_query_spec_version(
        conn,
        org_id=chain["org_id"],
        project_id=chain["project_id"],
        validated=validated,
        actor=chain["identity"],
        name="Sessions by device category",
    )
    attempt = accept_execution(
        conn,
        org_id=chain["org_id"],
        project_id=chain["project_id"],
        query_spec_version_id=version["id"],
        actor=chain["identity"],
    )
    result = run_execution(
        conn,
        attempt=attempt,
        org_id=chain["org_id"],
        project_id=chain["project_id"],
        spec=validated.spec,
        semantic_view_version_id=chain["view_version_id"],
    )
    if result["outcome"] != "success":
        raise SystemExit(
            f"error: the execution answered `{result['outcome']}`, not `success`. "
            "The fixture may only be captured from a Result the warehouse produced."
        )
    return {
        "query_spec_id": version["query_spec_id"],
        "query_spec_version_id": version["id"],
        "attempt_id": attempt["attempt_id"],
        **result,
    }


def create_presentation(conn, chain: dict[str, Any], executed: dict[str, Any]) -> str:
    """One grammar-validated Visualization Spec version pinned to that Query Spec."""
    from core import visualization_specs

    document = {
        "spec_contract_version": "visualization-spec.v1",
        "schema_version": 1,
        "family": FAMILY,
        "bindings": {
            "dimension": [chain["dimension_id"]],
            "measure": [chain["measure_id"]],
        },
        "order": {"source": "result"},
        "axes": {
            "x": {"scale": "categorical", "zero_baseline": True, "tick_density": "normal"},
            "y": {"scale": "linear", "zero_baseline": True, "tick_density": "normal"},
        },
        "legend": {"position": "right", "visible": True},
        "formatting": {
            "number_style": "auto",
            "date_style": "auto",
            "unit_source": "semantic_view",
        },
        "color": {"role": "categorical", "semantic_direction": "higher_is_better"},
        "interactions": {
            "hover": True,
            "select": True,
            "zoom": False,
            "legend_toggle": True,
            "local_filter": False,
        },
        "evidence": {"datum_fields": [chain["dimension_id"]], "mark_binding": "datum"},
        "responsive": {"profiles": [PROFILE]},
        "accessibility": {
            "summary_source": "result_manifest",
            "table_fallback": "required",
        },
    }
    validated = visualization_specs.validate_visualization_spec(
        conn,
        project_id=chain["project_id"],
        query_spec_version_id=executed["query_spec_version_id"],
        payload=document,
    )
    version = visualization_specs.create_visualization_spec_version(
        conn,
        org_id=chain["org_id"],
        project_id=chain["project_id"],
        validated=validated,
        actor=chain["identity"],
        name="Sessions by device category",
    )
    return str(version["id"])


# ---------------------------------------------------------------------------
# The capture.
# ---------------------------------------------------------------------------


def normalize_meta(meta: dict) -> dict:
    """Keep the emitted wire shape while replacing volatile signed-context values.

    ONLY the signed feedback context is replaced, and only because it is a MAC
    over a clock: its token and expiry change every second and prove nothing about
    the envelope. Every other identity in this file is the one the run minted and
    is left exactly as it was emitted -- that is the point of the capture.
    """
    normalized = dict(meta)
    # `gate` is the AD-18 adherence verdict of THIS exchange (2026-09-01,
    # `analyze-and-test.md` amendment of that date): whether a context consult
    # preceded the call in the same session, on which basis. It is evidence about
    # the run, not about the envelope -- a capture with no consult before it says
    # `time_window_inference / not adherent`, the same tool inside a Skill session
    # says `observed_ai_path / adherent`. The frozen wire carries the envelope; the
    # verdict is asserted where it is measured, `tests/core/test_adherence.py`.
    normalized.pop("gate", None)
    sidecar = normalized.get("toorow.feedback")
    if not isinstance(sidecar, dict) or set(sidecar) != {
        "schema_version",
        "token",
        "interaction_ref",
        "expires_at",
    }:
        raise AssertionError("render fixture emitted a non-canonical feedback context")
    normalized["toorow.feedback"] = {
        "schema_version": sidecar["schema_version"],
        "token": "SIGNED_CONTEXT_FIXTURE",
        "interaction_ref": "afi_FIXTURE",
        "expires_at": "2026-01-01T00:15:00Z",
    }
    return normalized


@contextlib.contextmanager
def _capture_seams(identity: str):
    """The two seams a script cannot supply: an OAuth token, and a built bundle."""
    from core import analyze_render_mcp as adapter

    runtime_dir = Path(tempfile.mkdtemp(prefix="analyze_render_runtime_"))
    bundle = _write_runtime(runtime_dir)
    previous = {
        name: os.environ.get(name)
        for name in (*_CAPTURE_ENV, "TOOROW_VISUALIZATION_RUNTIME_DIST")
    }
    for name, value in _CAPTURE_ENV.items():
        os.environ.setdefault(name, value)
    os.environ["TOOROW_VISUALIZATION_RUNTIME_DIST"] = str(bundle)
    original_identity = adapter._identity
    adapter._identity = lambda: identity
    try:
        yield
    finally:
        adapter._identity = original_identity
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(runtime_dir, ignore_errors=True)


async def _call_render_tool(
    *, project_id: str, result_id: str, visualization_spec_version_id: str
) -> dict[str, Any]:
    from core import analyze_render_mcp as adapter
    from core.mcp_profiles import RENDER_TOOL_NAME
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import FastMCPTransport

    target = FastMCP("analyze-render-capture")
    adapter.register(target)
    async with Client(FastMCPTransport(target)) as client:
        result = await client.call_tool(
            RENDER_TOOL_NAME,
            {
                "project_id": project_id,
                "result_id": result_id,
                "visualization_spec_version_id": visualization_spec_version_id,
            },
        )
    return {
        "content": [block.model_dump(exclude_none=True) for block in result.content],
        "structuredContent": result.structured_content,
        "_meta": normalize_meta(result.meta),
        "isError": result.is_error,
    }


def build_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return `(wire, traced)` -- the captured CallToolResult and what produced it."""
    import psycopg

    workspace = Path(tempfile.mkdtemp(prefix="analyze_render_warehouse_"))
    try:
        if not WAREHOUSE.exists():
            raise SystemExit(
                f"error: warehouse not found: {WAREHOUSE}\n"
                "build it first: uv run python "
                "server/modules/google-analytics/seeds/run_local_loop.py"
            )
        # A COPY, opened read-only by the runner: the seeded warehouse stays
        # untouched and unlocked, and dbt bakes the database name into its views
        # so the copy keeps the same stem.
        warehouse_copy = workspace / WAREHOUSE.name
        shutil.copy2(WAREHOUSE, warehouse_copy)
        os.environ["TOOROW_DB_MODE"] = "duckdb"
        os.environ["TOOROW_DUCKDB_PATH"] = str(warehouse_copy)

        dsn = _dsn()
        os.environ["PLATFORM_DB_URL"] = dsn
        conn = psycopg.connect(dsn)
        try:
            chain = seed_governed_chain(conn)
            executed = execute_query_spec(conn, chain)
            spec_version_id = create_presentation(conn, chain, executed)
            conn.commit()
            # AFTER the commit, because the tool opens its own connection: a
            # decision read inside this uncommitted transaction would be a
            # different question from the one the tool asks.
            assert_capture_identity_can_view(
                conn, project_id=chain["project_id"], identity=chain["identity"]
            )
        finally:
            conn.close()

        with _capture_seams(chain["identity"]):
            wire = asyncio.run(
                _call_render_tool(
                    project_id=chain["project_id"],
                    result_id=executed["result_id"],
                    visualization_spec_version_id=spec_version_id,
                )
            )
        traced = {
            "relation": RELATION,
            "project_id": chain["project_id"],
            "semantic_view_version_id": chain["view_version_id"],
            "query_spec_version_id": executed["query_spec_version_id"],
            "result_id": executed["result_id"],
            "content_hash": executed["content_hash"],
            "row_count": executed["row_count"],
            "ai_path": executed["ai_path"],
            "visualization_spec_version_id": spec_version_id,
        }
        return wire, traced
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def fold_run_identities(value: Any) -> Any:
    """Fold every identity a fresh run necessarily mints anew into its SHAPE.

    Used by `--check` only. A ULID id becomes `<prefix>_ULID`, a 64-hex content
    hash becomes `<hash64>`, an ISO instant becomes `<timestamp>`. What survives is
    the data: rows, schema names, the spec document, the pins and the answer text.
    """
    if isinstance(value, dict):
        return {key: fold_run_identities(item) for key, item in value.items()}
    if isinstance(value, list):
        return [fold_run_identities(item) for item in value]
    if isinstance(value, str):
        folded = _ULID_ID.sub(lambda match: f"{match.group(1)}_ULID", value)
        folded = _HASH64.sub("<hash64>", folded)
        return _TIMESTAMP.sub("<timestamp>", folded)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    wire, traced = build_fixture()
    rendered = json.dumps(wire, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not FIXTURE_PATH.exists():
            print("error: the fixture has never been captured")
            return 1
        committed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        if fold_run_identities(committed) != fold_run_identities(wire):
            print("error: the capture no longer reproduces the committed fixture")
            return 1
        print("ok: the capture reproduces the committed fixture up to run identity")
        return 0

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(rendered, encoding="utf-8")
    render_input = wire["_meta"]["toorow.app_payload"]["render_input"]
    print(f"wrote {FIXTURE_PATH} ({len(rendered.encode('utf-8'))} bytes)")
    for key, value in traced.items():
        print(f"{key}: {value}")
    print(f"rows: {json.dumps(render_input['result']['rows'], ensure_ascii=False)}")
    print(f"pins: {json.dumps(render_input['pins'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
