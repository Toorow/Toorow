"""Country's governed model: vocabulary, presets and the resolved projection.

The lifecycle these build on is proven in ``test_master_data.py``; what is
proven here is the Country meaning laid on top of it -- and specifically the two
distinctions Story 48.2 exists to restore: an official ISO assignment is not a
retained extension, and an unresolved value is not an ungrouped country.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest
from core.country_registry import (
    ASSIGNMENT_EXTENSION,
    ASSIGNMENT_OFFICIAL,
    BUCKET_ASSIGNED,
    BUCKET_REST_OF_WORLD,
    BUCKET_UNKNOWN,
    COUNTRY_PRESETS,
    DEFAULT_REST_OF_WORLD_LABEL,
    MARKET,
    REGION,
    REST_OF_WORLD,
    USER_ASSIGNED_EXTENSIONS,
    CountryRegistryError,
    build_projection,
    preset_selection,
    render_seed_csv,
    rest_of_world_payload,
    seed_provenance,
    vocabulary_entries,
)
from core.country_vocabulary import Country, get_country_vocabulary
from core.master_data import Membership, content_hash

FRANCE, DACH, EMEA, ROW = "mdnode_FR", "mdnode_DA", "mdnode_EM", "mdnode_RW"

NODES = [
    {"id": FRANCE, "label": "France", "node_kind": MARKET},
    {"id": DACH, "label": "DACH", "node_kind": MARKET},
    {"id": EMEA, "label": "EMEA", "node_kind": REGION},
    {"id": ROW, "label": DEFAULT_REST_OF_WORLD_LABEL, "node_kind": REST_OF_WORLD},
]
VALUES = ("FR", "RE", "DE", "AT", "CH", "BR", "JP")


def _projection(*, memberships=None, rest_of_world=None, as_of=date(2026, 7, 30)):
    edges = memberships if memberships is not None else [
        Membership(parent_node_id=FRANCE, child_value="FR"),
        Membership(parent_node_id=FRANCE, child_value="RE"),
        Membership(parent_node_id=DACH, child_value="DE"),
        Membership(parent_node_id=EMEA, child_node_id=FRANCE),
        Membership(parent_node_id=EMEA, child_node_id=DACH),
    ]
    return build_projection(
        hierarchy_version_id="mdver_1",
        vocabulary_version_id="mdvoc_1",
        registry_id="mdreg_1",
        memberships=edges,
        nodes=NODES,
        canonical_values=VALUES,
        rest_of_world=rest_of_world
        if rest_of_world is not None
        else rest_of_world_payload(node_id=ROW),
        as_of=as_of,
    )


# ---------------------------------------------------------------------------
# AC2 -- the vocabulary is complete, versioned and honest about assignment.
# ---------------------------------------------------------------------------


def test_the_vocabulary_covers_the_whole_canonical_country_set() -> None:
    entries = vocabulary_entries()
    assert len(entries) == len(get_country_vocabulary())
    assert len({entry["code"] for entry in entries}) == len(entries)


def test_a_retained_extension_is_labelled_and_never_presented_as_iso() -> None:
    by_code = {entry["code"]: entry for entry in vocabulary_entries()}
    for code, reason in USER_ASSIGNED_EXTENSIONS.items():
        assert by_code[code]["assignment"] == ASSIGNMENT_EXTENSION
        assert by_code[code]["assignment_note"] == reason
    assert by_code["FR"]["assignment"] == ASSIGNMENT_OFFICIAL
    assert "assignment_note" not in by_code["FR"]


def test_vocabulary_entries_are_deterministic() -> None:
    first = vocabulary_entries()
    second = vocabulary_entries()
    assert content_hash(first) == content_hash(second)
    assert [entry["code"] for entry in first] == sorted(entry["code"] for entry in first)


def test_aliases_are_deduplicated_so_the_hash_cannot_drift_on_reorder() -> None:
    noisy = [Country(code="FR", display_name="France", aliases=("FR", "France", "FR"))]
    tidy = [Country(code="FR", display_name="France", aliases=("France", "FR"))]
    assert content_hash(vocabulary_entries(noisy)) == content_hash(vocabulary_entries(tidy))


def test_the_seed_projection_keeps_the_columns_the_macro_reads_and_adds_assignment() -> None:
    rendered = render_seed_csv(vocabulary_entries())
    rows = list(csv.DictReader(io.StringIO(rendered)))
    assert list(rows[0]) == ["iso_code", "display_name", "aliases", "assignment", "status"]
    by_code = {row["iso_code"]: row for row in rows}
    assert "|" in by_code["FR"]["aliases"]
    for code in USER_ASSIGNED_EXTENSIONS:
        assert by_code[code]["assignment"] == ASSIGNMENT_EXTENSION


def test_the_seed_projection_is_byte_identical_across_runs() -> None:
    entries = vocabulary_entries()
    assert render_seed_csv(entries) == render_seed_csv(list(reversed(entries)))


def test_seed_provenance_pins_the_exact_governed_version() -> None:
    pin = seed_provenance(
        {
            "id": "mdvoc_X",
            "vocabulary_key": "country",
            "content_hash": "a" * 64,
            "source_authority": "ISO 3166-1 alpha-2",
            "source_version": "2026-07",
            "source_reference": "https://example.com",
            "effective_date": date(2026, 7, 30),
            "entry_count": 250,
        }
    )
    assert pin["vocabulary_version_id"] == "mdvoc_X"
    assert pin["content_hash"] == "a" * 64
    assert pin["entry_count"] == 250


# ---------------------------------------------------------------------------
# AC4 -- presets accelerate setup without becoming authority.
# ---------------------------------------------------------------------------


def test_every_preset_states_its_provenance_and_classification() -> None:
    for preset in COUNTRY_PRESETS:
        assert preset.classification in {"standard_derived", "toorow_curated"}
        assert preset.source_authority.strip()
        assert preset.description.strip()
        assert preset.nodes, preset.preset_key


def test_a_commercial_grouping_is_never_labelled_as_a_standard() -> None:
    by_key = {preset.preset_key: preset for preset in COUNTRY_PRESETS}
    assert by_key["emea-apac-amer"].classification == "toorow_curated"
    assert by_key["un-m49-subregions-europe"].classification == "standard_derived"
    assert "UN Statistics" in by_key["un-m49-subregions-europe"].source_authority


def test_the_france_starter_keeps_fr_explicit_and_offers_territories() -> None:
    france = next(p for p in COUNTRY_PRESETS if p.preset_key == "france-and-territories")
    members = {member.value: member for member in france.members}
    assert members["FR"].optional is False
    for territory in ("RE", "GF"):
        assert members[territory].optional is True
        assert members[territory].default_selected is False


def test_a_preset_attaches_nothing_optional_unless_it_is_selected() -> None:
    france = next(p for p in COUNTRY_PRESETS if p.preset_key == "france-and-territories")
    payload = france.payload()
    assert preset_selection(payload, None) == ["FR"]
    assert preset_selection(payload, {"RE"}) == ["FR", "RE"]
    assert preset_selection(payload, set()) == ["FR"]


def test_preset_payloads_are_explicit_about_every_member() -> None:
    for preset in COUNTRY_PRESETS:
        payload = preset.payload()
        node_keys = {node["key"] for node in payload["nodes"]}
        for member in payload["members"]:
            assert member["parent_key"] in node_keys, preset.preset_key
            assert member["value"]


# ---------------------------------------------------------------------------
# AC5 -- Rest of World is configurable; Unknown is never hidden inside it.
# ---------------------------------------------------------------------------


def test_an_assigned_country_resolves_to_its_market_and_region() -> None:
    bucket = _projection().bucket_for("FR")
    assert bucket["geography_bucket_kind"] == BUCKET_ASSIGNED
    assert bucket["market_id"] == FRANCE
    assert bucket["market_label"] == "France"
    assert bucket["region_id"] == EMEA
    assert bucket["region_label"] == "EMEA"
    assert bucket["geography_hierarchy_version_id"] == "mdver_1"


def test_a_territory_reports_through_its_market_while_keeping_its_own_code() -> None:
    bucket = _projection().bucket_for("RE")
    assert bucket["country_id"] == "RE"
    assert bucket["market_id"] == FRANCE
    assert bucket["region_id"] == EMEA


def test_a_valid_ungrouped_country_is_rest_of_world_not_unknown() -> None:
    bucket = _projection().bucket_for("BR")
    assert bucket["geography_bucket_kind"] == BUCKET_REST_OF_WORLD
    assert bucket["country_id"] == "BR"
    assert bucket["market_id"] == ROW


def test_a_value_outside_the_vocabulary_is_unknown_and_keeps_no_country_identity() -> None:
    bucket = _projection().bucket_for("ZZ")
    assert bucket["geography_bucket_kind"] == BUCKET_UNKNOWN
    assert bucket["country_id"] is None
    assert bucket["market_id"] is None


def test_an_absent_value_is_unknown() -> None:
    assert _projection().bucket_for(None)["geography_bucket_kind"] == BUCKET_UNKNOWN


def test_rest_of_world_drills_to_the_exact_country_rows_it_stands_for() -> None:
    members = _projection().rest_of_world_members()
    assert members == ("AT", "BR", "CH", "JP")
    assert "FR" not in members and "DE" not in members


def test_rest_of_world_is_renameable_and_carries_its_drill_policy() -> None:
    projection = _projection(
        rest_of_world=rest_of_world_payload(
            node_id=ROW, label="Everywhere else", drill="aggregate"
        )
    )
    assert projection.rest_of_world_label == "Everywhere else"
    assert projection.rest_of_world_drill == "aggregate"
    assert projection.bucket_for("BR")["market_label"] == "Everywhere else"


def test_an_unknown_drill_policy_is_rejected() -> None:
    with pytest.raises(CountryRegistryError):
        rest_of_world_payload(node_id=ROW, drill="whatever")


def test_rest_of_world_is_never_bindable_but_markets_are() -> None:
    descriptors = {item["id"]: item for item in _projection().descriptors()}
    assert descriptors[FRANCE]["bindable"] is True
    assert descriptors[ROW]["bindable"] is False


def test_assigned_markets_plus_rest_of_world_plus_unknown_cover_every_value() -> None:
    """The additive reconciliation of AC5, stated over the vocabulary itself."""

    projection = _projection()
    assigned, residual = set(), set()
    for value in VALUES:
        kind = projection.bucket_for(value)["geography_bucket_kind"]
        (assigned if kind == BUCKET_ASSIGNED else residual).add(value)
    assert assigned | residual == set(VALUES)
    assert assigned & residual == set()
    assert residual == set(projection.rest_of_world_members())


# ---------------------------------------------------------------------------
# AC3/AC7 -- meaning is pinned to a version and a date.
# ---------------------------------------------------------------------------


def test_membership_effective_dates_change_the_answer_not_the_facts() -> None:
    edges = [
        Membership(parent_node_id=FRANCE, child_value="FR", effective_to=date(2026, 7, 1)),
        Membership(parent_node_id=DACH, child_value="FR", effective_from=date(2026, 7, 1)),
    ]
    before = _projection(memberships=edges, as_of=date(2026, 6, 1)).bucket_for("FR")
    after = _projection(memberships=edges, as_of=date(2026, 8, 1)).bucket_for("FR")
    assert before["market_id"] == FRANCE
    assert after["market_id"] == DACH
    assert before["country_id"] == after["country_id"] == "FR"


def test_a_market_without_a_region_reports_no_region_rather_than_guessing() -> None:
    bucket = _projection(
        memberships=[Membership(parent_node_id=DACH, child_value="DE")]
    ).bucket_for("DE")
    assert bucket["market_id"] == DACH
    assert bucket["region_id"] is None


def test_a_market_nested_under_a_market_still_resolves_the_region_above() -> None:
    sub = "mdnode_SUB"
    nodes = [*NODES, {"id": sub, "label": "France South", "node_kind": MARKET}]
    projection = build_projection(
        hierarchy_version_id="mdver_1",
        vocabulary_version_id="mdvoc_1",
        registry_id="mdreg_1",
        memberships=[
            Membership(parent_node_id=sub, child_value="FR"),
            Membership(parent_node_id=FRANCE, child_node_id=sub),
            Membership(parent_node_id=EMEA, child_node_id=FRANCE),
        ],
        nodes=nodes,
        canonical_values=VALUES,
        rest_of_world=rest_of_world_payload(node_id=ROW),
        as_of=date(2026, 7, 30),
    )
    bucket = projection.bucket_for("FR")
    assert bucket["market_id"] == sub
    assert bucket["region_id"] == EMEA

def test_loading_a_pinned_version_uses_its_immutable_node_labels(monkeypatch) -> None:
    import core.country_registry as registry

    monkeypatch.setattr(
        registry,
        "fetch_country_registry",
        lambda *_args, **_kwargs: {"id": "mdreg_1", "current_version_id": "mdver_new"},
    )
    monkeypatch.setattr(
        registry,
        "require_version",
        lambda *_args, **_kwargs: {
            "id": "mdver_old",
            "vocabulary_version_id": "mdvoc_1",
            "payload": {
                "node_labels": {FRANCE: "France historique"},
                "rest_of_world": rest_of_world_payload(node_id=ROW),
            },
        },
    )
    monkeypatch.setattr(
        registry,
        "fetch_vocabulary_version",
        lambda *_args, **_kwargs: {"entries": [{"code": "FR"}, {"code": "DE"}]},
    )
    monkeypatch.setattr(registry, "list_nodes", lambda *_args, **_kwargs: NODES)
    monkeypatch.setattr(
        registry,
        "fetch_memberships",
        lambda *_args, **_kwargs: (Membership(parent_node_id=FRANCE, child_value="FR"),),
    )

    projection = registry.load_projection(
        object(), project_id="project", version_id="mdver_old"
    )

    assert projection is not None
    assert projection.hierarchy_version_id == "mdver_old"
    assert projection.bucket_for("FR")["market_label"] == "France historique"
    assert DACH not in {item["id"] for item in projection.descriptors()}
