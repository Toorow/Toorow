"""Story 51.1 -- proofs for the product Golden Question object.

TWO LAYERS, AND THE REASON FOR EACH.

The first half drives a fake connection: every refusal path of
`validate_golden_question_version` is a decision made in Python before any row is
written, and a fake cursor is the honest instrument for it -- it makes the
refusal list, its codes and its subjects observable.

The second half REFUSES to use a fake, and is gated on a live PostgreSQL. Version
immutability, Project isolation through composite foreign keys and the structural
trigger are enforced by migration 153, not by this module. A mocked assertion
about them would prove that the test author and the test agree, which is the
failure mode CLAUDE.md section 6 names by hand. The gated half skips when
`TEST_POSTGRES_DSN` is unset and skips with a named reason when migration 153 has
not been applied to that database -- never silently, and never green.

The fixtures name nothing outside this repository: `proj_EXAMPLE`,
`owner@example.com`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import core.db
import core.golden_questions_api as golden_questions_api
import pytest
from core.golden_questions import (
    ASSERTION_TYPES,
    DECLARED_ABSENCES,
    GOLDEN_QUESTION_CONTRACT_VERSION,
    GOLDEN_QUESTION_V2_CONTRACT_VERSION,
    LIFECYCLE_TRANSITIONS,
    PROVENANCE_LINK_KINDS,
    RESULT_TYPES,
    UNVERIFIABLE,
    GoldenQuestionRefused,
    canonical_hash,
    connector_names,
    declared_absences,
    is_exact_version_pin,
    validate_golden_question_version,
)
from starlette.datastructures import QueryParams


@pytest.mark.anyio
async def test_collection_api_rejects_a_stale_cursor(monkeypatch):
    async def authorize(_request, _capability):
        return "owner@example.com", "org_EXAMPLE"

    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection
    context.__exit__.return_value = False
    monkeypatch.setattr(golden_questions_api, "_authorize", authorize)
    monkeypatch.setattr(core.db, "get_connection", lambda: context)
    monkeypatch.setattr(
        golden_questions_api,
        "list_golden_questions",
        lambda *_args, **_kwargs: [{"id": "gq-1"}],
    )
    request = MagicMock()
    request.path_params = {"project_id": "proj_EXAMPLE"}
    request.query_params = QueryParams("cursor=25&limit=25")

    response = await golden_questions_api._list_golden_questions(request)

    assert response.status_code == 422
    assert "cursor does not reference" in response.body.decode("utf-8")


def _module_source(module_name: str) -> str:
    """Read one delivered module from ITS OWN location, not from the cwd.

    A test that reads `server/core/...` relative to the working directory passes
    or fails depending on where pytest was launched, which is a green test that
    proves nothing.
    """
    import importlib

    module = importlib.import_module(module_name)
    return Path(module.__file__).read_text(encoding="utf-8")


def _executable_text(module_name: str) -> str:
    """The module's CODE: every identifier and every non-docstring string literal.

    Scanning raw source would make a prohibition unstatable -- this module's own
    header explains why it never reads `server/tests/evals/corpus.yaml`, and a
    naive grep would read that explanation as the violation it forbids. Comments
    and docstrings are prose about the code; SQL text and table names are the
    code. Only the second kind is evidence.
    """
    import ast

    tree = ast.parse(_module_source(module_name))
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstring_nodes.add(id(value))

    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_nodes:
                parts.append(node.value)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            parts.append(node.name)
        elif isinstance(node, ast.alias):
            parts.append(node.name)
            if node.asname:
                parts.append(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts.append(node.module)
        elif isinstance(node, ast.keyword) and node.arg:
            parts.append(node.arg)
    return "\n".join(parts)


def _string_literals(module_name: str) -> list[str]:
    import ast

    tree = ast.parse(_module_source(module_name))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


_ORG = "org_EXAMPLE"
_PROJECT = "proj_EXAMPLE"
_DOMAIN = "bd_EXAMPLE"
_VIEW = "sv_EXAMPLE"
_VIEW_VERSION = "svv_EXAMPLE"
_SPEC_VERSION_A = "qsv_EXAMPLE_A"
_SPEC_VERSION_B = "qsv_EXAMPLE_B"


# ---------------------------------------------------------------------------
# A fake connection that answers the four SELECTs the validator issues.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, state: dict):
        self._state = state
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, _params=None):
        self._last = sql

    def fetchone(self):
        if "mdm_business_domain_versions" in self._last:
            return (1,) if self._state["domain_found"] else None
        if "mdm_business_classifications" in self._last:
            return (_DOMAIN,) if self._state["classification_found"] else None
        if "semantic_view_versions" in self._last:
            if not self._state["view_found"]:
                return None
            return (self._state["view_status"],)
        return None

    def fetchall(self):
        if "query_spec_versions" in self._last:
            return [(spec,) for spec in self._state["known_specs"]]
        return []


class _Conn:
    def __init__(
        self,
        *,
        domain_found=True,
        classification_found=True,
        view_found=True,
        view_status="published",
        known_specs=(_SPEC_VERSION_A, _SPEC_VERSION_B),
    ):
        self._state = {
            "domain_found": domain_found,
            "classification_found": classification_found,
            "view_found": view_found,
            "view_status": view_status,
            "known_specs": list(known_specs),
        }

    def cursor(self):
        return _Cursor(self._state)


def _definition(**overrides):
    """A complete, legal Golden Question document."""
    payload = {
        "business_domain_id": _DOMAIN,
        "business_domain_version_number": 1,
        "semantic_view_id": _VIEW,
        "semantic_view_version_id": _VIEW_VERSION,
        "semantic_view_version_role": "baseline",
        "question": "What was paid media spend last completed month, by market?",
        "time_boundary": {"as_of": "2026-07-31", "grain": "month"},
        "expected_result": [
            {"assertion_type": "value", "member_id": "sc_spend", "tolerance": None},
            {
                "assertion_type": "cardinality",
                "rows": 12,
                "tolerance": {"kind": "numeric", "absolute": 1},
            },
        ],
        "required_provenance": [
            {"link_kind": "source", "required": True},
            {"link_kind": "semantic_view", "required": True},
            {"link_kind": "citation", "required": True},
        ],
        "expected_ai_path": {
            "grammar_version": 1,
            "required_nodes": [
                {"key": "context-hub/knowledge-note/kn_EXAMPLE", "step_kind": "knowledge_read"},
                {"key": "governance/semantic-view/sv_EXAMPLE", "step_kind": "semantic_query"},
            ],
            "forbidden_nodes": [{"tool_name": "raw_sql_passthrough"}],
            "order_constraints": [
                {
                    "before": "context-hub/knowledge-note/kn_EXAMPLE",
                    "after": "governance/semantic-view/sv_EXAMPLE",
                }
            ],
        },
        "result_type": "breakdown",
        "capability_tags": ["media_spend_reporting"],
        "severity": "critical",
    }
    payload.update(overrides)
    return payload


def _validate(definition=None, **conn_kwargs):
    return validate_golden_question_version(
        _Conn(**conn_kwargs),
        org_id=_ORG,
        project_id=_PROJECT,
        payload=_definition() if definition is None else definition,
    )


def _v2_definition(**overrides):
    payload = _definition(
        contract_version=GOLDEN_QUESTION_V2_CONTRACT_VERSION,
        expected_result=[
            {
                "assertion_type": "value",
                "selectors": [{"field": "market", "operator": "eq", "value": "FR"}],
                "field": "spend",
                "operator": "equals",
                "expected": 12.5,
                "tolerance": {"kind": "numeric", "mode": "relative", "amount": 0.01},
            },
            {
                "assertion_type": "row_set",
                "selectors": [],
                "fields": ["market", "spend"],
                "operator": "equals",
                "expected_rows": [{"market": "FR", "spend": 12.5}],
                "tolerance": {"kind": "set", "max_missing": 0, "max_extra": 0},
            },
        ],
        required_provenance=[{"link_kind": "source", "required": True, "expected_ref": "srcv_1"}],
    )
    payload.update(overrides)
    return payload


def test_v2_is_strict_bounded_and_does_not_change_the_v1_contract():
    v1 = _validate()
    v2 = _validate(_v2_definition())
    assert v1.document["contract_version"] == "golden-question.v1"
    assert v2.document["contract_version"] == "golden-question.v2"
    assert v2.document["expected_result"][0]["expected"] == 12.5


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update(client_result_id="qr_untrusted"), "unknown_field"),
        (
            lambda value: value["expected_result"][0].update(approximately=True),
            "unknown_assertion_field",
        ),
        (
            lambda value: value["expected_result"][0].update(operator="roughly"),
            "unknown_assertion_operator",
        ),
        (
            lambda value: value["expected_result"][0].update(
                tolerance={"kind": "numeric", "mode": "absolute", "amount": -1}
            ),
            "invalid_tolerance",
        ),
        (
            lambda value: value.update(
                required_provenance=[{"link_kind": "source", "required": True, "moving": "latest"}]
            ),
            "unknown_provenance_field",
        ),
        (
            lambda value: value["expected_result"][0].pop("expected"),
            "missing_assertion_field",
        ),
        (
            lambda value: value["expected_result"][0].update(expected=float("nan")),
            "non_json_value",
        ),
        (
            lambda value: value["expected_result"][0].update(
                expected=42,
                tolerance={"kind": "temporal", "seconds": 0},
            ),
            "invalid_temporal_expected",
        ),
        (
            lambda value: value["expected_result"][0].update(
                expected="2026-08-11T12:00:00",
                tolerance={"kind": "temporal", "seconds": 0},
            ),
            "invalid_temporal_expected",
        ),
        (
            lambda value: value["expected_result"][0].update(
                expected="2026-08-11 12:00:00+00:00",
                tolerance={"kind": "temporal", "seconds": 0},
            ),
            "invalid_temporal_expected",
        ),
    ],
)
def test_v2_refuses_unknown_fields_operators_and_malformed_tolerances(mutate, code):
    payload = _v2_definition()
    mutate(payload)
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(payload)
    assert code in _codes(exc.value)


def test_v2_enforces_assertion_selector_row_and_canonical_byte_budgets():
    cases = [
        (
            _v2_definition(expected_result=_v2_definition()["expected_result"] * 17),
            "too_many_assertions",
        ),
        (
            _v2_definition(
                expected_result=[
                    {
                        "assertion_type": "cardinality",
                        "selectors": [{"field": f"f{i}", "operator": "exists"} for i in range(9)],
                        "operator": "equals",
                        "expected": 1,
                        "tolerance": None,
                    }
                ]
            ),
            "too_many_selectors",
        ),
        (
            _v2_definition(
                expected_result=[
                    {
                        "assertion_type": "row_set",
                        "selectors": [],
                        "fields": ["market"],
                        "operator": "equals",
                        "expected_rows": [{"market": str(i)} for i in range(101)],
                        "tolerance": None,
                    }
                ]
            ),
            "too_many_expected_rows",
        ),
        (_v2_definition(question="x" * 65_536), "document_too_large"),
    ]
    for payload, code in cases:
        with pytest.raises(GoldenQuestionRefused) as exc:
            _validate(payload)
        assert code in _codes(exc.value)


def _codes(exc: GoldenQuestionRefused) -> set[str]:
    return {r.code for r in exc.refusals}


def _subjects(exc: GoldenQuestionRefused) -> set[str]:
    return {r.subject for r in exc.refusals}


# ---------------------------------------------------------------------------
# The seven mandatory fields, and the hash over the normalized document.
# ---------------------------------------------------------------------------


def test_a_complete_definition_validates_and_carries_all_seven_mandatory_fields():
    validated = _validate()
    document = validated.document
    assert document["contract_version"] == GOLDEN_QUESTION_CONTRACT_VERSION
    assert document["business_domain"]["id"] == _DOMAIN
    assert document["business_domain"]["version_number"] == 1
    assert document["semantic_view"]["version_id"] == _VIEW_VERSION
    assert document["semantic_view"]["role"] == "baseline"
    assert document["question"]
    assert document["time_boundary"] == {"as_of": "2026-07-31", "grain": "month"}
    assert len(document["expected_result"]) == 2
    assert len(document["required_provenance"]) == 3
    assert document["expected_ai_path"]["required_nodes"]
    assert validated.result_type in RESULT_TYPES
    assert validated.severity == "critical"
    assert validated.capability_tags == ["media_spend_reporting"]


def test_the_hash_is_stable_across_equivalent_documents_and_moves_with_meaning():
    a = _validate()
    reordered = _definition(
        capability_tags=["media_spend_reporting", "MEDIA_SPEND_REPORTING"],
        time_boundary={"grain": "month", "as_of": "2026-07-31"},
    )
    b = _validate(reordered)
    assert a.content_hash == b.content_hash, (
        "key order and a duplicate capability tag must not mint a different version"
    )
    assert a.content_hash == canonical_hash(a.document)

    changed = _validate(_definition(severity="minor"))
    assert changed.content_hash != a.content_hash, "a semantic change must move the hash"


def test_the_document_stores_no_sql_text_anywhere():
    document = _validate().document
    serialized = json.dumps(document).lower()
    for forbidden in ("select ", " from ", "reference_sql", "expected_sql"):
        assert forbidden not in serialized, (
            "one implementation-specific SQL string is never the definition of correctness"
        )


# ---------------------------------------------------------------------------
# `latest` is not a version.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("moving", ["latest", "LATEST", " current ", "head", ""])
def test_a_moving_semantic_view_pin_is_refused_and_never_resolved(moving):
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(semantic_view_version_id=moving))
    assert {"moving_version_pin", "missing_field"} & _codes(exc.value)


def test_latest_is_refused_inside_an_expected_path_node_too():
    definition = _definition(
        expected_ai_path={
            "required_nodes": [
                {
                    "step_kind": "knowledge_read",
                    "owner_workspace": "context-hub",
                    "owner_object_type": "knowledge-note",
                    "owner_object_id": "kn_EXAMPLE",
                    "owner_version_id": "latest",
                }
            ]
        }
    )
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(definition)
    assert "moving_version_pin" in _codes(exc.value)


def test_is_exact_version_pin_matches_the_database_function():
    assert is_exact_version_pin("svv_01H")
    for moving in ("latest", "Current", " HEAD ", "", "   ", None, 3):
        assert not is_exact_version_pin(moving)


# ---------------------------------------------------------------------------
# The two governed pins.
# ---------------------------------------------------------------------------


def test_a_business_domain_version_of_another_organization_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(domain_found=False)
    assert "unknown_business_domain_version" in _codes(exc.value)
    # The same refusal for "no such version" and "another tenant's domain": a
    # distinct message here would be a cross-tenant existence oracle.
    assert all("organization" in r.message for r in exc.value.refusals)


def test_a_domain_id_without_its_version_number_is_not_a_pin():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(business_domain_version_number=None))
    assert "missing_field" in _codes(exc.value)
    assert "business_domain_version_number" in _subjects(exc.value)


def test_a_classification_that_does_not_narrow_this_domain_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(business_classification_id="bc_EXAMPLE"), classification_found=False)
    assert "unknown_business_classification" in _codes(exc.value)


@pytest.mark.parametrize("status", ["draft", "superseded", "archived"])
def test_an_unpublished_or_superseded_semantic_view_version_is_refused(status):
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(view_status=status)
    assert "version_not_pinnable" in _codes(exc.value)


def test_a_semantic_view_version_of_another_project_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(view_found=False)
    assert "unknown_semantic_view_version" in _codes(exc.value)


def test_a_half_semantic_pin_is_refused_rather_than_completed_by_the_server():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(semantic_view_id=""))
    assert "missing_field" in _codes(exc.value)


def test_the_view_version_role_is_baseline_or_candidate():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(semantic_view_version_role="whatever"))
    assert "invalid_role" in _codes(exc.value)


# ---------------------------------------------------------------------------
# AC3 -- typed assertions with an EXPLICIT tolerance.
# ---------------------------------------------------------------------------


def test_an_empty_expected_result_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(expected_result=[]))
    assert "empty_expected_result" in _codes(exc.value)


def test_an_assertion_without_a_tolerance_key_is_refused_and_the_index_is_named():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_result=[
                    {"assertion_type": "value", "member_id": "sc_spend", "tolerance": None},
                    {"assertion_type": "value", "member_id": "sc_clicks"},
                ]
            )
        )
    assert "missing_tolerance" in _codes(exc.value)
    # The offending element, not "the document": an author fixes one field.
    assert "expected_result[1]" in _subjects(exc.value)


def test_a_declared_null_tolerance_means_exact_and_is_accepted():
    validated = _validate(
        _definition(expected_result=[{"assertion_type": "row_set", "rows": [], "tolerance": None}])
    )
    assert validated.document["expected_result"][0]["tolerance"] is None


def test_an_unknown_assertion_type_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(expected_result=[{"assertion_type": "vibes", "tolerance": None}]))
    assert "unknown_assertion_type" in _codes(exc.value)


def test_the_non_success_states_are_first_class_assertion_types():
    for state in ("empty", "degraded", "refused"):
        assert state in ASSERTION_TYPES
        validated = _validate(
            _definition(expected_result=[{"assertion_type": state, "tolerance": None}])
        )
        assert validated.document["expected_result"][0]["assertion_type"] == state


@pytest.mark.parametrize("kind", ["numeric", "temporal", "set"])
def test_the_three_declared_tolerance_kinds_are_accepted(kind):
    validated = _validate(
        _definition(
            expected_result=[{"assertion_type": "value", "tolerance": {"kind": kind, "amount": 1}}]
        )
    )
    assert validated.document["expected_result"][0]["tolerance"]["kind"] == kind


def test_an_unknown_tolerance_kind_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_result=[{"assertion_type": "value", "tolerance": {"kind": "roughly"}}]
            )
        )
    assert "unknown_tolerance_kind" in _codes(exc.value)


# ---------------------------------------------------------------------------
# AC4 -- provenance is a typed requirement, not prose.
# ---------------------------------------------------------------------------


def test_a_free_text_provenance_description_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(required_provenance=["cite the source please"]))
    assert "invalid_shape" in _codes(exc.value)


def test_an_empty_provenance_array_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(required_provenance=[]))
    assert "empty_required_provenance" in _codes(exc.value)


def test_an_unknown_link_kind_is_refused_and_virtual_pull_is_a_kind_of_its_own():
    assert "virtual_pull" in PROVENANCE_LINK_KINDS
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(required_provenance=[{"link_kind": "vibes", "required": True}]))
    assert "unknown_link_kind" in _codes(exc.value)


def test_each_provenance_link_declares_a_boolean_required_flag():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(required_provenance=[{"link_kind": "source"}]))
    assert "missing_required_flag" in _codes(exc.value)


# ---------------------------------------------------------------------------
# AC5 -- the expected AI Path is a pattern in the observed-path vocabulary.
# ---------------------------------------------------------------------------


def test_a_step_kind_the_observed_record_cannot_express_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(expected_ai_path={"required_nodes": [{"step_kind": "thinking"}]}))
    assert "unknown_step_kind" in _codes(exc.value)


def test_an_owner_workspace_outside_the_enumeration_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "required_nodes": [{"step_kind": "tool_call", "owner_workspace": "marketing"}]
                }
            )
        )
    assert "unknown_owner_workspace" in _codes(exc.value)


def test_a_node_field_app_ai_path_steps_cannot_record_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "required_nodes": [{"step_kind": "tool_call", "reasoning": "because"}]
                }
            )
        )
    assert "unknown_node_field" in _codes(exc.value)


def test_half_a_skill_pin_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "required_nodes": [
                        {"step_kind": "skill_step", "skill_version_id": "skv_EXAMPLE"}
                    ]
                }
            )
        )
    assert "half_skill_pin" in _codes(exc.value)


def test_an_order_constraint_naming_an_undeclared_node_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "required_nodes": [{"step_kind": "tool_call"}],
                    "order_constraints": [{"before": 0, "after": 4}],
                }
            )
        )
    assert "dangling_order_constraint" in _codes(exc.value)


def test_forbidden_nodes_and_declared_alternatives_are_part_of_the_pattern():
    validated = _validate(
        _definition(
            expected_ai_path={
                "grammar_version": 1,
                "required_nodes": [
                    {"key": "governance/semantic-view/sv_EXAMPLE", "step_kind": "semantic_query"}
                ],
                "forbidden_nodes": [{"tool_name": "raw_sql_passthrough"}],
                "alternatives": [
                    {
                        "name": "how the figure may be read",
                        "branches": [
                            {
                                "name": "cached semantic read",
                                "required_nodes": [
                                    {"key": "data/dataset/ds_EXAMPLE", "step_kind": "data_read"}
                                ],
                            },
                            {
                                "name": "live semantic query",
                                "required_nodes": [
                                    {"key": "governance/semantic-view/sv_EXAMPLE"}
                                ],
                            },
                        ],
                    }
                ],
            }
        )
    )
    pattern = validated.document["expected_ai_path"]
    assert pattern["forbidden_nodes"] == [{"tool_name": "raw_sql_passthrough"}]
    assert pattern["alternatives"][0]["branches"][0]["name"] == "cached semantic read"


def test_the_pre_grammar_vocabulary_is_projected_at_the_door_and_never_stored():
    # A console tab opened before the two dialects were unified still submits
    # `forbidden_tools` and integer order constraints. It is accepted, and what is
    # STORED is the grammar -- `grammar_version` included, so a later widening of
    # the schema cannot silently re-read this version.
    validated = _validate(
        _definition(
            expected_ai_path={
                "required_nodes": [
                    {
                        "step_kind": "knowledge_read",
                        "owner_workspace": "context-hub",
                        "owner_object_type": "knowledge-note",
                        "owner_object_id": "kn_EXAMPLE",
                    },
                    {
                        "step_kind": "semantic_query",
                        "owner_workspace": "governance",
                        "owner_object_type": "semantic-view",
                        "owner_object_id": "sv_EXAMPLE",
                    },
                ],
                "forbidden_tools": ["raw_sql_passthrough"],
                "order_constraints": [{"before": 0, "after": 1}],
            }
        )
    )
    pattern = validated.document["expected_ai_path"]
    assert pattern["grammar_version"] == 1
    assert "forbidden_tools" not in pattern and "alternative_paths" not in pattern
    assert pattern["required_nodes"][0]["key"] == "context-hub/knowledge-note/kn_EXAMPLE"
    assert pattern["forbidden_nodes"] == [{"tool_name": "raw_sql_passthrough"}]
    assert pattern["order_constraints"] == [
        {
            "before": "context-hub/knowledge-note/kn_EXAMPLE",
            "after": "governance/semantic-view/sv_EXAMPLE",
        }
    ]


def test_a_node_without_an_owner_triple_is_refused_where_it_is_written():
    # It is addressed by `workspace/type/id` and by nothing else, so a node
    # without one is a requirement no observed step can ever satisfy.
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "required_nodes": [{"step_kind": "tool_call", "tool_name": "get_card"}]
                }
            )
        )
    assert "node_without_owner" in _codes(exc.value)


def test_one_alternative_is_not_a_choice():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "required_nodes": [
                        {
                            "step_kind": "semantic_query",
                            "owner_workspace": "governance",
                            "owner_object_type": "semantic-view",
                            "owner_object_id": "sv_EXAMPLE",
                        }
                    ],
                    "alternative_paths": [
                        {
                            "label": "cached semantic read",
                            "required_nodes": [
                                {
                                    "step_kind": "data_read",
                                    "owner_workspace": "data",
                                    "owner_object_type": "dataset",
                                    "owner_object_id": "ds_EXAMPLE",
                                }
                            ],
                        }
                    ],
                }
            )
        )
    assert "single_alternative_path" in _codes(exc.value)


def test_a_pattern_that_asserts_nothing_is_stored_as_nothing():
    # Not an object full of empty lists: that object is truthy, and a truthy
    # pattern asserting nothing made every observed path `pass` by vacuity.
    validated = _validate(_definition(expected_ai_path={"required_nodes": []}))
    assert validated.document["expected_ai_path"] == {}


def test_an_expected_path_is_stored_and_never_compared_here():
    # Story 51.3 owns comparison. If this module ever grew a verdict for a path,
    # a Golden Question would start judging itself.
    source = _executable_text("core.golden_questions")
    for owned_elsewhere in ("compare_expected_path", "observed_ai_path_id", "path_verdict"):
        assert owned_elsewhere not in source


# ---------------------------------------------------------------------------
# AC6 -- result type, capability, severity.
# ---------------------------------------------------------------------------


def test_a_version_missing_result_type_capability_or_severity_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(result_type=None, capability_tags=[], severity=None))
    codes = _codes(exc.value)
    assert {"unknown_result_type", "missing_capability_tags", "unknown_severity"} <= codes


def test_a_connector_name_is_not_a_capability():
    installed = connector_names()
    assert installed, "the connector registry must be readable for this refusal to mean anything"
    a_connector = sorted(installed)[0]
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(capability_tags=[a_connector]))
    assert "connector_name_is_not_a_capability" in _codes(exc.value)


def test_a_capability_that_merely_contains_a_connector_name_is_not_refused():
    validated = _validate(_definition(capability_tags=["shopify_order_reconciliation"]))
    assert validated.capability_tags == ["shopify_order_reconciliation"]


def test_every_declared_lifecycle_transition_is_reachable_and_archived_is_terminal():
    assert LIFECYCLE_TRANSITIONS["archived"] == ()
    reachable = {target for targets in LIFECYCLE_TRANSITIONS.values() for target in targets}
    assert reachable == {"active", "deprecated", "archived"}


# ---------------------------------------------------------------------------
# AC7 -- 0..N reference execution paths.
# ---------------------------------------------------------------------------


def test_zero_one_and_several_reference_paths_are_all_valid():
    assert _validate(_definition(reference_paths=[])).reference_paths == []
    one = _validate(
        _definition(
            reference_paths=[{"query_spec_version_id": _SPEC_VERSION_A, "role": "canonical"}]
        )
    )
    assert len(one.reference_paths) == 1
    several = _validate(
        _definition(
            reference_paths=[
                {"query_spec_version_id": _SPEC_VERSION_A, "role": "canonical"},
                {"query_spec_version_id": _SPEC_VERSION_B, "role": "alternative"},
            ]
        )
    )
    assert [p["ordinal"] for p in several.reference_paths] == [0, 1]


def test_a_reference_path_outside_this_project_is_refused():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                reference_paths=[
                    {"query_spec_version_id": "qsv_FROM_ANOTHER_PROJECT", "role": "canonical"}
                ]
            )
        )
    assert "unknown_reference_path" in _codes(exc.value)


# ---------------------------------------------------------------------------
# AC11 -- every missing owner is declared and reports `Unverifiable`.
# ---------------------------------------------------------------------------


def test_a_render_pin_supplied_by_a_caller_is_refused_rather_than_stored():
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(_definition(expected_render_ref="rnd_INVENTED"))
    assert "render_owner_not_delivered" in _codes(exc.value)


def test_the_render_pin_is_declared_in_the_document_and_left_empty():
    document = _validate().document
    assert "expected_render_ref" in document, "a declared absence must stay visible"
    assert document["expected_render_ref"] is None


def test_every_declared_absence_is_unverifiable_with_a_reason_and_a_named_owner():
    absences = declared_absences()
    assert {a["dimension"] for a in absences} == {
        "render",
        "mcp_app_behavior",
        "run_coverage",
        "observed_ai_path_coverage",
        "live_deployed_answer",
    }
    for absence in absences:
        assert absence["verdict"] == UNVERIFIABLE, "never pass, never fail"
        assert absence["reason_code"] and absence["reason_code"].islower()
        # An owner nobody can name is how an absence starts reading as evidence:
        # `future_owner` is exactly the placeholder that had Story 49.6 rejected.
        assert absence["owner_story"] not in ("", None, "future_owner", "tbd", "unknown")
        assert absence["owner_story"][0].isdigit()
        assert absence["message"]


def test_the_absent_live_run_has_a_reason_code_and_not_a_sentence():
    """Story 69.5 AC4: the gap of the live journey is MATCHABLE, not narrated.

    Story 69.5 reported its own AC1 with the words
    `no_deployed_environment_in_this_session`. That string is the code of nothing:
    `DECLARED_ABSENCES` was closed at four entries and none of them was it, so any
    reader matching on `reason_code` -- which is the whole reason the field exists
    -- read four absences where there were five, and the fifth read as prose a
    machine skips. This test is the reason that cannot happen again silently.
    """
    by_dimension = {a["dimension"]: a for a in declared_absences()}
    absence = by_dimension.get("live_deployed_answer")
    assert absence is not None, (
        "the live end-to-end run of the entity spine (story 69.5 AC1) has no "
        "deployed environment here -- that absence must be declared with a code, "
        "not written as a sentence in a story record"
    )
    assert absence["reason_code"] == "deployed_environment_absent"
    assert absence["owner_story"] == "69.5"
    assert absence["verdict"] == UNVERIFIABLE
    # The three identities the epic promises the answer will name. An absence that
    # did not say WHAT cannot be observed would leave a reader unable to tell a
    # missing deployment from a missing chain.
    for named in ("execution id", "mapping version", "rule version"):
        assert named in absence["message"], (
            f"the declared absence must name {named!r}: it is one of the three "
            "identities the live answer is required to cite"
        )
    # `no_deployed_environment_in_this_session` was the prose. No code carries it,
    # and none should: a session is not an owner.
    assert all(
        "in_this_session" not in a["reason_code"] for a in declared_absences()
    ), "a reason code names what is absent, never which session noticed"


def test_no_declared_absence_can_be_reported_as_pass_or_fail():
    for absence in DECLARED_ABSENCES:
        assert absence.verdict == UNVERIFIABLE
    # The module owns no other verdict value at all: `pass` and `fail` belong to
    # Story 51.3, which compares. Nothing here can mint one.
    literals = _string_literals("core.golden_questions")
    assert "pass" not in literals and "fail" not in literals


# ---------------------------------------------------------------------------
# AC1 and AC12 -- one owner for the noun, and no downstream object created.
# ---------------------------------------------------------------------------

_MODULE_SOURCES = ("core.golden_questions", "core.golden_questions_api")


def _sources() -> str:
    return "\n".join(_executable_text(name) for name in _MODULE_SOURCES)


def test_neither_the_epic_14_corpus_nor_the_legacy_table_is_an_alias_of_this_object():
    source = _sources()
    for alias in ("corpus.yaml", "tests/evals", "eval_benchmark_questions", "app.eval_runs"):
        assert alias not in source, (
            f"`{alias}` is the Epic 14 benchmark record; reading it at runtime would make "
            "the evaluation subject part of the instrument"
        )


def test_this_story_creates_no_downstream_test_object():
    source = _sources()
    for downstream in (
        "app.evaluation_runs",
        "app.evaluation_run_cases",
        "app.evaluation_case_dimension_verdicts",
        "app.evaluation_baselines",
        "app.evaluation_comparisons",
        "app.evaluation_gate_decisions",
        "app.observed_cohorts",
        "app.observed_cohort_members",
        "app.golden_question_proposals",
        "app.feedback_annotations",
        "app.feedback_reviews",
    ):
        assert downstream not in source, f"{downstream} belongs to Stories 51.2 to 51.5"


def test_test_writes_to_no_governance_or_context_hub_table():
    source = _sources()
    for write in (
        "INSERT INTO app.semantic_view",
        "UPDATE app.semantic_view",
        "INSERT INTO app.mdm_business",
        "UPDATE app.mdm_business",
        "INSERT INTO app.context_",
        "UPDATE app.context_",
    ):
        assert write not in source, (
            "Test references governed versions and never edits them (analyze-and-test.md:370)"
        )


def test_no_render_object_or_identifier_is_minted_anywhere():
    source = _sources()
    for invented in ("render_id", "CREATE TABLE", "app.renders"):
        assert invented not in source


# ---------------------------------------------------------------------------
# The route module: order is a contract, not a formatting choice.
# ---------------------------------------------------------------------------


def test_the_route_module_exports_its_routes_and_does_not_mount_itself():
    from core import golden_questions_api

    paths = [route.path for route in golden_questions_api.golden_question_routes]
    base = "/api/projects/{project_id}/test/golden-questions"
    assert paths.count(base) == 2, "list and create share the collection address"
    assert f"{base}/options" in paths
    assert f"{base}/{{golden_question_id}}/versions" in paths
    assert f"{base}/{{golden_question_id}}/versions/{{version_id}}" in paths
    assert f"{base}/{{golden_question_id}}/coverage" in paths
    assert f"{base}/{{golden_question_id}}/lifecycle" in paths
    assert f"{base}/{{golden_question_id}}" in paths

    literal = paths.index(f"{base}/options")
    parameterized = paths.index(f"{base}/{{golden_question_id}}")
    assert literal < parameterized, "`options` must never be captured as a question id"
    # The orchestrator mounts this list; the module must not reach into admin_api.
    source = _executable_text("core.golden_questions_api")
    assert "add_route" not in source
    assert "Mount" not in source


def test_foreign_denied_and_absent_share_one_envelope():
    from core.golden_questions_api import _NOT_FOUND

    assert _NOT_FOUND == {"code": "not_found", "message": "Not found"}


# ---------------------------------------------------------------------------
# Live PostgreSQL. Immutability, Project isolation and the structural trigger
# are database properties; a mock cannot prove any of them.
# ---------------------------------------------------------------------------


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@pytest.fixture
def scope(live_postgres):
    """An organization, two Projects, a domain version and a view version.

    Everything is rolled back. Nothing here is a production identifier.
    """
    import psycopg
    from ulid import ULID

    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('app.golden_question_versions')")
        if cur.fetchone()[0] is None:
            pytest.skip(
                "migration 153 is not applied to TEST_POSTGRES_DSN -- run "
                "scripts/apply_migrations.py before the pg-gated Golden Question suite"
            )

    org_id = f"org_{ULID()}"
    project_a = f"proj_{ULID()}"
    project_b = f"proj_{ULID()}"
    domain_id = f"bd_{ULID()}"
    view_id = f"sv_{ULID()}"
    view_version_id = f"svv_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Golden Question test org", org_id.lower(), "owner@example.com"),
        )
        for project_id, label in ((project_a, "A"), (project_b, "B")):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s,%s,%s,%s,'active',%s)",
                (
                    project_id,
                    org_id,
                    f"Golden Question test project {label}",
                    project_id.lower(),
                    "owner@example.com",
                ),
            )
        cur.execute(
            "INSERT INTO app.mdm_business_domains (id, org_id, slug, name, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                domain_id,
                org_id,
                "gq-test-domain",
                "Golden Question test domain",
                "owner@example.com",
            ),
        )
        cur.execute(
            """
            INSERT INTO app.mdm_business_domain_versions
                (domain_id, version_number, org_id, slug, name, status, created_by,
                 created_at, updated_at, change_kind, changed_by)
            VALUES (%s, 1, %s, %s, %s, 'active', %s, NOW(), NOW(), 'created', %s)
            """,
            (
                domain_id,
                org_id,
                "gq-test-domain",
                "Golden Question test domain",
                "owner@example.com",
                "owner@example.com",
            ),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by) "
            "VALUES (%s,%s,%s,'published',%s)",
            (view_id, project_a, "gq_test_view", "owner@example.com"),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', %s, %s, %s, %s, %s)
            """,
            (
                view_version_id,
                view_id,
                project_a,
                "gq_test_view",
                "Golden Question test view",
                _hex64("dependency"),
                _hex64("content"),
                "owner@example.com",
            ),
        )

    yield {
        "conn": conn,
        "psycopg": psycopg,
        "org_id": org_id,
        "project_id": project_a,
        "other_project_id": project_b,
        "domain_id": domain_id,
        "semantic_view_id": view_id,
        "semantic_view_version_id": view_version_id,
    }
    conn.rollback()


def _live_definition(scope, **overrides):
    payload = _definition(
        business_domain_id=scope["domain_id"],
        semantic_view_id=scope["semantic_view_id"],
        semantic_view_version_id=scope["semantic_view_version_id"],
    )
    payload.update(overrides)
    return payload


def _create(scope, **overrides):
    from core.golden_questions import create_golden_question

    conn = scope["conn"]
    validated = validate_golden_question_version(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        payload=_live_definition(scope, **overrides),
    )
    return create_golden_question(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        title="Paid media spend by market",
        owner="owner@example.com",
        validated=validated,
        actor="owner@example.com",
    )


def test_pg_a_head_and_its_first_version_are_created_together(scope):
    from core.golden_questions import get_golden_question

    created = _create(scope)
    assert created["version_number"] == 1

    question = get_golden_question(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        golden_question_id=created["golden_question_id"],
    )
    assert question["current_version_id"] == created["version_id"]
    assert question["lifecycle"] == "draft"
    assert question["current_version"]["expected_render_ref"] is None
    assert len(question["current_version"]["expected_result"]) == 2


def test_pg_an_edit_creates_the_next_version_and_never_rewrites_the_previous(scope):
    from core.golden_questions import create_golden_question_version, get_golden_question

    created = _create(scope)
    first_hash = created["content_hash"]

    validated = validate_golden_question_version(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        payload=_live_definition(scope, severity="major"),
    )
    second = create_golden_question_version(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        golden_question_id=created["golden_question_id"],
        validated=validated,
        actor="owner@example.com",
    )
    assert second["version_number"] == 2
    assert second["predecessor_version_id"] == created["version_id"]

    question = get_golden_question(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        golden_question_id=created["golden_question_id"],
    )
    versions = {v["version_number"]: v for v in question["versions"]}
    assert versions[1]["content_hash"] == first_hash, "version 1 must be byte-for-byte unchanged"
    assert versions[1]["severity"] == "critical"
    assert versions[2]["severity"] == "major"


def test_pg_a_stored_version_refuses_update_and_delete(scope):
    created = _create(scope)
    conn, psycopg = scope["conn"], scope["psycopg"]

    with pytest.raises(psycopg.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE app.golden_question_versions SET severity = 'minor' WHERE id = %s",
            (created["version_id"],),
        )

    with pytest.raises(psycopg.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.golden_question_versions WHERE id = %s", (created["version_id"],)
        )


def test_pg_a_head_cannot_be_moved_to_another_project(scope):
    created = _create(scope)
    conn, psycopg = scope["conn"], scope["psycopg"]

    with pytest.raises(psycopg.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE app.golden_questions SET project_id = %s WHERE id = %s",
            (scope["other_project_id"], created["golden_question_id"]),
        )


def test_pg_a_version_cannot_be_attached_to_a_head_in_another_project(scope):
    from ulid import ULID

    created = _create(scope)
    conn, psycopg = scope["conn"], scope["psycopg"]

    # The composite `(id, org_id, project_id)` foreign key is what refuses this,
    # not the service: a bare `golden_question_id` would have been accepted.
    with pytest.raises(psycopg.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.golden_question_versions
                (id, golden_question_id, org_id, project_id, version_number,
                 business_domain_id, business_domain_version_number,
                 semantic_view_id, semantic_view_version_id, semantic_view_version_role,
                 question, expected_result, required_provenance, expected_ai_path,
                 result_type, capability_tags, severity, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, 1, %s, %s, 'baseline', 'q',
                    '[{"assertion_type":"value","tolerance":null}]'::jsonb,
                    '[{"link_kind":"source","required":true}]'::jsonb,
                    '{}'::jsonb, 'scalar', ARRAY['media_spend_reporting'], 'minor',
                    %s, %s)
            """,
            (
                f"gqv_{ULID()}",
                created["golden_question_id"],
                scope["org_id"],
                scope["other_project_id"],
                scope["domain_id"],
                scope["semantic_view_id"],
                scope["semantic_view_version_id"],
                _hex64("cross-project"),
                "owner@example.com",
            ),
        )


def test_pg_reading_a_question_from_another_project_is_not_found(scope):
    from core.golden_questions import GoldenQuestionNotFound, get_golden_question

    created = _create(scope)
    with pytest.raises(GoldenQuestionNotFound):
        get_golden_question(
            scope["conn"],
            org_id=scope["org_id"],
            project_id=scope["other_project_id"],
            golden_question_id=created["golden_question_id"],
        )


def test_pg_the_structural_trigger_refuses_an_assertion_without_a_tolerance(scope):
    """The database is the rampart, not this module.

    The service refuses first and with a better message; this proves the refusal
    survives a writer that is not the service.
    """
    from ulid import ULID

    created = _create(scope)
    conn, psycopg = scope["conn"], scope["psycopg"]

    with pytest.raises(psycopg.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.golden_question_versions
                (id, golden_question_id, org_id, project_id, version_number,
                 business_domain_id, business_domain_version_number,
                 semantic_view_id, semantic_view_version_id, semantic_view_version_role,
                 question, expected_result, required_provenance, expected_ai_path,
                 result_type, capability_tags, severity, content_hash,
                 predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, %s, 1, %s, %s, 'baseline', 'q',
                    '[{"assertion_type":"value"}]'::jsonb,
                    '[{"link_kind":"source","required":true}]'::jsonb,
                    '{}'::jsonb, 'scalar', ARRAY['media_spend_reporting'], 'minor',
                    %s, %s, %s)
            """,
            (
                f"gqv_{ULID()}",
                created["golden_question_id"],
                scope["org_id"],
                scope["project_id"],
                scope["domain_id"],
                scope["semantic_view_id"],
                scope["semantic_view_version_id"],
                _hex64("no-tolerance"),
                created["version_id"],
                "owner@example.com",
            ),
        )


def test_pg_the_coverage_tab_reports_unverifiable_with_named_owners(scope):
    from core.golden_questions import golden_question_coverage

    created = _create(scope)
    coverage = golden_question_coverage(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        golden_question_id=created["golden_question_id"],
    )
    assert coverage["pinned"]["business_domain"]["id"] == scope["domain_id"]
    assert coverage["pinned"]["expected_assertion_count"] == 2
    verdicts = {d["dimension"]: d for d in coverage["dimensions"]}
    assert verdicts["run_coverage"]["verdict"] == UNVERIFIABLE
    assert verdicts["run_coverage"]["owner_story"] == "51.2"
    assert verdicts["render"]["owner_story"] == "50.4"
    assert verdicts["mcp_app_behavior"]["owner_story"] == "50.6"
    assert verdicts["observed_ai_path_coverage"]["owner_story"] == "49.6"
    # No aggregate score exists, anywhere: a percentage would let a critical
    # failure hide behind an average (analyze-and-test.md:336-337).
    assert "score" not in json.dumps(coverage)


def test_pg_the_governed_pickers_are_served_from_the_live_rows(scope):
    """A hand-maintained UI catalogue keeps offering an archived version."""
    from core.golden_questions import golden_question_options

    options = golden_question_options(
        scope["conn"], org_id=scope["org_id"], project_id=scope["project_id"]
    )
    domains = {d["id"]: d for d in options["business_domains"]}
    assert scope["domain_id"] in domains
    assert domains[scope["domain_id"]]["latest_version_number"] == 1
    views = {v["semantic_view_version_id"] for v in options["semantic_view_versions"]}
    assert scope["semantic_view_version_id"] in views
    assert options["result_types"] == list(RESULT_TYPES)
    assert options["assertion_types"] == list(ASSERTION_TYPES)


def test_pg_lifecycle_advances_only_through_declared_transitions(scope):
    from core.golden_questions import set_lifecycle

    created = _create(scope)
    advanced = set_lifecycle(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        golden_question_id=created["golden_question_id"],
        lifecycle="active",
        actor="owner@example.com",
    )
    assert advanced["lifecycle"] == "active"

    with pytest.raises(GoldenQuestionRefused) as exc:
        set_lifecycle(
            scope["conn"],
            org_id=scope["org_id"],
            project_id=scope["project_id"],
            golden_question_id=created["golden_question_id"],
            lifecycle="draft",
            actor="owner@example.com",
        )
    assert exc.value.code == "invalid_transition"


def test_pg_a_lifecycle_change_on_a_question_of_another_project_is_not_found(scope):
    from core.golden_questions import GoldenQuestionNotFound, set_lifecycle

    created = _create(scope)
    with pytest.raises(GoldenQuestionNotFound):
        set_lifecycle(
            scope["conn"],
            org_id=scope["org_id"],
            project_id=scope["other_project_id"],
            golden_question_id=created["golden_question_id"],
            lifecycle="active",
            actor="owner@example.com",
        )


# ===========================================================================
# THE SEAM. Written here, compared there -- in one test, through both paths.
#
# Until 2026-08-17 nothing crossed it. `test_golden_questions.py` proved the
# write side in the authoring vocabulary, `test_expected_ai_path.py` proved the
# comparison side in the grammar, both suites were green, and the first Golden
# Question with a required node raised `KeyError: 'key'` inside
# `record_case_verdicts` the moment a case pinned an observed path -- the only
# moment the comparison counts. These tests write with the REAL write path and
# compare with the REAL comparison path, so the two can never drift apart again
# without one of them turning red.
# ===========================================================================


def _observed_path(scope, steps, *, outcome="succeeded"):
    """One finalized observed AI Path, written by `ai_paths`, not by fixture SQL."""
    from core.ai_paths import append_step, begin_path, finalize_path

    conn = scope["conn"]
    path = begin_path(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor="owner@example.com",
        tool_catalog_version="tools@1",
    )
    for step in steps:
        append_step(conn, path_id=path["id"], project_id=scope["project_id"], **step)
    finalize_path(conn, path_id=path["id"], project_id=scope["project_id"], outcome=outcome)
    return path["id"]


def _seam_pattern(scope):
    """A pattern that requires the pinned view and forbids one tool."""
    return {
        "grammar_version": 1,
        "required_nodes": [
            {
                "key": f"governance/semantic-view/{scope['semantic_view_id']}",
                "step_kind": "semantic_query",
            }
        ],
        "forbidden_nodes": [
            {"tool_name": "raw_sql_passthrough", "reason": "the view is the only door"}
        ],
    }


def _seam_verdict(scope, ai_path_id, version_id):
    from core.expected_ai_path import evaluate_case_path

    return evaluate_case_path(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        case={"ai_path_id": ai_path_id, "golden_question_version_id": version_id},
    )


def test_pg_a_pattern_written_here_is_read_and_compared_by_the_comparator(scope):
    from core.expected_ai_path import REASON_PATH_MATCHES, VERDICT_PASS

    created = _create(scope, expected_ai_path=_seam_pattern(scope))
    ai_path_id = _observed_path(
        scope,
        [
            {
                "step_kind": "semantic_query",
                "outcome": "succeeded",
                "owner_workspace": "governance",
                "owner_object_type": "semantic-view",
                "owner_object_id": scope["semantic_view_id"],
            }
        ],
    )

    verdict = _seam_verdict(scope, ai_path_id, created["version_id"])

    assert verdict["verdict"] == VERDICT_PASS
    assert verdict["reason_code"] == REASON_PATH_MATCHES
    assert verdict["evidence_refs"]["comparison"]["missing_required_nodes"] == []


def test_pg_a_forbidden_tool_actually_observed_fails_the_case(scope):
    """The finding the two dialects made impossible.

    `forbidden_tools` was written by one side and read by neither: `compare()`
    has never known that field, so a pattern whose only statement was a forbidden
    tool passed by vacuity -- a `pass` earned by a pattern that could not fail.
    """
    from core.expected_ai_path import REASON_FORBIDDEN_NODE_OBSERVED, VERDICT_FAIL

    created = _create(scope, expected_ai_path=_seam_pattern(scope))
    ai_path_id = _observed_path(
        scope,
        [
            {
                "step_kind": "semantic_query",
                "outcome": "succeeded",
                "owner_workspace": "governance",
                "owner_object_type": "semantic-view",
                "owner_object_id": scope["semantic_view_id"],
            },
            {
                "step_kind": "tool_call",
                "outcome": "succeeded",
                "tool_name": "raw_sql_passthrough",
            },
        ],
    )

    verdict = _seam_verdict(scope, ai_path_id, created["version_id"])

    assert verdict["verdict"] == VERDICT_FAIL
    assert verdict["reason_code"] == REASON_FORBIDDEN_NODE_OBSERVED
    hits = verdict["evidence_refs"]["comparison"]["observed_forbidden_nodes"]
    assert [hit["rule"] for hit in hits] == [{"tool_name": "raw_sql_passthrough"}]
    assert hits[0]["reason"] == "the view is the only door"


def test_pg_a_required_node_written_here_is_missed_there_when_it_never_ran(scope):
    from core.expected_ai_path import REASON_MISSING_REQUIRED_NODE, VERDICT_FAIL

    created = _create(scope, expected_ai_path=_seam_pattern(scope))
    ai_path_id = _observed_path(
        scope,
        [{"step_kind": "tool_call", "outcome": "succeeded", "tool_name": "get_card"}],
    )

    verdict = _seam_verdict(scope, ai_path_id, created["version_id"])

    assert verdict["verdict"] == VERDICT_FAIL
    assert verdict["reason_code"] == REASON_MISSING_REQUIRED_NODE
    missed = verdict["evidence_refs"]["comparison"]["missing_required_nodes"]
    assert missed[0]["key"] == f"governance/semantic-view/{scope['semantic_view_id']}"


def test_pg_the_pre_grammar_vocabulary_survives_the_whole_crossing(scope):
    """The authoring shape, written at the door, still fails on its forbidden tool.

    This is the projection proving itself end to end rather than in a unit: what
    a console tab opened before the repair submits is stored as the grammar and
    compared as the grammar.
    """
    from core.expected_ai_path import REASON_FORBIDDEN_NODE_OBSERVED, VERDICT_FAIL

    created = _create(
        scope,
        expected_ai_path={
            "required_nodes": [
                {
                    "step_kind": "semantic_query",
                    "owner_workspace": "governance",
                    "owner_object_type": "semantic-view",
                    "owner_object_id": scope["semantic_view_id"],
                }
            ],
            "forbidden_tools": ["raw_sql_passthrough"],
        },
    )
    ai_path_id = _observed_path(
        scope,
        [
            {
                "step_kind": "semantic_query",
                "outcome": "succeeded",
                "owner_workspace": "governance",
                "owner_object_type": "semantic-view",
                "owner_object_id": scope["semantic_view_id"],
            },
            {
                "step_kind": "tool_call",
                "outcome": "succeeded",
                "tool_name": "raw_sql_passthrough",
            },
        ],
    )

    verdict = _seam_verdict(scope, ai_path_id, created["version_id"])

    assert verdict["verdict"] == VERDICT_FAIL
    assert verdict["reason_code"] == REASON_FORBIDDEN_NODE_OBSERVED


def test_pg_a_question_declaring_no_path_is_unverifiable_and_never_a_pass(scope):
    """The vacuous pass, closed at the seam.

    An empty pattern used to be stored as an object of empty lists, which is
    truthy, so `compare()` found nothing and every observed path `pass`ed.
    """
    from core.expected_ai_path import REASON_NO_EXPECTED_PATTERN, VERDICT_UNVERIFIABLE

    created = _create(scope, expected_ai_path={"required_nodes": []})
    ai_path_id = _observed_path(
        scope,
        [{"step_kind": "tool_call", "outcome": "succeeded", "tool_name": "raw_sql_passthrough"}],
    )

    verdict = _seam_verdict(scope, ai_path_id, created["version_id"])

    assert verdict["verdict"] == VERDICT_UNVERIFIABLE
    assert verdict["reason_code"] == REASON_NO_EXPECTED_PATTERN


def test_an_owner_object_type_the_column_cannot_hold_is_refused_and_the_detail_word_is_accepted():
    """AI-376 (Opus F2): the VALUE is judged, not only the key -- a word the column refuses names a
    node no observed step can match; the detail vocabulary (`schema_doc`) is accepted, compared as
    the column's word."""
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "required_nodes": [
                        {"owner_workspace": "context-hub", "owner_object_type": "NOT a slug!!", "owner_object_id": "x_1"}
                    ]
                }
            )
        )
    assert "unrecordable_owner_object_type" in _codes(exc.value)
    _validate(
        _definition(
            expected_ai_path={
                "required_nodes": [
                    {"owner_workspace": "context-hub", "owner_object_type": "schema_doc", "owner_object_id": "doc_1"}
                ]
            }
        )
    )


def test_a_forbidden_node_the_column_cannot_hold_is_refused_in_both_vocabularies():
    """AI-376 (Opus N1): the value check reaches forbidden nodes, grammar or pre-grammar."""
    with pytest.raises(GoldenQuestionRefused) as exc:
        _validate(
            _definition(
                expected_ai_path={
                    "grammar_version": 1,
                    "required_nodes": [{"key": "context-hub/topic/t1"}],
                    "forbidden_nodes": [{"owner_object_type": "NOT a slug!!"}],
                }
            )
        )
    assert "unrecordable_owner_object_type" in _codes(exc.value)
    # The pre-grammar vocabulary has no forbidden nodes at all (`unknown_pattern_field`); the
    # detail word on a grammar forbidden node is accepted -- it is compared as the column's word.
    _validate(
        _definition(
            expected_ai_path={
                "grammar_version": 1,
                "required_nodes": [{"key": "context-hub/topic/t1"}],
                "forbidden_nodes": [{"owner_object_type": "schema_doc"}],
            }
        )
    )
