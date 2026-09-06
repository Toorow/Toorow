"""The governed field catalogue answers from the SUCCESSOR first (story 49.3).

WHY THIS FILE IS PG-GATED AND NOT MOCKED. Everything this module does is SQL:
a scope clause, a `lifecycle_status` filter, an `ORDER BY project_id NULLS LAST`
that decides which of two rows with the same name wins, and a second query whose
whole job is to NOT overwrite the first. A `MagicMock` cursor accepts any string
and returns whatever the test already believed, so a mocked version of this file
would assert that I wrote the code I wrote. The precedence is proved by MUTATING
the successor and watching the reader follow.

WHAT EACH TEST WOULD CATCH. Every one of them fails on a specific, nameable
reversal:

  * `test_a_published_concept_answers_before_the_dictionary` fails if the two
    layers are merged in the other order -- the dictionary would win and the
    label a person published would never reach a reader;
  * `test_the_projects_own_concept_wins_over_the_platform_one` fails if
    `NULLS LAST` becomes `NULLS FIRST`, or if `setdefault` becomes an assignment
    -- either edit is one word long and silently serves every project the
    platform definition instead of its own;
  * `test_a_name_only_the_dictionary_carries_still_resolves` fails if the
    fallback is dropped -- the whole risk of this change, because it would
    RETIRE names rather than add them;
  * `test_a_non_additive_concept_answers_no_measure` fails if the aggregation is
    copied blind, which hands a permission to sum to a metric that refuses it;
  * `test_a_binding_is_not_checked_against_a_concept` fails if
    `binding_vocabulary` reuses `resolve`, which would tell a person their column
    is bound to an object that binds no column.

Rolled back by the `live_postgres` fixture: nothing this file writes survives it.
"""

from __future__ import annotations

import pytest
from core.governed_field_catalogue import binding_vocabulary, governed_names, resolve
from ulid import ULID

pytestmark = pytest.mark.live_postgres

#: A project that exists in every migrated database (migration 001 seeds it).
_PROJECT = "default"

_METRIC_EXPRESSION = '{"type": "source_measure", "ref": "qa_probe"}'


def _seed_concept(
    conn,
    *,
    name: str,
    project_id: str | None,
    kind: str = "metric",
    label: str = "Seeded",
    value_type: str = "integer",
    definition: str | None = None,
    aggregation: str | None = '{"function": "sum"}',
    additivity_class: str | None = "additive",
) -> str:
    """Publish one Concept, the way a confirmed change set leaves the tables."""
    concept_id = f"sc_{ULID()}"
    version_id = f"scv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_concepts
                (id, project_id, kind, name, lifecycle_status, created_by)
            VALUES (%s, %s, %s, %s, 'published', 'qa-harness')
            """,
            (concept_id, project_id, kind, name),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name,
                 label, definition, value_type, expression, aggregation,
                 additivity_class, semantic_type, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s, 'qa-harness')
            """,
            (
                version_id,
                concept_id,
                project_id,
                kind,
                name,
                label,
                definition,
                value_type,
                _METRIC_EXPRESSION if kind == "metric" else None,
                aggregation if kind == "metric" else None,
                additivity_class if kind == "metric" else None,
                "text" if kind == "dimension" else None,
                "0" * 64,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (version_id, concept_id),
        )
    return concept_id


def _unpublish(conn, name: str, project_id: str | None = None) -> None:
    """Retire a Concept WITHOUT touching the dictionary row of the same name."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.semantic_concepts SET lifecycle_status = 'archived'
             WHERE name = %s
               AND (project_id IS NOT DISTINCT FROM %s)
            """,
            (name, project_id),
        )


def _dictionary_row(conn, name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT display_name, data_type, measure FROM app.target_fields "
            "WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None, f"{name} is expected in the migration 023 seed"
    return {"display_name": row[0], "data_type": row[1], "measure": row[2]}


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


def test_a_published_concept_answers_before_the_dictionary(live_postgres) -> None:
    """MUTATE the successor; the reader must follow it, not `app.target_fields`."""
    dictionary = _dictionary_row(live_postgres, "clicks")
    _seed_concept(
        live_postgres,
        name="clicks",
        project_id=_PROJECT,
        label="Paid clicks",
        value_type="ratio",
        definition="Clicks a person paid for.",
    )

    field = resolve(live_postgres, names=["clicks"], project_id=_PROJECT)["clicks"]

    assert field["display_name"] == "Paid clicks" != dictionary["display_name"]
    assert field["data_type"] == "ratio" != dictionary["data_type"]
    assert field["description"] == "Clicks a person paid for."
    # A published version IS the successor's approval, spelled in the token the
    # callers of this catalogue branch on.
    assert field["status"] == "approved"


def test_the_projects_own_concept_wins_over_the_platform_one(live_postgres) -> None:
    _seed_concept(
        live_postgres,
        name="clicks",
        project_id=_PROJECT,
        label="Paid clicks",
        value_type="integer",
    )

    scoped = resolve(live_postgres, names=["clicks"], project_id=_PROJECT)["clicks"]
    platform = resolve(live_postgres, names=["clicks"])["clicks"]

    assert scoped["display_name"] == "Paid clicks"
    # Asking WITHOUT a project never borrows a project's declaration.
    assert platform["display_name"] != "Paid clicks"


def test_another_projects_concept_is_invisible(live_postgres) -> None:
    _seed_concept(
        live_postgres,
        name="qa_probe_elsewhere",
        project_id="integ-test-project",
        label="Elsewhere",
    )
    assert "qa_probe_elsewhere" not in resolve(
        live_postgres, names=["qa_probe_elsewhere"], project_id=_PROJECT
    )


# ---------------------------------------------------------------------------
# The fallback -- the half that must never be dropped
# ---------------------------------------------------------------------------


def test_a_name_only_the_dictionary_carries_still_resolves(live_postgres) -> None:
    """The successor may ADD a name; it may never take one away."""
    _unpublish(live_postgres, "clicks")
    dictionary = _dictionary_row(live_postgres, "clicks")

    field = resolve(live_postgres, names=["clicks"], project_id=_PROJECT)["clicks"]

    assert field["display_name"] == dictionary["display_name"]
    assert field["data_type"] == dictionary["data_type"]


def test_a_name_only_the_semantic_model_carries_resolves(live_postgres) -> None:
    """The whole point: a metric declared in the workbench becomes readable."""
    assert "qa_probe_workbench_metric" not in resolve(
        live_postgres, names=["qa_probe_workbench_metric"], project_id=_PROJECT
    )
    _seed_concept(
        live_postgres,
        name="qa_probe_workbench_metric",
        project_id=_PROJECT,
        label="Workbench metric",
    )
    assert "qa_probe_workbench_metric" in resolve(
        live_postgres, names=["qa_probe_workbench_metric"], project_id=_PROJECT
    )


def test_a_name_neither_store_carries_is_absent(live_postgres) -> None:
    assert resolve(live_postgres, names=["qa_probe_nowhere"], project_id=_PROJECT) == {}


def test_the_whole_catalogue_is_the_union_of_both_stores(live_postgres) -> None:
    """`names=None` is the shape the console step will read; it is a UNION.

    Without this the branch would be reachable only from a caller nobody has
    written yet, and an unexercised branch in a reader that decides what a
    binding may claim is a defect waiting for its first user.
    """
    before = resolve(live_postgres, project_id=_PROJECT)
    assert {"clicks", "cost", "country"} <= set(before)
    _seed_concept(
        live_postgres,
        name="qa_probe_union",
        project_id=_PROJECT,
        label="Probe union",
    )
    after = resolve(live_postgres, project_id=_PROJECT)
    assert set(after) - set(before) == {"qa_probe_union"}


def test_a_draft_concept_does_not_answer(live_postgres) -> None:
    """Only a PUBLISHED version answers -- a draft is not a declaration."""
    concept_id = _seed_concept(
        live_postgres, name="qa_probe_draft", project_id=_PROJECT, label="Draft"
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.semantic_concepts SET lifecycle_status = 'draft' WHERE id = %s",
            (concept_id,),
        )
    assert "qa_probe_draft" not in resolve(
        live_postgres, names=["qa_probe_draft"], project_id=_PROJECT
    )


# ---------------------------------------------------------------------------
# The translation
# ---------------------------------------------------------------------------


def test_money_reads_as_the_dictionarys_currency(live_postgres) -> None:
    """The one word the two vocabularies differ by, per `governance.md`."""
    _seed_concept(
        live_postgres,
        name="qa_probe_money",
        project_id=_PROJECT,
        label="Probe money",
        value_type="money",
    )
    field = resolve(live_postgres, names=["qa_probe_money"], project_id=_PROJECT)
    assert field["qa_probe_money"]["data_type"] == "currency"


def test_a_non_additive_concept_answers_no_measure(live_postgres) -> None:
    """Handing back `sum` here would grant a permission the version refuses.

    The version STORES an aggregation and declares itself non-additive at the
    same time -- the migration 142 CHECK allows exactly that, and it is the shape
    `average_position` has in production (`measure = 'average'`,
    `additive = false`). Seeding a NULL aggregation instead would make this test
    pass for the wrong reason: `_measure_of` would answer None on the missing
    dict before ever reading the additivity class.
    """
    _seed_concept(
        live_postgres,
        name="qa_probe_non_additive",
        project_id=_PROJECT,
        label="Probe ratio",
        value_type="ratio",
        aggregation='{"function": "sum"}',
        additivity_class="non_additive",
    )
    field = resolve(live_postgres, names=["qa_probe_non_additive"], project_id=_PROJECT)
    assert field["qa_probe_non_additive"]["measure"] is None


def test_an_additive_concept_answers_with_its_aggregation(live_postgres) -> None:
    """The other side of the branch above -- otherwise `measure` could be dead."""
    _seed_concept(
        live_postgres,
        name="qa_probe_additive",
        project_id=_PROJECT,
        label="Probe additive",
        aggregation='{"function": "sum"}',
        additivity_class="additive",
    )
    field = resolve(live_postgres, names=["qa_probe_additive"], project_id=_PROJECT)
    assert field["qa_probe_additive"]["measure"] == "sum"


def test_a_dimension_answers_as_a_dimension(live_postgres) -> None:
    _seed_concept(
        live_postgres,
        name="qa_probe_dimension",
        project_id=_PROJECT,
        kind="dimension",
        label="Probe dimension",
        value_type="string",
    )
    field = resolve(live_postgres, names=["qa_probe_dimension"], project_id=_PROJECT)
    assert field["qa_probe_dimension"]["field_kind"] == "dimension"
    assert field["qa_probe_dimension"]["measure"] is None


# ---------------------------------------------------------------------------
# The validator's half
# ---------------------------------------------------------------------------


def test_governed_names_accepts_what_either_store_governs(live_postgres) -> None:
    _seed_concept(
        live_postgres,
        name="qa_probe_governed",
        project_id=_PROJECT,
        label="Probe governed",
    )
    known = governed_names(
        live_postgres,
        names=["qa_probe_governed", "clicks", "qa_probe_nowhere"],
        project_id=_PROJECT,
    )
    assert known == {"qa_probe_governed", "clicks"}


# ---------------------------------------------------------------------------
# The OTHER successor -- and it is not interchangeable with the first
# ---------------------------------------------------------------------------


def test_a_project_canonical_field_becomes_a_legal_binding_target(live_postgres) -> None:
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_canonical_fields
                (id, project_id, concept_kind, canonical_name, value_type,
                 aggregation, created_by)
            VALUES (%s, %s, 'metric', 'qa_probe_binding', 'integer', 'sum', 'qa-harness')
            """,
            (f"mdm_{ULID()}", _PROJECT),
        )
    assert "qa_probe_binding" in binding_vocabulary(live_postgres, project_id=_PROJECT)


def test_a_binding_is_not_checked_against_a_concept(live_postgres) -> None:
    """`glossary.md`: a binding names a canonical field id, NEVER a Concept."""
    _seed_concept(
        live_postgres,
        name="qa_probe_concept_only",
        project_id=_PROJECT,
        label="Concept only",
    )
    vocabulary = binding_vocabulary(live_postgres, project_id=_PROJECT)
    assert "qa_probe_concept_only" not in vocabulary
    # ... while the dictionary rows a binding could always claim are still there.
    assert "clicks" in vocabulary


def test_another_projects_canonical_field_is_not_a_binding_target(live_postgres) -> None:
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_canonical_fields
                (id, project_id, concept_kind, canonical_name, value_type, created_by)
            VALUES (%s, 'integ-test-project', 'dimension', 'qa_probe_foreign',
                    'string', 'qa-harness')
            """,
            (f"mdm_{ULID()}",),
        )
    assert "qa_probe_foreign" not in binding_vocabulary(
        live_postgres, project_id=_PROJECT
    )
