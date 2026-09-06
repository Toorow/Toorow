"""AI-357 -- the constraint-to-refusal map, checked without a database.

The pg suite proves the map COVERS the catalogue. This file proves each entry is
worth having: a refusal that carries no gesture is the mute 500 with a nicer
status code, and a message that quotes the constraint names the cause rather than
the move.
"""

from __future__ import annotations

import pytest
from core.analyze_check_refusals import (
    ANALYZE_DOMAIN_TABLES,
    CHECK_TRANSLATIONS,
    constraint_name_of,
    translate,
)


def test_every_translation_carries_a_code_a_subject_and_a_gesture():
    for name, entry in CHECK_TRANSLATIONS.items():
        assert entry.code and " " not in entry.code, name
        assert entry.message and len(entry.message) > 20, name
        assert entry.subject, name
        assert entry.remedy, f"{name} states a fact and no gesture"


def test_no_message_or_remedy_speaks_the_database():
    """A refusal names the gesture, never the cause (`CLAUDE.md`, `L'ecran`).

    The words below are how a constraint, a table or a driver would leak into a
    sentence a person reads.
    """
    forbidden = ("ck_", "CHECK", "constraint", "psycopg", "postgres", "app.", "SQLSTATE")
    for name, entry in CHECK_TRANSLATIONS.items():
        for word in forbidden:
            assert word not in entry.message, f"{name}: message says `{word}`"
            assert word not in entry.remedy, f"{name}: remedy says `{word}`"


def test_the_map_covers_only_the_tables_the_module_declares():
    """Every key is named `ck_<something>` of a declared table, and none is orphaned.

    A stale key would be invisible otherwise: the pg enumeration only reports
    constraints with NO translation, never translations with no constraint.
    """
    stems = {table.rstrip("s") for table in ANALYZE_DOMAIN_TABLES}
    stems |= set(ANALYZE_DOMAIN_TABLES)
    #  `analysis_notebook_version_blocks` is constrained under the shorter prefix
    #  migration 154 chose (`ck_analysis_notebook_blocks_*`); the map must not
    #  invent a table, so the prefix is asserted rather than the full name.
    stems.add("analysis_notebook_blocks")
    for name in CHECK_TRANSLATIONS:
        assert name.startswith("ck_"), name
        assert any(name.startswith(f"ck_{stem}_") for stem in stems), name


def test_an_unknown_constraint_translates_to_nothing_rather_than_a_guess():
    assert translate("ck_something_a_migration_added_yesterday") is None
    assert translate(None) is None
    assert translate("") is None


@pytest.mark.parametrize(
    "exc",
    [ValueError("no diag"), type("E", (Exception,), {"diag": None})()],
)
def test_an_error_that_names_no_constraint_answers_none(exc):
    assert constraint_name_of(exc) is None


def test_the_service_only_translates_a_check_violation():
    """A unique violation, a foreign key or a timeout must NOT become a pin refusal.

    Telling a person to repair their request when the database refused for another
    reason is a worse answer than the 500 -- it is a wrong one.
    """
    from core.analyze_artifacts import _as_named_refusal

    class _Fake(Exception):
        sqlstate = "23505"  # unique_violation
        diag = type("D", (), {"constraint_name": "uq_renders_scope"})()

    assert _as_named_refusal(_Fake()) is None
    assert _as_named_refusal(ValueError("not a database error at all")) is None


def test_a_check_with_no_translation_is_still_named_and_still_carries_a_gesture():
    """The safety net under the enumeration, and it is never a traceback."""
    from core.analyze_artifacts import ArtifactRefused, _as_named_refusal

    class _Fake(Exception):
        sqlstate = "23514"
        diag = type("D", (), {"constraint_name": "ck_a_rule_nobody_translated"})()

    refused = _as_named_refusal(_Fake())
    assert isinstance(refused, ArtifactRefused)
    assert refused.code == "refused_by_an_unnamed_rule"
    (refusal,) = refused.refusals
    assert refusal.remedy
    #  The constraint name is a fact about the database: it belongs in the log.
    assert "ck_a_rule_nobody_translated" not in refusal.message


def test_a_translated_check_becomes_its_named_refusal():
    from core.analyze_artifacts import _as_named_refusal

    class _Fake(Exception):
        sqlstate = "23514"
        diag = type("D", (), {"constraint_name": "ck_renders_pins_are_exact"})()

    refused = _as_named_refusal(_Fake())
    assert refused is not None
    assert refused.code == "placeholder_pin"
    (refusal,) = refused.refusals
    assert refusal.subject == "renderer_build_id"
    assert refusal.remedy
