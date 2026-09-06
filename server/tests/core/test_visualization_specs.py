"""Story 50.4 -- the grammar itself: closed, versioned twice, and canonically hashed.

These tests never touch a database. They pin the SHAPE of the contract: which
keys exist, that both version keys are mandatory, that an undeclared key is
refused with its exact JSON pointer at every depth rather than stripped, and that
two documents meaning the same thing hash identically while two meaning different
things do not.

The key-set assertion is the load-bearing one. Adding a key to `GRAMMAR` without
updating the story's list turns this file red rather than widening the persisted
surface silently -- which is the only mechanism that stops a grammar from growing
a free-form field one story at a time.
"""

from __future__ import annotations

import pytest
from core.query_specs import canonical_hash
from core.visualization_families import FAMILY_IDS, WELL_ROLES
from core.visualization_specs import (
    GRAMMAR_KEYS,
    MAX_SPEC_BYTES,
    RESPONSIVE_PROFILES,
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VISUALIZATION_SPEC_SCHEMA_VERSION,
    normalize_document,
)

#: The exact top-level key list of AC2, transcribed from the story, in its order.
#: It is written out here rather than imported so the test can DISAGREE with the
#: module -- a test that imports the thing it checks proves nothing.
AC2_KEYS = (
    "spec_contract_version",
    "schema_version",
    "family",
    "bindings",
    "order",
    "top_n",
    "axes",
    "legend",
    "formatting",
    "color",
    "thresholds",
    "reference_lines",
    "annotations",
    "interactions",
    "evidence",
    "responsive",
    "accessibility",
    "labels",
)


def minimal(**overrides) -> dict:
    """The smallest document that means something: a bar of one measure by one dimension."""
    document = {
        "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
        "schema_version": VISUALIZATION_SPEC_SCHEMA_VERSION,
        "family": "bar",
        "bindings": {"measure": ["clicks"], "dimension": ["channel"]},
    }
    document.update(overrides)
    return document


def codes(refusals) -> list[str]:
    return [r.code for r in refusals]


def subjects(refusals) -> list[str]:
    return [r.subject for r in refusals]


# ---------------------------------------------------------------------------
# AC2 -- the key set, both version keys, and the responsive enum.
# ---------------------------------------------------------------------------


def test_the_declared_key_set_is_exactly_the_contract():
    """AC2: adding a key without updating the contract turns this red."""
    assert GRAMMAR_KEYS == AC2_KEYS


def test_both_version_keys_exist_and_are_different_kinds():
    """Decision D1: a literal for the database CHECK, an integer for the runtime."""
    assert VISUALIZATION_SPEC_CONTRACT_VERSION == "visualization-spec.v1"
    assert VISUALIZATION_SPEC_SCHEMA_VERSION == 1
    assert isinstance(VISUALIZATION_SPEC_SCHEMA_VERSION, int)
    assert isinstance(VISUALIZATION_SPEC_CONTRACT_VERSION, str)


@pytest.mark.parametrize("missing", ["spec_contract_version", "schema_version"])
def test_a_document_missing_either_version_key_is_refused(missing):
    document = minimal()
    document.pop(missing)
    _normalized, refusals = normalize_document(document)
    assert "missing_field" in codes(refusals)
    assert f"/{missing}" in subjects(refusals)


def test_a_wrong_contract_version_literal_is_refused():
    _n, refusals = normalize_document(minimal(spec_contract_version="visualization-spec.v2"))
    assert "invalid_value" in codes(refusals)
    assert "/spec_contract_version" in subjects(refusals)


def test_a_wrong_schema_version_integer_is_refused():
    _n, refusals = normalize_document(minimal(schema_version=2))
    assert "invalid_value" in codes(refusals)
    assert "/schema_version" in subjects(refusals)


def test_a_schema_version_sent_as_a_string_is_refused():
    """The integer exists so a runtime can range-COMPARE it. `"1"` cannot be."""
    _n, refusals = normalize_document(minimal(schema_version="1"))
    assert "invalid_value" in codes(refusals)


def test_the_responsive_profile_enum_is_exactly_five_members():
    """Decision D6: this module is the ONE definition site. A sixth profile
    appearing in a second module turns this red instead of forking the enum.

    `mcp-pip` joined it on 2026-08-24. It was the named divergence from the Surface
    parity table of `visualization-and-rendering.md:346`, which gives the MCP
    layout as "Inline/fullscreen/PiP profile when supported"; the decision taken
    was to build the mode rather than withdraw it from the target, so the enum now
    carries the three MCP profiles the table promises.
    """
    assert set(RESPONSIVE_PROFILES) == {
        "console",
        "mcp-inline",
        "mcp-fullscreen",
        "mcp-pip",
        "share",
    }
    assert len(RESPONSIVE_PROFILES) == 5


def test_picture_in_picture_is_accepted_by_the_grammar():
    """The layout the parity table promises can be WRITTEN into a Spec version.

    Until 2026-08-24 this document was refused, so no Render could be pinned to the
    mode the ratified table named -- the promise had no storable form.
    """
    normalized, refusals = normalize_document(minimal(responsive={"profiles": ["mcp-pip"]}))
    assert refusals == []
    assert normalized["responsive"]["profiles"] == ["mcp-pip"]


def test_an_unknown_responsive_profile_is_refused():
    _n, refusals = normalize_document(minimal(responsive={"profiles": ["mcp-carousel"]}))
    assert "invalid_value" in codes(refusals)


def test_the_database_check_mirrors_this_enum():
    """The vocabulary has one definition site; the CHECK that gates the deploy-time
    projection must carry the same list, or the drift is discovered on a deployment.

    `app.renderer_runtime_builds.responsive_profiles` is written by
    `scripts/register_renderer_builds.py` from the shipped registry, and every
    renderer declares the whole vocabulary. So a value added to the tuple above
    without its migration does not fail here, or in the browser, or in CI -- it
    fails when the projection runs against production, which is the last place to
    learn it. This test reads the NEWEST migration that defines the constraint,
    never a number transcribed into it, so the next widening is measured the same
    way without editing this file.
    """
    import pathlib
    import re

    migrations = pathlib.Path(__file__).resolve().parents[3] / "infra" / "nango" / "migrations"
    defining = sorted(
        (
            path
            for path in migrations.glob("*.sql")
            if "ck_renderer_runtime_builds_profiles CHECK" in path.read_text(encoding="utf-8")
        ),
        # By the migration NUMBER, not the filename: a four-digit migration would
        # sort before `999_` as text, and this test would then read a superseded
        # constraint and call the drift clean.
        key=lambda path: int(path.name.split("_", 1)[0]),
    )
    assert defining, "no migration defines ck_renderer_runtime_builds_profiles"
    newest = defining[-1].read_text(encoding="utf-8")
    block = newest.split("ck_renderer_runtime_builds_profiles CHECK", 1)[1].split(");", 1)[0]
    listed = re.findall(r"'([^']+)'", block.split("<@", 1)[1])
    assert listed == list(RESPONSIVE_PROFILES), (
        f"{defining[-1].name} allows {listed}; the enum is {list(RESPONSIVE_PROFILES)}"
    )


def test_the_ten_wells_are_the_grammar_binding_keys():
    """AC7's vocabulary and the grammar's `bindings` keys are one list, not two."""
    normalized, refusals = normalize_document(minimal())
    assert refusals == []
    assert tuple(normalized["bindings"].keys()) == WELL_ROLES
    assert WELL_ROLES == (
        "measure",
        "dimension",
        "time",
        "series",
        "breakdown",
        "facet",
        "color",
        "size",
        "label",
        "detail",
    )


def test_the_family_enum_is_the_first_release_subset():
    # Story 55.1 added `ai_path`. It is DECLARED but not spec-selectable: its rung
    # well accepts only `classification`, a role with no server source, so
    # `check_shape_compatibility` refuses it for every Result. The tuple that the
    # database CHECK mirrors is `SPEC_SELECTABLE_FAMILY_IDS`, asserted in
    # `test_ai_path_visual_family.py`.
    assert FAMILY_IDS == (
        "table",
        "kpi",
        "line",
        "area",
        "bar",
        "stacked_bar",
        "scatter",
        "waterfall",
        "ai_path",
    )


# ---------------------------------------------------------------------------
# AC2 -- unknown keys are REFUSED with a pointer, at every depth, never stripped.
# ---------------------------------------------------------------------------


def test_a_top_level_unknown_key_is_refused_with_its_pointer():
    _n, refusals = normalize_document(minimal(subtitle="hello"))
    assert "unknown_field" in codes(refusals)
    assert "/subtitle" in subjects(refusals)


def test_a_nested_unknown_key_is_refused_with_the_same_code():
    """AC2: the rule applies at EVERY depth, not only at the top level."""
    _n, refusals = normalize_document(
        minimal(axes={"y": {"scale": "linear", "__proto__": {"polluted": True}}})
    )
    assert "unknown_field" in codes(refusals)
    assert "/axes/y/__proto__" in subjects(refusals)


def test_a_binding_that_is_an_object_names_the_word_it_smuggled():
    """`bindings.measure.encode` is refused as an unknown key, not only as a shape."""
    _n, refusals = normalize_document(
        minimal(bindings={"measure": {"encode": {"x": 0}}, "dimension": ["channel"]})
    )
    assert "unknown_field" in codes(refusals)
    assert "/bindings/measure/encode" in subjects(refusals)


def test_an_unknown_key_is_never_stripped_into_silence():
    """A stripped key leaves an accepted document that hides a rejected instruction."""
    normalized, refusals = normalize_document(minimal(onClick="alert(1)"))
    assert refusals, "the document was accepted with an undeclared key"
    assert "onClick" not in normalized


def test_the_normalized_document_carries_every_declared_key():
    """Absent-vs-default is normalized, which is what makes two equal meanings hash equal."""
    normalized, refusals = normalize_document(minimal())
    assert refusals == []
    assert tuple(normalized.keys()) == AC2_KEYS
    assert normalized["accessibility"]["table_fallback"] == "required"
    assert normalized["order"] == {"source": "result"}
    assert normalized["top_n"] is None


# ---------------------------------------------------------------------------
# AC2 -- canonical hashing.
# ---------------------------------------------------------------------------


def test_two_documents_that_mean_the_same_thing_hash_identically():
    a, _ = normalize_document(minimal())
    b, _ = normalize_document(
        {
            "family": "bar",
            "schema_version": 1,
            "bindings": {"dimension": ["channel"], "measure": ["clicks"]},
            "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
            # Every one of these is the DEFAULT, restated explicitly.
            "order": {"source": "result"},
            "legend": {"position": "right", "visible": True},
            "accessibility": {"summary_source": "result_manifest", "table_fallback": "required"},
        }
    )
    assert canonical_hash(a) == canonical_hash(b)


def test_an_order_irrelevant_array_does_not_change_the_hash():
    a, _ = normalize_document(minimal(responsive={"profiles": ["console", "share"]}))
    b, _ = normalize_document(minimal(responsive={"profiles": ["share", "console"]}))
    assert canonical_hash(a) == canonical_hash(b)


def test_an_ordered_array_does_change_the_hash():
    """Measure order is series order, and series order is visible. It is preserved."""
    a, _ = normalize_document(
        minimal(bindings={"measure": ["clicks", "cost"], "dimension": ["channel"]})
    )
    b, _ = normalize_document(
        minimal(bindings={"measure": ["cost", "clicks"], "dimension": ["channel"]})
    )
    assert canonical_hash(a) != canonical_hash(b)


def test_two_documents_that_mean_different_things_hash_differently():
    a, _ = normalize_document(minimal())
    b, _ = normalize_document(minimal(family="line"))
    assert canonical_hash(a) != canonical_hash(b)


def test_the_hash_function_is_story_50_1s_and_is_not_reimplemented():
    """One hash function per epic. Two is two authorities that diverge."""
    import inspect

    import core.visualization_specs as module

    source = inspect.getsource(module)
    assert "from core.query_specs import canonical_hash" in source
    assert "def canonical_hash" not in source


def test_the_size_cap_matches_the_database_check():
    """Anchored on THIS FILE, never on the process working directory.

    Opened by a CWD-relative path this raised `FileNotFoundError` under
    `cd server && pytest ...`, so the assertion that the Python cap and the
    database CHECK agree simply did not run outside the repository root.
    """
    import pathlib

    assert MAX_SPEC_BYTES == 32_768
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    sql = repo_root.joinpath("infra/nango/migrations/156_visualization_specs.sql").read_text(
        encoding="utf-8"
    )
    assert f"pg_column_size(spec) <= {MAX_SPEC_BYTES}" in sql
