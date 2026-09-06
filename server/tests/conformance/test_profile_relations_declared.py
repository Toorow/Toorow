"""Every report profile declares the relations it lands in -- story 58.3.

WHY A CONFORMANCE GUARD AND NOT A UNIT TEST. The declaration this checks is
spread over 39 manifests and 140 profiles, and the failure it prevents is silent:
a profile added tomorrow without `raw_relation` would simply read as an absence,
and a profile naming a relation its connector does not create would read as
ANOTHER connector's rows under this flux's name. Neither shows up as an error
anywhere else -- `core/verification.py` already wrote that sentence about its own
per-module registry, where the same fall-through produced a false row count.

THE THREE THINGS IT REFUSES, and each has an instance in the repository today:

  * a profile without the two keys. An ABSENT key and a declared `null` are two
    different statements: the second is a decision (`campaign_launch` lands in
    `app.context_events`), the first is an omission.
  * a `raw_relation` no connector.py of that module creates. This is the wrong
    address, and it is the expensive one.
  * a `staging_relation` that is not a dbt staging model of that module reading
    that raw relation. `raw_gsc_daily` is read by four models; naming the wrong
    one serves the rows of another profile.

It reads the DECLARATIONS -- the `CREATE TABLE` statements of each connector and
the `source()` references of each dbt model -- so it cannot drift from them.
"""

from __future__ import annotations

from core.stage_relation_resolver import (
    RAW_RELATION_KEY,
    STAGING_RELATION_KEY,
    declared_profile_keys,
    declared_profile_relations,
    declared_raw_relations,
    declared_staging_models,
)


def test_every_report_profile_carries_both_relation_keys(capsys) -> None:
    keys = declared_profile_keys()
    missing = [
        f"{connector}/{profile_id}"
        for connector, profiles in sorted(keys.items())
        for profile_id, present in sorted(profiles.items())
        if RAW_RELATION_KEY not in present or STAGING_RELATION_KEY not in present
    ]
    total = sum(len(profiles) for profiles in keys.values())
    with capsys.disabled():
        print()
        print(f"  report profiles carrying the pair : {total - len(missing)}/{total}")

    assert not missing, (
        "report profile(s) with no relation declared:\n  "
        + "\n  ".join(missing)
        + "\n\nDeclare both keys. `null` is a legitimate value -- a profile that "
        "lands in `app.context_events` has no warehouse relation -- but it has to "
        "be WRITTEN, because an absent key and a declared absence read the same "
        "to a resolver and mean opposite things to a person."
    )


def test_no_profile_names_a_raw_relation_its_connector_does_not_create() -> None:
    declared = declared_raw_relations()
    offenders = []
    for connector, profiles in sorted(declared_profile_relations().items()):
        creatable = set(declared.get(connector) or ())
        for profile_id, relations in sorted(profiles.items()):
            relation = relations[RAW_RELATION_KEY]
            if relation and relation not in creatable:
                offenders.append(
                    f"{connector}/{profile_id} -> {relation} "
                    f"(this module creates: {sorted(creatable) or 'nothing'})"
                )

    assert not offenders, (
        "report profile(s) addressing a relation their own connector never "
        "creates:\n  " + "\n  ".join(offenders) + "\n\nAn address that resolves to "
        "another module's table serves that module's rows under this flux's name."
    )


def test_no_profile_names_a_staging_model_that_does_not_read_its_raw_relation() -> None:
    staging = declared_staging_models()
    offenders = []
    for connector, profiles in sorted(declared_profile_relations().items()):
        readers = staging.get(connector) or {}
        for profile_id, relations in sorted(profiles.items()):
            raw = relations[RAW_RELATION_KEY]
            mapped = relations[STAGING_RELATION_KEY]
            if not mapped:
                continue
            if not raw:
                offenders.append(
                    f"{connector}/{profile_id} -> {mapped} with no raw relation"
                )
                continue
            if mapped not in (readers.get(raw) or ()):
                offenders.append(
                    f"{connector}/{profile_id} -> {mapped} does not read {raw} "
                    f"(models reading it: {sorted(readers.get(raw) or ()) or 'none'})"
                )

    assert not offenders, (
        "report profile(s) whose mapped relation is not a staging model of their "
        "own raw relation:\n  " + "\n  ".join(offenders)
    )


def test_the_declaration_covers_the_whole_catalogue_of_connectors() -> None:
    """39 manifests, and the count is asserted rather than described.

    A connector added without report profiles, or a manifest that stops parsing,
    would otherwise make the three guards above pass over a smaller world.
    """
    profiles = declared_profile_relations()
    assert len(profiles) == 39
    # 133 -> 140 le 2026-08-11 : youtube-analytics expose enfin ses repartitions
    # (audience, geo, appareil, source de trafic, lieu de lecture, statut
    # d'abonnement) et le releve d'audience, sept combinaisons que la
    # documentation de l'API declare supportees et que le manifeste taisait.
    # 140 -> 142 le 2026-09-01 : le meme connecteur porte la sortie Competitors
    # (capabilities/competitors.md) -- `competitor_channel_snapshot` (stocks des
    # chaines suivies) et `channel_video_directory` (les uploads NOMMES, dates,
    # durees, compteur public), pilotes par la declaration tracked_entity.
    assert sum(len(entries) for entries in profiles.values()) == 142


def test_every_declared_profile_is_also_a_profile_the_landing_guard_can_reach() -> None:
    """The two declarations name the SAME profiles -- AI-311.

    A profile is declared twice in its manifest, and for two different readers:
    `report_profiles[]` carries the address (this file), and
    `source_capabilities.reports[]` carries the callable that writes to it
    (`test_profile_landing_is_where_the_pull_writes.py`). Neither file can see a
    profile that is missing from ITS list.

    So a profile added to `report_profiles` alone keeps this guard at
    `140/140` -- one more -- while dropping out of the landing guard's subject
    without a word: no wrong address is reported, because the profile is never
    read. That is the reach shrinking in silence, which is the one failure a
    conformance guard cannot afford. Measured 2026-08-31: 140 on each side, and
    the two sets are equal.
    """
    from core.profile_landing_trace import traced_profile_landings

    addressed = {
        (connector, profile_id)
        for connector, profiles in declared_profile_relations().items()
        for profile_id in profiles
    }
    dispatched = {
        (record["connector"], record["report_profile_id"])
        for record in traced_profile_landings()
    }

    assert addressed == dispatched, (
        "the two profile declarations of these manifests disagree:\n"
        f"  declared in report_profiles only : {sorted(addressed - dispatched)}\n"
        f"  declared in source_capabilities only : {sorted(dispatched - addressed)}\n\n"
        "Declare the profile in both, or neither. A profile present on one side "
        "only is invisible to the guard reading the other side, and the number "
        "that guard prints stays green while its subject gets smaller."
    )
