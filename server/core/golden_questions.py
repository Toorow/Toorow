"""Story 51.1 -- the product Golden Question: contract, validation, versions.

WHAT THIS OWNS. The versioned product evaluation specification of
`docs/product-architecture/analyze-and-test.md:241-264`: a stable Project-scoped
head, and immutable versions that each pin a Business Domain version, a Semantic
View version, the business question and its time boundary, a typed expected
Result with an explicit per-assertion tolerance, typed required provenance and an
expected AI Path pattern -- plus the result type, capability tags and severity a
run must be able to say it judged against.

WHAT IT REFUSES TO BE.

  * NOT the Epic 14 benchmark record. Migration 153 renamed that table to
    `app.eval_benchmark_questions` (AI-81 point 1, Jean 2026-07-20). Nothing in
    this module reads it, and nothing here reads `server/tests/evals/corpus.yaml`
    either: that file says of itself that it is TEST CODE. Reading the evaluation
    subject at runtime would make the instrument part of the result.
  * NOT a definition of correctness expressed as SQL. `analyze-and-test.md:263`:
    "one implementation-specific SQL string is never the definition of
    correctness". Correctness is the typed assertion array; a reference execution
    path is an *approach*, and a question may declare none, one or several.
  * NOT an owner of governed meaning. A Golden Question REFERENCES a Business
    Domain version and a Semantic View version and writes to neither
    (`analyze-and-test.md:370`).

WHAT IS ENFORCED WHERE. Immutability, Project scope and the structural shape of
the three documents are enforced by migration 153 -- composite `(id, org_id,
project_id)` foreign keys, `trg_golden_question_versions_immutable`,
`trg_golden_question_versions_structure` and fail-closed RLS. This module is the
sole writer, not the only rampart: it refuses earlier and with a better message,
and the database refuses regardless of who is writing.

`Unverifiable` IS A VERDICT. Render (Stories 50.4/50.5/50.7), the evaluated MCP
App behaviour (Story 50.6), the Evaluation Run (Story 51.2) and observed AI Path
instrumentation (Story 49.6) do not exist. Every one of them is DECLARED here and
reported as `unverifiable` with a machine-readable reason code and the exact
owner story -- never `pass`, never `fail`, and never a placeholder owner that
reads like evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from ulid import ULID

from core import business_identity_catalogue as catalogue
from core import expected_ai_path
from core.ai_paths import OWNER_WORKSPACES, STEP_KINDS
from core.audit import declare_action, insert_audit_row

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Elles passaient par le wrapper local `_audit`, donc
# le releve des appelants directs de `write_audit_row` ne les voyait pas :
# un ecrivain INDIRECT est un ecrivain. Le garde de conformance suit
# maintenant les wrappers qui transmettent `action`.
ACTION_TEST_GOLDEN_QUESTION_CREATED = declare_action("test.golden_question.created")
ACTION_TEST_GOLDEN_QUESTION_LIFECYCLE_CHANGED = declare_action(
    "test.golden_question.lifecycle_changed"
)
ACTION_TEST_GOLDEN_QUESTION_VERSION_CREATED = declare_action("test.golden_question.version_created")


#: Bumped when the canonical serialization changes shape. A stored
#: `content_hash` is only comparable to another produced by the same contract, so
#: the contract version travels inside the hashed document.
GOLDEN_QUESTION_CONTRACT_VERSION = "golden-question.v1"
GOLDEN_QUESTION_V2_CONTRACT_VERSION = "golden-question.v2"

_V2_MAX_ASSERTIONS = 32
_V2_MAX_FIELDS = 16
_V2_MAX_SELECTORS = 8
_V2_MAX_EXPECTED_ROWS = 100
_V2_MAX_CANONICAL_BYTES = 65_536
_RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

#: `analyze-and-test.md:239` -- the declared output contracts. Stored on the
#: VERSION because changing it changes what correctness means.
RESULT_TYPES = (
    "scalar",
    "series",
    "breakdown",
    "comparison",
    "table",
    "narrative",
    "refusal",
)

#: Stored on the version: `analyze-and-test.md:354` makes a critical question
#: non-compensating for a gate, so a run must pin the severity it judged against.
SEVERITIES = ("critical", "major", "minor")

#: Stewardship, on the mutable head. Changing it does not change the meaning of
#: any past verdict, which is exactly why it does not mint a version.
LIFECYCLES = ("draft", "active", "deprecated", "archived")

#: Declared transitions. `archived` is terminal: a question whose history was
#: retired can be superseded by a new question, not resurrected under the same
#: identity, or a past run's subject silently comes back to life.
LIFECYCLE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("active", "archived"),
    "active": ("deprecated", "archived"),
    "deprecated": ("active", "archived"),
    "archived": (),
}

#: `analyze-and-test.md:256` -- typed assertions "over values, rows, ordering,
#: empty/degraded/refusal state or other deterministic invariants".
ASSERTION_TYPES = (
    "value",
    "row_set",
    "ordering",
    "cardinality",
    "invariant",
    "empty",
    "degraded",
    "refused",
)

#: `analyze-and-test.md:257` -- "explicit per-assertion numeric, temporal or set
#: tolerance; exact where no tolerance is declared".
TOLERANCE_KINDS = ("numeric", "temporal", "set")

#: `analyze-and-test.md:258` -- the exact provenance chain. `virtual_pull` is
#: named separately because a source that is queried live still has to prove the
#: same link, and collapsing it into `pull` would hide which one happened.
PROVENANCE_LINK_KINDS = (
    "source",
    "pull",
    "virtual_pull",
    "mapping",
    "publication",
    "semantic_view",
    "citation",
)

#: `analyze-and-test.md:254` -- the view version is exercised as the baseline or
#: as the candidate.
SEMANTIC_VIEW_ROLES = ("baseline", "candidate")

#: A Semantic View version this question may pin. A `draft`, `superseded` or
#: `archived` version is refused: pinning one would make the question evaluate a
#: world nobody can execute against, and silently advancing to the current
#: version would change the meaning of the pin.
PINNABLE_VIEW_STATUSES = ("published", "candidate")

REFERENCE_PATH_ROLES = ("canonical", "alternative")

#: The exact key set an expected-path node may use. Every one of them is a column
#: of `app.ai_path_steps` (migration 150). A node that could name something the
#: observed record cannot express would make its comparison `Unverifiable` by
#: construction -- Story 51.3 would then be comparing against a wish.
_PATH_NODE_KEYS = frozenset(
    {
        "step_kind",
        "owner_workspace",
        "owner_object_type",
        "owner_object_id",
        "owner_version_id",
        "skill_version_id",
        "skill_step_id",
        "tool_name",
    }
)

#: Version pins that name a moving target rather than a stored identity.
#: `analyze-and-test.md:230`: "never an unqualified `latest`". This is the Python
#: half of `app.is_exact_version_pin` (migration 153); both exist because the
#: service is not the only writer a repository ever grows.
_MOVING_PINS = frozenset({"latest", "current", "head"})

#: The verdict a missing pin produces. Not a soft `fail`, not a silent `pass`.
UNVERIFIABLE = "unverifiable"

_CAPABILITY_PATTERN_MAX = 120


class GoldenQuestionNotFound(LookupError):
    """The Golden Question, version or pinned governed object is not in scope.

    Foreign, denied and nonexistent identities must be externally
    indistinguishable, so all three raise THIS. The distinguishing reason belongs
    in audit, not in a response body.
    """


@dataclass(frozen=True)
class Refusal:
    """One named, actionable reason a write was not accepted."""

    code: str
    message: str
    #: The exact field, index or member the refusal is about -- `expected_result[2]`,
    #: not "the document" -- so an author fixes the offending element rather than
    #: re-reading the whole form.
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "subject": self.subject}


class GoldenQuestionRefused(ValueError):
    """A structured refusal carrying EVERY reason, not just the first one.

    Returning one failure at a time makes an author fix a field, resubmit, and
    discover the next -- so validation collects them all before raising.
    """

    def __init__(self, code: str, message: str, refusals: list[Refusal] | None = None):
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
# Declared absences.
#
# Each entry names a referent that does not exist, the reason code a reader can
# match on, and the STORY that will deliver it. There is no `future_owner`
# placeholder here: an owner nobody can name is how an absence starts reading as
# evidence, and it is the defect that had Story 49.6 rejected.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnverifiableDimension:
    """One dimension that cannot be judged, and the exact reason why."""

    dimension: str
    reason_code: str
    owner_story: str
    message: str
    verdict: str = UNVERIFIABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "owner_story": self.owner_story,
            "message": self.message,
        }


DECLARED_ABSENCES: tuple[UnverifiableDimension, ...] = (
    UnverifiableDimension(
        dimension="render",
        reason_code="render_owner_not_delivered",
        owner_story="50.4",
        message=(
            "the rendered artifact does not exist: the Render pin is declared and left "
            "empty, so render fidelity cannot be judged"
        ),
    ),
    UnverifiableDimension(
        dimension="mcp_app_behavior",
        reason_code="mcp_app_evaluation_not_delivered",
        owner_story="50.6",
        message=(
            "evaluated MCP App behaviour does not exist, so widget behaviour cannot be judged"
        ),
    ),
    UnverifiableDimension(
        dimension="run_coverage",
        reason_code="evaluation_run_owner_not_delivered",
        owner_story="51.2",
        message=(
            "no Evaluation Run owner exists, so this question has no recorded run "
            "coverage; the legacy Epic 14 benchmark record is not read as a substitute"
        ),
    ),
    UnverifiableDimension(
        dimension="observed_ai_path_coverage",
        reason_code="observed_path_evidence_absent",
        owner_story="49.6",
        message=(
            "no caller records an observed AI Path yet, so expected-path adherence has "
            "no server-owned evidence to compare against"
        ),
    ),
    # ADDED 2026-08-24 (story 69.5 AC4). The entity spine's end-to-end run was
    # reported in prose -- "no_deployed_environment_in_this_session" -- which is
    # the code of NOTHING: this tuple was closed at four, so an instrument that
    # matches on `reason_code` could not see the gap at all, and a reader of the
    # record saw four absences where there were five. The rule the other four obey
    # is that an absence is only honest when it is MACHINE-READABLE and OWNED;
    # a sentence in a story record is neither.
    UnverifiableDimension(
        dimension="live_deployed_answer",
        reason_code="deployed_environment_absent",
        owner_story="69.5",
        message=(
            "no deployed environment answers here, so the last link -- a model answer "
            "naming its execution id, its mapping version and its rule version -- has "
            "nothing to run against; the disposable local warehouse proves the links "
            "hold together, never that the live journey was walked"
        ),
    ),
)


def declared_absences() -> list[dict[str, Any]]:
    """The five dimensions that report `unverifiable`, with reason and owner."""
    return [absence.as_dict() for absence in DECLARED_ABSENCES]


# ---------------------------------------------------------------------------
# Canonical form and hashing.
#
# The hash is over the NORMALIZED document, not the caller's payload: two edits
# that mean the same thing must hash the same, or "an equivalent edit does not
# mint a version that differs only in key order" cannot hold.
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise GoldenQuestionRefused(
            "invalid_document", "the Golden Question document must be JSON serializable"
        ) from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def is_exact_version_pin(candidate: Any) -> bool:
    """Python half of `app.is_exact_version_pin`: an exact stored identity only."""
    if not isinstance(candidate, str):
        return False
    trimmed = candidate.strip()
    return bool(trimmed) and trimmed.lower() not in _MOVING_PINS


@lru_cache(maxsize=1)
def connector_names() -> frozenset[str]:
    """The installed connector directory names.

    `analyze-and-test.md:237-238`: a capability is "the governed product or tool
    capability exercised, not a connector name". The database enforces the shape
    of a capability key only; naming the live registry is the service's job, and
    the registry is the set of installed connector modules -- not a list copied
    into this file, which would go stale the day a connector is added.
    """
    modules_dir = Path(__file__).resolve().parent.parent / "modules"
    if not modules_dir.is_dir():
        return frozenset()
    names: set[str] = set()
    for entry in modules_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        names.add(entry.name)
        names.add(entry.name.replace("-", "_"))
    return frozenset(names)


@dataclass(frozen=True)
class ValidatedGoldenQuestionVersion:
    """A Golden Question document proven legal against live governed rows."""

    document: dict[str, Any]
    content_hash: str
    business_domain_id: str
    business_domain_version_number: int
    business_classification_id: str | None
    semantic_view_id: str
    semantic_view_version_id: str
    semantic_view_version_role: str
    result_type: str
    capability_tags: list[str]
    severity: str
    reference_paths: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading the two governed pins from the live rows.
# ---------------------------------------------------------------------------


def _check_business_domain_pin(
    conn, *, org_id: str, payload: dict[str, Any], refusals: list[Refusal]
) -> tuple[str, int, str | None]:
    """Resolve `(domain_id, version_number)` inside THIS organization.

    Business Domains are ORG-scoped while Golden Questions are Project-scoped, so
    both halves are proved: the exact (domain, version) pair exists, AND the
    domain belongs to this organization. Checking only the first would let a
    Project pin another tenant's meaning.
    """
    domain_id = payload.get("business_domain_id")
    raw_number = payload.get("business_domain_version_number")
    classification_id = payload.get("business_classification_id") or None

    if not isinstance(domain_id, str) or not domain_id.strip():
        refusals.append(
            Refusal(
                "missing_field",
                "a Golden Question version must pin a Business Domain",
                "business_domain_id",
            )
        )
        domain_id = ""
    else:
        domain_id = domain_id.strip()

    version_number = 0
    if isinstance(raw_number, bool) or not isinstance(raw_number, int):
        refusals.append(
            Refusal(
                "missing_field",
                "the Business Domain pin needs an exact integer version number "
                "(a domain id without its version is not a pin)",
                "business_domain_version_number",
            )
        )
    elif raw_number < 1:
        refusals.append(
            Refusal(
                "invalid_pin",
                "a Business Domain version number starts at 1",
                "business_domain_version_number",
            )
        )
    else:
        version_number = raw_number

    if classification_id is not None and not isinstance(classification_id, str):
        refusals.append(
            Refusal(
                "invalid_pin",
                "the optional classification pin must be an identifier",
                "business_classification_id",
            )
        )
        classification_id = None

    if not domain_id or not version_number:
        return domain_id, version_number, classification_id

    with conn.cursor() as cur:
        # Story 49.2: the pin is checked against the AUTHORITY's version ledger,
        # falling back to the superseded one only where no node holds the id. A
        # Business Domain minted in Master Data has version 1 there and NO row at
        # all in `mdm_business_domain_versions`, so this check used to refuse a
        # pin on an identity the product had just created -- and the refusal said
        # the version "is not a Business Domain version of this organization",
        # about the only one it has.
        cur.execute(
            f"""
            SELECT v.version_number
            FROM {catalogue.DOMAIN_VERSION_SOURCE} v
            JOIN {catalogue.DOMAIN_SOURCE} d
              ON d.id = v.domain_id AND d.org_id = %s
            WHERE v.domain_id = %s AND v.version_number = %s
            """,
            (org_id, domain_id, version_number),
        )
        found = cur.fetchone()
        if found is None:
            # One refusal for "no such version" and "another organization's
            # domain" alike: distinguishing them would turn this endpoint into a
            # cross-tenant existence oracle.
            refusals.append(
                Refusal(
                    "unknown_business_domain_version",
                    f"`{domain_id}` version {version_number} is not a Business Domain "
                    "version of this organization",
                    "business_domain_id",
                )
            )
            return domain_id, version_number, classification_id

        if classification_id:
            cur.execute(
                f"""
                SELECT id FROM {catalogue.CLASSIFICATION_SOURCE} c
                WHERE id = %s AND org_id = %s AND domain_id = %s
                """,
                (classification_id, org_id, domain_id),
            )
            if cur.fetchone() is None:
                refusals.append(
                    Refusal(
                        "unknown_business_classification",
                        f"`{classification_id}` does not narrow `{domain_id}` in this organization",
                        "business_classification_id",
                    )
                )
    return domain_id, version_number, classification_id


def _check_semantic_view_pin(
    conn, *, project_id: str, payload: dict[str, Any], refusals: list[Refusal]
) -> tuple[str, str, str]:
    """Resolve the exact Semantic View version and the role it is exercised as."""
    view_id = payload.get("semantic_view_id")
    version_id = payload.get("semantic_view_version_id")
    role = payload.get("semantic_view_version_role")

    view_id = view_id.strip() if isinstance(view_id, str) else ""
    version_id = version_id.strip() if isinstance(version_id, str) else ""
    role = role.strip().lower() if isinstance(role, str) else ""

    if not view_id or not version_id:
        # Both or neither: half a pin would make the server choose a version, and
        # choosing is exactly what pinning exists to prevent.
        refusals.append(
            Refusal(
                "missing_field",
                "semantic_view_id and semantic_view_version_id are both required",
                "semantic_view_version_id",
            )
        )
    if role not in SEMANTIC_VIEW_ROLES:
        refusals.append(
            Refusal(
                "invalid_role",
                "the Semantic View version is exercised as `baseline` or `candidate`",
                "semantic_view_version_role",
            )
        )
    if version_id and not is_exact_version_pin(version_id):
        refusals.append(
            Refusal(
                "moving_version_pin",
                f"`{version_id}` names a moving target; a Golden Question pins an exact "
                "stored version",
                "semantic_view_version_id",
            )
        )
        return view_id, version_id, role

    if not view_id or not version_id:
        return view_id, version_id, role

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status FROM app.semantic_view_versions
            WHERE id = %s AND view_id = %s AND project_id = %s
            """,
            (version_id, view_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        refusals.append(
            Refusal(
                "unknown_semantic_view_version",
                f"`{version_id}` is not a Semantic View version of this Project",
                "semantic_view_version_id",
            )
        )
        return view_id, version_id, role

    status = str(row[0])
    if status not in PINNABLE_VIEW_STATUSES:
        refusals.append(
            Refusal(
                "version_not_pinnable",
                f"this Semantic View version is {status}; a Golden Question exercises a "
                "published or candidate version",
                "semantic_view_version_id",
            )
        )
    return view_id, version_id, role


# ---------------------------------------------------------------------------
# The three typed documents.
# ---------------------------------------------------------------------------


def _validate_expected_result(payload: Any, refusals: list[Refusal]) -> list[dict[str, Any]]:
    """A non-empty array of typed assertions, each with an EXPLICIT tolerance key.

    A declared `null` tolerance means exact. An ABSENT tolerance key means nobody
    decided, and is refused -- the difference between "we accept no drift" and
    "we never thought about drift" is the whole point of the field.
    """
    if not isinstance(payload, list):
        refusals.append(
            Refusal("invalid_shape", "`expected_result` must be an array", "expected_result")
        )
        return []
    if not payload:
        refusals.append(
            Refusal(
                "empty_expected_result",
                "correctness is defined by at least one typed assertion, never by a "
                "reference query",
                "expected_result",
            )
        )
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        subject = f"expected_result[{index}]"
        if not isinstance(item, dict):
            refusals.append(
                Refusal(
                    "invalid_shape",
                    "an assertion is a typed object, not free text",
                    subject,
                )
            )
            continue
        assertion_type = item.get("assertion_type")
        if not isinstance(assertion_type, str) or assertion_type.strip() not in ASSERTION_TYPES:
            refusals.append(
                Refusal(
                    "unknown_assertion_type",
                    f"`{assertion_type}` is not one of {', '.join(ASSERTION_TYPES)}",
                    subject,
                )
            )
            continue
        if "tolerance" not in item:
            refusals.append(
                Refusal(
                    "missing_tolerance",
                    "every assertion declares a tolerance; declare null for exact",
                    subject,
                )
            )
            continue
        tolerance = item["tolerance"]
        if tolerance is not None:
            if not isinstance(tolerance, dict):
                refusals.append(
                    Refusal(
                        "invalid_tolerance",
                        "a declared tolerance is an object naming its kind, or null for exact",
                        subject,
                    )
                )
                continue
            kind = tolerance.get("kind")
            if not isinstance(kind, str) or kind not in TOLERANCE_KINDS:
                refusals.append(
                    Refusal(
                        "unknown_tolerance_kind",
                        f"`{kind}` is not one of {', '.join(TOLERANCE_KINDS)}",
                        subject,
                    )
                )
                continue
        entry = {k: v for k, v in item.items()}
        entry["assertion_type"] = assertion_type.strip()
        normalized.append(entry)
    return normalized


_V2_ASSERTION_KEYS = {
    "value": {"assertion_type", "selectors", "field", "operator", "expected", "tolerance"},
    "row_set": {
        "assertion_type",
        "selectors",
        "fields",
        "operator",
        "expected_rows",
        "tolerance",
    },
    "ordering": {"assertion_type", "selectors", "fields", "operator", "tolerance"},
    "cardinality": {
        "assertion_type",
        "selectors",
        "operator",
        "expected",
        "tolerance",
    },
    "invariant": {"assertion_type", "selectors", "fields", "operator", "tolerance"},
    "empty": {"assertion_type", "selectors", "operator", "tolerance"},
    "degraded": {"assertion_type", "selectors", "operator", "tolerance"},
    "refused": {"assertion_type", "selectors", "operator", "tolerance"},
}
_V2_ASSERTION_OPERATORS = {
    "value": {"equals"},
    "row_set": {"equals"},
    "ordering": {"ascending", "descending"},
    "cardinality": {"equals"},
    "invariant": {"unique", "non_null"},
    "empty": {"is"},
    "degraded": {"is"},
    "refused": {"is"},
}
_V2_SELECTOR_OPERATORS = {"eq", "ne", "in", "exists"}


def _v2_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _v2_json_value(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_v2_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _v2_json_value(item) for key, item in value.items())
    return False


def _v2_rfc3339_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or _RFC3339_TIMESTAMP.fullmatch(value) is None:
        return False
    text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_v2_selectors(
    payload: Any, subject: str, refusals: list[Refusal]
) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(payload, list):
        refusals.append(Refusal("invalid_shape", "selectors must be an array", subject))
        return [], set()
    if len(payload) > _V2_MAX_SELECTORS:
        refusals.append(
            Refusal("too_many_selectors", "an assertion has at most 8 selectors", subject)
        )
    normalized: list[dict[str, Any]] = []
    fields: set[str] = set()
    for index, selector in enumerate(payload[: _V2_MAX_SELECTORS + 1]):
        item_subject = f"{subject}[{index}]"
        if not isinstance(selector, dict):
            refusals.append(Refusal("invalid_shape", "a selector is an object", item_subject))
            continue
        operator = selector.get("operator")
        allowed = {"field", "operator"} if operator == "exists" else {"field", "operator", "value"}
        unknown = sorted(set(selector) - allowed)
        if unknown:
            refusals.append(
                Refusal(
                    "unknown_selector_field",
                    f"`{unknown[0]}` is not a selector field",
                    item_subject,
                )
            )
            continue
        field_name = selector.get("field")
        if not isinstance(field_name, str) or not field_name.strip():
            refusals.append(Refusal("invalid_field", "a selector names a field", item_subject))
            continue
        field_name = field_name.strip()
        if operator not in _V2_SELECTOR_OPERATORS:
            refusals.append(
                Refusal(
                    "unknown_selector_operator",
                    f"`{operator}` is not a selector operator",
                    item_subject,
                )
            )
            continue
        if operator == "exists" and "value" in selector:
            refusals.append(
                Refusal("invalid_selector", "an exists selector carries no value", item_subject)
            )
            continue
        if operator != "exists" and "value" not in selector:
            refusals.append(
                Refusal("invalid_selector", "this selector requires a value", item_subject)
            )
            continue
        if operator == "in" and not isinstance(selector.get("value"), list):
            refusals.append(
                Refusal("invalid_selector", "an in selector value is an array", item_subject)
            )
            continue
        normalized.append(
            {key: value for key, value in selector.items() if key in allowed}
            | {"field": field_name}
        )
        fields.add(field_name)
    return normalized, fields


def _validate_v2_fields(
    payload: Any, subject: str, refusals: list[Refusal]
) -> tuple[list[str], set[str]]:
    if not isinstance(payload, list) or not payload:
        refusals.append(Refusal("invalid_fields", "fields must be a non-empty array", subject))
        return [], set()
    values: list[str] = []
    for index, value in enumerate(payload):
        if not isinstance(value, str) or not value.strip():
            refusals.append(
                Refusal("invalid_field", "a field name is non-empty", f"{subject}[{index}]")
            )
            continue
        cleaned = value.strip()
        if cleaned not in values:
            values.append(cleaned)
    if len(values) > _V2_MAX_FIELDS:
        refusals.append(Refusal("too_many_fields", "an assertion names at most 16 fields", subject))
    return values, set(values)


def _validate_v2_tolerance(
    payload: Any, assertion_type: str, subject: str, refusals: list[Refusal]
) -> dict[str, Any] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        refusals.append(Refusal("invalid_tolerance", "tolerance is an object or null", subject))
        return None
    kind = payload.get("kind")
    allowed_kind = {
        "value": {"numeric", "temporal"},
        "cardinality": {"numeric"},
        "row_set": {"set"},
    }.get(assertion_type, set())
    if kind not in allowed_kind:
        refusals.append(Refusal("invalid_tolerance", "this tolerance kind does not apply", subject))
        return None
    if kind == "numeric":
        if (
            set(payload) != {"kind", "mode", "amount"}
            or payload.get("mode")
            not in {
                "absolute",
                "relative",
            }
            or not _v2_number(payload.get("amount"))
            or payload["amount"] < 0
        ):
            refusals.append(
                Refusal(
                    "invalid_tolerance",
                    "numeric tolerance requires mode and non-negative amount",
                    subject,
                )
            )
            return None
    elif kind == "temporal":
        if (
            set(payload) != {"kind", "seconds"}
            or not _v2_number(payload.get("seconds"))
            or payload["seconds"] < 0
        ):
            refusals.append(
                Refusal(
                    "invalid_tolerance", "temporal tolerance requires non-negative seconds", subject
                )
            )
            return None
    elif (
        set(payload) != {"kind", "max_missing", "max_extra"}
        or isinstance(payload.get("max_missing"), bool)
        or not isinstance(payload.get("max_missing"), int)
        or isinstance(payload.get("max_extra"), bool)
        or not isinstance(payload.get("max_extra"), int)
        or payload["max_missing"] < 0
        or payload["max_extra"] < 0
    ):
        refusals.append(
            Refusal(
                "invalid_tolerance",
                "set tolerance requires non-negative missing and extra limits",
                subject,
            )
        )
        return None
    return dict(payload)


def _validate_expected_result_v2(payload: Any, refusals: list[Refusal]) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        refusals.append(
            Refusal("invalid_shape", "expected_result is a non-empty array", "expected_result")
        )
        return []
    if len(payload) > _V2_MAX_ASSERTIONS:
        refusals.append(
            Refusal(
                "too_many_assertions",
                "expected_result has at most 32 assertions",
                "expected_result",
            )
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(payload[: _V2_MAX_ASSERTIONS + 1]):
        subject = f"expected_result[{index}]"
        if not isinstance(item, dict):
            refusals.append(Refusal("invalid_shape", "an assertion is an object", subject))
            continue
        assertion_type = item.get("assertion_type")
        if assertion_type not in _V2_ASSERTION_KEYS:
            refusals.append(
                Refusal(
                    "unknown_assertion_type",
                    f"`{assertion_type}` is not a v2 assertion type",
                    subject,
                )
            )
            continue
        unknown = sorted(set(item) - _V2_ASSERTION_KEYS[assertion_type])
        if unknown:
            refusals.append(
                Refusal(
                    "unknown_assertion_field",
                    f"`{unknown[0]}` is not valid for {assertion_type}",
                    subject,
                )
            )
            continue
        missing = sorted(_V2_ASSERTION_KEYS[assertion_type] - set(item))
        if missing:
            refusals.append(
                Refusal(
                    "missing_assertion_field",
                    f"`{missing[0]}` is required for {assertion_type}",
                    subject,
                )
            )
            continue
        operator = item.get("operator")
        if operator not in _V2_ASSERTION_OPERATORS[assertion_type]:
            refusals.append(
                Refusal(
                    "unknown_assertion_operator",
                    f"`{operator}` is not valid for {assertion_type}",
                    subject,
                )
            )
        if "tolerance" not in item:
            refusals.append(
                Refusal(
                    "missing_tolerance",
                    "every assertion declares tolerance; null means exact",
                    subject,
                )
            )
        tolerance = _validate_v2_tolerance(
            item.get("tolerance"), assertion_type, f"{subject}.tolerance", refusals
        )
        if (
            tolerance is not None
            and tolerance.get("kind") == "temporal"
            and not _v2_rfc3339_timestamp(item.get("expected"))
        ):
            refusals.append(
                Refusal(
                    "invalid_temporal_expected",
                    "temporal tolerance requires a timezone-qualified RFC3339 expected string",
                    f"{subject}.expected",
                )
            )
        selectors, selector_fields = _validate_v2_selectors(
            item.get("selectors"), f"{subject}.selectors", refusals
        )
        fields: list[str] = []
        referenced = set(selector_fields)
        if assertion_type in {"row_set", "ordering", "invariant"}:
            fields, named = _validate_v2_fields(item.get("fields"), f"{subject}.fields", refusals)
            referenced.update(named)
        elif assertion_type == "value":
            field_name = item.get("field")
            if not isinstance(field_name, str) or not field_name.strip():
                refusals.append(
                    Refusal("invalid_field", "value assertion names one field", f"{subject}.field")
                )
            else:
                field_name = field_name.strip()
                referenced.add(field_name)
        if len(referenced) > _V2_MAX_FIELDS:
            refusals.append(
                Refusal("too_many_fields", "an assertion references at most 16 fields", subject)
            )
        if assertion_type == "cardinality":
            expected = item.get("expected")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                refusals.append(
                    Refusal(
                        "invalid_expected",
                        "cardinality expected is a non-negative integer",
                        f"{subject}.expected",
                    )
                )
        if assertion_type == "row_set":
            expected_rows = item.get("expected_rows")
            if not isinstance(expected_rows, list) or any(
                not isinstance(row, dict) for row in expected_rows
            ):
                refusals.append(
                    Refusal(
                        "invalid_expected",
                        "expected_rows is an array of objects",
                        f"{subject}.expected_rows",
                    )
                )
            elif len(expected_rows) > _V2_MAX_EXPECTED_ROWS:
                refusals.append(
                    Refusal(
                        "too_many_expected_rows",
                        "row_set has at most 100 expected rows",
                        f"{subject}.expected_rows",
                    )
                )
            else:
                allowed_fields = set(fields)
                if any(set(row) != allowed_fields for row in expected_rows):
                    refusals.append(
                        Refusal(
                            "invalid_expected",
                            "each expected row has exactly the declared fields",
                            f"{subject}.expected_rows",
                        )
                    )
        entry = {
            "assertion_type": assertion_type,
            "selectors": selectors,
            "operator": operator,
            "tolerance": tolerance,
        }
        for key in ("field", "expected", "expected_rows"):
            if key in item:
                entry[key] = (
                    item[key].strip()
                    if key == "field" and isinstance(item[key], str)
                    else item[key]
                )
        if fields:
            entry["fields"] = fields
        normalized.append(entry)
    return normalized


def _validate_required_provenance(payload: Any, refusals: list[Refusal]) -> list[dict[str, Any]]:
    """Typed link requirements over the exact provenance chain. Prose is refused."""
    if not isinstance(payload, list):
        refusals.append(
            Refusal(
                "invalid_shape", "`required_provenance` must be an array", "required_provenance"
            )
        )
        return []
    if not payload:
        refusals.append(
            Refusal(
                "empty_required_provenance",
                "a Golden Question states which provenance links its answer must carry",
                "required_provenance",
            )
        )
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        subject = f"required_provenance[{index}]"
        if not isinstance(item, dict):
            # A prose description reads like a requirement and cannot be checked
            # by anything. Story 51.3 would have to guess what it meant.
            refusals.append(
                Refusal(
                    "invalid_shape",
                    "a provenance requirement is a typed link requirement, not a description",
                    subject,
                )
            )
            continue
        link_kind = item.get("link_kind")
        if not isinstance(link_kind, str) or link_kind not in PROVENANCE_LINK_KINDS:
            refusals.append(
                Refusal(
                    "unknown_link_kind",
                    f"`{link_kind}` is not one of {', '.join(PROVENANCE_LINK_KINDS)}",
                    subject,
                )
            )
            continue
        required = item.get("required")
        if not isinstance(required, bool):
            refusals.append(
                Refusal(
                    "missing_required_flag",
                    "each provenance link declares a boolean `required` flag",
                    subject,
                )
            )
            continue
        entry = {k: v for k, v in item.items()}
        normalized.append(entry)
    return normalized


def _validate_required_provenance_v2(payload: Any, refusals: list[Refusal]) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        refusals.append(
            Refusal(
                "invalid_shape",
                "required_provenance is a non-empty array",
                "required_provenance",
            )
        )
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        subject = f"required_provenance[{index}]"
        if not isinstance(item, dict):
            refusals.append(
                Refusal("invalid_shape", "a provenance requirement is an object", subject)
            )
            continue
        unknown = sorted(set(item) - {"link_kind", "required", "expected_ref"})
        if unknown:
            refusals.append(
                Refusal(
                    "unknown_provenance_field",
                    f"`{unknown[0]}` is not a provenance requirement field",
                    subject,
                )
            )
            continue
        link_kind = item.get("link_kind")
        required = item.get("required")
        expected_ref = item.get("expected_ref")
        if link_kind not in PROVENANCE_LINK_KINDS:
            refusals.append(
                Refusal(
                    "unknown_link_kind", f"`{link_kind}` is not a provenance link kind", subject
                )
            )
            continue
        if not isinstance(required, bool):
            refusals.append(Refusal("missing_required_flag", "required must be boolean", subject))
            continue
        if expected_ref is not None and (
            not isinstance(expected_ref, str) or not is_exact_version_pin(expected_ref)
        ):
            refusals.append(
                Refusal(
                    "invalid_expected_ref", "expected_ref is an exact immutable reference", subject
                )
            )
            continue
        normalized.append(
            {"link_kind": link_kind, "required": required}
            | ({"expected_ref": expected_ref.strip()} if expected_ref is not None else {})
        )
    return normalized


#: The two vocabularies a caller may put on the wire. The grammar is the one
#: that is STORED and compared; the authoring set is what Story 51.1 wrote and
#: what a console tab loaded before this repair still holds. Which one a payload
#: speaks is read from `grammar_version`, and the field sets are checked before
#: projection so that no field is ever silently dropped in translation.
_PATTERN_GRAMMAR_KEYS = frozenset(
    {
        "grammar_version",
        "required_nodes",
        "forbidden_nodes",
        "order_constraints",
        "alternatives",
        "extra_step_rules",
    }
)
_PATTERN_AUTHORING_KEYS = frozenset(
    {"required_nodes", "forbidden_tools", "order_constraints", "alternative_paths"}
)

#: What a Golden Question that declares no expected path stores. NOT an object
#: full of empty lists: such an object is truthy, and a truthy pattern asserting
#: nothing used to make every observed path `pass` by vacuity -- the exact
#: laundering `path_verdict` exists to prevent.
EMPTY_EXPECTED_AI_PATH: dict[str, Any] = {}


def _check_authoring_nodes(nodes: Any, subject: str, refusals: list[Refusal]) -> None:
    """Unknown fields on a pre-grammar node, named before the node is projected.

    `canonicalize` reads the fields it knows and ignores the rest, which is the
    right behaviour for a pattern already stored and the wrong one for a write: a
    field the projector cannot carry must be refused, not dropped.
    """
    if not isinstance(nodes, list):
        return
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            refusals.append(
                Refusal(
                    "invalid_shape",
                    "an expected path node is an object",
                    f"{subject}[{index}]",
                )
            )
            continue
        unknown = sorted(set(node) - _PATH_NODE_KEYS)
        if unknown:
            refusals.append(
                Refusal(
                    "unknown_node_field",
                    f"`{unknown[0]}` is not a field app.ai_path_steps can record, so an "
                    "observed path could never match it",
                    f"{subject}[{index}]",
                )
            )
        # Both or neither, exactly as `ck_ai_path_steps_skill_pin` requires. The
        # projector emits a `skill_pin` only when both halves are there, so a half
        # pin would otherwise vanish in translation instead of being refused.
        if ("skill_version_id" in node) != ("skill_step_id" in node):
            refusals.append(
                Refusal(
                    "half_skill_pin",
                    "skill_version_id and skill_step_id are both or neither",
                    f"{subject}[{index}]",
                )
            )
        _check_node_word(node, f"{subject}[{index}]", refusals)


def _check_node_word(node: Any, where: str, refusals: list[Refusal]) -> None:
    """The VALUE, not only the key (AI-376, F2 and N1): an owner object type the column
    cannot hold names a node no observed step can ever match -- required or forbidden,
    pre-grammar or grammar."""
    if not isinstance(node, dict):
        return
    object_type = node.get("owner_object_type")
    if object_type is None:
        return
    from core.ai_paths import OWNER_OBJECT_TYPE_PATTERN  # noqa: PLC0415
    from core.candidate_emission import owner_object_type_for  # noqa: PLC0415

    if not isinstance(object_type, str) or not OWNER_OBJECT_TYPE_PATTERN.match(owner_object_type_for(object_type) or ""):
        refusals.append(
            Refusal(
                "unrecordable_owner_object_type",
                "owner_object_type is a word app.ai_path_steps can hold (lowercase, digits, hyphens), "
                "so an observed path could match it",
                where,
            )
        )


def _check_node_vocabulary(node: dict[str, Any], subject: str, refusals: list[Refusal]) -> None:
    """The product vocabulary the JSON Schema cannot express.

    The grammar knows a node key is `workspace/type/id` and that a version pin is
    not `latest`; it does not know which workspaces and which step kinds this
    product has. Left unchecked, a node naming a workspace nobody deploys would
    be a required node no observed path can ever satisfy -- a question that looks
    specified and fails forever.
    """
    step_kind = node.get("step_kind")
    if step_kind is not None and step_kind not in STEP_KINDS:
        refusals.append(
            Refusal(
                "unknown_step_kind",
                f"`{step_kind}` is not one of {', '.join(STEP_KINDS)}",
                f"{subject}.step_kind",
            )
        )

    key = node.get("key")
    workspace = key.split("/", 1)[0] if isinstance(key, str) else ""
    if workspace and workspace not in OWNER_WORKSPACES:
        refusals.append(
            Refusal(
                "unknown_owner_workspace",
                f"`{workspace}` is not one of {', '.join(OWNER_WORKSPACES)}",
                f"{subject}.key",
            )
        )

    skill_pin = node.get("skill_pin")
    if isinstance(skill_pin, dict) and not (
        str(skill_pin.get("skill_version_id") or "").strip()
        and str(skill_pin.get("skill_step_id") or "").strip()
    ):
        refusals.append(
            Refusal(
                "half_skill_pin",
                "skill_version_id and skill_step_id are both or neither",
                f"{subject}.skill_pin",
            )
        )

    # The Python half of `app.is_exact_version_pin` (migration 153). The schema
    # refuses `latest`, `current` and `head`; this refuses a pin that is blank,
    # which reads as a pin and asserts nothing.
    for field_name, value in (
        ("owner_version_id", node.get("owner_version_id")),
        ("skill_pin.skill_version_id", (node.get("skill_pin") or {}).get("skill_version_id")),
    ):
        if value is not None and not is_exact_version_pin(value):
            refusals.append(
                Refusal(
                    "moving_version_pin",
                    f"`{value}` names a moving target; an expected path pins an exact version",
                    f"{subject}.{field_name}",
                )
            )


def _validate_expected_ai_path(payload: Any, refusals: list[Refusal]) -> dict[str, Any]:
    """A PATTERN, in the one grammar `expected_ai_path` compares against.

    `analyze-and-test.md:266-280`. Until 2026-08-17 this function wrote a second
    vocabulary -- `forbidden_tools`, order constraints as integer indices, nodes
    with no `key` -- that the comparator could not read, while the comparator's
    own `validate_pattern` had no production caller at all. A Golden Question
    with one required node therefore raised `KeyError: 'key'` inside
    `record_case_verdicts` the first time a case pinned an observed path, and a
    forbidden tool could never fail anything, because `compare()` never read that
    field.

    What is stored is now the grammar of `schemas/expected-ai-path.schema.json`,
    `grammar_version` included, and an invalid pattern is refused HERE -- never
    later, at the moment it is asked to compare.
    """
    # Only the refusals THIS function adds may short-circuit it. Reading the
    # shared list would let an unrelated refusal on `expected_result` hide every
    # pattern refusal, and an author would fix one field to discover the next.
    before = len(refusals)

    if not isinstance(payload, dict):
        refusals.append(
            Refusal("invalid_shape", "`expected_ai_path` must be an object", "expected_ai_path")
        )
        return dict(EMPTY_EXPECTED_AI_PATH)

    speaks_grammar = "grammar_version" in payload
    allowed = _PATTERN_GRAMMAR_KEYS if speaks_grammar else _PATTERN_AUTHORING_KEYS
    unknown = sorted(set(payload) - allowed)
    if unknown:
        refusals.append(
            Refusal(
                "unknown_pattern_field",
                f"`{unknown[0]}` is not part of the expected-path pattern",
                "expected_ai_path",
            )
        )
        return dict(EMPTY_EXPECTED_AI_PATH)

    # Forbidden nodes are judged on their WORD in both vocabularies (AI-376, N1): a
    # forbidden rule no observed step can match never fires, and that reads as pass.
    for index, node in enumerate(payload.get("forbidden_nodes") or []):
        _check_node_word(node, f"expected_ai_path.forbidden_nodes[{index}]", refusals)

    if not speaks_grammar:
        # The pre-grammar vocabulary is accepted for exactly as long as a console
        # tab opened before this repair can still be submitting it. Its unknown
        # node fields are named here, because the projector below carries what it
        # knows and would drop the rest without a word.
        _check_authoring_nodes(
            payload.get("required_nodes"), "expected_ai_path.required_nodes", refusals
        )
        for index, alternative in enumerate(payload.get("alternative_paths") or []):
            if isinstance(alternative, dict):
                _check_authoring_nodes(
                    alternative.get("required_nodes"),
                    f"expected_ai_path.alternative_paths[{index}].required_nodes",
                    refusals,
                )
        # An index pointing past the declared nodes resolves to no key at all, so
        # the projector drops the constraint. Dropping it silently would turn a
        # typo into a rule that reads as permanently satisfied.
        declared_count = len(payload.get("required_nodes") or [])
        for index, constraint in enumerate(payload.get("order_constraints") or []):
            if not isinstance(constraint, dict):
                continue
            for side in ("before", "after"):
                position = constraint.get(side)
                if isinstance(position, bool) or not isinstance(position, int):
                    refusals.append(
                        Refusal(
                            "invalid_order_constraint",
                            "`before` and `after` are indices into required_nodes",
                            f"expected_ai_path.order_constraints[{index}]",
                        )
                    )
                elif not 0 <= position < declared_count:
                    refusals.append(
                        Refusal(
                            "dangling_order_constraint",
                            "an order constraint names a required node that was not declared",
                            f"expected_ai_path.order_constraints[{index}].{side}",
                        )
                    )
        if len(refusals) > before:
            return dict(EMPTY_EXPECTED_AI_PATH)

    pattern = expected_ai_path.canonicalize(payload)
    if not expected_ai_path.declares_expectations(pattern):
        return dict(EMPTY_EXPECTED_AI_PATH)

    addressed: list[tuple[dict[str, Any], str]] = [
        (node, f"expected_ai_path.required_nodes[{index}]")
        for index, node in enumerate(pattern.get("required_nodes") or [])
        if isinstance(node, dict)
    ]
    for group_index, group in enumerate(pattern.get("alternatives") or []):
        for branch_index, branch in enumerate(group.get("branches") or []):
            addressed.extend(
                (
                    node,
                    f"expected_ai_path.alternatives[{group_index}].branches"
                    f"[{branch_index}].required_nodes[{node_index}]",
                )
                for node_index, node in enumerate(branch.get("required_nodes") or [])
                if isinstance(node, dict)
            )

    # An expected node is found by its owner triple and by nothing else, because
    # that triple is the only address `app.ai_path_steps` records. A node without
    # one is not a loose specification, it is a node no observed step can ever
    # satisfy -- and the schema would say so as `'//' does not match`.
    for node, subject in addressed:
        _check_node_vocabulary(node, subject, refusals)
        if node.get("key") == "//":
            refusals.append(
                Refusal(
                    "node_without_owner",
                    "name the owner workspace, object type and object id: an expected node is "
                    "matched by that triple, and a node without it can never be observed",
                    subject,
                )
            )

    # The grammar refuses a group of one branch, and it is right to: a lone
    # "alternative" is satisfied or the case fails, which is what a required node
    # already means. The schema would say "is too short"; this says what to do.
    for group_index, group in enumerate(pattern.get("alternatives") or []):
        if len(group.get("branches") or []) < 2:
            refusals.append(
                Refusal(
                    "single_alternative_path",
                    "one alternative is not a choice: add its nodes to the required path, or "
                    "declare a second accepted path beside it",
                    f"expected_ai_path.alternatives[{group_index}]",
                )
            )
    if len(refusals) > before:
        return dict(EMPTY_EXPECTED_AI_PATH)

    try:
        expected_ai_path.validate_pattern(pattern)
    except expected_ai_path.ExpectedPathInvalid as invalid:
        refused = invalid.as_refusal()
        refusals.append(Refusal(refused["code"], refused["message"], refused["subject"]))
        return dict(EMPTY_EXPECTED_AI_PATH)

    return pattern


def _validate_capability_tags(payload: Any, refusals: list[Refusal]) -> list[str]:
    if not isinstance(payload, list) or not payload:
        refusals.append(
            Refusal(
                "missing_capability_tags",
                "a Golden Question names at least one governed capability it exercises",
                "capability_tags",
            )
        )
        return []
    installed = connector_names()
    tags: list[str] = []
    for index, tag in enumerate(payload):
        subject = f"capability_tags[{index}]"
        if not isinstance(tag, str) or not tag.strip():
            refusals.append(Refusal("invalid_shape", "a capability tag is a key", subject))
            continue
        value = tag.strip().lower()
        if len(value) > _CAPABILITY_PATTERN_MAX:
            refusals.append(
                Refusal(
                    "invalid_capability",
                    f"a capability key is at most {_CAPABILITY_PATTERN_MAX} characters",
                    subject,
                )
            )
            continue
        if value in installed:
            # `analyze-and-test.md:237-238`. A connector name aggregates evidence
            # by where the data came from, not by what the product was asked to
            # do -- and it makes every question of a connector look like one
            # capability the day a second source answers it.
            refusals.append(
                Refusal(
                    "connector_name_is_not_a_capability",
                    f"`{value}` is a connector, not a governed capability",
                    subject,
                )
            )
            continue
        if value not in tags:
            tags.append(value)
    return sorted(tags)


def _validate_reference_paths(
    conn, *, org_id: str, project_id: str, payload: Any, refusals: list[Refusal]
) -> list[dict[str, Any]]:
    """0..N reference execution paths, each an exact Query Spec version identity.

    Zero is valid and is not a gap: correctness is the typed assertion array. Two
    or more is the multi-approach question AI-81 point (2) describes. NOTHING here
    stores SQL text.
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        refusals.append(
            Refusal("invalid_shape", "`reference_paths` must be an array", "reference_paths")
        )
        return []

    normalized: list[dict[str, Any]] = []
    wanted: list[str] = []
    for index, entry in enumerate(payload):
        subject = f"reference_paths[{index}]"
        if not isinstance(entry, dict):
            refusals.append(
                Refusal(
                    "invalid_shape",
                    "a reference path names a Query Spec version and its role",
                    subject,
                )
            )
            continue
        version_id = entry.get("query_spec_version_id")
        role = entry.get("role")
        if not isinstance(version_id, str) or not is_exact_version_pin(version_id):
            refusals.append(
                Refusal(
                    "moving_version_pin",
                    "a reference path pins an exact Query Spec version",
                    subject,
                )
            )
            continue
        if not isinstance(role, str) or role not in REFERENCE_PATH_ROLES:
            refusals.append(
                Refusal(
                    "invalid_role",
                    f"a reference path is `{'` or `'.join(REFERENCE_PATH_ROLES)}`",
                    subject,
                )
            )
            continue
        if version_id in wanted:
            refusals.append(
                Refusal(
                    "duplicate_reference_path",
                    f"`{version_id}` is declared twice",
                    subject,
                )
            )
            continue
        wanted.append(version_id)
        normalized.append(
            {"ordinal": len(normalized), "query_spec_version_id": version_id, "role": role}
        )

    if wanted:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.query_spec_versions
                WHERE id = ANY(%s) AND org_id = %s AND project_id = %s
                """,
                (wanted, org_id, project_id),
            )
            found = {str(row[0]) for row in cur.fetchall()}
        for entry in list(normalized):
            if entry["query_spec_version_id"] not in found:
                refusals.append(
                    Refusal(
                        "unknown_reference_path",
                        f"`{entry['query_spec_version_id']}` is not a Query Spec version of "
                        "this Project",
                        f"reference_paths[{entry['ordinal']}]",
                    )
                )
                normalized.remove(entry)
    return normalized


def validate_golden_question_version(
    conn, *, org_id: str, project_id: str, payload: dict[str, Any]
) -> ValidatedGoldenQuestionVersion:
    """Validate one Golden Question document against the live governed rows.

    Raises :class:`GoldenQuestionRefused` carrying EVERY reason. Never repairs,
    never substitutes a near-miss pin, never falls back to another version.
    """
    if not isinstance(payload, dict):
        raise GoldenQuestionRefused("invalid_document", "a Golden Question is an object")

    refusals: list[Refusal] = []
    requested_contract = payload.get("contract_version")
    is_v2 = requested_contract == GOLDEN_QUESTION_V2_CONTRACT_VERSION
    if requested_contract not in (
        None,
        GOLDEN_QUESTION_CONTRACT_VERSION,
        GOLDEN_QUESTION_V2_CONTRACT_VERSION,
    ):
        refusals.append(
            Refusal(
                "unknown_contract_version",
                f"`{requested_contract}` is not a supported Golden Question contract",
                "contract_version",
            )
        )
    if is_v2:
        allowed = {
            "contract_version",
            "business_domain_id",
            "business_domain_version_number",
            "business_classification_id",
            "semantic_view_id",
            "semantic_view_version_id",
            "semantic_view_version_role",
            "question",
            "time_boundary",
            "expected_result",
            "required_provenance",
            "expected_ai_path",
            "result_type",
            "capability_tags",
            "severity",
            "expected_render_ref",
            "reference_paths",
        }
        for key in sorted(set(payload) - allowed):
            refusals.append(
                Refusal(
                    "unknown_field",
                    f"`{key}` is not part of golden-question.v2",
                    key,
                )
            )

    domain_id, domain_version, classification_id = _check_business_domain_pin(
        conn, org_id=org_id, payload=payload, refusals=refusals
    )
    view_id, view_version_id, view_role = _check_semantic_view_pin(
        conn, project_id=project_id, payload=payload, refusals=refusals
    )

    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        refusals.append(
            Refusal("missing_field", "a Golden Question states its business question", "question")
        )
        question = ""
    elif len(question.strip()) > 4000:
        refusals.append(
            Refusal(
                "invalid_length",
                "the business question is at most 4000 characters",
                "question",
            )
        )

    time_boundary = payload.get("time_boundary")
    if time_boundary is None:
        # An undeclared boundary is not a missing one: `{}` says "this question
        # declares no fixed range", which is a decision a reader can see.
        time_boundary = {}
    if not isinstance(time_boundary, dict):
        refusals.append(
            Refusal(
                "invalid_shape",
                "`time_boundary` is an object; declare `{}` when the question fixes none",
                "time_boundary",
            )
        )
        time_boundary = {}

    expected_result = (
        _validate_expected_result_v2(payload.get("expected_result"), refusals)
        if is_v2
        else _validate_expected_result(payload.get("expected_result"), refusals)
    )
    required_provenance = (
        _validate_required_provenance_v2(payload.get("required_provenance"), refusals)
        if is_v2
        else _validate_required_provenance(payload.get("required_provenance"), refusals)
    )
    expected_ai_path = _validate_expected_ai_path(
        payload.get("expected_ai_path") if payload.get("expected_ai_path") is not None else {},
        refusals,
    )

    result_type = payload.get("result_type")
    if not isinstance(result_type, str) or result_type not in RESULT_TYPES:
        refusals.append(
            Refusal(
                "unknown_result_type",
                f"`{result_type}` is not one of {', '.join(RESULT_TYPES)}",
                "result_type",
            )
        )
        result_type = ""

    capability_tags = _validate_capability_tags(payload.get("capability_tags"), refusals)

    severity = payload.get("severity")
    if not isinstance(severity, str) or severity not in SEVERITIES:
        refusals.append(
            Refusal(
                "unknown_severity",
                f"`{severity}` is not one of {', '.join(SEVERITIES)}",
                "severity",
            )
        )
        severity = ""

    if payload.get("expected_render_ref") is not None:
        # The rendered artifact does not exist. Accepting an identifier here would
        # store a pin nothing can resolve and turn `unverifiable` into a green
        # verdict for a Render nobody ever produced.
        refusals.append(
            Refusal(
                "render_owner_not_delivered",
                "the Render pin is declared and left empty until Story 50.4 delivers the "
                "object; a missing pin reports Unverifiable",
                "expected_render_ref",
            )
        )

    reference_paths = _validate_reference_paths(
        conn,
        org_id=org_id,
        project_id=project_id,
        payload=payload.get("reference_paths"),
        refusals=refusals,
    )

    document = {
        "contract_version": (
            GOLDEN_QUESTION_V2_CONTRACT_VERSION if is_v2 else GOLDEN_QUESTION_CONTRACT_VERSION
        ),
        "business_domain": {
            "id": domain_id,
            "version_number": domain_version,
            "classification_id": classification_id,
        },
        "semantic_view": {
            "id": view_id,
            "version_id": view_version_id,
            "role": view_role,
        },
        "question": question.strip(),
        "time_boundary": time_boundary,
        "expected_result": expected_result,
        "required_provenance": required_provenance,
        "expected_ai_path": expected_ai_path,
        "result_type": result_type,
        "capability_tags": capability_tags,
        "severity": severity,
        # Declared and empty. It travels inside the hashed document so the day
        # Story 50.4 fills it, the hash changes and the difference is visible.
        "expected_render_ref": None,
        "reference_paths": reference_paths,
    }
    if is_v2 and not _v2_json_value(document):
        refusals.append(
            Refusal(
                "non_json_value",
                "golden-question.v2 accepts finite JSON values only",
                None,
            )
        )
    if is_v2 and len(_canonical_json(document).encode("utf-8")) > _V2_MAX_CANONICAL_BYTES:
        refusals.append(
            Refusal(
                "document_too_large",
                "golden-question.v2 canonical JSON is at most 65,536 bytes",
                None,
            )
        )
    if refusals:
        raise GoldenQuestionRefused(
            "invalid_golden_question",
            f"the Golden Question was refused on {len(refusals)} point(s)",
            refusals,
        )
    return ValidatedGoldenQuestionVersion(
        document=document,
        content_hash=canonical_hash(document),
        business_domain_id=domain_id,
        business_domain_version_number=domain_version,
        business_classification_id=classification_id,
        semantic_view_id=view_id,
        semantic_view_version_id=view_version_id,
        semantic_view_version_role=view_role,
        result_type=result_type,
        capability_tags=capability_tags,
        severity=severity,
        reference_paths=reference_paths,
    )


# ---------------------------------------------------------------------------
# Persistence: a stable head, and immutable versions on top of it.
#
# Both writes happen in the CALLER's transaction, so the head's
# `current_version_id` advances in the same transaction as the version insert --
# which is what makes the deferrable foreign key of migration 153 safe.
# ---------------------------------------------------------------------------


def _audit(
    conn,
    *,
    actor: str,
    action: str,
    org_id: str,
    project_id: str,
    resource_id: str,
    after: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
) -> None:
    insert_audit_row(
        conn,
        identity=actor,
        action=action,
        provider_account="test",
        connection_ref="",
        metadata={
            "effective_org_id": org_id,
            "project_id": project_id,
            "resource_id": resource_id,
            "before": before,
            "after": after,
        },
    )


def _insert_version(
    cur,
    *,
    golden_question_id: str,
    org_id: str,
    project_id: str,
    version_number: int,
    predecessor_version_id: str | None,
    validated: ValidatedGoldenQuestionVersion,
    actor: str,
) -> tuple[str, Any]:
    version_id = f"gqv_{ULID()}"
    cur.execute(
        """
        INSERT INTO app.golden_question_versions
            (id, golden_question_id, org_id, project_id, version_number,
             business_domain_id, business_domain_version_number,
             business_classification_id,
             semantic_view_id, semantic_view_version_id, semantic_view_version_role,
             question, time_boundary, expected_result, required_provenance,
             expected_ai_path, result_type, capability_tags, severity,
             expected_render_ref, content_hash, predecessor_version_id, created_by,
             contract_version)
        VALUES (%s, %s, %s, %s, %s,
                %s, %s,
                %s,
                %s, %s, %s,
                %s, %s::jsonb, %s::jsonb, %s::jsonb,
                %s::jsonb, %s, %s, %s,
                NULL, %s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (
            version_id,
            golden_question_id,
            org_id,
            project_id,
            version_number,
            validated.business_domain_id,
            validated.business_domain_version_number,
            validated.business_classification_id,
            validated.semantic_view_id,
            validated.semantic_view_version_id,
            validated.semantic_view_version_role,
            validated.document["question"],
            _canonical_json(validated.document["time_boundary"]),
            _canonical_json(validated.document["expected_result"]),
            _canonical_json(validated.document["required_provenance"]),
            _canonical_json(validated.document["expected_ai_path"]),
            validated.result_type,
            validated.capability_tags,
            validated.severity,
            validated.content_hash,
            predecessor_version_id,
            actor,
            validated.document["contract_version"],
        ),
    )
    row = cur.fetchone()
    for path in validated.reference_paths:
        cur.execute(
            """
            INSERT INTO app.golden_question_reference_paths
                (id, golden_question_version_id, org_id, project_id, ordinal,
                 query_spec_version_id, role)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"gqr_{ULID()}",
                version_id,
                org_id,
                project_id,
                path["ordinal"],
                path["query_spec_version_id"],
                path["role"],
            ),
        )
    return str(row[0]), row[1]


def create_golden_question(
    conn,
    *,
    org_id: str,
    project_id: str,
    title: str,
    owner: str,
    validated: ValidatedGoldenQuestionVersion,
    actor: str,
) -> dict[str, Any]:
    """Create the stable head and its version 1, in one transaction."""
    refusals: list[Refusal] = []
    title = title.strip() if isinstance(title, str) else ""
    owner = owner.strip() if isinstance(owner, str) else ""
    if not title or len(title) > 200:
        refusals.append(Refusal("invalid_title", "a title of 1 to 200 characters", "title"))
    if not owner or len(owner) > 200:
        refusals.append(Refusal("invalid_owner", "a Golden Question names its steward", "owner"))
    if refusals:
        raise GoldenQuestionRefused(
            "invalid_golden_question", "the Golden Question head was refused", refusals
        )

    golden_question_id = f"gq_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.golden_questions
                (id, org_id, project_id, title, owner, lifecycle, created_by)
            VALUES (%s, %s, %s, %s, %s, 'draft', %s)
            """,
            (golden_question_id, org_id, project_id, title, owner, actor),
        )
        version_id, created_at = _insert_version(
            cur,
            golden_question_id=golden_question_id,
            org_id=org_id,
            project_id=project_id,
            version_number=1,
            predecessor_version_id=None,
            validated=validated,
            actor=actor,
        )
        cur.execute(
            """
            UPDATE app.golden_questions
            SET current_version_id = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, golden_question_id, org_id, project_id),
        )
    _audit(
        conn,
        actor=actor,
        action=ACTION_TEST_GOLDEN_QUESTION_CREATED,
        org_id=org_id,
        project_id=project_id,
        resource_id=golden_question_id,
        after={"version_id": version_id, "content_hash": validated.content_hash},
    )
    return {
        "golden_question_id": golden_question_id,
        "version_id": version_id,
        "version_number": 1,
        "content_hash": validated.content_hash,
        "created_at": created_at.isoformat() if created_at else None,
    }


def create_golden_question_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    golden_question_id: str,
    validated: ValidatedGoldenQuestionVersion,
    actor: str,
) -> dict[str, Any]:
    """Append the next immutable version. The predecessor is never rewritten."""
    with conn.cursor() as cur:
        # Scope is part of the lookup, not checked afterwards: a foreign head
        # simply does not resolve, and answers exactly like a missing one.
        cur.execute(
            """
            SELECT current_version_id, lifecycle FROM app.golden_questions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (golden_question_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise GoldenQuestionNotFound("golden question not found in this Project")
        predecessor = head[0]
        if predecessor is None:
            raise GoldenQuestionRefused(
                "head_without_version", "this Golden Question has no current version to revise"
            )
        if str(head[1]) == "archived":
            raise GoldenQuestionRefused(
                "lifecycle_closed",
                "an archived Golden Question is history; author a new question instead of "
                "reviving its identity",
            )
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.golden_question_versions
            WHERE golden_question_id = %s AND project_id = %s
            """,
            (golden_question_id, project_id),
        )
        version_number = int(cur.fetchone()[0])
        version_id, created_at = _insert_version(
            cur,
            golden_question_id=golden_question_id,
            org_id=org_id,
            project_id=project_id,
            version_number=version_number,
            predecessor_version_id=predecessor,
            validated=validated,
            actor=actor,
        )
        cur.execute(
            """
            UPDATE app.golden_questions
            SET current_version_id = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, golden_question_id, org_id, project_id),
        )
    _audit(
        conn,
        actor=actor,
        action=ACTION_TEST_GOLDEN_QUESTION_VERSION_CREATED,
        org_id=org_id,
        project_id=project_id,
        resource_id=golden_question_id,
        before={"version_id": predecessor},
        after={"version_id": version_id, "content_hash": validated.content_hash},
    )
    return {
        "golden_question_id": golden_question_id,
        "version_id": version_id,
        "version_number": version_number,
        "predecessor_version_id": predecessor,
        "content_hash": validated.content_hash,
        "created_at": created_at.isoformat() if created_at else None,
    }


def set_lifecycle(
    conn,
    *,
    org_id: str,
    project_id: str,
    golden_question_id: str,
    lifecycle: str,
    actor: str,
    owner: str | None = None,
) -> dict[str, Any]:
    """Advance the head's stewardship. Declared transitions only."""
    if lifecycle not in LIFECYCLES:
        raise GoldenQuestionRefused(
            "unknown_lifecycle",
            f"`{lifecycle}` is not one of {', '.join(LIFECYCLES)}",
            [Refusal("unknown_lifecycle", "unknown lifecycle state", "lifecycle")],
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle, owner FROM app.golden_questions
            WHERE id = %s AND org_id = %s AND project_id = %s
            FOR UPDATE
            """,
            (golden_question_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise GoldenQuestionNotFound("golden question not found in this Project")
        current = str(row[0])
        if lifecycle != current and lifecycle not in LIFECYCLE_TRANSITIONS[current]:
            raise GoldenQuestionRefused(
                "invalid_transition",
                f"`{current}` does not transition to `{lifecycle}`",
                [
                    Refusal(
                        "invalid_transition",
                        f"declared transitions from `{current}`: "
                        f"{', '.join(LIFECYCLE_TRANSITIONS[current]) or 'none'}",
                        "lifecycle",
                    )
                ],
            )
        next_owner = owner.strip() if isinstance(owner, str) and owner.strip() else str(row[1])
        cur.execute(
            """
            UPDATE app.golden_questions
            SET lifecycle = %s, owner = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (lifecycle, next_owner, golden_question_id, org_id, project_id),
        )
    _audit(
        conn,
        actor=actor,
        action=ACTION_TEST_GOLDEN_QUESTION_LIFECYCLE_CHANGED,
        org_id=org_id,
        project_id=project_id,
        resource_id=golden_question_id,
        before={"lifecycle": current, "owner": row[1]},
        after={"lifecycle": lifecycle, "owner": next_owner},
    )
    return {
        "golden_question_id": golden_question_id,
        "lifecycle": lifecycle,
        "owner": next_owner,
    }


# ---------------------------------------------------------------------------
# Reads. Every one of them is scoped by `(id, org_id, project_id)`; an id from
# the client is never trusted on its own.
# ---------------------------------------------------------------------------


def list_golden_questions(
    conn, *, org_id: str, project_id: str, lifecycle: str | None = None
) -> list[dict[str, Any]]:
    clause = ""
    params: list[Any] = [org_id, project_id]
    if lifecycle:
        clause = "AND q.lifecycle = %s"
        params.append(lifecycle)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT q.id, q.title, q.owner, q.lifecycle, q.current_version_id,
                   q.created_by, q.created_at, q.updated_at,
                   v.version_number, v.business_domain_id,
                   v.business_domain_version_number, v.semantic_view_id,
                   v.semantic_view_version_id, v.result_type, v.severity,
                   v.capability_tags, d.name
            FROM app.golden_questions q
            LEFT JOIN app.golden_question_versions v
              ON v.id = q.current_version_id
             AND v.org_id = q.org_id AND v.project_id = q.project_id
            -- THE DOMAIN'S NAME IS SERVED, NOT COMPOSED IN THE BROWSER. The
            -- Explorer used to look this pin up in the org's domain list and
            -- fall back to `bd_<ULID>` when the pin named a domain that list
            -- does not carry -- an identifier in a picker, which
            -- `visualization-and-rendering.md` names in its `Incomplete if`.
            -- The join is a LEFT JOIN and the name may be NULL: an archived or
            -- deleted domain still has a version pinned by an immutable Golden
            -- Question version, and inventing a word for it would be worse than
            -- saying there is none.
            LEFT JOIN {catalogue.DOMAIN_SOURCE} d
              ON d.id = v.business_domain_id AND d.org_id = q.org_id
            WHERE q.org_id = %s AND q.project_id = %s {clause}
            ORDER BY q.created_at DESC, q.id
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "owner": r[2],
            "lifecycle": r[3],
            "current_version_id": r[4],
            "created_by": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "updated_at": r[7].isoformat() if r[7] else None,
            "current_version": (
                None
                if r[8] is None
                else {
                    "version_number": r[8],
                    "business_domain_id": r[9],
                    "business_domain_version_number": r[10],
                    # NULL, never the id: a pin whose domain no longer exists has
                    # no name, and the reader is told so rather than shown a ULID.
                    "business_domain_name": r[16],
                    "semantic_view_id": r[11],
                    "semantic_view_version_id": r[12],
                    "result_type": r[13],
                    "severity": r[14],
                    "capability_tags": list(r[15] or []),
                }
            ),
        }
        for r in rows
    ]


def _version_payload(row: Any, reference_paths: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row[0],
        "golden_question_id": row[1],
        "version_number": row[2],
        "business_domain_id": row[3],
        "business_domain_version_number": row[4],
        "business_classification_id": row[5],
        "semantic_view_id": row[6],
        "semantic_view_version_id": row[7],
        "semantic_view_version_role": row[8],
        "question": row[9],
        "time_boundary": row[10],
        "expected_result": row[11],
        "required_provenance": row[12],
        "expected_ai_path": row[13],
        "result_type": row[14],
        "capability_tags": list(row[15] or []),
        "severity": row[16],
        # Always NULL, by CHECK. Carried in the payload rather than omitted, so a
        # reader sees a declared-and-empty pin instead of an absent field.
        "expected_render_ref": row[17],
        "content_hash": row[18],
        "predecessor_version_id": row[19],
        "created_by": row[20],
        "created_at": row[21].isoformat() if row[21] else None,
        "contract_version": (row[22] if len(row) > 22 else GOLDEN_QUESTION_CONTRACT_VERSION),
        "reference_paths": reference_paths,
    }


_VERSION_COLUMNS = """
    id, golden_question_id, version_number, business_domain_id,
    business_domain_version_number, business_classification_id,
    semantic_view_id, semantic_view_version_id, semantic_view_version_role,
    question, time_boundary, expected_result, required_provenance,
    expected_ai_path, result_type, capability_tags, severity,
    expected_render_ref, content_hash, predecessor_version_id, created_by,
    created_at, contract_version
"""


def _reference_paths(cur, *, version_id: str, org_id: str, project_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT ordinal, query_spec_version_id, role
        FROM app.golden_question_reference_paths
        WHERE golden_question_version_id = %s AND org_id = %s AND project_id = %s
        ORDER BY ordinal
        """,
        (version_id, org_id, project_id),
    )
    return [{"ordinal": r[0], "query_spec_version_id": r[1], "role": r[2]} for r in cur.fetchall()]


def get_golden_question(
    conn, *, org_id: str, project_id: str, golden_question_id: str
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, owner, lifecycle, current_version_id, created_by,
                   created_at, updated_at
            FROM app.golden_questions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (golden_question_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise GoldenQuestionNotFound("golden question not found in this Project")
        cur.execute(
            f"""
            SELECT {_VERSION_COLUMNS}
            FROM app.golden_question_versions
            WHERE golden_question_id = %s AND org_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (golden_question_id, org_id, project_id),
        )
        version_rows = cur.fetchall()
        versions = []
        for row in version_rows:
            versions.append(
                _version_payload(
                    row,
                    _reference_paths(
                        cur, version_id=str(row[0]), org_id=org_id, project_id=project_id
                    ),
                )
            )
    current = next((v for v in versions if v["id"] == head[4]), None)
    return {
        "id": head[0],
        "title": head[1],
        "owner": head[2],
        "lifecycle": head[3],
        "current_version_id": head[4],
        "created_by": head[5],
        "created_at": head[6].isoformat() if head[6] else None,
        "updated_at": head[7].isoformat() if head[7] else None,
        "lifecycle_transitions": list(LIFECYCLE_TRANSITIONS[str(head[3])]),
        "current_version": current,
        "versions": versions,
    }


def get_golden_question_version(
    conn, *, org_id: str, project_id: str, golden_question_id: str, version_id: str
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_VERSION_COLUMNS}
            FROM app.golden_question_versions
            WHERE id = %s AND golden_question_id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, golden_question_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise GoldenQuestionNotFound("golden question version not found in this Project")
        paths = _reference_paths(cur, version_id=version_id, org_id=org_id, project_id=project_id)
    return _version_payload(row, paths)


def golden_question_options(conn, *, org_id: str, project_id: str) -> dict[str, Any]:
    """The governed choices, served from the live rows.

    A hand-maintained UI catalogue keeps offering a domain version the day it is
    archived, and a Semantic View version the day it is superseded. The server
    composes this from the same tables the validator reads, so what the console
    offers and what the server accepts cannot drift.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.id, d.name, d.status, MAX(v.version_number)
            FROM {catalogue.DOMAIN_SOURCE} d
            JOIN {catalogue.DOMAIN_VERSION_SOURCE} v
              ON v.domain_id = d.id AND v.org_id = d.org_id
            WHERE d.org_id = %s AND d.status = 'active'
            GROUP BY d.id, d.name, d.status
            ORDER BY d.name, d.id
            """,
            (org_id,),
        )
        domains = [
            {"id": r[0], "name": r[1], "status": r[2], "latest_version_number": r[3]}
            for r in cur.fetchall()
        ]
        cur.execute(
            f"""
            SELECT c.id, c.domain_id, c.name
            FROM {catalogue.CLASSIFICATION_SOURCE} c
            WHERE c.org_id = %s AND c.status = 'active'
            ORDER BY c.domain_id, c.name, c.id
            """,
            (org_id,),
        )
        classifications = [
            {"id": r[0], "business_domain_id": r[1], "name": r[2]} for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT v.id, v.view_id, v.name, v.version_number, v.status
            FROM app.semantic_view_versions v
            WHERE v.project_id = %s AND v.status = ANY(%s)
            ORDER BY v.view_id, v.version_number DESC
            """,
            (project_id, list(PINNABLE_VIEW_STATUSES)),
        )
        views = [
            {
                "semantic_view_version_id": r[0],
                "semantic_view_id": r[1],
                "name": r[2],
                "version_number": r[3],
                "status": r[4],
            }
            for r in cur.fetchall()
        ]
    return {
        "business_domains": domains,
        "business_classifications": classifications,
        "semantic_view_versions": views,
        "semantic_view_version_roles": list(SEMANTIC_VIEW_ROLES),
        "result_types": list(RESULT_TYPES),
        "severities": list(SEVERITIES),
        "lifecycles": list(LIFECYCLES),
        "lifecycle_transitions": {k: list(v) for k, v in LIFECYCLE_TRANSITIONS.items()},
        "assertion_types": list(ASSERTION_TYPES),
        "tolerance_kinds": list(TOLERANCE_KINDS),
        "provenance_link_kinds": list(PROVENANCE_LINK_KINDS),
        "path_step_kinds": list(STEP_KINDS),
        "path_owner_workspaces": list(OWNER_WORKSPACES),
        "reference_path_roles": list(REFERENCE_PATH_ROLES),
        # Named so a console cannot invent its own capability list; the refusal
        # itself stays server-side because the registry is the installed set.
        "capability_rule": "a capability names a governed product or tool capability, "
        "never a connector",
    }


def golden_question_coverage(
    conn, *, org_id: str, project_id: str, golden_question_id: str
) -> dict[str, Any]:
    """The Coverage tab: what is pinned, and what cannot yet be judged.

    Everything reported here is either a stored pin -- a fact -- or an
    `unverifiable` dimension with a reason code and the owner story that will
    lift it. There is no aggregate score and no verdict that could be read as
    green: `analyze-and-test.md:336-337` forbids a percentage that hides a
    critical failure, and an absence dressed as a number is exactly that.
    """
    question = get_golden_question(
        conn, org_id=org_id, project_id=project_id, golden_question_id=golden_question_id
    )
    version = question["current_version"]
    pinned: dict[str, Any] = {
        "current_version_id": question["current_version_id"],
        "business_domain": None,
        "semantic_view": None,
        "result_type": None,
        "severity": None,
        "capability_tags": [],
        "reference_path_count": 0,
        "expected_assertion_count": 0,
        "required_provenance_count": 0,
        "expected_required_node_count": 0,
    }
    if version is not None:
        pinned.update(
            {
                "business_domain": {
                    "id": version["business_domain_id"],
                    "version_number": version["business_domain_version_number"],
                    "classification_id": version["business_classification_id"],
                },
                "semantic_view": {
                    "id": version["semantic_view_id"],
                    "version_id": version["semantic_view_version_id"],
                    "role": version["semantic_view_version_role"],
                },
                "result_type": version["result_type"],
                "severity": version["severity"],
                "capability_tags": version["capability_tags"],
                "reference_path_count": len(version["reference_paths"]),
                "expected_assertion_count": len(version["expected_result"] or []),
                "required_provenance_count": len(version["required_provenance"] or []),
                "expected_required_node_count": len(
                    (version["expected_ai_path"] or {}).get("required_nodes") or []
                ),
            }
        )

    semantic_view_members: dict[str, Any] | None = None
    if version is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status FROM app.semantic_view_versions
                WHERE id = %s AND view_id = %s AND project_id = %s
                """,
                (
                    version["semantic_view_version_id"],
                    version["semantic_view_id"],
                    project_id,
                ),
            )
            row = cur.fetchone()
            semantic_view_members = {"status": row[0] if row else None}

    return {
        "golden_question_id": golden_question_id,
        "pinned": pinned,
        "semantic_view_version": semantic_view_members,
        # Five declared absences, each with its owner story. Never `pass`, never
        # `fail`, and never a placeholder owner.
        "dimensions": declared_absences(),
    }
