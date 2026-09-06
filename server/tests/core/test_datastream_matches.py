"""How a match is composed, ranked and told apart from a guess (story 66.2).

Pure: composition and ranking take dictionaries, so the properties that matter --
the order is deterministic, a candidate is never executable, three states are
reported separately -- are proven without a database. The reads are proven on real
Postgres in `server/tests/integration/test_datastream_matches_pg.py`.
"""

from __future__ import annotations

import random

from core.datastream_matches import (
    MAX_MATCHES,
    _candidate_key_missing,
    _governed_match,
    _pair_matches,
    rank,
    rank_key,
)


def _side(name, bindings, measures=0, fields=None, measure_fields=None, types=None):
    return {
        "id": f"ds_{name}",
        "name": name,
        "mapping_version_id": f"dmv_{name}",
        "published_execution_id": f"exec_{name}",
        "output_version_id": f"dsov_{name}",
        "mapping_version_number": 1,
        "bindings": bindings,
        # The declared physical type per component. Defaults to `date`/`string`
        # per the canonical value type, which is what a real mapping carries and
        # what makes the temporal gate silent on a well-formed pair.
        "binding_types": types or {
            "mdm_day": "date",
            "mdm_campaign": "string",
        },
        "physical_fields": fields or {physical: True for physical in bindings.values()},
        "measures": measure_fields or [],
        "measure_count": measures,
    }


DAY = {"ordinal": 0, "canonical_field_id": "mdm_day", "canonical_name": "day",
       "value_type": "date"}
CAMPAIGN = {"ordinal": 1, "canonical_field_id": "mdm_campaign",
            "canonical_name": "campaign_id", "value_type": "string"}


def _key(name="Day and Campaign", components=(DAY, CAMPAIGN), version_id="mckv_1"):
    return {
        "key_id": "mck_1",
        "key_name": name,
        "key_version_id": version_id,
        "key_version_number": 1,
        "components": list(components),
        "content_hash": "0" * 64,
    }


def _relationship(cardinality="many_to_one"):
    return {
        "relationship_name": "spend_to_conversions",
        "from_dataset": "spend",
        "to_dataset": "conversions",
        "cardinality": cardinality,
        "fan_out_policy": "forbid",
        "bridge_dataset": None,
        "view_version_id": "svv_1",
        "view_id": "sv_1",
        "view_version_number": 3,
        "view_name": "media_performance",
        "left_datastream_id": "ds_Campaign spend",
        "right_datastream_id": "ds_Conversions",
    }


LEFT = _side("Campaign spend", {"mdm_day": "date", "mdm_campaign": "campaign"}, measures=4)
RIGHT = _side("Conversions", {"mdm_day": "event_date", "mdm_campaign": "campaign_key"}, measures=2)


# ---------------------------------------------------------------------------
# A governed match carries everything a person needs to decide
# ---------------------------------------------------------------------------


def test_a_governed_match_names_both_physical_paths_component_by_component():
    match = _governed_match(LEFT, RIGHT, _key(), _relationship())
    paths = {p["canonical_name"]: (p["left_field"], p["right_field"]) for p in match["key_paths"]}
    assert paths == {"day": ("date", "event_date"), "campaign_id": ("campaign", "campaign_key")}


def test_the_three_states_are_reported_separately():
    """One badge merging them would let a governed path with no rows read as ready."""
    match = _governed_match(LEFT, RIGHT, _key(), _relationship())
    assert match["authority"] == "governed"
    assert match["observed_coverage"] == "unavailable"
    assert match["execution_safety"] == "ready"


def test_a_many_to_many_relationship_is_unsafe_and_names_the_executors_refusal():
    """CHANGED 2026-08-16, from `review_required`, and the reason is the honesty.

    `review_required` promises that a human decision unblocks it. No human
    decision unblocks a many-to-many: `multi_source_plan` refuses it outright
    with `many_to_many_not_supported`, so an approval would end at the same
    refusal one click later. `unsafe` is what is true, and the refusal travels
    beside it so the screen names the missing thing (a governed bridge execution
    path) rather than showing a badge nobody can act on.
    """
    match = _governed_match(LEFT, RIGHT, _key(), _relationship("many_to_many"))
    assert match["execution_safety"] == "unsafe"
    assert match["execution_blocked"]["code"] == "many_to_many_not_supported"


def test_a_deduplicating_relationship_is_never_advertised_ready():
    """THE DEFECT THIS REPAIR EXISTS FOR, measured 2026-08-16.

    `many_to_one` is a safe cardinality, so `_safety` answered `ready` while
    `compile_plan` refused the very same relationship with
    `deduplication_not_supported`. Discovery promised what compilation refused.
    """
    relationship = {**_relationship("many_to_one"), "fan_out_policy": "deduplicate"}
    match = _governed_match(LEFT, RIGHT, _key(), relationship)
    assert match["execution_safety"] == "unsafe"
    assert match["execution_blocked"]["code"] == "deduplication_not_supported"


def test_a_bridged_relationship_is_never_advertised_ready():
    """Same leak, second door: the bridge is named by the policy OR by the dataset."""
    by_policy = {**_relationship("many_to_one"), "fan_out_policy": "bridge"}
    by_dataset = {**_relationship("many_to_one"), "bridge_dataset": "bridge_campaigns"}
    for relationship in (by_policy, by_dataset):
        match = _governed_match(LEFT, RIGHT, _key(), relationship)
        assert match["execution_safety"] == "unsafe"
        assert match["execution_blocked"]["code"] == "bridge_execution_not_supported"


def test_an_executable_relationship_carries_no_refusal_rather_than_an_empty_one():
    """`null` means "the executor has no objection", never "we did not look"."""
    match = _governed_match(LEFT, RIGHT, _key(), _relationship())
    assert match["execution_blocked"] is None


def test_discovery_and_compilation_read_the_same_rule():
    """The two doors, proven to be ONE implementation.

    A copy of the rule in the discovery module is exactly how the two came to
    disagree; this asserts the import, not a duplicated table of codes.
    """
    from core.datastream_matches import unsupported_relationship as discovery_rule
    from core.multi_source_plan import unsupported_relationship as executor_rule

    assert discovery_rule is executor_rule


def test_the_handoff_pins_the_exact_versions_that_were_read():
    """A handoff that re-resolved a head would open a different question."""
    match = _governed_match(LEFT, RIGHT, _key(), _relationship())
    handoff = match["explore_together"]
    assert [d["mapping_version_id"] for d in handoff["datastreams"]] == [
        "dmv_Campaign spend",
        "dmv_Conversions",
    ]
    assert handoff["common_key_version_id"] == "mckv_1"
    assert handoff["view_version_id"] == "svv_1"
    assert "latest" not in str(handoff)


def test_each_side_exposes_the_exact_governed_measures_the_explorer_can_select():
    left = _side(
        "Spend",
        {"mdm_day": "date", "mdm_spend": "spend_micros"},
        measures=1,
        measure_fields=[
            {"canonical_field_id": "mdm_spend", "name": "spend_micros", "aggregation": "sum"}
        ],
    )
    match = _governed_match(left, RIGHT, _key(components=(DAY,)), _relationship())
    assert match["left"]["measures"] == [
        {"canonical_field_id": "mdm_spend", "name": "spend_micros", "aggregation": "sum"}
    ]


def test_the_analysis_sentence_names_the_sources_and_the_axis():
    match = _governed_match(LEFT, RIGHT, _key(), _relationship())
    assert match["analysis"] == "Campaign spend and Conversions, by day and campaign_id."


# ---------------------------------------------------------------------------
# A candidate is never executable, and says which gesture repairs it
# ---------------------------------------------------------------------------


def test_a_key_without_an_approved_relationship_is_a_candidate_not_a_match():
    matches = _pair_matches(LEFT, RIGHT, [_key()], {}, {})
    assert len(matches) == 1
    candidate = matches[0]
    assert candidate["kind"] == "candidate_key_missing"
    assert candidate["authority"] == "needs_governance"
    assert candidate["explore_together"] is None


def test_a_candidate_names_the_governing_gesture_and_no_table():
    candidate = _candidate_key_missing(LEFT, RIGHT, [DAY, CAMPAIGN])
    action = candidate["next_action"]
    assert "Declare a common key" in action
    for forbidden in ("mdm_common_key", "app.", "table", "migration", "NULL"):
        assert forbidden not in action, action


def test_an_existing_common_key_points_to_relationship_approval_not_key_creation():
    candidate = _candidate_key_missing(LEFT, RIGHT, [DAY, CAMPAIGN], key=_key())
    action = candidate["next_action"]
    assert "Approve one exact relationship" in action
    assert "Declare a common key" not in action


def test_same_named_columns_without_a_binding_are_the_other_candidate_kind():
    left = _side("A", {}, fields={"campaign_id": False, "spend": False})
    right = _side("B", {}, fields={"campaign_id": False, "clicks": False})
    matches = _pair_matches(left, right, [], {}, {})
    assert [m["kind"] for m in matches] == ["candidate_binding_missing"]
    assert "not an identity" in matches[0]["next_action"]


def test_two_sources_with_nothing_in_common_produce_nothing():
    left = _side("A", {}, fields={"spend": False})
    right = _side("B", {}, fields={"clicks": False})
    assert _pair_matches(left, right, [], {}, {}) == []


def test_a_governed_path_wins_over_the_candidate_for_the_same_pair():
    """When a relationship exists, the pair is answered once -- as governed."""
    pinned = {"mckv_1": [_relationship()]}
    matches = _pair_matches(LEFT, RIGHT, [_key()], pinned, {})
    assert [m["kind"] for m in matches] == ["governed"]


def test_a_candidate_uses_the_canonical_names_not_the_raw_ids():
    names = {"mdm_day": "day", "mdm_campaign": "campaign_id"}
    matches = _pair_matches(LEFT, RIGHT, [], {}, names)
    assert matches[0]["analysis"].endswith("by campaign_id and day.")


# ---------------------------------------------------------------------------
# Ranking is a tuple, not a score
# ---------------------------------------------------------------------------


def _match(authority="governed", measures=0, components=2, key="mckv", left="a", right="b"):
    return {
        "authority": authority,
        "unlocked_measures": measures,
        "common_key": {"version_id": key, "components": [DAY] * components},
        "left": {"datastream_id": left},
        "right": {"datastream_id": right},
    }


def test_governed_matches_rank_before_candidates_whatever_their_size():
    ordered = rank([_match("needs_governance", measures=99), _match("governed", measures=1)])
    assert [m["authority"] for m in ordered] == ["governed", "needs_governance"]


def test_more_unlocked_measures_ranks_higher():
    ordered = rank([_match(measures=2, key="b"), _match(measures=7, key="a")])
    assert [m["unlocked_measures"] for m in ordered] == [7, 2]


def test_a_shorter_key_ranks_before_a_longer_one_at_equal_value():
    ordered = rank([_match(components=3, key="b"), _match(components=1, key="a")])
    assert [len(m["common_key"]["components"]) for m in ordered] == [1, 3]


def test_the_order_does_not_depend_on_the_order_of_discovery():
    """Shuffled input, identical output. This is what 'deterministic' has to mean."""
    matches = [
        _match(measures=5, key="k1", left="l1", right="r1"),
        _match(measures=5, key="k2", left="l2", right="r2"),
        _match("needs_governance", measures=9, key="k3", left="l3", right="r3"),
        _match(measures=1, key="k4", left="l4", right="r4"),
    ]
    reference = [rank_key(m) for m in rank(matches)]
    for seed in range(5):
        shuffled = list(matches)
        random.Random(seed).shuffle(shuffled)
        assert [rank_key(m) for m in rank(shuffled)] == reference


def test_the_returned_bound_is_a_real_number_not_a_promise():
    assert MAX_MATCHES == 50


def test_orienting_a_reversed_relationship_turns_EVERYTHING_that_has_a_direction():
    """MESURE DU 2026-08-16 : la charge se contredisait elle-meme.

    Cette branche retournait les deux Datastreams et la cardinalite, et laissait
    `from_dataset` / `to_dataset` dans le sens AUTEUR. Un lecteur qui prenait
    `from_dataset` a cote de `left_datastream_id` lisait deux directions opposees
    dans le meme objet.
    """
    authored = {
        **_relationship("many_to_one"),
        "left_datastream_id": "ds_Conversions",   # l inverse de LEFT
        "right_datastream_id": "ds_Campaign spend",
        "from_dataset": "conversions_daily",
        "to_dataset": "campaign_spend_daily",
    }
    match = _governed_match(LEFT, RIGHT, _key(), authored)
    oriented = match["relationship"]

    assert oriented["left_datastream_id"] == "ds_Campaign spend"
    assert oriented["right_datastream_id"] == "ds_Conversions"
    # Les deux noms suivent, sinon ils designent l autre bout.
    assert oriented["from_dataset"] == "campaign_spend_daily"
    assert oriented["to_dataset"] == "conversions_daily"
    assert oriented["cardinality"] == "one_to_many"
    # Et le fait d avoir ete retournee se dit, avec le mot du compilateur : c est
    # ce qui permet de retrouver la relation dans la Semantic View, ou elle est
    # ecrite dans l autre sens.
    assert oriented["authored_direction_reversed"] is True


def test_a_relationship_read_in_its_authored_direction_is_untouched():
    authored = {
        **_relationship("many_to_one"),
        "from_dataset": "campaign_spend_daily",
        "to_dataset": "conversions_daily",
    }
    oriented = _governed_match(LEFT, RIGHT, _key(), authored)["relationship"]

    assert oriented["from_dataset"] == "campaign_spend_daily"
    assert oriented["to_dataset"] == "conversions_daily"
    assert oriented["cardinality"] == "many_to_one"
    # `False` et non ABSENT : un lecteur ne doit pas avoir a deduire d une cle
    # manquante que la direction est celle de l auteur.
    assert oriented["authored_direction_reversed"] is False


# ---------------------------------------------------------------------------
# The response describes its own size, and stays inside its budget
# ---------------------------------------------------------------------------


def test_the_response_states_its_own_byte_size_exactly():
    """THE FIXED POINT, and the reason it has to be one.

    The number is part of the payload, so writing it makes the payload bigger,
    so the number becomes wrong. It converges because the size only grows and
    each turn adds at most a digit.

    Held here because until 2026-08-16 THREE nested loops shared this job, the
    first of them measuring a payload that did not yet carry `response_bytes` --
    it trimmed against a size the response would never have.
    """
    from core.datastream_matches import _canonical_bytes, _settle_response_bytes

    response = {"matches": [], "bounds": {"truncated": False}, "counts": {"returned": 0}}
    _settle_response_bytes(response)

    assert response["bounds"]["response_bytes"] == len(_canonical_bytes(response))


def test_settling_is_idempotent_so_a_second_pass_moves_nothing():
    from core.datastream_matches import _settle_response_bytes

    response = {"matches": [{"a": "x" * 500}], "bounds": {}, "counts": {}}
    _settle_response_bytes(response)
    first = response["bounds"]["response_bytes"]
    _settle_response_bytes(response)

    assert response["bounds"]["response_bytes"] == first


# ---------------------------------------------------------------------------
# The catalogue refuses what the compiler will refuse (2026-08-22).
#
# The temporal gate lived only in `_compile_edge`, so this catalogue advertised a
# day-against-instant cross as a viable candidate: a person opened it, measured
# it, pressed Run, and met `false_day_equivalence` one click later. That is the
# exact sequence the 2026-08-16 repair exists to prevent, arriving through the
# gate that repair did not cover.
# ---------------------------------------------------------------------------


def test_a_day_against_an_instant_is_unsafe_in_the_catalogue_too():
    instant = _side(
        "Conversions",
        {"mdm_day": "event_ts", "mdm_campaign": "campaign_key"},
        measures=2,
        types={"mdm_day": "timestamp", "mdm_campaign": "string"},
    )
    match = _governed_match(LEFT, instant, _key(), _relationship())

    assert match["execution_safety"] == "unsafe"
    assert match["execution_blocked"]["code"] == "false_day_equivalence"
    # The sentence names a gesture that EXISTS. There is no timestamp-to-day
    # treatment in the mapping contract, so "project it to a day in its mapping"
    # sent a person looking for a control the product does not have.
    message = match["execution_blocked"]["message"]
    assert "midnight" in message
    # DES DENTS, cette fois. L'assertion precedente cherchait « map », qui etait
    # deja VRAI de l'ancienne phrase -- « project the column to a day in its
    # MAPping ». Elle gardait donc la reparation qu'elle etait censee prouver
    # sans pouvoir la distinguer du defaut. Ce qui est neuf est le geste :
    # l'ancienne phrase envoyait vers un traitement timestamp->jour qui n'existe
    # pas dans le contrat de mapping.
    assert "project" not in message.lower()
    assert "a component both sources stamp the same way" in message


def test_the_catalogue_and_the_compiler_read_the_same_temporal_rule():
    """One predicate, asked twice — never two copies free to disagree."""
    from core.multi_source_plan import incompatible_key_time

    instant = _side(
        "Conversions",
        {"mdm_day": "event_ts", "mdm_campaign": "campaign_key"},
        types={"mdm_day": "timestamp", "mdm_campaign": "string"},
    )
    catalogue = _governed_match(LEFT, instant, _key(), _relationship())
    compiler = incompatible_key_time(
        component=DAY,
        left_name="Campaign spend",
        right_name="Conversions",
        left_binding={"physical_type": "date"},
        right_binding={"physical_type": "timestamp"},
    )

    assert catalogue["execution_blocked"] == {"code": compiler[0], "message": compiler[1]}


def test_a_well_typed_pair_is_untouched_by_the_gate():
    """The gate is a refusal, not a tax."""
    match = _governed_match(LEFT, RIGHT, _key(), _relationship())
    assert match["execution_blocked"] is None
    assert match["execution_safety"] == "ready"
    day = next(p for p in match["key_paths"] if p["canonical_name"] == "day")
    assert day["left_physical_type"] == "date"
    assert day["right_physical_type"] == "date"


def test_every_branch_states_execution_blocked_rather_than_omitting_it():
    """`undefined` is the third answer the contract exists to forbid.

    `explorerClient.ts` declares `ExecutionBlock | null`, and the governed
    branch's own comment promises `null` means "the executor has no objection,
    never we did not look". Three of the four branches omitted the key.
    """
    candidate = _candidate_key_missing(LEFT, RIGHT, [DAY, CAMPAIGN])
    assert "execution_blocked" in candidate
    assert candidate["execution_blocked"] is None


def test_a_candidate_never_offers_a_gesture_that_leads_to_a_refusal():
    """LA MEME QUESTION, UNE PORTE PLUS LOIN.

    `_governed_match` et `_ambiguous_safety` interrogeaient le refus temporel ;
    la branche candidate, qui est celle qui dit « approuvez une relation », ne
    demandait rien. Mesure : meme paire jour-contre-instant -> `review_required`,
    `execution_blocked: None`, et le geste propose menait tout droit a
    `false_day_equivalence`.
    """
    instant = _side(
        "Conversions",
        {"mdm_day": "event_ts", "mdm_campaign": "campaign_key"},
        types={"mdm_day": "timestamp", "mdm_campaign": "string"},
    )
    candidate = _candidate_key_missing(LEFT, instant, [DAY, CAMPAIGN], key=_key())

    assert candidate["execution_safety"] == "unsafe"
    assert candidate["execution_blocked"]["code"] == "false_day_equivalence"
    # Le geste suit le verdict : proposer d'approuver enverrait depenser une
    # decision pour rien.
    assert "Approve one exact relationship" not in candidate["next_action"]
    assert "midnight" in candidate["next_action"]


def test_a_well_typed_candidate_still_asks_for_its_relationship():
    """Le garde est un refus, pas une taxe : le candidat sain garde son geste."""
    candidate = _candidate_key_missing(LEFT, RIGHT, [DAY, CAMPAIGN], key=_key())
    assert candidate["execution_safety"] == "review_required"
    assert candidate["execution_blocked"] is None
    assert "Approve one exact relationship" in candidate["next_action"]


def test_the_ambiguous_badge_and_its_gesture_say_the_same_thing():
    """Le badge disait << aucun choix ne mene nulle part >>, le geste
    << choisissez >>. Les deux dans le meme objet."""
    from core.datastream_matches import _ambiguous_safety

    blocked = _relationship("many_to_many")
    verdict = _ambiguous_safety(LEFT, RIGHT, [DAY, CAMPAIGN], [blocked, blocked])
    assert verdict["execution_safety"] == "unsafe"
    assert verdict["execution_blocked"]["code"]

    # Et quand UN seul des candidats est refuse, choisir sert encore.
    mixed = _ambiguous_safety(LEFT, RIGHT, [DAY, CAMPAIGN], [blocked, _relationship()])
    assert mixed["execution_safety"] == "review_required"
    assert mixed["execution_blocked"] is None


def test_the_other_candidate_branch_states_execution_blocked_too():
    """`test_every_branch_...` n'en couvrait qu'une malgre son nom."""
    from core.datastream_matches import _candidate_binding_missing

    entry = _candidate_binding_missing(LEFT, RIGHT, ["campaign_id"])
    assert "execution_blocked" in entry
    assert entry["execution_blocked"] is None
