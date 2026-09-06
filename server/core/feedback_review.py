"""Story 51.5 -- the User Feedback evidence mode: pinned annotations and review.

WHAT THIS OWNS. One annotation object that pins the exact immutable observation a
person reacted to, and an append-only review that classifies it against exactly
one objective dimension. Nothing else. `analyze-and-test.md:216` states the whole
blocking contract of this evidence mode: feedback *"prioritizes investigation;
never proves correctness or regression alone"*. No function here returns a figure
that reads as correctness, and no response merges a human judgement with a
machine one.

WHY `app.feedback` (migration 012) IS NOT THIS. That table pins `module`,
`report_ref` and `trace_id`. None of the four objects the ratified contract
requires -- the exact Result, the rendered artifact, the AI Path and the
interaction -- can be recovered from those three strings, so an aggregate built
on them cannot state which version it describes. The legacy table stays readable
and is never backfilled: fabricating pins onto rows that never carried them would
turn an honest gap into false evidence.

THE THREE ABSENCES, DECLARED AND NOT FILLED. The rendered artifact (Stories 50.4
/ 50.5 / 50.7), the Visualization Spec datum/mark (Story 50.4) and the evaluated
MCP App behaviour (Story 50.6) do not exist in this repository. Their pins are
declared by migration 153, held NULL by CHECK, and never written from here. Every
lens over them answers ``unverifiable`` with a reason code and the story that
will lift it -- never ``pass``, never ``fail``, and never a neutral zero folded
into a count.

WHERE ASSESSMENT LIVES. A human review verdict is stored, because a person wrote
it at a moment and rewriting it would destroy the only evidence that the
judgement was ever different. Everything else -- severity escalation, "unresolved
critical negative", coverage, every aggregate figure -- is DERIVED at read time
from the pinned rows, for the reason migration 150 gives in its own header: a
stored derivation makes a historical record re-judge itself under today's policy.

WHAT IS NOT HERE, deliberately. No evaluation-run field, no machine judgement of
any kind, no score, no percentage, no Golden Question write, no Context Hub or
Governance write. A seed is a recorded *request*; a deep link is a link
(`analyze-and-test.md:370`).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ulid import ULID

from core import business_identity_catalogue as catalogue
from core.ai_paths import NO_AI_PATH, AiPathError, ai_path_reference
from core.audit import declare_action, insert_audit_row

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : elles etaient retapees en dur a l appel, donc rien
# ne pouvait distinguer une action d une faute de frappe. Declarees ici,
# a cote du code qui les ecrit.
ACTION_FEEDBACK_ANNOTATION_CREATED = declare_action("feedback.annotation.created")
ACTION_FEEDBACK_REVIEW_APPENDED = declare_action("feedback.review.appended")


# ---------------------------------------------------------------------------
# Closed vocabularies. Each one is the database CHECK restated in Python so a
# caller is refused with a named reason instead of a raw integrity error -- the
# database stays the guarantee, this is only the message.
# ---------------------------------------------------------------------------

#: `analyze-and-test.md:302` -- an annotation is positive or negative. There is
#: no neutral: a reaction nobody expressed is an absent row, not a third value.
POLARITIES = ("positive", "negative")

#: Who produced the annotation. Never a machine producer: an annotation is a
#: human judgement by definition (`analyze-and-test.md:306`).
ACTOR_SOURCES = ("user", "reviewer")

#: `share` is Story 50.7's to add. Declaring it before its owner exists would
#: let a surface that cannot yet be pinned record evidence as if it could.
OBSERVED_SURFACES = ("console", "mcp_app")

#: `analyze-and-test.md:239` -- the declared output contract of a Result.
RESULT_TYPES = ("scalar", "series", "breakdown", "comparison", "table", "narrative", "refusal")

#: The ratified six of `analyze-and-test.md:319-326`, plus `not_applicable` for
#: an annotation that judges none of them. No seventh dimension exists, and none
#: is scored on tone, fluency, eloquence or layout taste (`:330-334`).
AFFECTED_DIMENSIONS = (
    "semantic_correctness",
    "provenance_correctness",
    "context_adherence",
    "path_quality",
    "dq_handling",
    "mcp_app_behavior",
    "not_applicable",
)

#: `analyze-and-test.md:282`. `unverifiable` is a real verdict, not a soft fail,
#: and `not_applicable` is not a silent pass.
HUMAN_VERDICTS = ("pass", "fail", "unverifiable", "not_applicable")

SEVERITIES = ("critical", "major", "minor")

REVIEW_STATES = ("unreviewed", "triaged", "accepted", "rejected", "duplicate", "resolved")

#: A negative whose review reached one of these is no longer an open lead. The
#: set is stated once here so the aggregate and the lead list cannot drift.
CLOSED_REVIEW_STATES = frozenset({"resolved", "rejected", "duplicate"})

#: Values that parse as a version but pin nothing. `analyze-and-test.md:230`
#: requires an exact identity, and `app.is_exact_version_pin` (migration 153) is
#: the database counterpart of this same refusal.
UNQUALIFIED_VERSION_WORDS = frozenset({"latest", "current", "head"})

#: The only seed target this story knows. Story 51.1 owns the object itself.
SEED_TARGET_KINDS = ("golden_question",)

_MAX_COMMENT = 4000
_MAX_REASON = 2000
_MAX_SHORT_TEXT = 200
_TRACE_ID_LENGTH = 32
_HEX_DIGITS = frozenset("0123456789abcdef")
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Declared absences. Each one names the exact reason code and the story that
# will lift it. A placeholder owner ("a future story", "TBD") is what made
# Story 49.6 unreviewable: an absence nobody owns is indistinguishable from an
# absence nobody noticed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredAbsence:
    """A pin that is declared by the schema and cannot be filled yet."""

    reason: str
    owner_stories: tuple[str, ...]
    detail: str

    def as_lens(self) -> dict[str, Any]:
        """The read-model shape. `state` is `unverifiable`; never pass, never fail."""
        return {
            "state": "unverifiable",
            "reason": self.reason,
            "owner_stories": list(self.owner_stories),
            "detail": self.detail,
        }


RENDER_ABSENT = DeclaredAbsence(
    reason="render_owner_not_delivered",
    owner_stories=("50.4", "50.5", "50.7"),
    detail=(
        "The rendered-artifact object does not exist in this repository and the "
        "render stack is not installed. Its pin is declared by migration 153 and "
        "held NULL, so no fidelity claim about a rendered artifact can be made."
    ),
)

DATUM_MARK_ABSENT = DeclaredAbsence(
    reason="visualization_spec_owner_not_delivered",
    owner_stories=("50.4",),
    detail=(
        "A datum/mark claim needs a Visualization Spec to resolve against. That "
        "owner is not delivered, so the pin is declared and left unset."
    ),
)

MCP_APP_BEHAVIOR_ABSENT = DeclaredAbsence(
    reason="mcp_app_evidence_owner_not_delivered",
    owner_stories=("50.6",),
    detail=(
        "Render/Result parity, drill-down and host fallback have no server-owned "
        "evidence yet, so the MCP App behaviour dimension carries no machine "
        "evidence. A reviewer may still record a human verdict on it; the two are "
        "reported under different names and never merged."
    ),
)

PATH_EVIDENCE_ABSENT = DeclaredAbsence(
    reason="server_owned_path_evidence_absent",
    owner_stories=("49.6",),
    detail=(
        "app.ai_paths exists and core.ai_paths is its sole writer, but no caller "
        "records a path yet, so this observation honestly carries no path. A bare "
        "adherence claim without server-owned path evidence is Unverifiable "
        "(analyze-and-test.md:284)."
    ),
)

RESULT_TYPE_AXIS_ABSENT = DeclaredAbsence(
    reason="result_type_contract_owner_not_delivered",
    owner_stories=("50.4",),
    detail=(
        "The Result's declared output contract has no owner yet, so `result_type` "
        "is whatever the annotating surface stated. The axis reports its own "
        "coverage rather than presenting itself as attributed evidence."
    ),
)

CONTEXT_VERSION_SET_ABSENT = DeclaredAbsence(
    reason="context_version_set_owner_not_delivered",
    owner_stories=("51.2",),
    detail=(
        "The Context Version Set resolved for an execution is Story 51.2's object. "
        "Without it there is no exact context identity to deep-link to, so no "
        "Context Hub link is offered rather than one that points at `latest`."
    ),
)

ELIGIBLE_DENOMINATOR_ABSENT = DeclaredAbsence(
    reason="eligible_denominator_not_derivable_from_result",
    owner_stories=("50.4",),
    detail=(
        "Business Domain, capability and result type are stated by the annotating "
        "surface, not carried by the Result. The set of Results that COULD have "
        "been annotated on those axes therefore cannot be counted, so coverage is "
        "reported as unverifiable instead of divided by a number nobody can state."
    ),
)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class FeedbackNotFound(LookupError):
    """The annotation, Result or review is not in the authorized Project.

    Foreign, denied and nonexistent raise the SAME error, so the transport cannot
    become an enumeration oracle. Which of the three it was belongs in audit.
    """


@dataclass(frozen=True)
class Refusal:
    """One named, actionable reason a request was not accepted."""

    code: str
    message: str
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "subject": self.subject}


class FeedbackRefused(ValueError):
    """A structured refusal carrying every reason, not only the first."""

    def __init__(self, code: str, message: str, refusals: Sequence[Refusal] | None = None):
        super().__init__(message)
        self.code = code
        self.refusals = list(refusals or [])

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "refusals": [r.as_dict() for r in self.refusals],
        }


# ---------------------------------------------------------------------------
# Small validators.
# ---------------------------------------------------------------------------


def _mint(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    """sha256 over the NORMALIZED document, so two equal snapshots hash equal."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _unavailable(reason: str, **context: Any) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason, **context}


def _optional_one(conn, savepoint: str, sql: str, params: Sequence[Any]):
    """Run optional authority SQL without leaving the caller transaction aborted."""
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {savepoint}")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        return row
    except Exception:  # noqa: BLE001
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        return None


def _optional_many(conn, savepoint: str, sql: str, params: Sequence[Any]):
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {savepoint}")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        return rows
    except Exception:  # noqa: BLE001
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        return None


def freeze_result_classification(
    conn,
    *,
    org_id: str,
    project_id: str,
    query_spec_version_id: str,
    outcome: str,
    ai_path_id: str | None,
) -> dict[str, Any]:
    """Resolve the server-owned classification frozen into a Result manifest.

    Missing owners are data, not execution failures.  The portable hash covers
    the document without its own hash field, allowing Python and the Share SQL
    definer to consume the same immutable identity.
    """
    semantic_view: dict[str, Any] = _unavailable("query_spec_unavailable")
    domains: dict[str, Any] = _unavailable("semantic_view_domains_unavailable")
    capability: dict[str, Any] = _unavailable("capability_not_required")
    result_type: dict[str, Any] = (
        {"state": "attributed", "value": "refusal"}
        if outcome == "refused"
        else _unavailable("result_type_not_published")
    )
    spec: Mapping[str, Any] = {}
    domain_refs: list[str] = []
    row = _optional_one(
        conn,
        "feedback_classification_query_spec",
        """
        SELECT qsv.semantic_view_id, qsv.semantic_view_version_id,
               qsv.spec, svv.business_domain_refs
        FROM app.query_spec_versions qsv
        JOIN app.semantic_view_versions svv
          ON svv.id = qsv.semantic_view_version_id
         AND svv.view_id = qsv.semantic_view_id
         AND svv.project_id = qsv.project_id
        WHERE qsv.id = %s AND qsv.org_id = %s AND qsv.project_id = %s
        """,
        (query_spec_version_id, org_id, project_id),
    )
    if row is not None:
        semantic_view = {
            "state": "attributed",
            "id": str(row[0]),
            "version_id": str(row[1]),
        }
        spec = row[2] if isinstance(row[2], Mapping) else {}
        if isinstance(row[3], list) and all(isinstance(value, str) for value in row[3]):
            domain_refs = sorted(set(row[3]))

    if row is not None:
        analysis_context = spec.get("analysis_context")
        pinned_domain = (
            analysis_context.get("business_domain")
            if isinstance(analysis_context, Mapping)
            else None
        )
        if isinstance(pinned_domain, Mapping):
            pinned_id = pinned_domain.get("id")
            pinned_version = pinned_domain.get("version_number")
            if (
                isinstance(pinned_id, str)
                and pinned_id in domain_refs
                and isinstance(pinned_version, int)
                and pinned_version >= 1
            ):
                resolved = _optional_one(
                    conn,
                    "feedback_classification_pinned_domain",
                    f"""
                    SELECT 1
                    FROM {catalogue.DOMAIN_VERSION_SOURCE} v
                    WHERE v.org_id = %s AND v.domain_id = %s
                      AND v.version_number = %s
                    """,
                    (org_id, pinned_id, pinned_version),
                )
                domains = (
                    {
                        "state": "attributed",
                        "versions": [{"id": pinned_id, "version_number": pinned_version}],
                    }
                    if resolved is not None
                    else _unavailable("business_domain_version_unavailable")
                )
            else:
                domains = _unavailable("business_domain_version_unavailable")
        elif not domain_refs:
            domains = {"state": "attributed", "versions": []}
        else:
            # Story 49.2: the attributed Business Domains resolve through the
            # authority. Read from the superseded ledger alone, a classification
            # attributed to a natively minted domain came back with FEWER rows
            # than it asked for -- and the equality below then answered
            # `business_domain_version_unavailable`, which reads as an outage
            # rather than as an identity this store simply never carried.
            resolved = _optional_many(
                conn,
                "feedback_classification_domains",
                f"""
                SELECT v.domain_id, MAX(v.version_number)
                FROM {catalogue.DOMAIN_VERSION_SOURCE} v
                JOIN {catalogue.DOMAIN_SOURCE} d
                  ON d.id = v.domain_id AND d.org_id = v.org_id
                WHERE v.org_id = %s AND v.domain_id = ANY(%s)
                GROUP BY v.domain_id
                ORDER BY v.domain_id
                """,
                (org_id, domain_refs),
            )
            if resolved is not None:
                if [str(item[0]) for item in resolved] == domain_refs:
                    domains = {
                        "state": "attributed",
                        "versions": [
                            {"id": str(item[0]), "version_number": int(item[1])}
                            for item in resolved
                        ],
                    }
                else:
                    domains = _unavailable("business_domain_version_unavailable")
            else:
                domains = _unavailable("business_domain_version_unavailable")

        requirement = spec.get("required_capability")
        if isinstance(requirement, Mapping) and isinstance(requirement.get("key"), str):
            key = str(requirement["key"])
            capability_row = _optional_one(
                conn,
                "feedback_classification_capability",
                """
                SELECT active_version_id
                FROM app.project_capabilities
                WHERE project_id = %s AND capability_key = %s
                """,
                (project_id, key),
            )
            if capability_row is not None:
                if capability_row is not None and capability_row[0] is not None:
                    capability = {
                        "state": "attributed",
                        "key": key,
                        "version_id": str(capability_row[0]),
                    }
                else:
                    capability = _unavailable(
                        "active_capability_version_unavailable", key=key
                    )
            else:
                capability = _unavailable("active_capability_version_unavailable", key=key)

        published_type = spec.get("result_type")
        shape = spec.get("result_shape") or "tabular_v1"
        descriptor = spec.get("result_shape_descriptor")
        if published_type is None and isinstance(descriptor, Mapping):
            published_type = descriptor.get("result_type")
        value: str | None = None
        if outcome == "refused":
            value = "refusal"
        elif published_type in RESULT_TYPES:
            value = str(published_type)
        elif shape == "tabular_v1":
            value = "table"
        elif shape == "waterfall_v1":
            value = "comparison"
        if value is not None:
            result_type = {"state": "attributed", "value": value}

    skills: dict[str, Any] = _unavailable("result_has_no_ai_path")
    if ai_path_id:
        path = _optional_one(
            conn,
            "feedback_classification_path",
            """
            SELECT lifecycle FROM app.ai_paths
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (ai_path_id, org_id, project_id),
        )
        if path is not None and path[0] == "finalized":
            skill_rows = _optional_many(
                conn,
                "feedback_classification_skills",
                """
                SELECT DISTINCT skill_version_id
                FROM app.ai_path_steps
                WHERE path_id = %s AND org_id = %s AND project_id = %s
                  AND skill_version_id IS NOT NULL
                ORDER BY skill_version_id
                """,
                (ai_path_id, org_id, project_id),
            )
            if skill_rows is not None:
                versions = sorted({str(item[0]) for item in skill_rows})
                skills = {"state": "attributed", "versions": versions}
            else:
                skills = _unavailable("ai_path_unavailable")
        elif path is None:
            skills = _unavailable("ai_path_unavailable")
        else:
            skills = _unavailable("ai_path_not_finalized")

    document = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": semantic_view,
        "business_domains": domains,
        "skills": skills,
        "capability": capability,
        "result_type": result_type,
    }
    return {**document, "classification_hash": canonical_hash(document)}


def result_classification_hash_from_manifest(manifest: Any) -> str | None:
    """Return a Result-owned classification hash only when its bytes verify."""
    candidate = manifest.get("evaluation_classification") if isinstance(manifest, Mapping) else None
    if (
        isinstance(candidate, Mapping)
        and candidate.get("schema_version") == "evaluation-classification.v1"
        and isinstance(candidate.get("classification_hash"), str)
        and candidate["classification_hash"]
        == canonical_hash(
            {key: value for key, value in candidate.items() if key != "classification_hash"}
        )
    ):
        return str(candidate["classification_hash"])
    return None


def _classification_from_manifest(manifest: Any) -> dict[str, Any]:
    candidate = manifest.get("evaluation_classification") if isinstance(manifest, Mapping) else None
    if result_classification_hash_from_manifest(manifest) is not None:
        return dict(candidate)
    document = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": _unavailable("historical_result_unclassified"),
        "business_domains": _unavailable("historical_result_unclassified"),
        "skills": _unavailable("historical_result_unclassified"),
        "capability": _unavailable("historical_result_unclassified"),
        "result_type": _unavailable("historical_result_unclassified"),
    }
    return {**document, "classification_hash": canonical_hash(document)}


def _compatibility_preimage(
    *,
    claims: Mapping[str, Any],
    source: str,
    query_spec_version_id: str,
    classification: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "feedback-compatibility.v1",
        "target_schema_version": "exact-feedback.v1",
        "source": source,
        "surface": claims.get("surface"),
        "query_spec_version_id": query_spec_version_id,
        "semantic_view": classification.get("semantic_view"),
        "business_domains": classification.get("business_domains"),
        "skills": classification.get("skills"),
        "capability": classification.get("capability"),
        "result_type": classification.get("result_type"),
        "render": {
            key: claims.get(key)
            for key in (
                "visualization_spec_version_id",
                "renderer_build_id",
                "runtime_build_id",
                "theme_version",
                "formatter_version",
            )
        },
    }


def _eligible_target_kinds(claims: Mapping[str, Any]) -> list[str]:
    window = claims.get("delivered_rows")
    count = window.get("count") if isinstance(window, Mapping) else None
    field_count = window.get("field_count") if isinstance(window, Mapping) else None
    ordinals = claims.get("path_step_ordinals")
    kinds = ["answer"]
    if isinstance(count, int) and count > 0 and isinstance(field_count, int) and field_count > 0:
        kinds.append("datum")
    if isinstance(ordinals, list) and ordinals:
        kinds.append("path_step")
    return kinds


def record_feedback_eligibility(
    conn,
    *,
    claims: Mapping[str, Any],
    source: str = "authenticated",
) -> dict[str, Any] | None:
    """Record one delivered feedback-capable interaction under a savepoint."""
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT feedback_eligibility")
        if source != "authenticated" or claims.get("schema_version") != "exact-feedback.v1":
            raise ValueError("invalid feedback eligibility source")
        org_id = _required_text(claims.get("org_id"), "org_id")
        project_id = _required_text(claims.get("project_id"), "project_id")
        interaction_ref = _required_text(claims.get("interaction_ref"), "interaction_ref")
        result_id = _required_text(claims.get("result_id"), "result_id")
        result_hash = _required_text(
            claims.get("result_content_hash"), "result_content_hash", maximum=64
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT qr.content_hash, qr.ai_path_id, qr.query_spec_version_id, p.manifest
                FROM app.query_results qr
                JOIN app.query_result_payloads p
                  ON p.result_id = qr.id AND p.org_id = qr.org_id
                 AND p.project_id = qr.project_id AND p.content_hash = qr.content_hash
                WHERE qr.id = %s AND qr.org_id = %s AND qr.project_id = %s
                  AND qr.content_hash = %s
                """,
                (result_id, org_id, project_id, result_hash),
            )
            result = cur.fetchone()
        if result is None or result[1] != claims.get("ai_path_id"):
            raise ValueError("feedback result unavailable")
        classification = _classification_from_manifest(result[3])
        authority = {
            "delivered_rows": claims.get("delivered_rows"),
            "ai_path_id": claims.get("ai_path_id"),
            "path_step_ordinals": claims.get("path_step_ordinals"),
            "target_kinds": _eligible_target_kinds(claims),
        }
        preimage = _compatibility_preimage(
            claims=claims,
            source=source,
            query_spec_version_id=str(result[2]),
            classification=classification,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.feedback_eligible_observations
                    (id, org_id, project_id, source, observed_surface, interaction_ref,
                     result_id, result_content_hash, render_ref,
                     visualization_spec_version_id, renderer_build_id, runtime_build_id,
                     theme_version, formatter_version, authority, classification,
                     classification_hash, compatibility_preimage, compatibility_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s, %s::jsonb, %s)
                ON CONFLICT (org_id, project_id, source, interaction_ref) DO NOTHING
                """,
                (
                    _mint("fbe"),
                    org_id,
                    project_id,
                    source,
                    claims.get("surface"),
                    interaction_ref,
                    result_id,
                    result_hash,
                    claims.get("render_id"),
                    claims.get("visualization_spec_version_id"),
                    claims.get("renderer_build_id"),
                    claims.get("runtime_build_id"),
                    claims.get("theme_version"),
                    claims.get("formatter_version"),
                    _canonical_json(authority),
                    _canonical_json(classification),
                    classification["classification_hash"],
                    _canonical_json(preimage),
                    canonical_hash(preimage),
                ),
            )
            cur.execute(
                """
                SELECT id, classification_hash, compatibility_key,
                       observed_surface, render_ref, visualization_spec_version_id,
                       renderer_build_id, runtime_build_id, theme_version,
                       formatter_version, authority, classification,
                       compatibility_preimage
                FROM app.feedback_eligible_observations
                WHERE org_id = %s AND project_id = %s AND source = %s
                  AND interaction_ref = %s AND result_id = %s AND result_content_hash = %s
                """,
                (org_id, project_id, source, interaction_ref, result_id, result_hash),
            )
            stored = cur.fetchone()
            expected_replay = (
                claims.get("surface"),
                claims.get("render_id"),
                claims.get("visualization_spec_version_id"),
                claims.get("renderer_build_id"),
                claims.get("runtime_build_id"),
                claims.get("theme_version"),
                claims.get("formatter_version"),
                authority,
                classification,
                preimage,
            )
            if stored is None or tuple(stored[3:]) != expected_replay:
                raise ValueError("feedback eligibility replay conflict")
            cur.execute("RELEASE SAVEPOINT feedback_eligibility")
        return {
            "schema_version": "feedback-eligibility.v1",
            "interaction_ref": interaction_ref,
            "classification_hash": str(stored[1]),
            "compatibility_key": str(stored[2]),
        }
    except Exception:  # noqa: BLE001 - omission is the fail-closed contract
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT feedback_eligibility")
                cur.execute("RELEASE SAVEPOINT feedback_eligibility")
        except Exception:  # noqa: BLE001
            pass
        return None


def record_share_feedback_eligibility(
    conn, *, session_hash: str, interaction_ref: str
) -> dict[str, Any] | None:
    """Call the sole public Share eligibility door; never INSERT from Python."""
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT feedback_eligibility")
            cur.execute(
                "SELECT * FROM app.record_render_share_feedback_eligibility_v1(%s, %s)",
                (session_hash, interaction_ref),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("share feedback eligibility unavailable")
            cur.execute("RELEASE SAVEPOINT feedback_eligibility")
        return {
            "schema_version": "feedback-eligibility.v1",
            "interaction_ref": str(row[1]),
            "status": str(row[0]),
        }
    except Exception:  # noqa: BLE001
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT feedback_eligibility")
                cur.execute("RELEASE SAVEPOINT feedback_eligibility")
        except Exception:  # noqa: BLE001
            pass
        return None


def require_feedback_eligibility(
    conn,
    *,
    org_id: str,
    project_id: str,
    claims: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    """Resolve the delivery fact used by a feedback writer, or fail closed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT classification, classification_hash, compatibility_key, authority
            FROM app.feedback_eligible_observations
            WHERE org_id = %s AND project_id = %s AND source = %s
              AND interaction_ref = %s AND result_id = %s AND result_content_hash = %s
              AND observed_surface = %s
            """,
            (
                org_id,
                project_id,
                source,
                claims.get("interaction_ref"),
                claims.get("result_id"),
                claims.get("result_content_hash"),
                claims.get("surface"),
            ),
        )
        row = cur.fetchone()
    expected = {
        "delivered_rows": claims.get("delivered_rows"),
        "ai_path_id": claims.get("ai_path_id"),
        "path_step_ordinals": claims.get("path_step_ordinals"),
        "target_kinds": _eligible_target_kinds(claims),
    }
    if row is None or row[3] != expected:
        raise FeedbackNotFound("Feedback target not found")
    return {
        "classification": row[0],
        "classification_hash": row[1],
        "compatibility_key": row[2],
    }


def _required_text(value: Any, label: str, *, maximum: int = _MAX_SHORT_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeedbackRefused(
            "missing_field",
            f"{label} is required",
            [Refusal("missing_field", f"{label} is required", label)],
        )
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise FeedbackRefused(
            "invalid_field",
            f"{label} must be at most {maximum} characters",
            [Refusal("too_long", f"{label} must be at most {maximum} characters", label)],
        )
    return cleaned


def _optional_text(value: Any, label: str, *, maximum: int = _MAX_SHORT_TEXT) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, maximum=maximum)


def _one_of(value: Any, allowed: Sequence[str], label: str) -> str:
    candidate = _required_text(value, label)
    if candidate not in allowed:
        raise FeedbackRefused(
            "invalid_vocabulary",
            f"{label} must be one of: {', '.join(allowed)}",
            [Refusal("invalid_vocabulary", f"{candidate} is not a {label}", label)],
        )
    return candidate


def _validate_trace_id(value: Any) -> str | None:
    """32 lowercase hex, never all-zero -- the rule of migration 150:103-106.

    All-zero is the W3C *invalid* value: storing it would correlate every
    uninstrumented execution into one apparent trace.
    """
    if value is None:
        return None
    candidate = _required_text(value, "w3c_trace_id").lower()
    if len(candidate) != _TRACE_ID_LENGTH or any(c not in _HEX_DIGITS for c in candidate):
        raise FeedbackRefused(
            "invalid_trace_id",
            "w3c_trace_id must be 32 lowercase hexadecimal characters",
            [Refusal("invalid_trace_id", "expected 32 lowercase hex characters", "w3c_trace_id")],
        )
    if candidate == "0" * _TRACE_ID_LENGTH:
        raise FeedbackRefused(
            "invalid_trace_id",
            "w3c_trace_id must not be the all-zero invalid trace id",
            [
                Refusal(
                    "invalid_trace_id",
                    "the all-zero trace id is the W3C invalid value",
                    "w3c_trace_id",
                )
            ],
        )
    return candidate


def is_exact_version_pin(candidate: Any) -> bool:
    """The Python side of `app.is_exact_version_pin` (migration 153).

    A shared address that follows `latest` silently changes what it shows, so an
    unqualified word is refused wherever a version identity is expected.
    """
    if not isinstance(candidate, str):
        return False
    cleaned = candidate.strip()
    return bool(cleaned) and cleaned.lower() not in UNQUALIFIED_VERSION_WORDS


def _reject_unqualified_pins(document: Mapping[str, Any], label: str) -> None:
    """Refuse `latest` anywhere in a version snapshot or a filter set.

    Checked recursively rather than key by key: the snapshot is caller-shaped, so
    a rule that only inspected known keys would pass the day someone nests one.
    """
    offenders: list[Refusal] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str) and node.strip().lower() in UNQUALIFIED_VERSION_WORDS:
            offenders.append(
                Refusal(
                    "unqualified_version_pin",
                    f"{node.strip()} is not a version identity",
                    f"{label}.{path}" if path else label,
                )
            )

    walk(document, "")
    if offenders:
        raise FeedbackRefused(
            "unqualified_version_pin",
            f"{label} must carry exact version identities, never latest/current/head",
            offenders,
        )


def _as_object(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FeedbackRefused(
            "invalid_field",
            f"{label} must be an object",
            [Refusal("invalid_field", f"{label} must be an object", label)],
        )
    return dict(value)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _parse_timestamp(value: Any, label: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _required_text(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackRefused(
            "invalid_field",
            f"{label} must be an ISO-8601 timestamp",
            [Refusal("invalid_field", str(exc), label)],
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Owner links.
#
# The server emits a STRUCTURED pointer -- workspace, section, object type,
# object id, version id -- and never a URL string. `ui/admin/src/shell/router.tsx`
# owns the address grammar; a second grammar assembled here would keep producing
# yesterday's links the day the shell canonicalizes differently.
# ---------------------------------------------------------------------------


def _owner_link(
    workspace: str,
    section: str,
    object_type: str,
    object_id: str,
    version_id: str | None = None,
    *,
    tab: str | None = None,
) -> dict[str, Any]:
    return {
        "workspace": workspace,
        "section": section,
        "object_type": object_type,
        "object_id": object_id,
        "version_id": version_id,
        "tab": tab,
    }


def _owner_links(pins: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Deep links built ONLY from identities this annotation actually pinned."""
    links = [
        _owner_link("analyze", "explore", "result", str(pins["result_id"])),
        _owner_link(
            "governance",
            "semantic-model",
            "semantic-view",
            str(pins["semantic_view_id"]),
            str(pins["semantic_view_version_id"]),
            tab="versions",
        ),
    ]
    if pins.get("query_spec_id") and pins.get("query_spec_version_id"):
        links.append(
            _owner_link(
                "analyze",
                "explore",
                "query-spec",
                str(pins["query_spec_id"]),
                str(pins["query_spec_version_id"]),
                tab="query",
            )
        )
    classification = _row_classification(pins)
    domain_versions = classification.get("business_domains", {}).get("versions", [])
    for domain in domain_versions if isinstance(domain_versions, list) else []:
        if not isinstance(domain, Mapping) or not domain.get("id"):
            continue
        version_number = domain.get("version_number")
        if (
            not isinstance(version_number, int)
            or isinstance(version_number, bool)
            or version_number < 1
        ):
            continue
        links.append(
            _owner_link(
                "governance",
                "master-data",
                "business-domain",
                str(domain["id"]),
                f"{domain['id']}:{version_number}",
                tab="versions",
            )
        )
    if pins.get("render_ref"):
        links.append(
            _owner_link("analyze", "renders", "render", str(pins["render_ref"]))
        )
    if pins.get("ai_path_id"):
        links.append(
            _owner_link(
                "test",
                "regression-runs",
                "trace-observation",
                str(pins["ai_path_id"]),
                tab="timeline",
            )
        )
    return links


def _unavailable_owner_links(pins: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Links a reviewer will look for and that cannot be built honestly today."""
    missing = []
    if not pins.get("render_ref"):
        missing.append({"target": "render", **RENDER_ABSENT.as_lens()})
    missing.append({"target": "context-hub", **CONTEXT_VERSION_SET_ABSENT.as_lens()})
    if not pins.get("ai_path_id"):
        missing.append({"target": "ai-path", **PATH_EVIDENCE_ABSENT.as_lens()})
    classification = _row_classification(pins)
    capability = classification.get("capability")
    if (
        isinstance(capability, Mapping)
        and capability.get("state") == "attributed"
        and capability.get("version_id")
    ):
        missing.append(
            {
                "target": "capability",
                "state": "unverifiable",
                "reason": "capability_version_route_unavailable",
                "owner_stories": [],
                "detail": (
                    "The classification pins an exact capability version, but the shell has no "
                    "version-addressable capability object. No moving capability link is emitted."
                ),
            }
        )
    skills = classification.get("skills")
    if (
        isinstance(skills, Mapping)
        and skills.get("state") == "attributed"
        and skills.get("versions")
    ):
        missing.append(
            {
                "target": "skill-versions",
                "state": "unverifiable",
                "reason": "skill_version_parent_route_unavailable",
                "owner_stories": [],
                "detail": (
                    "The Result pins Skill version ids but not their route parent Skill ids, so "
                    "an exact Skills Registry link cannot be constructed."
                ),
            }
        )
    if pins.get("visualization_spec_version_id"):
        missing.append(
            {
                "target": "visualization-spec-version",
                "state": "unverifiable",
                "reason": "visualization_spec_version_route_unavailable",
                "owner_stories": [],
                "detail": (
                    "The exact Visualization Spec version is pinned, but Analyze / Renders has "
                    "no Visualization Spec object route. No inert owner link is emitted."
                ),
            }
        )
    domains = classification.get("business_domains")
    if not isinstance(domains, Mapping) or domains.get("state") != "attributed":
        missing.append(
            {
                "target": "business-domain",
                "state": "unverifiable",
                "reason": "business_domain_not_attributed",
                "owner_stories": [],
                "detail": (
                    "The Result classification states no exact Business Domain owner, "
                    "so it cannot be "
                    "linked to one. It counts in the `unattributed` bucket of that "
                    "axis rather than being assigned a default owner."
                ),
            }
        )
    return missing


# ---------------------------------------------------------------------------
# Resolution helpers -- every one of them Project-scoped. No function below
# trusts a bare id from a caller.
# ---------------------------------------------------------------------------


def _resolve_result(conn, *, org_id: str, project_id: str, result_id: str) -> dict[str, Any]:
    """The pinned observation, with the Semantic View DERIVED from its own owner.

    Derived rather than copied: a Semantic View version stored on the annotation
    could disagree with the Result's the moment either side is written by a
    second path, and then nobody could say which one the person actually saw.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT qr.id, qr.outcome, qr.query_spec_version_id,
                   qr.ai_path_id, qr.ai_path_absent_literal,
                   qsv.semantic_view_id, qsv.semantic_view_version_id,
                   qsv.query_spec_id
            FROM app.query_results qr
            JOIN app.query_spec_versions qsv
              ON qsv.id = qr.query_spec_version_id
             AND qsv.org_id = qr.org_id
             AND qsv.project_id = qr.project_id
            WHERE qr.id = %s AND qr.org_id = %s AND qr.project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise FeedbackNotFound("Result not found")
    return {
        "result_id": row[0],
        "outcome": row[1],
        "query_spec_version_id": row[2],
        "result_ai_path_id": row[3],
        "result_ai_path_absent_literal": row[4],
        "semantic_view_id": row[5],
        "semantic_view_version_id": row[6],
        "query_spec_id": row[7],
    }


def _refuse_capability_that_is_a_connector(
    conn, *, project_id: str, capability: str | None
) -> None:
    """`analyze-and-test.md:237` -- capability *"is not a connector name"*.

    This is where that sentence becomes enforceable: an installed connector name
    accepted as a capability would make the aggregate axis describe a data source
    rather than the product capability exercised, and the two are not the same
    question.
    """
    if capability is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.project_modules WHERE project_id = %s AND module_name = %s",
            (project_id, capability),
        )
        clash = cur.fetchone()
    if clash is not None:
        raise FeedbackRefused(
            "capability_is_a_connector_name",
            "capability is the governed product capability exercised, not a connector name",
            [
                Refusal(
                    "capability_is_a_connector_name",
                    f"{capability} is an installed connector in this Project",
                    "capability",
                )
            ],
        )


def _resolve_business_domain(
    conn, *, org_id: str, domain_id: str | None, version_number: Any
) -> tuple[str | None, int | None]:
    """Both halves or neither. A domain id without its version pins nothing."""
    if domain_id is None and version_number is None:
        return (None, None)
    if domain_id is None or version_number is None:
        raise FeedbackRefused(
            "incomplete_version_pin",
            "business_domain_id and business_domain_version_number are required together",
            [
                Refusal(
                    "incomplete_version_pin",
                    "a Business Domain id without its version number is not a pin",
                    "business_domain",
                )
            ],
        )
    cleaned_id = _required_text(domain_id, "business_domain_id")
    try:
        cleaned_version = int(version_number)
    except (TypeError, ValueError) as exc:
        raise FeedbackRefused(
            "invalid_field",
            "business_domain_version_number must be an integer",
            [Refusal("invalid_field", "expected an integer", "business_domain_version_number")],
        ) from exc
    with conn.cursor() as cur:
        # Story 49.2: same authority-first ledger as the aggregate above. A pin on
        # an identity Master Data minted used to raise `FeedbackNotFound`.
        cur.execute(
            f"""
            SELECT 1
            FROM {catalogue.DOMAIN_VERSION_SOURCE} v
            JOIN {catalogue.DOMAIN_SOURCE} d ON d.id = v.domain_id AND d.org_id = %s
            WHERE v.domain_id = %s AND v.version_number = %s
            """,
            (org_id, cleaned_id, cleaned_version),
        )
        found = cur.fetchone()
    if found is None:
        # Foreign, denied and absent answer alike: an aggregate axis must not
        # become a way to enumerate another organization's taxonomy.
        raise FeedbackNotFound("Business Domain version not found")
    return (cleaned_id, cleaned_version)


def _resolve_ai_path_pin(
    conn, *, project_id: str, requested: Any, result: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """Exactly one of (path id, absent literal) -- the rule migration 151 encodes.

    The literal is never spelled here: :data:`core.ai_paths.NO_AI_PATH` is its
    only source, so a second wording cannot appear in the repository.
    """
    if requested is None:
        # Default to what the Result itself pinned: the person reacted to that
        # execution, and inventing a different path would be a second claim.
        if result.get("result_ai_path_id"):
            return (str(result["result_ai_path_id"]), None)
        return (None, NO_AI_PATH)

    candidate = _required_text(requested, "ai_path_id")
    if candidate == NO_AI_PATH:
        return (None, NO_AI_PATH)
    try:
        reference = ai_path_reference(conn, path_id=candidate, project_id=project_id)
    except AiPathError as exc:
        # A recording path can still grow steps; an annotation pinned to one
        # would describe evidence that changes after the fact.
        raise FeedbackRefused(
            "invalid_ai_path_pin",
            str(exc),
            [Refusal(getattr(exc, "code", "invalid_ai_path_pin"), str(exc), "ai_path_id")],
        ) from exc
    if reference == NO_AI_PATH:
        return (None, NO_AI_PATH)
    return (reference, None)


def _refuse_undeliverable_pins(payload: Mapping[str, Any]) -> None:
    """Refuse a caller that tries to supply a pin whose owner does not exist.

    Accepting and dropping it silently would be worse than refusing: the caller
    would believe a rendered artifact was recorded, and a later reader would find
    an annotation that claims render evidence it never had.
    """
    offenders: list[Refusal] = []
    for key in ("render_id", "render_ref"):
        if payload.get(key) is not None:
            offenders.append(Refusal(RENDER_ABSENT.reason, RENDER_ABSENT.detail, key))
    if payload.get("datum_mark") is not None:
        offenders.append(Refusal(DATUM_MARK_ABSENT.reason, DATUM_MARK_ABSENT.detail, "datum_mark"))
    if offenders:
        raise FeedbackRefused(
            "pin_owner_not_delivered",
            "this pin has no owner yet and is therefore refused rather than stored",
            offenders,
        )


# ---------------------------------------------------------------------------
# Create.
# ---------------------------------------------------------------------------


def create_annotation(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Retired Story 51 writer; Analytics callers must use exact-feedback.v1."""
    del conn, org_id, project_id, actor, payload
    raise FeedbackRefused(
        "exact_feedback_required",
        "Analytics feedback requires a signed exact-feedback.v1 context.",
        [],
    )


def _create_annotation_legacy_contract_for_tests(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Record one reaction, pinned to the exact immutable observation it judges.

    The caller owns the transaction: the annotation, its review head and the
    audit row commit together or not at all.
    """
    org_id = _required_text(org_id, "org_id")
    project_id = _required_text(project_id, "project_id")
    actor = _required_text(actor, "actor")

    _refuse_undeliverable_pins(payload)

    polarity = _one_of(payload.get("polarity"), POLARITIES, "polarity")
    actor_source = _one_of(payload.get("actor_source", "user"), ACTOR_SOURCES, "actor_source")
    observed_surface = _one_of(
        payload.get("observed_surface"), OBSERVED_SURFACES, "observed_surface"
    )
    comment = _optional_text(payload.get("comment"), "comment", maximum=_MAX_COMMENT)
    interaction_ref = _optional_text(payload.get("interaction_ref"), "interaction_ref")
    trace_id = _validate_trace_id(payload.get("w3c_trace_id"))
    observed_at = _parse_timestamp(payload.get("observed_at"), "observed_at")

    result_id = _required_text(payload.get("result_id"), "result_id")
    result = _resolve_result(conn, org_id=org_id, project_id=project_id, result_id=result_id)

    capability = _optional_text(payload.get("capability"), "capability")
    _refuse_capability_that_is_a_connector(conn, project_id=project_id, capability=capability)

    result_type = payload.get("result_type")
    result_type = None if result_type is None else _one_of(result_type, RESULT_TYPES, "result_type")

    domain_id, domain_version = _resolve_business_domain(
        conn,
        org_id=org_id,
        domain_id=payload.get("business_domain_id"),
        version_number=payload.get("business_domain_version_number"),
    )

    ai_path_id, ai_path_absent = _resolve_ai_path_pin(
        conn, project_id=project_id, requested=payload.get("ai_path_id"), result=result
    )

    visible_versions = _as_object(payload.get("visible_versions"), "visible_versions")
    _reject_unqualified_pins(visible_versions, "visible_versions")
    visible_hash = canonical_hash(visible_versions)

    feedback_id = _mint("fba")
    with conn.cursor() as cur:
        # `render_ref` and `datum_mark` are absent from this column list on
        # purpose: the only way a pin cannot be fabricated is for no write path
        # to name it.
        cur.execute(
            """
            INSERT INTO app.feedback_annotations
                (id, org_id, project_id, polarity, comment, actor, actor_source,
                 observed_surface, interaction_ref, w3c_trace_id,
                 result_id, ai_path_id, ai_path_absent_literal,
                 business_domain_id, business_domain_version_number,
                 capability, result_type,
                 visible_versions, visible_versions_hash, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s, %s)
            """,
            (
                feedback_id,
                org_id,
                project_id,
                polarity,
                comment,
                actor,
                actor_source,
                observed_surface,
                interaction_ref,
                trace_id,
                result["result_id"],
                ai_path_id,
                ai_path_absent,
                domain_id,
                domain_version,
                capability,
                result_type,
                _canonical_json(visible_versions),
                visible_hash,
                observed_at,
            ),
        )
        # The stable head exists from the first moment, so a reviewer never has
        # to create one as a side effect of judging.
        cur.execute(
            """
            INSERT INTO app.feedback_reviews
                (feedback_id, org_id, project_id, current_review_version_id,
                 current_state, updated_at)
            VALUES (%s, %s, %s, NULL, 'unreviewed', NOW())
            ON CONFLICT (feedback_id) DO NOTHING
            """,
            (feedback_id, org_id, project_id),
        )

    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_FEEDBACK_ANNOTATION_CREATED,
        provider_account=project_id,
        connection_ref="",
        metadata={
            "feedback_id": feedback_id,
            "result_id": result["result_id"],
            "polarity": polarity,
            "observed_surface": observed_surface,
        },
    )

    return get_annotation(conn, org_id=org_id, project_id=project_id, feedback_id=feedback_id)


def _exact_feedback_authority(
    conn,
    *,
    org_id: str,
    project_id: str,
    claims: Mapping[str, Any],
    target: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-resolve every signed owner on the request-scoped connection."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT qr.id, qr.content_hash, qr.ai_path_id, qr.ai_path_absent_literal,
                   p.result_schema, p.rows_chunk, p.manifest, ap.lifecycle,
                   qr.query_spec_version_id, qsv.semantic_view_id,
                   qsv.semantic_view_version_id, qsv.content_hash,
                   svv.content_hash, ap.content_hash
            FROM app.query_results qr
            JOIN app.query_result_payloads p
              ON p.result_id = qr.id AND p.org_id = qr.org_id AND p.project_id = qr.project_id
             AND p.content_hash = qr.content_hash
            JOIN app.query_spec_versions qsv
              ON qsv.id = qr.query_spec_version_id AND qsv.org_id = qr.org_id
             AND qsv.project_id = qr.project_id
            JOIN app.semantic_view_versions svv
              ON svv.id = qsv.semantic_view_version_id
             AND svv.view_id = qsv.semantic_view_id
             AND svv.project_id = qsv.project_id
            LEFT JOIN app.ai_paths ap
              ON ap.id = qr.ai_path_id AND ap.org_id = qr.org_id AND ap.project_id = qr.project_id
            WHERE qr.id = %s AND qr.org_id = %s AND qr.project_id = %s
            """,
            (claims.get("result_id"), org_id, project_id),
        )
        result_row = cur.fetchone()
    if result_row is None or result_row[1] != claims.get("result_content_hash"):
        raise FeedbackNotFound("Feedback target not found")
    if result_row[2] != claims.get("ai_path_id"):
        raise FeedbackNotFound("Feedback target not found")
    if result_row[2] is not None and result_row[7] != "finalized":
        raise FeedbackNotFound("Feedback target not found")

    from core.analyze_feedback import (  # noqa: PLC0415
        feedback_fields_from_schema,
        feedback_fields_from_visualization_spec,
    )

    schema = result_row[4] if isinstance(result_row[4], Mapping) else {}
    feedback_fields = feedback_fields_from_schema(schema)
    delivered = claims.get("delivered_rows") or {}
    start, count = delivered.get("start"), delivered.get("count")
    rows = result_row[5] if isinstance(result_row[5], list) else []
    if (
        not isinstance(start, int)
        or not isinstance(count, int)
        or start < 0
        or count < 0
        or start + count > len(rows)
    ):
        raise FeedbackNotFound("Feedback target not found")
    render_id = claims.get("render_id")
    spec_document = None
    if render_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT result_id, result_content_hash, visualization_spec_version_id,
                       renderer_build_id, runtime_build_id, theme_version, formatter_version
                FROM app.renders
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (render_id, org_id, project_id),
            )
            render = cur.fetchone()
        expected = (
            claims.get("result_id"),
            claims.get("result_content_hash"),
            claims.get("visualization_spec_version_id"),
            claims.get("renderer_build_id"),
            claims.get("runtime_build_id"),
            claims.get("theme_version"),
            claims.get("formatter_version"),
        )
        if render != expected:
            raise FeedbackNotFound("Feedback target not found")
    elif any(
        claims.get(field) is not None
        for field in (
            "visualization_spec_version_id",
            "renderer_build_id",
            "runtime_build_id",
            "theme_version",
            "formatter_version",
        )
    ):
        # Inline rendering has no retained Render. The signature attests the
        # mint-time served build tuple, which remains valid after a deployment;
        # only the immutable scoped Spec/Result relationship is re-resolved.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.spec
                FROM app.visualization_spec_versions v
                JOIN app.query_results qr
                  ON qr.query_spec_version_id = v.query_spec_version_id
                 AND qr.org_id = v.org_id AND qr.project_id = v.project_id
                WHERE v.id = %s AND v.org_id = %s AND v.project_id = %s AND qr.id = %s
                """,
                (
                    claims.get("visualization_spec_version_id"),
                    org_id,
                    project_id,
                    claims.get("result_id"),
                ),
            )
            spec = cur.fetchone()
        if spec is None:
            raise FeedbackNotFound("Feedback target not found")
        spec_document = spec[0]
    if render_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT spec FROM app.visualization_spec_versions
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (claims.get("visualization_spec_version_id"), org_id, project_id),
            )
            spec = cur.fetchone()
        if spec is None:
            raise FeedbackNotFound("Feedback target not found")
        spec_document = spec[0]
    if spec_document is not None:
        feedback_fields = feedback_fields_from_visualization_spec(
            schema, spec_document, result_row[6]
        )
    if target.get("kind") == "datum":
        fields_hash = hashlib.sha256(
            json.dumps(
                feedback_fields,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        if (
            delivered.get("field_count") != len(feedback_fields)
            or delivered.get("fields_hash") != fields_hash
            or target.get("field") not in set(feedback_fields)
        ):
            raise FeedbackNotFound("Feedback target not found")
    return {
        "result_id": result_row[0],
        "content_hash": result_row[1],
        "ai_path_id": result_row[2],
        "ai_path_absent_literal": result_row[3],
        "query_spec_version_id": result_row[8],
        "query_spec_version_content_hash": result_row[11],
        "semantic_view_id": result_row[9],
        "semantic_view_version_id": result_row[10],
        "semantic_view_version_content_hash": result_row[12],
        "ai_path_content_hash": result_row[13],
    }


def _visible_versions_snapshot(
    *, result: Mapping[str, Any], claims: Mapping[str, Any]
) -> dict[str, Any]:
    """Freeze every immutable owner identity visible in the delivered slice."""
    snapshot = {
        "result_id": result["result_id"],
        "result_content_hash": result["content_hash"],
        "query_spec_version_id": result["query_spec_version_id"],
        "query_spec_version_content_hash": result["query_spec_version_content_hash"],
        "semantic_view_id": result["semantic_view_id"],
        "semantic_view_version_id": result["semantic_view_version_id"],
        "semantic_view_version_content_hash": result["semantic_view_version_content_hash"],
        "ai_path_id": result.get("ai_path_id") or result.get("ai_path_absent_literal"),
        "ai_path_content_hash": (
            result.get("ai_path_content_hash")
            if result.get("ai_path_id")
            else _unavailable("result_has_no_ai_path")
        ),
    }
    for key in (
        "render_id",
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
    ):
        value = claims.get(key)
        if value is not None:
            snapshot[key] = value
    return snapshot


#: The one surface word the Console writes under. `OBSERVED_SURFACES` is the
#: database CHECK restated; this names WHICH of the two an annotation filed from
#: a Context Hub screen carries, so no caller spells it at a call site.
CONSOLE_SURFACE = "console"

#: The exact target the Context Hub can annotate. Migration 249 already carried
#: `(ai_path_id, path_step_ordinal) -> app.ai_path_steps` and the
#: `target_kind = 'path_step'` branch of `ck_feedback_annotations_target`; only
#: the delivered Result slices ever wrote them, so the column pair existed and no
#: surface filled it.
PATH_STEP_TARGET_KIND = "path_step"


def _append_exact_annotation(
    conn,
    *,
    feedback_id: str,
    org_id: str,
    project_id: str,
    polarity: str,
    comment: str | None,
    actor: str,
    observed_surface: str,
    interaction_ref: str,
    w3c_trace_id: str | None,
    result_id: str,
    result_content_hash: str,
    ai_path_id: str | None,
    ai_path_absent_literal: str | None,
    render_ref: str | None,
    pins: Mapping[str, Any],
    target: Mapping[str, Any],
    retry_key_hash: str,
    request_hash: str,
    visible_versions: Mapping[str, Any],
) -> None:
    """Append ONE exact-feedback.v1 row, its review head and its audit line.

    THE ONE WRITER, and that is the whole point of it being a function. Two entry
    points reach this table now -- the Result slice a person was delivered
    (`submit_exact_feedback`) and the observed AI Path step a person opened
    (`submit_ai_path_step_feedback`) -- and a second INSERT would answer
    differently the first time one of them was fixed. The caller owns the
    transaction; nothing here commits.
    """
    from core.analyze_feedback import SCHEMA_VERSION  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.feedback_annotations
                (id, org_id, project_id, polarity, comment, actor, actor_source,
                 observed_surface, interaction_ref, w3c_trace_id, result_id, result_content_hash,
                 ai_path_id, ai_path_absent_literal, render_ref,
                 visualization_spec_version_id, renderer_build_id, runtime_build_id,
                 theme_version, formatter_version, target_schema_version,
                 eligibility_schema_version, target_kind,
                 datum_row_index, datum_field, path_step_ordinal, retry_key_hash,
                 request_hash, visible_versions, visible_versions_hash, observed_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, 'user', %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, 'feedback-eligibility.v1', %s, %s, %s, %s,
                 %s, %s, %s::jsonb, %s, NOW())
            """,
            (
                feedback_id,
                org_id,
                project_id,
                polarity,
                comment,
                actor,
                observed_surface,
                interaction_ref,
                w3c_trace_id,
                result_id,
                result_content_hash,
                ai_path_id,
                ai_path_absent_literal,
                render_ref,
                pins.get("visualization_spec_version_id"),
                pins.get("renderer_build_id"),
                pins.get("runtime_build_id"),
                pins.get("theme_version"),
                pins.get("formatter_version"),
                SCHEMA_VERSION,
                target["kind"],
                target.get("row_index"),
                target.get("field"),
                target.get("ordinal"),
                retry_key_hash,
                request_hash,
                _canonical_json(visible_versions),
                canonical_hash(visible_versions),
            ),
        )
        cur.execute(
            """
            INSERT INTO app.feedback_reviews
                (feedback_id, org_id, project_id, current_review_version_id,
                 current_state, updated_at)
            VALUES (%s, %s, %s, NULL, 'unreviewed', NOW())
            ON CONFLICT (feedback_id) DO NOTHING
            """,
            (feedback_id, org_id, project_id),
        )
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_FEEDBACK_ANNOTATION_CREATED,
        provider_account=project_id,
        connection_ref="",
        metadata={
            "feedback_id": feedback_id,
            "result_id": result_id,
            "target_kind": target["kind"],
            "observed_surface": observed_surface,
        },
    )


def _ai_path_step_authority(
    conn,
    *,
    org_id: str,
    project_id: str,
    path_id: str,
    step_ordinal: int,
) -> dict[str, Any]:
    """Re-resolve the walk, the step and the Result it delivered, in this scope.

    Every owner is read on the request-scoped connection, exactly as
    `_exact_feedback_authority` does for a delivered slice: a caller names a path
    and an ordinal and nothing else, so nothing it sent can become an authority.

    Three refusals, and they are not the same fact:

      * an unknown path, a foreign one and one this caller may not read answer
        the SAME `FeedbackNotFound` -- the non-disclosure `ai_paths_api._not_found`
        keeps on the reads this write sits behind;
      * a step ordinal the walk does not carry is that same not-found: an ordinal
        is part of the target, not a filter on it;
      * a walk still `recording`, and a walk that delivered no Result, are NAMED
        refusals. The reader has the path open in front of them, so a 404 there
        would deny what the screen is already showing; what is missing is the
        immutable observation to pin the annotation to, and the sentence says so.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle, content_hash, w3c_trace_id
            FROM app.ai_paths
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (path_id, org_id, project_id),
        )
        path_row = cur.fetchone()
    if path_row is None:
        raise FeedbackNotFound("Feedback target not found")
    if path_row[0] != "finalized":
        raise FeedbackRefused(
            "ai_path_still_recording",
            "This walk is still being recorded. Annotate it once it has finished.",
            [Refusal("ai_path_still_recording", "The walk can still grow steps.", "ai_path_id")],
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM app.ai_path_steps
            WHERE path_id = %s AND org_id = %s AND project_id = %s AND ordinal = %s
            """,
            (path_id, org_id, project_id, step_ordinal),
        )
        if cur.fetchone() is None:
            raise FeedbackNotFound("Feedback target not found")

    # The annotation table pins a Result by NOT NULL column and composite foreign
    # key (migration 153), and migration 249 requires its content hash on every
    # exact row. A `path_step` annotation is therefore anchored on the Result the
    # walk delivered -- the shape 249 designed, not a shortcut taken here.
    # `ORDER BY qr.id` is deterministic: the id is a ULID, so this is the FIRST
    # Result the walk delivered and the same one on every replay.
    #
    # OPEN POINT, stated rather than hidden (`context-hub.md`, amendment of
    # 2026-08-30). When a walk delivered SEVERAL Results, which one a step
    # annotation pins is arbitrary-but-deterministic: deterministic because the
    # ULID ordering never moves, arbitrary because nobody chose it. The reader on
    # the Context Hub screen has no Result open, so this surface cannot ask them,
    # and inventing a rule here (the last one? the one the step fed?) would be a
    # design decision taken by a repair. It is owed to the `analyze-and-test.md`
    # owner and is recorded in the amendment so it cannot quietly become
    # permanent.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT qr.id, qr.content_hash, qr.query_spec_version_id,
                   qsv.semantic_view_id, qsv.semantic_view_version_id,
                   qsv.content_hash, svv.content_hash
            FROM app.query_results qr
            JOIN app.query_spec_versions qsv
              ON qsv.id = qr.query_spec_version_id AND qsv.org_id = qr.org_id
             AND qsv.project_id = qr.project_id
            JOIN app.semantic_view_versions svv
              ON svv.id = qsv.semantic_view_version_id
             AND svv.view_id = qsv.semantic_view_id
             AND svv.project_id = qsv.project_id
            WHERE qr.ai_path_id = %s AND qr.org_id = %s AND qr.project_id = %s
            ORDER BY qr.id
            LIMIT 1
            """,
            (path_id, org_id, project_id),
        )
        result_row = cur.fetchone()
    if result_row is None:
        raise FeedbackRefused(
            "ai_path_delivered_no_result",
            "This walk delivered no Result, so a step annotation has nothing "
            "immutable to pin. Annotate the answer it produced instead.",
            [
                Refusal(
                    "ai_path_delivered_no_result",
                    "No Result of this Project names this walk.",
                    "ai_path_id",
                )
            ],
        )

    return {
        "result_id": result_row[0],
        "content_hash": result_row[1],
        "ai_path_id": path_id,
        "ai_path_absent_literal": None,
        "query_spec_version_id": result_row[2],
        "query_spec_version_content_hash": result_row[5],
        "semantic_view_id": result_row[3],
        "semantic_view_version_id": result_row[4],
        "semantic_view_version_content_hash": result_row[6],
        "ai_path_content_hash": path_row[1],
        "w3c_trace_id": _validate_trace_id(path_row[2]),
    }


def read_actor_path_step_reactions(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    path_id: str,
) -> dict[int, dict[str, Any]]:
    """The caller's OWN recorded reaction on each step of one walk, by ordinal.

    A READ, and only a read: no INSERT, no UPDATE, no eligibility row, nothing
    that a second call would make grow. It exists so the Context Hub AI Path
    screen can start a step's control in its recorded state after a reload
    instead of asking the same question twice (`context-hub.md`, amendment of
    2026-08-30, corrected the same day: the standing reaction is the LATEST
    row, comment included, and "Incomplete if ... a reaction the person
    recorded is forgotten by the screen after a reload").

    IT LIVES HERE, with the Feedback owner, and not in `core.ai_paths_api`. The
    Context Hub "owns neither Feedback nor Events and adds no editor for them":
    it may READ the owner's answer through the owner's own function -- the same
    seam `_compose_event_references` uses for the Data-owned Events -- and it may
    not name `app.feedback_annotations` itself.

    Scope travels in the WHERE clause and the connection is the caller's, so
    another person's reaction on the same step is not visible here and RLS
    refuses it besides. `actor` is the authenticated identity, never a field of
    the request.
    """
    org_id = _required_text(org_id, "org_id")
    project_id = _required_text(project_id, "project_id")
    actor = _required_text(actor, "actor")
    path_id = _required_text(path_id, "ai_path_id")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (path_step_ordinal)
                   path_step_ordinal, polarity, observed_at, comment
            FROM app.feedback_annotations
            WHERE org_id = %s AND project_id = %s AND actor = %s
              AND ai_path_id = %s AND target_kind = %s
              AND path_step_ordinal IS NOT NULL
            ORDER BY path_step_ordinal, observed_at DESC, id DESC
            """,
            (org_id, project_id, actor, path_id, PATH_STEP_TARGET_KIND),
        )
        rows = cur.fetchall() or []

    reactions: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            ordinal = int(row[0])
        except (TypeError, ValueError):
            continue
        # LATEST WINS (review of 2026-08-30): the table is insert-once and a
        # revision is a NEW row, so the person's standing reaction on a step is
        # the most recent one -- the first by `observed_at` is the superseded one.
        reactions.setdefault(
            ordinal,
            {
                "polarity": row[1],
                "recorded_at": row[2],
                "comment": row[3] if len(row) > 3 else None,
            },
        )
    return reactions


def submit_ai_path_step_feedback(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    path_id: str,
    step_ordinal: Any,
    polarity: Any,
    comment: Any,
    retry_key: Any,
    secret: bytes | None = None,
) -> dict[str, Any]:
    """Append one +/- annotation on ONE observed AI Path step (Story 49.6 AC8).

    ADDITIVE, in the exact sense AC8 asks for. `submit_feedback` keeps its
    signature, `submit_exact_feedback` keeps its signed-sidecar contract, and
    this adds the third entry point the store was already shaped for: migration
    249 carries the `(ai_path_id, path_step_ordinal)` foreign key and the
    `target_kind = 'path_step'` branch of `ck_feedback_annotations_target`, and
    until now only a delivered Result slice could reach them.

    WHY THERE IS NO SIGNED CONTEXT HERE. The sidecar of `core.analyze_feedback`
    attests what a person was DELIVERED -- a Result slice, its rows, its Render
    build tuple -- because a widget could otherwise claim to have been shown
    something it was not. A Context Hub reader is not being delivered a slice;
    they are reading a governed walk they hold a grant on, and the target they
    name is re-resolved here on the scoped connection. Nothing the caller sends
    survives into the row except the polarity, the comment and the retry key.

    The caller owns the transaction and must commit only after this returns.
    """
    from core.analyze_feedback import (  # noqa: PLC0415
        MAX_COMMENT,
        RECEIPT_SCHEMA_VERSION,
        request_fingerprint,
    )

    org_id = _required_text(org_id, "org_id")
    project_id = _required_text(project_id, "project_id")
    actor = _required_text(actor, "actor")
    path_id = _required_text(path_id, "ai_path_id")
    polarity = _one_of(polarity, POLARITIES, "polarity")
    comment = _optional_text(comment, "comment", maximum=MAX_COMMENT)
    retry = _required_text(retry_key, "retry_key", maximum=_MAX_SHORT_TEXT)
    if not isinstance(step_ordinal, int) or isinstance(step_ordinal, bool) or step_ordinal < 0:
        raise FeedbackRefused(
            "invalid_field",
            "step_ordinal must be the ordinal of a step of this walk.",
            [Refusal("invalid_field", "step_ordinal is not an ordinal.", "step_ordinal")],
        )

    resolved = _ai_path_step_authority(
        conn,
        org_id=org_id,
        project_id=project_id,
        path_id=path_id,
        step_ordinal=step_ordinal,
    )

    target = {"kind": PATH_STEP_TARGET_KIND, "ordinal": step_ordinal}
    # The interaction is the READING of this exact step, and it names it rather
    # than minting an opaque id: two annotations of the same step by the same
    # person are then visibly the same interaction, which is what the Feedback
    # Review Workbench of Story 51.5 will group on.
    interaction_ref = _required_text(
        f"context-hub:ai-path:{path_id}:step:{step_ordinal}",
        "interaction_ref",
        maximum=_MAX_SHORT_TEXT,
    )

    # THE DELIVERY FACT, recorded here and nowhere else. Migration 251 puts
    # `require_feedback_eligible_observation` on this table: every exact-feedback
    # row must name an observation that was delivered as annotatable, and without
    # one the INSERT below fails with a raw integrity error nobody can read.
    #
    # It is recorded by the WRITE and not by the read that displayed the walk,
    # because a read that writes is the defect this repository refuses ("une
    # lecture n'ecrit pas"). What it attests is exactly what happened: an
    # authenticated console interaction on this Result, eligible for the `answer`
    # and `path_step` targets and -- `delivered_rows` being absent -- for no
    # `datum` target at all.
    eligibility_claims = {
        "schema_version": "exact-feedback.v1",
        "org_id": org_id,
        "project_id": project_id,
        "surface": CONSOLE_SURFACE,
        "interaction_ref": interaction_ref,
        "result_id": resolved["result_id"],
        "result_content_hash": resolved["content_hash"],
        "delivered_rows": None,
        "ai_path_id": path_id,
        "path_step_ordinals": [step_ordinal],
    }

    fingerprint_claims = {
        "org_id": org_id,
        "project_id": project_id,
        "surface": CONSOLE_SURFACE,
        "interaction_ref": interaction_ref,
        "result_id": resolved["result_id"],
        "result_content_hash": resolved["content_hash"],
        "ai_path_id": path_id,
    }
    retry_hash, request_hash = request_fingerprint(
        {"target": target, "polarity": polarity, "comment": comment, "retry_key": retry},
        claims=fingerprint_claims,
        secret=secret,
    )

    with conn.cursor() as cur:
        # The same serialization the Result slice writer takes, on the same key
        # shape: two clicks racing on one retry key must not both insert.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{org_id}:{project_id}:{actor}:{CONSOLE_SURFACE}:{retry_hash}",),
        )
        cur.execute(
            """
            SELECT id, request_hash, interaction_ref, path_step_ordinal, polarity, comment
            FROM app.feedback_annotations
            WHERE org_id = %s AND project_id = %s AND actor = %s
              AND observed_surface = %s AND retry_key_hash = %s
              AND target_schema_version = 'exact-feedback.v1'
            """,
            (org_id, project_id, actor, CONSOLE_SURFACE, retry_hash),
        )
        existing = cur.fetchone()
    if existing is not None:
        if existing[1] != request_hash:
            raise FeedbackRefused(
                "idempotency_conflict",
                "This retry key was already used for different feedback.",
                [
                    Refusal(
                        "idempotency_conflict",
                        "The same key carried a different annotation.",
                        "retry_key",
                    )
                ],
            )
        # READ BACK, never echoed: the verdict a surface displays after a replay
        # is the STORED one, so a second click cannot make the screen show a
        # polarity the table does not hold.
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "replayed",
            "feedback_id": existing[0],
            "interaction_ref": existing[2],
            "ai_path_id": path_id,
            "target": {"kind": PATH_STEP_TARGET_KIND, "ordinal": existing[3]},
            "polarity": existing[4],
            "comment": existing[5],
        }

    # THE DELIVERY FACT IS WRITTEN ONLY ONCE THE REACTION IS GOING TO BE WRITTEN.
    #
    # It used to be recorded above, before the retry key was even looked up, so a
    # refused reaction still left an eligibility row behind: a second polarity on
    # the same key answered `idempotency_conflict` -- nothing was annotated -- and
    # `app.feedback_eligible_observations` had grown all the same. A refusal that
    # leaves a row is a refusal that half happened, and the table it grows is the
    # one that says what a person was DELIVERED as annotatable.
    #
    # Everything that can refuse has already refused by this line: the walk, the
    # step and the Result were re-resolved, the advisory lock is held and the
    # retry key was read back. What remains is the append itself.
    if record_feedback_eligibility(conn, claims=eligibility_claims) is None:
        raise FeedbackRefused(
            "feedback_eligibility_unavailable",
            "This reaction could not be recorded against what the walk delivered. "
            "Reopen the path and try again.",
            [
                Refusal(
                    "feedback_eligibility_unavailable",
                    "The delivered observation behind this walk is unavailable.",
                    "ai_path_id",
                )
            ],
        )
    require_feedback_eligibility(
        conn,
        org_id=org_id,
        project_id=project_id,
        claims=eligibility_claims,
        source="authenticated",
    )

    feedback_id = _mint("fba")
    visible_versions = _visible_versions_snapshot(result=resolved, claims={})
    _append_exact_annotation(
        conn,
        feedback_id=feedback_id,
        org_id=org_id,
        project_id=project_id,
        polarity=polarity,
        comment=comment,
        actor=actor,
        observed_surface=CONSOLE_SURFACE,
        interaction_ref=interaction_ref,
        w3c_trace_id=resolved["w3c_trace_id"],
        result_id=resolved["result_id"],
        result_content_hash=resolved["content_hash"],
        ai_path_id=path_id,
        ai_path_absent_literal=None,
        render_ref=None,
        pins={},
        target=target,
        retry_key_hash=retry_hash,
        request_hash=request_hash,
        visible_versions=visible_versions,
    )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "recorded",
        "feedback_id": feedback_id,
        "interaction_ref": interaction_ref,
        "ai_path_id": path_id,
        "target": target,
        "polarity": polarity,
        "comment": comment,
    }


def submit_exact_feedback(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    payload: Mapping[str, Any],
    secret: bytes | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and atomically append one exact-feedback.v1 annotation.

    The caller owns the transaction and must commit only after this function
    returns.  An expired but authentic context is reissued after the same owner
    checks and performs no write.
    """
    from core.analyze_feedback import (  # noqa: PLC0415
        RECEIPT_SCHEMA_VERSION,
        FeedbackContextError,
        mint_feedback_context,
        normalize_feedback_request,
        request_fingerprint,
        verify_feedback_context,
    )

    required_payload_keys = {"context", "target", "polarity", "comment", "retry_key"}
    if set(payload) != required_payload_keys:
        raise FeedbackRefused(
            "invalid_request",
            "Feedback accepts exactly context, target, polarity, comment and retry_key.",
            [],
        )
    context = payload.get("context")
    expired = False
    try:
        claims = verify_feedback_context(context, secret=secret, now=now)
    except FeedbackContextError as exc:
        if exc.code != "feedback_context_expired" or not exc.verified_claims:
            raise FeedbackRefused(exc.code, str(exc), []) from exc
        claims = exc.verified_claims
        expired = True
    if claims.get("org_id") != org_id or claims.get("project_id") != project_id:
        raise FeedbackNotFound("Feedback target not found")
    try:
        normalized = normalize_feedback_request(
            claims=claims,
            target=payload.get("target"),
            polarity=payload.get("polarity"),
            comment=payload.get("comment"),
            retry_key=payload.get("retry_key"),
        )
    except FeedbackContextError as exc:
        if exc.code == "invalid_target":
            raise FeedbackNotFound("Feedback target not found") from exc
        raise FeedbackRefused(exc.code, str(exc), []) from exc
    result = _exact_feedback_authority(
        conn,
        org_id=org_id,
        project_id=project_id,
        claims=claims,
        target=normalized["target"],
    )
    require_feedback_eligibility(
        conn,
        org_id=org_id,
        project_id=project_id,
        claims=claims,
        source="authenticated",
    )
    target = normalized["target"]
    if target["kind"] == "path_step":
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM app.ai_path_steps
                WHERE path_id = %s AND org_id = %s AND project_id = %s AND ordinal = %s
                """,
                (result["ai_path_id"], org_id, project_id, target["ordinal"]),
            )
            if cur.fetchone() is None:
                raise FeedbackNotFound("Feedback target not found")
    if expired:
        refreshed = mint_feedback_context(claims, secret=secret, now=now)
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "refresh_required",
            "code": "feedback_context_expired",
            "message": "The feedback context expired. Retry with the refreshed context.",
            "feedback_context": refreshed,
        }

    retry_hash, request_hash = request_fingerprint(
        normalized, claims=claims, secret=secret
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{org_id}:{project_id}:{actor}:{claims['surface']}:{retry_hash}",),
        )
        cur.execute(
            """
            SELECT id, request_hash, interaction_ref, target_kind,
                   datum_row_index, datum_field, path_step_ordinal
            FROM app.feedback_annotations
            WHERE org_id = %s AND project_id = %s AND actor = %s
              AND observed_surface = %s AND retry_key_hash = %s
              AND target_schema_version = 'exact-feedback.v1'
            """,
            (org_id, project_id, actor, claims["surface"], retry_hash),
        )
        existing = cur.fetchone()
    if existing is not None:
        if existing[1] != request_hash:
            raise FeedbackRefused(
                "idempotency_conflict",
                "This retry key was already used for different feedback.",
                [],
            )
        replay_target: dict[str, Any] = {"kind": existing[3]}
        if existing[3] == "datum":
            replay_target.update(row_index=existing[4], field=existing[5])
        elif existing[3] == "path_step":
            replay_target["ordinal"] = existing[6]
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "replayed",
            "feedback_id": existing[0],
            "interaction_ref": existing[2],
            "target": replay_target,
        }

    feedback_id = _mint("fba")
    pins = {
        key: claims.get(key)
        for key in (
            "visualization_spec_version_id",
            "renderer_build_id",
            "runtime_build_id",
            "theme_version",
            "formatter_version",
        )
    }
    visible_versions = _visible_versions_snapshot(result=result, claims=claims)
    _append_exact_annotation(
        conn,
        feedback_id=feedback_id,
        org_id=org_id,
        project_id=project_id,
        polarity=normalized["polarity"],
        comment=normalized["comment"],
        actor=actor,
        observed_surface=claims["surface"],
        interaction_ref=claims["interaction_ref"],
        w3c_trace_id=claims.get("w3c_trace_id"),
        result_id=result["result_id"],
        result_content_hash=result["content_hash"],
        ai_path_id=result["ai_path_id"],
        ai_path_absent_literal=result["ai_path_absent_literal"],
        render_ref=claims.get("render_id"),
        pins=pins,
        target=target,
        retry_key_hash=retry_hash,
        request_hash=request_hash,
        visible_versions=visible_versions,
    )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "recorded",
        "feedback_id": feedback_id,
        "interaction_ref": claims["interaction_ref"],
        "target": target,
    }


# ---------------------------------------------------------------------------
# Read.
# ---------------------------------------------------------------------------

_ANNOTATION_SELECT = """
    WITH feedback_union AS (
        SELECT a.id, a.org_id, a.project_id, 'authenticated'::TEXT AS source,
               a.polarity, a.comment, a.actor, a.actor_source,
               a.observed_surface, a.interaction_ref, a.w3c_trace_id,
               a.result_id, a.ai_path_id, a.ai_path_absent_literal,
               a.business_domain_id, a.business_domain_version_number,
               a.capability, a.result_type,
               a.target_schema_version, a.result_content_hash, a.render_ref,
               a.visualization_spec_version_id, a.renderer_build_id, a.runtime_build_id,
               a.theme_version, a.formatter_version, a.target_kind,
               a.datum_row_index, a.datum_field, a.path_step_ordinal,
               a.visible_versions, a.visible_versions_hash,
               a.observed_at, a.created_at
        FROM app.feedback_annotations a
        UNION ALL
        SELECT s.id, s.org_id, s.project_id, 'anonymous_share'::TEXT,
               CASE s.polarity WHEN 'helpful' THEN 'positive' ELSE 'negative' END,
               s.comment, NULL::TEXT, NULL::TEXT,
               s.observed_surface, s.interaction_ref, NULL::TEXT,
               s.result_id, s.ai_path_id, NULL::TEXT,
               NULL::TEXT, NULL::INTEGER, NULL::TEXT, NULL::TEXT,
               s.target_schema_version, s.result_content_hash, s.render_id,
               s.visualization_spec_version_id, s.renderer_build, s.runtime_build,
               s.theme_version, s.formatter_version, s.target_kind,
               s.datum_row_index, s.datum_field, s.path_step_ordinal,
               jsonb_strip_nulls(jsonb_build_object(
                   'result_id', s.result_id,
                   'result_content_hash', s.result_content_hash,
                   'visualization_spec_version_id', s.visualization_spec_version_id,
                   'renderer_build_id', s.renderer_build,
                   'runtime_build_id', s.runtime_build,
                   'theme_version', s.theme_version,
                   'formatter_version', s.formatter_version
               )), s.evidence_manifest_hash,
               s.submitted_at, s.submitted_at
        FROM app.render_share_feedback s
    )
    SELECT a.id, a.polarity, a.comment, a.actor, a.actor_source,
           a.observed_surface, a.interaction_ref, a.w3c_trace_id,
           a.result_id, a.ai_path_id,
           COALESCE(a.ai_path_absent_literal, qr.ai_path_absent_literal),
           a.business_domain_id, a.business_domain_version_number,
           a.capability, a.result_type,
           a.target_schema_version, a.result_content_hash, a.render_ref,
           a.visualization_spec_version_id, a.renderer_build_id, a.runtime_build_id,
           a.theme_version, a.formatter_version, a.target_kind,
           a.datum_row_index, a.datum_field, a.path_step_ordinal,
           CASE
             WHEN a.source = 'anonymous_share' THEN
               a.visible_versions || jsonb_strip_nulls(jsonb_build_object(
                   'query_spec_version_id', qr.query_spec_version_id,
                   'query_spec_version_content_hash', qsv.content_hash,
                   'semantic_view_id', qsv.semantic_view_id,
                   'semantic_view_version_id', qsv.semantic_view_version_id,
                   'semantic_view_version_content_hash', svv.content_hash,
                   'ai_path_id', COALESCE(a.ai_path_id, qr.ai_path_absent_literal),
                   'ai_path_content_hash', CASE
                       WHEN a.ai_path_id IS NULL THEN jsonb_build_object(
                           'state', 'unavailable',
                           'reason', 'result_has_no_ai_path'
                       )
                       ELSE to_jsonb(ap.content_hash)
                   END,
                   'render_id', a.render_ref
               ))
             ELSE a.visible_versions
           END AS visible_versions,
           a.visible_versions_hash,
           a.observed_at, a.created_at,
           qr.outcome, qr.query_spec_version_id, qsv.query_spec_id,
           qsv.semantic_view_id, qsv.semantic_view_version_id,
           rev.current_state, rev.current_review_version_id,
           a.source, COALESCE(e.classification,
                              p.manifest->'evaluation_classification') AS classification,
           qsv.content_hash, svv.content_hash,
           CASE
             WHEN a.ai_path_id IS NULL THEN jsonb_build_object(
                 'state', 'unavailable',
                 'reason', 'result_has_no_ai_path'
             )
             ELSE to_jsonb(ap.content_hash)
           END AS ai_path_content_hash
    FROM feedback_union a
    JOIN app.query_results qr
      ON qr.id = a.result_id AND qr.org_id = a.org_id AND qr.project_id = a.project_id
    JOIN app.query_spec_versions qsv
      ON qsv.id = qr.query_spec_version_id AND qsv.project_id = a.project_id
    JOIN app.semantic_view_versions svv
      ON svv.id = qsv.semantic_view_version_id
     AND svv.view_id = qsv.semantic_view_id
     AND svv.project_id = qsv.project_id
    LEFT JOIN app.ai_paths ap
      ON ap.id = a.ai_path_id AND ap.org_id = a.org_id AND ap.project_id = a.project_id
    LEFT JOIN app.query_result_payloads p
      ON p.result_id = qr.id AND p.org_id = qr.org_id AND p.project_id = qr.project_id
     AND p.content_hash = qr.content_hash
    LEFT JOIN app.feedback_eligible_observations e
      ON e.org_id = a.org_id AND e.project_id = a.project_id
     AND e.source = a.source AND e.interaction_ref = a.interaction_ref
    LEFT JOIN app.feedback_reviews rev
      ON rev.feedback_id = a.id AND rev.org_id = a.org_id AND rev.project_id = a.project_id
"""

_ANNOTATION_FIELDS = (
    "id",
    "polarity",
    "comment",
    "actor",
    "actor_source",
    "observed_surface",
    "interaction_ref",
    "w3c_trace_id",
    "result_id",
    "ai_path_id",
    "ai_path_absent_literal",
    "business_domain_id",
    "business_domain_version_number",
    "capability",
    "result_type",
    "target_schema_version",
    "result_content_hash",
    "render_ref",
    "visualization_spec_version_id",
    "renderer_build_id",
    "runtime_build_id",
    "theme_version",
    "formatter_version",
    "target_kind",
    "datum_row_index",
    "datum_field",
    "path_step_ordinal",
    "visible_versions",
    "visible_versions_hash",
    "observed_at",
    "created_at",
    "outcome",
    "query_spec_version_id",
    "query_spec_id",
    "semantic_view_id",
    "semantic_view_version_id",
    "current_state",
    "current_review_version_id",
    "source",
    "classification",
    "query_spec_version_content_hash",
    "semantic_view_version_content_hash",
    "ai_path_content_hash",
)


def _version_divergence(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Disclose, never reconcile.

    `visible_versions` is what the person could SEE. The authority stays the
    pinned owner rows. When the two disagree, silently preferring either one
    would erase the fact that the screen and the record disagreed at all.
    """
    snapshot = row.get("visible_versions") or {}
    if not isinstance(snapshot, Mapping):
        return []
    derived = {
        "semantic_view_id": row.get("semantic_view_id"),
        "semantic_view_version_id": row.get("semantic_view_version_id"),
        "query_spec_version_id": row.get("query_spec_version_id"),
        "query_spec_version_content_hash": row.get("query_spec_version_content_hash"),
        "result_id": row.get("result_id"),
        "ai_path_id": row.get("ai_path_id") or row.get("ai_path_absent_literal"),
        "semantic_view_version_content_hash": row.get(
            "semantic_view_version_content_hash"
        ),
        "ai_path_content_hash": row.get("ai_path_content_hash"),
        "render_id": row.get("render_ref"),
        "visualization_spec_version_id": row.get("visualization_spec_version_id"),
        "renderer_build_id": row.get("renderer_build_id"),
        "runtime_build_id": row.get("runtime_build_id"),
        "theme_version": row.get("theme_version"),
        "formatter_version": row.get("formatter_version"),
    }
    divergences = []
    for field, pinned in derived.items():
        seen = snapshot.get(field)
        if seen is None:
            continue
        same = seen == pinned if isinstance(seen, (Mapping, list)) else str(seen) == str(pinned)
        if not same:
            divergences.append({"field": field, "visible": seen, "pinned": pinned})
    return divergences


def _row_classification(row: Mapping[str, Any]) -> dict[str, Any]:
    candidate = row.get("classification")
    if isinstance(candidate, Mapping):
        return _classification_from_manifest({"evaluation_classification": candidate})
    domains: dict[str, Any]
    if row.get("business_domain_id") and row.get("business_domain_version_number"):
        domains = {
            "state": "attributed",
            "versions": [
                {
                    "id": row["business_domain_id"],
                    "version_number": row["business_domain_version_number"],
                }
            ],
        }
    else:
        domains = _unavailable("historical_result_unclassified")
    document = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {
            "state": "attributed",
            "id": row.get("semantic_view_id"),
            "version_id": row.get("semantic_view_version_id"),
        },
        "business_domains": domains,
        "skills": _unavailable("historical_result_unclassified"),
        "capability": (
            {"state": "attributed", "key": row["capability"], "version_id": None}
            if row.get("capability")
            else _unavailable("historical_result_unclassified")
        ),
        "result_type": (
            {"state": "attributed", "value": row["result_type"]}
            if row.get("result_type")
            else _unavailable("historical_result_unclassified")
        ),
    }
    return {**document, "classification_hash": canonical_hash(document)}


def _annotation_read_model(row: Mapping[str, Any]) -> dict[str, Any]:
    has_path = bool(row.get("ai_path_id"))
    is_v1 = row.get("target_schema_version") == "exact-feedback.v1"
    target: dict[str, Any] | None = None
    if is_v1:
        target = {"kind": row.get("target_kind")}
        if row.get("target_kind") == "datum":
            target.update(row_index=row.get("datum_row_index"), field=row.get("datum_field"))
        elif row.get("target_kind") == "path_step":
            target["ordinal"] = row.get("path_step_ordinal")
    render_pins = {
        "render_id": row.get("render_ref"),
        "visualization_spec_version_id": row.get("visualization_spec_version_id"),
        "renderer_build_id": row.get("renderer_build_id"),
        "runtime_build_id": row.get("runtime_build_id"),
        "theme_version": row.get("theme_version"),
        "formatter_version": row.get("formatter_version"),
    }
    visible_versions = row.get("visible_versions") or {}
    visible_hash_kind = (
        "evidence_manifest" if row.get("source") == "anonymous_share" else "visible_versions"
    )
    visible_hash_valid = (
        canonical_hash(visible_versions) == row.get("visible_versions_hash")
        if visible_hash_kind == "visible_versions" and isinstance(visible_versions, Mapping)
        else None
    )
    return {
        "schema_version": "feedback-review-detail.v1",
        "id": row["id"],
        "source": row.get("source") or "authenticated",
        "target_schema_version": row.get("target_schema_version"),
        "polarity": row["polarity"],
        "comment": row["comment"],
        "actor": row["actor"],
        "actor_source": row["actor_source"],
        "observed_surface": row["observed_surface"],
        "interaction_ref": row["interaction_ref"],
        "w3c_trace_id": row["w3c_trace_id"],
        "result": {
            "id": row["result_id"],
            "outcome": row["outcome"],
            "query_spec_version_id": row["query_spec_version_id"],
            "content_hash": row.get("result_content_hash"),
        },
        "render": render_pins if any(render_pins.values()) else None,
        "target": target,
        "semantic_view": {
            "id": row["semantic_view_id"],
            "version_id": row["semantic_view_version_id"],
        },
        # Exactly one of the two is set; the database CHECK guarantees it, so a
        # caller never has to reason about a missing AI Path.
        "ai_path": row.get("ai_path_id") or row.get("ai_path_absent_literal"),
        "business_domain": (
            {
                "id": row["business_domain_id"],
                "version_number": row["business_domain_version_number"],
            }
            if row.get("business_domain_id")
            else None
        ),
        "capability": row["capability"],
        "result_type": row["result_type"],
        "classification": _row_classification(row),
        "visible_versions": visible_versions,
        "visible_versions_hash": row["visible_versions_hash"],
        "visible_versions_hash_kind": visible_hash_kind,
        "visible_versions_hash_valid": visible_hash_valid,
        "version_divergence": _version_divergence(row),
        "observed_at": _iso(row.get("observed_at")),
        "created_at": _iso(row.get("created_at")),
        # Every declared absence, stated where a reader looks for the evidence.
        "evidence_lenses": {
            "render": (
                {"state": "observed", **render_pins}
                if any(render_pins.values())
                else RENDER_ABSENT.as_lens()
            ),
            "datum_mark": (
                {"state": "observed", "locator": target}
                if target and target.get("kind") == "datum"
                else {"state": "not_targeted"}
                if is_v1
                else DATUM_MARK_ABSENT.as_lens()
            ),
            "mcp_app_behavior": MCP_APP_BEHAVIOR_ABSENT.as_lens(),
            "server_owned_path_evidence": (
                {"state": "observed", "reason": None, "owner_stories": [], "detail": None}
                if has_path
                else PATH_EVIDENCE_ABSENT.as_lens()
            ),
        },
        "owner_links": _owner_links(row),
        "unavailable_owner_links": _unavailable_owner_links(row),
        "review": {
            "current_state": row.get("current_state") or "unreviewed",
            "current_version_id": row.get("current_review_version_id"),
        },
        "blocking_use": (
            "Prioritizes investigation; never proves correctness or regression alone."
        ),
    }


def _automated_verdicts(
    conn, *, org_id: str, project_id: str, row: Mapping[str, Any]
) -> dict[str, Any]:
    if not row.get("current_review_version_id"):
        return {
            "state": "unavailable",
            "reason": "current_objective_review_unavailable",
            "items": [],
            "truncated": False,
        }
    classification = _row_classification(row)
    render_pins = tuple(
        row.get(key)
        for key in (
            "visualization_spec_version_id",
            "renderer_build_id",
            "runtime_build_id",
            "theme_version",
            "formatter_version",
        )
    )
    pins_are_exact = not any(render_pins) or (all(render_pins) and row.get("render_ref"))
    source_classification = row.get("classification")
    classification_is_exact = (
        isinstance(source_classification, Mapping)
        and source_classification.get("classification_hash")
        == classification.get("classification_hash")
    )
    if (
        row.get("target_schema_version") != "exact-feedback.v1"
        or not pins_are_exact
        or not classification_is_exact
    ):
        return {
            "state": "unavailable",
            "reason": "exact_classification_unavailable",
            "items": [],
            "truncated": False,
        }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT er.id, c.id, v.dimension, v.verdict, v.reason_code,
                   v.evidence_refs, v.created_at
            FROM app.evaluation_run_cases c
            JOIN app.evaluation_runs er
              ON er.id = c.run_id AND er.org_id = c.org_id AND er.project_id = c.project_id
            JOIN app.evaluation_case_dimension_verdicts v
              ON v.case_id = c.id AND v.org_id = c.org_id AND v.project_id = c.project_id
            JOIN app.feedback_review_versions human_review
              ON human_review.id = %s AND human_review.feedback_id = %s
             AND human_review.org_id = c.org_id AND human_review.project_id = c.project_id
            JOIN app.feedback_reviews human_head
              ON human_head.feedback_id = human_review.feedback_id
             AND human_head.org_id = human_review.org_id
             AND human_head.project_id = human_review.project_id
             AND human_head.current_review_version_id = human_review.id
            JOIN app.query_results qr
              ON qr.id = c.result_id AND qr.org_id = c.org_id AND qr.project_id = c.project_id
            JOIN app.query_result_payloads p
              ON p.result_id = qr.id AND p.org_id = qr.org_id
             AND p.project_id = qr.project_id AND p.content_hash = qr.content_hash
            LEFT JOIN app.renders pinned_render
              ON pinned_render.id = c.render_ref AND pinned_render.org_id = c.org_id
             AND pinned_render.project_id = c.project_id
            WHERE c.org_id = %s AND c.project_id = %s
              AND c.result_id = %s AND qr.content_hash = %s
              AND v.dimension = human_review.affected_dimension
              AND er.semantic_view_id = %s AND er.semantic_view_version_id = %s
              AND c.render_ref IS NOT DISTINCT FROM %s
              AND c.result_classification_hash = %s
              AND c.ai_path_id IS NOT DISTINCT FROM %s
              AND c.ai_path_absent_literal IS NOT DISTINCT FROM %s
              AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(COALESCE(
                        p.manifest->'evaluation_classification'
                          ->'business_domains'->'versions',
                        '[]'::jsonb
                    )) domain
                    WHERE domain->>'id' = c.business_domain_id
                      AND (domain->>'version_number')::INTEGER
                            = c.business_domain_version_number
                  )
              AND p.manifest->'evaluation_classification'->'capability'->>'state'
                    = 'attributed'
              AND p.manifest->'evaluation_classification'->'capability'->>'key'
                    = c.capability_key
              AND p.manifest->'evaluation_classification'->'result_type'->>'state'
                    = 'attributed'
              AND p.manifest->'evaluation_classification'->'result_type'->>'value'
                    = c.result_type
              AND p.manifest->'evaluation_classification'->>'classification_hash'
                    = c.result_classification_hash
              AND encode(sha256(convert_to(app.feedback_canonical_json(
                    (p.manifest->'evaluation_classification') - 'classification_hash'
                  ), 'UTF8')), 'hex') = c.result_classification_hash
              AND (
                    (%s::text IS NULL AND c.render_ref IS NULL)
                    OR (
                        pinned_render.id = %s
                        AND pinned_render.result_id = %s
                        AND pinned_render.result_content_hash = %s
                        AND pinned_render.visualization_spec_version_id = %s
                        AND pinned_render.renderer_build_id = %s
                        AND pinned_render.runtime_build_id = %s
                        AND pinned_render.theme_version = %s
                        AND pinned_render.formatter_version = %s
                    )
                  )
            ORDER BY v.created_at DESC, er.id DESC, c.id DESC, v.dimension
            LIMIT 51
            """,
            (
                row.get("current_review_version_id"),
                row.get("id"),
                org_id,
                project_id,
                row.get("result_id"),
                row.get("result_content_hash"),
                row.get("semantic_view_id"),
                row.get("semantic_view_version_id"),
                row.get("render_ref"),
                classification.get("classification_hash"),
                row.get("ai_path_id"),
                row.get("ai_path_absent_literal"),
                row.get("render_ref"),
                row.get("render_ref"),
                row.get("result_id"),
                row.get("result_content_hash"),
                *render_pins,
            ),
        )
        verdict_rows = cur.fetchall() or []
    items = [
        {
            "run_id": value[0],
            "case_id": value[1],
            "dimension": value[2],
            "verdict": value[3],
            "reason_code": value[4],
            "evidence_refs": value[5] if isinstance(value[5], Mapping) else {},
            "created_at": _iso(value[6]),
            "owner_link": _owner_link(
                "test", "regression-runs", "evaluation-run", str(value[0])
            ),
        }
        for value in verdict_rows[:50]
    ]
    return {
        "state": "available",
        "reason": None,
        "items": items,
        "truncated": len(verdict_rows) > 50,
    }


def get_annotation(conn, *, org_id: str, project_id: str, feedback_id: str) -> dict[str, Any]:
    """One annotation with its pins, its lenses, its links and its review head."""
    with conn.cursor() as cur:
        cur.execute(
            _ANNOTATION_SELECT + " WHERE a.id = %s AND a.org_id = %s AND a.project_id = %s",
            (_required_text(feedback_id, "feedback_id"), org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise FeedbackNotFound("Feedback not found")
    model = _annotation_read_model(dict(zip(_ANNOTATION_FIELDS, row)))
    model["project_id"] = project_id
    versions = list_review_versions(
        conn,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        limit=_DEFAULT_PAGE_SIZE + 1,
    )
    model["review"]["versions"] = versions[:_DEFAULT_PAGE_SIZE]
    model["review"]["versions_truncated"] = len(versions) > _DEFAULT_PAGE_SIZE
    model["review"]["versions_next_cursor"] = (
        encode_review_cursor(
            project_id=project_id,
            feedback_id=feedback_id,
            version_number=versions[_DEFAULT_PAGE_SIZE - 1]["version_number"],
        )
        if len(versions) > _DEFAULT_PAGE_SIZE
        else None
    )
    mapped = dict(zip(_ANNOTATION_FIELDS, row))
    model["automated_verdicts"] = _automated_verdicts(
        conn, org_id=org_id, project_id=project_id, row=mapped
    )
    return model


def _encode_list_cursor(
    observed_at: Any,
    feedback_id: str,
    *,
    project_id: str,
    filters: Mapping[str, Any],
) -> str:
    timestamp = _iso(observed_at)
    if timestamp is None:
        raise FeedbackRefused("invalid_cursor", "Feedback row has no stable timestamp", [])
    raw = _canonical_json(
        [project_id, canonical_hash(filters), timestamp, feedback_id]
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_list_cursor(
    value: str | None, *, project_id: str, filters: Mapping[str, Any]
) -> tuple[datetime, str] | None:
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parts = json.loads(decoded)
    except (ValueError, TypeError, IndexError, json.JSONDecodeError) as exc:
        raise FeedbackRefused("invalid_cursor", "Invalid feedback cursor", []) from exc
    if not isinstance(parts, list) or len(parts) != 4 or not all(
        isinstance(part, str) for part in parts
    ):
        raise FeedbackRefused("invalid_cursor", "Invalid feedback cursor", [])
    if parts[:2] != [project_id, canonical_hash(filters)]:
        raise FeedbackRefused("invalid_cursor", "Cursor does not match this scope", [])
    try:
        timestamp = datetime.fromisoformat(parts[2].replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackRefused("invalid_cursor", "Invalid feedback cursor", []) from exc
    return timestamp, parts[3]


def encode_review_cursor(*, project_id: str, feedback_id: str, version_number: int) -> str:
    raw = _canonical_json([project_id, feedback_id, int(version_number)]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_review_cursor(
    value: str | None, *, project_id: str, feedback_id: str
) -> int | None:
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parts = json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FeedbackRefused("invalid_cursor", "Invalid review cursor", []) from exc
    if (
        not isinstance(parts, list)
        or len(parts) != 3
        or parts[:2] != [project_id, feedback_id]
        or not isinstance(parts[2], int)
        or isinstance(parts[2], bool)
        or parts[2] < 1
    ):
        raise FeedbackRefused("invalid_cursor", "Invalid review cursor", [])
    return parts[2]


def list_annotations(
    conn,
    *,
    org_id: str,
    project_id: str,
    filters: Mapping[str, Any] | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    polarity: str | None = None,
) -> dict[str, Any]:
    """Keyset-paged exact collection with the same closed filters as aggregates."""
    bounded = max(1, min(int(limit or _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE))
    normalized = _normalize_aggregate_filters(filters or {})
    _require_known_filter_owners(
        conn, org_id=org_id, project_id=project_id, filters=normalized
    )
    cursor_filters = dict(normalized)
    clauses = [
        "a.org_id = %s",
        "a.project_id = %s",
        "a.observed_at >= %s",
        "a.observed_at <= %s",
    ]
    params: list[Any] = [org_id, project_id]
    params.extend([normalized["observed_from"], normalized["observed_to"]])
    if polarity is not None:
        checked_polarity = _one_of(polarity, POLARITIES, "polarity")
        clauses.append("a.polarity = %s")
        params.append(checked_polarity)
        cursor_filters["polarity"] = checked_polarity
    direct = {
        "source": "a.source = %s",
        "surface": "a.observed_surface = %s",
        "target_kind": "a.target_kind = %s",
        "semantic_view_version_id": "qsv.semantic_view_version_id = %s",
        "visualization_spec_version_id": "a.visualization_spec_version_id = %s",
        "renderer_build_id": "a.renderer_build_id = %s",
        "runtime_build_id": "a.runtime_build_id = %s",
        "theme_version": "a.theme_version = %s",
        "formatter_version": "a.formatter_version = %s",
    }
    for name, expression in direct.items():
        if name in normalized:
            clauses.append(expression)
            params.append(normalized[name])
    for name, path in (
        ("capability", "capability"),
        ("result_type", "result_type"),
    ):
        if name in normalized:
            leaf = "key" if name == "capability" else "value"
            clauses.append(f"e.classification->'{path}'->>'{leaf}' = %s")
            params.append(normalized[name])
    if "business_domain_id" in normalized:
        clauses.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE("
            "e.classification->'business_domains'->'versions', '[]'::jsonb)) d "
            "WHERE d->>'id' = %s AND (d->>'version_number')::integer = %s)"
        )
        params.extend(
            [normalized["business_domain_id"], normalized["business_domain_version_number"]]
        )
    if "skill_version_id" in normalized:
        clauses.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE("
            "e.classification->'skills'->'versions', '[]'::jsonb)) s WHERE s = %s)"
        )
        params.append(normalized["skill_version_id"])
    after = _decode_list_cursor(cursor, project_id=project_id, filters=cursor_filters)
    if after is not None:
        clauses.append("(a.observed_at, a.id) < (%s, %s)")
        params.extend(after)
    params.append(bounded + 1)
    with conn.cursor() as cur:
        cur.execute(
            _ANNOTATION_SELECT
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY a.observed_at DESC, a.id DESC LIMIT %s",
            tuple(params),
        )
        rows = cur.fetchall() or []
    truncated = len(rows) > bounded
    rows = rows[:bounded]
    items = [_annotation_read_model(dict(zip(_ANNOTATION_FIELDS, r))) for r in rows]
    response = {
        "schema_version": "feedback-review-collection.v1",
        "project_id": project_id,
        "normalized_filters": cursor_filters,
        "limit": bounded,
        "items": items,
        "truncated": truncated,
        "next_cursor": (
            _encode_list_cursor(
                rows[-1][29],
                str(rows[-1][0]),
                project_id=project_id,
                filters=cursor_filters,
            )
            if truncated and rows
            else None
        ),
    }
    while rows and len(_canonical_json(response).encode("utf-8")) > 250_000:
        rows.pop()
        items.pop()
        response["truncated"] = True
        response["next_cursor"] = (
            _encode_list_cursor(
                rows[-1][29],
                str(rows[-1][0]),
                project_id=project_id,
                filters=cursor_filters,
            )
            if rows
            else None
        )
    return response


_REVIEW_VERSION_FIELDS = (
    "id",
    "version_number",
    "review_state",
    "affected_dimension",
    "human_verdict",
    "severity",
    "reason",
    "reviewer",
    "seed_requested_at",
    "seed_reason",
    "seed_target_kind",
    "seed_target_id",
    "predecessor_version_id",
    "created_at",
)


def _review_version_model(row: Sequence[Any]) -> dict[str, Any]:
    values = dict(zip(_REVIEW_VERSION_FIELDS, row))
    seed = None
    if values.get("seed_requested_at") is not None:
        seed = {
            "requested_at": _iso(values["seed_requested_at"]),
            "requested_by": values["reviewer"],
            "reason": values["seed_reason"],
            "target_kind": values["seed_target_kind"],
            "target_id": values["seed_target_id"],
            # An honest state, not a defect: Story 51.1 owns the object a seed
            # would become. The request is recorded; nothing was created.
            "target_state": (
                "linked"
                if values.get("seed_target_id")
                else "requested, target owner absent"
            ),
            "target_owner_story": "51.1",
        }
    return {
        "id": values["id"],
        "version_number": values["version_number"],
        "review_state": values["review_state"],
        "affected_dimension": values["affected_dimension"],
        # The HUMAN verdict, under a name that says so. Machine evidence, when
        # Stories 51.2/51.3 deliver it, is read from its own owner and returned
        # under its own distinct field; no response merges the two.
        "human_verdict": values["human_verdict"],
        "severity": values["severity"],
        "reason": values["reason"],
        "reviewer": values["reviewer"],
        "seed_request": seed,
        "predecessor_version_id": values["predecessor_version_id"],
        "created_at": _iso(values.get("created_at")),
        "machine_evidence": (
            MCP_APP_BEHAVIOR_ABSENT.as_lens()
            if values["affected_dimension"] == "mcp_app_behavior"
            else {
                "state": "unverifiable",
                "reason": "evaluation_run_owner_not_delivered",
                "owner_stories": ["51.2", "51.3"],
                "detail": (
                    "No Evaluation Run has judged this dimension. The human "
                    "verdict above stands alone and is reported as such."
                ),
            }
        ),
    }


def list_review_versions(
    conn,
    *,
    org_id: str,
    project_id: str,
    feedback_id: str,
    limit: int = _DEFAULT_PAGE_SIZE,
    before_version: int | None = None,
) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit or _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE + 1))
    before_clause = ""
    params: list[Any] = [feedback_id, org_id, project_id]
    if before_version is not None:
        before_clause = " AND version_number < %s"
        params.append(int(before_version))
    params.append(bounded)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_REVIEW_VERSION_FIELDS)}
            FROM app.feedback_review_versions
            WHERE feedback_id = %s AND org_id = %s AND project_id = %s
            {before_clause}
            ORDER BY version_number DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []
    return [_review_version_model(row) for row in rows]


# ---------------------------------------------------------------------------
# Review.
# ---------------------------------------------------------------------------


def append_review_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    feedback_id: str,
    reviewer: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Append/replay the exact feedback-review-command.v1 atomically."""
    exact_keys = {
        "schema_version",
        "expected_head",
        "retry_key",
        "state",
        "affected_dimension",
        "human_verdict",
        "severity",
        "reason",
    }
    if set(payload) != exact_keys or payload.get("schema_version") != "feedback-review-command.v1":
        raise FeedbackRefused(
            "invalid_review_command",
            "Review accepts exactly feedback-review-command.v1 fields.",
            [],
        )
    org_id = _required_text(org_id, "org_id")
    project_id = _required_text(project_id, "project_id")
    feedback_id = _required_text(feedback_id, "feedback_id")
    reviewer = _required_text(reviewer, "reviewer")
    expected_head = payload.get("expected_head")
    if expected_head is not None:
        expected_head = _required_text(expected_head, "expected_head")
    retry_key = _required_text(payload.get("retry_key"), "retry_key", maximum=200)
    retry_hash = canonical_hash({"purpose": "feedback-review", "retry_key": retry_key})
    state = _one_of(payload.get("state"), REVIEW_STATES, "state")
    dimension = _one_of(
        payload.get("affected_dimension"), AFFECTED_DIMENSIONS, "affected_dimension"
    )
    verdict = _one_of(payload.get("human_verdict"), HUMAN_VERDICTS, "human_verdict")
    severity = _one_of(payload.get("severity"), SEVERITIES, "severity")
    reason = _required_text(payload.get("reason"), "reason", maximum=_MAX_REASON)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_review_version_id
            FROM app.feedback_reviews
            WHERE feedback_id = %s AND org_id = %s AND project_id = %s
            FOR UPDATE
            """,
            (feedback_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise FeedbackNotFound("Feedback not found")
        predecessor = head[0]
        cur.execute(
            """
            SELECT r.review_version_id, v.version_number,
                   v.predecessor_version_id, v.review_state,
                   v.affected_dimension, v.human_verdict, v.severity, v.reason
            FROM app.feedback_review_retries r
            JOIN app.feedback_review_versions v
              ON v.id = r.review_version_id AND v.org_id = r.org_id
             AND v.project_id = r.project_id AND v.feedback_id = r.subject_id
            WHERE r.subject_id = %s AND r.org_id = %s AND r.project_id = %s
              AND r.reviewer = %s AND r.retry_key_hash = %s
            """,
            (feedback_id, org_id, project_id, reviewer, retry_hash),
        )
        replay = cur.fetchone()
        if replay is not None:
            supplied = (expected_head, state, dimension, verdict, severity, reason)
            if tuple(replay[2:]) != supplied:
                raise FeedbackRefused(
                    "idempotency_conflict",
                    "This retry key was already used for a different review command.",
                    [],
                )
            return {
                "schema_version": "feedback-review-receipt.v1",
                "status": "replayed",
                "feedback_id": feedback_id,
                "review_version_id": str(replay[0]),
                "version_number": int(replay[1]),
                "predecessor_version_id": replay[2],
            }
        if predecessor != expected_head:
            raise FeedbackRefused(
                "stale_review_head",
                "The review head changed; refresh before appending.",
                [Refusal("stale_review_head", "expected_head does not match", "expected_head")],
            )
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0)
            FROM app.feedback_review_versions
            WHERE feedback_id = %s AND org_id = %s AND project_id = %s
            """,
            (feedback_id, org_id, project_id),
        )
        next_number = int((cur.fetchone() or [0])[0]) + 1
        version_id = _mint("fbrv")
        cur.execute(
            """
            INSERT INTO app.feedback_review_versions
                (id, feedback_id, org_id, project_id, version_number,
                 review_state, affected_dimension, human_verdict, severity,
                 reason, reviewer, predecessor_version_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version_id,
                feedback_id,
                org_id,
                project_id,
                next_number,
                state,
                dimension,
                verdict,
                severity,
                reason,
                reviewer,
                predecessor,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.feedback_review_retries
                (subject_id, org_id, project_id, reviewer, retry_key_hash,
                 review_version_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (feedback_id, org_id, project_id, reviewer, retry_hash, version_id),
        )
        cur.execute(
            """
            UPDATE app.feedback_reviews
               SET current_review_version_id = %s, current_state = %s, updated_at = NOW()
             WHERE feedback_id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, state, feedback_id, org_id, project_id),
        )

    insert_audit_row(
        conn,
        identity=reviewer,
        action=ACTION_FEEDBACK_REVIEW_APPENDED,
        provider_account=project_id,
        connection_ref="",
        metadata={
            "feedback_id": feedback_id,
            "review_version_id": version_id,
            "review_state": state,
            "affected_dimension": dimension,
            "severity": severity,
        },
    )
    return {
        "schema_version": "feedback-review-receipt.v1",
        "status": "recorded",
        "feedback_id": feedback_id,
        "review_version_id": version_id,
        "version_number": next_number,
        "predecessor_version_id": predecessor,
    }


# ---------------------------------------------------------------------------
# Aggregates.
#
# `analyze-and-test.md:308-313` is the whole specification: five axes, positive
# and negative counts, the eligible/annotated denominator, coverage, unresolved
# critical negatives and the version filters used -- and never a raw thumbs-up
# percentage presented as correctness. There is no ratio of positives anywhere
# below, and no place to add one without deleting a comment that says why.
# ---------------------------------------------------------------------------

_AGGREGATE_ROW_FIELDS = (
    "id",
    "polarity",
    "result_id",
    "ai_path_id",
    "business_domain_id",
    "business_domain_version_number",
    "capability",
    "result_type",
    "semantic_view_id",
    "semantic_view_version_id",
    "current_state",
    "severity",
)


def _validate_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse an aggregate that cannot state its version filters.

    Two ways it cannot: a value that is not an exact version identity, and half a
    pin. Either one produces a figure whose provenance a reader cannot restate,
    which is the same as a figure nobody can check.
    """
    stated = {k: v for k, v in filters.items() if v is not None}
    _reject_unqualified_pins(stated, "version_filters")
    has_domain = "business_domain_id" in stated
    has_domain_version = "business_domain_version_number" in stated
    if has_domain != has_domain_version:
        raise FeedbackRefused(
            "incomplete_version_pin",
            "business_domain_id and business_domain_version_number filter together or not at all",
            [
                Refusal(
                    "incomplete_version_pin",
                    "half a Business Domain pin cannot be echoed as a filter",
                    "version_filters",
                )
            ],
        )
    return stated


def _bucket() -> dict[str, Any]:
    return {
        "positive": 0,
        "negative": 0,
        "annotated": 0,
        "annotated_results": set(),
        "unresolved_critical_negatives": 0,
        "negatives_without_review": 0,
    }


def _finalize_bucket(
    key: Mapping[str, Any] | None,
    raw: Mapping[str, Any],
    *,
    eligible: int | None,
    unattributed_reason: str | None,
    filters: Mapping[str, Any],
) -> dict[str, Any]:
    annotated_results = len(raw["annotated_results"])
    coverage: float | None = None
    coverage_state = "unverifiable"
    coverage_reason: str | None = ELIGIBLE_DENOMINATOR_ABSENT.reason
    if eligible is not None and eligible > 0:
        coverage = round(annotated_results / eligible, 6)
        coverage_state = "stated"
        coverage_reason = None
    elif eligible == 0:
        coverage_state = "unverifiable"
        coverage_reason = "no_eligible_observation_in_scope"
    return {
        "key": dict(key) if key else None,
        "attributed": key is not None,
        "unattributed_reason": unattributed_reason,
        "positive": raw["positive"],
        "negative": raw["negative"],
        "annotated": raw["annotated"],
        "annotated_results": annotated_results,
        "eligible_results": eligible,
        "coverage": coverage,
        "coverage_state": coverage_state,
        "coverage_reason": coverage_reason,
        "unresolved_critical_negatives": raw["unresolved_critical_negatives"],
        "negatives_without_review": raw["negatives_without_review"],
        "version_filters": dict(filters),
    }


def _accumulate(bucket: dict[str, Any], row: Mapping[str, Any]) -> None:
    bucket["annotated"] += 1
    bucket["annotated_results"].add(row["result_id"])
    if row["polarity"] == "positive":
        bucket["positive"] += 1
        return
    bucket["negative"] += 1
    state = row.get("current_state")
    severity = row.get("severity")
    if severity is None:
        # Never dropped and never assumed minor: an unreviewed negative is its
        # own fact, and folding it into either bucket would fabricate a triage
        # decision nobody made.
        bucket["negatives_without_review"] += 1
    elif severity == "critical" and state not in CLOSED_REVIEW_STATES:
        bucket["unresolved_critical_negatives"] += 1


def _axis(
    rows: Iterable[Mapping[str, Any]],
    key_of,
    *,
    eligible_by_key: Mapping[tuple, int] | None,
    unattributed_reason: str,
    filters: Mapping[str, Any],
) -> dict[str, Any]:
    buckets: dict[tuple, dict[str, Any]] = {}
    keys: dict[tuple, dict[str, Any]] = {}
    unattributed = _bucket()
    for row in rows:
        key = key_of(row)
        if key is None:
            _accumulate(unattributed, row)
            continue
        identity = tuple(sorted(key.items()))
        buckets.setdefault(identity, _bucket())
        keys[identity] = key
        _accumulate(buckets[identity], row)

    finalized = [
        _finalize_bucket(
            keys[identity],
            raw,
            eligible=(eligible_by_key or {}).get(identity),
            unattributed_reason=None,
            filters=filters,
        )
        for identity, raw in sorted(buckets.items(), key=lambda item: str(item[0]))
    ]
    # The `unattributed` bucket is emitted even when empty, so a reader can tell
    # "nothing was unattributed" from "the axis never reported it".
    finalized.append(
        _finalize_bucket(
            None,
            unattributed,
            eligible=None,
            unattributed_reason=unattributed_reason,
            filters=filters,
        )
    )
    return {"buckets": finalized}


def _eligible_by_semantic_view(
    conn, *, org_id: str, project_id: str, filters: Mapping[str, Any]
) -> dict[tuple, int]:
    clauses = ["qr.org_id = %s", "qr.project_id = %s"]
    params: list[Any] = [org_id, project_id]
    if filters.get("semantic_view_version_id"):
        clauses.append("qsv.semantic_view_version_id = %s")
        params.append(filters["semantic_view_version_id"])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT qsv.semantic_view_id, qsv.semantic_view_version_id, COUNT(*)
            FROM app.query_results qr
            JOIN app.query_spec_versions qsv
              ON qsv.id = qr.query_spec_version_id AND qsv.project_id = qr.project_id
            WHERE {" AND ".join(clauses)}
            GROUP BY 1, 2
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []
    return {
        tuple(sorted({"semantic_view_id": r[0], "semantic_view_version_id": r[1]}.items())): int(
            r[2]
        )
        for r in rows
    }


def _skills_by_annotation(
    conn, *, org_id: str, project_id: str
) -> dict[str, list[str]]:
    """The Skill axis, read from `app.ai_path_steps` -- the only Skill owner here.

    A Skill has no table in this repository; `skill_tool_catalog` is a read-only
    projection. The pinned path's steps are therefore the sole authority, and an
    annotation whose path touches several Skill versions belongs to each of their
    buckets. That makes the axis non-exclusive, which the response states rather
    than letting a reader add the buckets up.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, s.skill_version_id
            FROM app.feedback_annotations a
            JOIN app.ai_path_steps s
              ON s.path_id = a.ai_path_id AND s.org_id = a.org_id AND s.project_id = a.project_id
            WHERE a.org_id = %s AND a.project_id = %s AND s.skill_version_id IS NOT NULL
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall() or []
    grouped: dict[str, list[str]] = {}
    for annotation_id, skill_version in rows:
        grouped.setdefault(str(annotation_id), [])
        if skill_version not in grouped[str(annotation_id)]:
            grouped[str(annotation_id)].append(str(skill_version))
    return grouped


def _aggregate_feedback_story_51(
    conn,
    *,
    org_id: str,
    project_id: str,
    filters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Counts, denominators, coverage and the exact version filters that produced them."""
    org_id = _required_text(org_id, "org_id")
    project_id = _required_text(project_id, "project_id")
    stated_filters = _validate_filters(filters or {})

    clauses = ["a.org_id = %s", "a.project_id = %s"]
    params: list[Any] = [org_id, project_id]
    if stated_filters.get("semantic_view_version_id"):
        clauses.append("qsv.semantic_view_version_id = %s")
        params.append(stated_filters["semantic_view_version_id"])
    if stated_filters.get("business_domain_id"):
        clauses.append("a.business_domain_id = %s")
        params.append(stated_filters["business_domain_id"])
        clauses.append("a.business_domain_version_number = %s")
        params.append(stated_filters["business_domain_version_number"])
    if stated_filters.get("capability"):
        clauses.append("a.capability = %s")
        params.append(stated_filters["capability"])
    if stated_filters.get("result_type"):
        clauses.append("a.result_type = %s")
        params.append(_one_of(stated_filters["result_type"], RESULT_TYPES, "result_type"))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT a.id, a.polarity, a.result_id, a.ai_path_id,
                   a.business_domain_id, a.business_domain_version_number,
                   a.capability, a.result_type,
                   qsv.semantic_view_id, qsv.semantic_view_version_id,
                   rev.current_state, rv.severity
            FROM app.feedback_annotations a
            JOIN app.query_results qr
              ON qr.id = a.result_id AND qr.org_id = a.org_id AND qr.project_id = a.project_id
            JOIN app.query_spec_versions qsv
              ON qsv.id = qr.query_spec_version_id AND qsv.project_id = a.project_id
            LEFT JOIN app.feedback_reviews rev
              ON rev.feedback_id = a.id AND rev.project_id = a.project_id
            LEFT JOIN app.feedback_review_versions rv
              ON rv.id = rev.current_review_version_id AND rv.project_id = a.project_id
            WHERE {" AND ".join(clauses)}
            """,
            tuple(params),
        )
        raw_rows = cur.fetchall() or []
    rows = [dict(zip(_AGGREGATE_ROW_FIELDS, r)) for r in raw_rows]

    eligible_views = _eligible_by_semantic_view(
        conn, org_id=org_id, project_id=project_id, filters=stated_filters
    )
    skills = _skills_by_annotation(conn, org_id=org_id, project_id=project_id)

    # The Skill axis is one row per (annotation, Skill version): an annotation
    # whose path touched two Skills is evidence about both.
    skill_rows: list[dict[str, Any]] = []
    for row in rows:
        versions = skills.get(str(row["id"])) or []
        if not versions:
            skill_rows.append({**row, "skill_version_id": None})
            continue
        skill_rows.extend({**row, "skill_version_id": v} for v in versions)

    eligible_total = sum(eligible_views.values()) if eligible_views else 0
    annotated_results = len({row["result_id"] for row in rows})

    axes = {
        "business_domain": _axis(
            rows,
            lambda r: (
                {
                    "business_domain_id": r["business_domain_id"],
                    "version_number": r["business_domain_version_number"],
                }
                if r["business_domain_id"]
                else None
            ),
            eligible_by_key=None,
            unattributed_reason="annotation_states_no_business_domain",
            filters=stated_filters,
        ),
        "skill": {
            **_axis(
                skill_rows,
                lambda r: (
                    {"skill_version_id": r["skill_version_id"]}
                    if r.get("skill_version_id")
                    else None
                ),
                eligible_by_key=None,
                unattributed_reason="pinned_ai_path_records_no_skill_step",
                filters=stated_filters,
            ),
            "buckets_are_exclusive": False,
            "buckets_overlap_note": (
                "One annotation contributes to every Skill version its pinned AI "
                "Path touched. These buckets must not be summed."
            ),
        },
        "semantic_view": _axis(
            rows,
            lambda r: {
                "semantic_view_id": r["semantic_view_id"],
                "semantic_view_version_id": r["semantic_view_version_id"],
            },
            eligible_by_key=eligible_views,
            unattributed_reason="result_resolves_no_semantic_view_version",
            filters=stated_filters,
        ),
        "capability": _axis(
            rows,
            lambda r: {"capability": r["capability"]} if r["capability"] else None,
            eligible_by_key=None,
            unattributed_reason="annotation_states_no_capability",
            filters=stated_filters,
        ),
        "result_type": {
            **_axis(
                rows,
                lambda r: {"result_type": r["result_type"]} if r["result_type"] else None,
                eligible_by_key=None,
                unattributed_reason="annotation_states_no_result_type",
                filters=stated_filters,
            ),
            "axis_evidence": RESULT_TYPE_AXIS_ABSENT.as_lens(),
        },
    }

    return {
        "project_id": project_id,
        # Echoed, always: a group whose filters a reader cannot restate is a
        # number nobody can check.
        "version_filters": dict(stated_filters),
        "scope": {
            "eligible_results": eligible_total,
            "annotated_results": annotated_results,
            "annotations": len(rows),
            "coverage": (
                round(annotated_results / eligible_total, 6) if eligible_total else None
            ),
            "coverage_state": "stated" if eligible_total else "unverifiable",
            "coverage_reason": None if eligible_total else "no_eligible_observation_in_scope",
        },
        "axes": axes,
        "evidence_lenses": {
            "render": RENDER_ABSENT.as_lens(),
            "mcp_app_behavior": MCP_APP_BEHAVIOR_ABSENT.as_lens(),
        },
        "blocking_use": (
            "Prioritizes investigation; never proves correctness or regression alone."
        ),
    }


_AGGREGATE_FILTERS = frozenset(
    {
        "observed_from",
        "observed_to",
        "semantic_view_version_id",
        "business_domain_id",
        "business_domain_version_number",
        "skill_version_id",
        "capability",
        "result_type",
        "target_kind",
        "source",
        "surface",
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
    }
)
_RENDER_COHORT_FILTERS = (
    "visualization_spec_version_id",
    "renderer_build_id",
    "runtime_build_id",
    "theme_version",
    "formatter_version",
)


def _parse_filter_time(value: Any, name: str) -> datetime:
    text = _required_text(value, name)
    date_only = len(text) == 10
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackRefused(
            "invalid_filter", f"{name} must be an ISO-8601 timestamp", []
        ) from exc
    if parsed.tzinfo is None and date_only:
        parsed = parsed.replace(tzinfo=timezone.utc)
        if name == "observed_to":
            parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    elif parsed.tzinfo is None:
        raise FeedbackRefused("invalid_filter", f"{name} must include a timezone", [])
    return parsed.astimezone(timezone.utc)


def _normalize_aggregate_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(filters) - _AGGREGATE_FILTERS
    if unknown:
        raise FeedbackRefused(
            "unknown_filter", f"Unknown feedback filter: {sorted(unknown)[0]}", []
        )
    _reject_unqualified_pins(filters, "version_filters")
    observed_from = _parse_filter_time(filters.get("observed_from"), "observed_from")
    observed_to = _parse_filter_time(filters.get("observed_to"), "observed_to")
    if observed_to < observed_from or observed_to - observed_from > timedelta(days=90):
        raise FeedbackRefused(
            "invalid_filter_window",
            "observed_from/observed_to must be ordered and span at most 90 days",
            [],
        )
    normalized: dict[str, Any] = {
        "observed_from": observed_from.isoformat().replace("+00:00", "Z"),
        "observed_to": observed_to.isoformat().replace("+00:00", "Z"),
    }
    for name, value in filters.items():
        if name in {"observed_from", "observed_to"} or value is None or value == "":
            continue
        if name == "business_domain_version_number":
            try:
                parsed_version = int(value)
            except (TypeError, ValueError) as exc:
                raise FeedbackRefused(
                    "invalid_filter", f"{name} must be a positive integer", []
                ) from exc
            if parsed_version < 1:
                raise FeedbackRefused("invalid_filter", f"{name} must be a positive integer", [])
            normalized[name] = parsed_version
        else:
            normalized[name] = _required_text(value, name)
    if ("business_domain_id" in normalized) != (
        "business_domain_version_number" in normalized
    ):
        raise FeedbackRefused(
            "incomplete_version_pin",
            "Business Domain id and version number must be filtered together",
            [],
        )
    stated_render = [name in normalized for name in _RENDER_COHORT_FILTERS]
    if any(stated_render) and not all(stated_render):
        raise FeedbackRefused(
            "incomplete_version_pin", "Spec/build cohort filters must be complete", []
        )
    if "result_type" in normalized:
        _one_of(normalized["result_type"], RESULT_TYPES, "result_type")
    if normalized.get("target_kind") not in {None, "answer", "datum", "path_step"}:
        raise FeedbackRefused("invalid_vocabulary", "Unknown target_kind", [])
    if normalized.get("source") not in {None, "authenticated", "anonymous_share"}:
        raise FeedbackRefused("invalid_vocabulary", "Unknown source", [])
    if normalized.get("surface") not in {None, "console", "mcp_app", "share"}:
        raise FeedbackRefused("invalid_vocabulary", "Unknown surface", [])
    return normalized


def _require_known_filter_owners(
    conn,
    *,
    org_id: str,
    project_id: str,
    filters: Mapping[str, Any],
) -> None:
    """Refuse exact-looking filter pins whose scoped owner does not exist."""
    checks: list[tuple[str, str, tuple[Any, ...]]] = []
    if "semantic_view_version_id" in filters:
        checks.append(
            (
                "semantic_view_version_id",
                "SELECT 1 FROM app.semantic_view_versions WHERE id = %s "
                "AND project_id = %s",
                (filters["semantic_view_version_id"], project_id),
            )
        )
    if "business_domain_id" in filters:
        checks.append(
            (
                "business_domain",
                # Story 49.2: the aggregate filter checks the same authority-first
                # ledger as the pin it mirrors. Two guards on one fact must not
                # read two different stores.
                f"SELECT 1 FROM {catalogue.DOMAIN_VERSION_SOURCE} v "
                "WHERE v.domain_id = %s "
                "AND v.version_number = %s AND v.org_id = %s",
                (
                    filters["business_domain_id"],
                    filters["business_domain_version_number"],
                    org_id,
                ),
            )
        )
    if "skill_version_id" in filters:
        checks.append(
            (
                "skill_version_id",
                "SELECT 1 FROM app.ai_path_steps WHERE skill_version_id = %s AND org_id = %s "
                "AND project_id = %s LIMIT 1",
                (filters["skill_version_id"], org_id, project_id),
            )
        )
    if "capability" in filters:
        checks.append(
            (
                "capability",
                "SELECT 1 FROM app.project_capabilities WHERE project_id = %s "
                "AND capability_key = %s",
                (project_id, filters["capability"]),
            )
        )
    if "visualization_spec_version_id" in filters:
        render_values = tuple(filters[name] for name in _RENDER_COHORT_FILTERS)
        checks.append(
            (
                "render_cohort",
                """
                SELECT 1 FROM app.renders
                 WHERE org_id = %s AND project_id = %s
                   AND visualization_spec_version_id = %s
                   AND renderer_build_id = %s AND runtime_build_id = %s
                   AND theme_version = %s AND formatter_version = %s
                UNION ALL
                SELECT 1 FROM app.feedback_eligible_observations
                 WHERE org_id = %s AND project_id = %s
                   AND visualization_spec_version_id = %s
                   AND renderer_build_id = %s AND runtime_build_id = %s
                   AND theme_version = %s AND formatter_version = %s
                LIMIT 1
                """,
                (org_id, project_id, *render_values, org_id, project_id, *render_values),
            )
        )
    for subject, sql, params in checks:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            known = cur.fetchone()
        if known is None:
            raise FeedbackRefused(
                "unknown_filter_owner",
                f"Unknown scoped feedback filter owner: {subject}",
                [Refusal("unknown_filter_owner", "The exact owner is unavailable", subject)],
            )


def _aggregate_where(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    expressions = {
        "semantic_view_version_id": "e.classification->'semantic_view'->>'version_id' = %s",
        "capability": "e.classification->'capability'->>'key' = %s",
        "result_type": "e.classification->'result_type'->>'value' = %s",
        "source": "e.source = %s",
        "surface": "e.observed_surface = %s",
        "visualization_spec_version_id": "e.visualization_spec_version_id = %s",
        "renderer_build_id": "e.renderer_build_id = %s",
        "runtime_build_id": "e.runtime_build_id = %s",
        "theme_version": "e.theme_version = %s",
        "formatter_version": "e.formatter_version = %s",
    }
    for name, expression in expressions.items():
        if name in filters:
            clauses.append(expression)
            params.append(filters[name])
    if "business_domain_id" in filters:
        clauses.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements("
            "COALESCE(e.classification->'business_domains'->'versions', '[]'::jsonb)) d "
            "WHERE d->>'id' = %s AND (d->>'version_number')::integer = %s)"
        )
        params.extend(
            [filters["business_domain_id"], filters["business_domain_version_number"]]
        )
    if "skill_version_id" in filters:
        clauses.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements_text("
            "COALESCE(e.classification->'skills'->'versions', '[]'::jsonb)) s "
            "WHERE s = %s)"
        )
        params.append(filters["skill_version_id"])
    if "target_kind" in filters:
        clauses.append("COALESCE(e.authority->'target_kinds', '[]'::jsonb) ? %s")
        params.append(filters["target_kind"])
    return " AND ".join(clauses) or "TRUE", params


_AGGREGATE_FEEDBACK_CTE = """
feedback_union AS (
    SELECT id, org_id, project_id, 'authenticated'::text AS source,
           interaction_ref, polarity, observed_at, observed_surface,
           target_kind, visualization_spec_version_id, renderer_build_id,
           runtime_build_id, theme_version, formatter_version
      FROM app.feedback_annotations
    UNION ALL
    SELECT id, org_id, project_id, 'anonymous_share'::text,
           interaction_ref,
           CASE polarity WHEN 'helpful' THEN 'positive' ELSE 'negative' END,
           submitted_at, observed_surface, target_kind,
           visualization_spec_version_id, renderer_build, runtime_build,
           theme_version, formatter_version
      FROM app.render_share_feedback
)
"""


def _historical_where(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses = ["f.observed_at >= %s", "f.observed_at <= %s"]
    params: list[Any] = [filters["observed_from"], filters["observed_to"]]
    classification_filters = {
        "semantic_view_version_id",
        "business_domain_id",
        "business_domain_version_number",
        "skill_version_id",
        "capability",
        "result_type",
    }
    if classification_filters.intersection(filters):
        clauses.append("FALSE")
    direct = {
        "source": "f.source = %s",
        "surface": "f.observed_surface = %s",
        "target_kind": "f.target_kind = %s",
        "visualization_spec_version_id": "f.visualization_spec_version_id = %s",
        "renderer_build_id": "f.renderer_build_id = %s",
        "runtime_build_id": "f.runtime_build_id = %s",
        "theme_version": "f.theme_version = %s",
        "formatter_version": "f.formatter_version = %s",
    }
    for name, expression in direct.items():
        if name in filters:
            clauses.append(expression)
            params.append(filters[name])
    return " AND ".join(clauses), params


def _historical_aggregate_rows(
    conn,
    *,
    org_id: str,
    project_id: str,
    filters: Mapping[str, Any],
    limit: int,
    after_id: str | None = None,
) -> list[Sequence[Any]]:
    where, values = _historical_where(filters)
    after_clause = ""
    if after_id is not None:
        after_clause = "AND f.id > %s"
        values.append(after_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH {_AGGREGATE_FEEDBACK_CTE}
            SELECT f.id, f.polarity, f.source, f.interaction_ref,
                   COUNT(*) AS annotations,
                   COUNT(*) FILTER (
                       WHERE f.polarity = 'negative' AND rv.severity = 'critical'
                         AND COALESCE(fr.current_state, 'unreviewed')
                             NOT IN ('resolved', 'rejected', 'duplicate')
                   ) AS unresolved
              FROM feedback_union f
              LEFT JOIN app.feedback_eligible_observations e
                ON e.org_id = f.org_id AND e.project_id = f.project_id
               AND e.source = f.source AND e.interaction_ref = f.interaction_ref
              LEFT JOIN app.feedback_reviews fr
                ON fr.feedback_id = f.id AND fr.org_id = f.org_id
               AND fr.project_id = f.project_id
              LEFT JOIN app.feedback_review_versions rv
                ON rv.id = fr.current_review_version_id AND rv.org_id = fr.org_id
               AND rv.project_id = fr.project_id
             WHERE f.org_id = %s AND f.project_id = %s AND e.id IS NULL
               AND {where}
               {after_clause}
             GROUP BY f.id, f.polarity, f.source, f.interaction_ref, f.observed_at
             ORDER BY f.id
             LIMIT %s
            """,
            (org_id, project_id, *values, limit),
        )
        return list(cur.fetchall() or [])


def _historical_aggregate_scope(
    conn, *, org_id: str, project_id: str, filters: Mapping[str, Any]
) -> tuple[int, int]:
    where, values = _historical_where(filters)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH {_AGGREGATE_FEEDBACK_CTE}
            SELECT COUNT(DISTINCT (f.source, COALESCE(f.interaction_ref, f.id))),
                   COUNT(f.id)
              FROM feedback_union f
              LEFT JOIN app.feedback_eligible_observations e
                ON e.org_id = f.org_id AND e.project_id = f.project_id
               AND e.source = f.source AND e.interaction_ref = f.interaction_ref
             WHERE f.org_id = %s AND f.project_id = %s AND e.id IS NULL
               AND {where}
            """,
            (org_id, project_id, *values),
        )
        row = cur.fetchone() or (0, 0)
    return int(row[0]), int(row[1])


def _axis_expression(axis: str) -> tuple[str, str]:
    if axis == "semantic_view":
        return (
            "LATERAL (SELECT CASE WHEN e.classification->'semantic_view'->>'state' = "
            "'attributed' THEN jsonb_build_object('semantic_view_id', "
            "e.classification->'semantic_view'->>'id', 'semantic_view_version_id', "
            "e.classification->'semantic_view'->>'version_id') END AS bucket_key) axis",
            "semantic_view_unavailable",
        )
    if axis == "capability":
        return (
            "LATERAL (SELECT CASE WHEN e.classification->'capability'->>'state' = "
            "'attributed' THEN jsonb_build_object('capability', "
            "e.classification->'capability'->>'key', 'version_id', "
            "e.classification->'capability'->>'version_id') END AS bucket_key) axis",
            "capability_unavailable",
        )
    if axis == "result_type":
        return (
            "LATERAL (SELECT CASE WHEN e.classification->'result_type'->>'state' = "
            "'attributed' THEN jsonb_build_object('result_type', "
            "e.classification->'result_type'->>'value') END AS bucket_key) axis",
            "result_type_unavailable",
        )
    if axis == "business_domain":
        return (
            "LATERAL (SELECT CASE WHEN d.value <> 'null'::jsonb THEN jsonb_build_object("
            "'business_domain_id', d.value->>'id', 'version_number', "
            "(d.value->>'version_number')::integer) END AS bucket_key FROM "
            "jsonb_array_elements(CASE WHEN jsonb_array_length(COALESCE("
            "e.classification->'business_domains'->'versions', '[]'::jsonb)) > 0 THEN "
            "e.classification->'business_domains'->'versions' ELSE '[null]'::jsonb END) d) axis",
            "business_domain_unavailable",
        )
    return (
        "LATERAL (SELECT CASE WHEN s.value <> 'null'::jsonb THEN jsonb_build_object("
        "'skill_version_id', s.value #>> '{}') END AS bucket_key FROM "
        "jsonb_array_elements(CASE WHEN jsonb_array_length(COALESCE("
        "e.classification->'skills'->'versions', '[]'::jsonb)) > 0 THEN "
        "e.classification->'skills'->'versions' ELSE '[null]'::jsonb END) s) axis",
        "skill_unavailable",
    )


def _aggregate_axis_rows(
    conn,
    *,
    org_id: str,
    project_id: str,
    filters: Mapping[str, Any],
    axis_name: str,
    after: tuple[str, str] | None = None,
    limit: int = _MAX_PAGE_SIZE + 1,
) -> list[Sequence[Any]]:
    where, values = _aggregate_where(filters)
    lateral, reason = _axis_expression(axis_name)
    annotation_target_join = (
        "AND f.target_kind = %s" if "target_kind" in filters else ""
    )
    annotation_target_values = (
        [filters["target_kind"]] if "target_kind" in filters else []
    )
    having = ""
    cursor_values: list[Any] = []
    if after is not None:
        having = (
            "HAVING (COALESCE(axis.bucket_key::text, '~'), e.compatibility_key) > (%s, %s)"
        )
        cursor_values.extend(after)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH {_AGGREGATE_FEEDBACK_CTE}, eligible AS (
                SELECT e.* FROM app.feedback_eligible_observations e
                 WHERE e.org_id = %s AND e.project_id = %s AND {where}
            )
            SELECT axis.bucket_key, COALESCE(axis.bucket_key::text, '~') AS key_token,
                   e.compatibility_key,
                   COUNT(f.id) FILTER (WHERE f.polarity = 'positive') AS positive,
                   COUNT(f.id) FILTER (WHERE f.polarity = 'negative') AS negative,
                   COUNT(f.id) AS annotations,
                   COUNT(DISTINCT (f.source, f.interaction_ref)) AS annotated_interactions,
                   COUNT(DISTINCT (e.source, e.interaction_ref)) AS eligible_interactions,
                   COUNT(f.id) FILTER (
                       WHERE f.polarity = 'negative' AND rv.severity = 'critical'
                         AND COALESCE(fr.current_state, 'unreviewed')
                             NOT IN ('resolved', 'rejected', 'duplicate')
                   ) AS unresolved,
                   %s::text AS unavailable_reason
              FROM eligible e
              CROSS JOIN {lateral}
              LEFT JOIN feedback_union f
                ON f.org_id = e.org_id AND f.project_id = e.project_id
               AND f.source = e.source AND f.interaction_ref = e.interaction_ref
               {annotation_target_join}
              LEFT JOIN app.feedback_reviews fr
                ON fr.feedback_id = f.id AND fr.org_id = f.org_id
               AND fr.project_id = f.project_id
              LEFT JOIN app.feedback_review_versions rv
                ON rv.id = fr.current_review_version_id AND rv.org_id = fr.org_id
               AND rv.project_id = fr.project_id
             WHERE COALESCE(f.observed_at, e.observed_at) >= %s
               AND COALESCE(f.observed_at, e.observed_at) <= %s
             GROUP BY axis.bucket_key, e.compatibility_key
             {having}
             ORDER BY key_token, e.compatibility_key
             LIMIT %s
            """,
            (
                org_id,
                project_id,
                *values,
                reason,
                *annotation_target_values,
                filters["observed_from"],
                filters["observed_to"],
                *cursor_values,
                limit,
            ),
        )
        return list(cur.fetchall() or [])


def _aggregate_cursor(
    value: Sequence[str], *, org_id: str, project_id: str, filters: Mapping[str, Any]
) -> str:
    raw = _canonical_json(
        [org_id, project_id, canonical_hash(filters), *list(value)]
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_aggregate_cursor(
    value: str | None, *, org_id: str, project_id: str, filters: Mapping[str, Any]
) -> tuple[str, str, str] | None:
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parts = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise FeedbackRefused("invalid_cursor", "Invalid aggregate cursor", []) from exc
    if not isinstance(parts, list) or len(parts) != 6 or not all(
        isinstance(part, str) for part in parts
    ):
        raise FeedbackRefused("invalid_cursor", "Invalid aggregate cursor", [])
    if parts[:3] != [org_id, project_id, canonical_hash(filters)]:
        raise FeedbackRefused("invalid_cursor", "Cursor does not match this scope", [])
    if parts[3] not in {"business_domain", "skill", "semantic_view", "capability", "result_type"}:
        raise FeedbackRefused("invalid_cursor", "Invalid aggregate cursor", [])
    if len(parts[4]) > 512 or (
        parts[5] != "unavailable"
        and (len(parts[5]) != 64 or any(char not in "0123456789abcdef" for char in parts[5]))
    ):
        raise FeedbackRefused("invalid_cursor", "Invalid aggregate cursor", [])
    return parts[3], parts[4], parts[5]


def aggregate_feedback(
    conn,
    *,
    org_id: str,
    project_id: str,
    filters: Mapping[str, Any] | None = None,
    limit: int = _MAX_PAGE_SIZE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Bounded SQL cohorts over exact eligible interactions and annotations."""
    org_id = _required_text(org_id, "org_id")
    project_id = _required_text(project_id, "project_id")
    normalized = _normalize_aggregate_filters(filters or {})
    _require_known_filter_owners(
        conn, org_id=org_id, project_id=project_id, filters=normalized
    )
    bounded = max(1, min(int(limit or _MAX_PAGE_SIZE), _MAX_PAGE_SIZE))
    after = _decode_aggregate_cursor(
        cursor, org_id=org_id, project_id=project_id, filters=normalized
    )
    axes_order = ("business_domain", "skill", "semantic_view", "capability", "result_type")
    axis_rank = {name: index for index, name in enumerate(axes_order)}
    flattened: list[tuple[str, str, str, dict[str, Any]]] = []
    axes: dict[str, Any] = {}
    for axis_name in axes_order:
        buckets: list[dict[str, Any]] = []
        if after is not None and axis_rank[axis_name] < axis_rank.get(after[0], -1):
            axes[axis_name] = {
                "buckets": buckets,
                "buckets_are_exclusive": axis_name not in {"business_domain", "skill"},
                "buckets_overlap_note": (
                    "Buckets overlap and must not be summed."
                    if axis_name in {"business_domain", "skill"}
                    else None
                ),
            }
            continue
        for row in _aggregate_axis_rows(
            conn,
            org_id=org_id,
            project_id=project_id,
            filters=normalized,
            axis_name=axis_name,
            after=(after[1], after[2]) if after is not None and axis_name == after[0] else None,
            limit=bounded + 1,
        ):
            key, key_text, compatibility_key = row[0], str(row[1]), str(row[2])
            positive, negative, annotations = int(row[3]), int(row[4]), int(row[5])
            annotated, eligible = int(row[6]), int(row[7])
            bucket = {
                "key": key,
                "attributed": key is not None,
                "unattributed_reason": None if key is not None else str(row[9]),
                "positive": positive,
                "negative": negative,
                "annotations": annotations,
                "annotated_interactions": annotated,
                "eligible_interactions": eligible,
                "coverage": round(annotated / eligible, 6) if eligible else None,
                "coverage_state": "stated" if eligible else "unavailable",
                "coverage_reason": None if eligible else "no_eligible_observation_in_scope",
                "unresolved_critical_negatives": int(row[8]),
                "normalized_filters": dict(normalized),
                "compatibility_key": compatibility_key,
            }
            flattened.append((axis_name, key_text, compatibility_key, bucket))
        historical_after = None
        if (
            after is not None
            and axis_name == after[0]
            and after[1].startswith("~historical:")
        ):
            historical_after = after[1].removeprefix("~historical:")
        historical_rows = _historical_aggregate_rows(
            conn,
            org_id=org_id,
            project_id=project_id,
            filters=normalized,
            limit=bounded + 1,
            after_id=historical_after,
        )
        for historical_row in historical_rows:
            feedback_id = str(historical_row[0])
            key_text = f"~historical:{feedback_id}"
            candidate = (axis_name, key_text, "unavailable")
            if after is not None and (
                axis_rank[axis_name], key_text, "unavailable"
            ) <= (axis_rank[after[0]], after[1], after[2]):
                continue
            polarity = str(historical_row[1])
            bucket = {
                "key": None,
                "attributed": False,
                "unattributed_reason": "historical_eligibility_unavailable",
                "positive": int(historical_row[4]) if polarity == "positive" else 0,
                "negative": int(historical_row[4]) if polarity == "negative" else 0,
                "annotations": int(historical_row[4]),
                "annotated_interactions": 1,
                "eligible_interactions": None,
                "coverage": None,
                "coverage_state": "unavailable",
                "coverage_reason": "historical_eligibility_unavailable",
                "unresolved_critical_negatives": int(historical_row[5]),
                "normalized_filters": dict(normalized),
                "compatibility_key": "unavailable",
            }
            flattened.append((*candidate, bucket))
        axes[axis_name] = {
            "buckets": buckets,
            "buckets_are_exclusive": axis_name not in {"business_domain", "skill"},
            "buckets_overlap_note": (
                "Buckets overlap and must not be summed."
                if axis_name in {"business_domain", "skill"}
                else None
            ),
        }
    flattened.sort(key=lambda item: (axis_rank[item[0]], item[1], item[2]))
    page = flattened[: bounded + 1]
    truncated = len(page) > bounded
    page = page[:bounded]
    for axis_name, _key_text, _compatibility_key, bucket in page:
        axes[axis_name]["buckets"].append(bucket)

    where, values = _aggregate_where(normalized)
    annotation_target_join = (
        "AND f.target_kind = %s" if "target_kind" in normalized else ""
    )
    annotation_target_values = (
        [normalized["target_kind"]] if "target_kind" in normalized else []
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH {_AGGREGATE_FEEDBACK_CTE}, eligible AS (
                SELECT e.* FROM app.feedback_eligible_observations e
                 WHERE e.org_id = %s AND e.project_id = %s AND {where}
            )
            SELECT COUNT(DISTINCT (e.source, e.interaction_ref)),
                   COUNT(DISTINCT (f.source, f.interaction_ref)), COUNT(f.id)
              FROM eligible e
              LEFT JOIN feedback_union f
                ON f.org_id = e.org_id AND f.project_id = e.project_id
               AND f.source = e.source AND f.interaction_ref = e.interaction_ref
               {annotation_target_join}
              WHERE COALESCE(f.observed_at, e.observed_at) >= %s
                AND COALESCE(f.observed_at, e.observed_at) <= %s
            """,
            (
                org_id,
                project_id,
                *values,
                *annotation_target_values,
                normalized["observed_from"],
                normalized["observed_to"],
            ),
        )
        scope_row = cur.fetchone() or (0, 0, 0)
    eligible, annotated, annotations = map(int, scope_row)
    historical_interactions, historical_annotations = _historical_aggregate_scope(
        conn,
        org_id=org_id,
        project_id=project_id,
        filters=normalized,
    )
    annotated += historical_interactions
    annotations += historical_annotations
    has_historical = historical_annotations > 0
    scope_eligible: int | None = eligible if not has_historical else None
    response = {
        "schema_version": "feedback-aggregate.v1",
        "compatibility_schema_version": "feedback-compatibility.v1",
        "project_id": project_id,
        "normalized_filters": dict(normalized),
        "scope": {
            "eligible_interactions": scope_eligible,
            "annotated_interactions": annotated,
            "annotations": annotations,
            "coverage": (
                round(annotated / eligible, 6) if eligible and not has_historical else None
            ),
            "coverage_state": (
                "stated" if eligible and not has_historical else "unavailable"
            ),
            "coverage_reason": (
                None
                if eligible and not has_historical
                else "historical_eligibility_unavailable"
                if has_historical
                else "no_eligible_observation_in_scope"
            ),
        },
        "axes": axes,
        "truncated": truncated,
        "next_cursor": (
            _aggregate_cursor(
                page[-1][:3], org_id=org_id, project_id=project_id, filters=normalized
            )
            if truncated and page
            else None
        ),
        "blocking_use": "Prioritizes investigation; never proves correctness or regression alone.",
    }
    while page and len(_canonical_json(response).encode("utf-8")) > 250_000:
        axis_name, _key_text, _compatibility_key, _bucket = page.pop()
        axes[axis_name]["buckets"].pop()
        response["truncated"] = True
        response["next_cursor"] = (
            _aggregate_cursor(
                page[-1][:3], org_id=org_id, project_id=project_id, filters=normalized
            )
            if page
            else None
        )
    return response


def list_unresolved_critical_negatives(
    conn,
    *,
    org_id: str,
    project_id: str,
    limit: int = _DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The improvement leads: a critical negative nobody has closed, with its owners."""
    bounded = max(1, min(int(limit or _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE))
    cursor_filters = {"critical_negatives": True}
    after = _decode_list_cursor(cursor, project_id=project_id, filters=cursor_filters)
    after_clause = ""
    params: list[Any] = [org_id, project_id]
    if after is not None:
        after_clause = "AND (a.observed_at, a.id) < (%s, %s)"
        params.extend(after)
    params.append(bounded + 1)
    with conn.cursor() as cur:
        cur.execute(
            _ANNOTATION_SELECT
            + f"""
              JOIN app.feedback_review_versions rv
                ON rv.id = rev.current_review_version_id AND rv.project_id = a.project_id
             WHERE a.org_id = %s AND a.project_id = %s
               AND a.polarity = 'negative'
               AND rv.severity = 'critical'
               AND rev.current_state NOT IN ('resolved', 'rejected', 'duplicate')
               {after_clause}
             ORDER BY a.observed_at DESC, a.id DESC
             LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []
    truncated = len(rows) > bounded
    rows = rows[:bounded]
    response = {
        "schema_version": "feedback-critical-negatives.v1",
        "project_id": project_id,
        "items": [_annotation_read_model(dict(zip(_ANNOTATION_FIELDS, r))) for r in rows],
        "truncated": truncated,
        "next_cursor": (
            _encode_list_cursor(
                rows[-1][29],
                str(rows[-1][0]),
                project_id=project_id,
                filters=cursor_filters,
            )
            if truncated and rows
            else None
        ),
        "seed_target_owner_story": "51.1",
    }
    while rows and len(_canonical_json(response).encode("utf-8")) > 250_000:
        rows.pop()
        response["items"].pop()
        response["truncated"] = True
        response["next_cursor"] = (
            _encode_list_cursor(
                rows[-1][29],
                str(rows[-1][0]),
                project_id=project_id,
                filters=cursor_filters,
            )
            if rows
            else None
        )
    return response
