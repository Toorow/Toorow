"""A view, a Datastream and a semantic object can carry a business key (49.6).

`context-hub.md` refuses the story while *"a view, Datastream or semantic object
cannot be linked to business context"*. Two of the three were already possible
server-side; the third was possible nowhere, and the Datastream was possible
everywhere except the browser.

The test that matters most here is the last one: it holds the two sides in step.
A target type the server accepts and the browser cannot name is a capability
nobody can reach, which is how this criterion stayed open with half of it built.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core.business_taxonomy import (
    BUSINESS_TARGET_TYPES,
    BusinessTaxonomyError,
    TaxonomyNotFoundError,
    _validate_link_kinds,
    validate_target_reference,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT = "proj_EXAMPLE"


class _Cursor:
    def __init__(self, found: bool):
        self._found = found
        self.statements: list[str] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append(" ".join(sql.split()))

    def fetchone(self):
        return (1,) if self._found else None


class _Conn:
    def __init__(self, found: bool = True):
        self.cur = _Cursor(found)

    def cursor(self) -> _Cursor:
        return self.cur


# --- The three the criterion names -----------------------------------------


@pytest.mark.parametrize(
    "target_type,table",
    [
        ("datastream", "app.datastreams"),
        ("semantic_view", "app.semantic_views"),
        ("semantic_concept", "app.semantic_concepts"),
    ],
)
def test_each_governed_target_resolves_against_its_own_owner(target_type, table) -> None:
    conn = _Conn(found=True)

    validate_target_reference(conn, project_id=PROJECT, target_type=target_type, target_id="x_1")

    assert table in conn.cur.statements[0]
    # Project scope is part of the lookup, not a filter afterwards.
    assert "project_id = %s" in conn.cur.statements[0]


@pytest.mark.parametrize(
    "target_type", ["datastream", "semantic_view", "semantic_concept"]
)
def test_a_target_that_does_not_exist_is_refused_not_linked(target_type) -> None:
    conn = _Conn(found=False)

    with pytest.raises(TaxonomyNotFoundError):
        validate_target_reference(
            conn, project_id=PROJECT, target_type=target_type, target_id="ghost"
        )


def test_a_semantic_link_pins_the_object_and_never_a_version() -> None:
    """A business link says "this belongs to that", which survives publication.

    Pinning a version would break the link every time the owner published, and
    re-pointing it would be Context Hub editing Governance's state.
    """
    conn = _Conn(found=True)

    validate_target_reference(
        conn, project_id=PROJECT, target_type="semantic_view", target_id="sv_1"
    )

    statement = conn.cur.statements[0]
    assert "semantic_view_versions" not in statement
    assert "app.semantic_views" in statement


def test_an_unknown_target_type_is_refused_by_a_message_that_lists_the_real_set() -> None:
    """The message is derived from the set, so it cannot drift below it."""
    with pytest.raises(BusinessTaxonomyError) as exc:
        _validate_link_kinds("business_domain", "spreadsheet")

    message = str(exc.value)
    for target_type in BUSINESS_TARGET_TYPES:
        assert target_type in message


# --- The two sides stay in step --------------------------------------------


def test_the_browser_can_name_every_target_type_the_server_accepts() -> None:
    """The asymmetry this commit closes, held closed.

    `businessTaxonomyApi.ts` stopped at `report_view` while the server already
    accepted `datastream`: the capability existed and no screen could express
    it. A type on one side only is a link nobody can create.
    """
    source = (
        REPO_ROOT / "ui" / "admin" / "src" / "connaissances" / "businessTaxonomyApi.ts"
    ).read_text(encoding="utf-8")
    start = source.index("export const BUSINESS_TARGET_TYPES")
    declared = source[start : source.index("] as const", start)]

    missing = [t for t in sorted(BUSINESS_TARGET_TYPES) if f'"{t}"' not in declared]
    assert missing == [], f"the browser cannot name these server-accepted targets: {missing}"


def test_the_link_dialog_derives_its_options_instead_of_retyping_them() -> None:
    """There were three copies of this list, and all three had drifted.

    The Context Hub link dialog carried a literal
    `["topic", "procedure", "target_field", "schema_doc", "report_view"]`, so the
    only screen that can create a business link offered five of the eight types
    the server accepts.

    Note what this does NOT assert. `KnowledgeGraphPage.NodeTypeKey` overlaps
    heavily with this list and is deliberately left alone: it is the GRAPH NODE
    vocabulary, and it contains `business_domain` and `business_classification`,
    which are the taxonomy side of a link and never a target of one. Two
    vocabularies that share most of their values are still two vocabularies, and
    merging them to remove a duplicate would be the more expensive mistake.
    """
    layout = (
        REPO_ROOT / "ui" / "admin" / "src" / "connaissances" / "ContextHubLayout.tsx"
    ).read_text(encoding="utf-8")

    assert "BUSINESS_TARGET_TYPES" in layout
    # Whitespace-insensitive, and it does not stop at the import. The first
    # version checked `'"schema_doc", "report_view"'` verbatim, so a re-typed
    # list written without spaces after the commas slipped past it -- and the
    # companion guard skips any file that mentions BUSINESS_TARGET_TYPES at all,
    # which this one does, in its import.
    squeezed = "".join(layout.split())
    assert '"schema_doc","report_view"' not in squeezed


def test_no_screen_reinvents_the_business_target_list() -> None:
    """The absence of `business_domain` is what identifies THIS list.

    The first version of this guard keyed on `report_view` next to `datastream`,
    which stopped discriminating the moment Story 49.6 taught the mindmap to draw
    Datastream nodes -- `NodeTypeKey` legitimately holds both now, and the guard
    fired on it.

    The durable difference is structural, not incidental: a business TARGET is
    never a business KEY, so `business_domain` and `business_classification`
    appear in the graph node vocabulary and can never appear in this one. A file
    holding the targets without the keys is re-typing this list.
    """
    ui = REPO_ROOT / "ui" / "admin" / "src"
    offenders = []
    for path in ui.rglob("*.ts*"):
        if path.name == "businessTaxonomyApi.ts":
            continue
        text = path.read_text(encoding="utf-8")
        if "BUSINESS_TARGET_TYPES" in text:
            continue
        looks_like_targets = '"report_view"' in text and '"datastream"' in text
        is_graph_vocabulary = '"business_domain"' in text
        if looks_like_targets and not is_graph_vocabulary:
            offenders.append(path.relative_to(ui).as_posix())

    assert offenders == [], f"these re-type the business target list: {offenders}"


# --- The field a project declared under its own source (AI-298) -------------


def test_a_canonical_field_resolves_by_id_against_the_mdm_registry() -> None:
    """The hole `target_field` could not reach.

    `target_field` resolves a NAME against `app.target_fields`, the governed
    dictionary -- one bounded platform list. A field a project declared under its
    own source has an id in `app.mdm_canonical_fields` and no dictionary row, so
    Governance listed it and a Datastream mapping bound it while Context Hub
    could name nothing but the dictionary.
    """
    conn = _Conn(found=True)

    validate_target_reference(
        conn, project_id=PROJECT, target_type="canonical_field", target_id="mdmcf_1"
    )

    statement = conn.cur.statements[0]
    assert "app.mdm_canonical_fields" in statement
    assert "id = %s" in statement
    # Resolved by id, never by name: two projects may each declare a field called
    # `revenue_net`, and only the id tells them apart.
    assert "canonical_name" not in statement


def test_a_canonical_field_is_visible_at_both_scopes() -> None:
    """`IS NULL OR =` is the scope rule of this repository.

    A platform field belongs to every project; a project field to its own only.
    A bare `project_id = %s` would have made the shared vocabulary unlinkable,
    which is the same class of hole one layer up.
    """
    conn = _Conn(found=True)

    validate_target_reference(
        conn, project_id=PROJECT, target_type="canonical_field", target_id="mdmcf_1"
    )

    assert "(project_id IS NULL OR project_id = %s)" in conn.cur.statements[0]


def test_an_archived_canonical_field_is_not_linkable() -> None:
    conn = _Conn(found=True)

    validate_target_reference(
        conn, project_id=PROJECT, target_type="canonical_field", target_id="mdmcf_1"
    )

    assert "status = 'active'" in conn.cur.statements[0]


def test_a_canonical_field_that_does_not_exist_is_refused_not_linked() -> None:
    conn = _Conn(found=False)

    with pytest.raises(TaxonomyNotFoundError):
        validate_target_reference(
            conn, project_id=PROJECT, target_type="canonical_field", target_id="ghost"
        )


def test_every_target_type_can_be_asked_for_its_version() -> None:
    """The reader the 49.6 wave forgot, found while adding the ninth type.

    `_version_for_node` looks the node type up in a dict. `datastream`,
    `semantic_view` and `semantic_concept` became linkable and were never added,
    so previewing the path of a link to one of them raised `KeyError` -- a 500
    on a governed read, reachable from the console.

    Driven for EVERY target type rather than for the new one: a target set is
    widened by walking its readers, and this is the one that had been skipped.
    """
    from core.business_taxonomy import _version_for_node

    for target_type in sorted(BUSINESS_TARGET_TYPES):
        conn = _Conn(found=True)
        # No assertion on the value: what is being proven is that every type has
        # an answer -- a version, or a stated absence -- and none explodes.
        _version_for_node(conn, node_type=target_type, node_id="obj_1")


def test_the_two_field_notions_stay_two() -> None:
    """Merging them would make the link ambiguous the moment anything resolved it.

    They differ in store, in key and in scope. A single `field` type would have
    to guess which one an id meant, and the guess would be wrong for exactly the
    fields this type was added to reach.
    """
    assert {"target_field", "canonical_field"} <= BUSINESS_TARGET_TYPES

    dictionary = _Conn(found=True)
    validate_target_reference(
        dictionary, project_id=PROJECT, target_type="target_field", target_id="revenue"
    )
    registry = _Conn(found=True)
    validate_target_reference(
        registry, project_id=PROJECT, target_type="canonical_field", target_id="mdmcf_1"
    )

    assert "app.target_fields" in dictionary.cur.statements[0]
    assert "name = %s" in dictionary.cur.statements[0]
    assert "app.mdm_canonical_fields" in registry.cur.statements[0]
