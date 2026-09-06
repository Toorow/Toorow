"""Land EVERY connector's seed into one DuckDB file, so `dbt build` can succeed.

WHY THIS EXISTS (measured 2026-08-04). ``dbt/models/marts/fact_daily_kpi.sql``
``ref()``s ~30 staging models spread over the module dbt trees. dbt refuses to
build a model whose source table is absent, so an unselected ``dbt build``
succeeds ONLY if every ``raw_*`` table those staging models read has been
landed first. The two fixtures that need this (``test_seed_to_mart_loop.py`` and
``test_org_mart_equivalence_24_4.py``) each hand-listed the loaders they knew
about -- 15 of them -- and every connector that landed afterwards silently broke
the build with ``Catalog Error: Table with name raw_<x> does not exist``.

``test_seed_to_mart_loop.py`` carried a skip-guard against exactly that, but it
tested for seed MATERIAL (``any file under seeds/``) rather than for a loader
being INVOKED, so it read 38/38 covered and stopped firing while the build was
red. A guard that measures the wrong thing is worse than none: it reports
covered.

So the list is DERIVED, never written: every ``server/modules/*/seeds/load*.py``
is discovered and invoked, and its ``run()`` signature is introspected to supply
the arguments it declares. Add a connector with a loader and it is seeded here
the same day, with no edit to any fixture.

Entry point of a loader, in the order this driver tries them -- the three shapes
that exist in the tree, measured, not assumed:

  1. ``run(duckdb_path=..., ...)``   -- 38 of 40 loaders;
  2. ``load_seed(duckdb_path, ...)`` -- google-ads;
  3. the argparse CLI ``--duckdb-path`` -- microsoft-ads, which exposes only
     ``main()``. Driven as a subprocess rather than by editing the loader: the
     seed loaders belong to their connectors, and a fixture has no business
     rewriting 40 of them to suit itself.

Optional parameters this driver fills when the callable declares them: ``days``,
``project_id``, ``grains`` (asks for ``"multi"`` so multi-grain connectors land
all their ``data_level`` rows), and the GA4-specific ``csv_path`` / ``mode`` /
``bq_project`` / ``profile``.

Ordering: ``stripe`` must run AFTER ``shopify`` -- its generator imports the
Shopify seed to correlate charges with orders (non-tautological dedup proof,
story 15.7). That is the ONLY ordering constraint; it is declared below rather
than implied by directory order.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
from datetime import date
from pathlib import Path
from typing import Any

# server/tests/integration/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
MODULES_DIR = REPO_ROOT / "server" / "modules"
GA4_SEEDS = MODULES_DIR / "google-analytics" / "seeds"

#: AI-213 (2026-08-17): the ONE corpus anchor this driver threads to every loader
#: that declares an ``end_date`` seam. Before this, only google-analytics was
#: anchored (AI-66) and the eight other date-emitting generators defaulted to
#: ``date.today()``, so the mart's window was the day it was built --
#: machine-day-local by construction, which is what made the evals fixtures
#: underivable. The default is the anchor two generators already declared
#: (``DEFAULT_SEED_END_DATE = 2026-07-19``) and nobody passed; the env seam is
#: the same one AI-66 chose (``TOOROW_SEED_END_DATE``). The google-analytics
#: family keeps its own internal anchor (2026-07-15, pinned to the evals
#: as_of_anchor and the checked-in GA4 CSVs) and deliberately declares no
#: ``end_date`` seam, so this driver never moves it.
SEED_END_DATE: date = date.fromisoformat(
    os.environ.get("TOOROW_SEED_END_DATE", "2026-07-19")
)

#: Loaders that must run after another one (see module docstring).
_AFTER: dict[str, str] = {"stripe": "shopify"}

#: GA4 loaders take a CSV + engine mode instead of generating rows themselves.
#: Mapping loader filename -> (csv filename, profile or None).
_GA4_ARGS: dict[str, tuple[str, str | None]] = {
    "load_seed.py": ("ga4_seed.csv", None),
    "load_seed_user_type.py": ("ga4_user_type_seed.csv", None),
    "load_seed_pages.py": ("ga4_landing_seed.csv", "landing"),
    "load_seed_acquisition.py": ("ga4_acquisition_session_seed.csv", "session"),
}

#: Extra invocations of a GA4 loader with a DIFFERENT profile/CSV. The staging
#: models read one raw table per profile, so a single call would leave the others
#: absent and the build red.
_GA4_EXTRA: list[tuple[str, str, str]] = [
    ("load_seed_pages.py", "ga4_paths_seed.csv", "paths"),
    ("load_seed_acquisition.py", "ga4_acquisition_first_user_seed.csv", "first_user"),
]


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_loaders() -> list[tuple[str, Path]]:
    """Return ``(connector_name, loader_path)`` for every seed loader, ordered.

    Derived from the tree, so a new connector needs no edit here. ``_AFTER``
    constraints are applied last.
    """
    found: list[tuple[str, Path]] = []
    for seeds_dir in sorted(MODULES_DIR.glob("*/seeds")):
        for loader in sorted(seeds_dir.glob("load*.py")):
            found.append((seeds_dir.parent.name, loader))

    def sort_key(item: tuple[str, Path]) -> tuple[int, str, str]:
        connector, path = item
        # A connector that must follow another sorts into a later bucket.
        return (1 if connector in _AFTER else 0, connector, path.name)

    return sorted(found, key=sort_key)


def _call_args(fn, connector: str, loader_path: Path, csv_path: str | None,
               profile: str | None, duckdb_path: str, project_id: str) -> dict[str, Any]:
    """Build the kwargs *fn* declares, from what this driver knows how to supply."""
    params = inspect.signature(fn).parameters
    kwargs: dict[str, Any] = {"duckdb_path": duckdb_path}
    if "project_id" in params:
        kwargs["project_id"] = project_id
    if "grains" in params:
        # Multi-grain connectors (meta-ads, tiktok-ads, linkedin-ads) land every
        # data_level; the staging models filter on it and would otherwise be empty.
        kwargs["grains"] = "multi"
    if "end_date" in params:
        # AI-213: every loader whose generator emits dated rows declares this
        # seam; the driver supplies the single corpus anchor so the same corpus
        # rebuilds on any machine, any day. Loaders whose rows come from a
        # checked-in fixture (fixed dates) have no seam and need none.
        kwargs["end_date"] = SEED_END_DATE
    if "mode" in params:
        kwargs["mode"] = "duckdb"
    if "bq_project" in params:
        kwargs["bq_project"] = None
    if "csv_path" in params:
        if csv_path is None:
            raise AssertionError(
                f"{connector}/{loader_path.name} declares csv_path and this driver "
                "has no CSV for it -- add it to _GA4_ARGS"
            )
        kwargs["csv_path"] = csv_path
    if "profile" in params:
        if profile is None:
            raise AssertionError(
                f"{connector}/{loader_path.name} declares profile and this driver "
                "has no value for it -- add it to _GA4_ARGS"
            )
        kwargs["profile"] = profile
    return kwargs


def _run_cli(loader_path: Path, duckdb_path: str, project_id: str) -> int:
    """Drive an argparse-only loader (``main()``, no importable entry) as a CLI."""
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    completed = subprocess.run(
        [sys.executable, str(loader_path),
         "--duckdb-path", duckdb_path, "--project-id", project_id],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{loader_path.parent.parent.name}/{loader_path.name} CLI failed:\n"
            + completed.stdout + completed.stderr
        )
    return -1


def seed_all(duckdb_path: str, project_id: str = "default") -> dict[str, dict[str, Any]]:
    """Run every discovered loader against *duckdb_path*.

    Returns ``{connector/loader: {"rows": int, "pull_id": str | None}}``.
    ``rows`` is ``-1`` and ``pull_id`` is ``None`` when the loader does not report
    them -- the return shape of a loader is not part of the contract, only the
    call signature is (see module docstring).

    Raises on the FIRST loader failure: a silently skipped loader is the defect
    this module exists to remove, so it must never be swallowed.
    """
    landed: dict[str, dict[str, Any]] = {}

    invocations: list[tuple[str, Path, str | None, str | None]] = []
    for connector, loader_path in discover_loaders():
        csv_name, profile = _GA4_ARGS.get(loader_path.name, (None, None))
        csv_path = str(GA4_SEEDS / csv_name) if csv_name else None
        invocations.append((connector, loader_path, csv_path, profile))
    for loader_name, csv_name, profile in _GA4_EXTRA:
        invocations.append(
            ("google-analytics", GA4_SEEDS / loader_name, str(GA4_SEEDS / csv_name), profile)
        )

    for connector, loader_path, csv_path, profile in invocations:
        module = _import(f"seedloader_{connector}_{loader_path.stem}", loader_path)
        entry = getattr(module, "run", None) or getattr(module, "load_seed", None)
        if entry is None:
            result = _run_cli(loader_path, duckdb_path, project_id)
        else:
            kwargs = _call_args(
                entry, connector, loader_path, csv_path, profile, duckdb_path, project_id
            )
            result = entry(**kwargs)
        key = f"{connector}/{loader_path.stem}" + (f":{profile}" if profile else "")
        # The common shape is ``(pull_id, rows)``; some loaders return the row
        # count alone, and the CLI-driven one returns nothing measurable.
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
            landed[key] = {"rows": result[1], "pull_id": str(result[0])}
        elif isinstance(result, int):
            landed[key] = {"rows": result, "pull_id": None}
        else:
            landed[key] = {"rows": -1, "pull_id": None}

    # INSIDE seed_all, not beside it. Every caller would otherwise have to
    # remember a third call, and a seeding step that can be forgotten is the
    # exact shape of the defects this driver was written to end (AI-164, AI-213):
    # a warehouse that looks seeded and is not what the corpus was pinned to.
    anchor_loaded_at(duckdb_path)

    return landed


def anchor_loaded_at(duckdb_path: str) -> dict[str, int]:
    """Give every landed row a `loaded_at` that follows its own date (AI-312).

    WHAT THIS ENDS. 43 loaders stamp `loaded_at = datetime.now(UTC)`, one value
    per run, so the seeded warehouse says every row of May, June and July was
    collected at the instant the seed was built. Two consequences, and the second
    is the one that cost a whole surface:

      * the provenance is implausible on its face -- a row dated 2026-05-01
        "loaded" three months later;
      * every as-of question is unanswerable. The corpus asks seven of them, and
        EVERY ONE anchors `as_of` to the END of its own date range at 23:59:59Z
        -- `as_of_gsc_clicks_historical` reads May with `as_of=2026-05-31`,
        `as_of_ga4_sessions_historical_replay` reads June with `as_of=2026-06-30`.
        That is the shape of daily collection, and it is the shape this seed did
        not have: `loaded_at <= as_of` matched NOTHING, so all seven returned an
        empty answer against a fixture that expects the window.

    THE RULE, STATED RATHER THAN INFERRED: this seed simulates a connector that
    collects once a day, at the end of the day. A row dated D was loaded at
    `D T23:59:59Z`. Rows with no date of their own (snapshots) take the corpus
    anchor's end of day -- they belong to the state of the world at the end of
    the seeded window, and a machine timestamp would make them the only
    non-reproducible thing left in the warehouse.

    HERE AND NOT IN THE 43 LOADERS, because a loader passes ONE `loaded_at` to
    its own `load_duckdb`: making it per-row would change 43 signatures to say
    something none of them decides. The rule belongs to the seed as a whole, and
    the driver is where the seed as a whole is assembled. A loader run alone
    keeps its wall-clock stamp, which is the honest thing for a one-off load.

    Returns ``{relation: rows_updated}`` -- a silent no-op would be
    indistinguishable from a driver that never ran.
    """
    import duckdb

    anchored: dict[str, int] = {}
    # THE WRITTEN FORM IS THE PRODUCT'S, to the character: 27 chars, six
    # fractional digits, trailing `Z` (measured on production `raw_youtube_daily`
    # 2026-08-23). `loaded_at` is a STRING everywhere, so `loaded_at <= as_of` is
    # a LEXICOGRAPHIC comparison and only a fixed-width canonical form makes it
    # chronological -- a seed in any other spelling compares wrong at exactly the
    # boundary the as-of questions ask about.
    #
    # MIDDAY, not the last microsecond of the day: a collection stamped at the
    # very instant an `as_of` names sits on a knife edge where `<=` decides the
    # answer. Noon is unambiguously inside its own day and outside the previous
    # one, which is the only property the as-of questions need.
    fallback = f"{SEED_END_DATE.isoformat()}T12:00:00.000000Z"
    con = duckdb.connect(duckdb_path)
    try:
        rows = con.execute(
            "SELECT table_name, list(column_name) FROM information_schema.columns "
            "WHERE table_schema = 'main' GROUP BY table_name"
        ).fetchall()
        for table, columns in rows:
            cols = set(columns or ())
            if "loaded_at" not in cols:
                continue
            if "date" in cols:
                # `date` is a VARCHAR ISO day in every landing relation. The
                # guard keeps a malformed or NULL date from producing a
                # `loaded_at` that sorts anywhere: such a row takes the anchor.
                sql = (
                    f'UPDATE main."{table}" SET loaded_at = CASE '
                    "WHEN date IS NOT NULL AND length(CAST(date AS VARCHAR)) = 10 "
                    "THEN CAST(date AS VARCHAR) || 'T12:00:00.000000Z' ELSE ? END"
                )
                params = [fallback]
            else:
                sql = f'UPDATE main."{table}" SET loaded_at = ?'
                params = [fallback]
            con.execute(sql, params)
            anchored[table] = con.execute(
                f'SELECT COUNT(*) FROM main."{table}"'
            ).fetchone()[0]
    finally:
        con.close()
    return anchored


def create_mirror(db_path: str) -> None:
    """Create the mirror.* relations dbt declares as sources, EMPTY but complete.

    In production mirror_sync.py lands these from Postgres (AD-8, sole writer).
    A local fixture has no Postgres, so it must create them itself -- and every
    fixture that did so kept its OWN partial copy, which is why one of them
    carried an 8-column project_preferences while the live table has 27 and
    fee_tax_country_resolution reads a column outside those 8. ONE definition,
    here, shared.
    """
    import duckdb

    con = duckdb.connect(db_path)
    con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    # mirror_sync syncs app.project_preferences with SELECT *, so the fixture must
    # carry EVERY column or a model reading a newer one dies with `Table "p" does
    # not have a column named ...` (measured on geographic_mode, which
    # fee_tax_country_resolution reads). Column list taken from the live
    # information_schema on 2026-08-04 (27 columns), not from a migration number
    # -- the applied set is not linear.
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.project_preferences ("
        "project_id VARCHAR, canonical_currency VARCHAR, reporting_timezone VARCHAR, "
        "created_at TIMESTAMP, updated_at TIMESTAMP, "
        "verification_source_type VARCHAR, verification_source_id VARCHAR, "
        "lead_event_name VARCHAR, max_projection_grain_cardinality BIGINT, "
        "max_projection_scan_bytes BIGINT, max_row_count_delta_pct DOUBLE, "
        "allow_empty_publication BOOLEAN, mediaplan_overrun_threshold DOUBLE, "
        "mediaplan_underdelivery_threshold DOUBLE, max_rejected_row_pct DOUBLE, "
        "quota_allow_hourly_sync BOOLEAN, rollback_window_hours BIGINT, "
        "geographic_mode VARCHAR, local_market_country_codes VARCHAR[], "
        "local_markets VARCHAR, fee_tax_alignment_enabled BOOLEAN, "
        "canonical_currency_origin VARCHAR, canonical_currency_confirmation_status VARCHAR, "
        "reporting_timezone_origin VARCHAR, reporting_timezone_confirmation_status VARCHAR, "
        "verification_source_origin VARCHAR, verification_source_confirmation_status VARCHAR)"
    )
    # Named columns, never positional: a column added upstream must not silently
    # shift this row's currency into the timezone.
    con.execute(
        "INSERT INTO mirror.project_preferences "
        "(project_id, canonical_currency, reporting_timezone, created_at, updated_at, "
        " fee_tax_alignment_enabled) VALUES "
        "('default','EUR','Europe/Paris',current_timestamp,current_timestamp,FALSE)"
    )
    con.execute(
        # Sept colonnes manquaient ici aussi (mesure 2026-08-08 : `app.context_events`
        # en porte 15). `platform`, `value` et `source` sont declarees dans
        # `sources_mirror.yml` depuis l'epic 31 ; les quatre dernieres viennent du
        # rattachement au Datastream. Meme raison qu'au-dessus : le mirroir syncre
        # SELECT *, donc une forme courte est une bombe a retardement, pas une
        # economie.
        # `metric` : migration 322. La metrique qu'un evenement NOMME, ou NULL
        # pour « toutes ». Les deux marches scopees la comparent a la metrique de
        # l'assertion ; absente du mirroir, la dimension est declaree non
        # verifiee plutot que presentee comme verifiee.
        "CREATE TABLE IF NOT EXISTS mirror.context_events ("
        "id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR, "
        "description VARCHAR, created_by VARCHAR, created_at TIMESTAMP, "
        "platform VARCHAR, value DOUBLE, source VARCHAR, "
        "event_configuration_version_id VARCHAR, datastream_id VARCHAR, "
        "execution_id VARCHAR, binding_state VARCHAR, metric VARCHAR)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.connection_ref_dim ("
        "project_id VARCHAR, connector_name VARCHAR, display_name VARCHAR)"
    )
    # Story 13.2, re-sourced by 67.20: staging models LEFT JOIN
    # mirror.fx_source_currency_bindings (AD-6). Empty here -> JOIN matches nothing
    # -> COALESCE falls back to raw currency. Columns follow the `Columns:` line of
    # the source in sources_mirror.yml, which now names the governed projection
    # (migration 282) instead of the dethroned `fx_conflict_resolutions` table.
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.fx_source_currency_bindings ("
        "project_id VARCHAR, target_field VARCHAR, source_module VARCHAR, "
        "resolved_source_currency VARCHAR, decided_by VARCHAR, decided_at TIMESTAMP, "
        "note VARCHAR, rule_set_id VARCHAR, rule_set_version_id VARCHAR)"
    )
    # Story 67.13 (migration 305): the two relations that carry a POSED rate to
    # the thirteen staging models. EMPTY here, and the emptiness is the point --
    # it is the state of every Project that has posted no rate, and in that state
    # the cutover must convert at exactly the number the seed gave before it.
    # `test_fx_at_read_totals_unchanged` and `test_epic39_totals_bit_identical`
    # are what hold that. The posed path itself is exercised by the singular
    # tests, which pass their own fixture relations into
    # `toorow_fx_posed_resolution` rather than writing here -- a rate seeded into
    # this fixture would move the totals those two tests pin.
    # Columns follow the `Columns:` line of each source in sources_mirror.yml.
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.fx_posed_rates ("
        "project_id VARCHAR, observation_id VARCHAR, base_currency VARCHAR, "
        "quote_currency VARCHAR, rate DECIMAL(38,18), effective_date DATE, "
        "valid_from DATE, valid_to DATE, condition_key_count INTEGER, "
        "provider VARCHAR, rate_set_version_id VARCHAR)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.fx_posed_rate_conditions ("
        "observation_id VARCHAR, condition_key VARCHAR, condition_value VARCHAR)"
    )
    # Epic 41 / 22 mirror relations. sources_mirror.yml declares them and says in
    # so many words that "the DEV DuckDB seeder for these relations is 41.3's" --
    # it was never written, so every fixture that builds the whole project hit
    # `Catalog Error: Table with name fee_tax_rules does not exist` on
    # fee_tax_rules_effective. Created EMPTY on purpose: absence of a rule means
    # the ladder is inert (the models' documented fast path), which is exactly the
    # state a bare seed fixture should be in. Column names/order follow the
    # `Columns:` line of each source in sources_mirror.yml; types follow the casts
    # the models apply.
    for ddl in (
        "mirror.fee_tax_rules ("
        "id VARCHAR, project_id VARCHAR, scope_kind VARCHAR, scope_ref VARCHAR, "
        "category VARCHAR, form VARCHAR, rate DOUBLE, amount_micros BIGINT, "
        "cpm_micros BIGINT, currency VARCHAR, base_target VARCHAR, "
        "cascade_phase BIGINT, sequence_order BIGINT, effective_from DATE, "
        "effective_to DATE, status VARCHAR, origin VARCHAR, dedup_hash VARCHAR, "
        "label VARCHAR, created_by VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, "
        # LES 19 SUIVANTES MANQUAIENT, et les 22 ci-dessus etaient exactement les 22
        # PREMIERES de la vue -- un prefixe fige a la date ou ce bloc a ete ecrit,
        # pendant que 48.4 et le volet geographie en ajoutaient. `mirror_sync` syncre
        # `SELECT * FROM app.fee_tax_rules_dim_v` : 41 colonnes, mesurees le
        # 2026-08-08. Un modele qui lira l'une des 19 mourra EN LOCAL et pas en cloud,
        # ce qui ressemble trait pour trait a un defaut produit.
        # Les seeders inserent en colonnes NOMMEES, donc elargir ne les touche pas.
        "rule_set_id VARCHAR, rule_set_version_id VARCHAR, rule_set_content_hash VARCHAR, "
        "rule_key VARCHAR, ladder_position BIGINT, rounding VARCHAR, money_basis VARCHAR, "
        "authority_kind VARCHAR, jurisdiction_kind VARCHAR, jurisdiction_id VARCHAR, "
        "geography_hierarchy_version_id VARCHAR, geography_dependent BOOLEAN, "
        "rest_of_world_posture VARCHAR, unknown_posture VARCHAR, source_issuer VARCHAR, "
        "source_reference VARCHAR, source_reference_version VARCHAR, "
        "source_preset_version_id VARCHAR, source_published_on DATE)",
        "mirror.fee_tax_rule_conditions ("
        "rule_id VARCHAR, condition_key VARCHAR, condition_value VARCHAR)",
        "mirror.fee_tax_rule_tiers ("
        "rule_id VARCHAR, tier_index BIGINT, threshold_micros BIGINT, rate DOUBLE, "
        "mode VARCHAR)",
        "mirror.project_tax_fee_activation ("
        "project_id VARCHAR, project_configuration_version_id VARCHAR, "
        "capability_state VARCHAR, tax_fees_active BOOLEAN, rule_set_id VARCHAR, "
        "rule_set_version_id VARCHAR, rule_set_content_hash VARCHAR, rounding VARCHAR, "
        "default_money_basis VARCHAR, rule_count BIGINT, reporting_currency VARCHAR, "
        # `money_policy_content_hash` MANQUAIT, et ce n'est pas une colonne de plus :
        # c'est ce qui faisait mourir `dbt/seeds/feetax/seed_tax_fee_activation_mirror.py`
        # (12 colonnes declarees ici, 13 valeurs fournies la-bas). `CREATE TABLE IF NOT
        # EXISTS` fait gagner celui qui passe en premier -- ce fichier -- donc le seeder
        # ne pouvait pas corriger la forme, seulement s'y casser. Mesure du 2026-08-08
        # sur `app.project_tax_fee_activation_v` en production : 13 colonnes, la
        # derniere est celle-ci. Meme classe que le `project_preferences` a 8 colonnes
        # que la docstring de cette fonction raconte -- rouverte une relation plus loin.
        "money_policy_version_id VARCHAR, money_policy_content_hash VARCHAR)",
        "mirror.datastreams_dim ("
        "project_id VARCHAR, datastream_id VARCHAR, connector VARCHAR, "
        "data_role VARCHAR, source_kind VARCHAR)",
        "mirror.datastream_source_types ("
        "datastream_id VARCHAR, source_type VARCHAR, declared_by VARCHAR, "
        "declared_at TIMESTAMP)",
        "mirror.datastream_country_binding_dim ("
        "project_id VARCHAR, connector VARCHAR, datastream_id VARCHAR, market_id VARCHAR)",
        # Story 37.9 / migration 269: the published Country meaning. Column list is
        # app.country_market_projection_v's final SELECT, in its declared order --
        # mirror_sync syncs it with SELECT *. EMPTY means exactly one thing here:
        # "no governed Country meaning", which fee_tax_country_resolution reads as
        # "cannot decide", never as Global. Absent, the whole dbt run dies instead:
        # the relation is GUARDED by design (the mart refuses to build without it).
        "mirror.country_market_projection ("
        "project_id VARCHAR, registry_id VARCHAR, hierarchy_version_id VARCHAR, "
        "vocabulary_version_id VARCHAR, hierarchy_content_hash VARCHAR, "
        "country_code VARCHAR, market_id VARCHAR, market_label VARCHAR, "
        "market_kind VARCHAR, region_id VARCHAR, region_label VARCHAR, "
        "display_order BIGINT)",
        "mirror.media_plans ("
        "id VARCHAR, project_id VARCHAR, name VARCHAR, currency VARCHAR, "
        "created_by VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, "
        "archived_at TIMESTAMP)",
        "mirror.media_plan_versions ("
        "id VARCHAR, plan_id VARCHAR, version_number BIGINT, status VARCHAR, "
        "is_active BOOLEAN, source_note VARCHAR, created_by VARCHAR, created_at TIMESTAMP)",
        "mirror.media_plan_lines ("
        "id VARCHAR, version_id VARCHAR, line_key VARCHAR, label VARCHAR, "
        "channel VARCHAR, start_date DATE, end_date DATE, budget DOUBLE, "
        "buy_mode VARCHAR, is_plan_only BOOLEAN, sort_order BIGINT)",
        "mirror.plan_allocation_daily ("
        "version_id VARCHAR, line_id VARCHAR, day DATE, amount DOUBLE)",
        "mirror.plan_line_mappings ("
        "id VARCHAR, plan_id VARCHAR, line_key VARCHAR, connector VARCHAR, "
        "campaign_ref VARCHAR, split_weight DOUBLE, status VARCHAR, "
        "created_by VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP)",
        # Story 61.4: the confirmed Money Policy. Column list taken from
        # app.project_money_policy_v (migration 148), in its declared order --
        # mirror_sync syncs it with SELECT *, so a short shape here is the same
        # time bomb the project_tax_fee_activation comment above describes.
        "mirror.project_money_policy ("
        "project_id VARCHAR, money_policy_rule_set_id VARCHAR, "
        "money_policy_version_id VARCHAR, money_policy_content_hash VARCHAR, "
        "reporting_currency VARCHAR, reporting_currency_minor_unit BIGINT, "
        "money_rounding VARCHAR)",
    ):
        con.execute(f"CREATE TABLE IF NOT EXISTS {ddl}")
    # The 'default' Project HAS confirmed a Money Policy, and it names the same
    # currency its FX join targets. Without this row every plan-pacing figure in
    # the fixture would carry `money_policy_unconfirmed` -- a true statement about
    # a fixture nobody wrote deliberately, which is a poor thing to measure a mart
    # against. Named columns, never positional (same rule as project_preferences).
    con.execute(
        "INSERT INTO mirror.project_money_policy "
        "(project_id, money_policy_rule_set_id, money_policy_version_id, "
        " money_policy_content_hash, reporting_currency, "
        " reporting_currency_minor_unit, money_rounding) "
        "SELECT 'default','rs_money_EXAMPLE_LOCAL','rsv_money_EXAMPLE_LOCAL',"
        "       'sha256:EXAMPLE_LOCAL','EUR',2,'half_even' "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM mirror.project_money_policy WHERE project_id = 'default')"
    )
    con.close()


#: Les cinq seeders fee/tax, DANS L'ORDRE QUI EST LE LEUR. L'ordre n'est pas une
#: preference : `seed_tax_fee_activation_mirror` DERIVE l'activation des projets
#: que les trois premiers ont ecrits, et `seed_datastream_source_types_mirror` est
#: le pont de forme qui doit passer avant lui. Leur en-tete le dit chacun
#: ("Run it after the other fee/tax seeders").
_FEE_TAX_SEEDERS = (
    # Le seeder du conflit FX partage l'omission des cinq autres : il existe, rien ne
    # l'appelait, donc `mirror.fx_source_currency_bindings` restait vide et
    # `test_fx_resolution_applied` echouait sur sa garde de cardinalite AVANT meme la
    # question des devises (AI-163). Il passe en premier : il lande les lignes meta du
    # projet de conflit, et le reste de la chaine ne le contredit pas.
    "seed_fx_source_currency_bindings",
    "seed_fee_tax_mirror",
    "seed_fee_tax_revenue_mirror",
    "seed_fee_tax_verification",
    "seed_datastream_source_types_mirror",
    "seed_tax_fee_activation_mirror",
)


def seed_fee_tax(db_path: str) -> dict[str, str]:
    """Lancer les cinq seeders `dbt/seeds/feetax/` sur *db_path*.

    ILS EXISTAIENT ET PERSONNE NE LES APPELAIT. Ecrits le 2026-07-27, deterministes
    et idempotents (chaque insert est precede d'un DELETE de ses propres cles), ils
    peuplent les relations `mirror.fee_tax_*` que `create_mirror` cree vides. Sans
    eux `fee_tax_ladder_daily` rend zero ligne et TOUTE assertion ON/OFF d'epic 41
    passe trivialement -- un vert qui ne prouve rien, ce que leur propre en-tete
    annonce mot pour mot.

    AUCUN N'INVENTE DE DONNEE CLIENT : les projets sont nommes `feetax_dev_*` et les
    identifiants `*_EXAMPLE_LOCAL`.

    L'appel echoue fort. Un seeder qui meurt en silence rend la moitie des relations
    peuplees et l'autre vide, ce qui produit des rouges dbt dont la cause est ailleurs
    -- exactement ce qui a coute la mesure du 2026-08-05.
    """
    # Ils ne vivent pas tous dans le meme dossier -- le conflit FX est sous
    # `dbt/seeds/fx/`. Le chemin est CHERCHE plutot que suppose : un seeder
    # deplace doit lever ici, pas disparaitre en silence.
    seeds_root = REPO_ROOT / "dbt" / "seeds"
    ran: dict[str, str] = {}
    for name in _FEE_TAX_SEEDERS:
        candidates = sorted(seeds_root.glob(f"*/{name}.py"))
        if len(candidates) != 1:
            raise AssertionError(
                f"expected exactly one {name}.py under dbt/seeds/, "
                f"found {len(candidates)}"
            )
        path = candidates[0]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entry = getattr(module, "run", None) or getattr(module, "seed", None)
        if entry is None:
            raise AssertionError(f"fee/tax seeder {name} exposes neither run() nor seed()")
        # `seed()` prend un Path, `run()` une str -- la difference est historique et
        # se lit dans leur signature plutot que de se deviner.
        annotation = getattr(entry, "__annotations__", {}).get("duckdb_path")
        entry(Path(db_path) if annotation is Path else db_path)
        ran[name] = "ok"
    return ran
