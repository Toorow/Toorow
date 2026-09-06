"""Story 72.2, AC7 -- the Chart Template grammar has ONE authority, and it is not this file.

THE RATIFIED CRITERION (`docs/product-architecture/visualization-and-rendering.md`,
§ *Amendment, 2026-08-31*): *"a Chart Template declares a well, a role, a visual
family or a responsive profile instead of importing it from the authority that
defines it"*.

WHY THIS IS NOT A GREP. A test that searched `visualization_templates.py` for the
word "measure" would pass the day someone wrote `WELLS = ("mesure", ...)` and fail
the day someone wrote an honest sentence in a docstring. So this file measures
two things a word count cannot:

  1. the IMPORTS -- the authority names are bound from the module that defines
     them, are never re-bound here, and no literal collection anywhere in the
     file restates one of their values;
  2. the DERIVATION, by MUTATING an authority and re-deriving. A well added to
     `WELL_ROLES` must reach `requires`; a root key added to `GRAMMAR` must cross
     over; a member anchor added with no well-anchored name must RAISE. That is
     the difference between a grammar that follows its authority and a grammar
     that merely agreed with it on the day it was written.

Nothing here touches a database. The derivation is pure, which is the property
that makes it testable by mutation at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from core import visualization_templates as templates
from core.visualization_families import (
    DEFERRED_FAMILIES,
    FAMILY_IDS,
    SEMANTIC_ROLES,
    SPEC_SELECTABLE_FAMILY_IDS,
    WELL_ROLES,
)
from core.visualization_specs import (
    GRAMMAR,
    RESPONSIVE_PROFILES,
    ArrayOf,
    Leaf,
    MapOf,
    Node,
)
from core.visualization_templates import (
    CHART_TEMPLATE_CONTRACT_VERSION,
    TEMPLATE_GRAMMAR,
    TemplateGrammarDerivationError,
    derive_template_grammar,
)

_MODULE = Path(templates.__file__)
_TREE = ast.parse(_MODULE.read_text(encoding="utf-8"))

#: What the derived grammar may never restate on its own account.
_AUTHORITY_VALUES: frozenset[str] = (
    frozenset(WELL_ROLES)
    | frozenset(SEMANTIC_ROLES)
    | frozenset(FAMILY_IDS)
    | frozenset(RESPONSIVE_PROFILES)
    | frozenset(str(entry["id"]) for entry in DEFERRED_FAMILIES)
)

#: `{name: defining module}` -- every authority this document is built out of.
_MUST_BE_IMPORTED: dict[str, str] = {
    "WELL_ROLES": "core.visualization_families",
    "WELL_LABELS": "core.visualization_families",
    "SEMANTIC_ROLES": "core.visualization_families",
    "SPEC_SELECTABLE_FAMILY_IDS": "core.visualization_families",
    "DEFERRED_FAMILIES": "core.visualization_families",
    "GRAMMAR": "core.visualization_specs",
    "RESPONSIVE_PROFILES": "core.visualization_specs",
    "walk_document": "core.visualization_specs",
}


def _imported_names() -> dict[str, str]:
    bound: dict[str, str] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = node.module
    return bound


def _assigned_names() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


# ---------------------------------------------------------------------------
# 1. The imports.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,module", sorted(_MUST_BE_IMPORTED.items()))
def test_every_authority_is_imported_from_the_module_that_defines_it(name, module):
    imported = _imported_names()
    assert name in imported, (
        f"`{name}` is not imported by {_MODULE.name}. A Chart Template must not "
        f"declare a well, a role, a family or a responsive profile."
    )
    assert imported[name] == module


@pytest.mark.parametrize("name", sorted(_MUST_BE_IMPORTED))
def test_no_authority_is_re_bound_in_this_module(name):
    """Importing and then shadowing is declaring with extra steps."""
    assert name not in _assigned_names()


def _restated_authorities(tree: ast.AST) -> list[str]:
    """Every place a literal collection writes down a value an authority owns.

    Tuples, lists and sets are read whole. A dict is read as a RECORD when its
    values are computed -- `{"name": name, "label": WELL_LABELS[name]}` is a
    payload shape, and its keys are field names, not a vocabulary -- and as a
    TABLE when a value is a constant, in which case both halves are read:
    `{"measure": "Measure"}` is a second well vocabulary however it is spelled.
    """
    offences: list[str] = []

    def _flag(entry: ast.AST) -> None:
        if isinstance(entry, ast.Constant) and isinstance(entry.value, str):
            if entry.value in _AUTHORITY_VALUES:
                offences.append(f"line {entry.lineno}: {entry.value!r}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for entry in node.elts:
                _flag(entry)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(value, ast.Constant):
                    if key is not None:
                        _flag(key)
                    _flag(value)
    return offences


def test_no_literal_collection_restates_a_well_a_role_a_family_or_a_profile():
    """The measure that a grep cannot make: a VALUE of an authority, written here.

    A second list of wells is a second vocabulary, and the second one drifts.
    """
    offences = _restated_authorities(_TREE)
    assert not offences, (
        f"{_MODULE.name} writes an authority's value into a literal collection: "
        f"{'; '.join(offences)}. Import it."
    )


def test_the_guard_above_is_not_vacuous():
    """An instrument that cannot fail measures nothing.

    Both shapes a redeclaration actually takes are put to it: the tuple of
    values, and the table keyed by them.
    """
    assert _restated_authorities(ast.parse('WELLS = ("measure", "dimension")'))
    assert _restated_authorities(ast.parse('LABELS = {"measure": "Measure"}'))
    assert _restated_authorities(ast.parse('FAMILIES = ["bar", "line"]'))
    assert _restated_authorities(ast.parse('PROFILES = {"console"}'))
    # And it does not fire on a payload shape built FROM the authority.
    assert not _restated_authorities(
        ast.parse('rows = [{"name": name, "label": WELL_LABELS[name]} for name in WELL_ROLES]')
    )


def test_the_walker_is_imported_and_this_module_declares_none():
    """One walker. A second one would carry a second closed key set and a second scan."""
    defined = {
        node.name for node in ast.walk(_TREE) if isinstance(node, ast.FunctionDef)
    }
    assert "walk_document" not in defined
    assert not {name for name in defined if name.startswith("_walk")}
    called = {
        node.func.id
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "walk_document" in called


# ---------------------------------------------------------------------------
# 2. The derived grammar agrees with the authorities, value for value.
# ---------------------------------------------------------------------------


def _leaves(spec, path="") -> list[tuple[str, object]]:
    if isinstance(spec, Leaf):
        return [(path, spec)]
    if isinstance(spec, Node):
        return [
            pair
            for key, child in spec.keys.items()
            for pair in _leaves(child, f"{path}/{key}")
        ]
    if isinstance(spec, ArrayOf):
        return _leaves(spec.item, f"{path}/*")
    if isinstance(spec, MapOf):
        return [(f"{path}<key>", spec), *_leaves(spec.value, f"{path}/*")]
    raise AssertionError(f"unhandled grammar node {type(spec).__name__}")


def _all_leaves(grammar) -> list[tuple[str, object]]:
    return [pair for key, child in grammar.items() for pair in _leaves(child, f"/{key}")]


def test_the_family_enum_is_the_shipped_registry_list():
    assert TEMPLATE_GRAMMAR["family"].values == frozenset(SPEC_SELECTABLE_FAMILY_IDS)


def test_the_requires_block_is_keyed_by_the_well_vocabulary():
    requires = TEMPLATE_GRAMMAR["requires"]
    assert requires.key_values == frozenset(WELL_ROLES)
    assert requires.value.keys["accepts"].values == frozenset(SEMANTIC_ROLES)


def test_the_responsive_profiles_cross_over_untouched():
    profiles = TEMPLATE_GRAMMAR["responsive"].keys["profiles"]
    assert profiles.values == frozenset(RESPONSIVE_PROFILES)
    assert profiles is GRAMMAR["responsive"].keys["profiles"], "it is the SAME leaf, not a copy"


def test_no_member_or_evidence_anchor_survives_anywhere_in_the_template_grammar():
    """The whole grammar, at every depth. This is the object's definition."""
    forbidden = {"member_id", "member_id_list", "evidence_id"}
    survivors = [
        path
        for path, leaf in _all_leaves(TEMPLATE_GRAMMAR)
        if getattr(leaf, "kind", None) in forbidden or getattr(leaf, "key_kind", None) in forbidden
    ]
    assert not survivors, f"a Chart Template would carry data at {survivors}"


def test_every_re_anchored_leaf_accepts_exactly_the_wells():
    """A well anchor is the well vocabulary and nothing else -- no wider, no narrower."""
    re_anchored = [
        (path, leaf)
        for path, leaf in _all_leaves(TEMPLATE_GRAMMAR)
        if path.endswith("/well") or path.endswith("/datum_wells") or path.endswith("<key>")
    ]
    assert re_anchored, "the derivation re-anchored nothing; the rule stopped running"
    for path, leaf in re_anchored:
        values = getattr(leaf, "values", None) or getattr(leaf, "key_values", None)
        assert values == frozenset(WELL_ROLES), path


def test_the_live_grammar_is_exactly_what_the_derivation_produces():
    """Nothing is hand-patched after the derivation runs."""
    again = derive_template_grammar(
        GRAMMAR,
        wells=WELL_ROLES,
        roles=SEMANTIC_ROLES,
        families=SPEC_SELECTABLE_FAMILY_IDS,
        contract_version=CHART_TEMPLATE_CONTRACT_VERSION,
    )
    assert again.grammar == TEMPLATE_GRAMMAR


# ---------------------------------------------------------------------------
# 3. The mutation proof: the derivation FOLLOWS its authorities, or it raises.
# ---------------------------------------------------------------------------


def _derive(grammar=None, *, wells=None, roles=None, families=None):
    return derive_template_grammar(
        grammar if grammar is not None else GRAMMAR,
        wells=wells if wells is not None else WELL_ROLES,
        roles=roles if roles is not None else SEMANTIC_ROLES,
        families=families if families is not None else SPEC_SELECTABLE_FAMILY_IDS,
        contract_version=CHART_TEMPLATE_CONTRACT_VERSION,
    )


def test_a_well_added_to_the_vocabulary_reaches_the_template():
    grown = (*WELL_ROLES, "trellis")
    derived = _derive(wells=grown).grammar
    assert derived["requires"].key_values == frozenset(grown)
    for path, leaf in _all_leaves(derived):
        if path.endswith("/well") or path.endswith("/datum_wells") or path.endswith("<key>"):
            values = getattr(leaf, "values", None) or getattr(leaf, "key_values", None)
            assert "trellis" in values, path


def test_a_semantic_role_added_to_the_vocabulary_reaches_the_requires_block():
    grown = (*SEMANTIC_ROLES, "cohort")
    derived = _derive(roles=grown).grammar
    assert derived["requires"].value.keys["accepts"].values == frozenset(grown)


def test_a_family_added_to_the_registry_reaches_the_template():
    grown = (*SPEC_SELECTABLE_FAMILY_IDS, "distribution")
    derived = _derive(families=grown).grammar
    assert derived["family"].values == frozenset(grown)


def test_a_root_key_added_to_the_spec_grammar_crosses_over_unchanged():
    grown = dict(GRAMMAR)
    grown["density"] = Node({"mode": Leaf("enum", frozenset({"packed", "loose"}), default="loose")})
    derived = _derive(grown).grammar
    assert "density" in derived
    assert derived["density"] == grown["density"], "a pass-through key crosses unchanged"


def test_a_root_key_removed_from_the_spec_grammar_leaves_the_template():
    shrunk = {key: value for key, value in GRAMMAR.items() if key != "legend"}
    derived = _derive(shrunk).grammar
    assert "legend" not in derived


def test_a_new_member_anchor_with_no_well_anchored_name_raises_rather_than_leaking():
    """THE guard. A Spec grammar that grows a member anchor cannot silently
    produce a Chart Template that names a member: the derivation stops."""
    grown = dict(GRAMMAR)
    grown["highlight"] = Node({"focus_member_id": Leaf("member_id")})
    with pytest.raises(TemplateGrammarDerivationError) as excinfo:
        _derive(grown)
    assert "focus_member_id" in str(excinfo.value)
    assert "_WELL_ANCHOR_NAMES" in str(excinfo.value)


def test_a_new_evidence_anchor_is_subtracted_and_the_subtraction_is_named():
    grown = dict(GRAMMAR)
    grown["callouts"] = ArrayOf(
        Node({"evidence_id": Leaf("evidence_id"), "tone": Leaf("enum", frozenset({"quiet"}))}), 4
    )
    derived = _derive(grown)
    assert "callouts" not in derived.grammar
    assert "callouts" in derived.subtracted
    assert "names no Result" in derived.subtracted["callouts"]


def test_the_bindings_block_is_replaced_and_never_merely_dropped():
    derived = _derive()
    assert "bindings" not in derived.grammar
    assert derived.renamed["bindings"] == "requires"
    assert "requires" in derived.grammar
    # And the sentence a caller sending `bindings` will read.
    assert "names no member" in derived.subtracted["bindings"]


def test_the_spec_grammar_is_not_mutated_by_deriving_from_it():
    """A derivation that edited its authority would be the second authority."""
    before = dict(GRAMMAR)
    _derive()
    assert GRAMMAR == before
    assert "requires" not in GRAMMAR
    assert "bindings" in GRAMMAR
