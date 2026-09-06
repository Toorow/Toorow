"""Generate branch parity from a real search, recorder, RLS and HTTP read."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import psycopg
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from jsonschema import Draft202012Validator
from starlette.applications import Starlette
from starlette.testclient import TestClient
from ulid import ULID


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _seed(dsn: str, *, issuer: str) -> dict[str, str]:
    """Seed two isolated tenants, each rooted on a REAL canonical person.

    `identity_a` / `identity_b` are the token SUBJECTS; `person_a` / `person_b`
    are what those subjects authenticate to. They are not interchangeable, and
    the fixture used to be able to ignore the difference only because the
    identity flag it set to "0" kept `app.persons` empty. With the flag removed
    (2026-08-24) the first authenticated call mints the person itself -- so the
    person is minted HERE instead, before anything runs, and membership and
    `ai_paths.actor` are keyed on it exactly as the server keys them.
    """
    values = {
        "org_a": _uid("org"),
        "org_b": _uid("org"),
        "project_a": _uid("proj"),
        "project_b": _uid("proj"),
        "identity_a": f"story-65-7-a-{ULID()}@example.com",
        "identity_b": f"story-65-7-b-{ULID()}@example.com",
    }
    topic_run = str(ULID())
    with psycopg.connect(dsn) as conn:
        from core.canonical_identity import resolve_canonical_identity

        for suffix in ("a", "b"):
            values[f"person_{suffix}"] = resolve_canonical_identity(
                conn, issuer=issuer, subject=values[f"identity_{suffix}"]
            ).person_id
        conn.commit()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for suffix in ("a", "b"):
            org_id = values[f"org_{suffix}"]
            project_id = values[f"project_{suffix}"]
            identity = values[f"identity_{suffix}"]
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, 'active', %s)",
                (org_id, f"Branch fixture {suffix}", org_id.lower(), identity),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, %s, 'active', %s)",
                (project_id, org_id, f"Branch fixture {suffix}", project_id.lower(), identity),
            )
            cur.execute(
                "INSERT INTO app.org_members "
                "(id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (_uid("omem"), org_id, values[f"person_{suffix}"]),
            )
        for index in range(1, 22):
            cur.execute(
                "INSERT INTO app.context_topics "
                "(id, project_id, title, body_md, status, created_by) "
                "VALUES (%s, %s, %s, %s, 'active', %s)",
                (
                    f"top_{topic_run}_{index:02d}",
                    values["project_a"],
                    f"Fixture Branch Alpha {index}",
                    "Fixture Branch governed context",
                    values["identity_a"],
                ),
            )
        cur.execute(
            "INSERT INTO app.context_topics "
            "(id, project_id, title, body_md, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (
                f"top_{topic_run}_foreign",
                values["project_b"],
                "FOREIGN-SECRET Fixture Branch",
                "FOREIGN-SECRET must never cross",
                values["identity_b"],
            ),
        )
        conn.commit()
    return values


def _runtime_role(dsn: str, identity: str) -> dict[str, bool]:
    from core.db import request_connection

    with request_connection(identity) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolsuper, r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user"
        )
        superuser, bypass_rls = cur.fetchone()
        cur.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid IN ('app.ai_paths'::regclass, 'app.ai_path_steps'::regclass)"
        )
        relations = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    return {
        "superuser": bool(superuser),
        "bypass_rls": bool(bypass_rls),
        "paths_rls": bool(relations["ai_paths"][0]),
        "paths_force_rls": bool(relations["ai_paths"][1]),
        "steps_rls": bool(relations["ai_path_steps"][0]),
        "steps_force_rls": bool(relations["ai_path_steps"][1]),
    }


async def _call_search(mcp_app, bearer: str, project_id: str):
    def client_factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp_app),
            base_url="http://story-65-7.test",
            **kwargs,
        )

    transport = StreamableHttpTransport(
        "http://story-65-7.test/mcp",
        auth=bearer,
        httpx_client_factory=client_factory,
    )
    async with Client(transport) as client:
        return await client.call_tool(
            "search_context", {"query": "Fixture Branch", "project_id": project_id}
        )


async def _invalid_mcp_is_refused(mcp_app, project_id: str) -> bool:
    try:
        await _call_search(mcp_app, "not-a-signed-jwt", project_id)
    except Exception:  # noqa: BLE001 -- the transport may expose 401 in several wrappers
        return True
    return False


def _tool_payload(value: Any) -> bytes:
    return _canonical(
        {
            "is_error": bool(value.is_error),
            "content": [item.model_dump(mode="json", by_alias=True) for item in value.content],
            "structured_content": value.structured_content,
        }
    )


def _branch_values(projection: dict[str, Any], *, observed: bool) -> list[Any]:
    ordinal_key = "ordinal" if observed else "step_order"
    return [
        step["branch_evidence"]
        for step in sorted(projection["steps"], key=lambda item: item[ordinal_key])
        if "branch_evidence" in step
    ]


def _independent_content_hash(path: dict[str, Any]) -> str:
    """Rebuild ai-path-content.v2 without calling either production helper."""
    steps = []
    for step in path["steps"]:
        steps.append(
            {
                "ordinal": step["ordinal"],
                "step_kind": step["step_kind"],
                "owner": [
                    step["owner_workspace"],
                    step["owner_object_type"],
                    step["owner_object_id"],
                    step["owner_version_id"],
                ],
                "skill": [step["skill_version_id"], step["skill_step_id"]],
                "tool_name": step["tool_name"],
                "outcome": step["outcome"],
                "detail": step.get("detail"),
            }
        )
    preimage = {
        "path_id": path["id"],
        "outcome": path["outcome"],
        "policy_snapshot_hash": path["policy_snapshot_hash"],
        "steps": steps,
        "schema_version": "ai-path-content.v2",
    }
    return hashlib.sha256(_canonical(preimage)).hexdigest()


def _validate_branch_evidence(*projections: dict[str, Any]) -> tuple[bool, bool]:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "core/schemas/retrieval-branch-evidence.schema.json"
    )
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    forbidden = {
        "candidate_ids",
        "candidate_kinds",
        "candidate_titles",
        "candidate_scores",
        "candidate_tiers",
        "candidate_matched",
        "candidate_ranks",
        "candidate_fates",
        "candidate_reasons",
        "query",
        "tokens",
        "snippet",
        "arguments",
        "actor",
        "project",
        "trace",
        "error",
        "body",
        "reasoning",
    }

    def keys(value: Any):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    evidence_values = []
    for projection in projections:
        evidence_values.extend(
            _branch_values(
                projection,
                observed=projection.get("schema_version") == "observed-ai-path.v1",
            )
        )
    for evidence in evidence_values:
        validator.validate(evidence)
    forbidden_absent = all(
        not (set(keys(value)) & forbidden) for value in evidence_values
    )
    return bool(evidence_values), forbidden_absent


def _normalize(api_detail: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    api = json.loads(_canonical(api_detail))
    obs = json.loads(_canonical(observed))
    api.update(
        {
            "id": "aip_FIXTURE",
            "actor": "fixture-owner@example.com",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:01+00:00",
            "policy_snapshot_hash": "0" * 64,
            "content_hash": "1" * 64,
        }
    )
    obs["path_id"] = "aip_FIXTURE"
    for step in obs["steps"]:
        step["observed_at"] = "2026-01-01T00:00:00+00:00"

    replacements: dict[str, str] = {}
    counter = 0
    for projection in (api, obs):
        for evidence in _branch_values(projection, observed=projection is obs):
            for branch in evidence.get("branches", []):
                old = branch["id"]
                if old not in replacements:
                    counter += 1
                    replacements[old] = f"top_FIXTURE_{counter:02d}"
                branch["id"] = replacements[old]
    for step in api["steps"]:
        owner_id = step.get("owner_object_id")
        if owner_id in replacements:
            step["owner_object_id"] = replacements[owner_id]
    for step in obs["steps"]:
        owner = step.get("owner")
        if isinstance(owner, dict) and owner.get("object_id") in replacements:
            owner["object_id"] = replacements[owner["object_id"]]
    return {
        "schema_version": "ai-path-branch-parity.v1",
        "api_detail": api,
        "observed_projection": obs,
    }


async def run_scenario(dsn: str) -> dict[str, Any]:
    """Exercise the real producer/readers and return auditable proof facts."""
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    key_pair = RSAKeyPair.generate()
    oauth_env = {
        "TOOROW_AUTH_MODE": "oauth",
        "TOOROW_JWT_PUBLIC_KEY": key_pair.public_key,
        "TOOROW_JWKS_URI": "",
        "TOOROW_JWT_AUDIENCE": "story-65-7-fixture",
        "TOOROW_JWT_ISSUER": "https://story-65-7.test",
        "TOOROW_FEEDBACK_CONTEXT_SECRET": "story-65-7-fixture-secret-32-bytes",
        "PLATFORM_DB_URL": dsn,
    }
    # core.main constructs the real FastMCP auth provider at import time.  The
    # fixture must therefore establish a complete OAuth trust configuration
    # before that import, even when the invoking shell only set AUTH_MODE.
    with patch.dict(os.environ, oauth_env):
        from core import ai_path_recorder, ai_paths, api_auth, main
        from core.ai_paths_api import AI_PATH_ROUTES
        from core.auth_config import build_auth_provider
        from core.db import request_connection

    issuer = oauth_env["TOOROW_JWT_ISSUER"]
    seeded = _seed(dsn, issuer=issuer)
    audience = oauth_env["TOOROW_JWT_AUDIENCE"]
    token_a = key_pair.create_token(
        subject=seeded["identity_a"], issuer=issuer, audience=audience
    )
    token_b = key_pair.create_token(
        subject=seeded["identity_b"], issuer=issuer, audience=audience
    )

    with patch.dict(os.environ, oauth_env):
        api_auth.reset_verifier_cache()
        target = FastMCP(
            "story-65-7-branch-evidence", auth=build_auth_provider()
        )
        middleware = ai_path_recorder.build_middleware()
        assert middleware is not None
        target.add_middleware(middleware)
        target.tool(main.search_context)
        mcp_app = target.http_app(path="/mcp", stateless_http=True)
        api_app = Starlette(routes=AI_PATH_ROUTES)

        async with mcp_app.router.lifespan_context(mcp_app):
            invalid_mcp_refused = await _invalid_mcp_is_refused(
                mcp_app, seeded["project_a"]
            )
            result = await _call_search(mcp_app, token_a, seeded["project_a"])

            with request_connection(seeded["identity_a"]) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.ai_paths WHERE project_id = %s AND actor = %s "
                    "ORDER BY started_at DESC LIMIT 1",
                    (seeded["project_a"], seeded["person_a"]),
                )
                path_id = str(cur.fetchone()[0])

            with TestClient(api_app) as client:
                invalid_http = client.get(
                    f"/api/projects/{seeded['project_a']}/context/ai-paths",
                    headers={"Authorization": "Bearer not-a-signed-jwt"},
                )
                list_a = client.get(
                    f"/api/projects/{seeded['project_a']}/context/ai-paths",
                    headers={"Authorization": f"Bearer {token_a}"},
                )
                detail_a = client.get(
                    f"/api/projects/{seeded['project_a']}/context/ai-paths/{path_id}",
                    headers={"Authorization": f"Bearer {token_a}"},
                )
                list_b = client.get(
                    f"/api/projects/{seeded['project_b']}/context/ai-paths",
                    headers={"Authorization": f"Bearer {token_b}"},
                )
                foreign = client.get(
                    f"/api/projects/{seeded['project_b']}/context/ai-paths/{path_id}",
                    headers={"Authorization": f"Bearer {token_b}"},
                )
                missing = client.get(
                    f"/api/projects/{seeded['project_b']}/context/ai-paths/"
                    "aip_00000000000000000000000000",
                    headers={"Authorization": f"Bearer {token_b}"},
                )

            api_detail = detail_a.json()
            with request_connection(seeded["identity_a"]) as conn:
                loaded = ai_paths.load_path(
                    conn, path_id=path_id, project_id=seeded["project_a"]
                )
                observed = ai_paths.project_observed_ai_path(
                    conn, project_id=seeded["project_a"], ai_path=path_id
                )
                recomputed = _independent_content_hash(loaded)
                original_detail = _canonical(loaded["steps"][0]["detail"])
                refused = False
                try:
                    with conn.transaction():
                        conn.execute(
                            "UPDATE app.ai_path_steps SET detail = '{}'::jsonb "
                            "WHERE path_id = %s AND ordinal = 0",
                            (path_id,),
                        )
                except psycopg.Error:
                    refused = True
                reread = ai_paths.load_path(
                    conn, path_id=path_id, project_id=seeded["project_a"]
                )
                unchanged = _canonical(reread["steps"][0]["detail"]) == original_detail
                count_before = conn.execute(
                    "SELECT COUNT(*) FROM app.ai_paths "
                    "WHERE project_id = %s AND actor = %s",
                    (seeded["project_a"], seeded["person_a"]),
                ).fetchone()[0]

            original_append = ai_paths.append_step

            def fail_branch_append(conn, **kwargs):
                if kwargs.get("step_kind") == "knowledge_read":
                    raise RuntimeError("fixture rollback tripwire")
                return original_append(conn, **kwargs)

            with patch.object(ai_paths, "append_step", side_effect=fail_branch_append):
                rollback_result = await _call_search(
                    mcp_app, token_a, seeded["project_a"]
                )
            with request_connection(seeded["identity_a"]) as conn:
                count_after = conn.execute(
                    "SELECT COUNT(*) FROM app.ai_paths "
                    "WHERE project_id = %s AND actor = %s",
                    (seeded["project_a"], seeded["person_a"]),
                ).fetchone()[0]
            runtime_role = _runtime_role(dsn, seeded["identity_a"])

    api_branches = _branch_values(api_detail, observed=False)
    observed_branches = _branch_values(observed, observed=True)
    every_branch_schema_valid, forbidden_absent = _validate_branch_evidence(
        api_detail, observed
    )
    list_a_ids = [item["id"] for item in list_a.json().get("paths", [])]
    list_b_ids = [item["id"] for item in list_b.json().get("paths", [])]
    nondisclosing_terms = {
        value
        for evidence in api_branches
        for branch in evidence.get("branches", [])
        for value in (branch.get("title"), branch.get("reason"))
        if isinstance(value, str) and value
    }
    refusal_text = foreign.text + missing.text
    fixture = _normalize(api_detail, observed)
    return {
        "mcp_succeeded": not bool(result.is_error),
        "mcp_invalid_token_refused": invalid_mcp_refused,
        "http_invalid_token_refused": invalid_http.status_code == 401,
        "runtime_role": runtime_role,
        "own_list_status": list_a.status_code,
        "owner_list_discovers_exact_path": list_a_ids == [path_id],
        "foreign_list_is_exactly_empty": list_b.status_code == 200 and list_b_ids == [],
        "own_detail_status": detail_a.status_code,
        "api_detail": api_detail,
        "observed_projection": observed,
        "branch_bytes_equal": _canonical(api_branches) == _canonical(observed_branches),
        "foreign_matches_missing": (
            foreign.status_code == missing.status_code
            and foreign.content == missing.content
            and foreign.headers.get("cache-control") == missing.headers.get("cache-control")
        ),
        "foreign_and_missing_are_exact_nondisclosing_404s": (
            foreign.status_code == missing.status_code == 404
            and all(term not in refusal_text for term in nondisclosing_terms)
        ),
        "foreign_secret_absent": "FOREIGN-SECRET" not in _canonical(api_detail).decode(),
        "content_hash_recomputed": recomputed == loaded["content_hash"],
        "content_hash_recomputed_independently": recomputed == loaded["content_hash"],
        "append_only_refused": refused,
        "stored_detail_unchanged": unchanged,
        "rollback_left_no_path": (
            not bool(rollback_result.is_error) and count_before == count_after
        ),
        "rollback_payload_byte_equal": _tool_payload(result) == _tool_payload(rollback_result),
        "every_branch_schema_valid": every_branch_schema_valid,
        "branch_forbidden_fields_absent": forbidden_absent,
        "fixture": fixture,
    }


async def build_fixture(dsn: str) -> dict[str, Any]:
    """Return only normalized public projections; proofs remain test-only."""
    return (await run_scenario(dsn))["fixture"]
