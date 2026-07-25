"""Klaviyo seed generator -- Story 15.8 (Epic 15).

Produit ~40 jours de donnees Klaviyo realistes (campagnes + flows) pour le loop
local dev/CI et les tests d'integration seed->mart. Les lignes suivent la forme
canonique raw que connector._parse_reporting_row() produit, de sorte que la
fixture golden de conformance (AI-54) puisse etre regeneree par la MEME logique
de generation (graine fixe + end_date fige), pas ecrite a la main.

Proprietes (calibrees sur les ACs de la story) :
  * 40 jours, 3 campagnes + 2 flows, seasonalite weekday.
  * Metriques presentes sur ~90% des lignes ; ~10% ont NULL sur au moins
    une metrique (pour exercer le chemin NULL honnete AD-9).
  * attributed_revenue TOUJOURS separe des colonnes revenue Shopify/Stripe
    (aucun total croise dans les donnees seedees).
  * ASCII-only stdout (AI-03).

Fixture golden (AI-54) :
  Les fichiers tests/fixtures/golden_pull.json et expected_facts.json sont
  generes depuis generate_rows(end_date=FIXTURE_END_DATE, days=FIXTURE_DAYS)
  avec la graine fixe integree (rng=random.Random(15_8_2026)). La date fixee
  FIXTURE_END_DATE = 2026-07-10 garantit le determinisme des fixtures, quel que
  soit le jour ou les tests s'executent. Toute valeur dans golden_pull.json qui
  ne correspond PAS a generate_rows(end_date=FIXTURE_END_DATE) est une erreur.

Usage :
    python generate_klaviyo_seed.py                     # ecrit klaviyo_seed.csv
    python generate_klaviyo_seed.py --out /tmp/test.csv --days 40
    python generate_klaviyo_seed.py --end-date 2026-07-10  # date figee (tests/CI)
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Constantes de generation
# ---------------------------------------------------------------------------

WEEKDAY_MULTIPLIERS: dict[int, float] = {
    0: 1.00,  # Lundi
    1: 1.05,  # Mardi
    2: 1.03,  # Mercredi
    3: 1.02,  # Jeudi
    4: 1.10,  # Vendredi
    5: 0.75,  # Samedi (emails envoyes moins souvent)
    6: 0.60,  # Dimanche
}

# Campagnes fictives (simulees comme des campagnes ponctuelles Klaviyo).
CAMPAIGNS = [
    {"campaign_id": "camp_promo_ete_01", "campaign_name": "Promo Ete 2026"},
    {"campaign_id": "camp_flash_sale_02", "campaign_name": "Flash Sale Juillet"},
    {"campaign_id": "camp_newsletter_03", "campaign_name": "Newsletter Mensuelle"},
]

# Flows fictifs (sequences automatisees Klaviyo).
FLOWS = [
    {"flow_id": "flow_abandon_cart_01", "flow_name": "Abandon Panier"},
    {"flow_id": "flow_welcome_02", "flow_name": "Welcome Series"},
]

# Nombre de base d'envois par campagne/flow par jour actif.
BASE_SENDS_CAMPAIGN = 5000
BASE_SENDS_FLOW = 800

# Taux d'ouverture, de clic, de conversion (approx. email e-commerce).
OPEN_RATE_RANGE = (0.18, 0.35)
CLICK_RATE_RANGE = (0.02, 0.08)
CONVERSION_RATE_RANGE = (0.01, 0.04)
REVENUE_PER_CONVERSION_RANGE = (55.0, 180.0)

# Probabilite qu'une metrique soit NULL (AD-9 : NULL honnete, pas de 0 implicit).
NULL_METRIC_PROBABILITY = 0.08  # ~8% des lignes ont au moins une metrique NULL

COLUMNS_CAMPAIGN = [
    "date",
    "campaign_id",
    "campaign_name",
    "flow_id",
    "flow_name",
    "sends",
    "opens",
    "clicks",
    "attributed_conversions",
    "attributed_revenue",
]


def _maybe_null(value: float, rng: random.Random) -> float | None:
    """Retourne None avec une probabilite NULL_METRIC_PROBABILITY (AD-9)."""
    if rng.random() < NULL_METRIC_PROBABILITY:
        return None
    return value


# Date figee pour les fixtures/CI (AI-54). Toute fixture golden doit utiliser
# cette constante pour garantir le determinisme entre regenerations.
FIXTURE_END_DATE = date(2026, 7, 10)
FIXTURE_DAYS = 40


def generate_rows(
    days: int = 40,
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
) -> list[dict]:
    """Retourne des lignes Klaviyo deterministes pour les derniers *days* jours.

    Forme des lignes : identique a la sortie de connector._parse_reporting_row()
    (AI-54 : la fixture golden de conformance est generee depuis cette fonction
    avec end_date=FIXTURE_END_DATE, pas ecrite a la main).

    Les campagnes ne sont pas toutes actives chaque jour (seasonalite realiste).
    Les flows produisent des lignes chaque jour (envoi automatise continu).

    REGLE DE NON-AGREGATION (AD-4) : attributed_revenue est une colonne DISTINCTE
    du revenue Shopify/Stripe. Les seeders ne melangent jamais ces sources.

    Parameters
    ----------
    days:
        Nombre de jours a generer (defaut 40).
    end_date:
        Dernier jour de la fenetre (inclus). Defaut : date.today() pour le runtime.
        Pour les fixtures/CI utiliser FIXTURE_END_DATE (2026-07-10).
    rng:
        Generateur aleatoire. Defaut : Random(15_8_2026) (graine fixe deterministe).
    """
    if end_date is None:
        end_date = date.today()
    if rng is None:
        # Graine fixe -> donnees deterministes (fixtures/tests reproductibles).
        rng = random.Random(15_8_2026)

    start_date = end_date - timedelta(days=days)
    rows: list[dict] = []

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]

        # --- Campagnes ---
        # Chaque campagne a une probabilite d'etre active ce jour-la
        # (les campagnes ponctuelles ne s'envoient pas tous les jours).
        for camp in CAMPAIGNS:
            # Activer la campagne ~60% des jours (realiste pour des newsletters).
            if rng.random() > 0.60:
                continue

            sends_raw = int(BASE_SENDS_CAMPAIGN * multiplier * rng.uniform(0.8, 1.2))
            opens_raw = int(sends_raw * rng.uniform(*OPEN_RATE_RANGE))
            clicks_raw = int(opens_raw * rng.uniform(*CLICK_RATE_RANGE))
            conversions_raw = int(clicks_raw * rng.uniform(*CONVERSION_RATE_RANGE))
            revenue_raw = round(
                conversions_raw * rng.uniform(*REVENUE_PER_CONVERSION_RANGE), 2
            )

            rows.append(
                {
                    "date": day.isoformat(),
                    "campaign_id": camp["campaign_id"],
                    "campaign_name": camp["campaign_name"],
                    "flow_id": None,
                    "flow_name": None,
                    "sends": _maybe_null(float(sends_raw), rng),
                    "opens": _maybe_null(float(opens_raw), rng),
                    "clicks": _maybe_null(float(clicks_raw), rng),
                    # REGLE : attributed_conversions = conversions REVENDIQUEES canal.
                    "attributed_conversions": _maybe_null(float(conversions_raw), rng),
                    # REGLE : attributed_revenue != revenue Shopify (sources distinctes).
                    "attributed_revenue": _maybe_null(revenue_raw, rng),
                }
            )

        # --- Flows ---
        # Les flows s'executent en continu (un email d'abandon panier chaque jour).
        for flow in FLOWS:
            sends_raw = int(BASE_SENDS_FLOW * multiplier * rng.uniform(0.7, 1.3))
            opens_raw = int(sends_raw * rng.uniform(*OPEN_RATE_RANGE))
            clicks_raw = int(opens_raw * rng.uniform(*CLICK_RATE_RANGE))
            conversions_raw = int(clicks_raw * rng.uniform(*CONVERSION_RATE_RANGE))
            revenue_raw = round(
                conversions_raw * rng.uniform(*REVENUE_PER_CONVERSION_RANGE), 2
            )

            rows.append(
                {
                    "date": day.isoformat(),
                    "campaign_id": None,
                    "campaign_name": None,
                    "flow_id": flow["flow_id"],
                    "flow_name": flow["flow_name"],
                    "sends": _maybe_null(float(sends_raw), rng),
                    "opens": _maybe_null(float(opens_raw), rng),
                    "clicks": _maybe_null(float(clicks_raw), rng),
                    "attributed_conversions": _maybe_null(float(conversions_raw), rng),
                    "attributed_revenue": _maybe_null(revenue_raw, rng),
                }
            )

    return rows


def write_csv(rows: list[dict], path: str | os.PathLike) -> None:
    """Ecrit les lignes dans *path* en CSV avec en-tete (None -> '' pour les CSV)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS_CAMPAIGN)
        writer.writeheader()
        for r in rows:
            row = dict(r)
            # CSV : les None deviennent des chaines vides (distinguables de '0').
            for col in COLUMNS_CAMPAIGN:
                if row.get(col) is None:
                    row[col] = ""
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Klaviyo seed CSV")
    parser.add_argument("--out", default=None, help="Chemin CSV de sortie")
    parser.add_argument("--days", type=int, default=40, help="Nombre de jours a seeder")
    parser.add_argument(
        "--end-date",
        default=None,
        help=(
            "Date de fin ISO-8601 (ex: 2026-07-10). Utiliser FIXTURE_END_DATE=2026-07-10 "
            "pour regenerer les fixtures CI (deterministe). Defaut: date.today()."
        ),
    )
    args = parser.parse_args()

    here = Path(__file__).parent
    out = args.out or str(here / "klaviyo_seed.csv")

    end_dt: date | None = None
    if args.end_date:
        end_dt = date.fromisoformat(args.end_date)
    rows = generate_rows(days=args.days, end_date=end_dt)
    write_csv(rows, out)

    campaign_rows = sum(1 for r in rows if r.get("campaign_id"))
    flow_rows = sum(1 for r in rows if r.get("flow_id"))
    null_metric_rows = sum(
        1 for r in rows
        if any(
            r.get(m) is None
            for m in (
                "sends", "opens", "clicks", "attributed_conversions", "attributed_revenue"
            )
        )
    )
    print(f"Generated {len(rows)} rows -> {out}")
    print(f"  campaign rows : {campaign_rows}")
    print(f"  flow rows     : {flow_rows}")
    print(f"  rows with NULL metric (AD-9 path) : {null_metric_rows}/{len(rows)}")


if __name__ == "__main__":
    main()
