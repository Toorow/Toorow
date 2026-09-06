"""The world an epic-66 Postgres test needs, built once.

Four stories of this epic (66.1, 66.2, 66.3, 66.4) each need the same chain --
an org, a project, canonical fields, a published Datastream mapping that binds
them, a published Semantic View version and a relationship that pins a common key
version. Rebuilding it per file is how two fixtures start disagreeing about what
"published" means, and the disagreement is invisible until one of them is wrong.

Every builder writes inside the caller's transaction; `live_postgres` rolls back.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from ulid import ULID


def uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def hash64() -> str:
    """A distinct 64-hex value -- the shape every `*_hash` CHECK of this schema wants."""
    return hashlib.sha256(str(ULID()).encode("utf-8")).hexdigest()


def make_project(conn, label: str = "Epic 66") -> tuple[str, str]:
    org_id, project_id = uid("org"), uid("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, label, org_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, label, project_id.lower(), "tester"),
        )
    return org_id, project_id


def make_canonical_field(
    conn, project_id: str, name: str, *, kind: str = "dimension", value_type: str = "string"
) -> str:
    """One project-scoped canonical field.

    Minted rather than assumed: `mdm_canonical_fields_api.py:24` records that the
    table held ZERO rows at both scopes on 2026-08-08.
    """
    field_id = f"mdm_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_canonical_fields
                (id, project_id, concept_kind, canonical_name, value_type, aggregation,
                 non_additive, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                field_id,
                project_id,
                kind,
                name,
                value_type,
                "sum" if kind == "metric" else None,
                False,
                "tester",
            ),
        )
    return field_id


def make_datastream(
    conn,
    org_id: str,
    project_id: str,
    name: str,
    *,
    bindings: Mapping[str, tuple[str, ...]] | None = None,
    measures: Mapping[str, str] | None = None,
    unbound_fields: tuple[str, ...] = (),
    grain: Sequence[str] | None = None,
) -> str:
    """A Datastream, optionally publishing a mapping version.

    `grain` writes the mapping's declared grain into the payload. It is a
    parameter and not a post-hoc UPDATE because
    `app.reject_datastream_mapping_version_mutation` makes a published mapping
    version IMMUTABLE -- a caller that published first and set the grain after
    got `datastream mapping versions are immutable`, measured 2026-08-17.

    `bindings` maps canonical field id -> (physical field id, binding status) or
    (physical field id, binding status, physical type).
    `measures` maps physical field id -> binding status, and marks them as measures
    so a match can count what the cross unlocks. `unbound_fields` are physical
    columns bound to nothing -- the raw material of a same-name candidate.
    """
    datastream_id = uid("ds")
    with conn.cursor() as cur:
        # `source_kind` is stated even though the column is nullable, and it has to
        # be. `app.sync_external_dispatch_excluded` is a BEFORE INSERT trigger that
        # assigns `external_dispatch_excluded` from it; migration 226 made a NULL
        # kind fold to FALSE, but a base whose installed body predates that fix
        # assigns NULL and the NOT NULL rejects the row -- with an error naming a
        # column no caller here ever touched. Measured 2026-08-17 on the disposable
        # cluster: the installed body was the pre-226 one, and every insert of this
        # helper failed until the kind was passed. `server/tests/core/conftest.py`
        # carries the same note for the same reason. No epic-66 producer reads
        # `source_kind` (grep over matches/plan/profile/execution: zero hits), so
        # stating it changes no measurement -- it only stops the fixture depending
        # on which trigger body a base happens to carry.
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, org_id, project_id, name, created_by, source_kind) "
            "VALUES (%s,%s,%s,%s,%s,'managed_feed')",
            (datastream_id, org_id, project_id, name, "tester"),
        )
    if bindings is None and measures is None and not unbound_fields and not grain:
        return datastream_id

    target_roles: dict[str, str] = {}
    target_ids = list((bindings or {}).keys())
    if target_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, concept_kind FROM app.mdm_canonical_fields "
                "WHERE project_id = %s AND id = ANY(%s)",
                (project_id, target_ids),
            )
            target_roles = {str(row[0]): str(row[1]) for row in cur.fetchall()}

    fields: list[dict[str, Any]] = []
    for target, binding in (bindings or {}).items():
        # `(physical, status)` or `(physical, status, physical_type)`. The third
        # element exists because the plan compiler now compares the two columns
        # implementing one key component: a fixture where every dimension is a
        # `string` can never exercise a type disagreement.
        physical, status = binding[0], binding[1]
        role = "measure" if target_roles.get(target) == "metric" else "dimension"
        declared_type = binding[2] if len(binding) > 2 else None
        fields.append(
            {
                "field_id": physical,
                "physical_type": declared_type
                or ("number" if role == "measure" else "string"),
                "suggestion": {
                    "semantic_role": role,
                    **({"aggregation": "sum"} if role == "metric" else {}),
                },
                "binding": {"mdm_target": target, "status": status},
            }
        )
    for physical, status in (measures or {}).items():
        fields.append(
            {
                "field_id": physical,
                "physical_type": "number",
                "suggestion": {"semantic_role": "measure", "aggregation": "sum"},
                "binding": {"mdm_target": None, "status": status},
            }
        )
    for physical in unbound_fields:
        fields.append(
            {
                "field_id": physical,
                "physical_type": "string",
                "suggestion": {"semantic_role": "dimension"},
                "binding": {"mdm_target": None, "status": "suggested"},
            }
        )

    plan_id, mapping_id = uid("dpv"), uid("dmv")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'plan.v1','connector_pull','toorow','managed_raw',
                    '{}'::jsonb,%s,%s,'tester')
            """,
            (plan_id, datastream_id, project_id, hash64(), hash64()),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'mapping.v1',%s,%s,%s,'0.1.1','2',%s::jsonb,'{}'::jsonb,%s,'tester')
            """,
            (
                mapping_id,
                datastream_id,
                project_id,
                hash64(),
                plan_id,
                hash64(),
                json.dumps(
                    {"fields": fields, **({"grain": list(grain)} if grain else {})}
                ),
                hash64(),
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (mapping_id, datastream_id),
        )
    return datastream_id


def publish_output(
    conn,
    org_id: str,
    project_id: str,
    datastream_id: str,
    relation: str,
) -> str:
    """The execution -> output -> output version chain that names a real relation.

    `match_profile.resolve_side` walks exactly this chain, and a break anywhere in
    it is one of the story's named refusals. Building it here means those refusals
    are proven by REMOVING a link rather than by mocking its absence.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_plan_version_id, current_mapping_version_id "
            "FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        plan_id, mapping_id = cur.fetchone()
        if plan_id is None:
            cur.execute(
                "SELECT id FROM app.datastream_plan_versions WHERE datastream_id = %s "
                "ORDER BY version_number DESC LIMIT 1",
                (datastream_id,),
            )
            plan_id = cur.fetchone()[0]

    execution_id, output_id, version_id = f"dse_{ULID()}", f"dso_{ULID()}", f"dsov_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, created_by)
            VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'published','tester')
            """,
            (execution_id, datastream_id, project_id, plan_id, mapping_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_outputs
                (id, org_id, project_id, datastream_id, output_kind, stable_name, created_by)
            VALUES (%s,%s,%s,%s,'full_grain',%s,'tester')
            """,
            (output_id, org_id, project_id, datastream_id, relation),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_output_versions
                (id, output_id, org_id, project_id, datastream_id, execution_id,
                 plan_version_id, mapping_version_id, relation_ref, grain_evidence,
                 evidence, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,'tester')
            """,
            (
                version_id,
                output_id,
                org_id,
                project_id,
                datastream_id,
                execution_id,
                plan_id,
                mapping_id,
                relation,
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = %s WHERE id = %s",
            (execution_id, datastream_id),
        )
    return version_id


def make_semantic_view(
    conn,
    project_id: str,
    *,
    name: str = "epic_66_view",
    status: str = "published",
    business_domain_refs: tuple[str, ...] = (),
) -> tuple[str, str]:
    """One Semantic View and one version in the requested lifecycle state.

    `status` is a parameter and not a constant because "published" is exactly what
    makes a relationship executable: a test that could only build published
    versions could never prove that a draft one grants nothing.
    """
    view_id, version_id = f"sv_{ULID()}", f"svv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s,%s,%s,%s)",
            (view_id, project_id, name, "tester"),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, business_domain_refs, created_by)
            VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s::jsonb,'tester')
            """,
            (
                version_id,
                view_id,
                project_id,
                status,
                name,
                "Epic 66 view",
                hash64(),
                hash64(),
                json.dumps(list(business_domain_refs)),
            ),
        )
    return view_id, version_id


def pin_relationship(
    conn,
    view_version_id: str,
    key_version_id: str,
    *,
    ordinal: int = 0,
    name: str = "left_to_right",
    cardinality: str = "many_to_one",
    fan_out_policy: str = "forbid",
    bridge_dataset: str | None = None,
    left_datastream_id: str | None = None,
    right_datastream_id: str | None = None,
) -> None:
    """One relationship of a view version, pinning an exact common key version."""
    if left_datastream_id is None or right_datastream_id is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_id, components FROM app.mdm_common_key_versions WHERE id = %s",
                (key_version_id,),
            )
            key_row = cur.fetchone()
            if key_row is None:
                raise AssertionError("fixture common key version missing")
            component_ids = {
                str(component.get("canonical_field_id") or "")
                for component in (key_row[1] or [])
                if isinstance(component, dict)
            }
            cur.execute(
                "SELECT d.id, m.mapping_payload FROM app.datastreams d "
                "JOIN app.datastream_mapping_versions m ON m.id = d.current_mapping_version_id "
                "WHERE d.project_id = %s AND d.archived_at IS NULL ORDER BY d.id",
                (key_row[0],),
            )
            candidates = []
            for datastream_id, payload in cur.fetchall():
                implemented = {
                    str((field.get("binding") or {}).get("mdm_target") or "")
                    for field in ((payload or {}).get("fields") or [])
                    if isinstance(field, dict)
                    and (field.get("binding") or {}).get("status") in {"confirmed", "resolved"}
                }
                if component_ids and component_ids <= implemented:
                    candidates.append(str(datastream_id))
        if len(candidates) != 2:
            raise AssertionError(
                "pin_relationship fixture must name endpoints when a key is implemented by "
                f"{len(candidates)} Datastreams"
            )
        left_datastream_id, right_datastream_id = candidates
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_view_version_relationships
                (view_version_id, ordinal, name, from_dataset, to_dataset, from_columns,
                 to_columns, cardinality_type, fan_out_policy, bridge_dataset,
                 mdm_common_key_version_id, left_datastream_id, right_datastream_id)
            VALUES (%s,%s,%s,'left','right',ARRAY['a'],ARRAY['b'],%s,%s,%s,%s,%s,%s)
            """,
            (
                view_version_id,
                ordinal,
                name,
                cardinality,
                fan_out_policy,
                bridge_dataset,
                key_version_id,
                left_datastream_id,
                right_datastream_id,
            ),
        )
