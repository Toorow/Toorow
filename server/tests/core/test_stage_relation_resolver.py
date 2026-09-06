"""The pair of relations a Datastream is read from -- story 58.3, step 1.

WHAT THIS FILE HOLDS. `Collected` and `Mapped` are two relation names, and every
way of GUESSING one has been measured false: `module_name` gives neither the
schema nor the table (36 connectors, 52 relations, 10 of the 38 outside the
`raw_<connector>_daily` shape), and the report profile gives it for only 4 of
google-analytics' 8. A guessed address does not return a wrong number, it returns
ANOTHER connector's rows under this flux's name -- `core/verification.py` says so
of its own registry, in those words.

So the resolution reads a DECLARATION and nothing else, and the four ways it can
have no address are four different words. The fixtures below are built on a
throw-away module tree so the test states the rule; the last block runs the same
resolution over the 39 real manifests, because a rule that only holds on a
fixture holds nowhere.
"""

from __future__ import annotations

import json

import pytest
from core.stage_relation_resolver import (
    RAW_RELATION_NOT_DECLARED,
    REPORT_PROFILE_NOT_SET,
    STAGING_MODEL_ABSENT,
    STAGING_RELATION_NOT_DECLARED,
    declared_profile_relations,
    declared_staging_models,
    message_for,
    resolve_stage_relations,
)


def _module(root, name, *, profiles, ddl=(), staging=()):
    """One throw-away connector: a manifest, a landing DDL, and dbt staging SQL."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps({"name": name, "report_profiles": list(profiles)}, indent=2),
        encoding="utf-8",
    )
    if ddl:
        body = "\n".join(
            f'_DDL_{index} = """CREATE TABLE IF NOT EXISTS {relation} (project_id VARCHAR)"""'
            for index, relation in enumerate(ddl)
        )
        (folder / "connector.py").write_text(body + "\n", encoding="utf-8")
    for model, relation in staging:
        target = folder / "dbt" / "staging"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{model}.sql").write_text(
            f"SELECT * FROM {{{{ source('raw_{name}', '{relation}') }}}}\n",
            encoding="utf-8",
        )


@pytest.fixture
def modules(tmp_path):
    """A tree that carries one of each case this resolver has to tell apart."""
    root = tmp_path / "modules"
    _module(
        root,
        "example-ads",
        profiles=[
            {
                "id": "campaign_daily",
                "raw_relation": "raw_example_ads_daily",
                "staging_relation": "stg_example_ads_daily",
            },
            # Declared raw relation, no staging model reads it.
            {
                "id": "catalog_daily",
                "raw_relation": "raw_example_ads_catalog_daily",
                "staging_relation": None,
            },
            # An event profile: it lands in `app.context_events`, not the warehouse.
            {"id": "campaign_launch", "raw_relation": None, "staging_relation": None},
        ],
        ddl=("raw_example_ads_daily", "raw_example_ads_catalog_daily"),
        staging=(("stg_example_ads_daily", "raw_example_ads_daily"),),
    )
    _module(
        root,
        "example-search",
        profiles=[
            {
                "id": "page_daily",
                "raw_relation": "raw_example_search_daily",
                "staging_relation": "stg_example_search_daily",
            },
            {
                "id": "query_page_daily",
                "raw_relation": "raw_example_search_daily",
                "staging_relation": "stg_example_search_query_page_daily",
            },
            # Several models read the relation and none is declared for this one.
            {
                "id": "catalog_daily",
                "raw_relation": "raw_example_search_daily",
                "staging_relation": None,
            },
        ],
        ddl=("raw_example_search_daily",),
        staging=(
            ("stg_example_search_daily", "raw_example_search_daily"),
            ("stg_example_search_query_page_daily", "raw_example_search_daily"),
        ),
    )
    return root


def _resolve(modules, connector, profile):
    return resolve_stage_relations(
        connector=connector, report_profile_id=profile, modules_dir=modules
    )


# ---------------------------------------------------------------------------
# The pair, when it exists.
# ---------------------------------------------------------------------------


def test_the_pair_is_the_one_the_manifest_declares(modules) -> None:
    out = _resolve(modules, "example-ads", "campaign_daily")
    assert out["collected_relation"] == "raw_example_ads_daily"
    assert out["mapped_relation"] == "stg_example_ads_daily"
    assert out["reason"] is None and out["message"] is None


def test_two_profiles_of_one_module_do_not_share_an_address(modules) -> None:
    """The defect this resolver exists to prevent, at its smallest.

    `example-search` reads ONE raw relation through two staging models, exactly as
    `gsc` reads `raw_gsc_daily` through four. Deriving the mapped relation from the
    raw one would serve the `query x page` rows of a flux whose profile is
    `page_daily` -- another profile's data under this flux's name.
    """
    page = _resolve(modules, "example-search", "page_daily")
    query = _resolve(modules, "example-search", "query_page_daily")
    assert page["collected_relation"] == query["collected_relation"]
    assert page["mapped_relation"] == "stg_example_search_daily"
    assert query["mapped_relation"] == "stg_example_search_query_page_daily"


def test_a_multi_relation_module_never_answers_another_profiles_relation(
    modules,
) -> None:
    addresses = {
        profile: _resolve(modules, "example-ads", profile)["collected_relation"]
        for profile in ("campaign_daily", "catalog_daily", "campaign_launch")
    }
    assert addresses == {
        "campaign_daily": "raw_example_ads_daily",
        "catalog_daily": "raw_example_ads_catalog_daily",
        "campaign_launch": None,
    }


# ---------------------------------------------------------------------------
# The four absences, and each one is its own word.
# ---------------------------------------------------------------------------


def test_a_datastream_with_no_profile_resolves_nothing_and_says_which(modules) -> None:
    """773 of the 842 live Datastreams. Not a conflict -- an absence with a name."""
    out = _resolve(modules, "example-ads", None)
    assert out["reason"] == REPORT_PROFILE_NOT_SET
    assert out["collected_relation"] is None and out["mapped_relation"] is None
    assert out["message"] == message_for(REPORT_PROFILE_NOT_SET)


def test_a_profile_that_declares_no_raw_relation_is_named_not_guessed(modules) -> None:
    out = _resolve(modules, "example-ads", "campaign_launch")
    assert out["reason"] == RAW_RELATION_NOT_DECLARED
    assert out["collected_relation"] is None


def test_a_profile_the_manifest_does_not_carry_answers_an_absence(modules) -> None:
    """And NEVER the relation of the profile beside it, which is the whole point."""
    out = _resolve(modules, "example-ads", "no_such_profile")
    assert out["reason"] == RAW_RELATION_NOT_DECLARED
    assert out["collected_relation"] is None


def test_an_unknown_connector_answers_an_absence_rather_than_a_neighbours_table(
    modules,
) -> None:
    out = _resolve(modules, "example-unknown", "campaign_daily")
    assert out["reason"] == RAW_RELATION_NOT_DECLARED
    assert out["collected_relation"] is None


def test_a_raw_relation_no_staging_model_reads_is_said_absent(modules) -> None:
    out = _resolve(modules, "example-ads", "catalog_daily")
    assert out["collected_relation"] == "raw_example_ads_catalog_daily"
    assert out["mapped_relation"] is None
    assert out["reason"] == STAGING_MODEL_ABSENT


def test_a_relation_several_models_read_without_a_declaration_is_a_second_word(
    modules,
) -> None:
    """Two causes, two words (arbitrage 7's rule).

    Answering `staging_model_absent` here would be a false statement about the
    repository: two models DO read the relation, and the manifest simply does not
    say which one this profile's rows reach.
    """
    out = _resolve(modules, "example-search", "catalog_daily")
    assert out["collected_relation"] == "raw_example_search_daily"
    assert out["mapped_relation"] is None
    assert out["reason"] == STAGING_RELATION_NOT_DECLARED


def test_every_absence_carries_the_sentence_the_screen_shows() -> None:
    """The reason is written HERE and read there; a screen holds none of its own."""
    for reason in (
        REPORT_PROFILE_NOT_SET,
        RAW_RELATION_NOT_DECLARED,
        STAGING_MODEL_ABSENT,
        STAGING_RELATION_NOT_DECLARED,
    ):
        sentence = message_for(reason)
        assert sentence and sentence[0].isupper() and sentence.endswith(".")
    assert message_for(None) is None


# ---------------------------------------------------------------------------
# And the same resolution over the 39 real manifests.
# ---------------------------------------------------------------------------


def test_the_real_manifests_resolve_a_pair_for_the_profiles_that_have_one() -> None:
    """Measured 2026-08-06, re-measured 2026-08-14 over the shipped declarations.

    Not a shape assertion: these four numbers are the state of the repository, and
    a profile that gains or loses an address moves one of them. The conformance
    guard beside this file is what refuses a WRONG address; this is what refuses a
    silent change of how many exist.

    114 -> 121 on 2026-08-14, and the seven are named rather than absorbed:
    `youtube-analytics` gained the seven supported combinations read from the
    provider's own channel-report documentation (audience by age and gender, by
    country, by device, traffic sources, playback locations, subscribed status,
    and the video grain). Each declares its raw and its staging relation, so each
    resolves a pair. Nine of its ten profiles resolve; the tenth is the event
    profile, which lands in `app.context_events` and has no warehouse address --
    the honest case this resolver already names.

    121 -> 123 on 2026-09-01, and the two are named the same way. `fdb78ef2` (the
    YouTube Competitors output) added `competitor_channel_snapshot` -- the stock
    of each tracked channel -- and `channel_video_directory` -- the named uploads
    with their dates, durations and public counter. Both declare a raw AND a
    staging relation, so both resolve a pair; the directory's staging model stays
    OUT of the fact table on purpose (a directory is a join key, not a measure),
    which is a warehouse decision and not an address, so it does not move any of
    the other three numbers. That commit moved four other ratchets (140 -> 142)
    and not this one, which is why the count is dated here rather than absorbed.
    Measured by diffing the resolving set against `3b7b7a83`, not by subtraction:
    two entries added, none removed.
    """
    counts: dict[str | None, int] = {}
    for connector, profiles in declared_profile_relations().items():
        for profile_id in profiles:
            reason = resolve_stage_relations(
                connector=connector, report_profile_id=profile_id
            )["reason"]
            counts[reason] = counts.get(reason, 0) + 1

    assert counts == {
        None: 123,
        RAW_RELATION_NOT_DECLARED: 10,
        STAGING_MODEL_ABSENT: 6,
        STAGING_RELATION_NOT_DECLARED: 3,
    }


def test_google_analytics_gives_each_of_its_eight_profiles_its_own_relation() -> None:
    """The module the story pins: 8 profiles, 8 relations, 4 of them underivable."""
    profiles = declared_profile_relations()["google-analytics"]
    addresses = {
        profile_id: resolve_stage_relations(
            connector="google-analytics", report_profile_id=profile_id
        )["collected_relation"]
        for profile_id in profiles
    }
    assert len(set(addresses.values())) == len(addresses) == 8
    assert addresses["pages_daily_landing"] == "raw_ga4_landing_daily"
    assert addresses["acquisition_daily_session"] == "raw_ga4_acquisition_session"
    # And no profile of this module answers with the relation of another one.
    assert addresses["standard_daily"] != addresses["user_type_daily"]


def test_the_six_relations_no_staging_model_reads_are_still_exactly_six() -> None:
    staging = declared_staging_models()
    orphans = sorted(
        relation
        for connector, profiles in declared_profile_relations().items()
        for declared in profiles.values()
        for relation in [declared["raw_relation"]]
        if relation and not (staging.get(connector) or {}).get(relation)
    )
    assert len(set(orphans)) == 6
    assert all(relation.endswith("_catalog_daily") for relation in set(orphans))
