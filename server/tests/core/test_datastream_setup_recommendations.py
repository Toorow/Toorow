"""AI-174 -- l'etape 1 du wizard doit RECOMMANDER, pas ouvrir trois selects vides.

`datastream-workbench-and-wizard.md:48` exige de la premiere section, en mode
`connector_pull` : << **Recommended:** best account and report family from
project intent and observed metadata >>. Rien ne le rendait. La demande initiale
lisait ce manque comme une galerie de templates metiers absente ; la cible ne
nomme aucune galerie, elle nomme cette recommandation-la.

Ce que ce fichier tient, et qui est exactement ce qui peut deriver :

  * une carte n'est produite QUE si l'evidence la porte -- compte expose,
    contrat persiste, rapport selectionnable, grain declare. Zero evidence rend
    une liste vide, jamais une supposition ;
  * la surete n'est PAS redecidee ici. `recommend_first_report` (story 36.8) la
    tient deja ; ce module l'apparie aux comptes et ordonne. Un second moteur
    serait un second avis sur ce qui est sur ;
  * l'ordre est deterministe : deux lectures de la meme page rendent le meme
    ordre, sinon un test et une cle React mentent tous les deux ;
  * une carte remplit la SOURCE et rien d'autre. Metriques, grain, champ de date
    appartiennent a l'etape 2, que le compilateur propose.

`conn` est `None` partout : le moteur ne l'interroge que pour resoudre une
fenetre depuis un Datastream existant, et il n'y en a aucun a la creation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.datastream_setup_observations import recommend_source_starts

_OBSERVED_AT = datetime(2026, 8, 4, tzinfo=UTC)


def _report(report_id: str, *, dimensions: list[str], selectable: bool = True) -> dict:
    return {
        "id": report_id,
        "display_name": report_id.replace("_", " ").title(),
        "availability": {"status": "selectable" if selectable else "blocked"},
        "metrics": ["clicks", "impressions"],
        "dimensions": dimensions,
        "supported_grains": [[dimensions[0]], dimensions],
        "quota_cost": {"read_points": 2, "unit": "read_points"},
    }


def _contract(connector_id: str, reports: list[dict]) -> tuple:
    snapshot = {
        "contract": {
            "display_name": connector_id.replace("-", " ").title(),
            "source_capabilities": {
                "module": {"name": connector_id},
                "contract_version": "v1",
                "reports": reports,
                "fields": [{"field_id": "clicks"}, {"field_id": "impressions"}],
            },
        }
    }
    return (connector_id, f"{connector_id}-ctr-1", "fingerprint-1", snapshot, _OBSERVED_AT)


def _account(
    account_id: str,
    connector_id: str,
    *,
    availability: str = "available",
    authorization: str = "healthy",
    used_by_count: int = 1,
    label: str | None = None,
) -> dict:
    return {
        "object_ref": {"id": account_id},
        "connector_ref": {"id": connector_id},
        "label": label or f"Account {account_id}",
        "states": {"availability": availability, "authorization": authorization},
        "evidence": {"used_by_count": used_by_count},
        "evidence_as_of": _OBSERVED_AT.isoformat(),
    }


def test_one_safe_report_yields_one_high_confidence_card():
    cards = recommend_source_starts(
        None,
        project_id="proj_EXAMPLE",
        contracts=[_contract("example-ads", [_report("campaign_daily", dimensions=["date"])])],
        source_accounts=[_account("acc-1", "example-ads")],
    )

    assert len(cards) == 1
    card = cards[0]
    assert card["confidence"]["level"] == "high"
    assert card["source_account_ref"] == "acc-1"
    assert card["connector_ref"] == "example-ads"
    assert card["report_ref"] == "campaign_daily"
    assert card["connector_contract_version_ref"] == "example-ads-ctr-1"
    # The grain travels as evidence to READ before clicking.
    assert card["derived_grain"] == ["date"]
    assert card["estimated_cost"]["unit"] == "read_points"
    # Both evidence sources the target names for this step, on every card.
    kinds = {ref["kind"] for ref in card["evidence_refs"]}
    assert kinds == {"connector_contract", "observed_metadata"}


def test_several_compatible_reports_are_offered_without_a_silent_pick():
    """The engine returns `needs_choice`; a card per candidate, never one guess."""
    cards = recommend_source_starts(
        None,
        project_id="proj_EXAMPLE",
        contracts=[
            _contract(
                "example-ads",
                [
                    _report("campaign_daily", dimensions=["date"]),
                    _report("placement_daily", dimensions=["date", "placement"]),
                ],
            )
        ],
        source_accounts=[_account("acc-1", "example-ads")],
    )

    assert {card["report_ref"] for card in cards} == {"campaign_daily", "placement_daily"}
    assert {card["confidence"]["level"] for card in cards} == {"medium"}


def test_an_unavailable_account_produces_no_card():
    """No exposed account is a handoff to Sources, not a recommendation."""
    assert (
        recommend_source_starts(
            None,
            project_id="proj_EXAMPLE",
            contracts=[_contract("example-ads", [_report("campaign_daily", dimensions=["date"])])],
            source_accounts=[_account("acc-1", "example-ads", availability="unavailable")],
        )
        == []
    )


def test_a_blocked_report_produces_no_card():
    """`availability != selectable` is the contract declining; we do not overrule it."""
    assert (
        recommend_source_starts(
            None,
            project_id="proj_EXAMPLE",
            contracts=[
                _contract(
                    "example-ads",
                    [_report("campaign_daily", dimensions=["date"], selectable=False)],
                )
            ],
            source_accounts=[_account("acc-1", "example-ads")],
        )
        == []
    )


def test_an_account_of_another_connector_is_never_paired():
    assert (
        recommend_source_starts(
            None,
            project_id="proj_EXAMPLE",
            contracts=[_contract("example-ads", [_report("campaign_daily", dimensions=["date"])])],
            source_accounts=[_account("acc-1", "example-analytics")],
        )
        == []
    )


def test_high_confidence_comes_first_and_the_order_is_stable():
    contracts = [
        _contract("zeta-ads", [_report("campaign_daily", dimensions=["date"])]),
        _contract(
            "alpha-ads",
            [
                _report("campaign_daily", dimensions=["date"]),
                _report("placement_daily", dimensions=["date", "placement"]),
            ],
        ),
    ]
    accounts = [_account("acc-z", "zeta-ads"), _account("acc-a", "alpha-ads")]

    cards = recommend_source_starts(
        None, project_id="proj_EXAMPLE", contracts=contracts, source_accounts=accounts
    )
    again = recommend_source_starts(
        None, project_id="proj_EXAMPLE", contracts=contracts, source_accounts=accounts
    )

    assert [card["recommendation_ref"] for card in cards] == [
        card["recommendation_ref"] for card in again
    ]
    # `zeta-ads` has the single safe report, so it leads despite the later name.
    assert cards[0]["confidence"]["level"] == "high"
    assert cards[0]["connector_ref"] == "zeta-ads"
    assert [card["confidence"]["level"] for card in cards[1:]] == ["medium", "medium"]


def test_a_healthy_used_account_outranks_a_degraded_unused_one():
    cards = recommend_source_starts(
        None,
        project_id="proj_EXAMPLE",
        contracts=[_contract("example-ads", [_report("campaign_daily", dimensions=["date"])])],
        source_accounts=[
            _account(
                "acc-cold",
                "example-ads",
                authorization="stale",
                used_by_count=0,
                label="Cold account",
            ),
            _account("acc-warm", "example-ads", label="Warm account"),
        ],
    )

    assert [card["source_account_ref"] for card in cards] == ["acc-warm", "acc-cold"]


def test_the_card_count_is_bounded():
    """Six is a ceiling: a rail of forty starting points is another blank page."""
    contracts = [
        _contract(
            f"connector-{index}",
            [
                _report("campaign_daily", dimensions=["date"]),
                _report("placement_daily", dimensions=["date", "placement"]),
                _report("creative_daily", dimensions=["date", "creative"]),
            ],
        )
        for index in range(5)
    ]
    accounts = [_account(f"acc-{index}", f"connector-{index}") for index in range(5)]

    cards = recommend_source_starts(
        None, project_id="proj_EXAMPLE", contracts=contracts, source_accounts=accounts
    )

    assert len(cards) == 6
