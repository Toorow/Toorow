"""HubSpot CRM seed generator -- Story 15.5 (Epic 15).

Produit ~30 jours de donnees HubSpot CRM realistes (contacts + deals) pour le loop
local dev/CI et les tests d'integration seed->mart. Les lignes suivent la forme
canonique raw que connector._pull_contacts_daily() / _pull_deals_daily() produit.

Proprietes (calibrees sur les ACs de la story) :
  * 30 jours, seasonalite weekday (moins de creation le week-end).
  * Contacts : 5-30 nouveaux contacts par jour (realiste PME/ETI).
  * Deals crees : 2-15 par jour ; deals fermes : 0-5 ; deal_amount 1000-50000 EUR.
  * ~10% des jours ont deal_amount NULL (aucun deal ferme ou sans montant, AD-9).
  * ASCII-only stdout (AI-03).

Fixture golden (AI-54) :
  Les fichiers tests/fixtures/golden_pull.json et expected_facts.json sont
  generes depuis generate_rows(end_date=FIXTURE_END_DATE, days=FIXTURE_DAYS)
  avec la graine fixe integree (rng=random.Random(15_5_2026)). La date figee
  FIXTURE_END_DATE = 2026-07-10 garantit le determinisme des fixtures, quel que
  soit le jour ou les tests s'executent.

  golden_pull.json : shape identique a la sortie de _pull_contacts_daily() /
  _pull_deals_daily() (liste de dicts, format raw avant insert DuckDB).
  Le format golden est un dict {'contacts': [...], 'deals': [...]}.

Usage :
    python generate_hubspot_seed.py          # ecrit hubspot_contacts/deals_seed.csv
    python generate_hubspot_seed.py --days 30
    python generate_hubspot_seed.py --end-date 2026-07-10  # date figee (tests/CI)
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
    0: 1.10,  # Lundi (retour de semaine, rattrapage contacts)
    1: 1.05,  # Mardi
    2: 1.00,  # Mercredi
    3: 1.02,  # Jeudi
    4: 0.90,  # Vendredi (moins de create en fin de semaine)
    5: 0.40,  # Samedi (CRM quasi inactif)
    6: 0.25,  # Dimanche
}

# Base de contacts crees par jour (realiste PME/ETI CRM actif).
BASE_CONTACTS_PER_DAY = 18

# Deals crees par jour.
BASE_DEALS_CREATED_PER_DAY = 8

# Taux de closing (deals fermes sur deals ouverts total) : ~15% des jours.
CLOSE_RATE_RANGE = (0.0, 0.3)
BASE_DEAL_AMOUNT_RANGE = (1_000.0, 50_000.0)

# Probabilite que deal_amount soit NULL (aucun deal ferme ou sans montant, AD-9).
NULL_AMOUNT_PROBABILITY = 0.12

# Colonnes CSV contacts
COLUMNS_CONTACTS = ["date", "new_contacts", "pull_id", "loaded_at", "project_id"]

# Colonnes CSV deals
COLUMNS_DEALS = [
    "date",
    "deals_created",
    "deals_closed",
    "deal_amount",
    "currency",
    "pull_id",
    "loaded_at",
    "project_id",
]

# Date figee pour les fixtures/CI (AI-54).
FIXTURE_END_DATE = date(2026, 7, 10)
FIXTURE_DAYS = 30

# Pull ID de fixture (deterministe).
FIXTURE_PULL_ID = "pull_15_5_fixture_2026_07_10"


def _maybe_null_amount(value: float, deals_closed: int, rng: random.Random) -> float | None:
    """Retourne None si deals_closed=0 ou avec probabilite NULL_AMOUNT_PROBABILITY (AD-9)."""
    if deals_closed == 0:
        return None
    if rng.random() < NULL_AMOUNT_PROBABILITY:
        return None
    return value


def generate_rows(
    days: int = 30,
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    pull_id: str | None = None,
    project_id: str = "default",
) -> tuple[list[dict], list[dict]]:
    """Retourne (contacts_rows, deals_rows) deterministes pour les derniers *days* jours.

    Forme des lignes : identique a la sortie de connector._pull_contacts_daily() /
    _pull_deals_daily() (AI-54 : la fixture golden est generee depuis cette fonction).

    REGLE CRM (AD-4) : new_contacts != conversions regies ;
    deal_amount != revenue Shopify/Stripe.

    Parameters
    ----------
    days:
        Nombre de jours a generer (defaut 30).
    end_date:
        Dernier jour de la fenetre (inclus). Defaut : date.today() pour le runtime.
        Pour les fixtures/CI utiliser FIXTURE_END_DATE (2026-07-10).
    rng:
        Generateur aleatoire. Defaut : Random(15_5_2026) (graine fixe deterministe).
    pull_id:
        ULID de pull. Defaut : FIXTURE_PULL_ID pour les fixtures, sinon genere.
    project_id:
        Binding projet (defaut: 'default').
    """
    if end_date is None:
        end_date = date.today()
    if rng is None:
        rng = random.Random(15_5_2026)
    if pull_id is None:
        if end_date == FIXTURE_END_DATE:
            pull_id = FIXTURE_PULL_ID
        else:
            pull_id = f"pull_hubspot_seed_{end_date.isoformat()}"

    loaded_at = f"{end_date.isoformat()}T00:00:00Z"
    # F-6 : fenetre [end_date-(days-1) .. end_date] INCLUSE (end_date compris).
    # Avant : start_date = end_date - timedelta(days=days) excluait end_date.
    start_date = end_date - timedelta(days=days - 1)
    contacts_rows: list[dict] = []
    deals_rows: list[dict] = []

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]

        # --- Contacts ---
        new_contacts = max(0, int(BASE_CONTACTS_PER_DAY * multiplier * rng.uniform(0.6, 1.4)))
        contacts_rows.append(
            {
                "date": day.isoformat(),
                "new_contacts": new_contacts,
                "pull_id": pull_id,
                "loaded_at": loaded_at,
                "project_id": project_id,
            }
        )

        # --- Deals ---
        deals_created = max(0, int(BASE_DEALS_CREATED_PER_DAY * multiplier * rng.uniform(0.5, 1.5)))
        # Fermetures : taux stochastique, plus faible le week-end
        raw_close_rate = rng.uniform(*CLOSE_RATE_RANGE) * multiplier
        deals_closed = max(0, int(deals_created * raw_close_rate))
        # Montant total des deals fermes
        if deals_closed > 0:
            total_amount = sum(rng.uniform(*BASE_DEAL_AMOUNT_RANGE) for _ in range(deals_closed))
            total_amount = round(total_amount, 2)
        else:
            total_amount = 0.0
        deal_amount = _maybe_null_amount(total_amount, deals_closed, rng)

        deals_rows.append(
            {
                "date": day.isoformat(),
                "deals_created": deals_created,
                "deals_closed": deals_closed,
                "deal_amount": deal_amount,
                "currency": "EUR",
                "pull_id": pull_id,
                "loaded_at": loaded_at,
                "project_id": project_id,
            }
        )

    return contacts_rows, deals_rows


def write_csv(rows: list[dict], columns: list[str], path: str | os.PathLike) -> None:
    """Ecrit les lignes dans *path* en CSV avec en-tete (None -> '' pour les CSV)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for r in rows:
            row = dict(r)
            for col in columns:
                if row.get(col) is None:
                    row[col] = ""
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HubSpot CRM seed CSV")
    parser.add_argument("--out-contacts", default=None, help="Chemin CSV contacts")
    parser.add_argument("--out-deals", default=None, help="Chemin CSV deals")
    parser.add_argument("--days", type=int, default=30, help="Nombre de jours")
    parser.add_argument(
        "--end-date",
        default=None,
        help="Date de fin ISO-8601. Utiliser 2026-07-10 pour regenerer les fixtures CI.",
    )
    args = parser.parse_args()

    here = Path(__file__).parent
    out_contacts = args.out_contacts or str(here / "hubspot_contacts_seed.csv")
    out_deals = args.out_deals or str(here / "hubspot_deals_seed.csv")

    end_dt: date | None = None
    if args.end_date:
        end_dt = date.fromisoformat(args.end_date)

    contacts_rows, deals_rows = generate_rows(days=args.days, end_date=end_dt)
    write_csv(contacts_rows, COLUMNS_CONTACTS, out_contacts)
    write_csv(deals_rows, COLUMNS_DEALS, out_deals)

    null_amount_rows = sum(1 for r in deals_rows if r.get("deal_amount") is None)
    print(f"Generated {len(contacts_rows)} contacts rows -> {out_contacts}")
    print(f"Generated {len(deals_rows)} deals rows -> {out_deals}")
    print(f"  deals rows with NULL deal_amount (AD-9 path): {null_amount_rows}/{len(deals_rows)}")


if __name__ == "__main__":
    main()
