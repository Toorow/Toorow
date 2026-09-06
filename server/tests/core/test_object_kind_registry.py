"""A client object kind refuses to exist without a stable identity (Story 64.1).

Proven WITHOUT a database, the discipline `test_master_data.py` states in its own
header: the rules are generic, so a test that reached for Postgres would prove
them for whoever had a Postgres and for nobody else.

The one rule these files exist to hold: an object whose feeding mapping declares
no grain has no stable identity. Two rows spelling the same thing would be one
object today and two tomorrow, and every alias resolved against that identity
(Story 64.10) inherits the instability -- a resolver that refuses ambiguity is
worth nothing on top of an identity that wobbles.
"""

from __future__ import annotations

import pytest
from core.master_data import MasterDataError
from core.object_kind_registry import identity_fields, validate_declaration

KIND = "video"
LABEL = "Videos"


def _declare(payload):
    return validate_declaration(object_kind=KIND, label=LABEL, mapping_payload=payload)


# ---------------------------------------------------------------------------
# The grain IS the identity -- it is read, never copied.
# ---------------------------------------------------------------------------


def test_identity_comes_from_the_mapping_grain():
    """The identity fields are the mapping's grain, in the order it declares them.

    Order is preserved because a reader shows the grain as the operator wrote it;
    re-sorting would make two identical declarations look different in a diff.
    """
    assert _declare({"grain": ["video_id"]}) == ("video_id",)
    assert _declare({"grain": ["channel_id", "video_id"]}) == ("channel_id", "video_id")


def test_a_payload_without_grain_is_not_read_as_empty_success():
    """No grain key at all and an empty grain are the same refusal, not a pass."""
    for payload in ({}, {"grain": []}, {"fields": [{"field_id": "video_id"}]}, None):
        with pytest.raises(MasterDataError) as excinfo:
            _declare(payload)
        assert "no stable identity" in str(excinfo.value)


def test_the_refusal_names_where_the_repair_belongs():
    """An error that does not say WHERE to go sends the operator to the wrong desk.

    The grain lives on the Datastream's Mapping workbench, which Data owns; this
    module may refuse the declaration but must never invite somebody to fix a
    mapping from Governance.
    """
    with pytest.raises(MasterDataError) as excinfo:
        _declare({"grain": []})
    assert "Datastream" in str(excinfo.value) and "mapping" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Malformed grains are reported, never silently repaired.
# ---------------------------------------------------------------------------


def test_a_repeated_field_is_refused_and_not_deduplicated():
    """Deduplicating here would hide a defect that belongs to the mapping.

    A grain naming a field twice is malformed. Collapsing it would let a broken
    mapping publish an object kind that looks well-formed from Governance, and
    the defect would be found much later, on a number.
    """
    with pytest.raises(MasterDataError) as excinfo:
        _declare({"grain": ["video_id", "video_id"]})
    assert "twice" in str(excinfo.value)


def test_an_unnamed_field_inside_the_grain_is_refused():
    """A column nobody can name cannot identify anything."""
    with pytest.raises(MasterDataError):
        _declare({"grain": ["video_id", "   "]})


def test_a_string_grain_is_refused_rather_than_iterated_as_characters():
    """`grain: "video_id"` is a malformed mapping, not an eight-field identity.

    Python would happily iterate the string into characters and produce a grain
    of `v, i, d, e, o, _, i, d`, every one of them a "field". The refusal is the
    only thing between that and an object kind identified by letters.
    """
    with pytest.raises(MasterDataError) as excinfo:
        identity_fields({"grain": "video_id"})
    assert "list of field ids" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The generic core's shape guards still apply at declaration time.
# ---------------------------------------------------------------------------


def test_a_malformed_kind_is_refused_before_the_grain_is_considered():
    """The caller learns at declaration time, not at INSERT time.

    `object_kind` is an opaque string to the core, but it is not free text: the
    database CHECK is `^[a-z][a-z0-9_]{1,39}$`. Refusing here means an operator
    sees "this name is not usable" instead of a constraint violation.
    """
    for bad in ("Video", "1video", "video-kind", ""):
        with pytest.raises(MasterDataError):
            validate_declaration(
                object_kind=bad, label=LABEL, mapping_payload={"grain": ["video_id"]}
            )


def test_a_valid_declaration_returns_its_identity_for_the_caller_to_show():
    """The happy path returns the fields rather than a boolean.

    A screen must be able to say WHICH columns identify the object -- "declared"
    with no identity shown is exactly the state this story refuses.
    """
    assert _declare({"grain": ["video_id"], "fields": []}) == ("video_id",)


# ---------------------------------------------------------------------------
# Story 64.13 -- the second mode, which is the MAJORITY case.
#
# Measured on the workbook that motivated this epic: one entity of five carries a
# key. `video` has 527 ids for 531 rows; `restaurant`, `produit`, `recette` and
# `film` carry none. The first version of `validate_declaration` knew only
# `source_key` and refused all four -- including `restaurant`, which carries the
# measured factor 3 in audience.
# ---------------------------------------------------------------------------

WORKBOOK = {
    "grain": [],
    "fields": [
        {"field_id": "restaurant_name"},
        {"field_id": "product_name"},
        {"field_id": "confidence"},
    ],
}


def test_a_source_with_no_key_declares_a_governed_label_instead_of_being_refused():
    """`restaurant`: 260 mentions, 253 labels, no id column anywhere.

    Under `source_key` this is refused and the whole entity is unreachable. The
    node becomes the identity instead, resolved through master_data_aliases.
    """
    assert (
        validate_declaration(
            object_kind="restaurant",
            label="Restaurants",
            mapping_payload=WORKBOOK,
            identity_mode="governed_label",
            label_field="restaurant_name",
        )
        == ()
    )


def test_a_governed_label_without_a_label_field_resolves_nothing():
    """A declaration that looks complete and produces no object is the worst state."""
    with pytest.raises(MasterDataError) as excinfo:
        validate_declaration(
            object_kind="restaurant",
            label="Restaurants",
            mapping_payload=WORKBOOK,
            identity_mode="governed_label",
        )
    assert "nothing to resolve" in str(excinfo.value)


def test_a_label_field_the_mapping_does_not_declare_is_refused():
    """A label column that does not exist resolves nothing, quietly, forever."""
    with pytest.raises(MasterDataError) as excinfo:
        validate_declaration(
            object_kind="restaurant",
            label="Restaurants",
            mapping_payload=WORKBOOK,
            identity_mode="governed_label",
            label_field="venue_name",
        )
    assert "declares no field" in str(excinfo.value)


def test_a_source_that_HAS_a_key_may_not_be_resolved_by_spelling():
    """Not a data error -- a CONTRADICTION in the declaration.

    A source carrying a grain should be joined on it. Resolving it by label would
    make two rows with the same name one object even when their keys differ.
    """
    with pytest.raises(MasterDataError) as excinfo:
        validate_declaration(
            object_kind="video",
            label="Videos",
            mapping_payload={"grain": ["video_id"], "fields": [{"field_id": "title"}]},
            identity_mode="governed_label",
            label_field="title",
        )
    assert "source_key" in str(excinfo.value)


def test_a_source_key_carrying_a_label_field_is_refused_rather_than_ignored():
    """A field nobody reads is a field somebody believes is used."""
    with pytest.raises(MasterDataError):
        validate_declaration(
            object_kind="video",
            label="Videos",
            mapping_payload={"grain": ["video_id"], "fields": [{"field_id": "title"}]},
            identity_mode="source_key",
            label_field="title",
        )


def test_an_unknown_identity_mode_is_refused_and_named():
    """The mode is a contract, not free text: a typo must not fall back to a default."""
    with pytest.raises(MasterDataError) as excinfo:
        validate_declaration(
            object_kind="video",
            label="Videos",
            mapping_payload={"grain": ["video_id"]},
            identity_mode="by_hand",
        )
    assert "unknown identity mode" in str(excinfo.value)


def test_the_source_key_refusal_now_points_at_both_repairs():
    """Refusing without naming the second mode sends the operator to the wrong desk.

    Before 64.13 the message said "declare the grain first" -- true for `video`,
    useless for the four entities that will never have one.
    """
    with pytest.raises(MasterDataError) as excinfo:
        validate_declaration(
            object_kind="restaurant", label="Restaurants", mapping_payload=WORKBOOK
        )
    message = str(excinfo.value)
    assert "governed_label" in message and "grain" in message


# ---------------------------------------------------------------------------
# Story 64.15 -- la couture entre « je declare mes champs » et « je declare mon
# objet ». L objet est DERIVE du flux, jamais reclame par l appelant.
# ---------------------------------------------------------------------------


def test_the_declaration_route_refuses_a_claimed_object_kind():
    """Le corps ne peut pas nommer l objet -- il nomme le flux, et le serveur lit.

    Un client qui pourrait nommer n importe quel `object_kind` accrocherait sa
    colonne a la definition d un AUTRE objet, et les deux repondraient ensuite
    differemment a la meme question. Lu depuis la source parce que le refus
    precede toute connexion : c est une garde de forme, pas une garde de donnee.
    """
    from pathlib import Path

    route = (
        Path(__file__).resolve().parents[2] / "core" / "file_source_template_api.py"
    ).read_text(encoding="utf-8")
    assert "object_kind_not_accepted" in route
    assert "object_kind is derived from the Datastream" in route
    # Et il est bien DERIVE, pas simplement refuse.
    assert "object_kind_for_datastream(" in route


def test_the_resolver_reads_the_registry_through_the_live_binding():
    """La requete joint le lien VIVANT au registre : un lien libere ne repond plus.

    Sans `released_at IS NULL`, un flux dont la source a ete retiree continuerait
    d estampiller des champs pour un objet qu il n alimente plus.
    """
    import inspect

    from core.object_kind_registry import object_kind_for_datastream

    source = inspect.getsource(object_kind_for_datastream)
    assert "released_at IS NULL" in source
    assert "master_data_source_bindings" in source and "master_data_registries" in source
