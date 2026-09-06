"""Story 50.1 -- Query Spec domain: canonical form, validation, immutable versions.

WHAT THIS OWNS. The analytical request, and only that. A Query Spec says which
governed measures and dimensions to ask for, over which pinned Semantic View
version, with which filters, grain, time window and limits. It says nothing about
how the answer is drawn -- no chart, no layout, no narrative. Those belong to
Stories 50.4-50.5, and a presentation field here would make this a second
presentation contract (AC12).

WHAT IT REFUSES TO BE. `app.entity_source_bindings.query_spec` (migration 089)
carries the same words and is NOT this: it is opaque source-connector
configuration -- what to pull from a provider. AC1 forbids treating it as the
analytical object, and nothing in this module reads it.

THE ONE AUTHORITY. Validation runs against the exact pinned Semantic View version
and its compiled artifact, never against `current` state and never against a
catalogue kept here. That matters because the compiled artifact carries the
`queryability_matrix`: the list of metric x dimension pairs the compiler PROVED
legal, each with its join path. Re-deriving compatibility in this module would
create a second semantic authority that drifts from the compiler the moment
either changes -- so this module reads the matrix and refuses anything absent
from it, rather than reasoning about joins itself.

NO FALLBACK, ANYWHERE. AC3 is explicit: an unknown member, an incompatible pair
or a stale version is a structured refusal. It is never repaired by picking a
near-miss member, never silently downgraded to a legacy report definition, and
never answered from a different version. Every refusal below names the exact
member or pair it rejected, so a caller can fix the request rather than guess.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ulid import ULID

from core.semantic_compiler import COMPILER_VERSION

#: Bumped when the canonical serialization changes shape. A stored
#: `content_hash` is only comparable to another hash produced by the same
#: contract version, so the version travels inside the hashed document.
QUERY_SPEC_CONTRACT_VERSION = "query-spec.v1"

#: Bounds applied when the pinned view version's `query_policy` sets none. They
#: are deliberately finite: an unbounded analytical request is how a governed
#: warehouse turns into an outage, and AC2 requires the limits to be part of the
#: recorded intent rather than a runtime surprise.
DEFAULT_ROW_LIMIT = 10_000
MAX_ROW_LIMIT = 100_000
MAX_MEASURES = 50
MAX_DIMENSIONS = 20
MAX_FILTERS = 100

_SORT_DIRECTIONS = frozenset({"asc", "desc"})
_COMPARISONS = frozenset({"none", "previous_period", "previous_year"})

#: The field the executed comparison labels each row with. Same name and same
#: two values as the cross-source path emits (`multi_source_plan`), because one
#: product may not have two names for the same column.
COMPARISON_PERIOD_FIELD = "comparison_period"
COMPARISON_CURRENT = "current"
COMPARISON_BASELINE = "baseline"


@dataclass(frozen=True)
class ComparisonPrecondition:
    """One condition a period comparison cannot be compiled without.

    `condition` is the machine name a screen tests before it offers the control;
    `code` is the refusal code the door emits when the request arrives anyway;
    `message` is THE sentence -- the only one this product has for this refusal.
    """

    condition: str
    code: str
    message: str


#: The three conditions `_comparison_windows` refuses on, in the order it tests
#: them, each with the sentence it refuses with.
#:
#: WHY THIS IS A TABLE AND NOT THREE LITERALS INSIDE THE FUNCTION. The Explore
#: door has to disable the comparison control BEFORE the round trip -- a question
#: that cannot be answered must not be asked -- so it needs the same three
#: conditions and the same three sentences. Story 67.9 gave it them by RETYPING
#: them into `ui/admin/src/analyze/QueryDoor.tsx`, with the tails adapted; from
#: that day the screen and the door said two different things about one refusal,
#: and nothing would have noticed the day this file changed its words. The screen
#: now reads these sentences off the facets response and phrases none of its own.
#:
#: The ORDER is part of the contract: the screen names the first unmet condition,
#: and it must be the same one the door would name.
COMPARISON_PRECONDITIONS: tuple[ComparisonPrecondition, ...] = (
    ComparisonPrecondition(
        "time_member_required",
        "comparison_window_required",
        "A period comparison needs the time member and the From and To dates "
        "of the window to compare. Choose them, or set comparison to none.",
    ),
    ComparisonPrecondition(
        "time_member_not_selected",
        "comparison_time_member_not_selected",
        "A period comparison labels each row with the period it belongs to, so the "
        "time member must be one of the dimensions of the request. Add it, or set "
        "comparison to none.",
    ),
    ComparisonPrecondition(
        "window_required",
        "comparison_window_required",
        "A period comparison needs both the From and the To date of the window "
        "to compare. Set them, or set comparison to none.",
    ),
)

_PRECONDITION_BY_CONDITION: dict[str, ComparisonPrecondition] = {
    precondition.condition: precondition for precondition in COMPARISON_PRECONDITIONS
}
_FILTER_OPERATORS = frozenset(
    {"eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "is_null", "is_not_null"}
)
#: Operators that carry no value. Sending one a value is a malformed filter, not
#: a harmless extra: it usually means the caller meant a different operator.
_VALUELESS_OPERATORS = frozenset({"is_null", "is_not_null"})


class QuerySpecNotFound(LookupError):
    """The Query Spec, version or Semantic View is not in the authorized Project.

    AC10: foreign, denied and nonexistent identities must be externally
    indistinguishable, so callers raise THIS for all three and never leak which
    of the three it was. The distinguishing reason belongs in audit, not in a
    response body.
    """


@dataclass(frozen=True)
class Refusal:
    """One named, actionable reason a request was not accepted."""

    code: str
    message: str
    #: The exact member, pair or control the refusal is about, so a UI can attach
    #: it to the right form field instead of showing one banner for everything.
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "subject": self.subject}


class QuerySpecRefused(ValueError):
    """A structured refusal carrying every reason, not just the first one.

    Returning only the first failure makes a caller fix one member, resubmit, and
    discover the next -- so validation collects them all.
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
# Canonical form and hashing.
#
# The hash is over the NORMALIZED document, not the caller's payload: two
# requests that mean the same thing must hash the same, or AC8's "identical
# content may be deduplicated by verified content hash" cannot hold. Key order,
# absent-vs-default and list order are all normalized before hashing.
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise QuerySpecRefused(
            "invalid_spec", "the analytical request must be JSON serializable"
        ) from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidatedSpec:
    """A request proven legal against one exact Semantic View version."""

    spec: dict[str, Any]
    content_hash: str
    semantic_view_id: str
    semantic_view_version_id: str
    #: The join paths the compiler proved for the requested pairs. Carried through
    #: so execution never has to re-derive them (Task 4 consumes this).
    join_paths: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading the pinned authority.
# ---------------------------------------------------------------------------


def _load_pinned_view(
    conn, *, project_id: str, semantic_view_id: str, semantic_view_version_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (view_version_row, queryability_matrix) for an executable pin.

    Raises QuerySpecNotFound when the pin does not resolve inside this Project --
    the same answer a foreign or nonexistent id gets (AC10).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, view_id, project_id, version_number, status, query_policy
            FROM app.semantic_view_versions
            WHERE id = %s AND view_id = %s AND project_id = %s
            """,
            (semantic_view_version_id, semantic_view_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise QuerySpecNotFound("semantic view version not found in this Project")
        version = {
            "id": row[0],
            "view_id": row[1],
            "project_id": row[2],
            "version_number": row[3],
            "status": row[4],
            "query_policy": row[5] or {},
        }

        # AC3: only a PUBLISHED version executes. A draft or superseded pin stays
        # visible to the caller as history but cannot answer a question -- which
        # is why this refuses rather than silently advancing to the current one.
        if version["status"] != "published":
            raise QuerySpecRefused(
                "version_not_executable",
                f"this Semantic View version is {version['status']}, not published",
                [
                    Refusal(
                        "version_not_executable",
                        "only a published, compiled Semantic View version can be queried",
                        semantic_view_version_id,
                    )
                ],
            )

        cur.execute(
            """
            SELECT queryability_matrix, compiler_version
            FROM app.semantic_compiled_artifacts
            WHERE view_version_id = %s AND project_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (semantic_view_version_id, project_id),
        )
        compiled = cur.fetchone()
        if compiled is None or not compiled[0]:
            # Published without a compiled artifact is an inconsistent upstream
            # state, not a caller error. Refusing is the honest answer; inventing
            # a matrix here would be the second authority this module exists to
            # avoid.
            raise QuerySpecRefused(
                "not_compiled",
                "this Semantic View version has no compiled artifact to query",
                [Refusal("not_compiled", "no compiled artifact", semantic_view_version_id)],
            )

        # 27.8 -- FAIL CLOSED ON A STALE ARTIFACT. `COMPILER_VERSION` declares itself
        # "bumped whenever the compiler's OUTPUT changes for unchanged input ... so an
        # artifact is never reinterpreted under a compiler that would have produced
        # something else". Adding `dimensions[].name` did exactly that, and the version
        # did not move: an artifact compiled before it carries no conformed name, so the
        # language-family guard silently ABSTAINED and the two dimensions it exists to
        # keep apart could be requested together. Abstaining is right for a pure
        # function reading whatever it was handed; it is wrong for the one place that
        # decides whether a question runs.
        stored_version = compiled[1]
        if stored_version != COMPILER_VERSION:
            # AI-346 -- THE REFUSAL NAMES THE ATTEMPT. The product re-derives every
            # stale artifact at process start and every night
            # (`semantic_model.recompile_stale_artifacts`); a version that is still
            # stale here was either not reached yet, or reached and refused. The
            # two are different situations and get different sentences: "publish
            # it again" alone sent a person to mint a version nothing required.
            attempt = _recompile_attempt(cur, semantic_view_version_id)
            refusals = [
                Refusal(
                    "stale_compiled_artifact",
                    f"compiled by {stored_version or 'an unrecorded compiler'}, "
                    f"this product compiles {COMPILER_VERSION}",
                    semantic_view_version_id,
                )
            ]
            if attempt is not None and attempt["outcome"] == "refused":
                codes = sorted(
                    {
                        str(r.get("code"))
                        for r in attempt["refusals"]
                        if isinstance(r, dict) and r.get("code")
                    }
                )
                message = (
                    "this Semantic View version was compiled by an earlier version of the "
                    f"product; the product tried to recompile it on {attempt['when']} and "
                    f"could not ({', '.join(codes) or 'no reason recorded'}) -- correct the "
                    "version and publish it again through a change set to recompile it"
                )
                refusals.extend(
                    Refusal(str(r.get("code")), str(r.get("message") or ""), r.get("path"))
                    for r in attempt["refusals"]
                    if isinstance(r, dict) and r.get("code")
                )
            else:
                message = (
                    "this Semantic View version was compiled by an earlier version of the "
                    "product and cannot be queried as it stands -- the product recompiles "
                    "published versions at start and every night; publish it again to "
                    "recompile it now"
                )
            raise QuerySpecRefused("stale_compiled_artifact", message, refusals)
    return version, dict(compiled[0])


def _recompile_attempt(cur, semantic_view_version_id: str) -> dict | None:
    """The newest recorded attempt to re-derive this version under the current compiler.

    `None` when the sweep has not reached the version, or when the attempts table
    is not readable here -- this runs on a refusal path, and a second failure must
    not replace the first sentence with a database error.
    """
    try:
        cur.execute(
            """
            SELECT outcome, refusals, attempted_at
              FROM app.semantic_recompile_attempts
             WHERE view_version_id = %s AND compiler_version = %s
            """,
            (semantic_view_version_id, COMPILER_VERSION),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- a refusal path never trades its sentence for a traceback
        return None
    if row is None:
        return None
    attempted_at = row[2]
    refusals = row[1]
    if isinstance(refusals, (str, bytes)):
        try:
            refusals = json.loads(refusals)
        except ValueError:
            refusals = []
    return {
        "outcome": row[0],
        "refusals": list(refusals or []),
        "when": (
            attempted_at.date().isoformat() if hasattr(attempted_at, "date") else "an earlier run"
        ),
    }


def _index_matrix(
    matrix: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[tuple, dict]]:
    """Index the compiled matrix by member id, and by (metric, dimension) pair."""
    metrics = {
        str(m.get("concept_id")): str(m.get("version_id"))
        for m in matrix.get("metrics") or []
        if m.get("concept_id")
    }
    dimensions = {
        str(d.get("concept_id")): str(d.get("version_id"))
        for d in matrix.get("dimensions") or []
        if d.get("concept_id")
    }
    cells: dict[tuple, dict] = {}
    for cell in matrix.get("cells") or []:
        key = (str(cell.get("metric_id")), str(cell.get("dimension_id")))
        cells[key] = cell
    return metrics, dimensions, cells


def _dimension_names(matrix: dict[str, Any]) -> dict[str, str]:
    """Index concept_id -> stable conformed-dimension name, for the entries that carry one.

    An artifact compiled before the compiler published `name` simply has no entry here.
    That absence is honest and deliberate: a validator that needs the name to judge a
    request abstains rather than guessing one from the display label, which a client is
    free to rename. Recompiling the view is what makes the judgement available.
    """
    return {
        str(d.get("concept_id")): str(d.get("name"))
        for d in matrix.get("dimensions") or []
        if d.get("concept_id") and d.get("name")
    }


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------


def _require_list(payload: dict[str, Any], key: str, refusals: list[Refusal]) -> list[Any]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        refusals.append(Refusal("invalid_shape", f"`{key}` must be a list", key))
        return []
    return value


def _member_ids(entries: list[Any], key: str, refusals: list[Refusal]) -> list[dict[str, str]]:
    """Normalize `[{id, version_id?}]` or `["id"]` into an explicit member list."""
    out: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            out.append({"id": entry, "version_id": ""})
        elif isinstance(entry, dict) and entry.get("id"):
            out.append({"id": str(entry["id"]), "version_id": str(entry.get("version_id") or "")})
        else:
            refusals.append(
                Refusal("invalid_member", f"`{key}[{index}]` must name a member id", key)
            )
    return out


def _validate_members(
    requested: list[dict[str, str]],
    known: dict[str, str],
    kind: str,
    refusals: list[Refusal],
) -> list[dict[str, str]]:
    """Resolve each requested member against the compiled matrix. No near-miss."""
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for member in requested:
        member_id = member["id"]
        if member_id in seen:
            refusals.append(
                Refusal("duplicate_member", f"{kind} `{member_id}` is requested twice", member_id)
            )
            continue
        seen.add(member_id)
        if member_id not in known:
            # Deliberately no "did you mean": a suggestion invites a caller to
            # accept a member they did not ask for, which is the semantic
            # substitution AC3 forbids.
            refusals.append(
                Refusal(
                    "unknown_member",
                    f"`{member_id}` is not a queryable {kind} of this Semantic View version",
                    member_id,
                )
            )
            continue
        exact_version = known[member_id]
        pinned = member.get("version_id") or ""
        if pinned and pinned != exact_version:
            # A caller pinning a version the published view does not carry is
            # asking a question about a different world. Answering with the
            # view's version would silently change the meaning.
            refusals.append(
                Refusal(
                    "version_mismatch",
                    f"{kind} `{member_id}` is pinned to `{pinned}` but this view version "
                    f"carries `{exact_version}`",
                    member_id,
                )
            )
            continue
        resolved.append({"id": member_id, "version_id": exact_version})
    return resolved


def _validate_pairs(
    measures: list[dict[str, str]],
    dimensions: list[dict[str, str]],
    cells: dict[tuple, dict],
    refusals: list[Refusal],
) -> list[dict[str, Any]]:
    """Every requested measure x dimension pair must be PROVEN queryable."""
    join_paths: list[dict[str, Any]] = []
    for measure in measures:
        for dimension in dimensions:
            cell = cells.get((measure["id"], dimension["id"]))
            if cell is None or not cell.get("queryable"):
                reason = (cell or {}).get("reason") or "the compiler did not prove this pair legal"
                refusals.append(
                    Refusal(
                        "incompatible_pair",
                        f"`{measure['id']}` cannot be split by `{dimension['id']}`: {reason}",
                        f"{measure['id']}|{dimension['id']}",
                    )
                )
                continue
            join_paths.append(
                {
                    "metric_id": measure["id"],
                    "dimension_id": dimension["id"],
                    "join_path": cell.get("join_path") or [],
                }
            )
    return join_paths


def _validate_incommensurable_dimensions(
    dimensions: list[dict[str, str]],
    names_by_id: dict[str, str],
    refusals: list[Refusal],
) -> None:
    """Refuse a request that splits ONE result by two dimensions that cannot share it.

    Story 27.8 named a family whose members carry the same word and NOT the same
    meaning: a language OBSERVED on the person, a language that is a PROPERTY OF THE
    ASSET served, and a language DECLARED AS TARGETED. Putting two of them on one
    result invites the reader to compare them -- and "we targeted FR, we reached
    NL-BE" is an INFORMATION, never a total. ``core.language_dimensions`` states the
    rule ("the contract of every aggregation surface: call this first"); this is that
    call, on the one surface where an analytical request is assembled.

    The judgement runs on the STABLE conformed name, never on the member id (a ULID
    says nothing about meaning) and never on the display label (a client renames it
    at will -- 27.9: the name belongs to the client, the identifier does not). A
    dimension whose artifact predates ``name`` in the matrix is simply not judged;
    silence is the correct answer when the evidence is absent.

    Consistent with the rest of this module (AC3): a structured refusal naming both
    members, never a repair. The caller asks two questions instead of one -- which is
    what the data actually supports. Every dimension outside a declared-incommensurable
    family passes through untouched, so a view that never uses one is unaffected.
    """
    from core.language_dimensions import (  # noqa: PLC0415 -- local, mirrors core.db usage
        IncommensurableDimensions,
        assert_language_dimensions_comparable,
    )

    named = [names_by_id[d["id"]] for d in dimensions if d["id"] in names_by_id]
    try:
        assert_language_dimensions_comparable(named)
    except IncommensurableDimensions as exc:
        # The message is the module's, not re-spelled here: one wording, one owner.
        refusals.append(Refusal("incommensurable_dimensions", str(exc), "dimensions"))


def _validate_filters(
    entries: list[Any], allowed: set[str], refusals: list[Refusal]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if len(entries) > MAX_FILTERS:
        refusals.append(
            Refusal("limit_exceeded", f"at most {MAX_FILTERS} filters are allowed", "filters")
        )
        return out
    for index, entry in enumerate(entries):
        subject = f"filters[{index}]"
        if not isinstance(entry, dict):
            refusals.append(Refusal("invalid_shape", "a filter must be an object", subject))
            continue
        member_id = str(entry.get("member_id") or "")
        operator = str(entry.get("operator") or "")
        if member_id not in allowed:
            refusals.append(
                Refusal(
                    "unknown_member",
                    f"`{member_id}` is not a queryable member of this Semantic View version",
                    subject,
                )
            )
            continue
        if operator not in _FILTER_OPERATORS:
            refusals.append(
                Refusal("unknown_operator", f"`{operator}` is not a supported operator", subject)
            )
            continue
        has_value = "value" in entry and entry.get("value") is not None
        if operator in _VALUELESS_OPERATORS and has_value:
            refusals.append(Refusal("value_not_allowed", f"`{operator}` takes no value", subject))
            continue
        if operator not in _VALUELESS_OPERATORS and not has_value:
            refusals.append(Refusal("value_required", f"`{operator}` requires a value", subject))
            continue
        normalized: dict[str, Any] = {"member_id": member_id, "operator": operator}
        if has_value:
            normalized["value"] = entry["value"]
        # Classification pins travel WITH the filter, because a classification
        # value only means something under the hierarchy version that defined it.
        for pin in ("classification_object_id", "hierarchy_id", "hierarchy_version_id"):
            if entry.get(pin):
                normalized[pin] = str(entry[pin])
        out.append(normalized)
    return out


def _validate_limits(
    payload: dict[str, Any], policy: dict[str, Any], refusals: list[Refusal]
) -> int:
    ceiling = int(policy.get("max_row_limit") or MAX_ROW_LIMIT)
    raw = payload.get("row_limit")
    if raw is None:
        return min(DEFAULT_ROW_LIMIT, ceiling)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        refusals.append(Refusal("invalid_limit", "`row_limit` must be an integer", "row_limit"))
        return min(DEFAULT_ROW_LIMIT, ceiling)
    if value < 1:
        refusals.append(Refusal("invalid_limit", "`row_limit` must be at least 1", "row_limit"))
    elif value > ceiling:
        refusals.append(
            Refusal("limit_exceeded", f"`row_limit` may not exceed {ceiling}", "row_limit")
        )
    return value


def _previous_year(value: date) -> date:
    """The same calendar day one year earlier, 29 February clamped to the 28th."""
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _comparison_refusal(condition: str) -> Refusal:
    """The door's refusal for one declared comparison precondition."""
    precondition = _PRECONDITION_BY_CONDITION[condition]
    return Refusal(precondition.code, precondition.message, "comparison")


def _comparison_windows(
    kind: str,
    time_window: dict[str, Any],
    requested_ids: set[str],
    refusals: list[Refusal],
) -> dict[str, Any] | None:
    """Freeze the two exact inclusive windows a period comparison will execute.

    WHY THE WINDOWS ARE FROZEN HERE AND NOT DERIVED AT EXECUTION. The Query Spec
    version is immutable and hashed; a baseline re-derived at each run would let
    the same pinned Spec answer a different question tomorrow. Freezing makes the
    comparison reproducible and lets the Definitions lens name the two windows it
    actually read instead of describing an intention.

    THE RULE IS THE ONE THE CROSS-SOURCE PATH ALREADY APPLIES
    (`multi_source_plan._compile_comparison`): previous_period is the window of
    the same length ending the day before this one, previous_year is the same
    calendar window one year earlier, and overlapping windows are refused. It is
    written here rather than imported because that function reads compiled plan
    filters, not a time window -- the shared thing is the rule, and the rule is
    stated in both places in the same words.

    THE THREE PRECONDITIONS ARE DECLARED, NOT TYPED HERE. They are tested in the
    order `COMPARISON_PRECONDITIONS` declares them, and each refusal carries that
    entry's code and that entry's sentence, because the Explore door reads the
    same table to decide whether the question can be asked at all.

    Returns None and appends refusals when the request cannot carry a comparison.
    """
    if not time_window.get("member_id"):
        refusals.append(_comparison_refusal("time_member_required"))
        return None
    if str(time_window["member_id"]) not in requested_ids:
        # The execution labels each row from the date column the physical plan
        # resolved, and the plan resolves only the members the request selected.
        # Accepting a comparison on an unselected date would compile a label the
        # read cannot produce -- and the read would then drop it in silence.
        refusals.append(_comparison_refusal("time_member_not_selected"))
        return None
    raw_start, raw_end = time_window.get("start"), time_window.get("end")
    if not raw_start or not raw_end:
        refusals.append(_comparison_refusal("window_required"))
        return None
    try:
        current_start = date.fromisoformat(str(raw_start))
        current_end = date.fromisoformat(str(raw_end))
    except ValueError:
        refusals.append(
            Refusal(
                "comparison_window_required",
                "Comparison dates use the ISO YYYY-MM-DD format.",
                "comparison",
            )
        )
        return None
    if current_start > current_end:
        refusals.append(
            Refusal(
                "comparison_window_required",
                "The comparison From date is after its To date.",
                "comparison",
            )
        )
        return None
    try:
        if kind == "previous_period":
            span = (current_end - current_start).days + 1
            baseline_end = current_start - timedelta(days=1)
            baseline_start = baseline_end - timedelta(days=span - 1)
        else:
            baseline_start = _previous_year(current_start)
            baseline_end = _previous_year(current_end)
    except (OverflowError, ValueError):
        refusals.append(
            Refusal(
                "comparison_window_required",
                "The selected window has no representable earlier comparison window.",
                "comparison",
            )
        )
        return None
    if baseline_end >= current_start:
        refusals.append(
            Refusal(
                "comparison_windows_overlap",
                "Current and baseline comparison windows must not overlap.",
                "comparison",
            )
        )
        return None
    return {
        "kind": kind,
        "period_field": COMPARISON_PERIOD_FIELD,
        "member_id": str(time_window["member_id"]),
        "current": {"start": current_start.isoformat(), "end": current_end.isoformat()},
        "baseline": {"start": baseline_start.isoformat(), "end": baseline_end.isoformat()},
    }


def _validate_as_of(time_window: dict[str, Any], refusals: list[Refusal]) -> str | None:
    """An as-of instant, or a refusal naming the format it must have.

    `as_of` is EXECUTED on this path: the read keeps only the rows loaded at or
    before it, so the answer is what was known then rather than what is known now
    (`query_execution._superseded_source`). A bare date is read as the END of that
    day -- "reported as of the 5th" means everything that had landed by the close
    of the 5th -- and the widening happens at build time, so the Spec keeps the
    instant exactly as it was asked for.
    """
    raw = time_window.get("as_of")
    if not raw:
        return None
    text = str(raw)
    try:
        date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text)
    except ValueError:
        refusals.append(
            Refusal(
                "invalid_as_of",
                "`as_of` is an ISO date (YYYY-MM-DD) or an ISO timestamp.",
                "time.as_of",
            )
        )
        return None
    return text


def _refuse_unexecuted_time_controls(
    time_window: dict[str, Any], refusals: list[Refusal]
) -> None:
    """Refuse the two time controls this path does not execute.

    A control that is accepted, frozen into an immutable Spec and described by the
    Definitions lens, but never reaches the SQL, is a half-truth the product does
    not allow: it is executed, or it is refused at the door with the limit named.
    These two are refused, and for two different reasons.

    `timezone` is refused because it CANNOT be executed here without contradicting
    a ratified invariant: every governed grain is a DATE, and the timezone module
    SIGNALS a misalignment rather than realigning rows at read time. Re-projecting
    a DATE under another zone would move measurements between days -- a second,
    unratified alignment authority sitting inside a read.

    `reporting_boundary_id` is refused because nothing in the product resolves one:
    there is no reporting-boundary object, table or governance surface to pin, so
    an accepted id could only ever be an id echoed back. It is refused as not yet
    executed on this path rather than stored as decoration.
    """
    if time_window.get("timezone"):
        refusals.append(
            Refusal(
                "timezone_not_executed",
                "A time zone is not applied on this path: governed grains are dates, and "
                "the reporting time zone is declared on the Datastream and signalled "
                "there, never re-applied when a Result is read. Remove it from the request.",
                "time.timezone",
            )
        )
    if time_window.get("reporting_boundary_id"):
        refusals.append(
            Refusal(
                "reporting_boundary_not_executed",
                "A reporting boundary is not yet executed on this path, and no governed "
                "reporting boundary exists to pin. Use the From and To dates of the "
                "window instead.",
                "time.reporting_boundary_id",
            )
        )


def validate_query_spec(
    conn,
    *,
    project_id: str,
    semantic_view_id: str,
    semantic_view_version_id: str,
    payload: dict[str, Any],
) -> ValidatedSpec:
    """Validate one analytical request against its exact pinned Semantic View version.

    Raises QuerySpecNotFound for an unresolvable pin (AC10) and QuerySpecRefused
    carrying EVERY reason for a resolvable but illegal request (AC3). Returns the
    normalized, hashable spec on success. Never repairs, never substitutes.
    """
    if not isinstance(payload, dict):
        raise QuerySpecRefused("invalid_spec", "the analytical request must be an object")

    version, matrix = _load_pinned_view(
        conn,
        project_id=project_id,
        semantic_view_id=semantic_view_id,
        semantic_view_version_id=semantic_view_version_id,
    )
    known_metrics, known_dimensions, cells = _index_matrix(matrix)
    policy = version["query_policy"] if isinstance(version["query_policy"], dict) else {}

    refusals: list[Refusal] = []

    raw_measures = _require_list(payload, "measures", refusals)
    raw_dimensions = _require_list(payload, "dimensions", refusals)
    if len(raw_measures) > MAX_MEASURES:
        refusals.append(
            Refusal("limit_exceeded", f"at most {MAX_MEASURES} measures are allowed", "measures")
        )
    if len(raw_dimensions) > MAX_DIMENSIONS:
        refusals.append(
            Refusal(
                "limit_exceeded", f"at most {MAX_DIMENSIONS} dimensions are allowed", "dimensions"
            )
        )
    if not raw_measures:
        # A request with no measure has no answer to give. Returning dimension
        # members alone would be a catalogue read dressed as an analytical query.
        refusals.append(
            Refusal("no_measure", "an analytical request needs at least one measure", "measures")
        )

    measures = _validate_members(
        _member_ids(raw_measures, "measures", refusals), known_metrics, "measure", refusals
    )
    dimensions = _validate_members(
        _member_ids(raw_dimensions, "dimensions", refusals),
        known_dimensions,
        "dimension",
        refusals,
    )

    join_paths = _validate_pairs(measures, dimensions, cells, refusals)
    _validate_incommensurable_dimensions(dimensions, _dimension_names(matrix), refusals)

    filterable = set(known_metrics) | set(known_dimensions)
    filters = _validate_filters(_require_list(payload, "filters", refusals), filterable, refusals)

    # Sort must name a member the request actually asked for: sorting by a member
    # that is not in the result is a meaning change disguised as presentation.
    requested_ids = {m["id"] for m in measures} | {d["id"] for d in dimensions}
    sort: list[dict[str, str]] = []
    for index, entry in enumerate(_require_list(payload, "sort", refusals)):
        subject = f"sort[{index}]"
        if not isinstance(entry, dict) or not entry.get("member_id"):
            refusals.append(Refusal("invalid_shape", "a sort entry needs a member_id", subject))
            continue
        member_id = str(entry["member_id"])
        direction = str(entry.get("direction") or "asc").lower()
        if member_id not in requested_ids:
            refusals.append(
                Refusal(
                    "sort_not_selected",
                    f"`{member_id}` is not part of this request, so it cannot order it",
                    subject,
                )
            )
            continue
        if direction not in _SORT_DIRECTIONS:
            refusals.append(
                Refusal("invalid_direction", f"`{direction}` is not a sort direction", subject)
            )
            continue
        sort.append({"member_id": member_id, "direction": direction})

    time_window = payload.get("time") if isinstance(payload.get("time"), dict) else {}

    comparison = str(payload.get("comparison") or "none").lower()
    comparison_windows: dict[str, Any] | None = None
    if comparison not in _COMPARISONS:
        refusals.append(
            Refusal(
                "unknown_comparison", f"`{comparison}` is not a source comparison", "comparison"
            )
        )
    elif comparison != "none":
        comparison_windows = _comparison_windows(
            comparison, time_window, requested_ids, refusals
        )

    as_of = _validate_as_of(time_window, refusals)
    _refuse_unexecuted_time_controls(time_window, refusals)

    row_limit = _validate_limits(payload, policy, refusals)

    # A Result shape is selected by the pinned Semantic View policy, never by a
    # client-authored descriptor. The caller may ask for a published shape id;
    # the immutable policy supplies the exact component/member bindings.
    result_shape = str(payload.get("result_shape") or "tabular_v1")
    result_shape_descriptor: dict[str, Any] | None = None
    if result_shape != "tabular_v1":
        published_shapes = policy.get("result_shapes")
        candidate = (
            published_shapes.get(result_shape) if isinstance(published_shapes, dict) else None
        )
        if not isinstance(candidate, dict):
            refusals.append(
                Refusal(
                    "result_shape_not_allowed",
                    f"`{result_shape}` is not published by this Semantic View version",
                    "result_shape",
                )
            )
        else:
            result_shape_descriptor = dict(candidate)
            shape_members = {
                str(component.get("member_id") or "")
                for component in result_shape_descriptor.get("components") or []
                if isinstance(component, dict)
            }
            for binding_name in (
                "currency",
                "covered_row_count",
                "total_row_count",
                "gap_codes",
            ):
                binding = result_shape_descriptor.get(binding_name)
                if isinstance(binding, dict) and binding.get("member_id"):
                    shape_members.add(str(binding["member_id"]))
            manifest_pins = result_shape_descriptor.get("manifest_pins")
            if isinstance(manifest_pins, dict):
                shape_members.update(
                    str(binding["member_id"])
                    for binding in manifest_pins.values()
                    if isinstance(binding, dict) and binding.get("member_id")
                )
            shape_members.discard("")
            missing_shape_members = sorted(shape_members - requested_ids)
            if missing_shape_members:
                refusals.append(
                    Refusal(
                        "result_shape_member_not_selected",
                        "the selected Result shape references members this query did not select: "
                        + ", ".join(missing_shape_members),
                        "result_shape",
                    )
                )

    if comparison_windows is not None and result_shape != "tabular_v1":
        # A shape that re-keys its rows by member id (waterfall_v1) drops the
        # period label with them, so the two windows would arrive indistinguishable
        # -- the comparison would be accepted and then lost.
        refusals.append(
            Refusal(
                "comparison_result_shape_not_supported",
                f"A period comparison is not carried by the `{result_shape}` Result shape. "
                "Ask for it as a table, or set comparison to none.",
                "comparison",
            )
        )
        comparison_windows = None

    grain = str(payload.get("grain") or "").lower() or None

    if refusals:
        raise QuerySpecRefused(
            "invalid_query_spec",
            f"the analytical request was refused on {len(refusals)} point(s)",
            refusals,
        )

    spec = {
        "contract_version": QUERY_SPEC_CONTRACT_VERSION,
        "semantic_view_id": semantic_view_id,
        "semantic_view_version_id": semantic_view_version_id,
        "measures": measures,
        "dimensions": dimensions,
        "filters": filters,
        "sort": sort,
        "comparison": comparison,
        "grain": grain,
        "row_limit": row_limit,
        "time": {
            "member_id": str(time_window.get("member_id") or "") or None,
            "start": str(time_window.get("start") or "") or None,
            "end": str(time_window.get("end") or "") or None,
            # THESE TWO ARE ALWAYS NULL, AND THE KEYS STAY. Any value for either
            # is refused above, so nothing can be stored here again; the keys
            # remain so the canonical serialization -- and therefore the
            # `content_hash` of every Spec version already written -- keeps the
            # exact shape `query-spec.v1` was hashed under.
            "timezone": None,
            "as_of": as_of,
            "reporting_boundary_id": None,
        },
        "result_shape": result_shape,
    }
    if comparison_windows is not None:
        # Frozen only when a comparison was actually compiled, exactly as
        # `result_shape_descriptor` below is carried only when one was pinned.
        spec["comparison_windows"] = comparison_windows
    required_capability = policy.get("required_capability")
    if isinstance(required_capability, dict) and required_capability.get("key"):
        spec["required_capability"] = {
            "key": str(required_capability["key"]),
        }
    if result_shape_descriptor is not None:
        spec["result_shape_descriptor"] = result_shape_descriptor
    return ValidatedSpec(
        spec=spec,
        content_hash=canonical_hash(spec),
        semantic_view_id=semantic_view_id,
        semantic_view_version_id=semantic_view_version_id,
        join_paths=join_paths,
    )


# ---------------------------------------------------------------------------
# Persistence: a stable head, and immutable versions on top of it.
#
# Both writes happen in the CALLER's transaction. The head's `current_version_id`
# advances in the same transaction as the version insert, which is what makes the
# deferred foreign key in migration 151 safe.
# ---------------------------------------------------------------------------


def create_query_spec_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    validated: ValidatedSpec,
    actor: str,
    query_spec_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Append the next immutable version, creating the stable head when absent."""
    with conn.cursor() as cur:
        if query_spec_id is None:
            query_spec_id = f"qs_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.query_specs
                    (id, org_id, project_id, semantic_view_id, name, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (query_spec_id, org_id, project_id, validated.semantic_view_id, name, actor),
            )
            predecessor = None
            version_number = 1
        else:
            # Scope is part of the lookup, not checked afterwards: a foreign head
            # simply does not resolve, and answers exactly like a missing one.
            cur.execute(
                """
                SELECT current_version_id FROM app.query_specs
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (query_spec_id, org_id, project_id),
            )
            head = cur.fetchone()
            if head is None:
                raise QuerySpecNotFound("query spec not found in this Project")
            predecessor = head[0]
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM app.query_spec_versions
                WHERE query_spec_id = %s AND project_id = %s
                """,
                (query_spec_id, project_id),
            )
            version_number = int(cur.fetchone()[0])
            if predecessor is None:
                raise QuerySpecRefused(
                    "head_without_version",
                    "this Query Spec has no current version to revise",
                )

        version_id = f"qsv_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id, version_number, content_hash, created_at
            """,
            (
                version_id,
                query_spec_id,
                org_id,
                project_id,
                version_number,
                validated.semantic_view_id,
                validated.semantic_view_version_id,
                _canonical_json(validated.spec),
                validated.content_hash,
                predecessor,
                actor,
            ),
        )
        row = cur.fetchone()
        cur.execute(
            """
            UPDATE app.query_specs
            SET current_version_id = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, query_spec_id, org_id, project_id),
        )
    return {
        "query_spec_id": query_spec_id,
        "id": row[0],
        "version_number": row[1],
        "content_hash": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
        "semantic_view_id": validated.semantic_view_id,
        "semantic_view_version_id": validated.semantic_view_version_id,
    }


#: The collection page a caller gets when it asks for none. Finite by default,
#: because the only screen that reads this list offers a picker: a select that
#: grows without bound stops being a choice long before the server notices.
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def list_query_specs(
    conn, *, org_id: str, project_id: str, limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The Project's Query Specs, newest first, with the version a caller can pin.

    WHAT A ROW CARRIES, AND WHY NOT MORE. `current_version_id` is the pinnable
    identity -- it is what `app.query_specs` advances on every appended version
    and what `answerable_topics.bind_query` accepts. It travels beside
    `current_version_number` so a chooser can SAY which version it is pinning:
    an id alone tells a reader nothing about whether the spec has been revised
    since they last looked, and a pin nobody can read is a pin nobody checks.

    `name` IS THE ONLY LABEL THIS STORE HOLDS, and it is nullable
    (`151_query_specs_and_results.sql`: `name TEXT`). It is returned exactly as
    stored, `null` included. Deriving a sentence from the spec body here would
    invent a title the author never wrote and that no other surface would agree
    with -- the caller decides what to show for an unnamed spec.

    A HEAD WITHOUT A VERSION is listed, with `current_version_id` null. The
    column is nullable and the row is real; hiding it would answer "this Project
    has no such Query Spec", which is a different fact from "it has one, and
    nothing can be pinned to it yet".

    Paged by KEYSET on `id DESC`, like `analyze_artifacts.list_renders`: ids are
    `qs_{ULID}` and sort by creation, so a page boundary cannot skip or repeat a
    row when one is written mid-scan -- which an OFFSET would.
    """
    limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    # Scope is part of the WHERE, never a check after the fact: a foreign Project
    # returns no row rather than a row that is then filtered in Python.
    params: list[Any] = [org_id, project_id]
    where = "s.org_id = %s AND s.project_id = %s"
    if cursor:
        where += " AND s.id < %s"
        params.append(cursor)
    params.append(limit + 1)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.id, s.name, s.semantic_view_id, s.current_version_id,
                   v.version_number, s.created_at
            FROM app.query_specs s
            LEFT JOIN app.query_spec_versions v
              ON v.id = s.current_version_id
             AND v.org_id = s.org_id
             AND v.project_id = s.project_id
            WHERE {where}
            ORDER BY s.id DESC LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "query_specs": [
            {
                "id": r[0],
                "name": r[1],
                "semantic_view_id": r[2],
                "current_version_id": r[3],
                "current_version_number": int(r[4]) if r[4] is not None else None,
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ],
        "next_cursor": rows[-1][0] if has_more and rows else None,
    }
