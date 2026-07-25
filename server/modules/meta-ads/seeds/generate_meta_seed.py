"""Meta Ads seed generator — review-15-9 F-1 (Epic 15, connecteurs vague 1).

Produces the raw Meta rows landed by load_meta_seed.py. Mirrors the tiktok-ads seed
generator shape EXACTLY (generate_rows = campaign grain only, default/retro-compat;
generate_multigrain_rows = the three grains coexisting so the mart exercises the
data_level filter that fixes the F-1 double-count).

review-15-9 F-1: the campaign grain rows carry data_level='CAMPAIGN'. The multigrain
generator emits CAMPAIGN + ADSET + CREATIVE rows for the same campaigns/days, each
stamped its data_level, so per (project, date, metric):
    SUM over campaign grain == SUM over adset grain == SUM over creative grain.
This is precisely the coexistence that double-counted the campaign_id series before
the data_level filter existed.

RETRO-COMPAT: generate_rows() keeps the EXACT campaign-grain roster the story-3.6 seed
used (2 campaigns/day, USD, same ids/amounts) so load_meta_seed.run() still lands 60
rows for 30 days (test_seed_to_mart_loop asserts meta_count == 60).

ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import date, timedelta
from pathlib import Path

# data_level values stamped per grain (mirror connector._DATA_LEVEL_BY_PROFILE).
DATA_LEVEL_CAMPAIGN = "CAMPAIGN"
DATA_LEVEL_ADSET = "ADSET"
DATA_LEVEL_CREATIVE = "CREATIVE"

# Two campaigns x two ads across the last N days -> a small but non-trivial set that
# exercises the campaign_id / adset_id / ad_id breakdowns in the mart. IDENTICAL to the
# story-3.6 roster so the campaign-grain seed stays byte-for-byte compatible (60 rows/30d).
_CAMPAIGNS = [
    {
        "campaign_id": "23851234500010001",
        "campaign_name": "Summer Sale - Prospecting",
        "adset_id": "23851234500020001",
        "adset_name": "FR 25-44 Interests",
        "ad_id": "23851234500030001",
        "creative_id": "9001",
        "spend": 250.75,
        "impressions": 48200,
        "clicks": 1310,
        "conversions": 42,
    },
    {
        "campaign_id": "23851234500010002",
        "campaign_name": "Summer Sale - Retargeting",
        "adset_id": "23851234500020002",
        "adset_name": "Cart Abandoners 30d",
        "ad_id": "23851234500030002",
        "creative_id": "9002",
        "spend": 118.40,
        "impressions": 12040,
        "clicks": 640,
        "conversions": 58,
    },
]

# review-15-9 F-1: each campaign fans out into this many ad sets, each into this many
# creatives. The creative-grain rows are generated first and rolled up so the grains
# reconcile EXACTLY (campaign == sum of adsets == sum of creatives).
ADSETS_PER_CAMPAIGN = 2
CREATIVES_PER_ADSET = 2

COLUMNS = [
    "date",
    "data_level",
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "creative_id",
    "spend",
    "impressions",
    "clicks",
    "conversions",
    "cost_source_currency",
]


def generate_rows(
    days: int = 30, project_id: str = "default", currency: str = "USD"
) -> list[dict]:
    """Return deterministic campaign-grain Meta rows for the last *days* days.

    Each row is stamped data_level='CAMPAIGN' (F-1). This is the retro-compatible
    default the story-3.6 loader used; for the three-grain coexistence seed (the
    double-count reconciliation case) use generate_multigrain_rows().
    """
    rows: list[dict] = []
    end = date.today() - timedelta(days=1)
    for offset in range(days):
        d = (end - timedelta(days=offset)).isoformat()
        for c in _CAMPAIGNS:
            rows.append(
                {
                    **c,
                    "date": d,
                    "data_level": DATA_LEVEL_CAMPAIGN,
                    "project_id": project_id,
                    "cost_source_currency": currency,
                }
            )
    return rows


def generate_multigrain_rows(
    days: int = 30, project_id: str = "default", currency: str = "USD"
) -> list[dict]:
    """Return the THREE grains (campaign / adset / creative daily) COEXISTING in one set.

    review-15-9 F-1: this is the case that double-counted the campaign_id series before
    the data_level filter existed. The creative-grain rows are generated first; the adset
    and campaign grains are EXACT roll-ups, so per (project, date, metric):
        SUM over campaign grain == SUM over adset grain == SUM over creative grain.
    Each grain is stamped its data_level so the mart reads only its own rows.
    """
    rows: list[dict] = []
    end = date.today() - timedelta(days=1)
    for offset in range(days):
        d = (end - timedelta(days=offset)).isoformat()
        for c in _CAMPAIGNS:
            # review-15-9 F-1: distinct campaign_id suffix ('9' prefix) so the multigrain
            # campaign-grain rows NEVER share a (project, date, data_level, campaign_id)
            # grain key with the flat campaign-grain seed (generate_rows). This keeps the
            # two seeds collision-free under the staging QUALIFY (no pull-ordering
            # dependency) while both still exercise the campaign_id series.
            campaign_id = f"9{c['campaign_id']}"
            campaign_name = f"{c['campaign_name']} (multigrain)"
            camp_spend = 0.0
            camp_impr = camp_clicks = camp_conv = 0
            adset_rows: list[dict] = []
            creative_rows: list[dict] = []

            for as_ix in range(ADSETS_PER_CAMPAIGN):
                adset_id = f"9{c['adset_id']}{as_ix + 1:02d}"
                adset_name = f"{c['adset_name']} / AS{as_ix + 1}"
                as_spend = 0.0
                as_impr = as_clicks = as_conv = 0

                # Split the campaign daily totals evenly across N creatives (integer
                # roll-up so campaign == sum of adsets == sum of creatives EXACTLY).
                n_creatives = CREATIVES_PER_ADSET
                for cr_ix in range(n_creatives):
                    creative_id = f"9{c['creative_id']}{as_ix + 1}{cr_ix + 1}"
                    ad_id = f"9{c['ad_id']}{as_ix + 1:02d}{cr_ix + 1:02d}"
                    denom = ADSETS_PER_CAMPAIGN * CREATIVES_PER_ADSET
                    spend = round(c["spend"] / denom, 2)
                    impressions = c["impressions"] // denom
                    clicks = c["clicks"] // denom
                    conversions = c["conversions"] // denom

                    creative_rows.append(
                        {
                            "date": d,
                            "data_level": DATA_LEVEL_CREATIVE,
                            "campaign_id": campaign_id,
                            "campaign_name": campaign_name,
                            "adset_id": adset_id,
                            "adset_name": adset_name,
                            "ad_id": ad_id,
                            "creative_id": creative_id,
                            "spend": spend,
                            "impressions": impressions,
                            "clicks": clicks,
                            "conversions": conversions,
                            "project_id": project_id,
                            "cost_source_currency": currency,
                        }
                    )
                    as_spend += spend
                    as_impr += impressions
                    as_clicks += clicks
                    as_conv += conversions

                adset_rows.append(
                    {
                        "date": d,
                        "data_level": DATA_LEVEL_ADSET,
                        "campaign_id": campaign_id,
                        "campaign_name": campaign_name,
                        "adset_id": adset_id,
                        "adset_name": adset_name,
                        # Adset-grain: no creative detail (None -> NULL).
                        "ad_id": None,
                        "creative_id": None,
                        "spend": round(as_spend, 2),
                        "impressions": as_impr,
                        "clicks": as_clicks,
                        "conversions": as_conv,
                        "project_id": project_id,
                        "cost_source_currency": currency,
                    }
                )
                camp_spend += as_spend
                camp_impr += as_impr
                camp_clicks += as_clicks
                camp_conv += as_conv

            # Campaign-grain row = exact roll-up of its adsets (== sum of its creatives).
            rows.append(
                {
                    "date": d,
                    "data_level": DATA_LEVEL_CAMPAIGN,
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "adset_id": None,
                    "adset_name": None,
                    "ad_id": None,
                    "creative_id": None,
                    "spend": round(camp_spend, 2),
                    "impressions": camp_impr,
                    "clicks": camp_clicks,
                    "conversions": camp_conv,
                    "project_id": project_id,
                    "cost_source_currency": currency,
                }
            )
            rows.extend(adset_rows)
            rows.extend(creative_rows)
    return rows


def write_csv(rows: list[dict], path: str | os.PathLike) -> None:
    """Write rows to *path* as a CSV with header (None -> blank cell)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            row = {k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS}
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Meta Ads seed CSV")
    parser.add_argument("--out", default=None, help="Output CSV path (defaults next to this file)")
    parser.add_argument("--days", type=int, default=30, help="Number of days to seed")
    parser.add_argument(
        "--grains",
        choices=["campaign", "multi"],
        default="campaign",
        help="campaign = campaign grain only (default); multi = 3 grains coexisting (F-1)",
    )
    args = parser.parse_args()

    here = Path(__file__).parent
    out = args.out or str(here / "meta_ads_seed.csv")
    if args.grains == "campaign":
        rows = generate_rows(days=args.days)
    else:
        rows = generate_multigrain_rows(days=args.days)
    write_csv(rows, out)
    # Campaign-grain rows carry the reconciled day totals -> sum only them to avoid
    # triple-counting across coexisting grains in this human-readable summary.
    camp_rows = [r for r in rows if r["data_level"] == DATA_LEVEL_CAMPAIGN]
    total_spend = sum(r["spend"] for r in camp_rows)
    total_conv = sum(r["conversions"] for r in camp_rows)
    print(f"Generated {len(rows)} rows ({args.grains} grains) -> {out}")
    print(f"campaign-grain total spend: {total_spend:.2f}  total conversions: {total_conv}")


if __name__ == "__main__":
    main()
