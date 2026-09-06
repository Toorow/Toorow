"""Story 51.3 AC6/AC7 -- the expected AI Path grammar and its comparison.

What these tests defend, in one line each:

  * a malformed pattern is refused at WRITE time, not discovered at read time;
  * a missing trace is `unverifiable`, never `pass` and never `fail`;
  * a node at the wrong version is a FAIL, and is never reported as missing;
  * an extra step is not a defect unless a declared rule says so;
  * one satisfied alternative branch is enough.

The comparison tests are pure: they hand `compare()` the two sides directly, so
they prove the logic rather than a fixture. The last section is pg-gated and
goes through the real database, because a rule the database also enforces must
be proved against the database -- a mock cannot refuse an INSERT.
"""

from __future__ import annotations

import json
import os
import pathlib

import jsonschema
import pytest
from core.expected_ai_path import (
    GRAMMAR_VERSION,
    REASON_EXTRA_STEP_RULE_VIOLATED,
    REASON_FORBIDDEN_NODE_OBSERVED,
    REASON_MISSING_REQUIRED_NODE,
    REASON_NO_EXPECTED_PATTERN,
    REASON_ORDER_VIOLATION,
    REASON_PATH_EVIDENCE_MISSING,
    REASON_PATH_NOT_FINALIZED,
    REASON_PATH_UNAVAILABLE,
    REASON_VERSION_MISMATCH,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_UNVERIFIABLE,
    ExpectedPathInvalid,
    compare,
    path_verdict,
    validate_pattern,
)

SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "core"
    / "schemas"
    / "expected-ai-path.schema.json"
)

_SEMANTIC_VIEW = "governance/semantic-view/svw_EXAMPLE"
_CONTEXT_TOPIC = "context-hub/context-topic/ctx_EXAMPLE"
_DQ_CONTROL = "governance/control/ctl_EXAMPLE"


def _step(ordinal, key, **extra):
    workspace, object_type, object_id = key.split("/", 2)
    step = {
        "ordinal": ordinal,
        "step_kind": extra.pop("step_kind", "read"),
        "owner_workspace": workspace,
        "owner_object_type": object_type,
        "owner_object_id": object_id,
        "owner_version_id": None,
        "skill_version_id": None,
        "skill_step_id": None,
        "tool_name": None,
        "outcome": "succeeded",
    }
    step.update(extra)
    return step


def _header(**extra):
    header = {
        "id": "aip_EXAMPLE",
        "lifecycle": "finalized",
        "outcome": "succeeded",
        "model_ref": "claude-opus-5",
        "tool_catalog_version": "tc_2026_07_31",
        "policy_snapshot": {"allow": []},
        "policy_snapshot_hash": "sha256:example",
    }
    header.update(extra)
    return header


def _pattern(**extra):
    pattern = {"grammar_version": GRAMMAR_VERSION, "required_nodes": [{"key": _SEMANTIC_VIEW}]}
    pattern.update(extra)
    return pattern


# ===========================================================================
# AC6 -- the grammar
# ===========================================================================


def test_grammar_the_schema_is_a_valid_2020_12_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_grammar_accepts_all_six_constructs_in_one_pattern():
    """The six constructs of analyze-and-test.md:271-280, together.

    Separately each could pass while the combination is refused; the grammar has
    to hold them at once because a real expected path uses several.
    """
    validate_pattern(
        {
            "grammar_version": GRAMMAR_VERSION,
            "required_nodes": [
                {"key": _CONTEXT_TOPIC},
                {
                    "key": _SEMANTIC_VIEW,
                    "owner_version_id": "svv_0001",
                    "skill_pin": {"skill_version_id": "skv_0001", "skill_step_id": "step_query"},
                    "tool": {"tool_name": "query_semantic_view", "tool_catalog_version": "tc_1"},
                },
            ],
            "forbidden_nodes": [{"tool_name": "raw_sql", "reason": "ungoverned shortcut"}],
            "order_constraints": [{"before": _CONTEXT_TOPIC, "after": _SEMANTIC_VIEW}],
            "alternatives": [
                {
                    "name": "how the DQ state is read",
                    "branches": [
                        {"name": "via the control", "required_nodes": [{"key": _DQ_CONTROL}]},
                        {"name": "via the topic", "required_nodes": [{"key": _CONTEXT_TOPIC}]},
                    ],
                }
            ],
            "extra_step_rules": [{"kind": "retry", "limit": 3}],
        }
    )


@pytest.mark.parametrize("token", ["latest", "current", "head", "LATEST", "  Head  "])
def test_grammar_refuses_a_version_pin_that_follows_a_moving_target(token):
    with pytest.raises(ExpectedPathInvalid):
        validate_pattern(
            _pattern(required_nodes=[{"key": _SEMANTIC_VIEW, "owner_version_id": token}])
        )


def test_grammar_refuses_a_skill_version_without_its_step():
    """`ck_ai_path_steps_skill_pin` holds the same rule on the observed side.

    A Skill version without the step it ran cannot be compared against anything:
    the observed row always carries both or neither.
    """
    with pytest.raises(ExpectedPathInvalid):
        validate_pattern(
            _pattern(
                required_nodes=[{"key": _SEMANTIC_VIEW, "skill_pin": {"skill_version_id": "skv_1"}}]
            )
        )


def test_grammar_refuses_an_order_constraint_over_an_unrequired_node():
    """A constraint nobody can violate reads as permanently satisfied."""
    with pytest.raises(ExpectedPathInvalid) as caught:
        validate_pattern(
            _pattern(order_constraints=[{"before": _CONTEXT_TOPIC, "after": _SEMANTIC_VIEW}])
        )
    assert "can never be violated" in str(caught.value)


def test_grammar_refuses_a_node_that_is_required_and_forbidden_at_once():
    with pytest.raises(ExpectedPathInvalid) as caught:
        validate_pattern(_pattern(forbidden_nodes=[{"key": _SEMANTIC_VIEW}]))
    assert "both required and forbidden" in str(caught.value)


def test_grammar_refuses_an_alternative_with_a_single_branch():
    """`alternatives` with one branch is a required node wearing a costume."""
    with pytest.raises(ExpectedPathInvalid):
        validate_pattern(
            _pattern(
                alternatives=[
                    {
                        "name": "x",
                        "branches": [
                            {"name": "only", "required_nodes": [{"key": _DQ_CONTROL}]}
                        ],
                    }
                ]
            )
        )


def test_grammar_pins_its_own_version():
    """A stored pattern says which grammar it was written against.

    Without it, widening this schema later would silently re-interpret every
    Golden Question version already stored.
    """
    with pytest.raises(ExpectedPathInvalid):
        validate_pattern({"grammar_version": 99, "required_nodes": []})


def _declared_property_names() -> set[str]:
    """Every property NAME the grammar declares, at any depth.

    Reading the raw text instead was the first form of these two tests, and it
    was wrong in a way worth keeping a note about: it also matched the prose that
    EXPLAINS why the field is absent, so documenting the rule broke the test that
    enforces it. A guard must read the structure it guards.
    """
    names: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("properties", "$defs"):
                    names.update(str(k).lower() for k in value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return names


def test_grammar_has_no_field_for_model_reasoning():
    """analyze-and-test.md:269 -- not two prose traces, not chain-of-thought."""
    declared = _declared_property_names()
    for banned in ("reasoning", "chain_of_thought", "thought", "rationale", "rationale_text"):
        assert banned not in declared, f"the grammar must not declare a `{banned}` field"


def test_grammar_has_no_exact_sequence_mode():
    """An extra step is a defect only against a DECLARED rule (`:279-280`)."""
    declared = _declared_property_names()
    for banned in ("exact_sequence", "strict", "exact_order"):
        assert banned not in declared, f"the grammar must not declare a `{banned}` field"


# ===========================================================================
# AC7 -- the comparison. Missing evidence is never a pass.
# ===========================================================================


@pytest.mark.parametrize(
    "header,expected_reason",
    [
        (None, REASON_PATH_EVIDENCE_MISSING),
        (_header(lifecycle="recording"), REASON_PATH_NOT_FINALIZED),
        (_header(outcome="unavailable"), REASON_PATH_UNAVAILABLE),
    ],
)
def test_missing_evidence_is_unverifiable_never_pass_never_fail(header, expected_reason):
    verdict = path_verdict(compare(_pattern(), header, []), has_pattern=True)
    assert verdict["verdict"] == VERDICT_UNVERIFIABLE
    assert verdict["verdict"] not in (VERDICT_PASS, VERDICT_FAIL)
    assert verdict["reason_code"] == expected_reason


def test_the_three_absences_stay_three_and_are_not_merged():
    """Three absences, three owners: instrumentation, a run in flight, a capture
    failure. Merging them sends every one of them to the wrong person."""
    codes = {
        path_verdict(compare(_pattern(), header, []), has_pattern=True)["reason_code"]
        for header in (None, _header(lifecycle="recording"), _header(outcome="unavailable"))
    }
    assert len(codes) == 3


def test_no_declared_pattern_is_unverifiable_not_pass():
    """A path nobody specified cannot be adhered to."""
    verdict = path_verdict(compare(None, _header(), [_step(1, _SEMANTIC_VIEW)]), has_pattern=False)
    assert verdict["verdict"] == VERDICT_UNVERIFIABLE
    assert verdict["reason_code"] == REASON_NO_EXPECTED_PATTERN


def test_a_matching_path_passes():
    comparison = compare(_pattern(), _header(), [_step(1, _SEMANTIC_VIEW)])
    verdict = path_verdict(comparison, has_pattern=True)
    assert verdict["verdict"] == VERDICT_PASS


def test_a_missing_required_node_fails_and_names_the_node():
    comparison = compare(_pattern(), _header(), [_step(1, _CONTEXT_TOPIC)])
    assert comparison["missing_required_nodes"] == [{"key": _SEMANTIC_VIEW, "step_kind": None}]
    assert path_verdict(comparison, has_pattern=True)["verdict"] == VERDICT_FAIL


def test_version_mismatch_is_fail_and_is_never_reported_as_missing():
    """The node WAS there, at the wrong version. Reporting it as absent would let
    a real regression be dismissed as instrumentation noise."""
    pattern = _pattern(required_nodes=[{"key": _SEMANTIC_VIEW, "owner_version_id": "svv_0002"}])
    observed = [_step(1, _SEMANTIC_VIEW, owner_version_id="svv_0001")]
    comparison = compare(pattern, _header(), observed)

    assert comparison["missing_required_nodes"] == []
    assert len(comparison["version_mismatches"]) == 1
    mismatch = comparison["version_mismatches"][0]
    assert mismatch["expected"]["owner_version_id"] == "svv_0002"
    assert mismatch["observed"]["owner_version_id"] == "svv_0001"

    verdict = path_verdict(comparison, has_pattern=True)
    assert verdict["verdict"] == VERDICT_FAIL
    assert verdict["reason_code"] == REASON_VERSION_MISMATCH


def test_a_tool_catalog_mismatch_reads_the_version_pinned_on_the_header():
    pattern = _pattern(
        required_nodes=[
            {"key": _SEMANTIC_VIEW, "tool": {"tool_name": "q", "tool_catalog_version": "tc_OTHER"}}
        ]
    )
    steps = [_step(1, _SEMANTIC_VIEW, tool_name="q")]
    comparison = compare(pattern, _header(tool_catalog_version="tc_2026_07_31"), steps)
    assert any(
        m["expected"].get("tool_catalog_version") == "tc_OTHER"
        for m in comparison["version_mismatches"]
    )


def test_a_forbidden_node_fails_even_when_everything_required_is_present():
    pattern = _pattern(forbidden_nodes=[{"tool_name": "raw_sql", "reason": "ungoverned shortcut"}])
    steps = [_step(1, _SEMANTIC_VIEW), _step(2, _CONTEXT_TOPIC, tool_name="raw_sql")]
    comparison = compare(pattern, _header(), steps)
    assert comparison["missing_required_nodes"] == []
    assert len(comparison["observed_forbidden_nodes"]) == 1
    verdict = path_verdict(comparison, has_pattern=True)
    assert verdict["reason_code"] == REASON_FORBIDDEN_NODE_OBSERVED


def test_an_order_violation_is_judged_on_first_occurrence():
    """A step repeated later does not retroactively satisfy a prerequisite."""
    pattern = _pattern(
        required_nodes=[{"key": _CONTEXT_TOPIC}, {"key": _SEMANTIC_VIEW}],
        order_constraints=[{"before": _CONTEXT_TOPIC, "after": _SEMANTIC_VIEW}],
    )
    late = compare(pattern, _header(), [_step(1, _SEMANTIC_VIEW), _step(2, _CONTEXT_TOPIC)])
    assert len(late["order_violations"]) == 1
    assert path_verdict(late, has_pattern=True)["reason_code"] == REASON_ORDER_VIOLATION

    repeated = compare(
        pattern,
        _header(),
        [_step(1, _SEMANTIC_VIEW), _step(2, _CONTEXT_TOPIC), _step(3, _SEMANTIC_VIEW)],
    )
    assert len(repeated["order_violations"]) == 1, "a later repeat must not repair the order"


def test_an_order_constraint_over_an_absent_node_is_reported_once_as_missing():
    """One defect must not read as two."""
    pattern = _pattern(
        required_nodes=[{"key": _CONTEXT_TOPIC}, {"key": _SEMANTIC_VIEW}],
        order_constraints=[{"before": _CONTEXT_TOPIC, "after": _SEMANTIC_VIEW}],
    )
    comparison = compare(pattern, _header(), [_step(1, _SEMANTIC_VIEW)])
    assert len(comparison["missing_required_nodes"]) == 1
    assert comparison["order_violations"] == []


def test_one_satisfied_alternative_branch_is_enough():
    """analyze-and-test.md:277-278 -- an allowed different sequence is not penalized."""
    pattern = _pattern(
        alternatives=[
            {
                "name": "how DQ is read",
                "branches": [
                    {"name": "control", "required_nodes": [{"key": _DQ_CONTROL}]},
                    {"name": "topic", "required_nodes": [{"key": _CONTEXT_TOPIC}]},
                ],
            }
        ]
    )
    passing = compare(pattern, _header(), [_step(1, _SEMANTIC_VIEW), _step(2, _CONTEXT_TOPIC)])
    assert passing["unsatisfied_alternatives"] == []
    assert path_verdict(passing, has_pattern=True)["verdict"] == VERDICT_PASS

    failing = compare(pattern, _header(), [_step(1, _SEMANTIC_VIEW)])
    assert len(failing["unsatisfied_alternatives"]) == 1
    assert path_verdict(failing, has_pattern=True)["verdict"] == VERDICT_FAIL


def test_an_extra_step_is_not_a_defect_without_a_declared_rule():
    """The default is permissive on purpose (`:279-280`)."""
    steps = [_step(1, _SEMANTIC_VIEW)] + [_step(i, _CONTEXT_TOPIC) for i in range(2, 9)]
    comparison = compare(_pattern(), _header(), steps)
    assert comparison["extra_step_violations"] == []
    assert path_verdict(comparison, has_pattern=True)["verdict"] == VERDICT_PASS


def test_an_extra_step_fails_only_against_the_rule_that_declares_it():
    pattern = _pattern(
        extra_step_rules=[{"kind": "retry", "limit": 2, "applies_to": _CONTEXT_TOPIC}]
    )
    steps = [_step(1, _SEMANTIC_VIEW)] + [_step(i, _CONTEXT_TOPIC) for i in range(2, 6)]
    comparison = compare(pattern, _header(), steps)
    assert comparison["extra_step_violations"][0]["observed"] == 4
    verdict = path_verdict(comparison, has_pattern=True)
    assert verdict["reason_code"] == REASON_EXTRA_STEP_RULE_VIOLATED


def test_every_reason_code_that_fired_is_reported_not_only_the_first():
    """A case failing on three counts must not read as failing on one."""
    pattern = _pattern(
        required_nodes=[{"key": _SEMANTIC_VIEW}, {"key": _DQ_CONTROL}],
        forbidden_nodes=[{"tool_name": "raw_sql"}],
    )
    comparison = compare(pattern, _header(), [_step(1, _CONTEXT_TOPIC, tool_name="raw_sql")])
    verdict = path_verdict(comparison, has_pattern=True)
    codes = verdict["evidence_refs"]["reason_codes"]
    assert REASON_MISSING_REQUIRED_NODE in codes
    assert REASON_FORBIDDEN_NODE_OBSERVED in codes


def test_the_five_findings_stay_five_and_no_score_is_produced():
    """analyze-and-test.md:275 -- reported separately. And never aggregated."""
    comparison = compare(_pattern(), _header(), [_step(1, _SEMANTIC_VIEW)])
    for field in (
        "missing_required_nodes",
        "observed_forbidden_nodes",
        "order_violations",
        "version_mismatches",
        "missing_path_evidence",
    ):
        assert isinstance(comparison[field], list)
    banned = {"score", "ratio", "pass_rate", "adherence_pct", "confidence", "weighted"}
    assert not banned & set(comparison), "the comparison must not carry an aggregate"


def test_the_policy_snapshot_echoed_is_the_one_pinned_before_the_execution():
    """Migration 150:51-56 pins it so a later policy cannot re-judge the run."""
    header = _header(policy_snapshot_hash="sha256:pinned")
    comparison = compare(_pattern(), header, [_step(1, _SEMANTIC_VIEW)])
    assert comparison["policy_snapshot_hash"] == "sha256:pinned"


# ===========================================================================
# The rule the database also holds. A mock cannot refuse an INSERT.
# ===========================================================================

pg = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="pg-gated: needs TEST_POSTGRES_DSN (python scripts/disposable_postgres.py up)",
)


@pg
def test_pg_a_null_observed_path_cannot_carry_a_passing_path_quality():
    """AC7's last line, proved against the database rather than asserted.

    `record_case_verdicts` refuses it in Python; this checks the CHECK exists so
    a future caller that bypasses the service cannot write the row either.
    """
    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_get_constraintdef(oid)
                  FROM pg_constraint
                 WHERE conrelid = 'app.evaluation_case_dimension_verdicts'::regclass
                   AND contype = 'c'
                """
            )
            definitions = " ".join(row[0] for row in cur.fetchall()).lower()

    assert "path_quality" in definitions, (
        "no CHECK mentions path_quality: nothing stops a direct INSERT from "
        "writing `pass` on a case with no observed AI Path"
    )


# ---------------------------------------------------------------------------
# Story 45.7 -- un motif qui exige un pas de Skill devient comparable
# ---------------------------------------------------------------------------


_SKILL_NODE = "context-hub/procedure/proc_EXAMPLE"


def test_a_required_skill_step_now_matches_because_the_observed_side_can_carry_one():
    """AVANT 45.7 ce test etait impossible a ecrire honnetement.

    Le motif savait exiger un pas de Skill (`skill_pin`) depuis la story 51.3, et
    AUCUN emetteur ne produisait de `skill_step` : le cote observe ne pouvait pas
    en contenir, donc l'exigence etait STRUCTURELLEMENT manquante -- un `fail`
    qui ne disait rien du chemin reellement pris.
    """
    pattern = _pattern(
        required_nodes=[{
            "key": _SKILL_NODE,
            "step_kind": "skill_step",
            "skill_pin": {"skill_version_id": "proc_EXAMPLE@7", "skill_step_id": "2"},
        }]
    )
    observed = [
        _step(
            1,
            _SKILL_NODE,
            step_kind="skill_step",
            skill_version_id="proc_EXAMPLE@7",
            skill_step_id="2",
        ),
    ]

    comparison = compare(pattern, _header(), observed)

    assert comparison["comparable"] is True
    assert comparison["missing_required_nodes"] == []
    assert comparison["version_mismatches"] == []


def test_the_same_step_at_another_served_version_is_a_mismatch_not_an_absence():
    """Le pas ETAIT la, a la mauvaise version epinglee. Fondre les deux laisserait
    une regression de version se lire comme un trou d'instrumentation."""
    pattern = _pattern(
        required_nodes=[{
            "key": _SKILL_NODE,
            "step_kind": "skill_step",
            "skill_pin": {"skill_version_id": "proc_EXAMPLE@7", "skill_step_id": "2"},
        }]
    )
    observed = [
        _step(
            1,
            _SKILL_NODE,
            step_kind="skill_step",
            skill_version_id="proc_EXAMPLE@8",
            skill_step_id="2",
        ),
    ]

    comparison = compare(pattern, _header(), observed)

    assert comparison["missing_required_nodes"] == []
    assert len(comparison["version_mismatches"]) == 1


def test_a_forbidden_rule_written_in_the_detail_vocabulary_fires_on_the_stored_word():
    """AI-376 (Opus N1): `schema_doc` forbidden must catch a stored `schema-doc` step -- a forbidden
    rule that never fires reads as pass, silently."""
    from core.expected_ai_path import _forbidden_hits

    step = {"ordinal": 0, "owner_workspace": "context-hub", "owner_object_type": "schema-doc", "owner_object_id": "doc_1", "tool_name": "search_context"}
    assert len(_forbidden_hits({"forbidden_nodes": [{"owner_object_type": "schema_doc"}]}, [step])) == 1
    assert len(_forbidden_hits({"forbidden_nodes": [{"key": "context-hub/schema_doc/doc_1"}]}, [step])) == 1
    assert len(_forbidden_hits({"forbidden_nodes": [{"key": "context-hub/schema-doc/doc_1"}]}, [step])) == 1
    assert _forbidden_hits({"forbidden_nodes": [{"owner_object_type": "topic"}]}, [step]) == []
