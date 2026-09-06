"""The REFERENCE route of an import, proven off base (Story 68.5).

WHAT THIS FILE PINS. Three decisions the route makes before a single row is
written, all of them pure and therefore provable without a database:

  * WHICH files take this route (`reference_designation`) -- and, just as
    importantly, which do NOT: a file with two designations, a file whose
    entity key is not its grain, a file carrying measures. A route that
    grabbed one of those would land facts as attributes, silently;
  * what a row that cannot name its entity gets (`reference_row_validator`):
    a rejection carrying the field, the rule and the value -- never a drop;
  * what one row becomes (`build_node_payload`) and what makes two landings
    the SAME content (`version_digest`) -- the per-entity no-op that turns a
    re-imported catalogue into an honest nothing.

The storage half (real nodes, real versions, the ledger, the rejection gate)
lives in `tests/integration/test_entity_reference_import_pg.py`: it needs a
database, and it is where the acceptance criteria about versions are proven.
"""

from __future__ import annotations

from core import entity_reference_import as eri

KIND = "video"


def _field(field_id, target, *, designation=None, status="confirmed"):
    binding = {"status": status, "canonical_target": target}
    if designation is not None:
        binding["designates_object_kind"] = designation
    return {"field_id": field_id, "binding": binding}


def _payload(fields=None, grain=("video_id",)):
    return {
        "grain": list(grain),
        "fields": list(
            fields
            if fields is not None
            else [
                _field("video_id", "video_id", designation=KIND),
                _field("title", "title"),
                _field("duration_seconds", "duration_seconds"),
            ]
        ),
    }


_PLAN = {"executable": True, "grain": ["video_id"]}


# ---------------------------------------------------------------------------
# Which files take this route -- and which never may.
# ---------------------------------------------------------------------------


def test_one_designation_in_the_grain_without_measures_is_a_reference_file():
    route = eri.reference_designation(_payload(), _PLAN)
    assert route is not None
    assert route["field_id"] == "video_id"
    assert route["object_kind"] == KIND
    assert route["key_target"] == "video_id"
    # Attributes keep the mapping's order: the file's own reading order is the
    # only order anybody declared.
    assert route["attribute_targets"] == ["title", "duration_seconds"]


def test_a_mapping_without_any_designation_is_not_a_reference_file():
    payload = _payload(
        [_field("video_id", "video_id"), _field("title", "title")],
    )
    assert eri.reference_designation(payload, _PLAN) is None


def test_two_designations_are_the_facts_shape_never_a_silent_pick():
    payload = _payload(
        [
            _field("video_id", "video_id", designation=KIND),
            _field("store_id", "store_id", designation="store"),
        ],
        grain=("video_id", "store_id"),
    )
    # Two keys is the facts-plus-matching shape (68.3/69). Choosing one here
    # would be the approximate join epic 68 exists to refuse.
    assert eri.reference_designation(payload, _PLAN) is None


def test_a_key_outside_the_grain_describes_facts_about_the_entity():
    payload = _payload(
        [
            _field("date", "date"),
            _field("video_id", "video_id", designation=KIND),
            _field("views", "views"),
        ],
        grain=("date", "video_id"),
    )
    # The grain is (date, video) -- one row per DAY per video. Those are facts
    # about the video, not the video's reference attributes.
    payload["grain"] = ["date", "video_id"]
    route = eri.reference_designation(payload, _PLAN)
    assert route is not None  # the key IS in the grain here...
    # ...but a grain the key is absent from is not a reference file at all.
    payload["grain"] = ["date"]
    assert eri.reference_designation(payload, _PLAN) is None


def test_a_file_carrying_measures_takes_the_warehouse_route():
    plan = {**_PLAN, "additive_measures": ["views"]}
    assert eri.reference_designation(_payload(), plan) is None


def test_an_excluded_designating_column_designates_nothing_landed():
    payload = _payload(
        [
            _field("video_id", "video_id", designation=KIND, status="excluded"),
            _field("title", "title"),
        ]
    )
    assert eri.reference_designation(payload, _PLAN) is None


def test_an_unconfirmed_binding_is_not_an_attribute_of_the_route():
    payload = _payload(
        [
            _field("video_id", "video_id", designation=KIND),
            _field("title", "title"),
            _field("draft_note", "draft_note", status="proposed"),
        ]
    )
    route = eri.reference_designation(payload, _PLAN)
    assert route is not None
    assert route["attribute_targets"] == ["title"]


# ---------------------------------------------------------------------------
# A row that cannot name its entity is rejected WITH evidence.
# ---------------------------------------------------------------------------


def test_a_row_without_its_key_is_rejected_naming_the_field_and_the_rule():
    validate = eri.reference_row_validator(eri.reference_designation(_payload(), _PLAN))
    rejected = validate({"video_id": "   ", "title": "Orphan"}, 7)
    assert rejected is not None
    assert rejected.row_number == 7
    assert rejected.field_name == "video_id"
    assert rejected.rule == eri.RULE_KEY_MISSING
    assert "names no entity key" in rejected.reason


def test_a_missing_key_column_is_rejected_like_an_empty_one():
    validate = eri.reference_row_validator(eri.reference_designation(_payload(), _PLAN))
    assert validate({"title": "Orphan"}, 2).rule == eri.RULE_KEY_MISSING


def test_an_over_long_key_is_rejected_naming_the_store_s_limit():
    validate = eri.reference_row_validator(eri.reference_designation(_payload(), _PLAN))
    rejected = validate({"video_id": "v" * 121}, 3)
    assert rejected.rule == eri.RULE_KEY_TOO_LONG
    assert "120" in rejected.reason


def test_a_row_that_names_its_entity_passes():
    validate = eri.reference_row_validator(eri.reference_designation(_payload(), _PLAN))
    assert validate({"video_id": "v-1", "title": "Ok"}, 1) is None


# ---------------------------------------------------------------------------
# What one row becomes, and what makes two landings the same content.
# ---------------------------------------------------------------------------


def test_the_key_is_stored_under_the_registry_s_declared_canonical_key():
    payload = eri.build_node_payload(
        {"vid": "v-1", "title": "Hello", "duration_seconds": 42},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["title", "duration_seconds"],
    )
    assert payload == {
        "attributes": {"title": "Hello", "duration_seconds": 42, "video_id": "v-1"}
    }


def test_a_column_colliding_with_the_canonical_key_never_overwrites_the_key():
    payload = eri.build_node_payload(
        {"vid": "v-1", "video_id": "SOMETHING ELSE"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["video_id"],
    )
    # Two answers to "what identifies this entity" is one answer too many.
    assert payload["attributes"]["video_id"] == "v-1"


def test_the_reserved_grain_column_is_not_an_attribute():
    payload = eri.build_node_payload(
        {"vid": "v-1", "grain_key": "v-1", "title": "Hello"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["grain_key", "title"],
    )
    assert "grain_key" not in payload["attributes"]


def test_identical_attributes_hash_identically_whatever_the_column_order():
    first = eri.build_node_payload(
        {"vid": "v-1", "title": "Hello", "lang": "fr"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["title", "lang"],
    )
    second = eri.build_node_payload(
        {"vid": "v-1", "lang": "fr", "title": "Hello"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["lang", "title"],
    )
    assert eri.version_digest("mdnode_x", first) == eri.version_digest("mdnode_x", second)


def test_a_changed_attribute_changes_the_digest():
    before = eri.build_node_payload(
        {"vid": "v-1", "title": "Hello"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["title"],
    )
    after = eri.build_node_payload(
        {"vid": "v-1", "title": "Bonjour"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["title"],
    )
    assert eri.version_digest("mdnode_x", before) != eri.version_digest("mdnode_x", after)


def test_the_digest_is_per_node_two_entities_never_share_one():
    payload = eri.build_node_payload(
        {"vid": "v-1", "title": "Hello"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["title"],
    )
    assert eri.version_digest("mdnode_a", payload) != eri.version_digest("mdnode_b", payload)


# ---------------------------------------------------------------------------
# Le schema de proprietes que le fichier declare (story 69.5).
# ---------------------------------------------------------------------------


def test_the_property_schema_comes_from_the_mapping_never_from_the_values():
    payload = _payload(
        [
            _field("video_id", "video_id", designation=KIND),
            _field("title", "title"),
            _field("duration_seconds", "duration_seconds"),
        ]
    )
    # Le mapping DECLARE les types ; deviner depuis la premiere valeur non nulle
    # marcherait jusqu'au fichier dont la premiere ligne est vide.
    payload["fields"][2]["physical_type"] = "integer"
    route = eri.reference_designation(payload, _PLAN)
    schema = eri.property_schema(route, canonical_key="video_id")
    assert schema == {
        "type": "object",
        "properties": {
            "video_id": {"type": "string"},
            "title": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
    }


def test_an_unknown_physical_type_becomes_a_string_rather_than_a_refusal():
    payload = _payload([_field("video_id", "video_id", designation=KIND), _field("note", "note")])
    payload["fields"][1]["physical_type"] = "quelquechose"
    schema = eri.property_schema(
        eri.reference_designation(payload, _PLAN), canonical_key="video_id"
    )
    assert schema["properties"]["note"] == {"type": "string"}


def test_the_reserved_grain_column_is_not_a_declared_property():
    route = {"attribute_targets": ["grain_key", "title"], "attribute_types": {}}
    schema = eri.property_schema(route, canonical_key="video_id")
    assert "grain_key" not in schema["properties"]


def test_the_digest_pins_the_type_version_so_a_re_import_stays_a_no_op():
    payload = eri.build_node_payload(
        {"vid": "v-1", "title": "Hello"},
        key_target="vid",
        canonical_key="video_id",
        attribute_targets=["title"],
    )
    # Le digest DOIT inclure la version de type : la version stockee la porte,
    # et un digest calcule sans elle differerait sur CHAQUE entite -- le no-op
    # par entite minterait un doublon a chaque re-import.
    assert eri.version_digest("mdnode_x", payload) != eri.version_digest(
        "mdnode_x", payload, type_version_id="mdtv_1"
    )
    assert eri.version_digest(
        "mdnode_x", payload, type_version_id="mdtv_1"
    ) == eri.version_digest("mdnode_x", payload, type_version_id="mdtv_1")
