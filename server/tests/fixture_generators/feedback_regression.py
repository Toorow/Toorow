"""Real-PostgreSQL producer for the Story 65.9 regression fixture."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date
from typing import Any

import psycopg
from core.evaluation_runs import (
    add_run_case,
    create_context_version_set,
    create_run_profile,
    finalize_evaluation_run,
    open_evaluation_run,
    run_cases,
)
from core.feedback_regression import (
    evaluate_feedback_regression_case,
    get_feedback_regression_draft,
    promote_feedback_regression,
    resolve_feedback_regression,
)
from core.feedback_review import append_review_version, canonical_hash
from core.golden_questions import (
    create_golden_question,
    get_golden_question,
    validate_golden_question_version,
)
from ulid import ULID

from tests.fixture_generators.analyze_feedback import build_fixture as build_delivery_fixture

SCHEMA_VERSION = "feedback-regression-fixture.v1"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^[a-z][a-z0-9]*_[0-9A-HJKMNP-TV-Z]{26}$")
_DOMAIN_VERSION_ID = re.compile(r"^([a-z][a-z0-9]*_[0-9A-HJKMNP-TV-Z]{26}):(\d+)$")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _review_command() -> dict[str, Any]:
    return {
        "schema_version": "feedback-review-command.v1",
        "expected_head": None,
        "retry_key": "fixture-regression-review-retry",
        "state": "accepted",
        "affected_dimension": "semantic_correctness",
        "human_verdict": "fail",
        "severity": "critical",
        "reason": "This exact failed path is reproducible and should become a regression case.",
    }


def _create_command(review_version_id: str, selected_domain: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "feedback-regression-create.v1",
        "expected_review_version_id": review_version_id,
        "retry_key": "fixture-regression-create-retry",
        "title": "Reproduce the exact feedback path failure",
        "owner": "owner@example.com",
        "reproduction_reason": "The reviewed failure is deterministic on the frozen fixture.",
        "selected_domain": selected_domain,
        "question": "Does the frozen fixture return exactly three result rows?",
        "time_boundary": {},
        "expected_result": [
            {
                "assertion_type": "cardinality",
                "selectors": [],
                "operator": "equals",
                "expected": 3,
                "tolerance": None,
            }
        ],
        "required_provenance": [{"link_kind": "semantic_view", "required": True}],
        "expected_ai_path": {
            "grammar_version": 1,
            "required_nodes": [],
        },
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
            return "2026-08-11T12:00:00Z"
        if key in {"actor", "reviewer", "owner", "created_by"} and "@" in value:
            return "owner@example.com"
        domain_version = _DOMAIN_VERSION_ID.fullmatch(value)
        if domain_version:
            return f"{self.value(domain_version.group(1), 'object_id')}:{domain_version.group(2)}"
        if value.startswith("afi_"):
            return self.interactions.setdefault(
                value, f"afi_FIXTURE_{len(self.interactions) + 1}"
            )
        if _HEX_64.fullmatch(value):
            return self.hashes.setdefault(
                value,
                hashlib.sha256(f"fixture-hash-{len(self.hashes) + 1}".encode()).hexdigest(),
            )
        if _OBJECT_ID.fullmatch(value):
            prefix = value.split("_", 1)[0]
            return self.ids.setdefault(value, f"{prefix}_FIXTURE_{len(self.ids) + 1}")
        return value


async def build_fixture(
    dsn: str, *, normalize: bool = True, actor: str = "owner@example.com"
) -> dict[str, Any]:
    """Produce one exact promotion, finalized offline case, evaluation and resolution.

    `actor` is the identity every write in this lineage is attributed to. It is
    a parameter because idempotency is scoped to it and nothing else would make
    that visible: `uq_feedback_regression_cases_retry`
    (`252_feedback_regression_cases.sql:103`) is
    UNIQUE (feedback_id, created_by, retry_key_hash, org_id, project_id), and
    `promote_feedback_regression` / `evaluate_feedback_regression_case` hash the
    actor into the retry key. A caller that replays this lineage through the
    HTTP door has to produce it under the identity that door authenticates as --
    a canonical `person_<ULID>` since 2026-08-24 -- or the replay looks up a row
    that does not exist. The default keeps the readable address the admin golden
    (`ui/admin/src/__tests__/fixtures/feedbackRegressionExact.json`) is
    generated with.
    """
    await build_delivery_fixture(dsn)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.org_id, f.project_id, f.result_id, f.render_id, f.ai_path_id,
                       q.semantic_view_id, q.semantic_view_version_id
                  FROM app.render_share_feedback f
                  JOIN app.feedback_eligible_observations e
                    ON e.org_id = f.org_id AND e.project_id = f.project_id
                   AND e.source = 'anonymous_share'
                   AND e.interaction_ref = f.interaction_ref
                  JOIN app.query_results qr
                    ON qr.id = f.result_id AND qr.org_id = f.org_id
                   AND qr.project_id = f.project_id
                  JOIN app.query_spec_versions q
                    ON q.id = qr.query_spec_version_id AND q.org_id = qr.org_id
                   AND q.project_id = qr.project_id
                  JOIN app.renders r
                    ON r.id = f.render_id AND r.org_id = f.org_id
                   AND r.project_id = f.project_id
                 WHERE f.polarity IN ('negative', 'not_helpful')
                   AND e.classification->'business_domains'->>'state' = 'attributed'
                   AND e.classification->'capability'->>'state' = 'attributed'
                   AND e.classification->'result_type'->>'state' = 'attributed'
                 ORDER BY f.submitted_at DESC
                 LIMIT 1
                """
            )
            subject = cur.fetchone()
        if subject is None:
            raise AssertionError("delivery producer emitted no promotable exact Share feedback")
        (
            feedback_id,
            org_id,
            project_id,
            result_id,
            render_id,
            ai_path_id,
            semantic_view_id,
            semantic_view_version_id,
        ) = map(lambda value: None if value is None else str(value), subject)

        review_command = _review_command()
        review_receipt = append_review_version(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            reviewer=actor,
            payload=review_command,
        )
        draft = get_feedback_regression_draft(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
        )
        if draft.get("state") != "available" or not draft.get("domain_options"):
            raise AssertionError(f"reviewed exact feedback was not promotable: {draft!r}")
        selected_domain = dict(draft["domain_options"][0])
        create_command = _create_command(review_receipt["review_version_id"], selected_domain)
        create_receipt = promote_feedback_regression(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            actor=actor,
            payload=create_command,
        )
        create_replay_receipt = promote_feedback_regression(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            actor=actor,
            payload=create_command,
        )
        if create_replay_receipt != create_receipt:
            raise AssertionError("promotion replay receipt is not byte-identical")
        golden_question = get_golden_question(
            conn,
            org_id=org_id,
            project_id=project_id,
            golden_question_id=create_receipt["golden_question_id"],
        )

        profile = create_run_profile(
            conn,
            org_id=org_id,
            project_id=project_id,
            name=f"Feedback regression {feedback_id[-8:]}",
            evidence_mode="offline",
            actor=actor,
        )
        context = create_context_version_set(
            conn,
            org_id=org_id,
            project_id=project_id,
            entries=[
                {
                    "owner_workspace": "governance",
                    "owner_object_type": "semantic-view",
                    "owner_object_id": semantic_view_id,
                    "owner_version_id": semantic_view_version_id,
                }
            ],
        )
        digest = hashlib.sha256(b"feedback-regression-offline-fixture").hexdigest()
        run = open_evaluation_run(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_profile_id=profile["id"],
            semantic_view_id=semantic_view_id,
            semantic_view_version_id=semantic_view_version_id,
            context_version_set_id=context["id"],
            model_ref="fixture-model-v1",
            host_capability_profile={"host": "fixture", "supports_apps": True},
            tool_catalog_version=digest,
            data_snapshot_ref={"kind": "fixture", "ref": "feedback-regression-v1"},
            as_of=date(2026, 8, 11),
            actor=actor,
        )
        case = add_run_case(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_id=run["id"],
            golden_question_version_id=create_receipt["golden_question_version_id"],
            result_id=result_id,
            ai_path_id=ai_path_id,
            ai_path_expected=ai_path_id is not None,
            render_ref=render_id,
        )
        finalized = finalize_evaluation_run(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_id=run["id"],
        )
        evaluation_command = {
            "schema_version": "evaluation-request.v1",
            "retry_key": "fixture-regression-evaluate-retry",
        }
        evaluation_receipt = evaluate_feedback_regression_case(
            conn,
            org_id=org_id,
            project_id=project_id,
            case_id=case["id"],
            actor=actor,
            payload=evaluation_command,
        )
        evaluation_case = next(
            item
            for item in run_cases(
                conn,
                org_id=org_id,
                project_id=project_id,
                run_id=run["id"],
            )["cases"]
            if item["id"] == case["id"]
        )

        # Build a second, fully real trusted case whose complete identity tuple
        # differs from the promotion under review.  A missing/fake id proves only
        # not-found handling; this case proves exact GQ/Result/classification
        # compatibility is rechecked after a trusted pass exists.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT qr.attempt_id, p.manifest
                  FROM app.query_results qr
                  JOIN app.query_result_payloads p
                    ON p.result_id = qr.id AND p.org_id = qr.org_id
                   AND p.project_id = qr.project_id AND p.content_hash = qr.content_hash
                 WHERE qr.id = %s AND qr.org_id = %s AND qr.project_id = %s
                """,
                (result_id, org_id, project_id),
            )
            result_owner = cur.fetchone()
        if result_owner is None:
            raise AssertionError("fixture Result payload disappeared before mismatch proof")
        original_attempt_id, original_manifest = result_owner
        mismatched_classification = copy.deepcopy(
            original_manifest["evaluation_classification"]
        )
        mismatched_classification["result_type"] = {
            "state": "attributed",
            "value": "comparison",
        }
        mismatched_classification.pop("classification_hash", None)
        mismatched_classification["classification_hash"] = canonical_hash(
            mismatched_classification
        )
        mismatched_manifest = copy.deepcopy(original_manifest)
        mismatched_manifest["evaluation_classification"] = mismatched_classification
        mismatched_attempt_id = _uid("qea")
        mismatched_result_id = _uid("qr")
        mismatched_result_hash = hashlib.sha256(
            f"feedback-regression-mismatch:{mismatched_result_id}".encode()
        ).hexdigest()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.query_execution_attempts
                SELECT (jsonb_populate_record(
                    NULL::app.query_execution_attempts,
                    to_jsonb(a) || jsonb_build_object(
                        'id', %s::text, 'result_id', %s::text
                    )
                )).* FROM app.query_execution_attempts a WHERE a.id = %s
                """,
                (mismatched_attempt_id, mismatched_result_id, original_attempt_id),
            )
            cur.execute(
                """
                INSERT INTO app.query_results
                SELECT (jsonb_populate_record(
                    NULL::app.query_results,
                    to_jsonb(qr) || jsonb_build_object(
                        'id', %s::text, 'attempt_id', %s::text,
                        'content_hash', %s::text
                    )
                )).* FROM app.query_results qr WHERE qr.id = %s
                """,
                (
                    mismatched_result_id,
                    mismatched_attempt_id,
                    mismatched_result_hash,
                    result_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO app.query_result_payloads
                SELECT (jsonb_populate_record(
                    NULL::app.query_result_payloads,
                    to_jsonb(p) || jsonb_build_object(
                        'result_id', %s::text, 'content_hash', %s::text,
                        'manifest', %s::jsonb
                    )
                )).* FROM app.query_result_payloads p
                 WHERE p.result_id = %s AND p.content_hash = %s
                """,
                (
                    mismatched_result_id,
                    mismatched_result_hash,
                    json.dumps(mismatched_manifest, sort_keys=True, separators=(",", ":")),
                    result_id,
                    draft["frozen"]["result"]["content_hash"],
                ),
            )

        selected = selected_domain
        frozen = draft["frozen"]
        mismatched_definition = {
            "contract_version": "golden-question.v2",
            "business_domain_id": selected["domain_id"],
            "business_domain_version_number": selected["version_number"],
            "semantic_view_id": frozen["semantic_view"]["id"],
            "semantic_view_version_id": frozen["semantic_view"]["version_id"],
            "semantic_view_version_role": "candidate",
            "question": "Does the deliberately different frozen Result still have three rows?",
            "time_boundary": {},
            "expected_result": create_command["expected_result"],
            "required_provenance": create_command["required_provenance"],
            "expected_ai_path": create_command["expected_ai_path"],
            "result_type": "comparison",
            "capability_tags": [frozen["capability"]["key"]],
            "severity": draft["review"]["severity"],
            "reference_paths": [
                {
                    "query_spec_version_id": frozen["query_spec_version_id"],
                    "role": "canonical",
                }
            ],
        }
        mismatched_validated = validate_golden_question_version(
            conn,
            org_id=org_id,
            project_id=project_id,
            payload=mismatched_definition,
        )
        mismatched_golden = create_golden_question(
            conn,
            org_id=org_id,
            project_id=project_id,
            title="Independent incompatible regression question",
            owner="owner@example.com",
            validated=mismatched_validated,
            actor=actor,
        )
        mismatched_regression_id = _uid("frc")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.feedback_regression_cases
                    (id, feedback_id, org_id, project_id, review_version_id,
                     golden_question_id, golden_question_version_id,
                     result_id, result_content_hash, query_spec_version_id,
                     result_classification_hash, semantic_view_id,
                     semantic_view_version_id, business_domain_id,
                     business_domain_version_number, capability_key,
                     capability_version_id, result_type, eligible_target,
                     render_ref, ai_path_id, ai_path_absent_literal,
                     reproduction_reason, retry_key_hash, request_hash, created_by)
                SELECT %s, feedback_id, org_id, project_id, review_version_id,
                       %s, %s, %s, %s, query_spec_version_id,
                       %s, semantic_view_id, semantic_view_version_id,
                       business_domain_id, business_domain_version_number,
                       capability_key, capability_version_id, 'comparison', eligible_target,
                       NULL, ai_path_id, ai_path_absent_literal,
                       'Independent incompatible exact tuple', %s, %s, created_by
                  FROM app.feedback_regression_cases
                 WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (
                    mismatched_regression_id,
                    mismatched_golden["golden_question_id"],
                    mismatched_golden["version_id"],
                    mismatched_result_id,
                    mismatched_result_hash,
                    mismatched_classification["classification_hash"],
                    hashlib.sha256(b"mismatched-regression-retry").hexdigest(),
                    hashlib.sha256(b"mismatched-regression-request").hexdigest(),
                    create_receipt["regression_case_id"],
                    org_id,
                    project_id,
                ),
            )
        mismatched_profile = create_run_profile(
            conn,
            org_id=org_id,
            project_id=project_id,
            name=f"Incompatible feedback regression {feedback_id[-8:]}",
            evidence_mode="offline",
            actor=actor,
        )
        mismatched_run = open_evaluation_run(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_profile_id=mismatched_profile["id"],
            semantic_view_id=semantic_view_id,
            semantic_view_version_id=semantic_view_version_id,
            context_version_set_id=context["id"],
            model_ref="fixture-model-v1",
            host_capability_profile={"host": "fixture", "supports_apps": True},
            tool_catalog_version=digest,
            data_snapshot_ref={"kind": "fixture", "ref": "feedback-regression-mismatch"},
            as_of=date(2026, 8, 11),
            actor=actor,
        )
        mismatched_case = add_run_case(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_id=mismatched_run["id"],
            golden_question_version_id=mismatched_golden["version_id"],
            result_id=mismatched_result_id,
            ai_path_id=ai_path_id,
            ai_path_expected=ai_path_id is not None,
        )
        finalize_evaluation_run(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_id=mismatched_run["id"],
        )
        mismatched_evaluation_command = {
            "schema_version": "evaluation-request.v1",
            "retry_key": "fixture-regression-mismatch-evaluate-retry",
        }
        mismatched_evaluation_receipt = evaluate_feedback_regression_case(
            conn,
            org_id=org_id,
            project_id=project_id,
            case_id=mismatched_case["id"],
            actor=actor,
            payload=mismatched_evaluation_command,
        )
        mismatched_evaluation_case = next(
            item
            for item in run_cases(
                conn,
                org_id=org_id,
                project_id=project_id,
                run_id=mismatched_run["id"],
            )["cases"]
            if item["id"] == mismatched_case["id"]
        )
        incompatible_resolution_command = {
            "schema_version": "feedback-regression-resolve.v1",
            "regression_case_id": create_receipt["regression_case_id"],
            "evaluation_case_id": mismatched_case["id"],
        }
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.feedback_regression_resolutions WHERE feedback_id = %s",
                (feedback_id,),
            )
            incompatible_resolution_count_before = cur.fetchone()[0]
        incompatible_resolution_receipt = resolve_feedback_regression(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            actor=actor,
            payload=incompatible_resolution_command,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.feedback_regression_resolutions WHERE feedback_id = %s",
                (feedback_id,),
            )
            incompatible_resolution_count_after = cur.fetchone()[0]
        if incompatible_resolution_count_after != incompatible_resolution_count_before:
            raise AssertionError("incompatible trusted evidence wrote resolution lineage")
        resolution_command = {
            "schema_version": "feedback-regression-resolve.v1",
            "regression_case_id": create_receipt["regression_case_id"],
            "evaluation_case_id": case["id"],
        }
        resolution_receipt = resolve_feedback_regression(
            conn,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            actor=actor,
            payload=resolution_command,
        )
        conn.commit()

    if create_receipt.get("status") != "created":
        raise AssertionError("fixture promotion did not create a new regression case")
    if evaluation_receipt.get("producer") != "result-case-evaluator.v1":
        raise AssertionError("fixture evaluation was not produced by the trusted evaluator")
    if resolution_receipt.get("status") != "resolved":
        raise AssertionError(
            f"trusted matching pass did not resolve feedback: {resolution_receipt!r}"
        )

    result = {
        "schema_version": SCHEMA_VERSION,
        "scope": {
            "org_id": org_id,
            "project_id": project_id,
            "feedback_id": feedback_id,
        },
        "draft": draft,
        "review_command": review_command,
        "review_receipt": review_receipt,
        "create_command": create_command,
        "create_receipt": create_receipt,
        "create_replay_receipt": create_replay_receipt,
        "golden_question": golden_question,
        "evaluation_run": finalized,
        "evaluation_case": evaluation_case,
        "evaluation_command": evaluation_command,
        "evaluation_receipt": evaluation_receipt,
        "mismatched_regression_case_id": mismatched_regression_id,
        "mismatched_golden_question": mismatched_golden,
        "mismatched_evaluation_case": mismatched_evaluation_case,
        "mismatched_evaluation_command": mismatched_evaluation_command,
        "mismatched_evaluation_receipt": mismatched_evaluation_receipt,
        "incompatible_resolution_command": incompatible_resolution_command,
        "incompatible_resolution_receipt": incompatible_resolution_receipt,
        "incompatible_resolution_count_before": incompatible_resolution_count_before,
        "incompatible_resolution_count_after": incompatible_resolution_count_after,
        "resolution_command": resolution_command,
        "resolution_receipt": resolution_receipt,
    }
    return _Normalizer().value(result) if normalize else result
