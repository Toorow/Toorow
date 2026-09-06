"""Story 51.4 -- Observed Cohorts and the Trace Observation read model.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE. The observed mode **describes**; it
never **decides**. `analyze-and-test.md:215` is literal: an Observed Cohort
"detects drift and proposes Golden Questions; cannot block by itself until
reproduced offline", and `:357-358` adds "observed cohorts use reference windows,
not deterministic baselines". So there is no approve, promote, baseline, gate or
block operation anywhere in this file, and no route in
``core.trace_observation_api`` offers one. Migration 153 proves the same absence
from the other side: the cohort tables carry no approval column, and
``app.assert_baseline_run_is_offline`` refuses to approve an ``observed_cohort``
run as a baseline.

FOUR PROPERTIES, in the order they matter.

1. **A pin is exact or it is not a pin.** ``latest``, ``current`` and ``head`` are
   refused for every version value (`analyze-and-test.md:230`), and a Semantic
   View id without its version id -- or a Business Domain id without its version
   number -- is refused as half a pin. The database says the same thing through
   ``app.is_exact_version_pin`` and the ``*_pin`` CHECKs of migration 153; this
   module refuses earlier and names the offending field.

2. **Membership is frozen evidence, not a saved filter.** Resolution happens
   once. ``member_count`` is stored at ``resolved_at`` and is the denominator of
   every aggregate over the cohort, so a later erasure cannot silently improve a
   historical percentage. Re-resolving mints a NEW cohort. Immutability is
   enforced by ``trg_observed_cohorts_immutable`` and
   ``trg_observed_cohort_members_immutable`` (migration 153, section 10) -- this
   module never issues an UPDATE or DELETE against either table, so Python is not
   the only rampart.

3. **A missing pin is a stated absence, never a default.** Three pins have no
   owner today: the rendered artifact (Stories 50.4 / 50.5 / 50.7), the evaluated
   MCP App behaviour (Story 50.6) and pinned feedback (Story 51.5). Each renders
   :data:`VERDICT_UNVERIFIABLE` with its reason code and the story that will lift
   it. Never ``pass``, never ``fail``, never a placeholder id, and never a
   ``future_owner`` stand-in.

4. **A percentage without a denominator is a defect.** Every aggregate entry
   produced here carries ``numerator``, ``denominator``, ``coverage`` and
   ``version_filters``; :func:`assert_aggregate_is_complete` refuses an entry that
   lost any of the four (`analyze-and-test.md:310-313`).

WHAT THIS MODULE READS AND NEVER WRITES. ``app.ai_paths`` and
``app.ai_path_steps`` belong to Story 49.6, ``app.query_results`` and
``app.query_result_payloads`` to Story 50.1, and every Golden Question table to
Story 51.1. This module is a reader of all of them. Assessment is delegated to
:func:`core.ai_paths.assess`, which already distinguishes missing evidence from
evidence of a deviation; adding a second assessor here would let the two drift.

WHAT AN OBSERVED SELECTION CAN AND CANNOT PROVE TODAY. Five of the cohort's pins
can be enforced against stored evidence -- the time window, ``model_ref``,
``tool_catalog_version`` and the Semantic View id/version pair (through the
Result and its Query Spec version). The others -- surface, actor class, Business
Domain, capability, result type and the Context Version Set hash -- have no
column on ``app.ai_paths`` to match against. They are STORED as the cohort's
declared intent (AC1 requires the columns) and reported as
``declared_not_enforced`` with the reason, rather than silently narrowing nothing
while looking like a filter. Concealing that would be the same defect as a
percentage without its denominator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from core import skill_steps
from core.ai_paths import (
    FINDING_FORBIDDEN_OBSERVED,
    FINDING_OUT_OF_ORDER,
    FINDING_REQUIRED_MISSING,
    FINDING_UNVERIFIABLE,
    FINDING_VERSION_MISMATCH,
    OUTCOME_UNAVAILABLE,
    VERDICT_UNVERIFIABLE,
    assess,
)
from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary. Every literal below is the one migration 153 already enforces --
# a second spelling here would be a synonym, and the glossary forbids synonyms.
# ---------------------------------------------------------------------------

#: The exact label AC3 requires the read model to carry. Written once, so no
#: screen invents a softer wording for "this cannot block".
OBSERVED_COHORT_LABEL = "Observed Cohort - reference window, non-blocking"

#: `analyze-and-test.md:282-284`. `unverifiable` is not a soft `fail` and
#: `not_applicable` is not a silent `pass`.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_NOT_APPLICABLE = "not_applicable"

#: `ck_observed_cohort_members_path_state` (migration 153).
PATH_EVIDENCE_OBSERVED = "observed"
PATH_EVIDENCE_UNVERIFIABLE = "unverifiable"

#: `ck_observed_cohort_members_render_state`. `pinned` is unreachable while
#: `ck_observed_cohort_members_render_unpinned` holds `render_ref` NULL.
RENDER_EVIDENCE_UNVERIFIABLE = "unverifiable"
RENDER_EVIDENCE_PINNED = "pinned"

#: The three absences this story reports rather than fills. Each names the exact
#: story that will lift it -- never a placeholder owner.
RENDER_ABSENCE = {
    "state": VERDICT_UNVERIFIABLE,
    "reason": "render_owner_not_delivered",
    "owner": "Stories 50.4 / 50.5 / 50.7",
    "detail": (
        "The rendered artifact object does not exist and the rendering stack is "
        "not installed. Its pin is declared and held NULL by "
        "ck_observed_cohort_members_render_unpinned."
    ),
}
MCP_APP_ABSENCE = {
    "state": VERDICT_UNVERIFIABLE,
    "reason": "mcp_app_behavior_owner_not_delivered",
    "owner": "Story 50.6",
    "detail": "Evaluated MCP App behaviour has no owner, so it cannot pass or fail.",
}
LINKED_FEEDBACK_ABSENCE = {
    "state": VERDICT_UNVERIFIABLE,
    "reason": "pinned_feedback_owner_not_delivered",
    "owner": "Story 51.5",
    "detail": (
        "app.feedback (migration 012) carries no Result, rendered artifact or AI "
        "Path reference. Matching its trace_id against app.ai_paths.w3c_trace_id "
        "would present correlation as identity, which migration 150:99-104 "
        "refuses in the schema itself."
    ),
}

#: `ck_golden_question_proposals_reason` (migration 153).
PROPOSAL_REASON_CODES = (
    "required_node_missing",
    "forbidden_node_used",
    "out_of_order",
    "version_mismatch",
    "path_unverifiable",
    "result_refused",
    "result_degraded",
    "result_unavailable",
)
PROPOSAL_SEVERITIES = ("low", "medium", "high", "critical")
PROPOSAL_STATES = ("proposed", "accepted", "declined")

#: `ck_observed_cohorts_surface` and `ck_observed_cohorts_result_type`.
SURFACES = ("mcp-app", "console", "api")
RESULT_TYPES = (
    "scalar",
    "series",
    "breakdown",
    "comparison",
    "table",
    "narrative",
    "refusal",
)

#: `app.is_exact_version_pin` refuses exactly these three, case-insensitively.
FORBIDDEN_VERSION_VALUES = frozenset({"latest", "current", "head"})

#: The five ratified lenses of `analyze-and-test.md:293`, in their ratified
#: order. The console tab slugs are these strings; a sixth lens is a contract
#: change, not an addition.
LENS_TIMELINE = "timeline"
LENS_CONTEXT_SKILLS = "context-skills"
LENS_TOOLS = "tools"
LENS_RESULT_RENDER = "result-render"
LENS_LINKED_FEEDBACK = "linked-feedback"
TRACE_OBSERVATION_LENSES = (
    LENS_TIMELINE,
    LENS_CONTEXT_SKILLS,
    LENS_TOOLS,
    LENS_RESULT_RENDER,
    LENS_LINKED_FEEDBACK,
)

#: Tables this story READS and must never write. The test asserts the absence of
#: any mutation verb against them in this module's own source.
FOREIGN_OWNED_TABLES = (
    "app.ai_paths",
    "app.ai_path_steps",
    "app.query_results",
    "app.query_result_payloads",
    "app.golden_questions",
    "app.golden_question_versions",
    "app.golden_question_reference_paths",
)

#: Only a finalized AI Path is admissible evidence. A recording path can still
#: grow steps, so a cohort that admitted one would freeze a denominator over
#: membership that keeps changing -- the same rule `ai_path_reference` applies to
#: a Result.
_ADMISSIBLE_LIFECYCLE = "finalized"

_MAX_PAGE = 200
_DEFAULT_PAGE = 50


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class TraceObservationError(ValueError):
    """An observed-evidence operation was rejected."""

    code = "invalid_observed_evidence_operation"


class TraceObservationNotFound(TraceObservationError):
    """Foreign, denied and absent all raise this. The caller cannot tell them apart."""

    code = "not_found"


class TraceObservationRefused(TraceObservationError):
    """A structured refusal naming every offending field.

    A refusal that only says "invalid" makes the caller guess, and guessing is
    how an unpinned version gets stored.
    """

    code = "refused"

    def __init__(self, reasons: Sequence[Mapping[str, Any]]):
        self.reasons = [dict(reason) for reason in reasons]
        super().__init__("; ".join(str(reason.get("message")) for reason in self.reasons))

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "reasons": self.reasons}


def _mint(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TraceObservationError(f"{label} is required")
    return value.strip()


# ---------------------------------------------------------------------------
# Pin validation (AC1). Pure -- it never touches a connection, so it is testable
# on its own and cannot be satisfied by a lenient fixture.
# ---------------------------------------------------------------------------


def _is_exact_version_pin(candidate: Any) -> bool:
    """The Python side of ``app.is_exact_version_pin``."""
    if not isinstance(candidate, str):
        return False
    trimmed = candidate.strip()
    return bool(trimmed) and trimmed.lower() not in FORBIDDEN_VERSION_VALUES


def _as_datetime(value: Any, field: str, reasons: list[dict[str, Any]]) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            reasons.append(
                {
                    "code": "invalid_timestamp",
                    "field": field,
                    "message": f"{field} must be an ISO-8601 timestamp",
                }
            )
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    reasons.append(
        {"code": "missing_pin", "field": field, "message": f"{field} is required"}
    )
    return None


def _bounded(value: Any, field: str, reasons: list[dict[str, Any]], limit: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        reasons.append(
            {
                "code": "invalid_value",
                "field": field,
                "message": f"{field} must be a non-empty string of at most {limit} characters",
            }
        )
        return None
    return value.strip()


def validate_cohort_pins(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and refuse the five pin families of AC1.

    Every refusal names its field. The half-pin refusals are the load-bearing
    ones: a Business Domain id without its version number, or a Semantic View id
    without its version id, is not a pin -- it is a hope that the reader will
    pick the right one later.
    """
    reasons: list[dict[str, Any]] = []

    window_start = _as_datetime(payload.get("window_start"), "window_start", reasons)
    window_end = _as_datetime(payload.get("window_end"), "window_end", reasons)
    if window_start is not None and window_end is not None and window_end <= window_start:
        reasons.append(
            {
                "code": "invalid_window",
                "field": "window_end",
                "message": "window_end must be strictly after window_start",
            }
        )

    label = _bounded(payload.get("label"), "label", reasons)

    surface = payload.get("surface")
    if surface is not None and surface not in SURFACES:
        reasons.append(
            {
                "code": "invalid_value",
                "field": "surface",
                "message": f"surface must be one of {list(SURFACES)}",
            }
        )
        surface = None
    actor_class = _bounded(payload.get("actor_class"), "actor_class", reasons)

    domain_id = _bounded(payload.get("business_domain_id"), "business_domain_id", reasons)
    domain_version = payload.get("business_domain_version")
    if domain_version is not None:
        malformed = (
            not isinstance(domain_version, int)
            or isinstance(domain_version, bool)
            or domain_version < 1
        )
        if malformed:
            reasons.append(
                {
                    "code": "invalid_value",
                    "field": "business_domain_version",
                    "message": "business_domain_version must be an integer >= 1",
                }
            )
            domain_version = None
    if (domain_id is None) != (domain_version is None):
        reasons.append(
            {
                "code": "half_pin",
                "field": "business_domain_version",
                "message": (
                    "a Business Domain is pinned by id AND version number, or not at all: "
                    "an id without its version number is not a pin"
                ),
            }
        )

    view_id = _bounded(payload.get("semantic_view_id"), "semantic_view_id", reasons)
    view_version_id = _bounded(
        payload.get("semantic_view_version_id"), "semantic_view_version_id", reasons
    )
    if (view_id is None) != (view_version_id is None):
        reasons.append(
            {
                "code": "half_pin",
                "field": "semantic_view_version_id",
                "message": (
                    "a Semantic View is pinned by id AND exact version id, or not at all"
                ),
            }
        )

    capability = _bounded(payload.get("capability"), "capability", reasons)
    result_type = payload.get("result_type")
    if result_type is not None and result_type not in RESULT_TYPES:
        reasons.append(
            {
                "code": "invalid_value",
                "field": "result_type",
                "message": f"result_type must be one of {list(RESULT_TYPES)}",
            }
        )
        result_type = None

    model_ref = _bounded(payload.get("model_ref"), "model_ref", reasons)
    tool_catalog_version = _bounded(
        payload.get("tool_catalog_version"), "tool_catalog_version", reasons
    )
    context_hash = _bounded(
        payload.get("context_version_set_hash"), "context_version_set_hash", reasons, limit=64
    )
    if context_hash is not None and (
        len(context_hash) != 64 or any(c not in "0123456789abcdef" for c in context_hash.lower())
    ):
        reasons.append(
            {
                "code": "invalid_value",
                "field": "context_version_set_hash",
                "message": "context_version_set_hash must be 64 lowercase hexadecimal characters",
            }
        )
        context_hash = None

    # `latest` is not a version. This is the refusal `analyze-and-test.md:230`
    # names, applied to every value that identifies an environment.
    for field, value in (
        ("semantic_view_version_id", view_version_id),
        ("model_ref", model_ref),
        ("tool_catalog_version", tool_catalog_version),
    ):
        if value is not None and not _is_exact_version_pin(value):
            reasons.append(
                {
                    "code": "unqualified_version",
                    "field": field,
                    "message": (
                        f"{field} must be an exact stored version identity; "
                        f"{list(sorted(FORBIDDEN_VERSION_VALUES))} are refused"
                    ),
                }
            )

    host_profile = payload.get("host_profile")
    if host_profile is None:
        host_profile = {}
    if not isinstance(host_profile, Mapping):
        reasons.append(
            {
                "code": "invalid_value",
                "field": "host_profile",
                "message": "host_profile must be an object",
            }
        )
        host_profile = {}

    if reasons:
        raise TraceObservationRefused(reasons)

    return {
        "label": label,
        "window_start": window_start,
        "window_end": window_end,
        "surface": surface,
        "actor_class": actor_class,
        "business_domain_id": domain_id,
        "business_domain_version": domain_version,
        "semantic_view_id": view_id,
        "semantic_view_version_id": view_version_id,
        "capability": capability,
        "result_type": result_type,
        "model_ref": model_ref,
        "host_profile": dict(host_profile),
        "tool_catalog_version": tool_catalog_version,
        "context_version_set_hash": context_hash,
    }


#: Which declared pins the observed record can actually be matched on today, and
#: which cannot. The second list is disclosed, never hidden: an unenforced filter
#: that looks enforced is how a cohort acquires a denominator nobody can defend.
_ENFORCEABLE_PINS = (
    "window_start",
    "window_end",
    "model_ref",
    "tool_catalog_version",
    "semantic_view_version_id",
)
_UNENFORCEABLE_PINS = {
    "surface": "app.ai_paths records no surface column (migration 150)",
    "actor_class": "app.ai_paths records an actor identity, not an actor class",
    "business_domain_id": "an observed AI Path carries no Business Domain reference",
    "business_domain_version": "an observed AI Path carries no Business Domain reference",
    "capability": "an observed AI Path carries no capability reference",
    "result_type": "app.query_results records an outcome, not a declared result type",
    "context_version_set_hash": (
        "app.ai_paths pins a policy snapshot hash, which is not a Context Version Set"
    ),
}


def describe_pin_enforcement(pins: Mapping[str, Any]) -> list[dict[str, Any]]:
    """State, pin by pin, whether it narrowed membership or only records intent."""
    described: list[dict[str, Any]] = []
    for field in (
        "window_start",
        "window_end",
        "surface",
        "actor_class",
        "business_domain_id",
        "business_domain_version",
        "semantic_view_id",
        "semantic_view_version_id",
        "capability",
        "result_type",
        "model_ref",
        "tool_catalog_version",
        "context_version_set_hash",
    ):
        value = pins.get(field)
        if value is None:
            continue
        if field in _ENFORCEABLE_PINS or field == "semantic_view_id":
            described.append({"pin": field, "state": "applied"})
        else:
            described.append(
                {
                    "pin": field,
                    "state": "declared_not_enforced",
                    "reason": _UNENFORCEABLE_PINS[field],
                }
            )
    return described


def _version_filters(pins: Mapping[str, Any]) -> dict[str, Any]:
    """The exact filters echoed back on every aggregate entry (AC7)."""
    return {
        "window_start": _isoformat(pins.get("window_start")),
        "window_end": _isoformat(pins.get("window_end")),
        "surface": pins.get("surface"),
        "actor_class": pins.get("actor_class"),
        "business_domain_id": pins.get("business_domain_id"),
        "business_domain_version": pins.get("business_domain_version"),
        "semantic_view_id": pins.get("semantic_view_id"),
        "semantic_view_version_id": pins.get("semantic_view_version_id"),
        "capability": pins.get("capability"),
        "result_type": pins.get("result_type"),
        "model_ref": pins.get("model_ref"),
        "tool_catalog_version": pins.get("tool_catalog_version"),
        "context_version_set_hash": pins.get("context_version_set_hash"),
        "pin_enforcement": describe_pin_enforcement(pins),
    }


def _isoformat(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Per-member evidence classification (AC8). Pure, and delegating the verdict to
# `core.ai_paths.assess` rather than re-deriving one.
# ---------------------------------------------------------------------------


def classify_path_evidence(
    steps: Sequence[Mapping[str, Any]], *, outcome: str | None
) -> str:
    """`observed` or `unverifiable` -- the stored member column, not a verdict.

    A step-less path and an `unavailable` outcome both count in the denominator
    and never in the numerator. Dropping them would inflate coverage, which is
    the same defect as a percentage without a denominator wearing a better
    number.
    """
    if outcome == OUTCOME_UNAVAILABLE:
        return PATH_EVIDENCE_UNVERIFIABLE
    if not steps:
        return PATH_EVIDENCE_UNVERIFIABLE
    return PATH_EVIDENCE_OBSERVED


def assess_member(member: Mapping[str, Any]) -> dict[str, Any]:
    """Derive, at read time, what the pinned policy says about this observation.

    Derived and never stored: migration 150 states the reason in its own header
    -- a stored verdict makes a historical path re-judge itself under today's
    policy.
    """
    if member.get("path_evidence_state") == PATH_EVIDENCE_UNVERIFIABLE:
        return {
            "verdict": VERDICT_UNVERIFIABLE,
            "findings": [
                {
                    "finding": FINDING_UNVERIFIABLE,
                    "detail": "no usable server-owned path evidence for this execution",
                }
            ],
        }
    return assess(
        member.get("policy_snapshot") or {},
        member.get("steps") or [],
        outcome=member.get("path_outcome"),
    )


def observed_dimensions(member: Mapping[str, Any]) -> dict[str, Any]:
    """The per-dimension outcome of one observation. Non-blocking, always.

    `path_quality` is the only dimension an observed record can speak to today.
    The rendered artifact and MCP App dimensions have no owner, so they report
    `Unverifiable` with the story that will lift them -- never `pass` over zero
    observations, never `fail` over missing evidence.
    """
    assessment = assess_member(member)
    return {
        "evidence_mode": "observed_cohort",
        "blocking": False,
        "label": OBSERVED_COHORT_LABEL,
        "path_quality": {
            "verdict": assessment["verdict"],
            "findings": assessment["findings"],
        },
        "render_behavior": dict(RENDER_ABSENCE),
        "mcp_app_behavior": dict(MCP_APP_ABSENCE),
    }


# ---------------------------------------------------------------------------
# Proposals (AC4). A proposal SUGGESTS. It never creates, edits or versions a
# Golden Question -- Story 51.1 owns that object and its authoring path.
# ---------------------------------------------------------------------------

_FINDING_TO_REASON = {
    FINDING_REQUIRED_MISSING: ("required_node_missing", "high"),
    FINDING_FORBIDDEN_OBSERVED: ("forbidden_node_used", "critical"),
    FINDING_OUT_OF_ORDER: ("out_of_order", "medium"),
    FINDING_VERSION_MISMATCH: ("version_mismatch", "high"),
}

_RESULT_OUTCOME_TO_REASON = {
    "refused": ("result_refused", "medium"),
    "degraded": ("result_degraded", "low"),
    "unavailable": ("result_unavailable", "high"),
}


def derive_member_proposals(member: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Machine reason codes for one observed member, deduplicated and ordered.

    The vocabulary is exactly ``ck_golden_question_proposals_reason``. A code
    this function could emit but the CHECK refuses would be a write that fails at
    the last possible moment, on a row nobody can inspect.
    """
    proposals: dict[str, str] = {}

    if member.get("path_evidence_state") == PATH_EVIDENCE_UNVERIFIABLE:
        proposals.setdefault("path_unverifiable", "medium")
    else:
        for finding in assess_member(member).get("findings") or []:
            mapped = _FINDING_TO_REASON.get(finding.get("finding"))
            if mapped is not None:
                proposals.setdefault(mapped[0], mapped[1])

    mapped_result = _RESULT_OUTCOME_TO_REASON.get(member.get("result_outcome"))
    if mapped_result is not None:
        proposals.setdefault(mapped_result[0], mapped_result[1])

    return [
        {
            "member_id": member.get("id"),
            "reason_code": reason_code,
            "severity_hint": severity,
        }
        for reason_code, severity in sorted(proposals.items())
        if reason_code in PROPOSAL_REASON_CODES
    ]


# ---------------------------------------------------------------------------
# Aggregates (AC7). Computed at read time, never stored: a stored aggregate is a
# stored verdict wearing a numeric costume.
# ---------------------------------------------------------------------------

#: The four fields every aggregate entry carries. Named once so the guard and the
#: test cannot disagree about what "complete" means.
AGGREGATE_REQUIRED_FIELDS = ("numerator", "denominator", "coverage", "version_filters")

#: The three classification axes `analyze-and-test.md:233-239` makes mandatory
#: and no observed record can supply today. Returned as a stated absence rather
#: than omitted, so a screen cannot mistake "not aggregated" for "no findings".
_UNAVAILABLE_AXES = {
    "business_domain": "an observed AI Path carries no Business Domain reference",
    "capability": "an observed AI Path carries no capability reference",
    "result_type": "app.query_results records an outcome, not a declared result type",
}


def assert_aggregate_is_complete(entry: Mapping[str, Any]) -> None:
    """Refuse an aggregate entry that lost its denominator, coverage or filters.

    `analyze-and-test.md:310-313` requires all four in the same payload. This is a
    guard rather than a convention because the failure mode is a number that
    still renders -- a bare percentage looks finished.
    """
    missing = [field for field in AGGREGATE_REQUIRED_FIELDS if field not in entry]
    if missing:
        raise TraceObservationError(
            "an aggregate entry must carry "
            f"{list(AGGREGATE_REQUIRED_FIELDS)}; missing {missing}"
        )
    coverage = entry.get("coverage")
    if not isinstance(coverage, Mapping) or "denominator" not in coverage:
        raise TraceObservationError("coverage must state its own denominator")


def _entry(
    key: Any,
    *,
    numerator: int,
    denominator: int,
    coverage: Mapping[str, Any],
    version_filters: Mapping[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    entry = {
        "key": key,
        "numerator": numerator,
        "denominator": denominator,
        "coverage": dict(coverage),
        "version_filters": dict(version_filters),
        **extra,
    }
    assert_aggregate_is_complete(entry)
    return entry


def build_aggregates(
    cohort: Mapping[str, Any], members: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Every aggregate over a frozen cohort, each carrying its own denominator.

    The denominator is the cohort's STORED ``member_count``, not ``len(members)``:
    if an erasure removed a member row, the historical percentage must get worse,
    not silently better.
    """
    filters = _version_filters(cohort)
    denominator = int(cohort.get("member_count") or 0)

    observed = sum(
        1 for m in members if m.get("path_evidence_state") == PATH_EVIDENCE_OBSERVED
    )
    unverifiable = sum(
        1 for m in members if m.get("path_evidence_state") == PATH_EVIDENCE_UNVERIFIABLE
    )
    coverage = {
        "denominator": denominator,
        "members_with_path_evidence": observed,
        "members_unverifiable": unverifiable,
    }

    verdict_counts: dict[str, int] = {}
    node_hits: dict[tuple[str, ...], int] = {}
    node_eligible: dict[tuple[str, ...], int] = {}
    skill_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    view_counts: dict[str, int] = {}
    result_outcomes: dict[str, int] = {}

    for member in members:
        verdict = assess_member(member)["verdict"]
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

        # The node address is the column's word on both sides (AI-376, Opus N2): the
        # cohort said 0/1 for a node `ai_paths.assess` called pass on the same walk.
        from core.candidate_emission import owner_object_type_for  # noqa: PLC0415

        for entry in (member.get("policy_snapshot") or {}).get("required") or []:
            key = (
                str(entry.get("owner_workspace")),
                str(owner_object_type_for(str(entry.get("owner_object_type")))),
                str(entry.get("owner_object_id")),
            )
            node_eligible[key] = node_eligible.get(key, 0) + 1

        for step in member.get("steps") or []:
            key = (
                str(step.get("owner_workspace")),
                str(owner_object_type_for(str(step.get("owner_object_type")))),
                str(step.get("owner_object_id")),
            )
            if key in node_eligible:
                node_hits[key] = node_hits.get(key, 0) + 1
            if step.get("skill_version_id"):
                skill = str(step["skill_version_id"])
                skill_counts[skill] = skill_counts.get(skill, 0) + 1
            if step.get("tool_name"):
                tool = str(step["tool_name"])
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            if step.get("step_kind") == "semantic_query" and step.get("owner_version_id"):
                view = str(step["owner_version_id"])
                view_counts[view] = view_counts.get(view, 0) + 1

        if member.get("result_outcome"):
            outcome = str(member["result_outcome"])
            result_outcomes[outcome] = result_outcomes.get(outcome, 0) + 1

    def _series(counts: Mapping[str, int]) -> list[dict[str, Any]]:
        return [
            _entry(
                key,
                numerator=value,
                denominator=denominator,
                coverage=coverage,
                version_filters=filters,
            )
            for key, value in sorted(counts.items())
        ]

    return {
        "cohort_id": cohort.get("id"),
        "label": OBSERVED_COHORT_LABEL,
        "evidence_mode": "observed_cohort",
        "blocking": False,
        "denominator": denominator,
        "coverage": coverage,
        "version_filters": filters,
        "by_required_node": [
            _entry(
                list(key),
                numerator=node_hits.get(key, 0),
                # A required node's denominator is how many members PINNED it,
                # not the whole cohort: dividing by the cohort would make a node
                # required of two members look like a 2% adherence rate.
                denominator=eligible,
                coverage=coverage,
                version_filters=filters,
            )
            for key, eligible in sorted(node_eligible.items())
        ],
        "by_skill": _series(skill_counts),
        "by_tool": _series(tool_counts),
        "by_semantic_view_version": _series(view_counts),
        "by_result_outcome": _series(result_outcomes),
        "by_path_verdict": _series(verdict_counts),
        # Stated absences, never omitted keys.
        "unavailable_axes": [
            {"axis": axis, "state": VERDICT_UNVERIFIABLE, "reason": reason}
            for axis, reason in sorted(_UNAVAILABLE_AXES.items())
        ],
    }


# ---------------------------------------------------------------------------
# Persistence. Cohorts and members are insert-once; nothing below issues an
# UPDATE or a DELETE against either table.
# ---------------------------------------------------------------------------

def _assert_project_scope(conn, *, org_id: str, project_id: str) -> None:
    """The Project must belong to this org. A bare id from the client is never trusted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.projects WHERE id = %s AND org_id = %s",
            (_required(project_id, "project_id"), _required(org_id, "org_id")),
        )
        if cur.fetchone() is None:
            raise TraceObservationNotFound("Not found")


def _select_candidate_paths(
    conn, *, org_id: str, project_id: str, pins: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The enforceable half of the selection, in one Project-scoped query."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.outcome, p.started_at, p.policy_snapshot,
                   r.id, r.outcome
              FROM app.ai_paths p
              LEFT JOIN LATERAL (
                   SELECT qr.id, qr.outcome
                     FROM app.query_results qr
                    WHERE qr.ai_path_id = p.id
                      AND qr.org_id = p.org_id
                      AND qr.project_id = p.project_id
                    ORDER BY qr.created_at, qr.id
                    LIMIT 1
              ) r ON TRUE
             WHERE p.org_id = %s
               AND p.project_id = %s
               AND p.lifecycle = %s
               AND p.started_at >= %s
               AND p.started_at < %s
               AND (%s::text IS NULL OR p.model_ref = %s::text)
               AND (%s::text IS NULL OR p.tool_catalog_version = %s::text)
               AND (%s::text IS NULL OR EXISTS (
                        SELECT 1
                          FROM app.query_results qr2
                          JOIN app.query_spec_versions qsv
                            ON qsv.id = qr2.query_spec_version_id
                           AND qsv.project_id = qr2.project_id
                         WHERE qr2.ai_path_id = p.id
                           AND qr2.project_id = p.project_id
                           AND qsv.semantic_view_version_id = %s::text
                   ))
             ORDER BY p.started_at, p.id
            """,
            (
                org_id,
                project_id,
                _ADMISSIBLE_LIFECYCLE,
                pins["window_start"],
                pins["window_end"],
                pins.get("model_ref"),
                pins.get("model_ref"),
                pins.get("tool_catalog_version"),
                pins.get("tool_catalog_version"),
                pins.get("semantic_view_version_id"),
                pins.get("semantic_view_version_id"),
            ),
        )
        rows = cur.fetchall()

    return [
        {
            "ai_path_id": row[0],
            "path_outcome": row[1],
            "observed_at": row[2],
            "policy_snapshot": row[3] or {},
            "query_result_id": row[4],
            "result_outcome": row[5],
        }
        for row in rows
    ]


def _load_steps_for(conn, *, project_id: str, path_ids: Sequence[str]) -> dict[str, list[dict]]:
    if not path_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT path_id, ordinal, step_kind, owner_workspace, owner_object_type,
                   owner_object_id, owner_version_id, skill_version_id, skill_step_id,
                   tool_name, outcome, observed_at
              FROM app.ai_path_steps
             WHERE project_id = %s AND path_id = ANY(%s)
             ORDER BY path_id, ordinal
            """,
            (project_id, list(path_ids)),
        )
        rows = cur.fetchall()
    names = (
        "ordinal",
        "step_kind",
        "owner_workspace",
        "owner_object_type",
        "owner_object_id",
        "owner_version_id",
        "skill_version_id",
        "skill_step_id",
        "tool_name",
        "outcome",
        "observed_at",
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(dict(zip(names, row[1:])))
    return grouped


def resolve_observed_cohort(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze one selection. Resolution happens ONCE and mints a new identity.

    A later execution matching the same filters does not join this cohort; the
    caller resolves again and gets a new cohort with its own filter hash and its
    own denominator. That is the difference between evidence and a saved filter.
    """
    pins = validate_cohort_pins(payload)
    _assert_project_scope(conn, org_id=org_id, project_id=project_id)

    candidates = _select_candidate_paths(conn, org_id=org_id, project_id=project_id, pins=pins)
    steps_by_path = _load_steps_for(
        conn, project_id=project_id, path_ids=[c["ai_path_id"] for c in candidates]
    )

    members: list[dict[str, Any]] = []
    for candidate in candidates:
        steps = steps_by_path.get(candidate["ai_path_id"], [])
        members.append(
            {
                **candidate,
                "id": _mint("ocm"),
                "steps": steps,
                "path_evidence_state": classify_path_evidence(
                    steps, outcome=candidate["path_outcome"]
                ),
                # Declared and left unpopulated. The rendered-artifact owner does
                # not exist, so `unverifiable` is the only reachable state and
                # `ck_observed_cohort_members_render_unpinned` holds it there.
                "render_ref": None,
                "render_evidence_state": RENDER_EVIDENCE_UNVERIFIABLE,
            }
        )

    filter_hash = content_hash({"pins": _version_filters(pins)})
    resolved_at = datetime.now(timezone.utc)
    cohort_id = _mint("ocoh")
    digest = content_hash(
        {
            "cohort_id": cohort_id,
            "filter_hash": filter_hash,
            "resolved_at": resolved_at.isoformat(),
            "member_ai_path_ids": sorted(m["ai_path_id"] for m in members),
        }
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.observed_cohorts
                (id, org_id, project_id, label, window_start, window_end, surface,
                 actor_class, business_domain_id, business_domain_version,
                 semantic_view_id, semantic_view_version_id, capability, result_type,
                 model_ref, host_profile, tool_catalog_version, context_version_set_hash,
                 resolved_at, member_count, filter_hash, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                cohort_id,
                org_id,
                project_id,
                pins["label"],
                pins["window_start"],
                pins["window_end"],
                pins["surface"],
                pins["actor_class"],
                pins["business_domain_id"],
                pins["business_domain_version"],
                pins["semantic_view_id"],
                pins["semantic_view_version_id"],
                pins["capability"],
                pins["result_type"],
                pins["model_ref"],
                canonical_json(pins["host_profile"]),
                pins["tool_catalog_version"],
                pins["context_version_set_hash"],
                resolved_at,
                len(members),
                filter_hash,
                digest,
                _required(actor, "actor"),
            ),
        )
        for member in members:
            cur.execute(
                """
                INSERT INTO app.observed_cohort_members
                    (id, cohort_id, org_id, project_id, ai_path_id, query_result_id,
                     render_ref, render_evidence_state, path_evidence_state, observed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    member["id"],
                    cohort_id,
                    org_id,
                    project_id,
                    member["ai_path_id"],
                    member["query_result_id"],
                    member["render_ref"],
                    member["render_evidence_state"],
                    member["path_evidence_state"],
                    member["observed_at"],
                ),
            )

    cohort = {
        **pins,
        "id": cohort_id,
        "org_id": org_id,
        "project_id": project_id,
        "resolved_at": resolved_at,
        "member_count": len(members),
        "filter_hash": filter_hash,
        "content_hash": digest,
        "created_by": actor,
    }
    return _cohort_envelope(cohort, members)


def _cohort_envelope(
    cohort: Mapping[str, Any], members: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "id": cohort["id"],
        "label": cohort.get("label"),
        "evidence_mode": "observed_cohort",
        # AC3, in the exact words the contract requires. No approval, no gate, no
        # blocking verdict exists on this object -- from the schema up.
        "evidence_label": OBSERVED_COHORT_LABEL,
        "blocking": False,
        "resolved_at": _isoformat(cohort.get("resolved_at")),
        "member_count": int(cohort.get("member_count") or 0),
        "filter_hash": cohort.get("filter_hash"),
        "content_hash": cohort.get("content_hash"),
        "created_by": cohort.get("created_by"),
        "version_filters": _version_filters(cohort),
        "members": [
            {
                "id": member["id"],
                "ai_path_id": member["ai_path_id"],
                "query_result_id": member.get("query_result_id"),
                "path_evidence_state": member["path_evidence_state"],
                "render_evidence_state": member.get(
                    "render_evidence_state", RENDER_EVIDENCE_UNVERIFIABLE
                ),
                "render": dict(RENDER_ABSENCE),
                "observed_at": _isoformat(member.get("observed_at")),
                "assessment": assess_member(member),
            }
            for member in members
        ],
    }


_COHORT_COLUMNS = (
    "id",
    "label",
    "window_start",
    "window_end",
    "surface",
    "actor_class",
    "business_domain_id",
    "business_domain_version",
    "semantic_view_id",
    "semantic_view_version_id",
    "capability",
    "result_type",
    "model_ref",
    "host_profile",
    "tool_catalog_version",
    "context_version_set_hash",
    "resolved_at",
    "member_count",
    "filter_hash",
    "content_hash",
    "created_by",
)


def _load_cohort_row(conn, *, org_id: str, project_id: str, cohort_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_COHORT_COLUMNS)}
              FROM app.observed_cohorts
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (
                _required(cohort_id, "cohort_id"),
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
            ),
        )
        row = cur.fetchone()
    if row is None:
        # Foreign, denied and absent are one answer. Distinguishing them would
        # confirm that another Project's cohort exists.
        raise TraceObservationNotFound("Not found")
    return dict(zip(_COHORT_COLUMNS, row))


def _load_members(conn, *, org_id: str, project_id: str, cohort_id: str) -> list[dict[str, Any]]:
    """Members plus everything read-time assessment needs, without an N+1."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.ai_path_id, m.query_result_id, m.render_ref,
                   m.render_evidence_state, m.path_evidence_state, m.observed_at,
                   p.policy_snapshot, p.outcome, r.outcome
              FROM app.observed_cohort_members m
              JOIN app.ai_paths p
                ON p.id = m.ai_path_id AND p.project_id = m.project_id
              LEFT JOIN app.query_results r
                ON r.id = m.query_result_id AND r.project_id = m.project_id
             WHERE m.cohort_id = %s AND m.org_id = %s AND m.project_id = %s
             ORDER BY m.observed_at, m.id
            """,
            (cohort_id, org_id, project_id),
        )
        rows = cur.fetchall()

    members = [
        {
            "id": row[0],
            "ai_path_id": row[1],
            "query_result_id": row[2],
            "render_ref": row[3],
            "render_evidence_state": row[4],
            "path_evidence_state": row[5],
            "observed_at": row[6],
            "policy_snapshot": row[7] or {},
            "path_outcome": row[8],
            "result_outcome": row[9],
        }
        for row in rows
    ]
    steps_by_path = _load_steps_for(
        conn, project_id=project_id, path_ids=[m["ai_path_id"] for m in members]
    )
    for member in members:
        member["steps"] = steps_by_path.get(member["ai_path_id"], [])
    return members


def load_observed_cohort(
    conn, *, org_id: str, project_id: str, cohort_id: str
) -> dict[str, Any]:
    """The frozen selection exactly as it was resolved."""
    cohort = _load_cohort_row(conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id)
    members = _load_members(conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id)
    return _cohort_envelope(cohort, members)


def list_observed_cohorts(
    conn, *, org_id: str, project_id: str, limit: int = _DEFAULT_PAGE, cursor: str | None = None
) -> list[dict[str, Any]]:
    """Bounded, Project-scoped, keyset-ordered. No unbounded collection read."""
    bounded = max(1, min(int(limit), _MAX_PAGE))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, label, window_start, window_end, resolved_at, member_count,
                   filter_hash
              FROM app.observed_cohorts
             WHERE org_id = %s AND project_id = %s
               AND (%s::text IS NULL OR id < %s::text)
             ORDER BY id DESC
             LIMIT %s
            """,
            (
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
                cursor,
                cursor,
                bounded,
            ),
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "label": row[1],
            "window_start": _isoformat(row[2]),
            "window_end": _isoformat(row[3]),
            "resolved_at": _isoformat(row[4]),
            "member_count": row[5],
            "filter_hash": row[6],
            "evidence_mode": "observed_cohort",
            "evidence_label": OBSERVED_COHORT_LABEL,
            "blocking": False,
        }
        for row in rows
    ]


def cohort_aggregates(conn, *, org_id: str, project_id: str, cohort_id: str) -> dict[str, Any]:
    cohort = _load_cohort_row(conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id)
    members = _load_members(conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id)
    return build_aggregates(cohort, members)


def emit_golden_question_proposals(
    conn, *, org_id: str, project_id: str, cohort_id: str
) -> list[dict[str, Any]]:
    """Record what the frozen membership suggests. It authors nothing.

    ``ON CONFLICT (member_id, reason_code) DO NOTHING`` makes emission idempotent:
    reading a cohort twice must not multiply its proposals, and a proposal that
    was already decided must not be resurrected as `proposed`.
    """
    _load_cohort_row(conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id)
    members = _load_members(conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id)

    with conn.cursor() as cur:
        for member in members:
            for proposal in derive_member_proposals(member):
                cur.execute(
                    """
                    INSERT INTO app.golden_question_proposals
                        (id, cohort_id, member_id, org_id, project_id, reason_code,
                         severity_hint, state)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'proposed')
                    ON CONFLICT (member_id, reason_code) DO NOTHING
                    """,
                    (
                        _mint("gqp"),
                        cohort_id,
                        proposal["member_id"],
                        org_id,
                        project_id,
                        proposal["reason_code"],
                        proposal["severity_hint"],
                    ),
                )
    return list_golden_question_proposals(
        conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id
    )


def list_golden_question_proposals(
    conn, *, org_id: str, project_id: str, cohort_id: str
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, member_id, reason_code, severity_hint, state, golden_question_id,
                   decided_by, decided_at, created_at
              FROM app.golden_question_proposals
             WHERE cohort_id = %s AND org_id = %s AND project_id = %s
             ORDER BY created_at, id
            """,
            (cohort_id, org_id, project_id),
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "member_id": row[1],
            "reason_code": row[2],
            "severity_hint": row[3],
            "state": row[4],
            # Populated only when someone authored the question through Story
            # 51.1's own path. This module never writes a Golden Question table.
            "golden_question_id": row[5],
            "decided_by": row[6],
            "decided_at": _isoformat(row[7]),
            "created_at": _isoformat(row[8]),
        }
        for row in rows
    ]


def decide_golden_question_proposal(
    conn,
    *,
    org_id: str,
    project_id: str,
    cohort_id: str,
    proposal_id: str,
    state: str,
    actor: str,
    golden_question_id: str | None = None,
) -> dict[str, Any]:
    """Record a human decision on a proposal. It still authors nothing.

    ``accepted`` requires a Golden Question that ALREADY exists -- the composite
    foreign key of migration 153 refuses an id this Project does not own, and
    this module has no code path that creates one.
    """
    if state not in ("accepted", "declined"):
        raise TraceObservationRefused(
            [
                {
                    "code": "invalid_state",
                    "field": "state",
                    "message": "the only legal decisions are accepted and declined",
                }
            ]
        )
    if state == "accepted" and not golden_question_id:
        raise TraceObservationRefused(
            [
                {
                    "code": "missing_field",
                    "field": "golden_question_id",
                    "message": (
                        "accepting a proposal names the Golden Question that Story 51.1 "
                        "authored; this surface never creates one"
                    ),
                }
            ]
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.golden_question_proposals
               SET state = %s,
                   golden_question_id = %s,
                   decided_by = %s,
                   decided_at = NOW()
             WHERE id = %s AND cohort_id = %s AND org_id = %s AND project_id = %s
               AND state = 'proposed'
            RETURNING id, state, golden_question_id, decided_by
            """,
            (
                state,
                golden_question_id if state == "accepted" else None,
                _required(actor, "actor"),
                _required(proposal_id, "proposal_id"),
                _required(cohort_id, "cohort_id"),
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise TraceObservationNotFound("Not found")
    return {
        "id": row[0],
        "state": row[1],
        "golden_question_id": row[2],
        "decided_by": row[3],
    }


# ---------------------------------------------------------------------------
# The Trace Observation read model (AC5, AC6, AC8, AC12).
# ---------------------------------------------------------------------------


def timeline_lens(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Ordered by the STORED ordinal, never re-derived from timestamps.

    Two steps can share a timestamp; sorting by time would then present an order
    that never happened.
    """
    return {
        "ordering": "stored ordinal",
        "steps": [
            {
                "ordinal": step.get("ordinal"),
                "observed_at": _isoformat(step.get("observed_at")),
                "step_kind": step.get("step_kind"),
                "outcome": step.get("outcome"),
            }
            for step in steps
        ],
    }


def context_skills_lens(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Governed owners, and the Skill pin shown as a PAIR or not at all.

    Half a Skill pin resolves to the wrong step the first time a Skill is
    revised, which is why ``ck_ai_path_steps_skill_pin`` refuses to store one and
    why this lens refuses to display one.

    Story 45.7 -- AND THE PAIR SAYS WHAT THE STEP WAS. Measured on 2026-08-05:
    this lens returned the two opaque identifiers and nothing else, at either
    version, so the reader of a trace could not tell which step of which Skill
    ran. ``core.ai_paths.load_path`` resolves the pin against the version that
    was SERVED and puts the answer on the step; this lens only shows it. A pin
    whose reading failed carries ``state = "unavailable"`` rather than an empty
    step, which would read as "the Skill declared none".
    """
    entries = []
    for step in steps:
        skill_version = step.get("skill_version_id")
        skill_step = step.get("skill_step_id")
        resolved = step.get("skill") if skill_version and skill_step else None
        entries.append(
            {
                "ordinal": step.get("ordinal"),
                "owner_workspace": step.get("owner_workspace"),
                "owner_object_type": step.get("owner_object_type"),
                "owner_object_id": step.get("owner_object_id"),
                "owner_version_id": step.get("owner_version_id"),
                "skill": (
                    # The resolution when the loader produced one; the bare pair
                    # when it did not -- a reader that never resolved is not a
                    # reader that resolved to nothing.
                    dict(resolved)
                    if resolved
                    else (
                        {
                            "skill_version_id": skill_version,
                            "skill_step_id": skill_step,
                            "state": skill_steps.PIN_UNAVAILABLE,
                        }
                        if skill_version and skill_step
                        else None
                    )
                ),
            }
        )
    return {"steps": entries}


def tools_lens(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        # Stated on the lens itself: `tool_name` is a recorded label. Nothing
        # joins on it, so it must never be read as a governed identity.
        "tool_name_is_a_label": True,
        "steps": [
            {
                "ordinal": step.get("ordinal"),
                "tool_name": step.get("tool_name"),
                "outcome": step.get("outcome"),
            }
            for step in steps
            if step.get("step_kind") == "tool_call"
        ],
    }


def result_render_lens(result: Mapping[str, Any] | None, *, project_id: str) -> dict[str, Any]:
    """The Result half resolves; the rendered half states why it is empty (AC6)."""
    if result is None:
        resolved: dict[str, Any] = {
            "state": "absent",
            "reason": "no_result_is_pinned_to_this_ai_path",
        }
    else:
        resolved = {
            "state": "resolved",
            "id": result.get("id"),
            "outcome": result.get("outcome"),
            "content_hash": result.get("content_hash"),
            "row_count": result.get("row_count"),
            "truncated": result.get("truncated"),
            # Deep-link to Story 50.1's owner. Test annotates versions; it never
            # duplicates the owning workbench's edit surface.
            "owner_href": f"/api/projects/{project_id}/analyze/results/{result.get('id')}",
        }
    return {"result": resolved, "render": dict(RENDER_ABSENCE)}


def linked_feedback_lens() -> dict[str, Any]:
    """The pinned-feedback owner does not exist, and saying so is the deliverable."""
    return {"feedback": dict(LINKED_FEEDBACK_ABSENCE), "records": []}


def compose_lenses(
    *,
    header: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any] | None,
    project_id: str,
) -> dict[str, Any]:
    """The five ratified lenses of `analyze-and-test.md:293`, and only those."""
    return {
        LENS_TIMELINE: timeline_lens(steps),
        LENS_CONTEXT_SKILLS: context_skills_lens(steps),
        LENS_TOOLS: tools_lens(steps),
        LENS_RESULT_RENDER: result_render_lens(result, project_id=project_id),
        LENS_LINKED_FEEDBACK: linked_feedback_lens(),
    }


def load_trace_observation(
    conn, *, org_id: str, project_id: str, ai_path_id: str
) -> dict[str, Any]:
    """One authorized read composing the five lenses. A reader, never a writer.

    ``app.ai_paths``, ``app.ai_path_steps`` and ``app.query_results`` belong to
    Stories 49.6 and 50.1. Project scope is part of the lookup rather than a
    filter applied afterwards, so a foreign id and an absent id are the same
    answer.
    """
    from core.ai_paths import AiPathNotFound, load_path  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.ai_paths WHERE id = %s AND org_id = %s AND project_id = %s",
            (
                _required(ai_path_id, "ai_path_id"),
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
            ),
        )
        if cur.fetchone() is None:
            raise TraceObservationNotFound("Not found")

        cur.execute(
            """
            SELECT id, outcome, content_hash, row_count, truncated
              FROM app.query_results
             WHERE ai_path_id = %s AND org_id = %s AND project_id = %s
             ORDER BY created_at, id
             LIMIT 1
            """,
            (ai_path_id, org_id, project_id),
        )
        result_row = cur.fetchone()

    try:
        path = load_path(conn, path_id=ai_path_id, project_id=project_id)
    except AiPathNotFound as exc:  # pragma: no cover - guarded by the check above
        raise TraceObservationNotFound("Not found") from exc

    result = (
        {
            "id": result_row[0],
            "outcome": result_row[1],
            "content_hash": result_row[2],
            "row_count": result_row[3],
            "truncated": result_row[4],
        }
        if result_row is not None
        else None
    )

    steps = path.get("steps") or []
    member_view = {
        "policy_snapshot": path.get("policy_snapshot") or {},
        "steps": steps,
        "path_outcome": path.get("outcome"),
        "result_outcome": result["outcome"] if result else None,
        "path_evidence_state": classify_path_evidence(steps, outcome=path.get("outcome")),
    }

    return {
        "ai_path_id": path["id"],
        "project_id": project_id,
        "lifecycle": path.get("lifecycle"),
        "outcome": path.get("outcome"),
        "actor": path.get("actor"),
        "started_at": _isoformat(path.get("started_at")),
        "ended_at": _isoformat(path.get("ended_at")),
        "model_ref": path.get("model_ref"),
        "tool_catalog_version": path.get("tool_catalog_version"),
        # Correlation, never identity: migration 150:99-104 says so in the schema.
        "w3c_trace_id": path.get("w3c_trace_id"),
        "w3c_trace_id_is_correlation_not_identity": True,
        "path_evidence_state": member_view["path_evidence_state"],
        "lens_order": list(TRACE_OBSERVATION_LENSES),
        "lenses": compose_lenses(
            header=path, steps=steps, result=result, project_id=project_id
        ),
        "dimensions": observed_dimensions(member_view),
        "proposals_suggested": derive_member_proposals({**member_view, "id": None}),
        "owner_links": {
            "ai_path": "Story 49.6 owns app.ai_paths and app.ai_path_steps",
            "result": "Story 50.1 owns app.query_results",
            "render": RENDER_ABSENCE["owner"],
            "feedback": LINKED_FEEDBACK_ABSENCE["owner"],
            "golden_question": "Story 51.1 owns Golden Question authoring",
        },
    }


def iter_absences() -> Iterable[Mapping[str, Any]]:
    """Every declared-but-unowned pin, in one place, for a read model to render."""
    return (RENDER_ABSENCE, MCP_APP_ABSENCE, LINKED_FEEDBACK_ABSENCE)
