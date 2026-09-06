"""Deterministic FX conflict-resolution seeder for Story 13.2 dbt tests.

Story 13.2 (AC11 / AD-6). The table mirror.fx_source_currency_bindings is written
exclusively by Postgres in production (AD-8) and mirrored to DuckDB by
mirror_sync.py. In dev/CI (seed-only mode) the table is empty, which makes
dbt/tests/test_fx_resolution_applied.sql trivially green (0 overridden rows).
This script inserts a deterministic fixture so the reconciliation check is
actually exercised.

Fixture (single resolution, project 'default'):
  project_id             = 'default'
  target_field           = 'cost'
  source_module          = 'meta-ads'
  resolved_source_currency = 'USD'

Context: the meta-ads seed rows use cost_source_currency = 'EUR'. The
resolution overrides that to 'USD'. The fx_rates seed has USD->EUR = 0.92 with
valid_from=2020-01-01 / valid_to=2099-12-31.  Therefore, for any meta-ads row
with cost_source_value V, after staging:

    cost = V * 0.92     (because COALESCE('USD', 'EUR') = 'USD' -> rate 0.92)

The stg_meta_ads_daily model uses:
    COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
in the JOIN fx key, so a resolved_source_currency='USD' makes cost differ from
cost_source_value * 1.0 (the EUR identity rate) and instead use 0.92.

For test_fx_resolution_applied.sql to see overridden_rows:
  1. staged.cost_source_currency must be 'EUR' (raw seed value).
  2. resolution.resolved_source_currency must be 'USD' != 'EUR'.
  3. fx.from_currency='USD', fx.to_currency='EUR', fx.rate=0.92.
  4. cost must equal cost_source_value * 0.92 within 0.01% tolerance.

Convention (same as dbt/seeds/mediaplan/seed_plan_mirror.py):
    uv run python dbt/seeds/fx/seed_fx_source_currency_bindings.py --duckdb-path /path/to/dev.duckdb

The orchestrator runs this BEFORE `dbt build` on the same DuckDB file.

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import argparse
import os

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------

# LE CONFLIT VIT SUR SON PROPRE PROJET, ET C'EST LA REPARATION D'AI-163.
#
# Il portait sur `default`, dont les lignes meta landent USD -- et la resolution
# declarait USD aussi. `resolved == raw` : aucune ligne surchargee, la garde de
# cardinalite du test tirait, et personne ne pouvait le reparer en changeant la
# devise de `default` sans vider la population cross-devise que
# `test_meta_cost_normalization` exige (mesure 2026-08-08).
#
# Les deux tests veulent le taux de DEUX devises differentes sur les memes lignes :
# le voisin veut celui de la devise RAW, celui-ci celui de la RESOLUE. Il n'existe
# donc aucune devise unique qui les satisfasse, et la seule sortie est une
# population DISJOINTE :
#
#   default          raw USD, canonique EUR, AUCUNE resolution  -> le voisin
#   fx_conflict_dev  raw EUR, canonique EUR, resolution USD     -> ce test
#
# `fx_conflict_dev` est invisible pour le voisin parce que sa devise raw EGALE sa
# devise canonique, donc il n'entre pas dans sa population cross-devise.
PROJECT_ID = "fx_conflict_dev"
CONFLICT_RAW_CURRENCY = "EUR"
TARGET_FIELD = "cost"
SOURCE_MODULE = "meta-ads"
RESOLVED_SOURCE_CURRENCY = "USD"
DECIDED_BY = "seed_fx_13_2"
NOTE = "Story 13.2 test fixture -- USD override to exercise 0.92 rate path"

# DDL mirrors what mirror_sync projects from `app.fx_source_currency_bindings_v`
# (migration 282), NOT the dethroned table of migration 053. The relation gained
# `rule_set_id` / `rule_set_version_id` and lost the surrogate `id`: a declaration
# is now identified by the version that published it.
_MIRROR_DDL = """
CREATE SCHEMA IF NOT EXISTS mirror;

CREATE TABLE IF NOT EXISTS mirror.fx_source_currency_bindings (
    project_id                VARCHAR NOT NULL,
    target_field              VARCHAR NOT NULL,
    source_module             VARCHAR NOT NULL,
    resolved_source_currency  VARCHAR NOT NULL,
    decided_by                VARCHAR NOT NULL,
    decided_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note                      VARCHAR,
    rule_set_id               VARCHAR,
    rule_set_version_id       VARCHAR
);
"""

_INSERT_RESOLUTION = """
INSERT INTO mirror.fx_source_currency_bindings
    (project_id, target_field, source_module,
     resolved_source_currency, decided_by, decided_at, note,
     rule_set_id, rule_set_version_id)
VALUES (?, ?, ?, ?, ?, NOW(), ?, 'grs_SEEDFIXTURE', 'grsv_SEEDFIXTURE')
"""


def _seed_conflict_project_rows(duckdb_path: str) -> int:
    """Lander les lignes meta du projet de conflit, en EUR.

    Par le loader du connecteur plutot qu'un INSERT a la main : le schema de
    `raw_meta_ads_daily` appartient a meta-ads, et une fixture qui le recopie
    devient la seconde definition d'une relation -- exactement la classe qui a
    tenu 40 tests rouges ailleurs dans ce depot.
    """
    import importlib.util  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    loader_path = (
        Path(__file__).resolve().parents[3]
        / "server" / "modules" / "meta-ads" / "seeds" / "load_meta_seed.py"
    )
    spec = importlib.util.spec_from_file_location("load_meta_seed", loader_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _pull_id, count = module.run(
        duckdb_path=duckdb_path,
        project_id=PROJECT_ID,
        currency=CONFLICT_RAW_CURRENCY,
    )
    return count


def run(duckdb_path: str) -> None:
    """Idempotent: delete this fixture's row, then re-insert."""
    import duckdb  # noqa: PLC0415

    _seed_conflict_project_rows(duckdb_path)

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_MIRROR_DDL)

        # LE PROJET DOIT EXISTER DANS project_preferences, sinon `dim_project` ne lui
        # donne AUCUNE devise canonique et la jointure de taux du staging -- qui porte
        # `fx.to_currency = dp.canonical_currency` -- ne peut pas aboutir : `fx_rate`
        # rend NULL sur les 60 lignes et le test echoue en accusant la resolution.
        # Mesure du 2026-08-08, avant cette ligne : `EUR | fx_rate=None | 60`.
        # `create_mirror` ne seme que `default` ; les seeders fee/tax posent chacun
        # leurs propres projets, et celui-ci fait de meme.
        con.execute(
            "DELETE FROM mirror.project_preferences WHERE project_id = ?", [PROJECT_ID]
        )
        con.execute(
            "INSERT INTO mirror.project_preferences "
            "(project_id, canonical_currency, reporting_timezone, created_at, updated_at, "
            " fee_tax_alignment_enabled) "
            "VALUES (?, ?, 'Europe/Paris', current_timestamp, current_timestamp, FALSE)",
            [PROJECT_ID, CONFLICT_RAW_CURRENCY],
        )

        # Idempotent: remove our fixture row only (id=1, specific grain).
        con.execute(
            """
            DELETE FROM mirror.fx_source_currency_bindings
            WHERE project_id   = ?
              AND target_field  = ?
              AND source_module = ?
            """,
            [PROJECT_ID, TARGET_FIELD, SOURCE_MODULE],
        )

        con.execute(
            _INSERT_RESOLUTION,
            [
                PROJECT_ID,
                TARGET_FIELD,
                SOURCE_MODULE,
                RESOLVED_SOURCE_CURRENCY,
                DECIDED_BY,
                NOTE,
            ],
        )

        count = con.execute(
            "SELECT COUNT(*) FROM mirror.fx_source_currency_bindings"
        ).fetchone()[0]

        print(
            "seed_fx_source_currency_bindings OK  "
            f"project={PROJECT_ID}  field={TARGET_FIELD}  module={SOURCE_MODULE}  "
            f"resolved_currency={RESOLVED_SOURCE_CURRENCY}  "
            f"total_rows_in_table={count}  -> {duckdb_path}"
        )
        print(
            "  Test proof: meta-ads rows have cost_source_currency='EUR'; "
            "resolution forces 'USD' -> rate 0.92; "
            "test_fx_resolution_applied will see overridden_rows>0 and verify "
            "cost == cost_source_value * 0.92 within 0.01%."
        )
    finally:
        con.close()


def main() -> None:
    default_duckdb = os.environ.get("TOOROW_DUCKDB_PATH", "")
    parser = argparse.ArgumentParser(
        description=(
            "Seed mirror.fx_source_currency_bindings for Story 13.2 dbt test. "
            "Run BEFORE `dbt build` on the same DuckDB path."
        )
    )
    parser.add_argument(
        "--duckdb-path",
        default=default_duckdb,
        help="DuckDB file path (or TOOROW_DUCKDB_PATH env var)",
    )
    args = parser.parse_args()
    if not args.duckdb_path:
        raise SystemExit("ERROR: --duckdb-path (or TOOROW_DUCKDB_PATH) is required")
    run(args.duckdb_path)


if __name__ == "__main__":
    main()
