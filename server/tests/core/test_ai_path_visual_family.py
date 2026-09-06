"""Story 55.1 AC1 / AC9 -- `ai_path` is a DECLARED family, and it stays honest.

Four properties, and each one closes a way this change could have been green and
wrong:

  1. the id is in exactly one of the two lists. A family that is both declared
     and deferred would let two readers of the same registry disagree;
  2. the family satisfies the class-level invariants every other family
     satisfies -- the parametrized tests in `test_visualization_compatibility.py`
     already sweep `VISUAL_FAMILIES`, so declaring the family put it inside them
     by construction. What is asserted HERE is the part those tests cannot know:
     the fallback columns name real wells AND the drawing renders them;
  3. declaring it did NOT make it pickable for a Query Result. This is the whole
     honesty of the move: the rung is a `classification`, a role with no server
     source, so validation refuses by name rather than offering "AI Path" as a
     chart type for any two columns;
  4. the database CHECK is not widened. `SPEC_SELECTABLE_FAMILY_IDS` is read
     against the literal in migration 156, from the SQL file, so a later session
     that adds a family to one and not the other gets a red test instead of a
     surprise on an insert.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.visualization_families import (
    DEFERRED_FAMILIES,
    FAMILY_IDS,
    SPEC_SELECTABLE_FAMILY_IDS,
    VISUAL_FAMILIES,
    get_family,
    registry_payload,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_156 = _REPO_ROOT / "infra" / "nango" / "migrations" / "156_visualization_specs.sql"
_MIGRATION_191 = _REPO_ROOT / "infra" / "nango" / "migrations" / "191_waterfall_visual_family.sql"

AI_PATH = "ai_path"


# ---------------------------------------------------------------------------
# 1. Declared, and declared once.
# ---------------------------------------------------------------------------


def test_ai_path_is_declared_and_no_longer_deferred():
    assert AI_PATH in FAMILY_IDS
    assert AI_PATH not in {entry["id"] for entry in DEFERRED_FAMILIES}


def test_no_id_is_both_declared_and_deferred():
    """The class, not the instance: the rule holds for every id, not just mine."""
    deferred = {entry["id"] for entry in DEFERRED_FAMILIES}
    assert deferred.isdisjoint(set(FAMILY_IDS))


def test_the_deferred_list_still_names_every_family_the_target_names():
    """Removing an id from BOTH lists would erase the inventory of what is left."""
    named_by_the_target = {
        "distribution",
        "heatmap",
        "calendar_heatmap",
        "gauge",
        "waterfall",
        "small_multiples",
        "timeline",
        "sankey",
        "geographic_map",
        "knowledge_graph",
        "mindmap",
        AI_PATH,
    }
    accounted = {entry["id"] for entry in DEFERRED_FAMILIES} | set(FAMILY_IDS)
    assert named_by_the_target <= accounted


# ---------------------------------------------------------------------------
# 2. The declaration is complete, and the fallback columns are real wells.
# ---------------------------------------------------------------------------


def test_the_family_declares_non_empty_fallback_columns_over_wells_it_has():
    family = get_family(AI_PATH)
    assert family is not None
    assert family.table_fallback_wells, "a path that cannot be read as a table is unreadable"
    declared = {well.name for well in family.wells}
    for well in family.table_fallback_wells:
        assert well in declared, well


def test_the_rung_cardinality_matches_the_recorder_grid():
    """The grid has ONE definition site; this registry only restates its size."""
    from core.ai_path_recorder import LEVELS

    family = get_family(AI_PATH)
    assert family is not None
    rung = family.well("dimension")
    assert rung is not None
    assert rung.max_cardinality == len(LEVELS)


def test_the_outcome_cardinality_matches_the_path_vocabulary():
    from core.ai_paths import OUTCOMES

    family = get_family(AI_PATH)
    assert family is not None
    colour = family.well("color")
    assert colour is not None
    assert colour.max_cardinality == len(OUTCOMES)


def test_the_registry_payload_carries_the_family_and_its_unavailable_reason():
    payload = registry_payload()
    families = {entry["id"]: entry for entry in payload["families"]}  # type: ignore[union-attr]
    assert AI_PATH in families
    entry = families[AI_PATH]
    assert entry["table_fallback"] == "required"
    rung = next(w for w in entry["wells"] if w["name"] == "dimension")  # type: ignore[union-attr]
    # The absence is visible IN THE PRODUCT, not only in a docstring.
    assert rung["available"] is False
    assert rung["unavailable_reason"]
    assert rung["unavailable_owner"]


# ---------------------------------------------------------------------------
# 3. Declared is not pickable.
# ---------------------------------------------------------------------------


def _refusal_codes(bindings: dict[str, list[str]]) -> set[str]:
    from core.visualization_specs import (
        VISUALIZATION_SPEC_CONTRACT_VERSION,
        VISUALIZATION_SPEC_SCHEMA_VERSION,
        PinnedMembers,
        check_shape_compatibility,
        normalize_document,
    )

    normalized, refusals = normalize_document(
        {
            "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
            "schema_version": VISUALIZATION_SPEC_SCHEMA_VERSION,
            "family": AI_PATH,
            "bindings": bindings,
        }
    )
    assert refusals == [], [r.as_dict() for r in refusals]
    pinned = PinnedMembers(
        roles={"cost": "measure", "channel": "dimension"},
        labels={"cost": "cost", "channel": "channel"},
        grain="day",
        comparison="none",
        row_limit=1000,
        query_spec_id="qs_example",
        semantic_view_id="sv_example",
        semantic_view_version_id="svv_example",
    )
    family = get_family(AI_PATH)
    assert family is not None
    return {r.as_dict()["code"] for r in check_shape_compatibility(normalized, family, pinned)}


def test_an_empty_ai_path_document_is_refused_because_the_rung_has_no_source():
    assert "missing_binding" in _refusal_codes({})


def test_binding_a_dimension_into_the_rung_is_refused_as_role_unavailable():
    """The failure the declaration exists to make impossible: "AI Path" offered
    as a chart type for any two columns."""
    assert "role_unavailable" in _refusal_codes({"dimension": ["channel"]})


def test_the_family_is_absent_from_the_spec_selectable_tuple():
    assert AI_PATH not in SPEC_SELECTABLE_FAMILY_IDS
    assert set(SPEC_SELECTABLE_FAMILY_IDS) < set(FAMILY_IDS)


# ---------------------------------------------------------------------------
# 4. The database CHECK is not widened by accident.
# ---------------------------------------------------------------------------


def test_the_migration_check_mirrors_the_spec_selectable_families_exactly():
    sql = _MIGRATION_191.read_text(encoding="utf-8")
    match = re.search(
        r"ck_visualization_spec_versions_family CHECK \(\s*family IN \(([^)]*)\)", sql
    )
    assert match is not None, "the family CHECK moved; this test must follow it"
    in_check = tuple(m.group(1) for m in re.finditer(r"'([^']+)'", match.group(1)))
    assert in_check == SPEC_SELECTABLE_FAMILY_IDS
    assert AI_PATH not in in_check


def test_every_family_the_check_allows_is_actually_declared():
    for family_id in SPEC_SELECTABLE_FAMILY_IDS:
        assert get_family(family_id) is not None


def test_the_declaration_added_no_migration_of_its_own():
    """Story 55.1 is a registry + runtime change. A migration here would mean the
    family became persistable, which is exactly what it is not.

    Later migrations may legitimately persist AI-path *evidence* for another
    aggregate (for example a frozen Share).  The forbidden coupling is narrower:
    none of those migrations may widen the persisted visualization-family check.
    """
    migrations = sorted(
        (_REPO_ROOT / "infra" / "nango" / "migrations").glob("*.sql")
    )
    for migration in migrations:
        sql = "\n".join(
            line
            for line in migration.read_text(encoding="utf-8").lower().splitlines()
            if not line.lstrip().startswith("--")
        )
        if "ck_visualization_spec_versions_family" in sql:
            assert "'ai_path'" not in sql, migration.name


def test_the_declared_families_are_a_superset_of_what_the_runtime_draws():
    """Every family id is a lowercase identifier -- the client mirrors it verbatim."""
    for family in VISUAL_FAMILIES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", family.id), family.id
