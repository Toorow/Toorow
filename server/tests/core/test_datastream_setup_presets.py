"""57.6 -- la projection du catalogue, prouvee HORS LIGNE.

Aucune base, aucun reseau : le contrat se construit en memoire, exactement comme
`test_datastream_setup_recommendations.py` le fait deja pour le classement. La
doctrine du depot est de prouver le contrat sans compte reel avant d'en reclamer
un, et un catalogue derive n'a besoin de rien d'autre que du contrat.

Trois faits, et ce sont les trois qui pouvaient casser :

  * le NOM du rapport vient de `report_profiles[].display_name`. Mesure du
    2026-08-05 : `source_capabilities.reports[].display_name` n'existe que sur 2
    entrees sur 133, donc sans la jointure 131 cartes affichent un identifiant
    technique -- et une carte qui affiche `daily_v2` n'est pas une carte ;
  * `safety` REPETE le verdict de `recommend_first_report` (story 36.8) et ne
    l'invente pas. Un rapport sans grain declare n'est pas un premier tirage sur,
    et la projection doit le dire au lieu de le presenter comme recommande ;
  * `smallest_declared_grain` est un grain DECLARE, jamais construit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.datastream_setup_observations import _connector_option_contract, recommend_source_starts

_OBSERVED_AT = datetime(2026, 8, 5, tzinfo=UTC)


def _capability_report(
    report_id: str,
    *,
    dimensions: list[str],
    grains: list[list[str]] | None,
    status: str = "selectable",
    reason_code: str | None = None,
) -> dict:
    """UN RAPPORT DU CATALOGUE, SANS `display_name` -- le cas de 131 sur 133.

    `availability.status` vaut `selectable` ou `unavailable` : aucun manifeste ne
    produit d'autre valeur, et une fixture qui en inventerait une prouverait un
    chemin que le produit n'emprunte jamais.
    """
    availability: dict[str, str] = {"status": status}
    if reason_code:
        availability["reason_code"] = reason_code
    return {
        "id": report_id,
        "availability": availability,
        "metrics": ["clicks", "impressions"],
        "dimensions": dimensions,
        "supported_grains": grains if grains is not None else [],
        "quota_cost": {"read_points": 2, "unit": "read_points"},
    }


def _snapshot(connector_id: str, reports: list[dict], profiles: list[dict]) -> dict:
    return {
        "contract": {
            "display_name": connector_id.replace("-", " ").title(),
            "report_profiles": profiles,
            "source_capabilities": {
                "module": {"name": connector_id},
                "contract_version": "v1",
                "reports": reports,
                "fields": [{"field_id": "clicks"}, {"field_id": "impressions"}],
            },
        }
    }


def test_report_name_comes_from_the_profile_when_the_catalog_carries_none():
    snapshot = _snapshot(
        "generic-ads",
        [_capability_report("campaign_daily", dimensions=["date"], grains=[["date"]])],
        [{"id": "campaign_daily", "display_name": "Campaign performance (daily)"}],
    )

    projected = _connector_option_contract(snapshot)

    assert [report["display_name"] for report in projected["reports"]] == [
        "Campaign performance (daily)"
    ]
    assert projected["contract_state"] == "verified"


def test_a_report_with_no_profile_falls_back_to_its_reference_never_to_a_guess():
    snapshot = _snapshot(
        "generic-ads",
        [_capability_report("campaign_daily", dimensions=["date"], grains=[["date"]])],
        [],
    )

    projected = _connector_option_contract(snapshot)

    # Le repli est la REFERENCE, telle quelle. Un de-soulignage fabriquerait un
    # libelle que ni le manifeste ni le contrat ne portent.
    assert projected["reports"][0]["display_name"] == "campaign_daily"


def test_safety_repeats_the_engine_and_never_promotes_a_report_without_a_grain():
    snapshot = _snapshot(
        "generic-ads",
        [
            _capability_report("campaign_daily", dimensions=["date"], grains=[["date"]]),
            _capability_report("grainless_daily", dimensions=["date"], grains=None),
        ],
        [
            {"id": "campaign_daily", "display_name": "Campaign performance (daily)"},
            {"id": "grainless_daily", "display_name": "Grainless report"},
        ],
    )

    by_ref = {
        report["report_ref"]: report
        for report in _connector_option_contract(snapshot)["reports"]
    }

    # Un seul candidat sur -> le moteur dit `recommended`, et la carte le repete.
    assert by_ref["campaign_daily"]["safety"]["outcome"] == "recommended"
    # Aucun grain declare -> le moteur ne le retient pas, et la projection ne le
    # promeut pas. C'est le refus qui compte : `recommended` par defaut serait la
    # fabrication que le moteur existe pour empecher.
    assert by_ref["grainless_daily"]["safety"]["outcome"] == "no_safe_recommendation"


def test_several_safe_reports_read_needs_choice_rather_than_a_silent_pick():
    snapshot = _snapshot(
        "generic-ads",
        [
            _capability_report("campaign_daily", dimensions=["date"], grains=[["date"]]),
            _capability_report("country_daily", dimensions=["date", "country"], grains=[["date"]]),
        ],
        [
            {"id": "campaign_daily", "display_name": "Campaign performance (daily)"},
            {"id": "country_daily", "display_name": "Country performance (daily)"},
        ],
    )

    outcomes = {
        report["report_ref"]: report["safety"]["outcome"]
        for report in _connector_option_contract(snapshot)["reports"]
    }

    assert outcomes == {"campaign_daily": "needs_choice", "country_daily": "needs_choice"}


def test_smallest_declared_grain_is_declared_never_constructed():
    snapshot = _snapshot(
        "generic-ads",
        [
            _capability_report(
                "campaign_daily",
                dimensions=["date", "campaign"],
                grains=[["date", "campaign"], ["date"]],
            ),
            _capability_report("grainless_daily", dimensions=["date"], grains=None),
        ],
        [],
    )

    by_ref = {
        report["report_ref"]: report
        for report in _connector_option_contract(snapshot)["reports"]
    }

    assert by_ref["campaign_daily"]["smallest_declared_grain"] == ["date"]
    # Aucun grain declare -> `None`, jamais un grain compose a partir des
    # dimensions : un grain non annonce est un `unsupported_grain`.
    assert by_ref["grainless_daily"]["smallest_declared_grain"] is None


def test_an_unavailable_report_keeps_its_reason_code_for_the_screen():
    snapshot = _snapshot(
        "generic-ads",
        [
            _capability_report(
                "creative_daily",
                dimensions=["date"],
                grains=[["date"]],
                status="unavailable",
                reason_code="endpoint_retired_by_provider",
            )
        ],
        [{"id": "creative_daily", "display_name": "Creative performance (daily)"}],
    )

    report = _connector_option_contract(snapshot)["reports"][0]

    assert report["availability"] == {
        "status": "unavailable",
        "reason_code": "endpoint_retired_by_provider",
    }


def test_a_manifest_projection_is_marked_unverified_and_reads_the_same_way():
    """Le catalogue vient du MANIFESTE quand aucun run n'a ecrit de contrat.

    Zero ligne dans `app.connector_contract_versions` a ce jour : conditionner le
    catalogue au contrat verifie le rendrait vide pour les 39 connecteurs. La
    projection accepte donc un manifeste nu -- meme forme, meme jointure -- et
    l'etat est porte par `contract_state`, jamais confondu avec `stale`.
    """
    manifest = {
        "display_name": "Generic Ads",
        "report_profiles": [
            {"id": "campaign_daily", "display_name": "Campaign performance (daily)"}
        ],
        "source_capabilities": {
            "module": {"name": "generic-ads"},
            "reports": [
                _capability_report("campaign_daily", dimensions=["date"], grains=[["date"]])
            ],
            "fields": [{"field_id": "clicks"}],
        },
    }

    projected = _connector_option_contract(manifest, contract_state="unverified")

    assert projected["contract_state"] == "unverified"
    assert projected["reports"][0]["display_name"] == "Campaign performance (daily)"
    assert projected["reports"][0]["safety"]["outcome"] == "recommended"


def test_the_recommendation_card_carries_the_profile_name_too():
    """Le meme defaut a DEUX sites, repare aux deux.

    Ne reparer que le catalogue laisserait chaque carte de recommandation porter
    un identifiant pour 131 rapports sur 133 -- c'est une classe, pas un bug.
    """
    snapshot = _snapshot(
        "generic-ads",
        [_capability_report("campaign_daily", dimensions=["date"], grains=[["date"]])],
        [{"id": "campaign_daily", "display_name": "Campaign performance (daily)"}],
    )
    contracts = [("generic-ads", "ccv-1", "fingerprint-1", snapshot, _OBSERVED_AT)]
    accounts = [
        {
            "object_ref": {"id": "sacct-1"},
            "connector_ref": {"id": "generic-ads"},
            "label": "Northern market account",
            "states": {"availability": "available", "authorization": "healthy"},
            "evidence": {"used_by_count": 1},
            "evidence_as_of": _OBSERVED_AT.isoformat(),
        }
    ]

    cards = recommend_source_starts(
        None, project_id="project-under-test", contracts=contracts, source_accounts=accounts
    )

    assert [card["report_display_name"] for card in cards] == ["Campaign performance (daily)"]
