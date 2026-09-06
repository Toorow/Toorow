"""Real-Postgres producer for the Story 65.8 admin feedback-review fixture."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from core.feedback_review import (
    aggregate_feedback,
    append_review_version,
    get_annotation,
    list_annotations,
    list_unresolved_critical_negatives,
)
from ulid import ULID

from tests.fixture_generators.analyze_feedback import build_fixture as build_delivery_fixture

SCHEMA_VERSION = "feedback-review-fixture.v1"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_ID_PREFIX = re.compile(
    r"^(?:org|proj|qr|qs|qsv|sv|svv|rnd|vsv|aip|fba|rsfb|fbrv|afi|bd|gq|gqv|"
    r"erp|ecvs|erun|ecase|edv|pcv)_[A-Za-z0-9_-]+$"
)
_DOMAIN_VERSION_ID = re.compile(r"^(bd_[A-Za-z0-9_-]+):([1-9][0-9]*)$")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _seed_automated_verdict(
    conn, *, org_id: str, project_id: str, share_feedback_id: str
) -> None:
    """Create one real, separately-owned Evaluation verdict matching the Share tuple."""
    from core.golden_questions import (  # noqa: PLC0415
        create_golden_question,
        validate_golden_question_version,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.result_id, f.render_id, f.ai_path_id,
                   q.query_spec_version_id, qsv.semantic_view_id,
                   qsv.semantic_view_version_id, e.classification_hash,
                   e.classification
              FROM app.render_share_feedback f
              JOIN app.query_results q
                ON q.id = f.result_id AND q.org_id = f.org_id
               AND q.project_id = f.project_id
              JOIN app.query_spec_versions qsv
                ON qsv.id = q.query_spec_version_id AND qsv.org_id = q.org_id
               AND qsv.project_id = q.project_id
              JOIN app.feedback_eligible_observations e
                ON e.org_id = f.org_id AND e.project_id = f.project_id
               AND e.source = 'anonymous_share'
               AND e.interaction_ref = f.interaction_ref
             WHERE f.id = %s AND f.org_id = %s AND f.project_id = %s
            """,
            (share_feedback_id, org_id, project_id),
        )
        owner = cur.fetchone()
    if owner is None:
        raise AssertionError("Share feedback has no exact classification owner")
    (
        result_id,
        render_id,
        ai_path_id,
        _query_spec,
        view_id,
        view_version_id,
        class_hash,
        classification,
    ) = owner
    domain_versions = classification.get("business_domains", {}).get("versions", [])
    capability = classification.get("capability", {})
    result_type = classification.get("result_type", {})
    if (
        len(domain_versions) != 1
        or capability.get("state") != "attributed"
        or result_type.get("state") != "attributed"
    ):
        raise AssertionError("Result classification must own the Evaluation case axes")
    domain_id = str(domain_versions[0]["id"])
    domain_version = int(domain_versions[0]["version_number"])
    capability_key = str(capability["key"])
    classified_result_type = str(result_type["value"])
    validated = validate_golden_question_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        payload={
            "business_domain_id": domain_id,
            "business_domain_version_number": domain_version,
            "semantic_view_id": str(view_id),
            "semantic_view_version_id": str(view_version_id),
            "semantic_view_version_role": "baseline",
            "question": "Does this exact delivered path satisfy the expected behavior?",
            "time_boundary": {},
            "expected_result": [
                {"assertion_type": "value", "member_id": "sessions", "tolerance": None}
            ],
            "required_provenance": [{"link_kind": "semantic_view", "required": True}],
            "expected_ai_path": {
                "grammar_version": 1,
                "required_nodes": [],
            },
            "result_type": classified_result_type,
            "capability_tags": [capability_key],
            "severity": "critical",
        },
    )
    golden = create_golden_question(
        conn,
        org_id=org_id,
        project_id=project_id,
        title="Feedback fixture question",
        owner="owner@example.com",
        validated=validated,
        actor="owner@example.com",
    )
    profile_id, context_id, run_id, case_id = (
        _uid("erp"),
        _uid("ecvs"),
        _uid("erun"),
        _uid("ecase"),
    )
    digest = hashlib.sha256(b"feedback-review-evaluation-fixture").hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_run_profiles
                (id, org_id, project_id, name, evidence_mode, created_by)
            VALUES (%s, %s, %s, %s, 'offline', 'fixture')
            """,
            (profile_id, org_id, project_id, f"feedback-fixture-{profile_id[-8:]}"),
        )
        cur.execute(
            """
            INSERT INTO app.evaluation_context_version_sets
                (id, org_id, project_id, content_hash)
            VALUES (%s, %s, %s, %s)
            """,
            (context_id, org_id, project_id, digest),
        )
        cur.execute(
            """
            INSERT INTO app.evaluation_runs
                (id, org_id, project_id, run_profile_id, evidence_mode,
                 semantic_view_id, semantic_view_version_id, context_version_set_id,
                 model_ref, tool_catalog_version, data_snapshot_hash, as_of, created_by)
            VALUES (%s, %s, %s, %s, 'offline', %s, %s, %s,
                    'fixture-model', %s, %s, CURRENT_DATE, 'fixture')
            """,
            (
                run_id,
                org_id,
                project_id,
                profile_id,
                view_id,
                view_version_id,
                context_id,
                digest,
                digest,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.evaluation_run_cases
                (id, run_id, org_id, project_id, golden_question_version_id,
                 result_id, render_ref, ai_path_id, business_domain_id,
                 business_domain_version_number, capability_key, result_type,
                 result_classification_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
            """,
            (
                case_id,
                run_id,
                org_id,
                project_id,
                golden["version_id"],
                result_id,
                render_id,
                ai_path_id,
                domain_id,
                domain_version,
                capability_key,
                classified_result_type,
                class_hash,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.evaluation_case_dimension_verdicts
                (id, case_id, org_id, project_id, dimension, verdict,
                 reason_code, evidence_refs)
            VALUES (%s, %s, %s, %s, 'path_quality', 'fail',
                    'expected_path_mismatch', %s::jsonb),
                   (%s, %s, %s, %s, 'semantic_correctness', 'pass',
                    'expected_value_matched', %s::jsonb)
            """,
            (
                _uid("edv"),
                case_id,
                org_id,
                project_id,
                json.dumps({"result_id": result_id, "render_id": render_id}),
                _uid("edv"),
                case_id,
                org_id,
                project_id,
                json.dumps({"result_id": result_id, "render_id": render_id}),
            ),
        )


def _review_command() -> dict[str, Any]:
    return {
        "schema_version": "feedback-review-command.v1",
        "expected_head": None,
        "retry_key": "fixture-share-review-retry",
        "state": "triaged",
        "affected_dimension": "path_quality",
        "human_verdict": "fail",
        "severity": "critical",
        "reason": "The delivered path step needs an exact owner review.",
    }


class _Normalizer:
    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.hashes: dict[str, str] = {}
        self.interactions: dict[str, str] = {}

    def value(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {name: self.value(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self.value(item, key) for item in value]
        if not isinstance(value, str):
            return value
        if key and (key.endswith("_at") or key in {"observed_from", "observed_to"}):
            return "2026-08-10T12:00:00Z"
        if key in {"actor", "reviewer"} and "@" in value:
            return "owner@example.com"
        domain_version = _DOMAIN_VERSION_ID.fullmatch(value)
        if domain_version:
            return (
                f"{self.value(domain_version.group(1), 'object_id')}:"
                f"{domain_version.group(2)}"
            )
        if value.startswith("afi_"):
            return self.interactions.setdefault(
                value, f"afi_FIXTURE_{len(self.interactions) + 1}"
            )
        if _HEX_64.fullmatch(value):
            return self.hashes.setdefault(
                value,
                hashlib.sha256(f"fixture-hash-{len(self.hashes) + 1}".encode()).hexdigest(),
            )
        if _ID_PREFIX.fullmatch(value):
            prefix = value.split("_", 1)[0]
            return self.ids.setdefault(value, f"{prefix}_FIXTURE_{len(self.ids) + 1}")
        return value


async def build_fixture(dsn: str) -> dict[str, Any]:
    """Run real delivery doors, then consume the exact review service projections."""
    await build_delivery_fixture(dsn)
    now = datetime.now(timezone.utc)
    observed_from = (now - timedelta(days=1)).isoformat()
    observed_to = (now + timedelta(days=1)).isoformat()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT org_id, project_id
                  FROM app.feedback_annotations
                 WHERE actor LIKE 'story-65-5-golden-%@example.com'
                 ORDER BY observed_at DESC LIMIT 1
                """
            )
            scope = cur.fetchone()
        if scope is None:
            raise AssertionError("real feedback producer emitted no authenticated annotation")
        org_id, project_id = map(str, scope)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.feedback_annotations
                 WHERE org_id = %s AND project_id = %s AND target_kind = 'datum'
                 ORDER BY observed_at DESC LIMIT 1
                """,
                (org_id, project_id),
            )
            authenticated = cur.fetchone()
            cur.execute(
                """
                SELECT id FROM app.render_share_feedback
                 WHERE org_id = %s AND project_id = %s
                 ORDER BY submitted_at DESC LIMIT 1
                """,
                (org_id, project_id),
            )
            anonymous = cur.fetchone()
        if authenticated is None or anonymous is None:
            raise AssertionError("producer must emit authenticated datum and anonymous Share")
        authenticated_id, anonymous_id = str(authenticated[0]), str(anonymous[0])
        _seed_automated_verdict(
            conn,
            org_id=org_id,
            project_id=project_id,
            share_feedback_id=anonymous_id,
        )
        command = _review_command()
        receipt = append_review_version(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=anonymous_id,
            reviewer="owner@example.com",
            payload=command,
        )
        conn.commit()
        filters = {"observed_from": observed_from, "observed_to": observed_to}
        collection = list_annotations(
            conn,
            org_id=org_id,
            project_id=project_id,
            filters=filters,
            limit=50,
        )
        authenticated_detail = get_annotation(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=authenticated_id,
        )
        share_detail = get_annotation(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=anonymous_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.classification_hash,
                       e.classification->>'classification_hash',
                       p.manifest->'evaluation_classification'->>'classification_hash',
                       c.result_classification_hash
                  FROM app.render_share_feedback f
                  JOIN app.feedback_eligible_observations e
                    ON e.org_id = f.org_id AND e.project_id = f.project_id
                   AND e.source = 'anonymous_share'
                   AND e.interaction_ref = f.interaction_ref
                  JOIN app.query_result_payloads p
                    ON p.result_id = f.result_id AND p.org_id = f.org_id
                   AND p.project_id = f.project_id
                  JOIN app.evaluation_run_cases c
                    ON c.result_id = f.result_id AND c.org_id = f.org_id
                   AND c.project_id = f.project_id AND c.render_ref = f.render_id
                 WHERE f.id = %s
                """,
                (anonymous_id,),
            )
            classification_identity = cur.fetchone()
        if classification_identity is None or len(set(classification_identity)) != 1:
            raise AssertionError(
                "Result writer, eligibility, feedback detail and Evaluation must share one "
                "classification hash"
            )
        if share_detail["classification"]["classification_hash"] != classification_identity[0]:
            raise AssertionError("detail did not retain the Result-owned classification identity")
        aggregates = aggregate_feedback(
            conn,
            org_id=org_id,
            project_id=project_id,
            filters=filters,
        )
        critical = list_unresolved_critical_negatives(
            conn, org_id=org_id, project_id=project_id
        )
    for axis in aggregates["axes"].values():
        axis["buckets"].sort(
            key=lambda bucket: (
                json.dumps(bucket["key"], sort_keys=True),
                bucket["positive"],
                bucket["negative"],
                bucket["annotations"],
                bucket["unresolved_critical_negatives"],
            )
        )
    if {item["source"] for item in collection["items"]} != {
        "authenticated",
        "anonymous_share",
    }:
        raise AssertionError("unified collection did not expose both immutable stores")
    if share_detail["review"]["current_version_id"] != receipt["review_version_id"]:
        raise AssertionError("Share review receipt did not advance its registry head")
    if not share_detail["automated_verdicts"]["items"]:
        raise AssertionError("exact Evaluation verdict did not match the Share detail tuple")
    automated = share_detail["automated_verdicts"]["items"][0]
    if (
        automated["dimension"],
        automated["verdict"],
        automated["reason_code"],
        automated["evidence_refs"].get("result_id"),
        automated["evidence_refs"].get("render_id"),
    ) != (
        "path_quality",
        "fail",
        "expected_path_mismatch",
        share_detail["result"]["id"],
        share_detail["render"]["render_id"],
    ):
        raise AssertionError("automated verdict did not retain its full exact evidence tuple")
    normalizer = _Normalizer()
    return normalizer.value(
        {
            "schema_version": SCHEMA_VERSION,
            "collection": collection,
            "detail": share_detail,
            "authenticated_detail": authenticated_detail,
            "aggregates": aggregates,
            "critical_negatives": critical,
            "review_command": command,
            "review_receipt": receipt,
        }
    )
