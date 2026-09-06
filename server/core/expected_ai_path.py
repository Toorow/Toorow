"""Expected AI Path: the grammar, and the comparison against what was observed.

Story 51.3, AC6 and AC7. This module is the piece the first delivery of Epic 51
left open and said so: every case carrying an observed path resolved to
`unverifiable / path_comparison_not_delivered`, which was honest and useless.

WHAT IT COMPARES, AND WHAT IT REFUSES TO COMPARE
------------------------------------------------
`analyze-and-test.md:268-269`: Test compares an expected path **pattern** with
the immutable observed AI Path -- "not two prose traces and not hidden
chain-of-thought". Nothing here reads model reasoning. Migration
`150_ai_path_observed_evidence.sql` has no column for it and this module adds
none; the grammar schema has no field for it either.

The observed side is `app.ai_paths` + `app.ai_path_steps` ordered by
`ai_path_steps.ordinal`, assessed against the `policy_snapshot` **pinned on the
header before the execution** (migration 150:51-56), never against today's
policy. That is the whole point of pinning it: a policy that moved after the
fact would silently re-judge an execution that already happened.

FIVE FINDINGS, REPORTED SEPARATELY AND NEVER MERGED
---------------------------------------------------
`analyze-and-test.md:275` requires that version mismatches, missing path
evidence and unavailable traces be "reported separately". So `compare()` returns
five distinct lists and never a count, a score or a ratio:

    missing_required_nodes / observed_forbidden_nodes / order_violations
    version_mismatches / missing_path_evidence

A version mismatch is a `fail`, not a missing node: the node WAS there, at the
wrong pinned version, and collapsing the two would let a real regression read as
an absence somebody could dismiss as instrumentation noise.

THE ASYMMETRY THAT MATTERS
--------------------------
Missing or unfinalized path evidence is `unverifiable`, NEVER `pass` and never
`fail` (`analyze-and-test.md:284`, and AC7). An absent trace says nothing about
the path that was taken -- treating it as a pass launders the gap, and treating
it as a failure invents a defect. The database holds the same line from the
other side: `record_case_verdicts` refuses `path_quality = pass` on a case whose
observed path reference is NULL.

EXTRA STEPS ARE NOT DEFECTS BY DEFAULT
--------------------------------------
`analyze-and-test.md:279-280`: an additional step matters only when it violates
an explicit cost, retry, safety or capability rule declared in the pattern. This
module therefore has no "strict" or "exact sequence" mode, and the grammar has
no flag for one. A pattern that declares no rule cannot report an extra step,
which is why a legitimate alternative call sequence is not penalized.

ONE DIALECT, AND ONE TRANSLATOR FOR THE ONE THAT PRECEDED IT
------------------------------------------------------------
Story 51.1 wrote patterns in a second vocabulary -- `forbidden_tools`, order
constraints as integer INDICES into `required_nodes`, nodes carrying the flat
`app.ai_path_steps` fields and no `key` -- while this module has always read the
grammar of `schemas/expected-ai-path.schema.json`. Nothing translated between
them, so the first Golden Question with a required node raised `KeyError: 'key'`
inside `record_case_verdicts`, and a `forbidden_tools` entry could never fail a
case at all: `compare()` does not read that field, so a pattern whose only
statement was a forbidden tool passed by vacuity.

The grammar wins, because it is the half that is versioned: `grammar_version` is
stored WITH the pattern so a later widening of the schema cannot silently
re-interpret an old Golden Question version. A pattern stored in the authoring
vocabulary carries no such pin, and would be re-read by whatever projector
happened to be deployed the day it was compared.

So there is exactly one translator, `canonicalize()`, and it runs in two places
only: at the door (`golden_questions._validate_expected_ai_path`, so what is
STORED is the grammar) and at `load_expected_pattern`, so a pattern written by
the deployment that preceded this repair is still comparable. It never runs
between two live writers, and no inverse translator exists -- the grammar is
what the API accepts, stores, returns and compares.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "expected-ai-path.schema.json"

GRAMMAR_VERSION = 1

# Verdicts. Imported by name rather than re-declared, so the six dimensions and
# this module cannot drift into two vocabularies.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNVERIFIABLE = "unverifiable"

# Reason codes. Each one names WHY, so a reader never has to guess which of the
# five findings produced the verdict.
REASON_PATH_MATCHES = "path_matches_expected_pattern"
REASON_MISSING_REQUIRED_NODE = "missing_required_node"
REASON_FORBIDDEN_NODE_OBSERVED = "forbidden_node_observed"
REASON_ORDER_VIOLATION = "order_violation"
REASON_VERSION_MISMATCH = "version_mismatch"
REASON_EXTRA_STEP_RULE_VIOLATED = "extra_step_rule_violated"
REASON_NO_EXPECTED_PATTERN = "no_expected_ai_path_declared"
REASON_PATH_EVIDENCE_MISSING = "path_evidence_missing"
REASON_PATH_NOT_FINALIZED = "path_evidence_not_finalized"
REASON_PATH_UNAVAILABLE = "path_evidence_unavailable"

# An observed path is usable evidence only in these states. `unavailable` is the
# outcome migration 150 writes when instrumentation could not capture the run --
# it is evidence OF AN ABSENCE, not evidence of a path.
_USABLE_OUTCOMES = frozenset({"succeeded", "failed", "refused"})
_FINALIZED = "finalized"


class ExpectedPathInvalid(ValueError):
    """A pattern that does not satisfy the grammar. Raised at WRITE time.

    Refusing late would mean storing a Golden Question version whose expected
    path can never be evaluated -- a question that looks specified and measures
    nothing.
    """

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message)
        self.message = message
        self.path = path

    def as_refusal(self) -> dict[str, str | None]:
        return {"code": "expected_ai_path_invalid", "message": self.message, "subject": self.path}


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def node_key(step: dict[str, Any]) -> str:
    """The stable address of a governed object, from an observed step row.

    The object type is the column's word (AI-376, F2): `schema_doc` written by an
    author addresses the same node as the stored `schema-doc`.
    """
    from core.candidate_emission import owner_object_type_for  # noqa: PLC0415

    return "/".join(
        (
            str(step.get("owner_workspace") or ""),
            str(owner_object_type_for(str(step.get("owner_object_type"))) if step.get("owner_object_type") else ""),
            str(step.get("owner_object_id") or ""),
        )
    )


#: The name given to the single alternatives group a projected pattern carries.
#: The authoring vocabulary had a FLAT list of accepted paths with no grouping,
#: and the grammar groups branches so that "any one of these passes" is a
#: statement about a named choice rather than about the whole pattern.
ALTERNATIVE_GROUP_NAME = "declared alternative paths"


def _node_from_authoring(node: dict[str, Any]) -> dict[str, Any]:
    """One node, from the flat `app.ai_path_steps` vocabulary into the grammar.

    The key is built by `node_key` -- the SAME function the observed side uses --
    applied to a dict carrying the same three field names. So a projected node
    addresses a step exactly as the comparison addresses it, including the
    degenerate `//` that an owner-less expected node and an owner-less observed
    step both produce: such a node matches owner-less steps and nothing else,
    which is what it was written to mean.
    """
    projected: dict[str, Any] = {"key": node_key(node)}
    if node.get("step_kind"):
        projected["step_kind"] = node["step_kind"]
    if node.get("owner_version_id"):
        projected["owner_version_id"] = node["owner_version_id"]
    if node.get("skill_version_id") and node.get("skill_step_id"):
        projected["skill_pin"] = {
            "skill_version_id": node["skill_version_id"],
            "skill_step_id": node["skill_step_id"],
        }
    if node.get("tool_name"):
        projected["tool"] = {"tool_name": node["tool_name"]}
    return projected


def _key_at(nodes: list[dict[str, Any]], index: Any) -> str | None:
    """The key an order constraint's integer index pointed at, or None."""
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if not 0 <= index < len(nodes):
        return None
    return nodes[index]["key"]


def canonicalize(pattern: Any) -> Any:
    """Project the authoring vocabulary of Story 51.1 onto the versioned grammar.

    Total and lenient by design. At WRITE time `validate_pattern` is the gate
    that runs immediately after it, so nothing malformed survives; at READ time a
    pattern already stored must be compared as well as the grammar allows rather
    than raise inside the six-or-none verdict writer, where the failure would
    read as an evaluation defect instead of a storage one.

    A pattern that already carries `grammar_version` is returned untouched: the
    projection is one-way, and a stored grammar is never re-projected.
    """
    if not isinstance(pattern, dict) or "grammar_version" in pattern:
        return pattern

    nodes = [
        _node_from_authoring(node)
        for node in (pattern.get("required_nodes") or [])
        if isinstance(node, dict)
    ]
    projected: dict[str, Any] = {"grammar_version": GRAMMAR_VERSION, "required_nodes": nodes}

    # A forbidden TOOL is a forbidden node named by its tool and nothing else --
    # the grammar allows exactly that (`forbiddenNode` needs one property, not a
    # key), so no expressiveness is lost and the field finally gets a reader.
    forbidden = [
        {"tool_name": tool.strip()}
        for tool in (pattern.get("forbidden_tools") or [])
        if isinstance(tool, str) and tool.strip()
    ]
    if forbidden:
        projected["forbidden_nodes"] = forbidden

    order: list[dict[str, Any]] = []
    for constraint in pattern.get("order_constraints") or []:
        if not isinstance(constraint, dict):
            continue
        before = _key_at(nodes, constraint.get("before"))
        after = _key_at(nodes, constraint.get("after"))
        if before is None or after is None:
            continue
        order.append({"before": before, "after": after})
    if order:
        projected["order_constraints"] = order

    branches: list[dict[str, Any]] = []
    for index, alternative in enumerate(pattern.get("alternative_paths") or []):
        if not isinstance(alternative, dict):
            continue
        branch_nodes = [
            _node_from_authoring(node)
            for node in (alternative.get("required_nodes") or [])
            if isinstance(node, dict)
        ]
        if not branch_nodes:
            continue
        label = alternative.get("label")
        branches.append(
            {
                "name": label.strip()
                if isinstance(label, str) and label.strip()
                else f"alternative {index + 1}",
                "required_nodes": branch_nodes,
            }
        )
    if branches:
        projected["alternatives"] = [{"name": ALTERNATIVE_GROUP_NAME, "branches": branches}]

    return projected


def validate_pattern(pattern: Any) -> dict[str, Any]:
    """Validate against the versioned grammar, then the two cross-field rules.

    JSON Schema cannot express either of the rules below, so they are checked
    here rather than left to a reader's goodwill.
    """
    if pattern is None:
        raise ExpectedPathInvalid("an expected AI Path pattern is required", "expected_ai_path")
    if not isinstance(pattern, dict):
        raise ExpectedPathInvalid("the expected AI Path must be an object", "expected_ai_path")

    errors = sorted(_validator().iter_errors(pattern), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        location = "expected_ai_path" + "".join(f"[{part!r}]" for part in first.absolute_path)
        raise ExpectedPathInvalid(first.message, location)

    declared = {node["key"] for node in pattern.get("required_nodes", [])}

    # Rule 1. An order constraint over a node nobody requires can never be
    # violated, so it would read as permanently satisfied -- a constraint that
    # asserts nothing is worse than no constraint, because it looks like one.
    for index, constraint in enumerate(pattern.get("order_constraints", [])):
        for side in ("before", "after"):
            if constraint[side] not in declared:
                raise ExpectedPathInvalid(
                    f"order constraint names `{constraint[side]}`, which is not in required_nodes: "
                    "a constraint over a node nobody requires can never be violated",
                    f"expected_ai_path['order_constraints'][{index}]['{side}']",
                )
        if constraint["before"] == constraint["after"]:
            raise ExpectedPathInvalid(
                "an order constraint cannot place a node before itself",
                f"expected_ai_path['order_constraints'][{index}]",
            )

    # Rule 2. A node cannot be required and forbidden at once. Left unchecked,
    # the case fails and misses at the same time and neither finding is true.
    forbidden_keys = {node["key"] for node in pattern.get("forbidden_nodes", []) if node.get("key")}
    both = sorted(declared & forbidden_keys)
    if both:
        raise ExpectedPathInvalid(
            f"these nodes are both required and forbidden: {', '.join(both)}",
            "expected_ai_path['forbidden_nodes']",
        )

    return pattern


#: Every construct through which a pattern can assert something. A pattern in
#: which all of them are empty states nothing about the path that was taken.
_ASSERTING_CONSTRUCTS = (
    "required_nodes",
    "forbidden_nodes",
    "order_constraints",
    "alternatives",
    "extra_step_rules",
)


def declares_expectations(pattern: Any) -> bool:
    """Does this pattern assert anything at all?

    `bool(pattern)` is not the question, and reading it as one was a laundering
    machine: `{"grammar_version": 1, "required_nodes": []}` is truthy, declares
    nothing, and would make `compare()` find no finding and `path_verdict` return
    `pass / path_matches_expected_pattern` for ANY observed path. A pattern that
    asserts nothing must read as `unverifiable / no_expected_ai_path_declared` --
    the same asymmetry AC7 applies to missing evidence, applied to a missing
    specification.
    """
    if not isinstance(pattern, dict):
        return False
    return any(pattern.get(construct) for construct in _ASSERTING_CONSTRUCTS)


def _version_of(step: dict[str, Any]) -> str | None:
    return step.get("owner_version_id")


def _match_node(expected: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Find the observed step satisfying one expected node.

    Returns one of three outcomes, and the distinction is the point:

      {"found": step}                     -- satisfied
      {"mismatch": {...}}                 -- present at the WRONG pin (a fail)
      {}                                  -- absent (a missing node)

    A node present at the wrong version is NOT reported as missing. Collapsing
    the two would let a version regression be read as an instrumentation gap.
    """
    key = expected["key"]
    candidates = [s for s in steps if node_key(s) == key]
    if expected.get("step_kind"):
        candidates = [s for s in candidates if s.get("step_kind") == expected["step_kind"]]
    if not candidates:
        return {}

    pins: list[tuple[str, Any]] = []
    if expected.get("owner_version_id"):
        pins.append(("owner_version_id", expected["owner_version_id"]))
    skill = expected.get("skill_pin") or {}
    if skill:
        pins.append(("skill_version_id", skill["skill_version_id"]))
        pins.append(("skill_step_id", skill["skill_step_id"]))
    tool = expected.get("tool") or {}
    if tool.get("tool_name"):
        pins.append(("tool_name", tool["tool_name"]))

    if not pins:
        return {"found": candidates[0]}

    for step in candidates:
        if all(step.get(field) == value for field, value in pins):
            return {"found": step}

    # Present, but no candidate carries every pin. Report the first candidate's
    # actual values against the expected ones -- naming both sides is what makes
    # the finding actionable instead of merely true.
    observed = candidates[0]
    return {
        "mismatch": {
            "key": key,
            "expected": dict(pins),
            "observed": {field: observed.get(field) for field, _ in pins},
            "step_ordinal": observed.get("ordinal"),
        }
    }


def _column_word(object_type: Any) -> str | None:
    """The column's word for an author's object type (AI-376): `schema_doc` -> `schema-doc`."""
    if object_type is None:
        return None
    from core.candidate_emission import owner_object_type_for  # noqa: PLC0415

    return owner_object_type_for(str(object_type))


def _column_key(key: Any) -> Any:
    """An author's `workspace/type/id` address with its type as the column's word."""
    if not isinstance(key, str) or key.count("/") != 2:
        return key
    workspace, object_type, object_id = key.split("/")
    return "/".join((workspace, _column_word(object_type) or "", object_id))


def _forbidden_hits(pattern: dict[str, Any], steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # THE FORBIDDEN SIDE SPEAKS THE COLUMN'S WORD TOO (AI-376, Opus N1): a rule
    # written as `schema_doc` compared raw against the stored `schema-doc` never
    # fired -- and a forbidden rule that never fires reads as `pass`, silently.
    hits: list[dict[str, Any]] = []
    for rule in pattern.get("forbidden_nodes", []):
        for step in steps:
            if rule.get("key") and node_key(step) != _column_key(rule["key"]):
                continue
            if rule.get("owner_workspace") and step.get("owner_workspace") != rule[
                "owner_workspace"
            ]:
                continue
            if rule.get("owner_object_type") and _column_word(step.get("owner_object_type")) != _column_word(
                rule["owner_object_type"]
            ):
                continue
            if rule.get("tool_name") and step.get("tool_name") != rule["tool_name"]:
                continue
            hits.append(
                {
                    "rule": {k: v for k, v in rule.items() if k != "reason"},
                    "reason": rule.get("reason"),
                    "step_ordinal": step.get("ordinal"),
                    "observed_key": node_key(step),
                }
            )
    return hits


def _order_violations(
    pattern: dict[str, Any], steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Prerequisites, judged on FIRST occurrence.

    A step repeated later does not retroactively satisfy a prerequisite: "inspect
    the context before querying" is about the first query, not the second.
    """
    first_seen: dict[str, int] = {}
    for step in steps:
        first_seen.setdefault(node_key(step), int(step.get("ordinal") or 0))

    violations = []
    for constraint in pattern.get("order_constraints", []):
        before, after = constraint["before"], constraint["after"]
        if before not in first_seen or after not in first_seen:
            # A prerequisite over a node that never ran is a MISSING NODE, and it
            # is already reported as one. Reporting it twice would make one
            # defect look like two.
            continue
        if first_seen[before] >= first_seen[after]:
            violations.append(
                {
                    "before": before,
                    "after": after,
                    "before_ordinal": first_seen[before],
                    "after_ordinal": first_seen[after],
                    "reason": constraint.get("reason"),
                }
            )
    return violations


def _alternative_outcomes(
    pattern: dict[str, Any], steps: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One satisfied branch is enough; report the group only if none is.

    `analyze-and-test.md:277-278`: declared alternative paths can pass, and a
    different but allowed call sequence is not penalized.
    """
    unsatisfied: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for group in pattern.get("alternatives", []):
        branch_reports = []
        for branch in group["branches"]:
            missing, wrong = [], []
            for node in branch["required_nodes"]:
                outcome = _match_node(node, steps)
                if "found" in outcome:
                    continue
                (wrong if "mismatch" in outcome else missing).append(
                    outcome.get("mismatch", {"key": node["key"]})
                )
            branch_reports.append({"name": branch["name"], "missing": missing, "mismatches": wrong})
        if any(not r["missing"] and not r["mismatches"] for r in branch_reports):
            continue
        unsatisfied.append({"alternative": group["name"], "branches": branch_reports})
        for report in branch_reports:
            mismatches.extend(report["mismatches"])
    return unsatisfied, mismatches


def _extra_step_violations(
    pattern: dict[str, Any], steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    violations = []
    for rule in pattern.get("extra_step_rules", []):
        scoped = (
            [s for s in steps if node_key(s) == rule["applies_to"]]
            if rule.get("applies_to")
            else steps
        )
        if len(scoped) > rule["limit"]:
            violations.append(
                {
                    "kind": rule["kind"],
                    "limit": rule["limit"],
                    "observed": len(scoped),
                    "applies_to": rule.get("applies_to"),
                    "reason": rule.get("reason"),
                }
            )
    return violations


def evidence_absence(header: dict[str, Any] | None) -> dict[str, Any] | None:
    """Why the observed path cannot be compared -- or None when it can.

    Three distinct absences, kept distinct because they have three different
    owners: no path at all is instrumentation; an unfinalized path is a run still
    in flight; `outcome='unavailable'` is instrumentation that ran and could not
    capture. Merging them would send every one of them to the wrong person.
    """
    if not header:
        return {
            "reason_code": REASON_PATH_EVIDENCE_MISSING,
            "detail": "no observed AI Path is pinned on this case",
        }
    if header.get("lifecycle") != _FINALIZED:
        return {
            "reason_code": REASON_PATH_NOT_FINALIZED,
            "detail": f"the observed AI Path is `{header.get('lifecycle')}`, not `{_FINALIZED}`",
        }
    if header.get("outcome") not in _USABLE_OUTCOMES:
        return {
            "reason_code": REASON_PATH_UNAVAILABLE,
            "detail": f"the observed AI Path outcome is `{header.get('outcome')}`",
        }
    return None


def compare(
    pattern: dict[str, Any] | None,
    header: dict[str, Any] | None,
    steps: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """The five findings, separate, plus the policy actually in force.

    `policy_snapshot` is echoed from the HEADER, never re-read from today's
    configuration: migration 150 pins it before the execution precisely so that a
    later policy change cannot re-judge a path that already ran.
    """
    absence = evidence_absence(header)
    if absence is not None:
        return {
            "comparable": False,
            "missing_path_evidence": [absence],
            "missing_required_nodes": [],
            "observed_forbidden_nodes": [],
            "order_violations": [],
            "version_mismatches": [],
            "unsatisfied_alternatives": [],
            "extra_step_violations": [],
            "policy_snapshot_hash": (header or {}).get("policy_snapshot_hash"),
        }

    ordered = sorted(steps or [], key=lambda s: int(s.get("ordinal") or 0))
    pattern = pattern or {}

    missing, mismatches = [], []
    for node in pattern.get("required_nodes", []):
        outcome = _match_node(node, ordered)
        if "found" in outcome:
            continue
        if "mismatch" in outcome:
            mismatches.append(outcome["mismatch"])
        else:
            missing.append({"key": node["key"], "step_kind": node.get("step_kind")})

    unsatisfied, alt_mismatches = _alternative_outcomes(pattern, ordered)
    mismatches.extend(alt_mismatches)

    # The tool catalog is pinned on the HEADER, not per step: it is the catalog
    # that was available during the execution, which is what the pattern asserts.
    catalog = (header or {}).get("tool_catalog_version")
    for node in pattern.get("required_nodes", []):
        expected_catalog = (node.get("tool") or {}).get("tool_catalog_version")
        if expected_catalog and expected_catalog != catalog:
            mismatches.append(
                {
                    "key": node["key"],
                    "expected": {"tool_catalog_version": expected_catalog},
                    "observed": {"tool_catalog_version": catalog},
                    "step_ordinal": None,
                }
            )

    return {
        "comparable": True,
        "missing_path_evidence": [],
        "missing_required_nodes": missing,
        "observed_forbidden_nodes": _forbidden_hits(pattern, ordered),
        "order_violations": _order_violations(pattern, ordered),
        "version_mismatches": mismatches,
        "unsatisfied_alternatives": unsatisfied,
        "extra_step_violations": _extra_step_violations(pattern, ordered),
        "policy_snapshot_hash": (header or {}).get("policy_snapshot_hash"),
        "observed_step_count": len(ordered),
    }


def path_verdict(
    comparison: dict[str, Any], *, has_pattern: bool
) -> dict[str, Any]:
    """One verdict from the five findings -- and never a score.

    Order matters and encodes the asymmetry of AC7:

    1. no comparable evidence  -> `unverifiable`. Never `fail`: an absent trace
       says nothing about the path that was taken, so calling it a failure
       invents a defect that may not exist.
    2. no declared pattern     -> `unverifiable`. A path nobody specified cannot
       be adhered to, and `pass` here would be the launder this whole module
       exists to prevent.
    3. any finding             -> `fail`, with EVERY reason code that fired, not
       the first. A case failing on three counts must not read as failing on one.
    4. otherwise               -> `pass`.
    """
    if not comparison.get("comparable"):
        absence = (comparison.get("missing_path_evidence") or [{}])[0]
        return {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": absence.get("reason_code", REASON_PATH_EVIDENCE_MISSING),
            "evidence_refs": {"comparison": comparison},
        }

    if not has_pattern:
        return {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": REASON_NO_EXPECTED_PATTERN,
            "evidence_refs": {"comparison": comparison},
        }

    fired = [
        code
        for code, findings in (
            (REASON_MISSING_REQUIRED_NODE, comparison["missing_required_nodes"]),
            (REASON_FORBIDDEN_NODE_OBSERVED, comparison["observed_forbidden_nodes"]),
            (REASON_ORDER_VIOLATION, comparison["order_violations"]),
            (REASON_VERSION_MISMATCH, comparison["version_mismatches"]),
            (REASON_MISSING_REQUIRED_NODE, comparison["unsatisfied_alternatives"]),
            (REASON_EXTRA_STEP_RULE_VIOLATED, comparison["extra_step_violations"]),
        )
        if findings
    ]
    if fired:
        ordered_unique = list(dict.fromkeys(fired))
        return {
            "verdict": VERDICT_FAIL,
            "reason_code": ordered_unique[0],
            "evidence_refs": {"reason_codes": ordered_unique, "comparison": comparison},
        }

    return {
        "verdict": VERDICT_PASS,
        "reason_code": REASON_PATH_MATCHES,
        "evidence_refs": {"comparison": comparison},
    }


# ---------------------------------------------------------------------------
# Reading the two sides out of the database.
# ---------------------------------------------------------------------------


def load_observed_path(
    conn, *, org_id: str, project_id: str, ai_path_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Header + steps, Project-scoped on both, ordered by `ordinal`.

    The scope is on BOTH queries and not only the header: a steps query keyed on
    `path_id` alone would cross Projects the moment a caller passed an id it did
    not own, and RLS is the floor, not the only door.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, lifecycle, outcome, model_ref, tool_catalog_version,
                   policy_snapshot, policy_snapshot_hash
              FROM app.ai_paths
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (ai_path_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None, []
        header = {
            "id": row[0],
            "lifecycle": row[1],
            "outcome": row[2],
            "model_ref": row[3],
            "tool_catalog_version": row[4],
            "policy_snapshot": row[5],
            "policy_snapshot_hash": row[6],
        }

        cur.execute(
            """
            SELECT ordinal, step_kind, owner_workspace, owner_object_type,
                   owner_object_id, owner_version_id, skill_version_id,
                   skill_step_id, tool_name, outcome
              FROM app.ai_path_steps
             WHERE path_id = %s AND org_id = %s AND project_id = %s
             ORDER BY ordinal
            """,
            (ai_path_id, org_id, project_id),
        )
        columns = (
            "ordinal", "step_kind", "owner_workspace", "owner_object_type",
            "owner_object_id", "owner_version_id", "skill_version_id",
            "skill_step_id", "tool_name", "outcome",
        )
        steps = [dict(zip(columns, values)) for values in cur.fetchall()]

    return header, steps


def load_expected_pattern(
    conn, *, org_id: str, project_id: str, golden_question_version_id: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT expected_ai_path
              FROM app.golden_question_versions
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (golden_question_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        return None
    # The one read-side use of the translator. A version written since this
    # repair already carries `grammar_version` and passes through untouched; one
    # written before it is projected here rather than handed raw to `compare`,
    # which is how `KeyError: 'key'` used to surface inside `record_case_verdicts`
    # at the exact moment both sides of the seam finally existed.
    return canonicalize(row[0])


def evaluate_case_path(
    conn, *, org_id: str, project_id: str, case: dict[str, Any]
) -> dict[str, Any]:
    """The `path_quality` verdict for one case, from both sides of the seam.

    Returns the same shape the six-dimension writer expects, so it drops into
    `record_case_verdicts` without that function learning anything about paths.
    """
    ai_path_id = case.get("ai_path_id")
    header, steps = (
        load_observed_path(conn, org_id=org_id, project_id=project_id, ai_path_id=ai_path_id)
        if ai_path_id
        else (None, [])
    )

    pattern = None
    if case.get("golden_question_version_id"):
        pattern = load_expected_pattern(
            conn,
            org_id=org_id,
            project_id=project_id,
            golden_question_version_id=case["golden_question_version_id"],
        )

    comparison = compare(pattern, header, steps)
    verdict = path_verdict(comparison, has_pattern=declares_expectations(pattern))
    verdict["evidence_refs"]["observed_ai_path_id"] = ai_path_id
    verdict["evidence_refs"]["golden_question_version_id"] = case.get("golden_question_version_id")
    return verdict
