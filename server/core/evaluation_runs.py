"""Stories 51.2 and 51.3 -- the Evaluation Run, its pins, its verdicts and its gate.

WHAT THIS OWNS. The evidence layer of the Test workspace: the immutable
Evaluation Run and the environment it pins, the per-question case, the six
independent dimension verdicts, the explicitly approved baseline, the paired
comparison and the Gate Decision. Migration `153_product_evaluation_evidence.sql`
is the schema authority; every table and column name used here comes from it.

WHAT IT REFUSES TO BE.

  * It is not a score. There is no aggregate column in the schema and no
    aggregate field in any payload this module returns. `analyze-and-test.md`
    (`:336-337`) forbids an overall percentage that hides a critical failure or
    pays for a correctness regression with better feedback. Counts per
    (dimension, verdict) with their denominator are reported; a ratio never is.
  * It is not an owner. Test writes evaluation evidence and a Gate Decision. No
    statement in this module inserts into or updates a Governance, Context Hub
    or Data table -- the owning workflow reads the decision before its own
    transition (`analyze-and-test.md:368-371`).
  * It is not the Epic 14 benchmark. `app.eval_runs` is one compensating
    `precision_pct` and `app.eval_benchmark_questions` (renamed by migration
    153) is a flat question row. Neither is read, wrapped or aliased here, and
    `server/tests/evals/` stays test code no runtime module imports.

THREE PINS ARE DECLARED AND LEFT UNSET, because their owners are not delivered.

  * the rendered artifact (Stories 50.4 / 50.5 / 50.7 -- the render stack is not
    even installed);
  * the datum / mark inside it (Story 50.4);
  * evaluated MCP App behaviour (Story 50.6).

The rule, stated once: a missing pin never becomes a placeholder value, a
sentinel row, an empty object or a default. It becomes an entry in
`unresolved_pins` naming its reason code and its owner, and the verdict
`unverifiable` -- never `pass`, never `fail`. `mcp_app_behavior` is therefore
`unverifiable / render_owner_not_delivered` on every case written today, and
this module refuses to write `pass` on it while the pin is NULL.

IMMUTABILITY LIVES IN THE DATABASE. Migration 153 refuses `UPDATE`, `DELETE` and
`TRUNCATE` on every evidence table, allows exactly `recording -> finalized` on a
run, and allows exactly one baseline mutation: recording its successor. Nothing
below is the only rampart; the checks here exist so a caller gets a named
refusal instead of a constraint violation.

SCOPE IS PART OF EVERY LOOKUP. Each statement filters on `org_id` and
`project_id`, so a foreign identity does not resolve rather than resolving and
being rejected afterwards. Foreign, denied and absent are externally
indistinguishable (`EvaluationNotFound` for all three).
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ulid import ULID

from core.ai_paths import NO_AI_PATH, ai_path_reference
from core.expected_ai_path import (
    REASON_PATH_UNAVAILABLE,
    evaluate_case_path,
    load_expected_pattern,
)
from core.governance_rule_sets import canonical_json, content_hash

# ---------------------------------------------------------------------------
# Vocabularies. Each one is the ratified list, copied from the document line it
# comes from, so a seventh dimension or a fifth verdict cannot appear by habit.
# ---------------------------------------------------------------------------

#: `analyze-and-test.md:212-216`. `user_feedback` is deliberately absent: an
#: annotation is a judgement on an observation, never a run.
EVIDENCE_MODES = ("offline", "observed_cohort")

#: `analyze-and-test.md:239`.
RESULT_TYPES = (
    "scalar",
    "series",
    "breakdown",
    "comparison",
    "table",
    "narrative",
    "refusal",
)

#: The six rows of `analyze-and-test.md:319-326`, in document order.
DIMENSIONS = (
    "semantic_correctness",
    "provenance_correctness",
    "context_adherence",
    "path_quality",
    "dq_handling",
    "mcp_app_behavior",
)

#: `analyze-and-test.md:282-283`. `unverifiable` is not a soft `fail` and
#: `not_applicable` is not a silent `pass`.
VERDICTS = ("pass", "fail", "unverifiable", "not_applicable")
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNVERIFIABLE = "unverifiable"
VERDICT_NOT_APPLICABLE = "not_applicable"

#: The owner_workspace vocabulary of `app.ai_path_steps` (migration 150), reused
#: verbatim by `app.evaluation_context_version_set_entries`.
OWNER_WORKSPACES = ("data", "governance", "analyze", "context-hub", "test")

#: Pin families. The seven of `analyze-and-test.md:223-231`, with the composite
#: `Model / Host / Tool Catalog` row split into its three independently
#: comparable halves, plus the two the baseline section requires to be held
#: constant (`:341-343`): the question set and the data snapshot.
PIN_QUESTION_SET = "question_set"
PIN_SEMANTIC_VIEW = "semantic_view"
PIN_SKILL = "skill"
PIN_CONTEXT_VERSION_SET = "context_version_set"
PIN_MODEL = "model"
PIN_HOST = "host"
PIN_TOOL_CATALOG = "tool_catalog"
PIN_DATA_SNAPSHOT = "data_snapshot"
PIN_RESULT = "result"
PIN_AI_PATH = "ai_path"
PIN_RENDER = "render"

#: The families a run fingerprints, and therefore the only ones a comparison can
#: hold constant or declare changed. `result` and `ai_path` are per-case
#: observations, not environment: they are expected to differ between two runs
#: and comparing their fingerprints would refuse every comparison.
FINGERPRINTED_PIN_FAMILIES = (
    PIN_QUESTION_SET,
    PIN_SEMANTIC_VIEW,
    PIN_SKILL,
    PIN_CONTEXT_VERSION_SET,
    PIN_MODEL,
    PIN_HOST,
    PIN_TOOL_CATALOG,
    PIN_DATA_SNAPSHOT,
)

#: `analyze-and-test.md:342-343`: a model, host or tool-catalog qualification is
#: a separate comparison and its results are never pooled with a semantic or
#: context one. This mapping is what makes "never pooled" a refusal rather than
#: a convention.
COMPARISON_KINDS = ("semantic", "context", "model", "host", "tool_catalog")
_KIND_DECLARABLE_FAMILIES: dict[str, frozenset[str]] = {
    "semantic": frozenset({PIN_SEMANTIC_VIEW}),
    # A Skill has no table in this repository: it is pinned as a Context Version
    # Set entry. The two are fingerprinted separately so a Skill revision and a
    # knowledge revision are distinguishable, and a context comparison may
    # legitimately declare either or both.
    "context": frozenset({PIN_CONTEXT_VERSION_SET, PIN_SKILL}),
    "model": frozenset({PIN_MODEL}),
    "host": frozenset({PIN_HOST}),
    "tool_catalog": frozenset({PIN_TOOL_CATALOG}),
}

GATE_DECISIONS = ("pass", "block", "unverifiable")
GATE_CANDIDATE_WORKSPACES = ("governance", "context-hub")

#: The exact owners of the three absences, named rather than deferred. A reason
#: code with no owner is the defect that had Story 49.6 rejected.
# Until 2026-07-31 the rendered artifact did not exist at all, and this named the
# story that would deliver it. Stories 50.4 and 50.5 ARE delivered (`app.renders`,
# `app.renderer_runtime_builds`), so an absence today is no longer about a missing
# owner -- it is about THIS subject not having a rendered artifact pinned. Keeping
# the old wording would have made every run assert something false about us.
RENDER_OWNER = "the caller: pass render_ref / render_runtime_version when the subject has one"
# Story 50.6 IS delivered (1e24348): it enforces the data/render tool split as a
# BOOT-TIME catalog invariant. That is not what this dimension judges -- it judges
# a per-execution interaction, and no table records one. Naming 50.6 as the owner
# sent a reader to a story that is already closed.
MCP_APP_OWNER = (
    "the story that emits observed MCP App interaction evidence "
    "(drill-down resolved, host fallback taken, state actually shown); "
    "the tool split of 50.6 is delivered and is a different object"
)
AI_PATH_INSTRUMENTATION_OWNER = "story 49.6 (server-owned observed AI Path instrumentation)"
DIMENSION_EVALUATOR_OWNER = "story 51.3 (objective dimension evaluators)"

# Kept: rows written before 2026-07-31 carry it, and immutable evidence is never
# rewritten to look better in hindsight. New rows use REASON_RENDER_NOT_PINNED.
REASON_RENDER_OWNER_NOT_DELIVERED = "render_owner_not_delivered"
REASON_RENDER_NOT_PINNED = "render_not_pinned_for_this_subject"
REASON_PATH_EVIDENCE_MISSING = "path_evidence_missing"
REASON_NO_AI_PATH_DECLARED = "no_ai_path_declared"
REASON_PATH_COMPARISON_NOT_DELIVERED = "path_comparison_not_delivered"
REASON_ADHERENCE_EVIDENCE_MISSING = "adherence_evidence_missing"
REASON_DIMENSION_EVALUATOR_NOT_DELIVERED = "dimension_evaluator_not_delivered"

#: `ck_evaluation_case_verdicts_reason` in migration 153.
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")
_CAPABILITY_KEY = re.compile(r"^[a-z][a-z0-9_.-]{1,120}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_TYPE = re.compile(r"^[a-z][a-z0-9-]{2,60}$")

#: `app.is_exact_version_pin` in migration 153, mirrored so a caller gets a named
#: refusal instead of a constraint violation. The database remains the authority.
_FORBIDDEN_VERSION_PINS = frozenset({"latest", "current", "head"})


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class EvaluationNotFound(LookupError):
    """The run, case, baseline, comparison or decision is not in this Project.

    Raised identically for foreign, denied and nonexistent identities so a
    response body never becomes an enumeration oracle. The distinguishing reason
    belongs in audit, not in an answer.
    """


@dataclass(frozen=True)
class Refusal:
    """One named, actionable reason a write was not accepted."""

    code: str
    message: str
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "subject": self.subject}


class EvaluationRefused(ValueError):
    """A structured refusal carrying every reason, not only the first one."""

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


def _refuse(code: str, message: str, subject: str | None = None) -> None:
    raise EvaluationRefused(code, message, [Refusal(code, message, subject)])


# ---------------------------------------------------------------------------
# Pin validation.
# ---------------------------------------------------------------------------


def is_exact_version_pin(candidate: Any) -> bool:
    """True only for an exact stored version identity.

    Refuses `None`, blank, `latest`, `current` and `head` in every letter case --
    the Python counterpart of `app.is_exact_version_pin`. The console encodes the
    same refusal for the Explore address (`navigation.ts` `forbiddenValues`).
    """
    if not isinstance(candidate, str):
        return False
    normalized = candidate.strip().lower()
    return bool(normalized) and normalized not in _FORBIDDEN_VERSION_PINS


def _require_exact_pin(value: Any, subject: str) -> str:
    if not is_exact_version_pin(value):
        _refuse(
            "unpinned_version",
            f"`{subject}` must name an exact version; `latest`, `current`, `head` "
            "and a blank value are not version pins",
            subject,
        )
    return str(value).strip()


def _require_text(value: Any, subject: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        _refuse("missing_field", f"`{subject}` is required", subject)
    text = value.strip()
    if len(text) > maximum:
        _refuse("too_long", f"`{subject}` may not exceed {maximum} characters", subject)
    return text


def _require_object(value: Any, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse("invalid_shape", f"`{subject}` must be an object", subject)
    return dict(value)


def _require_choice(value: Any, allowed: tuple[str, ...], subject: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _refuse(
            "unknown_value",
            f"`{subject}` must be one of {', '.join(allowed)}",
            subject,
        )
    return value


def _require_sha256(value: Any, subject: str) -> str:
    text = value if isinstance(value, str) else ""
    if not _SHA256.match(text):
        _refuse("invalid_hash", f"`{subject}` must be a sha256 hex digest", subject)
    return text


def _require_reason_code(value: Any, subject: str) -> str:
    text = value if isinstance(value, str) else ""
    if not _REASON_CODE.match(text):
        _refuse(
            "invalid_reason_code",
            f"`{subject}` must be a machine-readable snake_case reason code",
            subject,
        )
    return text


def _require_date(value: Any, subject: str) -> _dt.date:
    """`as_of` is a DATE. A run whose `as_of` carried an hour would compare
    against a baseline that did not, so a timestamp is refused rather than
    truncated -- truncating would silently answer a different question."""
    if isinstance(value, _dt.datetime):
        _refuse("invalid_grain", f"`{subject}` is a date, not a timestamp", subject)
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    _refuse("invalid_date", f"`{subject}` must be an ISO date (YYYY-MM-DD)", subject)
    raise AssertionError("unreachable")  # pragma: no cover


_MODULES_DIR = Path(__file__).resolve().parents[1] / "modules"


@lru_cache(maxsize=1)
def connector_names() -> frozenset[str]:
    """The connector names this repository ships, normalized.

    `analyze-and-test.md:237-238`: a capability is the governed product or tool
    capability exercised, **not a connector name**. The database can only check
    the shape of `capability_key`, so the refusal lives here -- and it reads the
    live connector set rather than a hand-kept list that would go stale the day
    a connector is added.
    """
    try:
        entries = [p.name for p in _MODULES_DIR.iterdir() if p.is_dir()]
    except OSError:  # pragma: no cover - a packaging accident, not a caller error
        return frozenset()
    return frozenset(
        _normalize_capability(name) for name in entries if not name.startswith((".", "_"))
    )


def _normalize_capability(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _require_capability_key(value: Any, allowed_tags: tuple[str, ...] | None = None) -> str:
    key = _require_text(value, "capability_key", maximum=120)
    if not _CAPABILITY_KEY.match(key):
        _refuse(
            "invalid_capability",
            "`capability_key` must be lowercase and may contain letters, digits, "
            "`_`, `.` and `-`",
            "capability_key",
        )
    if _normalize_capability(key) in connector_names():
        _refuse(
            "capability_is_a_connector",
            f"`{key}` is a connector name, not a governed capability "
            "(analyze-and-test.md:237-238)",
            "capability_key",
        )
    if allowed_tags is not None and key not in allowed_tags:
        # The classification axis belongs to the governed Golden Question
        # version. Letting a run invent its own label would make evidence
        # aggregate under a name no governed object carries.
        _refuse(
            "capability_not_declared",
            f"`{key}` is not one of the capability tags declared by this Golden "
            "Question version",
            "capability_key",
        )
    return key


def _unresolved_pin(family: str, reason_code: str, owner: str, detail: str) -> dict[str, str]:
    """One honest absence: which pin, why, and who will resolve it.

    `owner` names a real story that owns the missing object. A disposition with
    no named owner -- the `future_owner` shape -- is what had Story 49.6
    rejected, and is deliberately impossible to produce here.
    """
    return {
        "pin_family": family,
        "reason_code": reason_code,
        "owner": owner,
        "detail": detail,
    }


def render_unresolved_pin() -> dict[str, str]:
    """The absence recorded when THIS subject has no rendered artifact pinned.

    It is no longer the absence every run carries: migration 166 made the pin
    fillable once Stories 50.4/50.5 delivered `app.renders` and
    `app.renderer_runtime_builds`. A run that pins one records nothing here.
    """
    return _unresolved_pin(
        PIN_RENDER,
        REASON_RENDER_NOT_PINNED,
        RENDER_OWNER,
        "no rendered artifact was pinned for this subject, so no render-dependent "
        "dimension can pass",
    )


# ---------------------------------------------------------------------------
# Run profiles: the named profile a baseline belongs to.
# ---------------------------------------------------------------------------


def create_run_profile(
    conn,
    *,
    org_id: str,
    project_id: str,
    name: str,
    evidence_mode: str,
    actor: str,
    description: str = "",
) -> dict[str, Any]:
    """Create the named profile. Its evidence mode can never be changed later
    (migration 153 refuses the reclassification), because every run and every
    baseline already approved under it was judged as one kind of evidence."""
    profile_name = _require_text(name, "name")
    mode = _require_choice(evidence_mode, EVIDENCE_MODES, "evidence_mode")
    profile_id = f"erp_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_run_profiles
                (id, org_id, project_id, name, description, evidence_mode, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, name, evidence_mode, created_at
            """,
            (
                profile_id,
                org_id,
                project_id,
                profile_name,
                str(description or ""),
                mode,
                _require_text(actor, "actor"),
            ),
        )
        row = cur.fetchone()
    return {
        "id": row[0],
        "name": row[1],
        "evidence_mode": row[2],
        "created_at": _iso(row[3]),
    }


def list_run_profiles(conn, *, org_id: str, project_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, description, evidence_mode, created_at
              FROM app.evaluation_run_profiles
             WHERE org_id = %s AND project_id = %s
             ORDER BY evidence_mode, name
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "evidence_mode": r[3],
            "created_at": _iso(r[4]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Context Version Sets: the resolved context, frozen, and never an unqualified
# `latest`.
# ---------------------------------------------------------------------------


def create_context_version_set(
    conn,
    *,
    org_id: str,
    project_id: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Freeze one exact context resolution.

    Every entry names an owner object and an EXACT version. A Skill is an entry
    with `owner_object_type = 'skill'`; when the caller cannot supply its exact
    version that is an unresolved pin on the run, never a `latest` here.
    """
    if not isinstance(entries, list) or not entries:
        _refuse("empty_context_set", "a Context Version Set needs at least one entry", "entries")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(entries):
        subject = f"entries[{index}]"
        entry = _require_object(raw, subject)
        workspace = _require_choice(entry.get("owner_workspace"), OWNER_WORKSPACES, subject)
        object_type = _require_text(entry.get("owner_object_type"), subject, maximum=60)
        if not _OBJECT_TYPE.match(object_type):
            _refuse(
                "invalid_object_type",
                f"`{object_type}` is not a canonical object type slug",
                subject,
            )
        object_id = _require_text(entry.get("owner_object_id"), subject)
        version_id = _require_exact_pin(entry.get("owner_version_id"), subject)
        key = (object_type, object_id)
        if key in seen:
            _refuse(
                "duplicate_entry",
                f"`{object_type}` `{object_id}` is pinned twice in one Context Version Set",
                subject,
            )
        seen.add(key)
        normalized.append(
            {
                "owner_workspace": workspace,
                "owner_object_type": object_type,
                "owner_object_id": object_id,
                "owner_version_id": version_id,
            }
        )

    normalized.sort(key=lambda e: (e["owner_object_type"], e["owner_object_id"]))
    set_hash = content_hash(normalized)
    set_id = f"ecvs_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_context_version_sets
                (id, org_id, project_id, content_hash)
            VALUES (%s, %s, %s, %s)
            """,
            (set_id, org_id, project_id, set_hash),
        )
        for ordinal, entry in enumerate(normalized):
            cur.execute(
                """
                INSERT INTO app.evaluation_context_version_set_entries
                    (id, set_id, org_id, project_id, ordinal, owner_workspace,
                     owner_object_type, owner_object_id, owner_version_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"ecve_{ULID()}",
                    set_id,
                    org_id,
                    project_id,
                    ordinal,
                    entry["owner_workspace"],
                    entry["owner_object_type"],
                    entry["owner_object_id"],
                    entry["owner_version_id"],
                ),
            )
    return {"id": set_id, "content_hash": set_hash, "entries": normalized}


def _load_context_entries(conn, *, set_id: str, org_id: str, project_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ordinal, owner_workspace, owner_object_type, owner_object_id,
                   owner_version_id
              FROM app.evaluation_context_version_set_entries
             WHERE set_id = %s AND org_id = %s AND project_id = %s
             ORDER BY ordinal
            """,
            (set_id, org_id, project_id),
        )
        rows = cur.fetchall()
    return [
        {
            "ordinal": r[0],
            "owner_workspace": r[1],
            "owner_object_type": r[2],
            "owner_object_id": r[3],
            "owner_version_id": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# The Evaluation Run.
# ---------------------------------------------------------------------------

_RUN_COLUMNS = (
    "id",
    "run_profile_id",
    "lifecycle",
    "evidence_mode",
    "question_set_fingerprint",
    "semantic_view_id",
    "semantic_view_version_id",
    "context_version_set_id",
    "model_ref",
    "host_capability_profile",
    "tool_catalog_version",
    "data_snapshot_ref",
    "data_snapshot_hash",
    "as_of",
    "render_runtime_version",
    "observed_cohort_id",
    "unresolved_pins",
    "pin_fingerprints",
    "started_at",
    "ended_at",
    "created_by",
    "content_hash",
)


def open_evaluation_run(
    conn,
    *,
    org_id: str,
    project_id: str,
    run_profile_id: str,
    semantic_view_id: str,
    semantic_view_version_id: str,
    context_version_set_id: str,
    model_ref: str,
    host_capability_profile: dict[str, Any],
    tool_catalog_version: str,
    data_snapshot_ref: dict[str, Any],
    as_of: Any,
    actor: str,
    observed_cohort_id: str | None = None,
    render_runtime_version: str | None = None,
) -> dict[str, Any]:
    """Open a `recording` run with its environment pinned.

    `tool_catalog_version` is the sha256 `catalog_version` that
    `skill_tool_catalog.list_skill_tool_catalog()` already returns. It is
    resolved by the caller from the live catalog and never minted here: a second
    numbering scheme for one catalog is how two runs stop being comparable.

    The evidence mode is read from the profile rather than accepted from the
    request. A run whose mode disagreed with its profile would reclassify every
    comparison the profile carries.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT evidence_mode FROM app.evaluation_run_profiles
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (run_profile_id, org_id, project_id),
        )
        profile = cur.fetchone()
    if profile is None:
        raise EvaluationNotFound("run profile not found in this Project")
    evidence_mode = str(profile[0])

    cohort_id = observed_cohort_id or None
    if evidence_mode == "observed_cohort" and not cohort_id:
        _refuse(
            "missing_cohort",
            "an observed-cohort run names the exact frozen cohort it describes",
            "observed_cohort_id",
        )
    if evidence_mode == "offline" and cohort_id:
        _refuse(
            "cohort_on_offline_run",
            "an offline run is reproducible from its own pins and names no cohort",
            "observed_cohort_id",
        )

    snapshot_ref = _require_object(data_snapshot_ref, "data_snapshot_ref")
    if not snapshot_ref:
        # An empty reference pins nothing. It would make two runs over different
        # data carry the same snapshot fingerprint and compare as equal.
        _refuse(
            "empty_data_snapshot",
            "`data_snapshot_ref` must name the exact data snapshot or seed set used",
            "data_snapshot_ref",
        )
    host_profile = _require_object(host_capability_profile, "host_capability_profile")

    # Migration 166 made the rendered-artifact pin fillable, so the absence is
    # recorded only when the caller has nothing to pin. Recording it regardless --
    # which is what this did while the object did not exist -- would now make every
    # run assert an absence that is no longer true.
    opened_pins: list[dict[str, str]] = [] if render_runtime_version else [render_unresolved_pin()]

    run_id = f"erun_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_runs
                (id, org_id, project_id, run_profile_id, lifecycle, evidence_mode,
                 semantic_view_id, semantic_view_version_id, context_version_set_id,
                 model_ref, host_capability_profile, tool_catalog_version,
                 data_snapshot_ref, data_snapshot_hash, as_of, observed_cohort_id,
                 render_runtime_version, unresolved_pins, created_by)
            VALUES (%s, %s, %s, %s, 'recording', %s, %s, %s, %s, %s, %s::jsonb, %s,
                    %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id, lifecycle, evidence_mode, started_at
            """,
            (
                run_id,
                org_id,
                project_id,
                run_profile_id,
                evidence_mode,
                _require_text(semantic_view_id, "semantic_view_id"),
                _require_exact_pin(semantic_view_version_id, "semantic_view_version_id"),
                _require_text(context_version_set_id, "context_version_set_id"),
                _require_exact_pin(model_ref, "model_ref"),
                canonical_json(host_profile),
                _require_sha256(tool_catalog_version, "tool_catalog_version"),
                canonical_json(snapshot_ref),
                content_hash(snapshot_ref),
                _require_date(as_of, "as_of"),
                cohort_id,
                render_runtime_version,
                canonical_json(opened_pins),
                _require_text(actor, "actor"),
            ),
        )
        row = cur.fetchone()
    return {
        "id": row[0],
        "lifecycle": row[1],
        "evidence_mode": row[2],
        "started_at": _iso(row[3]),
        "unresolved_pins": opened_pins,
    }


def _load_run(conn, *, run_id: str, org_id: str, project_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_RUN_COLUMNS)}
              FROM app.evaluation_runs
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            # The interpolated fragment is a module constant, never caller input;
            # every value below still travels as a bound parameter.
            (run_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise EvaluationNotFound("evaluation run not found in this Project")
    return dict(zip(_RUN_COLUMNS, row, strict=False))


def add_run_case(
    conn,
    *,
    org_id: str,
    project_id: str,
    run_id: str,
    golden_question_version_id: str,
    result_id: str | None = None,
    ai_path_id: str | None = None,
    ai_path_expected: bool = True,
    capability_key: str | None = None,
    render_ref: str | None = None,
) -> dict[str, Any]:
    """Pin one question's subject inside a recording run.

    The classification axes are READ from the governed Golden Question version,
    never accepted from the request: a case that carried its own Business Domain
    or result type would let evidence aggregate under a classification no
    governed object declares.

    The AI Path pin has three honest states, one more than a Result has:

      * an exact finalized path;
      * the exact literal `No AI path`, when the question declares no AI
        involvement;
      * neither -- the path evidence was expected and is missing. That is an
        unresolved pin and `Unverifiable`, and collapsing it into `No AI path`
        would assert that no AI was involved, which is a different claim.
    """
    run = _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    if run["lifecycle"] != "recording":
        _refuse(
            "run_is_finalized",
            "a finalized Evaluation Run is immutable evidence and accepts no case",
            run_id,
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT business_domain_id, business_domain_version_number, result_type,
                   capability_tags
              FROM app.golden_question_versions
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (golden_question_version_id, org_id, project_id),
        )
        version = cur.fetchone()
    if version is None:
        raise EvaluationNotFound("golden question version not found in this Project")
    domain_id, domain_version, result_type, capability_tags = version
    tags = tuple(capability_tags or ())
    if not tags:
        _refuse(
            "question_without_capability",
            "this Golden Question version declares no capability tag to classify a case by",
            golden_question_version_id,
        )
    key = _require_capability_key(capability_key or sorted(tags)[0], tags)
    _require_choice(result_type, RESULT_TYPES, "result_type")

    # Same rule as the run: since migration 166 the pin is fillable, so the absence
    # is recorded only when this case genuinely has no rendered artifact.
    unresolved: list[dict[str, str]] = [] if render_ref else [render_unresolved_pin()]
    resolved_path: str | None = None
    absent_literal: str | None = None
    if ai_path_id:
        # `ai_path_reference` refuses a still-recording path: a case pinned to
        # one would describe evidence that can still grow after the fact.
        reference = ai_path_reference(conn, path_id=ai_path_id, project_id=project_id)
        resolved_path = None if reference == NO_AI_PATH else reference
        absent_literal = NO_AI_PATH if reference == NO_AI_PATH else None
    elif not ai_path_expected:
        absent_literal = NO_AI_PATH
    else:
        unresolved.append(
            _unresolved_pin(
                PIN_AI_PATH,
                REASON_PATH_EVIDENCE_MISSING,
                AI_PATH_INSTRUMENTATION_OWNER,
                "the question expects a governed AI Path and no finalized observed "
                "path was recorded for this execution",
            )
        )
    if result_id is None:
        unresolved.append(
            _unresolved_pin(
                PIN_RESULT,
                "result_evidence_missing",
                "story 50.1 (immutable Result)",
                "no immutable Result was pinned for this case",
            )
        )

    result_classification_hash: str | None = None
    if result_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.manifest
                  FROM app.query_results r
                  JOIN app.query_result_payloads p
                    ON p.result_id = r.id AND p.org_id = r.org_id
                   AND p.project_id = r.project_id AND p.content_hash = r.content_hash
                 WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
                """,
                (result_id, org_id, project_id),
            )
            result_owner = cur.fetchone()
        if result_owner is not None:
            from core.feedback_review import (  # noqa: PLC0415
                result_classification_hash_from_manifest,
            )

            result_classification_hash = result_classification_hash_from_manifest(
                result_owner[0]
            )

    case_id = f"ecase_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_run_cases
                (id, run_id, org_id, project_id, golden_question_version_id, result_id,
                 ai_path_id, ai_path_absent_literal, business_domain_id,
                 business_domain_version_number, capability_key, result_type,
                  render_ref, unresolved_pins, result_classification_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id, created_at
            """,
            (
                case_id,
                run_id,
                org_id,
                project_id,
                golden_question_version_id,
                result_id,
                resolved_path,
                absent_literal,
                domain_id,
                domain_version,
                key,
                result_type,
                render_ref,
                canonical_json(unresolved),
                result_classification_hash,
            ),
        )
        row = cur.fetchone()
    return {
        "id": row[0],
        "run_id": run_id,
        "golden_question_version_id": golden_question_version_id,
        "result_id": result_id,
        "ai_path": resolved_path or absent_literal,
        "business_domain_id": domain_id,
        "business_domain_version_number": domain_version,
        "capability_key": key,
        "result_type": result_type,
        "result_classification_hash": result_classification_hash,
        "unresolved_pins": unresolved,
        "created_at": _iso(row[1]),
    }


def _load_cases(conn, *, run_id: str, org_id: str, project_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.golden_question_version_id, c.result_id, c.ai_path_id,
                   c.ai_path_absent_literal, c.business_domain_id,
                   c.business_domain_version_number, c.capability_key, c.result_type,
                   c.unresolved_pins, c.render_ref, promotion.feedback_id, promotion.id
              FROM app.evaluation_run_cases c
              LEFT JOIN app.query_results qr
                ON qr.id = c.result_id AND qr.org_id = c.org_id
               AND qr.project_id = c.project_id
              LEFT JOIN app.feedback_regression_cases promotion
                ON promotion.golden_question_version_id = c.golden_question_version_id
               AND promotion.result_id = c.result_id
               AND promotion.result_content_hash = qr.content_hash
               AND promotion.result_classification_hash = c.result_classification_hash
               AND promotion.org_id = c.org_id AND promotion.project_id = c.project_id
             WHERE c.run_id = %s AND c.org_id = %s AND c.project_id = %s
             ORDER BY c.created_at, c.id
            """,
            (run_id, org_id, project_id),
        )
        rows = cur.fetchall()
    names = (
        "id",
        "golden_question_version_id",
        "result_id",
        "ai_path_id",
        "ai_path_absent_literal",
        "business_domain_id",
        "business_domain_version_number",
        "capability_key",
        "result_type",
        "unresolved_pins",
        "render_ref",
        "feedback_id",
        "regression_case_id",
    )
    return [dict(zip(names, row, strict=False)) for row in rows]


def _pin_fingerprints(run: dict[str, Any], *, questions: list[str], context: list[dict]) -> dict:
    """One fingerprint per comparable pin family.

    This is the material AC7 compares. A family whose pin could not be resolved
    is ABSENT from the mapping rather than hashed as an empty value: hashing
    "nothing" would make two runs that both failed to resolve a family compare
    as identical, which is the mixed-pin comparison the contract forbids.
    """
    skills = [e for e in context if e["owner_object_type"] == "skill"]
    others = [e for e in context if e["owner_object_type"] != "skill"]

    def _entry_material(rows: list[dict]) -> list[list[str]]:
        return sorted(
            [e["owner_workspace"], e["owner_object_type"], e["owner_object_id"],
             e["owner_version_id"]]
            for e in rows
        )

    fingerprints = {
        PIN_QUESTION_SET: content_hash(sorted(questions)),
        PIN_SEMANTIC_VIEW: content_hash(
            [run["semantic_view_id"], run["semantic_view_version_id"]]
        ),
        PIN_SKILL: content_hash(_entry_material(skills)),
        PIN_CONTEXT_VERSION_SET: content_hash(_entry_material(others)),
        PIN_MODEL: content_hash([run["model_ref"]]),
        PIN_HOST: content_hash(run["host_capability_profile"] or {}),
        PIN_TOOL_CATALOG: content_hash([run["tool_catalog_version"]]),
        PIN_DATA_SNAPSHOT: content_hash([run["data_snapshot_hash"]]),
    }
    return fingerprints


def finalize_evaluation_run(
    conn, *, org_id: str, project_id: str, run_id: str
) -> dict[str, Any]:
    """Freeze the run: `recording -> finalized`, and nothing else, ever.

    Finalizing creates, moves and updates NO baseline. `analyze-and-test.md:346`
    is explicit -- a baseline is an explicitly approved run and is never updated
    automatically -- so there is deliberately no statement here that touches
    `app.evaluation_baselines`.

    A run may finalize only when every unresolvable pin has a recorded reason.
    Today that is at minimum the rendered artifact, whose owner is not
    delivered: while `render_runtime_version` is NULL the database refuses a
    finalization whose `unresolved_pins` carries no `pin_family=render` entry.
    An empty list is not forbidden in itself -- it is the honest state once
    stories 50.4/50.5/50.7 land a Render and every pin resolves.
    """
    run = _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    if run["lifecycle"] == "finalized":
        _refuse("already_finalized", "this Evaluation Run is already frozen", run_id)

    cases = _load_cases(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    if not cases:
        _refuse(
            "empty_run",
            "an Evaluation Run with no case pins no subject and cannot be finalized",
            run_id,
        )
    context = _load_context_entries(
        conn, set_id=run["context_version_set_id"], org_id=org_id, project_id=project_id
    )

    questions = [c["golden_question_version_id"] for c in cases]
    question_set_fingerprint = content_hash(sorted(questions))
    fingerprints = _pin_fingerprints(run, questions=questions, context=context)

    unresolved = list(run["unresolved_pins"] or [])
    known = {(p.get("pin_family"), p.get("reason_code")) for p in unresolved}
    for case in cases:
        for pin in case["unresolved_pins"] or []:
            key = (pin.get("pin_family"), pin.get("reason_code"))
            if key not in known:
                known.add(key)
                unresolved.append(dict(pin))
    # The database refuses a finalization whose render pin is neither filled nor
    # recorded (migration 153, corrected by 157). Mirror that rule rather than
    # forcing the absence unconditionally: a run that pinned a renderer runtime has
    # nothing to record, and appending the absence anyway would make the guard
    # unsatisfiable in the direction it was corrected to allow.
    if not run.get("render_runtime_version") and not any(
        p.get("pin_family") == PIN_RENDER for p in unresolved
    ):
        unresolved.append(render_unresolved_pin())

    frozen = {
        "run_id": run_id,
        "question_set_fingerprint": question_set_fingerprint,
        "pin_fingerprints": fingerprints,
        "unresolved_pins": unresolved,
        "case_count": len(cases),
    }
    run_hash = content_hash(frozen)

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.evaluation_runs
               SET lifecycle = 'finalized',
                   ended_at = NOW(),
                   question_set_fingerprint = %s,
                   pin_fingerprints = %s::jsonb,
                   unresolved_pins = %s::jsonb,
                   content_hash = %s
             WHERE id = %s AND org_id = %s AND project_id = %s
               AND lifecycle = 'recording'
            RETURNING id, lifecycle, ended_at, content_hash
            """,
            (
                question_set_fingerprint,
                canonical_json(fingerprints),
                canonical_json(unresolved),
                run_hash,
                run_id,
                org_id,
                project_id,
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise EvaluationNotFound("evaluation run not found in this Project")
    return {
        "id": row[0],
        "lifecycle": row[1],
        "ended_at": _iso(row[2]),
        "content_hash": row[3],
        "question_set_fingerprint": question_set_fingerprint,
        "pin_fingerprints": fingerprints,
        "unresolved_pins": unresolved,
    }


# ---------------------------------------------------------------------------
# The six dimension verdicts. Six rows per case or none.
# ---------------------------------------------------------------------------


def mechanical_case_verdicts(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The six verdicts that follow MECHANICALLY from what is pinned.

    Nothing here judges an answer. Each entry is either an absence with its exact
    reason and owner, or a `not_applicable` the pins themselves establish. The
    dimensions that need a real evaluator -- semantic correctness, provenance and
    DQ handling -- resolve to `unverifiable` naming the module that will supply
    them, because reporting `pass` on a dimension nobody measured would launder
    the gap.
    """
    verdicts: dict[str, dict[str, Any]] = {}

    for dimension in ("semantic_correctness", "provenance_correctness", "dq_handling"):
        verdicts[dimension] = {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": REASON_DIMENSION_EVALUATOR_NOT_DELIVERED,
            "evidence_refs": {"owner": DIMENSION_EVALUATOR_OWNER, "dimension": dimension},
        }

    if case.get("ai_path_id"):
        verdicts["path_quality"] = {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": REASON_PATH_COMPARISON_NOT_DELIVERED,
            "evidence_refs": {
                "owner": DIMENSION_EVALUATOR_OWNER,
                "observed_ai_path_id": case["ai_path_id"],
            },
        }
        verdicts["context_adherence"] = {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": REASON_PATH_COMPARISON_NOT_DELIVERED,
            "evidence_refs": {
                "owner": DIMENSION_EVALUATOR_OWNER,
                "observed_ai_path_id": case["ai_path_id"],
            },
        }
    elif case.get("ai_path_absent_literal") == NO_AI_PATH:
        verdicts["path_quality"] = {
            "verdict": VERDICT_NOT_APPLICABLE,
            "reason_code": REASON_NO_AI_PATH_DECLARED,
            "evidence_refs": {"ai_path": NO_AI_PATH},
        }
        verdicts["context_adherence"] = {
            "verdict": VERDICT_NOT_APPLICABLE,
            "reason_code": REASON_NO_AI_PATH_DECLARED,
            "evidence_refs": {"ai_path": NO_AI_PATH},
        }
    else:
        verdicts["path_quality"] = {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": REASON_PATH_EVIDENCE_MISSING,
            "evidence_refs": {"owner": AI_PATH_INSTRUMENTATION_OWNER},
        }
        # `analyze-and-test.md:284`: a bare adherence flag without server-owned
        # path evidence is `Unverifiable`. `core.adherence` records a boolean
        # gate that names no Skill version and swallows its own sink failures,
        # so it cannot carry this dimension on its own.
        verdicts["context_adherence"] = {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": REASON_ADHERENCE_EVIDENCE_MISSING,
            "evidence_refs": {"owner": AI_PATH_INSTRUMENTATION_OWNER},
        }

    # Since migration 166 this dimension no longer waits on the rendered artifact --
    # it exists. It waits on Story 50.6, which delivers the MCP data/render tool
    # split it actually judges, and the database now says so with a CHECK of its own
    # (`ck_evaluation_case_verdicts_mcp_evaluator_absent`) instead of relying on a
    # render pin being unfillable.
    verdicts["mcp_app_behavior"] = {
        "verdict": VERDICT_UNVERIFIABLE,
        "reason_code": REASON_RENDER_NOT_PINNED,
        "evidence_refs": {"owner": RENDER_OWNER, "also_awaiting": MCP_APP_OWNER},
    }
    return verdicts


#: `evidence_state` of `app.evaluation_path_comparisons` (migration 153:1017).
#: `unavailable` is instrumentation that RAN and could not capture; `missing` is
#: no usable evidence at all. The database keeps them apart, so this does too.
_PATH_EVIDENCE_STATES = {"observed", "missing", "unavailable"}


def record_path_comparison(
    conn,
    *,
    org_id: str,
    project_id: str,
    case_id: str,
    ai_path_id: str | None,
    expected_pattern: Any,
    verdict: dict[str, Any],
) -> str | None:
    """Persist the expected-versus-observed comparison behind a `path_quality` verdict.

    THIS IS NOT BOOKKEEPING. `trg_evaluation_case_verdicts_path_evidence`
    (migration 153:1946-1968) refuses `path_quality = pass` unless a row here
    resolves an observed AI Path for the case. `evaluate_case_path` computed the
    comparison and nobody stored it, so the FIRST case whose observed path
    actually matched its expected pattern would have been refused by the database
    at the exact moment the loop finally worked -- the same shape of defect as the
    two dialects commit 2cb09041 closed, one table further down.

    One writer, one mapping. `feedback_regression` used to carry its own copy of
    this INSERT with its own hash rule; a second copy is how the stored evidence
    of one comparison starts meaning two things.

    Returns the row id, or None when there was no comparison to record.
    """
    comparison = (verdict.get("evidence_refs") or {}).get("comparison") or {}
    if not comparison:
        return None

    if comparison.get("comparable"):
        evidence_state = "observed"
        observed = ai_path_id
    else:
        absence = (comparison.get("missing_path_evidence") or [{}])[0]
        evidence_state = (
            "unavailable" if absence.get("reason_code") == REASON_PATH_UNAVAILABLE else "missing"
        )
        # `ck_evaluation_path_comparisons_state_matches_pin`: a state other than
        # `observed` may not pin a path. Which path could not be read still
        # travels, on the verdict's own evidence refs.
        observed = None

    comparison_id = f"epc_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_path_comparisons
                (id, case_id, org_id, project_id, observed_ai_path_id,
                 expected_pattern_hash, evidence_state, path_verdict,
                 required_missing, forbidden_present, order_violations,
                 version_mismatches, matched_alternative_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, NULL)
            ON CONFLICT (case_id) DO NOTHING
            """,
            (
                comparison_id,
                case_id,
                org_id,
                project_id,
                observed,
                content_hash(expected_pattern or {}),
                evidence_state,
                verdict["verdict"],
                # An unsatisfied alternative group is a required node that never
                # ran, seen from the branch that was supposed to supply it.
                canonical_json(
                    list(comparison.get("missing_required_nodes") or [])
                    + list(comparison.get("unsatisfied_alternatives") or [])
                ),
                canonical_json(comparison.get("observed_forbidden_nodes") or []),
                canonical_json(comparison.get("order_violations") or []),
                canonical_json(comparison.get("version_mismatches") or []),
            ),
        )
    # `matched_alternative_key` stays NULL: `compare()` reports which alternative
    # groups were NOT satisfied and does not name the branch that was. Writing a
    # guess there would make a column that is supposed to say WHICH branch passed
    # say something nobody measured.
    return comparison_id


def record_case_verdicts(
    conn,
    *,
    org_id: str,
    project_id: str,
    case_id: str,
    verdicts: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Write the six verdict rows for one case, atomically, or refuse.

    Six rows or none. A fifth, a seventh, or a supplied dimension outside the
    ratified six is refused rather than partially written: a case carrying four
    verdicts reads as three passes and one gap that nobody declared.

    Two verdicts are not negotiable, and the database enforces both as well:
    `mcp_app_behavior` cannot be `pass` while its pin is NULL, and `path_quality`
    cannot be `pass` without a resolved observed AI Path.

    No aggregate is computed here, or anywhere. The six stay six.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_id, ai_path_id, ai_path_absent_literal, render_ref,
                   golden_question_version_id
              FROM app.evaluation_run_cases
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (case_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise EvaluationNotFound("evaluation case not found in this Project")
    case = {
        "id": row[0],
        "run_id": row[1],
        "ai_path_id": row[2],
        "ai_path_absent_literal": row[3],
        "render_ref": row[4],
        "golden_question_version_id": row[5],
    }

    merged = mechanical_case_verdicts(case)

    # Story 51.3 AC7. The mechanical pass can only say "nobody compared this".
    # When both sides of the seam exist -- an expected pattern on the Golden
    # Question version, an observed path on the case -- `expected_ai_path` does
    # the real comparison and REPLACES that placeholder. It is called only when
    # an observed path is pinned: with none, the mechanical verdict is already
    # the right one (`unverifiable`, naming the instrumentation owner), and
    # asking the comparator would just rediscover the same absence.
    if case["ai_path_id"]:
        merged["path_quality"] = evaluate_case_path(
            conn, org_id=org_id, project_id=project_id, case=case
        )
        # The comparison the database asks for before it will accept a passing
        # path verdict. Written from the SAME evaluation the verdict came from,
        # never recomputed, so the row and the verdict cannot disagree.
        record_path_comparison(
            conn,
            org_id=org_id,
            project_id=project_id,
            case_id=case_id,
            ai_path_id=case["ai_path_id"],
            expected_pattern=load_expected_pattern(
                conn,
                org_id=org_id,
                project_id=project_id,
                golden_question_version_id=case["golden_question_version_id"],
            ),
            verdict=merged["path_quality"],
        )
    supplied = verdicts or {}
    if not isinstance(supplied, dict):
        _refuse("invalid_shape", "`verdicts` must be an object keyed by dimension", "verdicts")
    for dimension, payload in supplied.items():
        if dimension not in DIMENSIONS:
            _refuse(
                "unknown_dimension",
                f"`{dimension}` is not one of the six ratified dimensions",
                dimension,
            )
        entry = _require_object(payload, dimension)
        verdict = _require_choice(entry.get("verdict"), VERDICTS, f"{dimension}.verdict")
        reason = _require_reason_code(entry.get("reason_code"), f"{dimension}.reason_code")
        evidence = _require_object(entry.get("evidence_refs") or {}, f"{dimension}.evidence_refs")
        merged[dimension] = {
            "verdict": verdict,
            "reason_code": reason,
            "evidence_refs": evidence,
        }

    if merged["mcp_app_behavior"]["verdict"] == VERDICT_PASS and case["render_ref"] is None:
        _refuse(
            "mcp_app_behavior_without_render",
            "`mcp_app_behavior` cannot pass while no rendered artifact is pinned: "
            f"its owner is {RENDER_OWNER}",
            "mcp_app_behavior",
        )
    if merged["path_quality"]["verdict"] == VERDICT_PASS and not case["ai_path_id"]:
        _refuse(
            "path_quality_without_evidence",
            "`path_quality` cannot pass without a resolved observed AI Path: "
            "missing path evidence is unverifiable",
            "path_quality",
        )

    written: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for dimension in DIMENSIONS:
            entry = merged[dimension]
            cur.execute(
                """
                INSERT INTO app.evaluation_case_dimension_verdicts
                    (id, case_id, org_id, project_id, dimension, verdict, reason_code,
                     evidence_refs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    f"edv_{ULID()}",
                    case_id,
                    org_id,
                    project_id,
                    dimension,
                    entry["verdict"],
                    entry["reason_code"],
                    canonical_json(entry["evidence_refs"]),
                ),
            )
            written.append({"dimension": dimension, **entry})
    return written


def _load_verdicts(conn, *, run_id: str, org_id: str, project_id: str) -> dict[str, dict]:
    """`{case_id: {dimension: {verdict_id, verdict, reason_code, evidence_refs}}}`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.case_id, v.id, v.dimension, v.verdict, v.reason_code, v.evidence_refs
              FROM app.evaluation_case_dimension_verdicts v
              JOIN app.evaluation_run_cases c
                ON c.id = v.case_id AND c.org_id = v.org_id AND c.project_id = v.project_id
             WHERE c.run_id = %s AND v.org_id = %s AND v.project_id = %s
             ORDER BY v.case_id, v.dimension
            """,
            (run_id, org_id, project_id),
        )
        rows = cur.fetchall()
    out: dict[str, dict] = {}
    for case_id, verdict_id, dimension, verdict, reason, evidence in rows:
        out.setdefault(case_id, {})[dimension] = {
            "verdict_id": verdict_id,
            "verdict": verdict,
            "reason_code": reason,
            "evidence_refs": evidence,
        }
    return out


def verdict_counts(verdicts_by_case: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Counts per (dimension, verdict), with no ratio and no cross-dimension sum.

    This is the only tally this module produces. It states its denominator by
    listing every verdict value, which is what makes it readable; a percentage
    over it would be the compensating figure `analyze-and-test.md:336-337`
    forbids, and there is deliberately no helper here that computes one.
    """
    counts = {d: dict.fromkeys(VERDICTS, 0) for d in DIMENSIONS}
    for dimensions in verdicts_by_case.values():
        for dimension, entry in dimensions.items():
            if dimension in counts and entry["verdict"] in counts[dimension]:
                counts[dimension][entry["verdict"]] += 1
    return counts


# ---------------------------------------------------------------------------
# Baselines: an approval, not a pointer that moves.
# ---------------------------------------------------------------------------


def approve_baseline(
    conn,
    *,
    org_id: str,
    project_id: str,
    run_id: str,
    approved_by: str,
    approval_reason: str,
) -> dict[str, Any]:
    """Approve one finalized offline run as the baseline of its profile.

    There is exactly one way a baseline exists: this call. Replacing one INSERTS
    a new row and marks the previous one superseded -- the single mutation
    migration 153 permits -- so the approval that was made is still readable
    after it stops being current.
    """
    actor = _require_text(approved_by, "approved_by")
    reason = _require_text(approval_reason, "approval_reason", maximum=2000)

    run = _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    if run["lifecycle"] != "finalized":
        _refuse(
            "run_not_finalized",
            "a baseline may only approve a finalized Evaluation Run",
            run_id,
        )
    if run["evidence_mode"] != "offline":
        # `analyze-and-test.md:357-358`: observed cohorts use reference windows,
        # not deterministic baselines.
        _refuse(
            "cohort_is_not_a_baseline",
            "an observed cohort is a reference window and can never be approved as a baseline",
            run_id,
        )

    compared_versions = {
        "semantic_view_id": run["semantic_view_id"],
        "semantic_view_version_id": run["semantic_view_version_id"],
        "context_version_set_id": run["context_version_set_id"],
        "model_ref": run["model_ref"],
        "tool_catalog_version": run["tool_catalog_version"],
        "data_snapshot_hash": run["data_snapshot_hash"],
        "question_set_fingerprint": run["question_set_fingerprint"],
        "as_of": str(run["as_of"]),
    }

    baseline_id = f"ebl_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM app.evaluation_baselines
             WHERE run_profile_id = %s AND org_id = %s AND project_id = %s
               AND superseded_by_baseline_id IS NULL
            """,
            (run["run_profile_id"], org_id, project_id),
        )
        active = cur.fetchone()
        cur.execute(
            """
            INSERT INTO app.evaluation_baselines
                (id, org_id, project_id, run_profile_id, run_id, approved_by,
                 approval_reason, compared_versions)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id, approved_at
            """,
            (
                baseline_id,
                org_id,
                project_id,
                run["run_profile_id"],
                run_id,
                actor,
                reason,
                canonical_json(compared_versions),
            ),
        )
        row = cur.fetchone()
        superseded = None
        if active is not None:
            superseded = active[0]
            # The ONLY mutation migration 153 allows on a baseline. Every other
            # column of the previous approval stays exactly as it was written.
            cur.execute(
                """
                UPDATE app.evaluation_baselines
                   SET superseded_by_baseline_id = %s
                 WHERE id = %s AND org_id = %s AND project_id = %s
                   AND superseded_by_baseline_id IS NULL
                """,
                (baseline_id, superseded, org_id, project_id),
            )
    return {
        "id": row[0],
        "run_id": run_id,
        "run_profile_id": run["run_profile_id"],
        "approved_by": actor,
        "approval_reason": reason,
        "approved_at": _iso(row[1]),
        "compared_versions": compared_versions,
        "superseded_baseline_id": superseded,
    }


def active_baseline(conn, *, org_id: str, project_id: str, run_profile_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_id, approved_by, approval_reason, approved_at, compared_versions
              FROM app.evaluation_baselines
             WHERE run_profile_id = %s AND org_id = %s AND project_id = %s
               AND superseded_by_baseline_id IS NULL
            """,
            (run_profile_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "run_id": row[1],
        "approved_by": row[2],
        "approval_reason": row[3],
        "approved_at": _iso(row[4]),
        "compared_versions": row[5],
    }


# ---------------------------------------------------------------------------
# Comparisons: everything held constant but the declared candidate versions.
# ---------------------------------------------------------------------------


def compare_pin_families(
    baseline_fingerprints: dict[str, Any],
    candidate_fingerprints: dict[str, Any],
    baseline_unresolved: list[dict[str, Any]],
    candidate_unresolved: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Return `(drifted_families, unverifiable_families)`.

    A family missing from either run's fingerprints, or named in either run's
    unresolved pins, is UNVERIFIABLE -- not "unchanged". Treating an absence as
    equality is exactly how a comparison silently mixes unpinned versions.
    """
    unverifiable = {
        str(p.get("pin_family"))
        for p in list(baseline_unresolved or []) + list(candidate_unresolved or [])
        if p.get("pin_family")
    }
    for family in FINGERPRINTED_PIN_FAMILIES:
        if family not in (baseline_fingerprints or {}) or family not in (
            candidate_fingerprints or {}
        ):
            unverifiable.add(family)
    drifted = {
        family
        for family in FINGERPRINTED_PIN_FAMILIES
        if family not in unverifiable
        and baseline_fingerprints.get(family) != candidate_fingerprints.get(family)
    }
    return drifted, unverifiable


def create_comparison(
    conn,
    *,
    org_id: str,
    project_id: str,
    baseline_run_id: str,
    candidate_run_id: str,
    comparison_kind: str,
    changed_pin_families: list[str],
    actor: str,
) -> dict[str, Any]:
    """Refuse any comparison where something other than the declared families moved.

    `analyze-and-test.md:341-343`: a context or semantic comparison holds the
    question set, data, model, host and tool catalog constant while changing only
    the intended candidate versions; a model, host or tool-catalog qualification
    is a separate comparison and results are never pooled.
    """
    kind = _require_choice(comparison_kind, COMPARISON_KINDS, "comparison_kind")
    if baseline_run_id == candidate_run_id:
        _refuse(
            "same_run",
            "a run cannot be compared with itself",
            candidate_run_id,
        )
    declared = list(changed_pin_families or [])
    if not declared:
        _refuse(
            "no_declared_change",
            "a comparison must declare which pin families it changes",
            "changed_pin_families",
        )
    declared_set = set()
    for family in declared:
        declared_set.add(
            _require_choice(family, FINGERPRINTED_PIN_FAMILIES, "changed_pin_families")
        )
    allowed = _KIND_DECLARABLE_FAMILIES[kind]
    if not declared_set <= allowed:
        _refuse(
            "families_not_in_kind",
            f"a `{kind}` comparison may only declare {', '.join(sorted(allowed))}; "
            f"pooling {', '.join(sorted(declared_set - allowed))} into it would merge "
            "two qualifications that must stay separate",
            "changed_pin_families",
        )

    baseline = _load_run(conn, run_id=baseline_run_id, org_id=org_id, project_id=project_id)
    candidate = _load_run(conn, run_id=candidate_run_id, org_id=org_id, project_id=project_id)
    for run in (baseline, candidate):
        if run["lifecycle"] != "finalized":
            _refuse(
                "run_not_finalized",
                "only finalized runs carry comparable pins",
                run["id"],
            )

    drifted, unverifiable = compare_pin_families(
        baseline["pin_fingerprints"] or {},
        candidate["pin_fingerprints"] or {},
        baseline["unresolved_pins"] or [],
        candidate["unresolved_pins"] or [],
    )
    if drifted != declared_set:
        undeclared = sorted(drifted - declared_set)
        unmoved = sorted(declared_set - drifted)
        _refuse(
            "undeclared_drift",
            "a comparison holds everything constant but the declared families. "
            f"Moved but not declared: {undeclared or 'none'}; "
            f"declared but unchanged: {unmoved or 'none'}",
            "changed_pin_families",
        )

    held_constant = {
        family: baseline["pin_fingerprints"][family]
        for family in FINGERPRINTED_PIN_FAMILIES
        if family not in declared_set and family not in unverifiable
    }
    comparison_id = f"ecmp_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_comparisons
                (id, org_id, project_id, baseline_run_id, candidate_run_id,
                 comparison_kind, changed_pin_families, held_constant_fingerprint,
                 unverifiable_families, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)
            RETURNING id, created_at
            """,
            (
                comparison_id,
                org_id,
                project_id,
                baseline_run_id,
                candidate_run_id,
                kind,
                canonical_json(sorted(declared_set)),
                content_hash(held_constant),
                canonical_json(sorted(unverifiable)),
                _require_text(actor, "actor"),
            ),
        )
        row = cur.fetchone()
    return {
        "id": row[0],
        "baseline_run_id": baseline_run_id,
        "candidate_run_id": candidate_run_id,
        "comparison_kind": kind,
        "changed_pin_families": sorted(declared_set),
        "unverifiable_families": sorted(unverifiable),
        "held_constant_fingerprint": content_hash(held_constant),
        "created_at": _iso(row[1]),
    }


def regressions_between(
    baseline_by_question: dict[str, dict[str, dict]],
    candidate_by_question: dict[str, dict[str, dict]],
) -> list[dict[str, str]]:
    """Regression per question AND per dimension. Nothing compensates anything.

    `analyze-and-test.md:352-353`: `Pass -> Fail`, newly `Unverifiable` evidence
    and lost coverage are each a regression. An improvement elsewhere does not
    cancel one, which is why this returns a LIST of named regressions and never
    a net figure.
    """
    found: list[dict[str, str]] = []
    for question, baseline_dimensions in sorted(baseline_by_question.items()):
        candidate_dimensions = candidate_by_question.get(question)
        if candidate_dimensions is None:
            found.append(
                {
                    "golden_question_version_id": question,
                    "dimension": "*",
                    "kind": "lost_coverage",
                    "from": "evaluated",
                    "to": "absent",
                }
            )
            continue
        for dimension, baseline_entry in sorted(baseline_dimensions.items()):
            candidate_entry = candidate_dimensions.get(dimension)
            before = baseline_entry["verdict"]
            if candidate_entry is None:
                found.append(
                    {
                        "golden_question_version_id": question,
                        "dimension": dimension,
                        "kind": "lost_coverage",
                        "from": before,
                        "to": "absent",
                    }
                )
                continue
            after = candidate_entry["verdict"]
            if before == VERDICT_PASS and after == VERDICT_FAIL:
                kind = "pass_to_fail"
            elif after == VERDICT_UNVERIFIABLE and before != VERDICT_UNVERIFIABLE:
                kind = "newly_unverifiable"
            else:
                continue
            found.append(
                {
                    "golden_question_version_id": question,
                    "dimension": dimension,
                    "kind": kind,
                    "from": before,
                    "to": after,
                }
            )
    return found


def _verdicts_by_question(
    conn, *, run_id: str, org_id: str, project_id: str
) -> dict[str, dict[str, dict]]:
    cases = _load_cases(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    verdicts = _load_verdicts(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    return {
        case["golden_question_version_id"]: verdicts.get(case["id"], {}) for case in cases
    }


# ---------------------------------------------------------------------------
# The Gate Decision: evidence the owning workflow reads. Never a transition.
# ---------------------------------------------------------------------------


def emit_gate_decision(
    conn,
    *,
    org_id: str,
    project_id: str,
    comparison_id: str,
    candidate_owner_workspace: str,
    candidate_object_type: str,
    candidate_object_id: str,
    candidate_version_id: str,
    decided_by: str,
) -> dict[str, Any]:
    """Emit one immutable `pass | block | unverifiable` decision.

    NOTHING owned by Governance or Context Hub is written. The decision is
    evidence the owning workflow reads before its own publish or activate
    transition (`analyze-and-test.md:368-371`); an override, when policy permits
    one, is recorded by that workspace, not here.

    Missing required coverage is `unverifiable`, never green. So is a comparison
    whose two sides do not pin the same environment, and so is any dimension
    whose evidence does not exist -- which today includes `mcp_app_behavior` on
    every case, because no rendered artifact exists to judge.
    """
    workspace = _require_choice(
        candidate_owner_workspace, GATE_CANDIDATE_WORKSPACES, "candidate_owner_workspace"
    )
    object_type = _require_text(candidate_object_type, "candidate_object_type", maximum=60)
    if not _OBJECT_TYPE.match(object_type):
        _refuse(
            "invalid_object_type",
            f"`{object_type}` is not a canonical object type slug",
            "candidate_object_type",
        )
    object_id = _require_text(candidate_object_id, "candidate_object_id")
    version_id = _require_exact_pin(candidate_version_id, "candidate_version_id")
    actor = _require_text(decided_by, "decided_by")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT baseline_run_id, candidate_run_id, comparison_kind, unverifiable_families
              FROM app.evaluation_comparisons
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (comparison_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise EvaluationNotFound("comparison not found in this Project")
    baseline_run_id, candidate_run_id, kind, unverifiable_families = row

    baseline_verdicts = _verdicts_by_question(
        conn, run_id=baseline_run_id, org_id=org_id, project_id=project_id
    )
    candidate_verdicts = _verdicts_by_question(
        conn, run_id=candidate_run_id, org_id=org_id, project_id=project_id
    )

    eligible_questions = set(baseline_verdicts) | set(candidate_verdicts)
    evaluated_questions = {
        question
        for question, dimensions in candidate_verdicts.items()
        if set(dimensions) == set(DIMENSIONS)
    }
    coverage = {
        "eligible": len(eligible_questions),
        "evaluated": len(evaluated_questions),
        "missing": len(eligible_questions) - len(evaluated_questions),
    }

    regressions = regressions_between(baseline_verdicts, candidate_verdicts)
    failing = sorted(
        {r["dimension"] for r in regressions if r["dimension"] != "*"}
        | {
            dimension
            for dimensions in candidate_verdicts.values()
            for dimension, entry in dimensions.items()
            if entry["verdict"] == VERDICT_FAIL
        }
    )
    unverifiable_dimensions = sorted(
        {
            dimension
            for dimensions in candidate_verdicts.values()
            for dimension, entry in dimensions.items()
            if entry["verdict"] == VERDICT_UNVERIFIABLE
        }
    )

    if coverage["missing"] > 0 or unverifiable_families or unverifiable_dimensions:
        decision = "unverifiable"
        reason = (
            "coverage or evidence is incomplete: "
            f"{coverage['missing']} question(s) unevaluated, "
            f"unverifiable pin families {sorted(unverifiable_families or [])}, "
            f"unverifiable dimensions {unverifiable_dimensions}"
        )
        # A `pass` that listed a failing dimension is not a pass, and neither is
        # an `unverifiable` that hides one -- so the list travels either way.
    elif failing:
        decision = "block"
        reason = f"regression on {', '.join(failing)}"
    else:
        decision = "pass"
        reason = "every eligible question was evaluated and no dimension regressed"

    payload = {
        "comparison_id": comparison_id,
        "comparison_kind": kind,
        "decision": decision,
        "candidate": {
            "owner_workspace": workspace,
            "object_type": object_type,
            "object_id": object_id,
            "version_id": version_id,
        },
        "coverage": coverage,
        "failing_dimensions": failing,
        "regressions": regressions,
    }
    decision_id = f"egd_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_gate_decisions
                (id, org_id, project_id, comparison_id, decision, candidate_owner_workspace,
                 candidate_object_type, candidate_object_id, candidate_version_id,
                 coverage, failing_dimensions, decision_reason, decided_by, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            RETURNING id, decided_at
            """,
            (
                decision_id,
                org_id,
                project_id,
                comparison_id,
                decision,
                workspace,
                object_type,
                object_id,
                version_id,
                canonical_json(coverage),
                canonical_json(failing if decision != "pass" else []),
                reason[:2000],
                actor,
                content_hash(payload),
            ),
        )
        row = cur.fetchone()
    return {
        "id": row[0],
        "decided_at": _iso(row[1]),
        "decision_reason": reason[:2000],
        "unverifiable_families": sorted(unverifiable_families or []),
        "unverifiable_dimensions": unverifiable_dimensions,
        **payload,
    }


# ---------------------------------------------------------------------------
# Read model -- one function per Level 3 tab, plus the collection.
# ---------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def list_evaluation_runs(
    conn, *, org_id: str, project_id: str, evidence_mode: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """The Regression Runs collection.

    `offline` and `observed_cohort` travel as a field on every row and are never
    merged. There is no pass rate here and no `latest score`: those are the
    on-screen form of the merge `analyze-and-test.md:210` forbids.
    """
    bounded = max(1, min(int(limit), 200))
    mode = None
    if evidence_mode:
        mode = _require_choice(evidence_mode, EVIDENCE_MODES, "evidence_mode")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.lifecycle, r.evidence_mode, p.name, r.as_of, r.started_at,
                   r.ended_at, r.question_set_fingerprint, r.unresolved_pins,
                   (SELECT COUNT(*) FROM app.evaluation_run_cases c
                     WHERE c.run_id = r.id AND c.project_id = r.project_id)
              FROM app.evaluation_runs r
              JOIN app.evaluation_run_profiles p
                ON p.id = r.run_profile_id AND p.org_id = r.org_id
               AND p.project_id = r.project_id
             WHERE r.org_id = %s AND r.project_id = %s
               AND (%s::text IS NULL OR r.evidence_mode = %s::text)
             ORDER BY r.started_at DESC, r.id DESC
             LIMIT %s
            """,
            (org_id, project_id, mode, mode, bounded),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "lifecycle": r[1],
            "evidence_mode": r[2],
            "run_profile": r[3],
            "as_of": _iso(r[4]),
            "started_at": _iso(r[5]),
            "ended_at": _iso(r[6]),
            "question_set_fingerprint": r[7],
            "unresolved_pin_count": len(r[8] or []),
            "case_count": r[9],
        }
        for r in rows
    ]


def run_overview(conn, *, org_id: str, project_id: str, run_id: str) -> dict[str, Any]:
    run = _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    cases = _load_cases(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    verdicts = _load_verdicts(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    return {
        "id": run["id"],
        "lifecycle": run["lifecycle"],
        "evidence_mode": run["evidence_mode"],
        "run_profile_id": run["run_profile_id"],
        "as_of": _iso(run["as_of"]),
        "started_at": _iso(run["started_at"]),
        "ended_at": _iso(run["ended_at"]),
        "content_hash": run["content_hash"],
        "question_set_fingerprint": run["question_set_fingerprint"],
        "case_count": len(cases),
        "unresolved_pins": run["unresolved_pins"] or [],
        # Per dimension, per verdict. No total, no rate, no trust score.
        "verdict_counts": verdict_counts(verdicts),
    }


def run_cases(conn, *, org_id: str, project_id: str, run_id: str) -> dict[str, Any]:
    _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    cases = _load_cases(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    verdicts = _load_verdicts(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    return {
        "run_id": run_id,
        "cases": [
            {
                "id": case["id"],
                "golden_question_version_id": case["golden_question_version_id"],
                "result_id": case["result_id"],
                # Exactly one of the two, or neither -- and "neither" is the
                # honest third state, not a missing value to be filled in later.
                "ai_path": case["ai_path_id"] or case["ai_path_absent_literal"],
                "business_domain_id": case["business_domain_id"],
                "business_domain_version_number": case["business_domain_version_number"],
                "capability_key": case["capability_key"],
                "result_type": case["result_type"],
                "unresolved_pins": case["unresolved_pins"] or [],
                "feedback_regression": (
                    {
                        "feedback_id": case["feedback_id"],
                        "regression_case_id": case["regression_case_id"],
                    }
                    if case.get("feedback_id") and case.get("regression_case_id")
                    else None
                ),
                "verdicts": [
                    {"dimension": d, **verdicts.get(case["id"], {}).get(d, _absent_verdict())}
                    for d in DIMENSIONS
                ],
            }
            for case in cases
        ],
    }


def _absent_verdict() -> dict[str, Any]:
    """What a dimension reads as before anything measured it.

    Not `pass`, not `fail`, and not an empty cell a reader would take for green.
    """
    return {
        "verdict_id": None,
        "verdict": VERDICT_UNVERIFIABLE,
        "reason_code": "verdict_not_recorded",
        "evidence_refs": {"owner": DIMENSION_EVALUATOR_OWNER},
    }


def run_environment(conn, *, org_id: str, project_id: str, run_id: str) -> dict[str, Any]:
    """The seven pinned families of `analyze-and-test.md:223-231`, each resolved
    to an exact identity or to a declared, owned absence."""
    run = _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    context = _load_context_entries(
        conn, set_id=run["context_version_set_id"], org_id=org_id, project_id=project_id
    )
    cases = _load_cases(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    unresolved = run["unresolved_pins"] or []
    by_family = {str(p.get("pin_family")): p for p in unresolved}

    def _family(name: str, pinned: Any) -> dict[str, Any]:
        absence = by_family.get(name)
        if absence is not None:
            return {"family": name, "state": "unresolved", "pinned": None, "absence": absence}
        return {"family": name, "state": "pinned", "pinned": pinned, "absence": None}

    skills = [e for e in context if e["owner_object_type"] == "skill"]
    return {
        "run_id": run_id,
        "families": [
            _family(PIN_RESULT, [c["result_id"] for c in cases if c["result_id"]]),
            _family(PIN_RENDER, None),
            _family(
                PIN_AI_PATH,
                [c["ai_path_id"] or c["ai_path_absent_literal"] for c in cases],
            ),
            _family(
                PIN_SEMANTIC_VIEW,
                {
                    "semantic_view_id": run["semantic_view_id"],
                    "semantic_view_version_id": run["semantic_view_version_id"],
                },
            ),
            _family(PIN_SKILL, skills),
            _family(
                PIN_CONTEXT_VERSION_SET,
                {"id": run["context_version_set_id"], "entries": context},
            ),
            _family(
                PIN_MODEL,
                {
                    "model_ref": run["model_ref"],
                    "host_capability_profile": run["host_capability_profile"],
                    "tool_catalog_version": run["tool_catalog_version"],
                },
            ),
            _family(
                PIN_DATA_SNAPSHOT,
                {
                    "data_snapshot_ref": run["data_snapshot_ref"],
                    "data_snapshot_hash": run["data_snapshot_hash"],
                    "as_of": _iso(run["as_of"]),
                },
            ),
        ],
        "pin_fingerprints": run["pin_fingerprints"] or {},
        "unresolved_pins": unresolved,
    }


def run_comparisons(conn, *, org_id: str, project_id: str, run_id: str) -> dict[str, Any]:
    _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, baseline_run_id, candidate_run_id, comparison_kind,
                   changed_pin_families, unverifiable_families, held_constant_fingerprint,
                   created_by, created_at
              FROM app.evaluation_comparisons
             WHERE org_id = %s AND project_id = %s
               AND (baseline_run_id = %s OR candidate_run_id = %s)
             ORDER BY created_at DESC, id DESC
            """,
            (org_id, project_id, run_id, run_id),
        )
        rows = cur.fetchall()
    return {
        "run_id": run_id,
        "comparisons": [
            {
                "id": r[0],
                "baseline_run_id": r[1],
                "candidate_run_id": r[2],
                "comparison_kind": r[3],
                "changed_pin_families": r[4],
                "unverifiable_families": r[5],
                "held_constant_fingerprint": r[6],
                "created_by": r[7],
                "created_at": _iso(r[8]),
            }
            for r in rows
        ],
    }


def run_gate_decisions(conn, *, org_id: str, project_id: str, run_id: str) -> dict[str, Any]:
    _load_run(conn, run_id=run_id, org_id=org_id, project_id=project_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.id, g.comparison_id, g.decision, g.candidate_owner_workspace,
                   g.candidate_object_type, g.candidate_object_id, g.candidate_version_id,
                   g.coverage, g.failing_dimensions, g.decision_reason, g.decided_by,
                   g.decided_at
              FROM app.evaluation_gate_decisions g
              JOIN app.evaluation_comparisons c
                ON c.id = g.comparison_id AND c.org_id = g.org_id
               AND c.project_id = g.project_id
             WHERE g.org_id = %s AND g.project_id = %s
               AND (c.baseline_run_id = %s OR c.candidate_run_id = %s)
             ORDER BY g.decided_at DESC, g.id DESC
            """,
            (org_id, project_id, run_id, run_id),
        )
        rows = cur.fetchall()
    return {
        "run_id": run_id,
        "gate_decisions": [
            {
                "id": r[0],
                "comparison_id": r[1],
                "decision": r[2],
                "candidate": {
                    "owner_workspace": r[3],
                    "object_type": r[4],
                    "object_id": r[5],
                    "version_id": r[6],
                },
                "coverage": r[7],
                "failing_dimensions": r[8],
                "decision_reason": r[9],
                "decided_by": r[10],
                "decided_at": _iso(r[11]),
            }
            for r in rows
        ],
    }
