"""Google Sheets seed generator -- Story 15.6 (Epic 15).

Produit des lignes ``raw_google_sheets_daily`` deterministiques pour la boucle dev/CI
locale, le test d'integration seed->mart et la fixture golden de conformance (AI-54).

Les lignes sont au shape canonique que connector._parse_sheet_row() produit :
(date, sheet_row_id, budget_declared, target_revenue, target_conversions).

La fixture golden est generee depuis ce meme generateur (AI-54 : fixture = appel
reel au parseur sur des payloads shape API reels, jamais ecrite a la main).

Proprietes :
  * >= 35 jours, quelques canaux avec saisonnalite mensuelle.
  * budget_declared / target_revenue / target_conversions DETERMINISTES (graine RNG fixe).
  * Simule une feuille d'objectifs marketing standard (canal x mois).
  * ASCII-only stdout (AI-03).

Usage :
    python generate_google_sheets_seed.py
    python generate_google_sheets_seed.py --out /tmp/gs.csv --days 40
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

# AI-54 : end_date figee pour la REGENERATION des fixtures de conformance.
# review-15-3 F-1 pattern : le DEFAUT runtime reste date.today() pour les pulls.
# NE PAS changer cette constante sans regenerer les fixtures.
DEFAULT_SEED_END_DATE: date = date(2026, 7, 19)

# Quelques canaux/lignes representatifs d'un plan media.
# Format : (sheet_row_id, budget_base, revenue_target_ratio, conversion_target)
CHANNELS: list[tuple[str, float, float, float]] = [
    ("Meta Ads",         5000.0, 3.5, 120.0),
    ("Google Ads",       4000.0, 4.0,  95.0),
    ("TikTok Ads",       1500.0, 2.8,  40.0),
    ("LinkedIn Ads",     2000.0, 5.0,  25.0),
    ("Email (Klaviyo)",   800.0, 6.0,  80.0),
]

COLUMNS = [
    "date",
    "sheet_row_id",
    "budget_declared",
    "target_revenue",
    "target_conversions",
]

# Rng seed fixe -> donnees deterministes.
_RNG_SEED: int = 15_6_2026


def generate_rows(
    days: int = 40,
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
) -> list[dict]:
    """Retourne des lignes Google Sheets raw au shape _parse_sheet_row() output.

    La fixture golden de conformance est une slice de ce meme generateur (AI-54).
    end_date par defaut = date.today() ; les fixtures passent DEFAULT_SEED_END_DATE.
    """
    if end_date is None:
        end_date = date.today()
    if rng is None:
        rng = random.Random(_RNG_SEED)

    start_date = end_date - timedelta(days=days)
    rows: list[dict] = []

    # Les objectifs sont declares par mois : on emet une ligne par canal par jour
    # (meme valeur pour tout le mois -- simule un plan media mensuel).
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        # Ajout d'un leger bruit mensuel (objectif recalibre chaque mois).
        month_seed = day.year * 100 + day.month
        month_rng = random.Random(month_seed + _RNG_SEED)

        for channel_id, budget_base, rev_ratio, conv_base in CHANNELS:
            # Variation mensuelle +/- 10%.
            budget = round(budget_base * month_rng.uniform(0.90, 1.10), 2)
            target_rev = round(budget * rev_ratio * month_rng.uniform(0.95, 1.05), 2)
            target_conv = round(conv_base * month_rng.uniform(0.90, 1.10), 1)

            rows.append(
                {
                    "date": day.isoformat(),
                    "sheet_row_id": channel_id,
                    "budget_declared": budget,
                    "target_revenue": target_rev,
                    "target_conversions": target_conv,
                }
            )

    return rows


def generate_api_payload(
    days: int = 5,
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
) -> list[list[str]]:
    """Genere un payload shape Google Sheets API (liste de lignes avec header).

    Utilise pour la fixture golden (AI-54) : simule la reponse de values.get.
    La premiere liste est le header ; les suivantes sont les lignes de donnees.
    """
    if end_date is None:
        end_date = date.today()
    if rng is None:
        rng = random.Random(_RNG_SEED)

    rows = generate_rows(days=days, end_date=end_date, rng=rng)
    if not rows:
        return [["Date", "Canal", "Budget", "Objectif CA", "Objectif Conversions"]]

    header = ["Date", "Canal", "Budget", "Objectif CA", "Objectif Conversions"]
    api_rows: list[list[str]] = [header]
    for r in rows:
        api_rows.append(
            [
                r["date"],
                r["sheet_row_id"],
                str(r["budget_declared"]),
                str(r["target_revenue"]),
                str(r["target_conversions"]),
            ]
        )
    return api_rows


# Le column_mapping correspondant au payload genere par generate_api_payload().
SEED_COLUMN_MAPPING: dict = {
    "date_column": "Date",
    "row_id_column": "Canal",
    "metric_columns": {
        "Budget": "budget_declared",
        "Objectif CA": "target_revenue",
        "Objectif Conversions": "target_conversions",
    },
}


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
    parser = argparse.ArgumentParser(description="Generate Google Sheets seed CSV")
    parser.add_argument("--out", default=None, help="Output CSV path")
    parser.add_argument("--days", type=int, default=40, help="Number of days to seed")
    args = parser.parse_args()

    here = Path(__file__).parent
    out = args.out or str(here / "google_sheets_seed.csv")
    rows = generate_rows(days=args.days)
    write_csv(rows, out)

    total_budget = sum(r["budget_declared"] for r in rows)
    total_rev_target = sum(r["target_revenue"] for r in rows)
    print(f"Generated {len(rows)} rows ({args.days} days x {len(CHANNELS)} channels) -> {out}")
    print(
        f"total budget_declared: {total_budget:.2f}  "
        f"total target_revenue: {total_rev_target:.2f}"
    )


if __name__ == "__main__":
    main()
