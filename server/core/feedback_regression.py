"""Promote reviewed exact feedback into authored, executable regression evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.feedback_review import (
    append_review_version,
    canonical_hash,
    get_annotation,
    result_classification_hash_from_manifest,
)
from core.golden_questions import (
    GOLDEN_QUESTION_V2_CONTRACT_VERSION,
    GoldenQuestionRefused,
    create_golden_question,
    validate_golden_question_version,
)
from core.result_assertion_evaluator import (
    evaluate_required_provenance,
    evaluate_result_assertions,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : elles etaient retapees en dur a l appel, donc rien
# ne pouvait distinguer une action d une faute de frappe. Declarees ici,
# a cote du code qui les ecrit.
ACTION_FEEDBACK_REGRESSION_PROMOTED = declare_action("test.feedback_regression.promoted")


RESULT_CASE_EVALUATOR = "result-case-evaluator.v1"
CALLER_DECLARED_PRODUCER = "caller-declared.v1"
_PRODUCER_CONTRACT = {
    "schema_version": RESULT_CASE_EVALUATOR,
    "assertion_contract": GOLDEN_QUESTION_V2_CONTRACT_VERSION,
    "number_contract": "decimal-string.v1",
    "timestamp_contract": "rfc3339-utc.v1",
    "row_contract": "canonical-json-multiset.v1",
}
RESULT_CASE_EVALUATOR_HASH = canonical_hash(_PRODUCER_CONTRACT)

_CREATE_KEYS = {
    "schema_version",
    "expected_review_version_id",
    "retry_key",
    "title",
    "owner",
    "reproduction_reason",
    "selected_domain",
    "question",
    "time_boundary",
    "expected_result",
    "required_provenance",
    "expected_ai_path",
}
_EVALUATE_KEYS = {"schema_version", "retry_key"}
_RESOLVE_KEYS = {"schema_version", "regression_case_id", "evaluation_case_id"}
_VERDICT_DIMENSIONS = (
    "semantic_correctness",
    "provenance_correctness",
    "context_adherence",
    "path_quality",
    "dq_handling",
    "mcp_app_behavior",
)


@dataclass(frozen=True)
class RegressionRefusal:
    code: str
    message: str
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "subject": self.subject}


class FeedbackRegressionNotFound(LookupError):
    pass


class FeedbackRegressionRefused(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        refusals: Sequence[RegressionRefusal | Mapping[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.refusals = list(refusals or [])

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "refusals": [
                item.as_dict() if isinstance(item, RegressionRefusal) else dict(item)
                for item in self.refusals
            ],
        }


def _refuse(code: str, message: str, subject: str | None = None) -> None:
    raise FeedbackRegressionRefused(code, message, [RegressionRefusal(code, message, subject)])


def _exact_payload(payload: Any, keys: set[str], schema_version: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        _refuse("invalid_shape", "request body must be an object")
    unknown = sorted(set(payload) - keys)
    if unknown:
        _refuse("unknown_field", f"`{unknown[0]}` is not accepted", unknown[0])
    missing = sorted(keys - set(payload))
    if missing:
        _refuse("missing_field", f"`{missing[0]}` is required", missing[0])
    if payload.get("schema_version") != schema_version:
        _refuse("invalid_schema_version", f"schema_version must be `{schema_version}`")
    return dict(payload)


def _text(value: Any, subject: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        _refuse("invalid_field", f"{subject} must contain 1 to {maximum} characters", subject)
    return value.strip()


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "schema_version": "feedback-regression-draft.v1",
        "state": "unavailable",
        "reason": reason,
        "subject": None,
        "review": None,
        "frozen": None,
        "domain_options": [],
        "requires_domain_selection": False,
        "create_contract": None,
    }


def _complete_classification(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "evaluation-classification.v1"
    ):
        return False
    semantic = value.get("semantic_view")
    domains = value.get("business_domains")
    skills = value.get("skills")
    capability = value.get("capability")
    result_type = value.get("result_type")
    return bool(
        isinstance(value.get("classification_hash"), str)
        and isinstance(semantic, Mapping)
        and semantic.get("state") == "attributed"
        and semantic.get("id")
        and semantic.get("version_id")
        and isinstance(domains, Mapping)
        and domains.get("state") == "attributed"
        and isinstance(domains.get("versions"), list)
        and domains["versions"]
        and isinstance(skills, Mapping)
        and skills.get("state") == "attributed"
        and isinstance(skills.get("versions"), list)
        and isinstance(capability, Mapping)
        and capability.get("state") == "attributed"
        and capability.get("key")
        and capability.get("version_id")
        and isinstance(result_type, Mapping)
        and result_type.get("state") == "attributed"
        and result_type.get("value")
    )


def get_feedback_regression_draft(
    conn,
    *,
    org_id: str,
    project_id: str,
    feedback_id: str,
) -> dict[str, Any]:
    """Return one promotion draft, or one stable unavailable reason and no action."""
    try:
        detail = get_annotation(conn, org_id=org_id, project_id=project_id, feedback_id=feedback_id)
    except Exception as exc:
        from core.feedback_review import FeedbackNotFound

        if isinstance(exc, FeedbackNotFound):
            raise FeedbackRegressionNotFound("feedback not found") from exc
        raise
    if detail.get("target_schema_version") != "exact-feedback.v1":
        return _unavailable("exact_feedback_required")
    if detail.get("polarity") != "negative":
        return _unavailable("positive_feedback_not_promotable")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.current_review_version_id, v.review_state, v.affected_dimension,
                   v.human_verdict, v.severity, v.reason
              FROM app.feedback_reviews r
              JOIN app.feedback_review_versions v
                ON v.id = r.current_review_version_id AND v.feedback_id = r.feedback_id
               AND v.org_id = r.org_id AND v.project_id = r.project_id
             WHERE r.feedback_id = %s AND r.org_id = %s AND r.project_id = %s
            """,
            (feedback_id, org_id, project_id),
        )
        review = cur.fetchone()
    if (
        review is None
        or review[1] != "accepted"
        or review[3] != "fail"
        or review[2] == "not_applicable"
    ):
        return _unavailable("accepted_fail_review_required")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.authority, e.classification, e.classification_hash
              FROM app.feedback_eligible_observations e
             WHERE e.org_id = %s AND e.project_id = %s AND e.source = %s
               AND e.interaction_ref = %s AND e.result_id = %s
               AND e.result_content_hash = %s
            """,
            (
                org_id,
                project_id,
                detail.get("source"),
                detail.get("interaction_ref"),
                detail["result"]["id"],
                detail["result"].get("content_hash"),
            ),
        )
        eligibility = cur.fetchone()
    if eligibility is None:
        return _unavailable("eligible_observation_missing")
    classification = eligibility[1]
    if (
        not _complete_classification(classification)
        or classification.get("classification_hash") != eligibility[2]
        or classification != detail.get("classification")
    ):
        return _unavailable("complete_classification_required")
    authority = eligibility[0] if isinstance(eligibility[0], Mapping) else {}
    target = detail.get("target") if isinstance(detail.get("target"), Mapping) else {}
    if target.get("kind") not in authority.get("target_kinds", []):
        return _unavailable("eligible_target_missing")

    domain_options = [
        {"domain_id": str(item["id"]), "version_number": int(item["version_number"])}
        for item in classification["business_domains"]["versions"]
    ]
    capability = classification["capability"]
    frozen = {
        "result": {
            "id": detail["result"]["id"],
            "content_hash": detail["result"]["content_hash"],
            "outcome": detail["result"]["outcome"],
        },
        "query_spec_version_id": detail["result"]["query_spec_version_id"],
        "classification_hash": classification["classification_hash"],
        "semantic_view": classification["semantic_view"],
        "capability": {"key": capability["key"], "version_id": capability["version_id"]},
        "result_type": classification["result_type"]["value"],
        "eligible_target": dict(target),
        "render": detail.get("render"),
        "ai_path": detail.get("ai_path"),
    }
    return {
        "schema_version": "feedback-regression-draft.v1",
        "state": "available",
        "reason": None,
        "subject": {"feedback_id": feedback_id, "source": detail.get("source")},
        "review": {
            "version_id": str(review[0]),
            "state": str(review[1]),
            "affected_dimension": str(review[2]),
            "human_verdict": str(review[3]),
            "severity": str(review[4]),
            "reason": str(review[5]),
        },
        "frozen": frozen,
        "domain_options": domain_options,
        "requires_domain_selection": len(domain_options) > 1,
        "create_contract": {
            "schema_version": "feedback-regression-create.v1",
            "expected_review_version_id": str(review[0]),
        },
    }


def _owner_link(
    section: str, object_type: str, object_id: str, version_id: str | None = None
) -> dict[str, Any]:
    return {
        "workspace": "test",
        "section": section,
        "object_type": object_type,
        "object_id": object_id,
        "version_id": version_id,
        "tab": (
            "definition"
            if object_type == "golden-question"
            else "cases"
            if object_type == "evaluation-run"
            else None
        ),
    }


def _evaluation_owner_links(
    run_id: str, case_id: str, passing_verdict_ids: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Route exact run/case/pass identities through the mounted Run workbench."""
    return [
        _owner_link("regression-runs", "evaluation-run", run_id),
        _owner_link("regression-runs", "evaluation-run", run_id, case_id),
        *[
            _owner_link("regression-runs", "evaluation-run", run_id, verdict_id)
            for verdict_id in passing_verdict_ids
        ],
    ]


def promote_feedback_regression(
    conn,
    *,
    org_id: str,
    project_id: str,
    feedback_id: str,
    actor: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    command = _exact_payload(payload, _CREATE_KEYS, "feedback-regression-create.v1")
    retry_key = _text(command["retry_key"], "retry_key", 200)
    actor = _text(actor, "actor", 200)
    request_hash = canonical_hash(
        {key: value for key, value in command.items() if key != "retry_key"}
    )
    retry_hash = canonical_hash(
        {"purpose": "feedback-regression-create", "actor": actor, "retry_key": retry_key}
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"feedback-regression-create:{org_id}:{project_id}:{feedback_id}",),
        )
        cur.execute(
            """
            SELECT id, request_hash, golden_question_id, golden_question_version_id,
                   review_version_id, result_id, result_content_hash,
                   result_classification_hash, feedback_id
              FROM app.feedback_regression_cases
             WHERE feedback_id = %s AND created_by = %s AND retry_key_hash = %s
               AND org_id = %s AND project_id = %s
            """,
            (feedback_id, actor, retry_hash, org_id, project_id),
        )
        existing = cur.fetchone()
    if existing is not None:
        if existing[1] != request_hash:
            _refuse("idempotency_conflict", "retry_key was already used for another command")
        return _promotion_receipt(existing, status="replayed")

    draft = get_feedback_regression_draft(
        conn, org_id=org_id, project_id=project_id, feedback_id=feedback_id
    )
    if draft["state"] != "available":
        _refuse(str(draft["reason"]), "feedback is not available for promotion")
    if command["expected_review_version_id"] != draft["review"]["version_id"]:
        _refuse("stale_review_version", "the current review version changed")
    selected = command["selected_domain"]
    if (
        not isinstance(selected, dict)
        or set(selected) != {"domain_id", "version_number"}
        or not isinstance(selected.get("domain_id"), str)
        or isinstance(selected.get("version_number"), bool)
        or not isinstance(selected.get("version_number"), int)
        or selected["version_number"] < 1
    ):
        _refuse("invalid_selected_domain", "selected_domain is an exact domain/version pair")
    if selected not in draft["domain_options"]:
        _refuse("invalid_selected_domain", "selected_domain is not a frozen option")

    frozen = draft["frozen"]
    definition = {
        "contract_version": GOLDEN_QUESTION_V2_CONTRACT_VERSION,
        "business_domain_id": selected["domain_id"],
        "business_domain_version_number": selected["version_number"],
        "semantic_view_id": frozen["semantic_view"]["id"],
        "semantic_view_version_id": frozen["semantic_view"]["version_id"],
        "semantic_view_version_role": "candidate",
        "question": command["question"],
        "time_boundary": command["time_boundary"],
        "expected_result": command["expected_result"],
        "required_provenance": command["required_provenance"],
        "expected_ai_path": command["expected_ai_path"],
        "result_type": frozen["result_type"],
        "capability_tags": [frozen["capability"]["key"]],
        "severity": draft["review"]["severity"],
        "reference_paths": [
            {
                "query_spec_version_id": frozen["query_spec_version_id"],
                "role": "canonical",
            }
        ],
    }
    try:
        validated = validate_golden_question_version(
            conn, org_id=org_id, project_id=project_id, payload=definition
        )
    except GoldenQuestionRefused as exc:
        raise FeedbackRegressionRefused(exc.code, str(exc), exc.refusals) from exc
    golden = create_golden_question(
        conn,
        org_id=org_id,
        project_id=project_id,
        title=_text(command["title"], "title", 200),
        owner=_text(command["owner"], "owner", 200),
        validated=validated,
        actor=actor,
    )
    regression_id = f"frc_{ULID()}"
    render = frozen.get("render") or {}
    ai_path = frozen.get("ai_path")
    ai_path_id = ai_path if isinstance(ai_path, str) and ai_path.startswith("aip_") else None
    ai_path_absent = ai_path if ai_path_id is None else None
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                regression_id,
                feedback_id,
                org_id,
                project_id,
                draft["review"]["version_id"],
                golden["golden_question_id"],
                golden["version_id"],
                frozen["result"]["id"],
                frozen["result"]["content_hash"],
                frozen["query_spec_version_id"],
                frozen["classification_hash"],
                frozen["semantic_view"]["id"],
                frozen["semantic_view"]["version_id"],
                selected["domain_id"],
                selected["version_number"],
                frozen["capability"]["key"],
                frozen["capability"]["version_id"],
                frozen["result_type"],
                _json(frozen["eligible_target"]),
                render.get("render_id"),
                ai_path_id,
                ai_path_absent,
                _text(command["reproduction_reason"], "reproduction_reason"),
                retry_hash,
                request_hash,
                actor,
            ),
        )
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_FEEDBACK_REGRESSION_PROMOTED,
        provider_account="test",
        connection_ref="",
        metadata={
            "effective_org_id": org_id,
            "project_id": project_id,
            "resource_id": regression_id,
        },
    )
    return _promotion_receipt(
        (
            regression_id,
            request_hash,
            golden["golden_question_id"],
            golden["version_id"],
            draft["review"]["version_id"],
            frozen["result"]["id"],
            frozen["result"]["content_hash"],
            frozen["classification_hash"],
            feedback_id,
        ),
        status="created",
    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _promotion_receipt(row: Sequence[Any], *, status: str) -> dict[str, Any]:
    return {
        "schema_version": "feedback-regression-receipt.v1",
        "status": "created",
        "regression_case_id": row[0],
        "golden_question_id": row[2],
        "golden_question_version_id": row[3],
        "review_version_id": row[4],
        "result_id": row[5],
        "result_content_hash": row[6],
        "classification_hash": row[7],
        "owner_links": [
            _owner_link("golden-questions", "golden-question", str(row[2]), str(row[3])),
            _owner_link("widget-feedback", "feedback-review", str(row[8])),
        ],
    }


def _aggregate_assertion_verdict(
    receipts: list[Mapping[str, Any]], assertion_types: set[str]
) -> dict[str, Any]:
    selected = [item for item in receipts if item["assertion_type"] in assertion_types]
    if not selected:
        return {"verdict": "not_applicable", "reason_code": "no_applicable_assertions"}
    if any(item["verdict"] == "unverifiable" for item in selected):
        return {"verdict": "unverifiable", "reason_code": "assertion_evidence_unverifiable"}
    if any(item["verdict"] == "fail" for item in selected):
        return {"verdict": "fail", "reason_code": "assertion_failed"}
    return {"verdict": "pass", "reason_code": "assertions_passed"}


def evaluate_feedback_regression_case(
    conn,
    *,
    org_id: str,
    project_id: str,
    case_id: str,
    actor: str,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    command = _exact_payload(payload, _EVALUATE_KEYS, "evaluation-request.v1")
    actor = _text(actor, "actor", 200)
    retry_hash = canonical_hash(
        {
            "purpose": "result-case-evaluation",
            "actor": actor,
            "retry_key": _text(command["retry_key"], "retry_key", 200),
        }
    )
    request_hash = canonical_hash({"schema_version": "evaluation-request.v1", "case_id": case_id})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"feedback-regression-evaluate:{org_id}:{project_id}:{case_id}",),
        )
        cur.execute(
            """
            SELECT c.id, c.run_id, r.lifecycle, r.evidence_mode,
                   c.golden_question_version_id, g.expected_result,
                   g.required_provenance, g.expected_ai_path,
                   c.result_id, qr.content_hash, qr.outcome, qr.row_count,
                   p.rows_chunk, p.manifest, c.ai_path_id, c.ai_path_absent_literal,
                   c.result_classification_hash,
                   p.manifest->'evaluation_classification'->>'classification_hash',
                   qr.truncated
              FROM app.evaluation_run_cases c
              JOIN app.evaluation_runs r
                ON r.id = c.run_id AND r.org_id = c.org_id AND r.project_id = c.project_id
              JOIN app.golden_question_versions g
                ON g.id = c.golden_question_version_id AND g.org_id = c.org_id
               AND g.project_id = c.project_id AND g.contract_version = 'golden-question.v2'
              JOIN app.query_results qr
                ON qr.id = c.result_id AND qr.org_id = c.org_id AND qr.project_id = c.project_id
              JOIN app.query_result_payloads p
                ON p.result_id = qr.id AND p.org_id = qr.org_id
               AND p.project_id = qr.project_id AND p.content_hash = qr.content_hash
              JOIN app.feedback_regression_cases promotion
                ON promotion.golden_question_version_id = c.golden_question_version_id
               AND promotion.result_id = c.result_id
               AND promotion.result_content_hash = qr.content_hash
               AND promotion.org_id = c.org_id AND promotion.project_id = c.project_id
               AND promotion.business_domain_id = c.business_domain_id
               AND promotion.business_domain_version_number = c.business_domain_version_number
               AND promotion.capability_key = c.capability_key
               AND promotion.result_type = c.result_type
             WHERE c.id = %s AND c.org_id = %s AND c.project_id = %s
            """,
            (case_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise FeedbackRegressionNotFound("evaluation case not found")
    if row[2] != "finalized" or row[3] != "offline":
        _refuse("finalized_offline_case_required", "only a finalized offline case can be evaluated")
    if (
        not row[8]
        or row[16] != row[17]
        or result_classification_hash_from_manifest(row[13]) != row[16]
    ):
        _refuse("exact_result_classification_required", "case Result classification is incomplete")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, request_hash, assertion_ordinal, assertion_type, verdict,
                   reason_code, expected_hash, observed_hash, evidence_refs
              FROM app.evaluation_assertion_results
             WHERE case_id = %s AND evaluated_by = %s AND retry_key_hash = %s
               AND org_id = %s AND project_id = %s
             ORDER BY assertion_ordinal
            """,
            (case_id, actor, retry_hash, org_id, project_id),
        )
        replay = cur.fetchall()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, dimension, verdict, reason_code, producer,
                   producer_contract_hash, evidence_refs
              FROM app.evaluation_case_dimension_verdicts
             WHERE case_id = %s AND org_id = %s AND project_id = %s
             ORDER BY dimension
            """,
            (case_id, org_id, project_id),
        )
        stored_verdict_rows = cur.fetchall()
    trusted_verdict_rows = [
        item
        for item in stored_verdict_rows
        if item[4] == RESULT_CASE_EVALUATOR and item[5] == RESULT_CASE_EVALUATOR_HASH
    ]
    if replay:
        if replay[0][1] != request_hash:
            _refuse("idempotency_conflict", "retry_key was already used for another evaluation")
        if len(trusted_verdict_rows) != 6:
            _refuse("evaluation_receipt_incomplete", "stored evaluation verdicts are incomplete")
        stored_verdicts = {
            item[1]: {
                "verdict_id": item[0],
                "verdict": item[2],
                "reason_code": item[3],
                "evidence_refs": item[6],
            }
            for item in trusted_verdict_rows
        }
        return _evaluation_receipt(
            row[0], row[1], replay, status="evaluated", verdicts=stored_verdicts
        )
    if stored_verdict_rows:
        _refuse("case_already_evaluated", "this immutable case already has trusted verdicts")

    receipts = evaluate_result_assertions(
        list(row[5]),
        result={
            "outcome": row[10],
            "row_count": row[11],
            "rows": row[12],
            "truncated": row[18],
        },
    )
    provenance = evaluate_required_provenance(list(row[6]), manifest=row[13])
    path = (
        {"verdict": "not_applicable", "reason_code": "no_ai_path"}
        if row[15]
        else {"verdict": "unverifiable", "reason_code": "path_evidence_missing"}
    )
    if row[14]:
        from core.evaluation_runs import evaluate_case_path, record_path_comparison

        path = evaluate_case_path(
            conn,
            org_id=org_id,
            project_id=project_id,
            case={"id": row[0], "golden_question_version_id": row[4], "ai_path_id": row[14]},
        )
        # This INSERT used to live here, spelled out, with its own hash rule. Two
        # copies of one mapping is how the stored evidence of a comparison starts
        # meaning two things depending on which flow wrote it, so the mapping now
        # lives once, beside the tables it writes.
        record_path_comparison(
            conn,
            org_id=org_id,
            project_id=project_id,
            case_id=case_id,
            ai_path_id=row[14],
            expected_pattern=row[7],
            verdict=path,
        )
    semantic = _aggregate_assertion_verdict(
        receipts, {"value", "row_set", "ordering", "cardinality", "invariant", "empty"}
    )
    dq = _aggregate_assertion_verdict(receipts, {"degraded", "refused"})
    verdicts = {
        "semantic_correctness": {
            **semantic,
            "evidence_refs": {
                "assertion_results": [
                    item["ordinal"]
                    for item in receipts
                    if item["assertion_type"]
                    in {"value", "row_set", "ordering", "cardinality", "invariant", "empty"}
                ]
            },
        },
        "provenance_correctness": {
            **provenance,
            "evidence_refs": {
                "result_id": row[8],
                "missing": provenance.get("missing", []),
            },
        },
        "context_adherence": {
            "verdict": "unverifiable",
            "reason_code": "context_skill_evidence_unavailable",
            "evidence_refs": {"owner": "context-skill-evaluator"},
        },
        "path_quality": {**path, "evidence_refs": path.get("evidence_refs", {})},
        "dq_handling": {
            **dq,
            "evidence_refs": {
                "assertion_results": [
                    item["ordinal"]
                    for item in receipts
                    if item["assertion_type"] in {"degraded", "refused"}
                ]
            },
        },
        "mcp_app_behavior": {
            "verdict": "unverifiable",
            "reason_code": "mcp_app_evaluator_unavailable",
            "evidence_refs": {"owner": "mcp-app-evaluator"},
        },
    }
    evaluated_at = now or datetime.now(UTC)
    stored_replay = []
    with conn.cursor() as cur:
        for item in receipts:
            assertion_id = f"ear_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.evaluation_assertion_results
                    (id, case_id, org_id, project_id, assertion_ordinal,
                     assertion_type, verdict, reason_code, expected_hash,
                     observed_hash, evidence_refs, producer,
                     producer_contract_hash, retry_key_hash, request_hash,
                     evaluated_by, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s, %s, %s)
                """,
                (
                    assertion_id,
                    case_id,
                    org_id,
                    project_id,
                    item["ordinal"],
                    item["assertion_type"],
                    item["verdict"],
                    item["reason_code"],
                    item["expected_hash"],
                    item["observed_hash"],
                    _json(item["evidence_refs"]),
                    RESULT_CASE_EVALUATOR,
                    RESULT_CASE_EVALUATOR_HASH,
                    retry_hash,
                    request_hash,
                    actor,
                    evaluated_at,
                ),
            )
            stored_replay.append(
                (
                    assertion_id,
                    request_hash,
                    item["ordinal"],
                    item["assertion_type"],
                    item["verdict"],
                    item["reason_code"],
                    item["expected_hash"],
                    item["observed_hash"],
                    item["evidence_refs"],
                )
            )
        for dimension, verdict in verdicts.items():
            verdict_id = f"edv_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.evaluation_case_dimension_verdicts
                    (id, case_id, org_id, project_id, dimension, verdict,
                     reason_code, evidence_refs, producer, producer_contract_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    verdict_id,
                    case_id,
                    org_id,
                    project_id,
                    dimension,
                    verdict["verdict"],
                    verdict["reason_code"],
                    _json(verdict["evidence_refs"]),
                    RESULT_CASE_EVALUATOR,
                    RESULT_CASE_EVALUATOR_HASH,
                ),
            )
            verdict["verdict_id"] = verdict_id
    return _evaluation_receipt(row[0], row[1], stored_replay, status="evaluated", verdicts=verdicts)


def _evaluation_receipt(
    case_id: str,
    run_id: str,
    rows: Sequence[Sequence[Any]],
    *,
    status: str,
    verdicts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    assertions = [
        {
            "assertion_result_id": row[0],
            "ordinal": row[2],
            "assertion_type": row[3],
            "verdict": row[4],
            "reason_code": row[5],
            "expected_hash": row[6],
            "observed_hash": row[7],
            "evidence_refs": row[8],
        }
        for row in rows
    ]
    receipt_verdicts = {
        dimension: {
            "verdict_id": verdicts[dimension]["verdict_id"],
            "verdict": verdicts[dimension]["verdict"],
            "reason_code": verdicts[dimension]["reason_code"],
            "evidence_refs": verdicts[dimension]["evidence_refs"],
        }
        for dimension in _VERDICT_DIMENSIONS
        if verdicts and dimension in verdicts
    }
    return {
        "schema_version": "evaluation-result-receipt.v1",
        "status": status,
        "evaluation_case_id": case_id,
        "run_id": run_id,
        "producer": RESULT_CASE_EVALUATOR,
        "producer_contract_hash": RESULT_CASE_EVALUATOR_HASH,
        "assertion_results": assertions,
        "verdicts": receipt_verdicts,
        "owner_links": _evaluation_owner_links(
            run_id,
            case_id,
            [
                item["verdict_id"]
                for item in receipt_verdicts.values()
                if item["verdict"] == "pass"
            ],
        ),
    }


def resolve_feedback_regression(
    conn,
    *,
    org_id: str,
    project_id: str,
    feedback_id: str,
    actor: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    command = _exact_payload(payload, _RESOLVE_KEYS, "feedback-regression-resolve.v1")
    actor = _text(actor, "actor", 200)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"feedback-regression-resolve:{org_id}:{project_id}:{feedback_id}",),
        )
        cur.execute(
            """
            SELECT r.id, r.regression_case_id, r.evaluation_case_id,
                   r.evaluation_run_id, r.verdict_id
              FROM app.feedback_regression_resolutions r
              JOIN app.feedback_regression_cases p
                ON p.id = r.regression_case_id AND p.org_id = r.org_id
               AND p.project_id = r.project_id AND p.feedback_id = r.feedback_id
             WHERE r.regression_case_id = %s AND r.feedback_id = %s
               AND r.evaluation_case_id = %s AND r.org_id = %s AND r.project_id = %s
            """,
            (
                command["regression_case_id"],
                feedback_id,
                command["evaluation_case_id"],
                org_id,
                project_id,
            ),
        )
        replay = cur.fetchone()
    if replay:
        return {
            "schema_version": "feedback-regression-resolution.v1",
            "status": "resolved",
            "resolution_id": replay[0],
            "regression_case_id": replay[1],
            "evaluation_case_id": replay[2],
            "evaluation_run_id": replay[3],
            "verdict_id": replay[4],
            "reason": None,
            "owner_links": _evaluation_owner_links(replay[3], replay[2], [replay[4]]),
        }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.review_version_id, p.golden_question_version_id,
                   rv.affected_dimension, p.result_id, p.result_content_hash,
                   p.result_classification_hash, rv.severity
              FROM app.feedback_regression_cases p
              JOIN app.feedback_reviews head
                ON head.feedback_id = p.feedback_id AND head.org_id = p.org_id
               AND head.project_id = p.project_id
               AND head.current_review_version_id = p.review_version_id
              JOIN app.feedback_review_versions rv
                ON rv.id = p.review_version_id AND rv.feedback_id = p.feedback_id
               AND rv.org_id = p.org_id AND rv.project_id = p.project_id
             WHERE p.id = %s AND p.feedback_id = %s AND p.org_id = %s AND p.project_id = %s
            """,
            (command["regression_case_id"], feedback_id, org_id, project_id),
        )
        promotion = cur.fetchone()
    if promotion is None:
        raise FeedbackRegressionNotFound("current feedback promotion not found")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.run_id, r.lifecycle, r.evidence_mode,
                   c.golden_question_version_id, c.result_id, qr.content_hash,
                   c.result_classification_hash, v.id, v.verdict, v.producer,
                   v.producer_contract_hash
              FROM app.evaluation_run_cases c
              JOIN app.evaluation_runs r
                ON r.id = c.run_id AND r.org_id = c.org_id AND r.project_id = c.project_id
              JOIN app.query_results qr
                ON qr.id = c.result_id AND qr.org_id = c.org_id AND qr.project_id = c.project_id
              JOIN app.evaluation_case_dimension_verdicts v
                ON v.case_id = c.id AND v.org_id = c.org_id AND v.project_id = c.project_id
               AND v.dimension = %s
             WHERE c.id = %s AND c.org_id = %s AND c.project_id = %s
            """,
            (promotion[3], command["evaluation_case_id"], org_id, project_id),
        )
        evidence = cur.fetchone()
    trusted = bool(
        promotion[3] != "context_adherence"
        and evidence
        and evidence[2] == "finalized"
        and evidence[3] == "offline"
        and evidence[4] == promotion[2]
        and evidence[5] == promotion[4]
        and evidence[6] == promotion[5]
        and evidence[7] == promotion[6]
        and evidence[9] == "pass"
        and evidence[10] == RESULT_CASE_EVALUATOR
        and evidence[11] == RESULT_CASE_EVALUATOR_HASH
    )
    if not trusted:
        reason = (
            "context_skill_evidence_unavailable"
            if promotion[3] == "context_adherence"
            else "trusted_pass_unavailable"
        )
        if evidence and promotion[3] != "context_adherence":
            if evidence[10] != RESULT_CASE_EVALUATOR or evidence[11] != RESULT_CASE_EVALUATOR_HASH:
                reason = "trusted_producer_required"
            elif evidence[9] != "pass":
                reason = "affected_dimension_not_passed"
            else:
                reason = "trusted_pass_incompatible"
        return {
            "schema_version": "feedback-regression-resolution.v1",
            "status": "unresolved",
            "resolution_id": None,
            "regression_case_id": promotion[0],
            "evaluation_case_id": command["evaluation_case_id"],
            "evaluation_run_id": evidence[1] if evidence else None,
            "verdict_id": evidence[8] if evidence else None,
            "reason": reason,
            "owner_links": (
                _evaluation_owner_links(
                    evidence[1],
                    evidence[0],
                    [evidence[8]] if evidence[9] == "pass" else [],
                )
                if evidence
                else []
            ),
        }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, regression_case_id, evaluation_case_id,
                   evaluation_run_id, verdict_id
              FROM app.feedback_regression_resolutions
             WHERE regression_case_id = %s
            """,
            (promotion[0],),
        )
        existing = cur.fetchone()
    if existing:
        return {
            "schema_version": "feedback-regression-resolution.v1",
            "status": "resolved",
            "resolution_id": existing[0],
            "regression_case_id": existing[1],
            "evaluation_case_id": existing[2],
            "evaluation_run_id": existing[3],
            "verdict_id": existing[4],
            "reason": None,
            "owner_links": _evaluation_owner_links(existing[3], existing[2], [existing[4]]),
        }
    resolution_id = f"frr_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.feedback_regression_resolutions
                (id, regression_case_id, feedback_id, org_id, project_id,
                 review_version_id, golden_question_version_id,
                 evaluation_run_id, evaluation_case_id, verdict_id,
                 affected_dimension, result_id, result_content_hash,
                 result_classification_hash, resolved_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            """,
            (
                resolution_id,
                promotion[0],
                feedback_id,
                org_id,
                project_id,
                promotion[1],
                promotion[2],
                evidence[1],
                evidence[0],
                evidence[8],
                promotion[3],
                promotion[4],
                promotion[5],
                promotion[6],
                actor,
            ),
        )
    append_review_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        reviewer=actor,
        payload={
            "schema_version": "feedback-review-command.v1",
            "expected_head": promotion[1],
            "retry_key": f"feedback-regression-resolution:{promotion[0]}:{evidence[0]}",
            "state": "resolved",
            "affected_dimension": promotion[3],
            "human_verdict": "fail",
            "severity": promotion[7],
            "reason": "Resolved by an exact trusted regression evaluation pass.",
        },
    )
    return {
        "schema_version": "feedback-regression-resolution.v1",
        "status": "resolved",
        "resolution_id": resolution_id,
        "regression_case_id": promotion[0],
        "evaluation_case_id": evidence[0],
        "evaluation_run_id": evidence[1],
        "verdict_id": evidence[8],
        "reason": None,
        "owner_links": _evaluation_owner_links(evidence[1], evidence[0], [evidence[8]]),
    }
