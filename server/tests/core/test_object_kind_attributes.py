"""A client property is typed, or it is not a property (Story 64.2).

Proven without a database, the discipline `test_master_data.py` states: the rules
are generic. What a database DOES prove -- that the projection reads the contract
and flags what contradicts it -- was measured on a disposable Postgres and
recorded in `story-log.md`; these are the refusals that must hold everywhere.
"""

from __future__ import annotations

import pytest
from core.master_data import MasterDataError
from core.object_kind_attributes import validate_node_attributes

# Amende le 2026-08-08 (64.14) : le contrat n est plus une liste JSONB authoree
# a part -- ce sont les CHAMPS CANONIQUES qui nomment l objet
# (`mdm_canonical_fields.object_kind`). La forme lue ici est donc celle des
# lignes de cette table, pas celle d un magasin propre.
CONTRACT = [
    {"name": "duration_s", "value_type": "integer", "unit": "second"},
    {"name": "category", "value_type": "string"},
    {"name": "born_at", "value_type": "date"},
    {"name": "is_short", "value_type": "boolean"},
]


# ---------------------------------------------------------------------------
# The node against its contract
# ---------------------------------------------------------------------------


def test_a_conforming_payload_passes_through_unchanged():
    contract = CONTRACT
    values = {"duration_s": 1269, "category": "Howto & Style", "born_at": "2026-08-07"}
    assert validate_node_attributes(values, contract) == values


def test_an_undeclared_attribute_is_refused_or_the_contract_is_advisory():
    """Accepting it would put a column in the warehouse no Concept can describe."""
    contract = CONTRACT
    with pytest.raises(MasterDataError) as excinfo:
        validate_node_attributes({"likes": 42}, contract)
    assert "not declared" in str(excinfo.value) and "likes" in str(excinfo.value)


def test_a_value_the_declared_type_cannot_hold_is_refused_never_coerced():
    """Coercing here would make Postgres and the warehouse disagree about one row.

    The view reports the same disagreement as `type_mismatch` for rows written
    before a contract tightened; a silent parse here would leave a number that is
    a string on one side and a number on the other.
    """
    contract = CONTRACT
    with pytest.raises(MasterDataError) as excinfo:
        validate_node_attributes({"duration_s": "vingt-et-une minutes"}, contract)
    assert "expected integer" in str(excinfo.value)


def test_a_boolean_is_not_accepted_as_a_number():
    """`bool` is an `int` in Python and sails through a naive numeric check.

    Without the explicit exclusion, `is_short: true` would satisfy an `integer`
    attribute and land in `value_number` as 1 -- a duration of one second.
    """
    contract = CONTRACT
    with pytest.raises(MasterDataError):
        validate_node_attributes({"duration_s": True}, contract)


def test_an_absent_attribute_is_absent_and_not_wrong():
    """A node that carries only some of its declared properties is legitimate."""
    contract = CONTRACT
    assert validate_node_attributes({"category": "Vlog"}, contract) == {"category": "Vlog"}
    assert validate_node_attributes(None, contract) == {}


def test_an_explicit_null_is_allowed_and_does_not_break_its_type():
    """`{"duration_s": null}` says "known to be unset", which is not a type error."""
    contract = CONTRACT
    assert validate_node_attributes({"duration_s": None}, contract) == {"duration_s": None}


# ---------------------------------------------------------------------------
# The projection is registered where it can actually be read.
# ---------------------------------------------------------------------------


def test_the_attribute_projection_is_registered_in_all_four_mirror_registries():
    """Missing from any one of the four and the properties never reach dbt."""
    from core import mirror_sync

    name = "master_data_node_attributes_dim"
    assert name in mirror_sync._DEFAULT_TABLES
    assert name in mirror_sync._ALLOWED_TABLES
    assert name in mirror_sync._CURATED_SQL
    assert name in mirror_sync._GUARDED_RELATIONS
    assert mirror_sync._CURATED_SQL[name] == (
        f"SELECT * FROM {mirror_sync._GUARDED_RELATIONS[name]}"
    )


def _live_view_sql() -> str:
    """The CURRENT definition, not the first one.

    Migration 239 created this view and 241 replaced it. A test that kept reading
    239 would assert a superseded artifact and stay green while the live view
    drifted -- the exact failure mode of pinning evidence to a version instead of
    to the truth.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sql = (
        root / "infra" / "nango" / "migrations"
        / "241_a_field_says_which_object_it_qualifies.sql"
    ).read_text(encoding="utf-8")
    body = sql[sql.index("CREATE OR REPLACE VIEW app.master_data_node_attributes_dim_v"):]
    # Borne a la vue elle-meme. Sans cela la tranche va jusqu a la fin du fichier
    # et attrape le `DROP COLUMN attribute_contract` -- l assertion « la vue ne lit
    # plus le contrat » passerait alors sur le texte qui le RETIRE.
    # Le `;` qui clot le CREATE VIEW. Sans cette borne la tranche va jusqu'a la
    # fin du fichier et attrape le `DROP COLUMN attribute_contract` -- l'assertion
    # « la vue ne lit plus le contrat » passerait alors sur le texte qui le RETIRE.
    end = body.index("d.attribute   = c.attribute;") + len("d.attribute   = c.attribute;")
    return body[:end]


def test_the_view_never_guesses_a_type():
    """A cast must be gated by the DECLARED type, never by the JSON type alone.

    A view that cast on the JSON type would turn a mistyped value into a
    plausible number, and a plausible wrong number is the one nobody questions.
    """
    body = _live_view_sql()
    assert "d.value_type IN ('integer','decimal','money','ratio','percent','duration')" in body
    assert "type_mismatch" in body and "undeclared" in body


def test_the_view_reads_the_declarations_and_not_a_second_list():
    """Story 64.14 -- l attribut d un objet EST le champ canonique qui le nomme.

    Tant que la vue lisait `attribute_contract`, le meme fait vivait dans deux
    magasins : le champ canonique sans `value_type`, le contrat sans genre ni
    agregation, et aucun des deux publiable en Concept. La jointure ci-dessous est
    ce qui les a fondus.
    """
    body = _live_view_sql()
    assert "app.mdm_canonical_fields f" in body
    assert "f.object_kind = r.object_kind" in body
    assert "attribute_contract" not in body
