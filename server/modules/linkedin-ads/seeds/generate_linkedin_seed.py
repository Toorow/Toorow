"""LinkedIn Ads seed generator — Story 15.3 (Epic 15).

Produit des lignes ``raw_linkedin_ads_daily`` deterministiques pour la boucle dev/CI
locale, le test d'integration seed->mart et la fixture golden de conformance (AI-54).

Les lignes sont au shape canonique que connector._parse_report_row() produit :
(date, data_level, campaign_id, campaign_group_id, costInLocalCurrency, impressions,
clicks, externalWebsiteConversions, leadGenerationMailContactInfoShares,
cost_source_currency).

Lecon review-15-2 F-1 (pattern TikTok) : le generateur emet les DEUX grains
(campaign / campaign_group) COEXISTANT dans la meme table raw, chacun estampille
de son data_level (CAMPAIGN | CAMPAIGN_GROUP). Les lignes campaign_group sont des
roll-ups EXACTS des campagnes du groupe, donc SUM(campaigns) == campaign_group total.
C'est precisement le cas qui double-compterait sans le filtre data_level au mart.

Proprietes :
  * >= 35 jours, quelques campaign groups avec saisonnalite hebdomadaire.
  * spend/impressions/clicks/conversions/leads DETERMINISTES (graine RNG fixe).
  * Les leads (leadGenerationMailContactInfoShares) sont plus faibles que les
    conversions web (b2b -- formulaires in-app plus rares).
  * ASCII-only stdout (AI-03).

Usage :
    python generate_linkedin_seed.py                     # campaign grain only
    python generate_linkedin_seed.py --grains multi      # 2 grains coexistant
    python generate_linkedin_seed.py --out /tmp/li.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

# AI-54 : end_date figee pour la REGENERATION des fixtures de conformance
# (tests/fixtures/golden_pull.json + expected_facts.json) -- passer explicitement
# end_date=DEFAULT_SEED_END_DATE dans ce chemin-la uniquement.
# review-15-3 F-1 : le DEFAUT runtime reste date.today() comme GA4/TikTok, sinon la
# fenetre du seed derive des autres connecteurs et le test dedup devient vide
# (F-2 : plus aucune date partagee avec GA4/Meta une fois l'horloge avancee).
# NE PAS changer cette constante sans regenerer les fixtures.
DEFAULT_SEED_END_DATE: date = date(2026, 7, 19)

WEEKDAY_MULTIPLIERS: dict[int, float] = {
    0: 1.02,  # Lundi (B2B : commence la semaine)
    1: 1.08,  # Mardi
    2: 1.10,  # Mercredi (pic B2B LinkedIn)
    3: 1.06,  # Jeudi
    4: 0.95,  # Vendredi
    5: 0.55,  # Samedi (tres faible B2B)
    6: 0.45,  # Dimanche
}

# Quelques campaign groups avec leurs campagnes associees.
# Format : (group_id, group_name, [(campaign_id, campaign_name), ...])
CAMPAIGN_GROUPS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "99000001",
        "Toorow - Brand Awareness B2B",
        [
            ("88000001", "Brand FR - Decision Makers"),
            ("88000002", "Brand EU - CTO Targeting"),
        ],
    ),
    (
        "99000002",
        "Toorow - Lead Gen SaaS",
        [
            ("88000003", "Lead Gen - Marketing Directors"),
            ("88000004", "Lead Gen - RevOps Profiles"),
        ],
    ),
    (
        "99000003",
        "Toorow - Retargeting",
        [
            ("88000005", "Retargeting - Website Visitors"),
        ],
    ),
]

# data_level stamps (miroir connector._DATA_LEVEL_BY_PROFILE).
DATA_LEVEL_CAMPAIGN = "CAMPAIGN"
DATA_LEVEL_CAMPAIGN_GROUP = "CAMPAIGN_GROUP"

# Parametres de cout/volume par campagne.
# LinkedIn est plus cher que Meta/TikTok (CPM eleve B2B).
SPEND_RANGE: tuple[float, float] = (30.0, 250.0)
CPM_RANGE: tuple[float, float] = (8.0, 25.0)    # cout par mille impressions (LinkedIn cher)
CTR_RANGE: tuple[float, float] = (0.004, 0.015)  # CTR LinkedIn (plus faible que GA4/Meta)
CVR_RANGE: tuple[float, float] = (0.01, 0.06)   # taux de conversion web
LEAD_RATE_RANGE: tuple[float, float] = (0.002, 0.015)  # taux de lead form (faible)

COLUMNS = [
    "date",
    "data_level",
    "campaign_id",
    "campaign_group_id",
    "costInLocalCurrency",
    "impressions",
    "clicks",
    "externalWebsiteConversions",
    "leadGenerationMailContactInfoShares",
    "cost_source_currency",
]


def generate_rows(
    days: int = 40,
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    currency: str = "EUR",
) -> list[dict]:
    """Retourne des lignes LinkedIn raw au grain campaign (data_level=CAMPAIGN).

    Les lignes correspondent au shape _parse_report_row() output (AI-54 fixture
    honnete) : la fixture golden de conformance est une slice de ce meme generateur.
    end_date par defaut = date.today() (pattern GA4/TikTok, review-15-3 F-1) ;
    les fixtures passent explicitement end_date=DEFAULT_SEED_END_DATE.
    """
    if end_date is None:
        end_date = date.today()
    if rng is None:
        # Graine fixe -> donnees deterministes (fixtures/tests reproductibles).
        rng = random.Random(15_3_2026)

    start_date = end_date - timedelta(days=days)
    rows: list[dict] = []

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]

        for group_id, _group_name, campaigns in CAMPAIGN_GROUPS:
            for campaign_id, _campaign_name in campaigns:
                spend = round(rng.uniform(*SPEND_RANGE) * multiplier, 2)
                cpm = rng.uniform(*CPM_RANGE)
                impressions = max(1, int(spend / cpm * 1000))
                clicks = max(0, int(impressions * rng.uniform(*CTR_RANGE)))
                conversions = max(0, int(clicks * rng.uniform(*CVR_RANGE)))
                leads = max(0, int(impressions * rng.uniform(*LEAD_RATE_RANGE)))

                rows.append(
                    {
                        "date": day.isoformat(),
                        "data_level": DATA_LEVEL_CAMPAIGN,
                        "campaign_id": campaign_id,
                        "campaign_group_id": None,  # grain campaign : group_id = NULL
                        "costInLocalCurrency": spend,
                        "impressions": impressions,
                        "clicks": clicks,
                        "externalWebsiteConversions": conversions,
                        "leadGenerationMailContactInfoShares": leads,
                        "cost_source_currency": currency,
                    }
                )
    return rows


def generate_multigrain_rows(
    days: int = 40,
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    currency: str = "EUR",
) -> list[dict]:
    """Retourne les DEUX grains (campaign + campaign_group) COEXISTANT.

    Lecon review-15-2 F-1 : les lignes campaign_group sont des roll-ups EXACTS
    des campagnes du groupe. SUM(campaigns) == campaign_group total par (date, metrique).
    Chaque grain est estampille de son data_level.
    end_date par defaut = date.today() (pattern GA4/TikTok, review-15-3 F-1) ;
    les fixtures passent explicitement end_date=DEFAULT_SEED_END_DATE.
    """
    if end_date is None:
        end_date = date.today()
    if rng is None:
        rng = random.Random(15_3_2026)

    start_date = end_date - timedelta(days=days)
    rows: list[dict] = []

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]

        for group_id, _group_name, campaigns in CAMPAIGN_GROUPS:
            # Accumulateurs pour le roll-up campaign_group (sommes exactes).
            group_spend = 0.0
            group_impr = group_clicks = group_conv = group_leads = 0
            campaign_rows: list[dict] = []

            for campaign_id, _campaign_name in campaigns:
                spend = round(rng.uniform(*SPEND_RANGE) * multiplier, 2)
                cpm = rng.uniform(*CPM_RANGE)
                impressions = max(1, int(spend / cpm * 1000))
                clicks = max(0, int(impressions * rng.uniform(*CTR_RANGE)))
                conversions = max(0, int(clicks * rng.uniform(*CVR_RANGE)))
                leads = max(0, int(impressions * rng.uniform(*LEAD_RATE_RANGE)))

                campaign_rows.append(
                    {
                        "date": day.isoformat(),
                        "data_level": DATA_LEVEL_CAMPAIGN,
                        "campaign_id": campaign_id,
                        "campaign_group_id": None,
                        "costInLocalCurrency": spend,
                        "impressions": impressions,
                        "clicks": clicks,
                        "externalWebsiteConversions": conversions,
                        "leadGenerationMailContactInfoShares": leads,
                        "cost_source_currency": currency,
                    }
                )
                group_spend += spend
                group_impr += impressions
                group_clicks += clicks
                group_conv += conversions
                group_leads += leads

            # Ligne campaign_group = roll-up exact des campagnes du groupe.
            rows.append(
                {
                    "date": day.isoformat(),
                    "data_level": DATA_LEVEL_CAMPAIGN_GROUP,
                    "campaign_id": None,  # grain group : campaign_id = NULL
                    "campaign_group_id": group_id,
                    "costInLocalCurrency": round(group_spend, 2),
                    "impressions": group_impr,
                    "clicks": group_clicks,
                    "externalWebsiteConversions": group_conv,
                    "leadGenerationMailContactInfoShares": group_leads,
                    "cost_source_currency": currency,
                }
            )
            rows.extend(campaign_rows)

    return rows


def write_csv(rows: list[dict], path: str | os.PathLike) -> None:
    """Ecrit les lignes dans *path* en CSV avec en-tete (None -> cellule vide)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            row = {k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS}
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LinkedIn Ads seed CSV")
    parser.add_argument("--out", default=None, help="Output CSV path")
    parser.add_argument("--days", type=int, default=40, help="Number of days to seed")
    parser.add_argument(
        "--grains",
        choices=["campaign", "multi"],
        default="campaign",
        help="campaign = grain campaign seul ; multi = 2 grains coexistant (F-1)",
    )
    args = parser.parse_args()

    here = Path(__file__).parent
    out = args.out or str(here / "linkedin_ads_seed.csv")
    if args.grains == "campaign":
        rows = generate_rows(days=args.days)
    else:
        rows = generate_multigrain_rows(days=args.days)
    write_csv(rows, out)

    camp_rows = [r for r in rows if r["data_level"] == DATA_LEVEL_CAMPAIGN]
    total_spend = sum(r["costInLocalCurrency"] for r in camp_rows)
    total_conv = sum(r["externalWebsiteConversions"] for r in camp_rows)
    total_leads = sum(r["leadGenerationMailContactInfoShares"] for r in camp_rows)
    print(f"Generated {len(rows)} rows ({args.grains} grains) -> {out}")
    print(
        f"campaign-grain total spend: {total_spend:.2f}  "
        f"conversions: {total_conv}  leads: {total_leads}"
    )


if __name__ == "__main__":
    main()
