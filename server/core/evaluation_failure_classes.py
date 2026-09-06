"""Story 75-7 -- the class a failing or unverifiable evaluation case belongs to.

WHY THIS MODULE EXISTS. A finalized Evaluation Run states six verdicts per case
and, per dimension, how many passed, failed, were unverifiable or did not apply.
It never states WHAT WOULD HAVE TO CHANGE for a failing case to pass. Epic 75
asks for a before/after cohort around exactly such repairs, and a delta between
two runs that cannot name which gap a repair filled is a number, not a
measurement.

`analyze-and-test.md`, amendment of 2026-09-05 ("a failed or unverifiable case
names the gap that would fill it") is the ratified target; this module is its
one implementation. `scripts/classify_evaluation_failures.py` loads rows and
calls it, and the tests call the same functions -- a second copy of these rules
is how one class would start meaning two things depending on who asked.

WHAT IT IS ALLOWED TO READ. Only what the case ALREADY carries: its pins, its
six dimension verdicts, its stored path comparison, its assertion results and
the Golden Question version's own contract. It never re-executes anything, never
reads a question's wording for a cause, and a signal these rules do not
recognise falls to `other` carrying the exact key or reason code that was not
recognised -- never into a class it merely resembles.

PURE. No SQL, no connection, no import of a data-access module. Every function
here takes dicts and returns dicts, which is what lets the pg test and the unit
test assert the SAME rules.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

# ---------------------------------------------------------------------------
# The classes, and the order they are tried in. First match wins.
# ---------------------------------------------------------------------------

CLASS_NO_SUBJECT = "no_subject"
CLASS_MISSING_DEFINITION = "missing_definition"
CLASS_JOIN_PATH = "join_path"
CLASS_FISCAL_OR_SCOPE = "fiscal_or_scope_setting"
CLASS_MISSING_EXEMPLAR = "missing_exemplar"
CLASS_PASSED = "passed"
CLASS_UNVERIFIABLE_BY_DESIGN = "unverifiable_by_design"
CLASS_OTHER = "other"

#: THE RULE ORDER, copied from the amendment. Two placements are deliberate and
#: neither is alphabetical:
#:
#:   * `no_subject` is first not because it is narrowest but because it makes
#:     every other class unreadable. A question nobody walked produces six
#:     `unverifiable` verdicts, which ALSO satisfies `unverifiable_by_design`;
#:     reporting it there would blame an undelivered evaluator for an execution
#:     that never happened.
#:   * `missing_definition` is ahead of `join_path` because a view bound to a
#:     concept the model does not have still cannot answer. Filling the
#:     definition is the prerequisite gesture; binding the view first would move
#:     a number without making the question answerable.
RULE_ORDER = (
    CLASS_NO_SUBJECT,
    CLASS_MISSING_DEFINITION,
    CLASS_JOIN_PATH,
    CLASS_FISCAL_OR_SCOPE,
    CLASS_MISSING_EXEMPLAR,
    CLASS_PASSED,
    CLASS_UNVERIFIABLE_BY_DESIGN,
    CLASS_OTHER,
)

# ---------------------------------------------------------------------------
# The object types each rail owns.
#
# `app.ai_path_steps.owner_object_type` has no enum -- migration 150 constrains
# it to `^[a-z][a-z0-9-]{2,60}$` and nothing more -- so these sets are the types
# this repository actually WRITES, and an unlisted type is deliberately not
# routed to a rail. It falls to `other` with its key printed, which is how a
# seventh class gets named from evidence instead of from a hunch.
# ---------------------------------------------------------------------------

#: Story 75-1's rail. `semantic-concept` / `semantic-concept-version` are written
#: by `core.analyze_workbench` and `core.evidence_index`; `canonical-field` by
#: `core.ai_path_recorder` and `core.governance_read_model`.
DEFINITION_OBJECT_TYPES = frozenset(
    {
        "semantic-concept",
        "semantic-concept-version",
        "canonical-field",
        "metric-definition",
        "calculated-field",
    }
)

#: Story 75-5's rail: what a topic must DECLARE before a path may cross it.
JOIN_PATH_OBJECT_TYPES = frozenset(
    {
        "semantic-view",
        "semantic-view-version",
        "query-spec",
        "answerable-topic",
        "relation",
        "common-key",
    }
)

#: Story 75-3's rail: the governed context a path was supposed to read first.
CONTEXT_OBJECT_TYPES = frozenset(
    {
        "knowledge-note",
        "knowledge-graph",
        "knowledge-library",
        "skill",
        "skills-registry",
        "procedure",
        "exemplar",
        "golden-question",
    }
)

#: Story 75-4's rail, when a pattern names the settings object itself.
SETTING_OBJECT_TYPES = frozenset({"ai-settings", "fiscal-calendar"})

#: The `unverifiable` reasons whose owner is DECLARED and is not the executor.
#: A case made only of these is not a gap epic 75 can fill, and saying otherwise
#: would send a reader to a rail that cannot move it.
#: Sources: `core.evaluation_runs` (the first four) and `core.feedback_regression`
#: (the last two, written by the trusted result-case evaluator).
#:
#: `render_owner_not_delivered` is SIX rather than five on purpose, and the
#: reason is written at `core.evaluation_runs:183-185`: rows written before
#: 2026-07-31 carry it, immutable evidence is never rewritten to look better in
#: hindsight, and `core.evidence_index`, `core.feedback_review`,
#: `core.golden_questions` and `core.trace_observation` still emit it today.
#: Dropping it here would push every one of those cases into `other`, which
#: means "a class nobody has named yet" -- the one thing they are not.
OWNER_NOT_EXECUTOR_REASONS = frozenset(
    {
        "dimension_evaluator_not_delivered",
        "render_not_pinned_for_this_subject",
        "render_owner_not_delivered",
        "path_comparison_not_delivered",
        "mcp_app_evaluator_unavailable",
        "context_skill_evidence_unavailable",
    }
)

#: `core.result_assertion_evaluator`: the expected result names a field the
#: Result does not carry. The one assertion reason that points at the MODEL
#: rather than at the evidence.
REASON_REQUIRED_FIELD_MISSING = "required_field_missing"

#: `core.ai_settings.resolve` returns the scope each field came from. `PLATFORM`
#: means nobody -- not the organization, not the Project -- ever declared it.
SCOPE_PLATFORM = "PLATFORM"

#: `core.ai_paths.NO_AI_PATH`, copied rather than imported so this module stays
#: free of the data-access graph. A case carrying it CLAIMS no AI was involved,
#: which is a different statement from "no path was recorded".
NO_AI_PATH = "No AI path"

# ---------------------------------------------------------------------------
# The gesture each class names. A rail, a door, and one sentence.
# ---------------------------------------------------------------------------

GESTURES: dict[str, dict[str, str]] = {
    CLASS_NO_SUBJECT: {
        "story": "the run's own subject pin",
        "door": "POST /api/projects/{project_id}/test/evaluation-runs/{run_id}/execute",
        "gesture": (
            "Promote a reviewed Result for this question, or name the Result this "
            "run should judge in `subjects`."
        ),
    },
    CLASS_MISSING_DEFINITION: {
        "story": "75-1",
        "door": (
            "propose_calculated_field (MCP, governance profile, confirmed_write) "
            "or POST /api/projects/{project_id}/calculated-field-proposals"
        ),
        "gesture": (
            "Propose the calculation as a governed field; resolving the proposal "
            "opens the prepared semantic change-set."
        ),
    },
    CLASS_JOIN_PATH: {
        "story": "75-5",
        "door": "POST /api/projects/{project_id}/answerable-topics/{topic_key}/views",
        "gesture": (
            "Bind the exact Semantic View version this topic reads, and the "
            "relation paths it is allowed to cross."
        ),
    },
    CLASS_FISCAL_OR_SCOPE: {
        "story": "75-4",
        "door": (
            "PUT /api/projects/{project_id}/ai-settings "
            "or PUT /api/organizations/{org_id}/ai-settings"
        ),
        "gesture": (
            "Declare the fiscal calendar, the query scope or the language at the "
            "scope that owns it."
        ),
    },
    CLASS_MISSING_EXEMPLAR: {
        "story": "75-3",
        "door": "get_exemplars (MCP, agent surface)",
        "gesture": (
            "Publish and activate a Golden Question for this subject so "
            "`get_exemplars` can serve it."
        ),
    },
    CLASS_PASSED: {
        "story": "-",
        "door": "-",
        "gesture": "None. Carried so the before/after delta has a denominator.",
    },
    CLASS_UNVERIFIABLE_BY_DESIGN: {
        "story": "-",
        "door": "-",
        "gesture": (
            "None in this epic: the reason code names its own owner, and no rail "
            "of 75-1..75-5 moves it."
        ),
    },
    CLASS_OTHER: {
        "story": "-",
        "door": "-",
        "gesture": (
            "None invented. The failing dimensions, their reason codes and any "
            "unrecognised node key are printed instead."
        ),
    },
}

#: The one variation a gesture takes, and it is READ, never guessed: a subject
#: that already has an active exemplar was not short of one, it was not
#: consulted.
GESTURE_EXEMPLAR_EXISTS = (
    "This subject already has {count} active exemplar(s): the exemplar exists "
    "and was not consulted. Check that the host reaches `get_exemplars` for it."
)


# ---------------------------------------------------------------------------
# Reading the evidence. Nothing below invents a value that is absent.
# ---------------------------------------------------------------------------


def object_type_of(node_key: Any) -> str | None:
    """`workspace/object_type/object_id` -> the object type, or None.

    The grammar is `schemas/expected-ai-path.schema.json`: exactly the triple
    `app.ai_path_steps` records. A key that is not that shape yields None rather
    than a best guess -- an unparsed key is reported as unrecognised.
    """
    if not isinstance(node_key, str):
        return None
    parts = node_key.split("/")
    if len(parts) < 3 or not all(parts[:2]) or not parts[2]:
        return None
    return parts[1]


def workspace_of(node_key: Any) -> str | None:
    """The owning workspace of a node key, or None when the key is not one."""
    if not isinstance(node_key, str):
        return None
    parts = node_key.split("/")
    if len(parts) < 3 or not all(parts[:2]) or not parts[2]:
        return None
    return parts[0]


def harvest_node_keys(findings: Any) -> list[str]:
    """Every node key inside a comparison finding list, nested ones included.

    `app.evaluation_path_comparisons.required_missing` holds two shapes at once
    (`core.evaluation_runs.record_path_comparison` concatenates them): the flat
    `{"key", "step_kind"}` of a missing required node, and the nested
    `{"alternative", "branches": [{"missing": [{"key"}...]}]}` of an alternative
    group no branch satisfied. Walking for `key` reads both without teaching this
    module two readers for one column.
    """
    found: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, Mapping):
            key = value.get("key")
            if isinstance(key, str) and key and key not in found:
                found.append(key)
            for inner in value.values():
                if isinstance(inner, (Mapping, list)):
                    _walk(inner)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(findings)
    return found


def _time_boundary_names_a_fiscal_calendar(time_boundary: Any) -> bool:
    """Does the question ASK in a fiscal calendar?

    `app.golden_question_versions.time_boundary` is a free object (migration 153
    constrains only `jsonb_typeof = 'object'`), so this reads what is there: any
    string value, or any key, carrying the word `fiscal`. It never infers a
    calendar from a grain that does not say so -- `month` is a month.
    """
    if not isinstance(time_boundary, Mapping):
        return False
    for key, value in time_boundary.items():
        if isinstance(key, str) and "fiscal" in key.lower():
            return True
        if isinstance(value, str) and "fiscal" in value.lower():
            return True
    return False


def _verdicts(case: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = case.get("verdicts") or {}
    return {k: v for k, v in raw.items() if isinstance(v, Mapping)}


def _dimensions_with(case: Mapping[str, Any], verdict: str) -> list[str]:
    return sorted(d for d, v in _verdicts(case).items() if v.get("verdict") == verdict)


def _assertion_missing_fields(case: Mapping[str, Any]) -> list[str]:
    fields: list[str] = []
    for item in case.get("assertion_results") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("reason_code") != REASON_REQUIRED_FIELD_MISSING:
            continue
        evidence = item.get("evidence_refs")
        field = evidence.get("field") if isinstance(evidence, Mapping) else None
        if isinstance(field, str) and field and field not in fields:
            fields.append(field)
        elif field is None:
            fields.append("(field not named)")
    return fields


# ---------------------------------------------------------------------------
# The classification itself.
# ---------------------------------------------------------------------------


def classify_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """The ONE class this case belongs to, the signals that decided it, its gesture.

    `case` is the shape `scripts/classify_evaluation_failures.py` loads, and the
    tests build by hand:

        {
          "case_id", "golden_question_version_id", "question", "severity",
          "result_id", "ai_path_id", "ai_path_absent_literal",
          "verdicts": {dimension: {"verdict", "reason_code", "evidence_refs"}},
          "path_comparison": {"evidence_state", "path_verdict", "required_missing",
                              "forbidden_present", "order_violations",
                              "version_mismatches"} | None,
          "assertion_results": [{"assertion_type", "verdict", "reason_code",
                                 "evidence_refs"}],
          "time_boundary": {...},
          "exemplars_active": int | None,       # None = not read, never 0
          "fiscal_calendar_source": str | None, # `core.ai_settings.resolve` sources
          "query_scope_source": str | None,
        }

    Every optional field is allowed to be absent or None, and an absent field
    never FIRES a rule: a fact nobody read cannot decide a class.

    EVERY RULE IS TRIED, AND ONLY THEN IS ONE CHOSEN. `analyze-and-test.md`,
    amendment of 2026-09-05: "a case that matches two rules therefore reports ONE
    class, always the earlier one, and the signals of both travel in the report
    so nothing is lost by the choice." Returning at the first match lost the
    later rule's evidence: a case whose concept AND whose view were missing named
    the concept and stayed silent about the view, so a reader filled one gap and
    found the question still unanswerable. The later signals travel prefixed by
    the class that saw them, so they are never read as the reason this class was
    chosen.
    """
    comparison = case.get("path_comparison") or {}
    missing_keys = harvest_node_keys(comparison.get("required_missing"))
    mismatch_keys = harvest_node_keys(comparison.get("version_mismatches"))
    named_keys = list(dict.fromkeys(missing_keys + mismatch_keys))
    forbidden = list(comparison.get("forbidden_present") or [])
    order_violations = [
        item for item in (comparison.get("order_violations") or []) if isinstance(item, Mapping)
    ]

    failed = _dimensions_with(case, "fail")
    passed = _dimensions_with(case, "pass")

    #: (class, its own signals, the extra fields it carries), in RULE_ORDER, one
    #: entry per rule that FIRED. The first is the class; the rest are evidence.
    matches: list[tuple[str, list[str], dict[str, Any]]] = []

    def _decide(
        name: str, signals: list[str], extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        gesture = dict(GESTURES[name])
        if name == CLASS_MISSING_EXEMPLAR:
            count = case.get("exemplars_active")
            if isinstance(count, int) and count > 0:
                gesture["gesture"] = GESTURE_EXEMPLAR_EXISTS.format(count=count)
        return {
            "case_id": case.get("case_id"),
            "golden_question_version_id": case.get("golden_question_version_id"),
            "question": case.get("question"),
            "severity": case.get("severity"),
            "class": name,
            "signals": signals,
            "failed_dimensions": failed,
            "passed_dimensions": passed,
            "story": gesture["story"],
            "door": gesture["door"],
            "gesture": gesture["gesture"],
            **(extra or {}),
        }

    # 1. no_subject -- nothing was walked, so every other class would be a guess
    #    about an execution that never happened.
    if (
        not case.get("result_id")
        and not case.get("ai_path_id")
        and case.get("ai_path_absent_literal") != NO_AI_PATH
    ):
        matches.append(
            (CLASS_NO_SUBJECT, ["no Result and no observed AI Path pinned on the case"], {})
        )

    # 2. missing_definition -- an object the project's model does not carry.
    definition_keys = [k for k in named_keys if object_type_of(k) in DEFINITION_OBJECT_TYPES]
    missing_fields = _assertion_missing_fields(case)
    if definition_keys or missing_fields:
        signals = [
            f"required node not crossed, or crossed at another version: {key}"
            for key in definition_keys
        ]
        signals += [
            f"assertion `{REASON_REQUIRED_FIELD_MISSING}` on field `{field}`"
            for field in missing_fields
        ]
        matches.append((CLASS_MISSING_DEFINITION, signals, {}))

    # 3. join_path -- the path crosses what the topic does not declare.
    join_keys = [k for k in named_keys if object_type_of(k) in JOIN_PATH_OBJECT_TYPES]
    if join_keys:
        matches.append(
            (
                CLASS_JOIN_PATH,
                [
                    f"required node not crossed, or crossed at another version: {key}"
                    for key in join_keys
                ],
                {},
            )
        )

    # 4. fiscal_or_scope_setting -- a setting nobody declared.
    setting_keys = [k for k in named_keys if object_type_of(k) in SETTING_OBJECT_TYPES]
    asks_fiscal = _time_boundary_names_a_fiscal_calendar(case.get("time_boundary"))
    fiscal_undeclared = case.get("fiscal_calendar_source") == SCOPE_PLATFORM
    scope_undeclared = case.get("query_scope_source") == SCOPE_PLATFORM
    if setting_keys or (asks_fiscal and fiscal_undeclared) or (forbidden and scope_undeclared):
        signals = [f"required settings node not crossed: {key}" for key in setting_keys]
        if asks_fiscal and fiscal_undeclared:
            signals.append(
                "the question asks in a fiscal calendar and `fiscal_calendar` "
                "still resolves from PLATFORM"
            )
        if forbidden and scope_undeclared:
            signals.append(
                f"{len(forbidden)} forbidden node(s) crossed while `query_scope` "
                "still resolves from PLATFORM"
            )
        matches.append((CLASS_FISCAL_OR_SCOPE, signals, {}))

    # 5. missing_exemplar -- the governed context was not read, or not read first.
    context_keys = [k for k in named_keys if object_type_of(k) in CONTEXT_OBJECT_TYPES]
    context_order = [
        item
        for item in order_violations
        if object_type_of(item.get("before")) in CONTEXT_OBJECT_TYPES
    ]
    adherence_failed = (_verdicts(case).get("context_adherence") or {}).get("verdict") == "fail"
    if context_keys or context_order or adherence_failed:
        signals = [f"required context node not crossed: {key}" for key in context_keys]
        signals += [
            f"prerequisite violated: `{item.get('before')}` was expected before "
            f"`{item.get('after')}`"
            for item in context_order
        ]
        if adherence_failed:
            signals.append("`context_adherence` failed")
        matches.append(
            (CLASS_MISSING_EXEMPLAR, signals, {"exemplars_active": case.get("exemplars_active")})
        )

    # 6. passed -- nothing failed and something was actually measured.
    if not failed and passed:
        matches.append((CLASS_PASSED, [f"{len(passed)} dimension(s) passed, none failed"], {}))

    # 7. unverifiable_by_design -- every absence has a declared owner, and none of
    #    them is a rail of this epic.
    #
    #    A stored comparison that NAMED something is disqualifying, even when the
    #    verdicts are all `unverifiable`: this class means "there is nothing here
    #    to act on", and a finding is something to act on. Without that guard a
    #    finding whose object type no rail owns would be laundered into "nobody
    #    could have done anything", which is the one thing it does not mean.
    reasons = {
        v.get("reason_code")
        for v in _verdicts(case).values()
        if v.get("verdict") == "unverifiable"
    }
    findings = bool(named_keys or forbidden or order_violations)
    if (
        not failed
        and not passed
        and not findings
        and reasons
        and reasons <= OWNER_NOT_EXECUTOR_REASONS
    ):
        matches.append(
            (
                CLASS_UNVERIFIABLE_BY_DESIGN,
                [
                    f"unverifiable, owner declared elsewhere: `{reason}`"
                    for reason in sorted(r for r in reasons if isinstance(r, str))
                ],
                {},
            )
        )

    if matches:
        name, own_signals, extra = matches[0]
        signals = list(own_signals)
        for other_name, other_signals, _extra in matches[1:]:
            for signal in other_signals:
                carried = f"also `{other_name}`: {signal}"
                if carried not in signals:
                    signals.append(carried)
        return _decide(name, signals, extra)

    # 8. other -- named, never a silent bucket.
    signals = [
        f"`{dimension}` failed: `{(_verdicts(case)[dimension]).get('reason_code')}`"
        for dimension in failed
    ]
    unrecognised = [k for k in named_keys if k not in definition_keys + join_keys + setting_keys]
    signals += [f"node key not routed to any rail: {key}" for key in unrecognised]
    for reason in sorted(r for r in reasons if isinstance(r, str)):
        if reason not in OWNER_NOT_EXECUTOR_REASONS:
            signals.append(f"unverifiable, reason not owned elsewhere: `{reason}`")
    if not signals:
        signals.append("no failing dimension, no passing dimension and no named absence")
    return _decide(CLASS_OTHER, signals)


def classify_run(cases: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One classification per case, in the order the cases were given."""
    return [classify_case(case) for case in cases]


def class_counts(classified: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Case count per class, EVERY class listed so the denominator travels."""
    counts = dict.fromkeys(RULE_ORDER, 0)
    for item in classified:
        name = item.get("class")
        if name in counts:
            counts[name] += 1
    return counts


#: The six ratified dimensions, in document order. Copied rather than imported so
#: this module stays free of the data-access graph; `core.evaluation_runs` is the
#: authority and `tests/core/test_evaluation_failure_classes.py` asserts they
#: still agree.
DIMENSIONS = (
    "semantic_correctness",
    "provenance_correctness",
    "context_adherence",
    "path_quality",
    "dq_handling",
    "mcp_app_behavior",
)

VERDICTS = ("pass", "fail", "unverifiable", "not_applicable")


def dimension_counts(cases: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Counts per (dimension, verdict). No ratio, here or anywhere.

    The same shape `core.evaluation_runs.verdict_counts` produces, rebuilt from
    the loaded cases so the report states one tally and not two.
    """
    counts = {d: dict.fromkeys(VERDICTS, 0) for d in DIMENSIONS}
    for case in cases:
        for dimension, entry in (case.get("verdicts") or {}).items():
            if dimension in counts and isinstance(entry, Mapping):
                verdict = entry.get("verdict")
                if verdict in counts[dimension]:
                    counts[dimension][verdict] += 1
    return counts


# ---------------------------------------------------------------------------
# The before/after delta.
# ---------------------------------------------------------------------------

#: The lifecycle a run must be in before anything classifies it. A run that can
#: still grow would have its classification invalidated by its next case.
LIFECYCLE_FINALIZED = "finalized"

#: THE ENVIRONMENT HELD CONSTANT ACROSS A COMPARISON, exactly the seven of
#: `analyze-and-test.md`, "The before/after protocol": "Semantic View version,
#: Context Version Set, model, host capability profile, tool-catalog version,
#: data snapshot and `as_of` are held constant. What changes between the runs is
#: only what the rails of 75-1, 75-3, 75-4 and 75-5 wrote."
#:
#: Every one of them is a NOT NULL column of `app.evaluation_runs` (migration
#: 153, section ENVIRONMENT), so an absent pin here means nobody read it -- and
#: an unread pin is not proof of agreement, exactly as an absent fingerprint is
#: not. `data_snapshot_hash` rather than `data_snapshot_ref`: the hash is what
#: the run writer derives from the ref, so two refs that differ only in key
#: order are one snapshot and not two.
ENVIRONMENT_PINS = (
    "semantic_view_version_id",
    "context_version_set_id",
    "model_ref",
    "host_capability_profile",
    "tool_catalog_version",
    "data_snapshot_hash",
    "as_of",
)


def _pin_value(value: Any) -> str | None:
    """One pin, in the one form two runs can be compared in.

    A JSONB pin (`host_capability_profile`) is canonicalised with sorted keys so
    a re-serialisation is not read as a change; a date is stringified so a
    `datetime.date` and its ISO form are one value and not two.
    """
    if value is None:
        return None
    if isinstance(value, (Mapping, list)):
        return json.dumps(value, sort_keys=True, default=str)
    text = str(value)
    return text or None


def environment_pins(source: Mapping[str, Any] | None) -> dict[str, str | None]:
    """The seven pins of one run, read from whatever carried them.

    The script reads them off the `app.evaluation_runs` columns; G16 reads them
    off `GET .../evaluation-runs/{run_id}/environment`. One normalization, so
    the two readers cannot disagree about what "the same environment" means.
    """
    read = source or {}
    return {pin: _pin_value(read.get(pin)) for pin in ENVIRONMENT_PINS}


def environment_drift(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Every pin that differs between two runs, or that nobody read on one side.

    An unread pin is listed like a differing one: a comparison claims the
    environment was held constant, and a pin nobody read proves nothing.
    """
    was = environment_pins(before)
    now = environment_pins(after)
    return [
        {"pin": pin, "before": was[pin], "after": now[pin]}
        for pin in ENVIRONMENT_PINS
        if was[pin] is None or now[pin] is None or was[pin] != now[pin]
    ]


class RunNotFinalized(ValueError):
    """A run that can still grow is not classified.

    Raised where BOTH callers inherit it -- the script and the gate build their
    report through `classification_report`, so neither can forget the guard, and
    a run finalized by one door and read a moment later by another is checked on
    the lifecycle that was READ, never on the intention of the call that froze
    it.
    """

    def __init__(self, run_id: str, lifecycle: str | None):
        super().__init__(
            f"Evaluation Run `{run_id}` is `{lifecycle}`, not `{LIFECYCLE_FINALIZED}`: a run "
            "that can still grow would have its classification invalidated by its next case. "
            "Finalize it (POST .../evaluation-runs/{run_id}/finalize), then classify."
        )
        self.run_id = run_id
        self.lifecycle = lifecycle


class FingerprintMismatch(ValueError):
    """Two runs that judged different question sets are not comparable.

    `analyze-and-test.md`, "Baselines, comparisons and regression": a comparison
    holds the question set constant.
    Printing a delta between two different cohorts would be the apples-to-oranges
    figure the baseline section exists to refuse, so this is raised by name and
    the two fingerprints travel on it.
    """

    def __init__(self, before: str | None, after: str | None):
        super().__init__(
            "these two runs judged different question sets, so their delta would "
            f"compare two cohorts: before={before!r} after={after!r}. Open the "
            "after-run on the same Golden Question versions as the before-run."
        )
        self.before = before
        self.after = after


class EnvironmentDrift(ValueError):
    """Two runs whose environment moved are not a before/after of one repair.

    `analyze-and-test.md`, "The before/after protocol": the seven environment
    pins are held constant, and what changes is only what the rails of 75-1,
    75-3, 75-4 and 75-5 wrote. A delta over two different Semantic View versions
    or two different models measures the change of environment and calls it a
    repair -- which is the one thing the epic's artefact must not be.
    """

    def __init__(self, differences: list[dict[str, Any]]):
        named = ", ".join(str(item["pin"]) for item in differences)
        super().__init__(
            "these two runs did not hold their environment constant, so their delta would "
            f"measure the environment and not the repair: {named}. Open the after-run with "
            "the before-run's pins (same Semantic View version, Context Version Set, model, "
            "host capability profile, tool catalog version, data snapshot and as_of), or "
            "read the pins that were not read."
        )
        self.differences = differences


def compare_runs(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """The before/after delta: pass counts per dimension, case counts per class.

    Both arguments are the shape `classification_report` returns. Refuses two
    runs whose `question_set_fingerprint` differs -- and refuses two runs that
    have none, because an absent fingerprint is not proof of agreement. Refuses,
    the same way and for the same reason, two runs whose seven environment pins
    are not identical: the question set held constant and the environment held
    constant are the two halves of one sentence.
    """
    before_fp = before.get("question_set_fingerprint")
    after_fp = after.get("question_set_fingerprint")
    if not before_fp or not after_fp or before_fp != after_fp:
        raise FingerprintMismatch(before_fp, after_fp)

    drift = environment_drift(before.get("environment"), after.get("environment"))
    if drift:
        raise EnvironmentDrift(drift)

    dimensions: dict[str, dict[str, int]] = {}
    for dimension in DIMENSIONS:
        row: dict[str, int] = {}
        for verdict in VERDICTS:
            was = (before["dimension_counts"].get(dimension) or {}).get(verdict, 0)
            now = (after["dimension_counts"].get(dimension) or {}).get(verdict, 0)
            row[f"{verdict}_before"] = was
            row[f"{verdict}_after"] = now
            row[f"{verdict}_delta"] = now - was
        dimensions[dimension] = row

    classes: dict[str, dict[str, int]] = {}
    for name in RULE_ORDER:
        was = before["class_counts"].get(name, 0)
        now = after["class_counts"].get(name, 0)
        classes[name] = {"before": was, "after": now, "delta": now - was}

    return {
        "question_set_fingerprint": before_fp,
        "before_run_id": before.get("run_id"),
        "after_run_id": after.get("run_id"),
        "case_count_before": before.get("case_count"),
        "case_count_after": after.get("case_count"),
        # The environment BOTH runs held: printed, so a reader of the delta sees
        # what was constant instead of taking it on trust.
        "environment": environment_pins(before.get("environment")),
        "dimensions": dimensions,
        "classes": classes,
    }


def classification_report(
    *,
    run_id: str,
    project_id: str | None,
    lifecycle: str,
    question_set_fingerprint: str | None,
    cases: list[Mapping[str, Any]],
    environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The whole report for one finalized run, from its loaded cases.

    Refuses a lifecycle other than `finalized` HERE rather than in each caller:
    the script and the gate both build their report through this function, so
    neither can be the one that forgot.
    """
    if lifecycle != LIFECYCLE_FINALIZED:
        raise RunNotFinalized(run_id, lifecycle)
    classified = classify_run(cases)
    return {
        "schema_version": "evaluation-failure-classification.v1",
        "run_id": run_id,
        "project_id": project_id,
        "lifecycle": lifecycle,
        "question_set_fingerprint": question_set_fingerprint,
        "environment": environment_pins(environment),
        "case_count": len(cases),
        "rule_order": list(RULE_ORDER),
        "cases": classified,
        "class_counts": class_counts(classified),
        "dimension_counts": dimension_counts(cases),
    }
