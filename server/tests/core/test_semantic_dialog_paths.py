"""The console must call the path the server actually serves.

Found by `python scripts/screens.py wiring`, which lists endpoints the console
calls and no route answers. Three of the seven were the semantic change-set
paths: the dialogs posted to
`/api/projects/{id}/semantic-model/change-sets` while
`semantic_model_api._ROOT` is
`/api/projects/{project_id}/governance/semantic-model/change-sets`.

The missing `/governance` segment is why creating a Concept or a Semantic View
never worked. It also explains what looked like two separate defects: the
swallowed `prepare` was swallowing a 404, and the flat intent was never read
because the request reached no handler at all.

Asserted against `_ROOT` itself rather than a copied literal: a rename that
moves the route must break this test rather than silently orphan the console
again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core.semantic_model_api import _ROOT

DIALOGS = Path(__file__).resolve().parents[3] / "ui" / "admin" / "src" / "governance"
CALLERS = ("NewConceptDialog.tsx", "NewSemanticViewDialog.tsx")

#: Every dialog that opens a change set, prepares it and confirms it — on EITHER
#: store. Story 60.2 measured why this second tuple has to exist: the guard above
#: covered two files, and a third (`VisualMappingWorkbenchDialog.tsx`) carried the
#: exact prefixless address this module was written to forbid, undetected, while
#: a fourth (`CaseDecisionDialog.tsx`) carried the same false belief about the
#: confirmation token. A guard that covers the two files someone remembered is
#: how the third one keeps the defect.
#: `VisualMappingWorkbenchDialog.tsx` was REMOVED (AI-244, 2026-08-15): it opened
#: a second mapping store, which `governance.md:80-82` forbids, over three
#: connectors written into the component. Its name stays in the prose above
#: because the reason this tuple exists is what it taught.
CHANGE_SET_DIALOGS = (
    "NewConceptDialog.tsx",
    "NewSemanticViewDialog.tsx",
    "CaseDecisionDialog.tsx",
)

#: `_ROOT` as the console writes it: the project id is interpolated.
_FRONT_ROOT = _ROOT.replace("{project_id}", "${encodeURIComponent(projectId)}")


@pytest.mark.parametrize("dialog", CALLERS)
def test_the_dialog_posts_to_the_route_that_exists(dialog: str):
    source = (DIALOGS / dialog).read_text(encoding="utf-8")
    assert _FRONT_ROOT in source, (
        f"{dialog} does not call {_ROOT}; a change set posted anywhere else "
        f"reaches no handler and the screen cannot tell you why"
    )


@pytest.mark.parametrize("dialog", CALLERS)
def test_no_caller_keeps_the_prefixless_path(dialog: str):
    source = (DIALOGS / dialog).read_text(encoding="utf-8")
    # The exact shape that was dead: `/semantic-model/change-sets` NOT preceded
    # by `/governance`.
    assert "}/semantic-model/change-sets" not in source


@pytest.mark.parametrize("dialog", CALLERS)
def test_prepare_and_confirm_hang_from_the_same_root(dialog: str):
    source = (DIALOGS / dialog).read_text(encoding="utf-8")
    for step in ("prepare", "confirm"):
        assert f"{_FRONT_ROOT}/${{changeSet.change_set_id}}/{step}" in source


# ---------------------------------------------------------------------------
# Story 60.2 — the palette the browser offers is the allowlist the server enforces
#
# `ALLOWED_OPERATIONS` says why it is exported: "so the workbench builds its
# palette from the SAME list the server enforces, instead of a hand-kept copy
# that drifts". There was no copy and no palette: the dialog composed
# `{op: "formula"}`, an operation this contract has never had, so every metric
# it submitted was refused with `unknown_operation`.
#
# TypeScript cannot import a Python frozenset, so `formulaContract.ts` is a
# mirror. These tests are what makes a mirror honest: every value in the file is
# read back and compared to the module, so a drift breaks here rather than in
# front of a person.
# ---------------------------------------------------------------------------

import re  # noqa: E402

from core.semantic_expressions import (  # noqa: E402
    ADDITIVITY_CLASSES,
    AGGREGATION_FUNCTIONS,
    ALLOWED_OPERATIONS,
    ZERO_DENOMINATOR_POLICIES,
)

CONTRACT = DIALOGS / "formulaContract.ts"


def _string_array(source: str, name: str) -> list[str]:
    """The values of `export const NAME = [...] as const;` — read, not assumed."""
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", source, re.S)
    assert match, f"{name} is not declared in formulaContract.ts"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_every_operation_the_dialog_offers_is_one_the_server_accepts():
    source = CONTRACT.read_text(encoding="utf-8")
    offered = set(_string_array(source, "FORMULA_OPERATIONS"))
    offered |= set(_string_array(source, "OPERAND_OPERATIONS"))
    assert offered, "the dialog offers no operation at all"
    assert offered <= set(ALLOWED_OPERATIONS), (
        f"offered but not accepted: {sorted(offered - set(ALLOWED_OPERATIONS))}"
    )


def _code_only(source: str) -> str:
    """The file with its comments removed.

    Necessary rather than fastidious: the dialog's header now QUOTES the dead
    shape it used to send, because the header is where the measurement lives. A
    grep that cannot tell a quotation from an emission would force the fix to be
    undocumented in order to pass.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.lstrip().startswith("//")
    )


def test_the_dead_operation_is_gone_from_every_dialog():
    # `formula` is the one this dialog invented. It never reached a handler, so
    # every formula anyone typed was refused. Asserted on the dialogs themselves
    # rather than on the contract file, because the defect was a literal in the
    # request body.
    assert "formula" not in ALLOWED_OPERATIONS
    for dialog in CALLERS:
        code = _code_only((DIALOGS / dialog).read_text(encoding="utf-8"))
        assert 'op: "formula"' not in code
        assert '"op": "formula"' not in code


def test_the_dialog_never_offers_a_shape_that_cannot_be_published():
    # `concept_name` parses so the workbench can SHOW migrated formulas, and can
    # never be published (`ExpressionAnalysis.publishable`). Offering it in a
    # creation palette would be offering a dead end.
    source = CONTRACT.read_text(encoding="utf-8")
    offered = set(_string_array(source, "FORMULA_OPERATIONS")) | set(
        _string_array(source, "OPERAND_OPERATIONS")
    )
    assert "concept_name" not in offered


def test_the_aggregations_offered_are_the_ones_the_server_matches():
    # They are matched by exact string: lowercase, and `average` — not `avg`.
    # The dialog used to offer SUM/AVG/COUNT/COUNT_DISTINCT/MIN/MAX: six values,
    # six refusals.
    source = CONTRACT.read_text(encoding="utf-8")
    offered = _string_array(source, "AGGREGATION_FUNCTIONS")
    assert set(offered) == set(AGGREGATION_FUNCTIONS)
    assert "avg" not in offered


def test_the_three_additivity_classes_are_the_schemas_three():
    source = CONTRACT.read_text(encoding="utf-8")
    assert set(_string_array(source, "ADDITIVITY_CLASSES")) == set(ADDITIVITY_CLASSES)


def test_the_zero_denominator_policies_match_the_contract():
    source = CONTRACT.read_text(encoding="utf-8")
    assert set(_string_array(source, "ZERO_DENOMINATOR_POLICIES")) == set(
        ZERO_DENOMINATOR_POLICIES
    )


def test_the_concept_dialog_emits_the_two_keys_the_server_reads():
    # `semantic_model.py:961-962` reads exactly these. They used to arrive as
    # `None`, so no metric could satisfy `validate_aggregation`.
    source = (DIALOGS / "NewConceptDialog.tsx").read_text(encoding="utf-8")
    assert "additivity_class:" in source
    assert "non_additive_dimensions:" in source


def test_the_concept_dialog_still_offers_no_sql_field():
    code = _code_only((DIALOGS / "NewConceptDialog.tsx").read_text(encoding="utf-8"))
    for forbidden in ("raw_sql", "SELECT ", "sql:"):
        assert forbidden not in code


# ---------------------------------------------------------------------------
# Story 60.2 — the confirmation token is not a verdict, on any of the dialogs
#
# THE BELIEF, MEASURED FALSE. Three dialogs read "prepare returned no token" as
# "validation refused", and one said so in a comment. Both change-set stores mint
# the token UNCONDITIONALLY:
#   * `semantic_model.prepare_change_set` returns `confirmation_token` beside the
#     refusals it just collected (`semantic_model.py:1140-1148`);
#   * `controls_change_sets.prepare_change_set` returns `(record, token)` with no
#     verdict field at all (`controls_change_sets.py:243-266`).
# So the branch never fired on a refusal: a refused change set was read as an
# accepted one. On the semantic store `validation.publishable` is the field that
# answers; on the controls store a refusal is a raised error, never a missing
# token.
#
# Asserted on the SOURCE because the defect is a belief written into it, and
# because covering only the file someone remembered is what let a third dialog
# keep the defect for the whole life of this module.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialog", CHANGE_SET_DIALOGS)
def test_no_dialog_calls_a_change_set_route_without_its_governance_prefix(dialog: str):
    code = _code_only((DIALOGS / dialog).read_text(encoding="utf-8"))
    # The exact dead shape: `/semantic-model/change-sets` NOT preceded by
    # `/governance`. `VisualMappingWorkbenchDialog.tsx` carried it on all three
    # of its calls while this module tested two other files.
    assert "}/semantic-model/change-sets" not in code


@pytest.mark.parametrize("dialog", CHANGE_SET_DIALOGS)
def test_no_dialog_treats_the_confirmation_token_as_an_acceptance_verdict(dialog: str):
    source = (DIALOGS / dialog).read_text(encoding="utf-8")
    lowered = source.lower()
    # The sentence that carried the belief, in any of the spellings the three
    # dialogs used. A comment asserting a guard the code does not perform is what
    # this checks for: the next reader trusts it instead of measuring.
    for claim in (
        "means validation refused",
        "without a token the server refused",
        "prepared without a token",
    ):
        assert claim not in lowered, f"{dialog} still states that a missing token is a refusal"


@pytest.mark.parametrize("dialog", ("NewConceptDialog.tsx", "NewSemanticViewDialog.tsx"))
def test_the_semantic_dialogs_read_publishable_before_confirming(dialog: str):
    # These post to the SEMANTIC change-set family, whose prepare response
    # carries `validation.publishable`. Reading the token instead is what made a
    # refusal look like a success.
    code = _code_only((DIALOGS / dialog).read_text(encoding="utf-8"))
    assert "publishable" in code, f"{dialog} never consults validation.publishable"


def test_the_removed_mapping_dialog_has_not_come_back():
    """AI-244: the file is gone, and a screen may not mount it again.

    It announced a save over a change set that published nothing -- `notify(...)`
    unconditionally, after a `prepare` swallowed by `.catch(() => null)`. That
    was repaired; what removed the dialog was the fact underneath it, that
    Governance does not own physical mapping (`governance.md:80-82`) and that its
    three connectors were written into the component rather than read.
    """
    assert not (DIALOGS / "VisualMappingWorkbenchDialog.tsx").exists()
    for screen in DIALOGS.glob("*.tsx"):
        assert "VisualMappingWorkbenchDialog" not in screen.read_text(encoding="utf-8"), screen.name
