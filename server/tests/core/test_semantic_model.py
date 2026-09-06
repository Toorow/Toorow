"""Story 49.3 — the restricted semantic expression contract and the change set.

What these tests hold in place:

* an operation outside the allowlist is refused at parse time, so neither a
  browser nor a connector can submit executable SQL as semantic truth;
* equal storage is not equal meaning: clicks + money, EUR + USD and two
  durations in different units are three separate named refusals;
* a reference without an exact version is refused, and one that parses by name
  can never be published;
* a metric that declares neither an aggregation nor non-additivity is refused;
* editing without an exact base version is refused rather than rebased.
"""

from __future__ import annotations

import pytest
from core.semantic_expressions import (
    ALLOWED_OPERATIONS,
    EXPRESSION_CONTRACT_VERSION,
    MAX_EXPRESSION_DEPTH,
    ConceptResolver,
    detect_cycle,
    topological_order,
    validate_aggregation,
    validate_expression,
)

MONEY_EUR = {"value_type": "money", "currency_behavior": {"scope": "EUR"}}
MONEY_USD = {"value_type": "money", "currency_behavior": {"scope": "USD"}}
CLICKS = {"value_type": "integer"}
SECONDS = {"value_type": "duration", "unit": "second"}
MINUTES = {"value_type": "duration", "unit": "minute"}
RATE = {"value_type": "ratio"}


def _resolver(**versions) -> ConceptResolver:
    return ConceptResolver({tuple(key.split("@")): value for key, value in versions.items()})


def _ref(concept_id: str, version_id: str) -> dict:
    return {"op": "concept_ref", "concept_id": concept_id, "version_id": version_id}


def _codes(analysis) -> list[str]:
    return [refusal.code for refusal in analysis.refusals]


# ---------------------------------------------------------------------------
# The allowlist is closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    ["raw_sql", "sql", "eval", "exec", "javascript", "lookup", "window_function"],
)
def test_an_operation_outside_the_allowlist_is_refused(op):
    analysis = validate_expression({"op": op, "sql": "SELECT 1"}, _resolver())
    assert _codes(analysis) == ["unknown_operation"]
    assert not analysis.ok


def test_the_allowlist_is_exported_so_the_workbench_cannot_drift_from_it():
    # The palette the browser offers is built from THIS tuple. A hand-kept copy
    # is how a UI ends up offering an operation the server refuses.
    assert "raw_sql" not in ALLOWED_OPERATIONS
    assert set(ALLOWED_OPERATIONS) == {
        "add",
        "aggregate",
        "comparison",
        "concept_name",
        "concept_ref",
        "conditional",
        "literal",
        "multiply",
        "ratio",
        "source_measure",
        "subtract",
    }
    assert EXPRESSION_CONTRACT_VERSION == "semantic-expression.v1"


# ---------------------------------------------------------------------------
# Equal storage is not equal meaning
# ---------------------------------------------------------------------------


def test_a_count_and_an_amount_of_money_cannot_be_added():
    analysis = validate_expression(
        {"op": "add", "operands": [_ref("c1", "v1"), _ref("c2", "v2")]},
        _resolver(**{"c1@v1": MONEY_EUR, "c2@v2": CLICKS}),
    )
    assert _codes(analysis) == ["incompatible_types"]


def test_money_in_two_currencies_cannot_be_added():
    analysis = validate_expression(
        {"op": "add", "operands": [_ref("c1", "v1"), _ref("c3", "v3")]},
        _resolver(**{"c1@v1": MONEY_EUR, "c3@v3": MONEY_USD}),
    )
    assert _codes(analysis) == ["incompatible_currency"]
    assert "EUR" in analysis.refusals[0].message and "USD" in analysis.refusals[0].message


def test_durations_in_two_units_cannot_be_added():
    analysis = validate_expression(
        {"op": "add", "operands": [_ref("s", "v1"), _ref("m", "v2")]},
        _resolver(**{"s@v1": SECONDS, "m@v2": MINUTES}),
    )
    assert _codes(analysis) == ["incompatible_unit"]


def test_two_rates_cannot_be_added():
    analysis = validate_expression(
        {"op": "add", "operands": [_ref("a", "v1"), _ref("b", "v2")]},
        _resolver(**{"a@v1": RATE, "b@v2": RATE}),
    )
    assert _codes(analysis) == ["ratio_arithmetic"]


def test_two_dimensioned_quantities_cannot_be_multiplied():
    analysis = validate_expression(
        {"op": "multiply", "operands": [_ref("a", "v1"), _ref("b", "v2")]},
        _resolver(**{"a@v1": MONEY_EUR, "b@v2": MONEY_EUR}),
    )
    assert _codes(analysis) == ["dimensioned_product"]


def test_money_times_a_dimensionless_factor_keeps_its_currency():
    analysis = validate_expression(
        {
            "op": "multiply",
            "operands": [_ref("a", "v1"), {"op": "literal", "value": 1.2, "value_type": "decimal"}],
        },
        _resolver(**{"a@v1": MONEY_EUR}),
    )
    assert analysis.ok
    assert analysis.result.value_type == "money"
    assert analysis.result.currency == "EUR"


# ---------------------------------------------------------------------------
# References are exact
# ---------------------------------------------------------------------------


def test_a_reference_without_a_version_is_refused():
    analysis = validate_expression(
        {"op": "concept_ref", "concept_id": "c1"}, _resolver(**{"c1@v1": CLICKS})
    )
    assert _codes(analysis) == ["malformed_node"]
    assert "latest" in analysis.refusals[0].message


def test_a_reference_to_another_projects_version_is_unknown_not_leaked():
    # The resolver holds only what THIS Project may read, so a foreign id is
    # simply absent. The refusal names neither the object nor its owner.
    analysis = validate_expression(_ref("c_other", "v_other"), _resolver(**{"c1@v1": CLICKS}))
    assert _codes(analysis) == ["unknown_reference"]


def test_a_name_reference_parses_but_can_never_be_published():
    analysis = validate_expression(
        {
            "op": "ratio",
            "zero_denominator": "null",
            "numerator": {"op": "concept_name", "name": "clicks"},
            "denominator": {"op": "concept_name", "name": "impressions"},
        },
        _resolver(),
    )
    assert analysis.ok, "the migrated shape must remain readable"
    assert not analysis.publishable
    assert sorted(analysis.unresolved_names) == ["clicks", "impressions"]


def test_a_source_measure_may_only_name_its_own_concept():
    analysis = validate_expression(
        {"op": "source_measure", "concept": "someone_else"},
        _resolver(),
        owning_concept_name="revenue",
    )
    assert _codes(analysis) == ["foreign_source_measure"]


# ---------------------------------------------------------------------------
# Ratios, conditionals and aggregations declare what they mean
# ---------------------------------------------------------------------------


def test_a_ratio_must_declare_its_zero_denominator_behavior():
    analysis = validate_expression(
        {
            "op": "ratio",
            "numerator": {"op": "literal", "value": 1, "value_type": "integer"},
            "denominator": {"op": "literal", "value": 2, "value_type": "integer"},
        },
        _resolver(),
    )
    assert _codes(analysis) == ["undeclared_zero_behavior"]


def test_a_conditional_must_declare_its_otherwise_branch():
    analysis = validate_expression(
        {
            "op": "conditional",
            "when": [
                {
                    "condition": {
                        "op": "comparison",
                        "operator": "gt",
                        "left": {"op": "literal", "value": 1, "value_type": "integer"},
                        "right": {"op": "literal", "value": 0, "value_type": "integer"},
                    },
                    "then": {"op": "literal", "value": 1, "value_type": "integer"},
                }
            ],
        },
        _resolver(),
    )
    assert _codes(analysis) == ["undeclared_default_branch"]


def test_summing_a_ratio_is_refused():
    analysis = validate_expression(
        {"op": "aggregate", "function": "sum", "operand": _ref("r", "v1")},
        _resolver(**{"r@v1": RATE}),
    )
    assert _codes(analysis) == ["unsafe_sum"]


def test_a_metric_declaring_neither_aggregation_nor_non_additivity_is_refused():
    refusals = validate_aggregation(
        None, additivity_class="additive", non_additive_dimensions=[], value_type="decimal"
    )
    assert [r.code for r in refusals] == ["undeclared_aggregation"]


def test_a_metric_declared_non_additive_needs_no_aggregation():
    assert (
        validate_aggregation(
            None, additivity_class="non_additive", non_additive_dimensions=[], value_type="ratio"
        )
        == []
    )


def test_an_average_cannot_be_declared_fully_additive():
    refusals = validate_aggregation(
        {"function": "average"},
        additivity_class="additive",
        non_additive_dimensions=[],
        value_type="decimal",
    )
    assert [r.code for r in refusals] == ["additivity_contradiction"]


def test_semi_additive_must_name_the_dimensions_it_cannot_cross():
    refusals = validate_aggregation(
        {"function": "sum"},
        additivity_class="semi_additive",
        non_additive_dimensions=[],
        value_type="decimal",
    )
    assert [r.code for r in refusals] == ["undeclared_non_additive_dimensions"]


def test_additive_and_named_non_additive_dimensions_contradict_each_other():
    refusals = validate_aggregation(
        {"function": "sum"},
        additivity_class="additive",
        non_additive_dimensions=["date"],
        value_type="decimal",
    )
    assert [r.code for r in refusals] == ["additivity_contradiction"]


# ---------------------------------------------------------------------------
# Bounds and the DAG
# ---------------------------------------------------------------------------


def test_an_over_deep_formula_is_refused_rather_than_walked():
    node: dict = {"op": "literal", "value": 1, "value_type": "integer"}
    for _ in range(MAX_EXPRESSION_DEPTH + 2):
        node = {
            "op": "add",
            "operands": [node, {"op": "literal", "value": 1, "value_type": "integer"}],
        }
    analysis = validate_expression(node, _resolver())
    assert "expression_too_deep" in _codes(analysis)


def test_a_cycle_is_reported_as_its_exact_path():
    assert detect_cycle("a", {"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    assert detect_cycle("a", {"a": ["b"], "b": []}) is None


def test_a_dag_has_an_evaluation_order_and_a_cycle_has_none():
    assert topological_order({"a": ["b"], "b": ["c"], "c": []}) == ["a", "b", "c"]
    assert topological_order({"a": ["b"], "b": ["a"]}) is None


# ---------------------------------------------------------------------------
# Change-set guards that need no database
# ---------------------------------------------------------------------------


def test_editing_without_an_exact_base_version_is_refused():
    from core.semantic_model import SemanticRefused, create_change_set

    with pytest.raises(SemanticRefused) as excinfo:
        create_change_set(
            object(),
            "proj_EXAMPLE",
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id="sc_1",
            base_version_id=None,
            intent={"action": "edit_concept", "concept": {}},
            idempotency_key="key-1",
        )
    assert excinfo.value.code == "missing_exact_base"


def test_a_consequential_command_without_an_idempotency_key_is_refused():
    from core.semantic_model import SemanticRefused, create_change_set

    with pytest.raises(SemanticRefused) as excinfo:
        create_change_set(
            object(),
            "proj_EXAMPLE",
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=None,
            base_version_id=None,
            intent={"action": "create_concept", "concept": {}},
            idempotency_key="",
        )
    assert excinfo.value.code == "missing_idempotency_key"


def test_an_unregistered_intent_is_refused():
    from core.semantic_model import SemanticRefused, create_change_set

    with pytest.raises(SemanticRefused) as excinfo:
        create_change_set(
            object(),
            "proj_EXAMPLE",
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=None,
            base_version_id=None,
            intent={"action": "delete_everything"},
            idempotency_key="key-1",
        )
    assert excinfo.value.code == "unknown_intent"


# ---------------------------------------------------------------------------
# Story 60.2 — the walk a person actually makes: create → prepare → confirm.
#
# Everything above this line is a refusal. Nothing proved that the contract lets
# a correct metric THROUGH, and "the validator refuses the wrong thing" is not
# evidence that anything can be published — 49.3 shipped a contract that no
# surface could satisfy for exactly that reason.
#
# Pg-gated: these need the real DDL of migrations 142 and 237. Skipped, never
# faked, when TEST_POSTGRES_DSN is unset.
# ---------------------------------------------------------------------------

import os  # noqa: E402
import uuid  # noqa: E402


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def fixture_project():
    """One org and one Project, removed through the repository's own eraser."""
    from core.db import get_connection

    org_id, project_id = _uid("org"), _uid("proj")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 60.2 fixture', %s, 'active', 'owner@example.com')",
                (org_id, org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 60.2 fixture', %s, 'owner@example.com')",
                (project_id, org_id, project_id.replace("_", "-")),
            )
        conn.commit()
    yield {"org_id": org_id, "project_id": project_id}
    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        purge_fixture_org(conn, org_id)
        conn.commit()


def _record_test_gate(
    conn,
    org_id: str,
    project_id: str,
    change_set_id: str,
    *,
    decision: str = "pass",
    object_type: str = "semantic-concept",
) -> str:
    """Write the Gate Decision the Test workspace emits for THIS candidate.

    REWRITTEN 2026-08-16, AND THE REWRITE IS THE POINT. This helper used to
    fabricate an `app.audit_log(action = 'semantic_model.test_gate')` row —
    a shape nothing in the product has ever written, invented here so the walk
    below could get past a gate no producer satisfied. A fixture that mints its
    own evidence format proves only that the reader can read the fixture.

    `_evaluate_test_gate` now reads `app.evaluation_gate_decisions`, the object
    `analyze-and-test.md:399-405` says Test emits and Governance checks. So this
    seeds THAT — through the same columns `evaluation_runs.emit_gate_decision`
    writes. The evidence chain above it (profile, context set, two runs, the
    comparison) is seeded directly because this test is about the Governance
    CONSUMER; how Test computes a decision is proven in `test_evaluation_runs.py`.
    """
    from ulid import ULID

    zero_hash = "0" * 64
    profile_id, context_set_id = f"erp_{ULID()}", f"ecvs_{ULID()}"
    view_id, view_version_id = f"sv_{ULID()}", f"svv_{ULID()}"
    # UNIQUE PER CALL. This name used to be the literal 'gate_fixture_view', and
    # `uq_semantic_views_name_project` made a SECOND call in one project a
    # UniqueViolation -- so no test could ever walk two change sets through the
    # gate on the same project, which is exactly what an edit test has to do.
    view_name = f"gate_fixture_view_{str(view_id)[-8:].lower()}"
    baseline_run, candidate_run = f"erun_{ULID()}", f"erun_{ULID()}"
    comparison_id, decision_id = f"ecmp_{ULID()}", f"egd_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.evaluation_run_profiles (id, org_id, project_id, name, "
            "evidence_mode, created_by) VALUES "
            "(%s, %s, %s, %s, 'offline', 'test-owner@example.com')",
            (profile_id, org_id, project_id, f"gate {profile_id}"),
        )
        cur.execute(
            "INSERT INTO app.evaluation_context_version_sets (id, org_id, project_id, "
            "content_hash) VALUES (%s, %s, %s, %s)",
            (context_set_id, org_id, project_id, zero_hash),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, "
            "created_by) VALUES "
            "(%s, %s, %s, 'published', 'test-owner@example.com')",
            (view_id, project_id, view_name),
        )
        cur.execute(
            "INSERT INTO app.semantic_view_versions (id, view_id, project_id, version_number, "
            "status, name, label, dependency_fingerprint, content_hash, created_by) "
            "VALUES (%s, %s, %s, 1, 'published', %s, 'Gate fixture', "
            "%s, %s, 'test-owner@example.com')",
            (view_version_id, view_id, project_id, view_name, zero_hash, zero_hash),
        )
        for run_id in (baseline_run, candidate_run):
            cur.execute(
                """
                INSERT INTO app.evaluation_runs
                    (id, org_id, project_id, run_profile_id, evidence_mode,
                     semantic_view_id, semantic_view_version_id, context_version_set_id,
                     model_ref, tool_catalog_version, data_snapshot_hash, as_of, created_by)
                VALUES (%s, %s, %s, %s, 'offline', %s, %s, %s, 'claude-opus-5',
                        %s, %s, DATE '2026-08-16', 'test-owner@example.com')
                """,
                (
                    run_id, org_id, project_id, profile_id,
                    view_id, view_version_id, context_set_id, zero_hash, zero_hash,
                ),
            )
        cur.execute(
            """
            INSERT INTO app.evaluation_comparisons
                (id, org_id, project_id, baseline_run_id, candidate_run_id,
                 comparison_kind, changed_pin_families, held_constant_fingerprint, created_by)
            VALUES (%s, %s, %s, %s, %s, 'semantic', '["semantic"]'::jsonb, %s,
                    'test-owner@example.com')
            """,
            (comparison_id, org_id, project_id, baseline_run, candidate_run, zero_hash),
        )
        cur.execute(
            """
            INSERT INTO app.evaluation_gate_decisions
                (id, org_id, project_id, comparison_id, decision, candidate_owner_workspace,
                 candidate_object_type, candidate_object_id, candidate_version_id,
                 coverage, failing_dimensions, decision_reason, decided_by, content_hash)
            VALUES (%s, %s, %s, %s, %s, 'governance', %s, %s, %s,
                    '{"eligible": 1, "evaluated": 1, "missing": 0}'::jsonb,
                    %s::jsonb, %s, 'test-owner@example.com', %s)
            """,
            (
                decision_id, org_id, project_id, comparison_id, decision,
                object_type,
                # A creation carries no object id until confirm; Test names the
                # candidate it was given, which is the change set.
                change_set_id, change_set_id,
                "[]" if decision == "pass" else '["correctness"]',
                "Every applicable Golden Question passed."
                if decision == "pass"
                else "A correctness regression was observed.",
                zero_hash,
            ),
        )
    conn.commit()
    return decision_id


#: A metric that says everything the contract asks: a ratio over two literals,
#: an explicit zero-denominator policy, an aggregation that is not `sum`, and the
#: additivity class the render will later read. Its NAME carries no ratio token —
#: `is_ratio_name("efficiency_index")` is False — so nothing about it is inferred.
_RATIO_CONCEPT = {
    "kind": "metric",
    "name": "efficiency_index",
    "label": "Efficiency index",
    "value_type": "ratio",
    "definition": "Value produced per unit of effort.",
    "expression": {
        "op": "ratio",
        "zero_denominator": "null",
        "numerator": {"op": "literal", "value_type": "decimal", "value": 1},
        "denominator": {"op": "literal", "value_type": "decimal", "value": 2},
    },
    "aggregation": {"function": "average"},
    "additivity_class": "non_additive",
    "non_additive_dimensions": [],
    "business_domain_refs": [],
}


@pg_available
def test_a_ratio_metric_walks_create_prepare_confirm_and_is_published(fixture_project):
    from core.db import get_connection
    from core.semantic_model import (
        confirm_change_set,
        create_change_set,
        prepare_change_set,
    )

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        change_set = create_change_set(
            conn,
            project_id,
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=None,
            base_version_id=None,
            intent={"action": "create_concept", "concept": _RATIO_CONCEPT},
            idempotency_key=_uid("idem"),
        )
        conn.commit()
        _record_test_gate(conn, fixture_project["org_id"], project_id, change_set.id)

        prepared = prepare_change_set(
            conn, project_id, change_set.id, actor="owner@example.com"
        )
        conn.commit()

        # The contract accepted the formula AND the declared additivity.
        assert prepared["refusals"] == []
        assert prepared["validation"]["publishable"] is True
        # The single-use token is what the dialog needs, and it must be there.
        assert prepared.get("confirmation_token")

        result = confirm_change_set(
            conn,
            project_id,
            change_set.id,
            actor="owner@example.com",
            confirmation_token=prepared["confirmation_token"],
            org_id=fixture_project["org_id"],
        )
        conn.commit()

        version_id = result["result_version_id"]
        assert version_id

        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, kind, status, additivity_class, expression->>'op' "
                "FROM app.semantic_concept_versions WHERE id = %s",
                (version_id,),
            )
            row = cur.fetchone()
    # The row that a render will later read: the class is STORED, not inferred.
    assert row == ("efficiency_index", "metric", "published", "non_additive", "ratio")


@pg_available
def test_the_same_metric_declared_additive_is_refused_and_nothing_is_published(
    fixture_project,
):
    from core.db import get_connection
    from core.semantic_model import (
        SemanticRefused,
        confirm_change_set,
        create_change_set,
        prepare_change_set,
    )

    project_id = fixture_project["project_id"]
    contradiction = {**_RATIO_CONCEPT, "additivity_class": "additive"}
    with get_connection() as conn:
        change_set = create_change_set(
            conn,
            project_id,
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=None,
            base_version_id=None,
            intent={"action": "create_concept", "concept": contradiction},
            idempotency_key=_uid("idem"),
        )
        conn.commit()
        _record_test_gate(conn, fixture_project["org_id"], project_id, change_set.id)

        prepared = prepare_change_set(
            conn, project_id, change_set.id, actor="owner@example.com"
        )
        conn.commit()

        codes = [refusal["code"] for refusal in prepared["refusals"]]
        assert "additivity_contradiction" in codes
        assert prepared["validation"]["publishable"] is False

        with pytest.raises(SemanticRefused) as excinfo:
            confirm_change_set(
                conn,
                project_id,
                change_set.id,
                actor="owner@example.com",
                confirmation_token=prepared["confirmation_token"],
                org_id=fixture_project["org_id"],
            )
        conn.commit()
        assert excinfo.value.code == "not_publishable"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.semantic_concept_versions WHERE project_id = %s",
                (project_id,),
            )
            assert cur.fetchone()[0] == 0


@pg_available
def test_a_metric_without_its_class_is_refused_by_name_and_never_reaches_the_check(
    fixture_project,
):
    """The refusal is the APPLICATION's, and the constraint is only the net.

    `{"function": "sum"}` with no `additivity_class` is precisely the row
    migration 237 refuses. Reaching the database with it produces a
    `CheckViolation`, which `semantic_model_api.py:131-138` converts into a 503
    `semantic_model_unavailable` — a sentence about the service, for a fact about
    a field. So `prepare` must refuse it by name, and `confirm` must stop on that
    refusal rather than on the constraint.

    The last assertion is the one that matters: `SemanticRefused`, not
    `psycopg.errors.CheckViolation`. Deleting the application guard and leaving
    the constraint would still keep the bad row out of the table, and would still
    be the defect this test exists to prevent.
    """
    import psycopg
    from core.db import get_connection
    from core.semantic_model import (
        SemanticRefused,
        confirm_change_set,
        create_change_set,
        prepare_change_set,
    )

    project_id = fixture_project["project_id"]
    undeclared = {
        **_RATIO_CONCEPT,
        "value_type": "decimal",
        "expression": {"op": "source_measure", "concept": "efficiency_index"},
        "aggregation": {"function": "sum"},
    }
    undeclared.pop("additivity_class")

    with get_connection() as conn:
        change_set = create_change_set(
            conn,
            project_id,
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=None,
            base_version_id=None,
            intent={"action": "create_concept", "concept": undeclared},
            idempotency_key=_uid("idem"),
        )
        conn.commit()
        _record_test_gate(conn, fixture_project["org_id"], project_id, change_set.id)

        prepared = prepare_change_set(
            conn, project_id, change_set.id, actor="owner@example.com"
        )
        conn.commit()

        named = [refusal for refusal in prepared["refusals"]]
        assert [refusal["code"] for refusal in named] == ["undeclared_aggregation"]
        assert named[0]["path"] == "$.additivity_class"
        assert prepared["validation"]["publishable"] is False

        with pytest.raises(SemanticRefused) as excinfo:
            confirm_change_set(
                conn,
                project_id,
                change_set.id,
                actor="owner@example.com",
                confirmation_token=prepared["confirmation_token"],
                org_id=fixture_project["org_id"],
            )
        conn.commit()
        assert excinfo.value.code == "not_publishable"
        # And NOT the database's answer, which the API would have dressed as 503.
        assert not isinstance(excinfo.value, psycopg.Error)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.semantic_concept_versions WHERE project_id = %s",
                (project_id,),
            )
            assert cur.fetchone()[0] == 0


@pg_available
def test_without_a_test_verdict_publication_is_blocked_and_says_which_gate(
    fixture_project,
):
    """The blocker this story does NOT lift, measured rather than left implicit.

    No Test verdict is recorded here. `_evaluate_test_gate` answers
    `unverifiable / no_test_coverage`, and that alone makes the change set
    unpublishable — even with a formula and an additivity class the contract
    accepts. Whoever draws the creation screen (60.4) needs to read this here
    rather than rediscover it against a green validator.
    """
    from core.db import get_connection
    from core.semantic_model import create_change_set, prepare_change_set

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        change_set = create_change_set(
            conn,
            project_id,
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=None,
            base_version_id=None,
            intent={"action": "create_concept", "concept": _RATIO_CONCEPT},
            idempotency_key=_uid("idem"),
        )
        conn.commit()
        prepared = prepare_change_set(
            conn, project_id, change_set.id, actor="owner@example.com"
        )
        conn.commit()

    assert prepared["refusals"] == []  # the formula is fine
    assert prepared["validation"]["publishable"] is False  # the gate is not
    assert prepared["validation"]["test_gate"]["state"] == "unverifiable"
    assert prepared["validation"]["test_gate"]["reason"] == "no_test_coverage"


def _prepare_with_gate(fixture_project, **gate):
    """Create a Concept change set, seed ONE gate decision, prepare it."""
    from core.db import get_connection
    from core.semantic_model import create_change_set, prepare_change_set

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        change_set = create_change_set(
            conn,
            project_id,
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=None,
            base_version_id=None,
            intent={"action": "create_concept", "concept": _RATIO_CONCEPT},
            idempotency_key=_uid("idem"),
        )
        conn.commit()
        _record_test_gate(
            conn, fixture_project["org_id"], project_id, change_set.id, **gate
        )
        prepared = prepare_change_set(
            conn, project_id, change_set.id, actor="owner@example.com"
        )
        conn.commit()
    return prepared


@pg_available
def test_a_test_gate_pass_publishes_without_an_override(fixture_project):
    """THE DEAD END THIS REPAIR OPENS, and the reason it mattered.

    Before 2026-08-16 the gate read an `app.audit_log` action nothing has ever
    written, so EVERY candidate answered `unverifiable` and the written override
    was the only way a Concept ever left the console. The override is meant to be
    the escape hatch (`first-figure-path.md:93-95`), not the door.
    """
    prepared = _prepare_with_gate(fixture_project)
    assert prepared["validation"]["test_gate"]["state"] == "pass"
    assert prepared["validation"]["publishable"] is True
    # A pass is EVIDENCE, not a boolean: the comparison it came from travels too.
    assert prepared["validation"]["test_gate"]["comparison_id"]
    assert prepared["validation"]["test_gate"]["coverage"]["missing"] == 0
    # And it is a pass on its own merit -- no override was recorded.
    assert "override" not in prepared["validation"]["test_gate"]


@pg_available
def test_a_blocking_test_gate_refuses_and_quotes_the_reason(fixture_project):
    """`block` is a FAIL, and the failing dimensions travel with it."""
    prepared = _prepare_with_gate(fixture_project, decision="block")
    gate = prepared["validation"]["test_gate"]
    assert gate["state"] == "fail"
    assert gate["reason"] == "test_gate_failed"
    assert gate["failing_dimensions"] == ["correctness"]
    assert prepared["validation"]["publishable"] is False


@pg_available
def test_a_verdict_decided_for_another_object_is_not_coverage_of_this_one(
    fixture_project,
):
    """A View's verdict must never answer for a Concept on the same change set."""
    prepared = _prepare_with_gate(fixture_project, object_type="semantic-view")
    gate = prepared["validation"]["test_gate"]
    assert gate["state"] == "unverifiable"
    assert gate["reason"] == "test_gate_object_mismatch"
    assert prepared["validation"]["publishable"] is False


# ---------------------------------------------------------------------------
# Retiring what was published — 2026-08-18
#
# `archive_object` has been in `_SUPPORTED_INTENTS` since the change set existed
# and `_apply_change_set` raised `unsupported_intent` for it, so an archive change
# set could be created, could be prepared, and could never be confirmed. Nothing
# in the product could retire a Concept or a Semantic View.
#
# No migration was needed for any of it: `app.semantic_concepts` and
# `app.semantic_views` have carried `lifecycle_status ... 'archived'` since
# migration 142, and both name-uniqueness indexes are already partial
# `WHERE lifecycle_status <> 'archived'` — "archived rows excluded so a name can
# be retired and re-minted".
# ---------------------------------------------------------------------------


def test_the_archive_refusal_names_its_holders_and_the_kind_that_does_not_block():
    from core.semantic_model import _archive_refusal

    refusal = _archive_refusal(
        [
            {
                "object_type": "semantic-view",
                "id": "sv_1",
                "label": "monthly_sales",
                "state": "published",
            },
            {
                "object_type": "semantic-view",
                "id": "sv_2",
                "label": "draft_view",
                "state": "draft",
            },
        ]
    ).as_dict()
    assert refusal["code"] == "live_consumers"
    # NAMED, not counted: a bare count sends a person hunting for what to fix.
    assert "monthly_sales (published)" in refusal["message"]
    assert "draft_view (draft)" in refusal["message"]
    # And the rule ratified on 2026-08-16 is stated where the person reads it:
    # history does not block, so an object never becomes unretirable.
    assert "superseded" in refusal["message"]


_ARCHIVABLE_CONCEPT = {
    "kind": "dimension",
    "name": "retirable_axis",
    "label": "Retirable axis",
    "value_type": "string",
    "semantic_type": "channel",
    "definition": "An axis nobody ended up using.",
    "business_domain_refs": [],
}

_ARCHIVE_REASON = "Superseded by the conformed channel dimension; nothing reads it."


def _publish_concept(conn, fixture_project, concept: dict) -> tuple[str, str]:
    """The create -> prepare -> confirm walk, so the archive tests start from a
    REAL published object rather than a row typed into the table."""
    from core.semantic_model import confirm_change_set, create_change_set, prepare_change_set

    project_id = fixture_project["project_id"]
    change_set = create_change_set(
        conn,
        project_id,
        actor="owner@example.com",
        object_type="semantic-concept",
        object_id=None,
        base_version_id=None,
        intent={"action": "create_concept", "concept": concept},
        idempotency_key=_uid("idem"),
    )
    conn.commit()
    _record_test_gate(conn, fixture_project["org_id"], project_id, change_set.id)
    prepared = prepare_change_set(conn, project_id, change_set.id, actor="owner@example.com")
    conn.commit()
    assert prepared["validation"]["publishable"] is True, prepared["refusals"]
    result = confirm_change_set(
        conn,
        project_id,
        change_set.id,
        actor="owner@example.com",
        confirmation_token=prepared["confirmation_token"],
        org_id=fixture_project["org_id"],
    )
    conn.commit()
    version_id = result["result_version_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT concept_id FROM app.semantic_concept_versions WHERE id = %s", (version_id,)
        )
        concept_id = cur.fetchone()[0]
    return str(concept_id), str(version_id)


def _archive(conn, fixture_project, object_type: str, object_id: str, version_id: str):
    """Open and prepare the archive change set.

    The written override is not a shortcut here: the module has ONE Test gate,
    and a retirement has no verdict of its own for Test to have recorded, so the
    reason is what carries it -- stored with its author and the state it
    replaced. The console offers the same panel for exactly this reason.
    """
    from core.semantic_model import create_change_set, prepare_change_set

    project_id = fixture_project["project_id"]
    change_set = create_change_set(
        conn,
        project_id,
        actor="owner@example.com",
        object_type=object_type,
        object_id=object_id,
        base_version_id=version_id,
        intent={"action": "archive_object"},
        idempotency_key=_uid("idem"),
    )
    conn.commit()
    prepared = prepare_change_set(
        conn,
        project_id,
        change_set.id,
        actor="owner@example.com",
        allow_test_override={"reason": _ARCHIVE_REASON},
    )
    conn.commit()
    return change_set, prepared


def _drop_holding_view(conn, holder_id: str, holder_version: str) -> None:
    """Remove the hand-seeded holder.

    NOT cosmetic: `purge_org_tree` deletes `app.semantic_concepts` before
    `app.semantic_view_version_concepts`, whose FK to the concept carries no
    `ON DELETE CASCADE`, so a Project where a View pins a Concept cannot be
    erased. That is a defect of the eraser and not of this test -- reported,
    not repaired here -- and these fixtures clean up after themselves so the
    finding stays visible instead of turning every archive test red.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.semantic_view_version_concepts WHERE view_version_id = %s",
            (holder_version,),
        )
    conn.commit()
    # The view and its version stay: `app.reject_semantic_version_mutation`
    # forbids deleting a published version and the org eraser reaches them
    # through the Project, exactly as it does for every other fixture here.
    assert holder_id


def _seed_holding_view(conn, project_id: str, name: str, status: str, concept):
    """A Semantic View version that PINS the concept, at the status given."""
    from ulid import ULID  # noqa: PLC0415 -- fixture-local, like every helper here

    concept_id, version_id = concept
    holder_id, holder_version = f"sv_{ULID()}", f"svv_{ULID()}"
    zero_hash = "0" * 64
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by) "
            "VALUES (%s, %s, %s, 'published', 'owner@example.com')",
            (holder_id, project_id, name),
        )
        cur.execute(
            "INSERT INTO app.semantic_view_versions (id, view_id, project_id, version_number, "
            "status, name, label, dependency_fingerprint, content_hash, created_by) "
            "VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, 'owner@example.com')",
            (holder_version, holder_id, project_id, status, name, name, zero_hash, zero_hash),
        )
        cur.execute(
            "INSERT INTO app.semantic_view_version_concepts (view_version_id, ordinal, "
            "concept_id, concept_version_id, role) VALUES (%s, 0, %s, %s, 'dimension')",
            (holder_version, concept_id, version_id),
        )
    conn.commit()
    return holder_id, holder_version


@pg_available
def test_an_unheld_concept_is_retired_and_not_one_of_its_versions_is_rewritten(
    fixture_project,
):
    from core.db import get_connection
    from core.semantic_model import confirm_change_set

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        concept_id, version_id = _publish_concept(conn, fixture_project, _ARCHIVABLE_CONCEPT)
        change_set, prepared = _archive(
            conn, fixture_project, "semantic-concept", concept_id, version_id
        )

        # Nothing holds it, and the measurement says so rather than staying silent.
        assert prepared["validation"]["live_consumers"] == []
        assert prepared["validation"]["publishable"] is True

        result = confirm_change_set(
            conn,
            project_id,
            change_set.id,
            actor="owner@example.com",
            confirmation_token=prepared["confirmation_token"],
            org_id=fixture_project["org_id"],
        )
        conn.commit()

        # The version this retirement was approved against is what it returns: no
        # version was written, and inventing an id would name a row nobody has.
        assert result["result_version_id"] == version_id

        with conn.cursor() as cur:
            cur.execute(
                "SELECT lifecycle_status, current_version_id, pending_version_id "
                "FROM app.semantic_concepts WHERE id = %s",
                (concept_id,),
            )
            head = cur.fetchone()
            cur.execute(
                "SELECT status FROM app.semantic_concept_versions WHERE id = %s", (version_id,)
            )
            version_status = cur.fetchone()[0]
    # The OBJECT is retired, and it still says what it was.
    assert head[0] == "archived"
    assert head[1] == version_id
    assert head[2] is None
    # The immutable snapshot is untouched: what already pins it keeps working.
    assert version_status == "published"


@pg_available
def test_a_concept_a_live_view_still_holds_is_refused_by_name_and_stays_published(
    fixture_project,
):
    from core.db import get_connection
    from core.semantic_model import SemanticRefused, confirm_change_set

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        published = _publish_concept(conn, fixture_project, _ARCHIVABLE_CONCEPT)
        concept_id, version_id = published
        holder = _seed_holding_view(conn, project_id, "holding_view", "published", published)

        change_set, prepared = _archive(
            conn, fixture_project, "semantic-concept", concept_id, version_id
        )
        codes = [refusal["code"] for refusal in prepared["refusals"]]
        assert "live_consumers" in codes
        assert prepared["validation"]["publishable"] is False
        # The holder is NAMED, and its state travels with it.
        assert "holding_view (published)" in prepared["refusals"][0]["message"]

        with pytest.raises(SemanticRefused) as excinfo:
            confirm_change_set(
                conn,
                project_id,
                change_set.id,
                actor="owner@example.com",
                confirmation_token=prepared["confirmation_token"],
                org_id=fixture_project["org_id"],
            )
        conn.commit()
        assert excinfo.value.code == "not_publishable"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT lifecycle_status FROM app.semantic_concepts WHERE id = %s", (concept_id,)
            )
            assert cur.fetchone()[0] == "published"
        _drop_holding_view(conn, *holder)


@pg_available
def test_a_superseded_version_of_a_view_never_makes_a_concept_unretirable(
    fixture_project,
):
    """History does not hold. `governance.md` (2026-08-16) settles it: a
    superseded version never comes back, and blocking on one makes the object
    unretirable for good."""
    from core.db import get_connection
    from core.semantic_model import confirm_change_set, live_consumers

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        published = _publish_concept(conn, fixture_project, _ARCHIVABLE_CONCEPT)
        concept_id, version_id = published
        holder = _seed_holding_view(conn, project_id, "historic_view", "superseded", published)

        assert live_consumers(conn, project_id, "semantic-concept", concept_id) == []
        change_set, prepared = _archive(
            conn, fixture_project, "semantic-concept", concept_id, version_id
        )
        assert prepared["validation"]["publishable"] is True

        confirm_change_set(
            conn,
            project_id,
            change_set.id,
            actor="owner@example.com",
            confirmation_token=prepared["confirmation_token"],
            org_id=fixture_project["org_id"],
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lifecycle_status FROM app.semantic_concepts WHERE id = %s", (concept_id,)
            )
            assert cur.fetchone()[0] == "archived"
        _drop_holding_view(conn, *holder)


@pg_available
def test_retiring_an_object_twice_says_so_rather_than_reporting_a_second_retirement(
    fixture_project,
):
    from core.db import get_connection
    from core.semantic_model import SemanticStale, confirm_change_set

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        concept_id, version_id = _publish_concept(conn, fixture_project, _ARCHIVABLE_CONCEPT)
        first, prepared_first = _archive(
            conn, fixture_project, "semantic-concept", concept_id, version_id
        )
        # A SECOND change set opened against the same base BEFORE the first is
        # confirmed: both are legal to open, and only one may retire.
        second, prepared_second = _archive(
            conn, fixture_project, "semantic-concept", concept_id, version_id
        )
        confirm_change_set(
            conn,
            project_id,
            first.id,
            actor="owner@example.com",
            confirmation_token=prepared_first["confirmation_token"],
            org_id=fixture_project["org_id"],
        )
        conn.commit()
        with pytest.raises(SemanticStale) as excinfo:
            confirm_change_set(
                conn,
                project_id,
                second.id,
                actor="owner@example.com",
                confirmation_token=prepared_second["confirmation_token"],
                org_id=fixture_project["org_id"],
            )
        conn.commit()
        assert excinfo.value.code == "already_archived"


@pg_available
def test_a_platform_concept_is_never_retired_from_one_project(fixture_project):
    """`_assert_base_belongs` deliberately lets a Project edit a platform Concept
    (the scope rule of migration 142). Retiring is not editing: it would remove
    the object from every other Project from a surface that speaks for one."""
    from core.db import get_connection
    from core.semantic_model import create_change_set, prepare_change_set
    from ulid import ULID

    project_id = fixture_project["project_id"]
    concept_id, version_id = f"sc_{ULID()}", f"scv_{ULID()}"
    zero_hash = "0" * 64
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.semantic_concepts (id, project_id, kind, name, "
                "lifecycle_status, current_version_id, created_by) "
                "VALUES (%s, NULL, 'dimension', %s, 'published', NULL, 'platform@example.com')",
                (concept_id, f"platform_axis_{concept_id[-6:].lower()}"),
            )
            cur.execute(
                "INSERT INTO app.semantic_concept_versions (id, concept_id, project_id, "
                "version_number, status, kind, name, label, value_type, semantic_type, "
                "content_hash, created_by) VALUES (%s, %s, NULL, 1, 'published', 'dimension', "
                "%s, 'Platform axis', 'string', 'channel', %s, 'platform@example.com')",
                (version_id, concept_id, f"platform_axis_{concept_id[-6:].lower()}", zero_hash),
            )
            cur.execute(
                "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
                (version_id, concept_id),
            )
        conn.commit()
        try:
            change_set = create_change_set(
                conn,
                project_id,
                actor="owner@example.com",
                object_type="semantic-concept",
                object_id=concept_id,
                base_version_id=version_id,
                intent={"action": "archive_object"},
                idempotency_key=_uid("idem"),
            )
            conn.commit()
            prepared = prepare_change_set(
                conn,
                project_id,
                change_set.id,
                actor="owner@example.com",
                allow_test_override={"reason": _ARCHIVE_REASON},
            )
            conn.commit()
            codes = [refusal["code"] for refusal in prepared["refusals"]]
            assert "platform_scope_archive_refused" in codes
            assert prepared["validation"]["publishable"] is False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lifecycle_status FROM app.semantic_concepts WHERE id = %s",
                    (concept_id,),
                )
                assert cur.fetchone()[0] == "published"
        finally:
            # A platform row belongs to no org, so the org eraser cannot reach it.
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.semantic_concepts WHERE id = %s", (concept_id,))
            conn.commit()


# ---------------------------------------------------------------------------
# Story 27.8 -- the identity a validator judges on cannot drift.
#
# The compiled matrix publishes `dimensions[].name` so the language-family guard
# can ask WHAT a dimension is rather than what it is called today. That name came
# from `semantic_concept_versions.name` -- free text, no check, written from the
# edit payload on every version -- while `semantic_concepts.name` carries the
# machine-name pattern and its scope's uniqueness index and is written ONCE, at
# creation, and never again. So an edit could give version N+1 a different name,
# the two halves would disagree for good, and a dimension could walk into or out
# of an incommensurable family without anyone deciding it.
# ---------------------------------------------------------------------------


@pg_available
def test_an_edit_may_change_the_label_but_never_the_name(fixture_project):
    from core.db import get_connection
    from core.semantic_model import create_change_set, prepare_change_set

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        concept_id, version_id = _publish_concept(conn, fixture_project, _ARCHIVABLE_CONCEPT)

        renamed = dict(_ARCHIVABLE_CONCEPT, name="something_else", label="Retirable axis")
        change_set = create_change_set(
            conn,
            project_id,
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=concept_id,
            base_version_id=version_id,
            intent={"action": "edit_concept", "concept": renamed},
            idempotency_key=_uid("idem"),
        )
        conn.commit()
        _record_test_gate(conn, fixture_project["org_id"], project_id, change_set.id)
        prepared = prepare_change_set(conn, project_id, change_set.id, actor="owner@example.com")
        conn.commit()
        codes = [refusal["code"] for refusal in prepared["refusals"]]
        assert "name_is_not_editable" in codes
        assert prepared["validation"]["publishable"] is False
        # The refusal names the gesture that repairs, not the internal cause.
        message = next(
            r["message"] for r in prepared["refusals"] if r["code"] == "name_is_not_editable"
        )
        assert "label" in message

        # The SAME edit keeping the name is publishable: the guard owns renaming,
        # not editing.
        relabelled = dict(_ARCHIVABLE_CONCEPT, label="A different thing to read")
        ok_set = create_change_set(
            conn,
            project_id,
            actor="owner@example.com",
            object_type="semantic-concept",
            object_id=concept_id,
            base_version_id=version_id,
            intent={"action": "edit_concept", "concept": relabelled},
            idempotency_key=_uid("idem"),
        )
        conn.commit()
        _record_test_gate(conn, fixture_project["org_id"], project_id, ok_set.id)
        prepared_ok = prepare_change_set(conn, project_id, ok_set.id, actor="owner@example.com")
        conn.commit()
        assert prepared_ok["validation"]["publishable"] is True, prepared_ok["refusals"]


@pg_available
def test_a_version_whose_name_left_its_concept_never_reaches_the_matrix(fixture_project):
    """The compiler reads the CONCEPT HEAD name, and refuses a version that drifted.

    Rows that already drifted before the guard above existed cannot be repaired by
    it -- a version is immutable. They must not compile silently under a name the
    Concept does not carry.
    """
    from core.db import get_connection
    from core.semantic_model import _members_for_compilation

    project_id = fixture_project["project_id"]
    with get_connection() as conn:
        concept_id, version_id = _publish_concept(conn, fixture_project, _ARCHIVABLE_CONCEPT)

        members, refusals = _members_for_compilation(
            conn, project_id, [{"concept_id": concept_id, "concept_version_id": version_id}]
        )
        assert not refusals
        assert [m.name for m in members] == [_ARCHIVABLE_CONCEPT["name"]]

        # Now force the drift the way history could have produced it. A published
        # version is immutable (`app.reject_semantic_version_mutation`), so the only
        # way in is version N+1 carrying a different name -- which is precisely what
        # a renaming edit used to write.
        from ulid import ULID

        drifted_version = f"scv_{ULID()}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.semantic_concept_versions (id, concept_id, project_id, "
                "version_number, status, kind, name, label, value_type, semantic_type, "
                "content_hash, created_by) VALUES (%s, %s, %s, 2, 'published', 'dimension', "
                "%s, 'Retirable axis', 'string', 'channel', %s, 'owner@example.com')",
                (drifted_version, concept_id, project_id, "audience_language", "0" * 64),
            )
        conn.commit()
        drifted, drift_refusals = _members_for_compilation(
            conn, project_id, [{"concept_id": concept_id, "concept_version_id": drifted_version}]
        )
        assert drifted == []
        assert [r.code for r in drift_refusals] == ["concept_identity_drift"]
        # Both names are stated: a person cannot correct what the refusal does not show.
        assert "audience_language" in drift_refusals[0].message
        assert _ARCHIVABLE_CONCEPT["name"] in drift_refusals[0].message
