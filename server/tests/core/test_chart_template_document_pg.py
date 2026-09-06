"""Story 72.2 -- migration 334, proved against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. The grammar's outer shape is a CHECK, and a CHECK
is only a promise until a database refuses a row. What is proved here is that a
psql session bypassing `validate_template_document` meets the same three
statements the walker makes: the contract has ONE name, a template DECLARES its
predicates and its question, and it still names no evidence a Result carries.

WHAT IS PROVED OFFLINE. The grammar itself, every named refusal and the hash
round trip live in `tests/core/test_visualization_templates.py`; the derivation
guard in `tests/conformance/test_template_grammar_has_one_authority.py`.

THE DIVISION WITH 333. Migration 333 owns the tables, the immutability triggers,
the seed pair and `..._is_unbound`; none of that is re-proved here, it is proved
in `tests/core/test_visualization_templates_pg.py`. This file only measures what
334 added.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import json

import psycopg
import pytest
from core.query_specs import canonical_hash
from core.visualization_specs import VISUALIZATION_SPEC_CONTRACT_VERSION
from core.visualization_templates import (
    CHART_TEMPLATE_CONTRACT_VERSION,
    MAX_TEMPLATE_BYTES,
    validate_template_document,
)
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _valid_document(**overrides) -> dict:
    """A document the SERVICE accepts, so the database is asked the same question.

    Built through `validate_template_document` on purpose: a hand-typed fixture
    would let this file and the walker drift, and then a green CHECK would prove
    nothing about the rows the product actually writes.
    """
    document = validate_template_document(
        {
            "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
            "schema_version": 1,
            "family": "bar",
            "answers_question": "How does one measure compare across a few categories?",
            "requires": {
                "measure": {"min": 1, "max": 1},
                "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
            },
        }
    ).document
    document.update(overrides)
    return document


class Chain:
    """org -> project -> template head, seeded here, so this file runs on an empty base."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.project_id = _uid("proj")
        self.template_id = _uid("vtpl")

    def build(self) -> "Chain":
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 72.2 fixture', %s, 'active', 'test')",
                (self.org_id, self.org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 72.2 fixture', %s, 'test')",
                (self.project_id, self.org_id, self.project_id.replace("_", "-")),
            )
            cur.execute(
                """
                INSERT INTO app.visualization_templates
                    (id, org_id, project_id, label, seed_origin, created_by)
                VALUES (%s, %s, %s, 'Measure by category', 'project', 'test')
                """,
                (self.template_id, self.org_id, self.project_id),
            )
        return self

    def insert_version(
        self,
        document: dict,
        *,
        contract: str | None = None,
        family: str | None = None,
    ) -> str:
        version_id = _uid("vtv")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.visualization_template_versions
                    (id, template_id, org_id, project_id, version_number, family,
                     spec_contract_version, schema_version, document, content_hash, created_by)
                VALUES (%s, %s, %s, %s, 1, %s, %s, 1, %s::jsonb, %s, 'test')
                """,
                (
                    version_id,
                    self.template_id,
                    self.org_id,
                    self.project_id,
                    family or document.get("family"),
                    contract or document.get("spec_contract_version"),
                    json.dumps(document),
                    canonical_hash(document),
                ),
            )
        return version_id


@pytest.fixture
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


def _refused(chain, document, **kwargs) -> str:
    """Insert, expect a CHECK to refuse, return the constraint name."""
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        chain.insert_version(document, **kwargs)
    chain.conn.rollback()
    return excinfo.value.diag.constraint_name or ""


# ---------------------------------------------------------------------------
# The document the service produces is a row the database accepts.
# ---------------------------------------------------------------------------


def test_pg_the_validated_document_inserts(chain):
    version_id = chain.insert_version(_valid_document())
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT spec_contract_version, document -> 'requires' "
            "FROM app.visualization_template_versions WHERE id = %s",
            (version_id,),
        )
        contract, requires = cur.fetchone()
    assert contract == CHART_TEMPLATE_CONTRACT_VERSION
    assert sorted(requires) == ["dimension", "measure"]


# ---------------------------------------------------------------------------
# 334.1 -- the contract has one name.
# ---------------------------------------------------------------------------


def test_pg_a_document_under_the_visualization_spec_contract_is_refused(chain):
    """The line that keeps the two objects apart, in the layer that cannot be bypassed."""
    document = _valid_document(spec_contract_version=VISUALIZATION_SPEC_CONTRACT_VERSION)
    assert (
        _refused(chain, document, contract=VISUALIZATION_SPEC_CONTRACT_VERSION)
        == "ck_visualization_template_versions_contract_pinned"
    )


def test_pg_an_invented_contract_name_is_refused(chain):
    document = _valid_document(spec_contract_version="chart-template.v2")
    assert (
        _refused(chain, document, contract="chart-template.v2")
        == "ck_visualization_template_versions_contract_pinned"
    )


# ---------------------------------------------------------------------------
# 334.2 -- what a template IS.
# ---------------------------------------------------------------------------


def test_pg_a_document_with_no_requires_is_refused(chain):
    document = _valid_document()
    document.pop("requires")
    assert (
        _refused(chain, document)
        == "ck_visualization_template_versions_declares_requires"
    )


def test_pg_requires_that_is_not_an_object_is_refused(chain):
    assert (
        _refused(chain, _valid_document(requires=["measure"]))
        == "ck_visualization_template_versions_declares_requires"
    )


def test_pg_a_document_with_no_question_is_refused(chain):
    document = _valid_document()
    document.pop("answers_question")
    assert (
        _refused(chain, document)
        == "ck_visualization_template_versions_declares_requires"
    )


# ---------------------------------------------------------------------------
# 334.3 -- what a template still is not.
# ---------------------------------------------------------------------------


def test_pg_a_document_that_names_evidence_is_refused(chain):
    """An annotation names evidence ONE Result carries, so it is a data value."""
    document = _valid_document(annotations=[{"evidence_id": "ev_one", "anchor": "datum"}])
    assert (
        _refused(chain, document)
        == "ck_visualization_template_versions_names_no_evidence"
    )


def test_pg_333_still_refuses_a_binding_a_query_pin_and_a_result(chain):
    """Not re-proving 333 -- proving 334 did not weaken it."""
    for key, value in (
        ("bindings", {"measure": ["revenue"]}),
        ("query_spec_version_id", "qsv_01EXAMPLE"),
        ("result_id", "res_01EXAMPLE"),
    ):
        assert (
            _refused(chain, _valid_document(**{key: value}))
            == "ck_visualization_template_versions_is_unbound"
        )


# ---------------------------------------------------------------------------
# AC8 -- one ceiling, and the database is the layer that holds.
# ---------------------------------------------------------------------------


def test_pg_the_size_ceiling_refuses_a_document_the_walker_never_wrote(chain):
    """`MAX_TEMPLATE_BYTES` and `pg_column_size` are one number.

    The document below could not come out of `validate_template_document` -- the
    grammar's own bounds refuse it long before the ceiling -- which is exactly
    why the CHECK matters: it is what stops a direct SQL insert.
    """
    document = _valid_document()
    document["answers_question"] = "x" * (MAX_TEMPLATE_BYTES + 4096)
    assert _refused(chain, document) == "ck_visualization_template_versions_size"
