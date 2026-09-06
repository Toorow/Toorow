"""Story 70.2 -- the observed-entity registry, its attachment and its inheritance.

The lifecycle these build on is proven in ``test_master_data.py``; what is
proven here is the meaning laid on top of it, and specifically the three
distinctions the story exists to restore:

* an entity id alone is not an identity -- the full path is;
* one creative seen under two parents is two nodes, each with its own
  inheritance, not one node with a contradiction inside it;
* ``unresolved`` is a state that is emitted and counted, never an empty string
  and never a label somebody parsed.

Everything runs without a database: the pure model is exercised directly, and
the four writers are exercised against a scripted connection, the same shape
``test_master_data_guarded_commands.py`` uses.
"""

from __future__ import annotations

from datetime import date

import pytest
from core.master_data import MasterDataConflict, MasterDataNotFound, MasterDataUnavailable
from core.observed_entities import (
    ATTACHMENT_CONSUMER_KIND,
    LEVEL_L1,
    LEVEL_L2,
    LEVEL_L3,
    NODE_KIND_OF_LEVEL,
    OBSERVED_ENTITY_ATTACHMENT_ACTIONS,
    OBSERVED_ENTITY_OBJECT_KIND,
    PROVENANCE_INHERITED_L1,
    PROVENANCE_INHERITED_PARENT,
    PROVENANCE_SELF,
    PROVENANCE_UNRESOLVED,
    PROVENANCES,
    ObservedEntityError,
    ObservedEntityPath,
    attach_observed_entity,
    attachment_namespace,
    build_entity_projection,
    build_hierarchy_memberships,
    canonical_entity_key,
    ensure_observed_entity_registry,
    normalize_declared_dimensions,
    reattach_observed_entity,
    resolution_counts,
)

ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
REGISTRY = "mdreg_EXAMPLE"
DATASTREAM = "ds_EXAMPLE"
ACTOR = "owner@example.com"
PLATFORM = "example_ads"

C1, C2 = "mdnode_C1", "mdnode_C2"
P1, P2 = "mdnode_P1", "mdnode_P2"
A1, A2 = "mdnode_A1", "mdnode_A2"

AS_OF = date(2026, 8, 25)


def _path(level: str, **ids: str) -> ObservedEntityPath:
    return ObservedEntityPath(platform=PLATFORM, level=level, **ids)


# ---------------------------------------------------------------------------
# The identity: the whole path, encoded once, deterministically.
# ---------------------------------------------------------------------------


def test_the_same_tuple_always_encodes_to_the_same_key() -> None:
    first = canonical_entity_key(
        platform=PLATFORM, level=LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9"
    )
    second = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9").canonical_key
    third = _path(LEVEL_L3, l1_id=" c_1 ", l2_id="p_1", l3_id="cr_9").canonical_key

    assert first == second == third


def test_two_different_tuples_are_two_different_keys() -> None:
    under_first_parent = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    under_second_parent = _path(LEVEL_L3, l1_id="c_2", l2_id="p_2", l3_id="cr_9")

    assert under_first_parent.canonical_key != under_second_parent.canonical_key


def test_a_separator_inside_an_id_cannot_forge_another_path() -> None:
    """The encoding is escaped, so no id can spell a different tuple."""

    forged = _path(LEVEL_L2, l1_id="c_1|cr_9", l2_id="p_1")
    honest = _path(LEVEL_L3, l1_id="c_1", l2_id="cr_9", l3_id="p_1")

    assert forged.canonical_key != honest.canonical_key
    assert "|" not in forged.canonical_key.split("|")[3]


def test_the_same_id_at_two_levels_is_two_keys() -> None:
    assert (
        _path(LEVEL_L1, l1_id="x_1").canonical_key
        != _path(LEVEL_L2, l1_id="x_1", l2_id="x_1").canonical_key
    )


# ---------------------------------------------------------------------------
# The refusal: an id alone is not an identity.
# ---------------------------------------------------------------------------


def test_an_entity_id_alone_is_not_an_identity() -> None:
    with pytest.raises(ObservedEntityError) as exc:
        ObservedEntityPath(platform=PLATFORM, level=LEVEL_L3, l3_id="cr_9")

    assert "l1_id is missing" in str(exc.value)
    assert "not an identity" in str(exc.value)


def test_a_level_two_path_without_its_parent_is_refused() -> None:
    with pytest.raises(ObservedEntityError):
        ObservedEntityPath(platform=PLATFORM, level=LEVEL_L2, l2_id="p_1")


def test_a_path_without_a_platform_is_refused() -> None:
    with pytest.raises(ObservedEntityError):
        ObservedEntityPath(platform="", level=LEVEL_L1, l1_id="c_1")


def test_a_path_cannot_carry_an_id_below_its_own_level() -> None:
    with pytest.raises(ObservedEntityError) as exc:
        ObservedEntityPath(platform=PLATFORM, level=LEVEL_L1, l1_id="c_1", l2_id="p_1")

    assert "stops at its own level" in str(exc.value)


def test_an_unknown_level_is_refused() -> None:
    with pytest.raises(ObservedEntityError):
        ObservedEntityPath(platform=PLATFORM, level="l4", l1_id="c_1")


def test_a_path_names_its_parent_and_its_l1() -> None:
    creative = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")

    assert creative.parent() == _path(LEVEL_L2, l1_id="c_1", l2_id="p_1")
    assert creative.parent().parent() == _path(LEVEL_L1, l1_id="c_1")
    assert creative.parent().parent().parent() is None
    assert creative.l1_path() == _path(LEVEL_L1, l1_id="c_1")
    assert creative.node_kind == NODE_KIND_OF_LEVEL[LEVEL_L3]
    assert creative.entity_id == "cr_9"


# ---------------------------------------------------------------------------
# The hierarchy: dated edges derived from the paths, never invented.
# ---------------------------------------------------------------------------


def test_the_edges_follow_the_paths() -> None:
    paths = [
        _path(LEVEL_L1, l1_id="c_1"),
        _path(LEVEL_L2, l1_id="c_1", l2_id="p_1"),
        _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9"),
    ]
    node_of_key = {
        paths[0].canonical_key: C1,
        paths[1].canonical_key: P1,
        paths[2].canonical_key: A1,
    }

    edges = build_hierarchy_memberships(node_of_key, paths)

    assert {(edge.parent_node_id, edge.child_node_id) for edge in edges} == {
        (C1, P1),
        (P1, A1),
    }


def test_an_orphan_path_never_gets_an_invented_parent() -> None:
    """A creative whose placement was never observed stays unattached, and says so."""

    orphan = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")

    edges = build_hierarchy_memberships({orphan.canonical_key: A1}, [orphan])

    assert edges == ()


def test_a_declared_dimension_may_not_be_empty() -> None:
    with pytest.raises(ObservedEntityError) as exc:
        normalize_declared_dimensions({C1: {"brand": "   "}})

    assert "empty string reads as an answer" in str(exc.value)


def test_a_dimension_name_is_a_declared_shape() -> None:
    with pytest.raises(ObservedEntityError):
        normalize_declared_dimensions({C1: {"Brand Name": "example"}})


# ---------------------------------------------------------------------------
# The read cascade: self -> inherited_parent -> inherited_l1 -> unresolved.
# ---------------------------------------------------------------------------


def _projection(*, dimensions, nodes=None, memberships=None, as_of=AS_OF):
    from core.master_data import Membership

    default_nodes = [
        {"id": C1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L1]},
        {"id": P1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L2]},
        {"id": A1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L3]},
    ]
    default_edges = [
        Membership(parent_node_id=C1, child_node_id=P1),
        Membership(parent_node_id=P1, child_node_id=A1),
    ]
    return build_entity_projection(
        hierarchy_version_id="mdver_EXAMPLE",
        registry_id=REGISTRY,
        memberships=memberships if memberships is not None else default_edges,
        nodes=nodes if nodes is not None else default_nodes,
        payload={"node_dimensions": dimensions},
        as_of=as_of,
    )


def test_the_cascade_emits_all_four_provenances_in_one_reading() -> None:
    projection = _projection(
        dimensions={
            C1: {"brand": "Example Brand", "channel": "search"},
            P1: {"channel": "display"},
            A1: {"format": "video"},
        }
    )

    resolved = projection.resolve(A1, ("format", "channel", "brand", "audience"))

    assert resolved["format"].provenance == PROVENANCE_SELF
    assert resolved["format"].source_node_id == A1
    assert resolved["channel"].provenance == PROVENANCE_INHERITED_PARENT
    assert resolved["channel"].value == "display"
    assert resolved["channel"].source_node_id == P1
    assert resolved["brand"].provenance == PROVENANCE_INHERITED_L1
    assert resolved["brand"].value == "Example Brand"
    assert resolved["brand"].source_node_id == C1
    assert resolved["audience"].provenance == PROVENANCE_UNRESOLVED
    assert resolved["audience"].value is None
    assert resolved["audience"].source_node_id is None


def test_unresolved_is_counted_and_is_never_an_empty_string() -> None:
    projection = _projection(dimensions={C1: {}, P1: {}, A1: {}})

    resolved = projection.resolve(A1, ("brand", "channel"))
    counts = resolution_counts(resolved)

    assert [resolution.value for resolution in resolved.values()] == [None, None]
    assert counts[PROVENANCE_UNRESOLVED] == 2
    # Every state is a key, at zero when nothing landed there: a state that
    # disappears when it is empty cannot be watched.
    assert set(counts) == set(PROVENANCES)
    assert counts[PROVENANCE_SELF] == 0


def test_a_stored_blank_is_not_an_answer() -> None:
    projection = _projection(dimensions={C1: {"brand": "Example Brand"}, A1: {"brand": "  "}})

    resolved = projection.resolve(A1, ("brand",))

    assert resolved["brand"].provenance == PROVENANCE_INHERITED_L1
    assert resolved["brand"].value == "Example Brand"


def test_nothing_parses_a_label_at_level_two_or_three() -> None:
    """The L3 label spells three dimensions. None of them is read."""

    projection = _projection(
        dimensions={},
        nodes=[
            {"id": C1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L1], "label": "brand-search-fr"},
            {"id": P1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L2], "label": "display_fr_video"},
            {"id": A1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L3], "label": "video_fr_brand"},
        ],
    )

    resolved = projection.resolve(A1, ("brand", "channel", "format"))

    assert {r.provenance for r in resolved.values()} == {PROVENANCE_UNRESOLVED}


def test_a_two_level_tree_never_reports_the_same_value_twice() -> None:
    """When the L1 IS the parent, the answer is `inherited_parent` and stops there."""

    projection = _projection(
        dimensions={C1: {"brand": "Example Brand"}},
        nodes=[
            {"id": C1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L1]},
            {"id": P1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L2]},
        ],
        memberships=None,
    )

    resolved = projection.resolve(P1, ("brand",))

    assert resolved["brand"].provenance == PROVENANCE_INHERITED_PARENT
    assert resolved["brand"].source_node_id == C1


def test_one_creative_under_two_parents_is_two_nodes_with_two_inheritances() -> None:
    """The measured case: 158 of 574 creatives sat under several parents.

    Under path identity the two sightings are two keys, so they are two nodes,
    and each one inherits from the branch it actually sits in. Bound by the
    entity id alone they would be one node claiming two brands.
    """

    from core.master_data import Membership

    left = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    right = _path(LEVEL_L3, l1_id="c_2", l2_id="p_2", l3_id="cr_9")
    assert left.canonical_key != right.canonical_key
    assert left.entity_id == right.entity_id == "cr_9"

    projection = build_entity_projection(
        hierarchy_version_id="mdver_EXAMPLE",
        registry_id=REGISTRY,
        memberships=[
            Membership(parent_node_id=C1, child_node_id=P1),
            Membership(parent_node_id=P1, child_node_id=A1),
            Membership(parent_node_id=C2, child_node_id=P2),
            Membership(parent_node_id=P2, child_node_id=A2),
        ],
        nodes=[
            {"id": C1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L1]},
            {"id": C2, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L1]},
            {"id": P1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L2]},
            {"id": P2, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L2]},
            {"id": A1, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L3]},
            {"id": A2, "node_kind": NODE_KIND_OF_LEVEL[LEVEL_L3]},
        ],
        payload={
            "node_paths": {A1: left.as_dict(), A2: right.as_dict()},
            "node_dimensions": {
                C1: {"brand": "Example Brand One"},
                C2: {"brand": "Example Brand Two"},
            },
        },
        as_of=AS_OF,
    )

    both = projection.resolve_many([A1, A2], ("brand",))

    assert both[A1]["brand"].value == "Example Brand One"
    assert both[A2]["brand"].value == "Example Brand Two"
    assert both[A1]["brand"].source_node_id == C1
    assert both[A2]["brand"].source_node_id == C2
    assert both[A1]["brand"].provenance == PROVENANCE_INHERITED_L1


# ---------------------------------------------------------------------------
# The writers, against a scripted connection.
# ---------------------------------------------------------------------------

_REGISTRY_ROW = (
    REGISTRY, ORG, PROJECT, OBSERVED_ENTITY_OBJECT_KIND, "Observed Entity Registry",
    "active", None, None, None, ACTOR, None, None, None, "project", "registry",
)
_NODE_ROW = (
    A1, ORG, PROJECT, REGISTRY, NODE_KIND_OF_LEVEL[LEVEL_L3], "cr_9",
    None, None, ACTOR, None, None,
)


def _alias_row(node_id: str, key: str, alias_id: str = "mdali_EXAMPLE") -> tuple:
    return (
        alias_id, ORG, PROJECT, node_id, attachment_namespace(PROJECT), key, key,
        "exact", "connector", DATASTREAM, {}, "none", ACTOR, None, None,
    )


class _Cursor:
    """Answers by the first unconsumed script entry whose needle is in the SQL."""

    def __init__(self, script, *, explode_on: str | None = None):
        self._script = list(script)
        self._rows: list = []
        self.statements: list[tuple[str, tuple]] = []
        self._explode_on = explode_on

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        flat = " ".join(sql.split())
        self.statements.append((flat, tuple(params or ())))
        if self._explode_on and self._explode_on in flat:
            raise RuntimeError("used-by store offline")
        for index, (needle, rows) in enumerate(self._script):
            if needle in flat:
                self._script.pop(index)
                self._rows = list(rows)
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


class _Conn:
    def __init__(self, script, *, explode_on: str | None = None):
        self.cur = _Cursor(script, explode_on=explode_on)

    def cursor(self) -> _Cursor:
        return self.cur

    def sql(self) -> list[str]:
        return [statement for statement, _ in self.cur.statements]

    def params_for(self, needle: str) -> tuple:
        for statement, params in self.cur.statements:
            if needle in statement:
                return params
        raise AssertionError(f"no statement matched {needle!r}")


def test_the_registry_mounts_on_the_generic_owner() -> None:
    conn = _Conn([("INSERT INTO app.master_data_registries", [_REGISTRY_ROW])])

    registry = ensure_observed_entity_registry(
        conn, org_id=ORG, project_id=PROJECT, actor=ACTOR
    )

    params = conn.params_for("INSERT INTO app.master_data_registries")
    assert params[3] == OBSERVED_ENTITY_OBJECT_KIND
    assert registry["id"] == REGISTRY
    # Nothing else was created: the five generic tables are the whole storage.
    assert all("CREATE TABLE" not in statement for statement in conn.sql())


def test_attaching_binds_the_whole_path_and_declares_its_used_by() -> None:
    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    conn = _Conn(
        [
            ("FROM app.master_data_registries", [_REGISTRY_ROW]),
            ("FROM app.master_data_aliases", []),
            ("INSERT INTO app.master_data_nodes", [_NODE_ROW]),
            ("INSERT INTO app.master_data_aliases", [_alias_row(A1, path.canonical_key)]),
        ]
    )

    result = attach_observed_entity(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        registry_id=REGISTRY,
        path=path,
        datastream_id=DATASTREAM,
        actor=ACTOR,
    )

    assert result["outcome"] == "attached"
    assert result["node_id"] == A1
    # The stored value is the whole path, not the entity id.
    alias_params = conn.params_for("INSERT INTO app.master_data_aliases")
    assert alias_params[5] == path.canonical_key
    assert alias_params[6] == path.canonical_key
    assert "cr_9" != alias_params[6]
    # The used-by row is written in the same transaction, so a later regroup
    # has something to refuse on.
    used_by_params = conn.params_for("INSERT INTO app.master_data_used_by")
    assert used_by_params[4] == ATTACHMENT_CONSUMER_KIND
    # And the audit line rides along, under a declared action.
    audit_params = conn.params_for("INSERT INTO app.audit_log")
    assert audit_params[2] == OBSERVED_ENTITY_ATTACHMENT_ACTIONS["attached"]


def test_a_path_already_bound_elsewhere_is_sent_to_the_guarded_door() -> None:
    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    conn = _Conn(
        [
            ("FROM app.master_data_registries", [_REGISTRY_ROW]),
            ("FROM app.master_data_aliases", [_alias_row(A1, path.canonical_key)]),
        ]
    )

    with pytest.raises(MasterDataConflict) as exc:
        attach_observed_entity(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            registry_id=REGISTRY,
            path=path,
            datastream_id=DATASTREAM,
            actor=ACTOR,
            node_id=A2,
        )

    assert "reattach_observed_entity" in str(exc.value)
    assert all("INSERT INTO app.master_data_aliases" not in s for s in conn.sql())


def test_reattaching_the_same_path_to_the_same_node_writes_nothing() -> None:
    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    conn = _Conn(
        [
            ("FROM app.master_data_registries", [_REGISTRY_ROW]),
            ("FROM app.master_data_aliases", [_alias_row(A1, path.canonical_key)]),
        ]
    )

    result = attach_observed_entity(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        registry_id=REGISTRY,
        path=path,
        datastream_id=DATASTREAM,
        actor=ACTOR,
        node_id=A1,
    )

    assert result["outcome"] == "unchanged"
    assert all("INSERT" not in statement for statement in conn.sql())


#: A used-by row is SEVEN columns since 2026-09-01: `created_at` closes it,
#: because AC7 requires every reference to carry the time its dependency was
#: recorded. A six-column fixture describes a query that no longer exists.
def _regroup_script(path, *, used_by_rows):
    return [
        ("FROM app.master_data_registries", [_REGISTRY_ROW]),
        ("FROM app.master_data_aliases", [_alias_row(A1, path.canonical_key)]),
        (
            "FROM app.master_data_nodes",
            [(A2, NODE_KIND_OF_LEVEL[LEVEL_L3], "cr_9", None)],
        ),
        ("FROM app.master_data_used_by", used_by_rows),
    ]


def test_a_destructive_regroup_is_refused_while_live_consumers_remain() -> None:
    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    conn = _Conn(
        _regroup_script(
            path,
            used_by_rows=[
                # The attachment being moved -- excluded, it is the thing moving.
                (A1, ATTACHMENT_CONSUMER_KIND, "mdali_EXAMPLE", DATASTREAM, None, None, None),
                # Somebody else, who would lose those rows.
                (A1, "datastream_mapping", "dsm_EXAMPLE", "Example mapping", None, None, None),
            ],
        )
    )

    with pytest.raises(MasterDataConflict) as exc:
        reattach_observed_entity(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            registry_id=REGISTRY,
            path=path,
            node_id=A2,
            actor=ACTOR,
        )

    assert "datastream_mapping" in str(exc.value)
    assert "1 live reference(s)" in str(exc.value)
    # Refused BEFORE any write: a refusal leaves no half-move behind.
    assert all("UPDATE app.master_data_aliases" not in s for s in conn.sql())


def test_a_regroup_whose_only_consumer_is_itself_is_not_blocked() -> None:
    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    script = _regroup_script(
        path,
        used_by_rows=[
            (A1, ATTACHMENT_CONSUMER_KIND, "mdali_EXAMPLE", DATASTREAM, None, None, None)
        ],
    )
    script.append(
        ("INSERT INTO app.master_data_aliases", [_alias_row(A2, path.canonical_key, "mdali_NEW")])
    )
    conn = _Conn(script)

    result = reattach_observed_entity(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        registry_id=REGISTRY,
        path=path,
        node_id=A2,
        actor=ACTOR,
    )

    assert result["outcome"] == "reattached"
    assert result["released_node_id"] == A1
    assert any("UPDATE app.master_data_used_by" in s for s in conn.sql())
    audit_params = conn.params_for("INSERT INTO app.audit_log")
    assert audit_params[2] == OBSERVED_ENTITY_ATTACHMENT_ACTIONS["reattached"]


def test_an_acknowledged_regroup_records_what_it_went_ahead_despite() -> None:
    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    script = _regroup_script(
        path,
        used_by_rows=[
            (A1, "datastream_mapping", "dsm_EXAMPLE", "Example mapping", None, None, None)
        ],
    )
    script.append(
        ("INSERT INTO app.master_data_aliases", [_alias_row(A2, path.canonical_key, "mdali_NEW")])
    )
    conn = _Conn(script)

    result = reattach_observed_entity(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        registry_id=REGISTRY,
        path=path,
        node_id=A2,
        actor=ACTOR,
        acknowledge_impact=True,
    )

    assert [c["consumer_kind"] for c in result["consumers_at_command_time"]] == [
        "datastream_mapping"
    ]


def test_an_unreadable_used_by_store_blocks_the_regroup() -> None:
    """`fetch_used_by` raises, and this module never swallows it."""

    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    conn = _Conn(
        _regroup_script(path, used_by_rows=[]),
        explode_on="app.master_data_used_by",
    )

    with pytest.raises(MasterDataUnavailable):
        reattach_observed_entity(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            registry_id=REGISTRY,
            path=path,
            node_id=A2,
            actor=ACTOR,
        )

    assert all("UPDATE app.master_data_aliases" not in s for s in conn.sql())


def test_regrouping_a_path_that_was_never_attached_is_not_found() -> None:
    path = _path(LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9")
    conn = _Conn(
        [
            ("FROM app.master_data_registries", [_REGISTRY_ROW]),
            ("FROM app.master_data_aliases", []),
        ]
    )

    with pytest.raises(MasterDataNotFound):
        reattach_observed_entity(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            registry_id=REGISTRY,
            path=path,
            node_id=A2,
            actor=ACTOR,
        )


def test_a_path_cannot_attach_to_a_node_of_another_level() -> None:
    path = _path(LEVEL_L2, l1_id="c_1", l2_id="p_1")
    conn = _Conn(
        [
            ("FROM app.master_data_registries", [_REGISTRY_ROW]),
            ("FROM app.master_data_aliases", []),
            (
                "FROM app.master_data_nodes",
                [(A1, NODE_KIND_OF_LEVEL[LEVEL_L3], "cr_9", None)],
            ),
        ]
    )

    with pytest.raises(MasterDataConflict) as exc:
        attach_observed_entity(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            registry_id=REGISTRY,
            path=path,
            datastream_id=DATASTREAM,
            actor=ACTOR,
            node_id=A1,
        )

    assert NODE_KIND_OF_LEVEL[LEVEL_L2] in str(exc.value)
