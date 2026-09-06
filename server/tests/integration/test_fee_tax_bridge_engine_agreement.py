"""Story 41.2 -- the Python bridge and its SQL twin must AGREE, or the gap must be DECLARED.

WHY THIS FILE EXISTS (review finding E, 2026-08-10). Story 41.2 ships the same question
twice: ``server/core/fee_tax_geo_bridge.resolve_row_geography`` in Python and
``dbt/models/marts/fee_tax_country_resolution.sql`` in SQL. The story ITSELF declares
three places where they cannot agree. Nothing checked any of it: the only test that
looked at both (``tests/core/test_fee_tax_geo_bridge.py``, tests 25-27) parses the model
OFF DISK and asserts its COLUMN NAMES. Two engines answering the same money question,
and the assertion was that their headers match.

A known divergence is acceptable. A divergence nothing watches is not -- it is how the
console and the warehouse come to disagree about an invoice with neither side wrong.

WHAT THIS TEST DOES. It builds ONE scenario matrix, feeds it to BOTH engines, and
demands one of two verdicts per scenario:

  * the two engines return the SAME (country, market, resolution_source, country reason,
    market reason); or
  * the pair appears in ``_DECLARED_DIVERGENCES`` below, with the reason it exists.

An undeclared disagreement FAILS. A declared divergence that has stopped happening ALSO
fails -- a stale exemption is how a list of "known issues" becomes a list of lies.

WHY THE SQL SIDE IS COMPILED RATHER THAN RE-IMPLEMENTED. Re-expressing the model's ladder
in Python would create a THIRD implementation and the test would then agree with itself.
``dbt compile`` renders the real model, and it is executed verbatim against a DuckDB
holding the same five mirror relations production mirrors. The only edit made to the
compiled text is stripping the catalog qualifier, which names the warehouse file and
nothing else.

WHAT IS *NOT* COMPARED, stated rather than quietly skipped:
  * ``attr_source_type`` and ``source_type_gap_reason``. The DERIVATION half of contract
    C.4 reads the module manifest's ``public_catalog.category`` off disk, which is not
    mirrored, so the two engines are answering different questions there by design. The
    axis compared here is the GEOGRAPHIC one, which both engines fully own.
  * ``binding_registry_unavailable``. The Python bridge can report it because it probes
    ``to_regclass``; the mirror lands a shape-stable EMPTY relation instead, so the SQL
    cannot tell "unreadable" from "nothing declared". It is not observable in a harness
    where the relation exists, and a scenario cannot be written for it -- so it is named
    here rather than listed as a divergence that never fires.

STORY 37.9 CHANGED WHAT BOTH ENGINES READ, AND THAT IS WHY THIS FILE MATTERED. Until
2026-08-17 the Python side read a ``GeographicPosture`` out of
``app.project_preferences`` and the SQL side unnested the same JSON column. Both now read
the PUBLISHED Country hierarchy -- the projection Analyze already reads -- and this test
is what proves the two readings landed on the same answers rather than on two new
disagreements.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import fee_tax_geo_bridge as bridge  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DBT_DIR = _REPO_ROOT / "dbt"
try:  # dbt is invoked via `python -m dbt.cli.main` from the venv
    import dbt.cli.main  # noqa: F401
    _DBT_AVAILABLE = True
except ImportError:
    _DBT_AVAILABLE = False

pytestmark = [
    # This file drives dbt in a subprocess. See
    # tests/conformance/test_dbt_tests_declare_their_time.py: without the ceiling a
    # blocking subprocess does not fail the file, it ENDS THE SESSION. `dbt compile
    # --select` is ~6 s measured; 300 s is margin for a loaded machine, not a licence.
    pytest.mark.timeout(300),
    pytest.mark.skipif(
        not _DBT_AVAILABLE,
        reason="dbt is not importable, so the SQL twin cannot be compiled",
    ),
]

_CONNECTOR = "connector-under-test"
_DATE = "2026-05-01"

# ---------------------------------------------------------------------------
# The scenario matrix. One project per scenario, one fact row each, so a red names the
# rung. Every rung of the four-rung ladder is here, plus every branch that can END it.
# ---------------------------------------------------------------------------

#: One published market: (market_id, label, node_kind, country_codes).
#:
#: Story 37.9: these WERE `project_preferences.local_markets` JSON blobs. Both engines
#: now read the PUBLISHED Country hierarchy instead -- the Python side through
#: `GovernedGeography.from_projection`, the SQL side through
#: `mirror.country_market_projection` -- so the fixture describes the hierarchy once and
#: each engine reads it its own way. The scenarios themselves did not move: what they
#: prove about the ladder is unchanged.
_Market = tuple[str, str, str, tuple[str, ...]]

_FRANCE_ONLY: tuple[_Market, ...] = (("france", "France", "market", ("FR",)),)
_FRANCE_AND_UK: tuple[_Market, ...] = (
    ("france", "France", "market", ("FR",)),
    ("uk", "UK", "market", ("GB",)),
)
_EMEA_MULTI: tuple[_Market, ...] = (("emea", "EMEA", "market", ("FR", "DE")),)
#: A tracked market PLUS the governed catch-all. Rest of World is resolved geography but
#: is not a budgetable market, so a binding naming it must be refused.
_FRANCE_AND_REST: tuple[_Market, ...] = (
    ("france", "France", "market", ("FR",)),
    ("row_node", "Rest of World", "rest_of_world", ("DE",)),
)

#: (project_id, published_markets, bound_market_ids, dimension, value)
_SCENARIOS: tuple[tuple[str, tuple[_Market, ...], tuple[str, ...], str, str], ...] = (
    # RUNG 4 -- nothing declared, and no Country model to infer from.
    ("agr_no_country_model", (), (), "campaign_id", "cmp-1"),
    # RUNG 3 -- the declared inference: exactly one tracked country.
    ("agr_single_country", _FRANCE_ONLY, (), "campaign_id", "cmp-1"),
    # RUNG 4 -- two governed countries, so rung 3 must NOT guess between them.
    ("agr_multi_country", _EMEA_MULTI, (), "campaign_id", "cmp-1"),
    # RUNG 2 -- a declared binding to a single-country market.
    ("agr_binding_single", _FRANCE_AND_UK, ("france",), "campaign_id", "cmp-1"),
    # RUNG 2 + D3 -- the market resolves, the country does not.
    ("agr_binding_multi_country", _EMEA_MULTI, ("emea",), "campaign_id", "cmp-1"),
    # RUNG 2 -- a CONTRADICTION: two markets on one connector. One is never picked.
    ("agr_binding_ambiguous", _FRANCE_AND_UK, ("france", "uk"), "campaign_id", "cmp-1"),
    # RUNG 2 -- a binding to the governed catch-all: resolved geography, NOT budgetable.
    # Story 37.9 made this expressible: the retired posture listed no catch-all at all,
    # so the case could only be written with a hard-coded synthetic id.
    ("agr_binding_rest_of_world", _FRANCE_AND_REST, ("row_node",), "campaign_id", "cmp-1"),
    # RUNG 1 -- the row carries its own ISO code.
    ("agr_row_iso", _FRANCE_ONLY, (), "country", "FR"),
    # RUNG 1 -- free text the governed vocabulary knows. A DECLARED DIVERGENCE.
    ("agr_row_free_text", _FRANCE_ONLY, (), "country", "France"),
    # RUNG 1 -- a value neither engine can canonicalise.
    ("agr_row_unmapped", _FRANCE_ONLY, (), "country", "Atlantis"),
    # RUNG 1 -- the declared no-country bucket.
    ("agr_row_absent", _FRANCE_ONLY, (), "country", "__country_absent__"),
    # RUNG 1 -- WELL-FORMED BUT NOT A REAL ISO CODE.
    ("agr_row_shape_only", _FRANCE_ONLY, (), "country", "ZZ"),
    # RUNG 1 -- a real code no TRACKED market owns: the country resolves, the market
    # does not. Under the governed model the code may sit in the catch-all and the
    # answer is the same, which is the point of `market_kind` travelling unresolved.
    ("agr_row_outside_tracked", _FRANCE_ONLY, (), "country", "GB"),
    ("agr_row_in_rest_of_world", _FRANCE_AND_REST, (), "country", "DE"),
)

# ---------------------------------------------------------------------------
# The declared divergences. Each key is a project of the matrix; each value is
# (python_verdict, sql_verdict, why). A verdict is
# (country, market, resolution_source, country_reason, market_reason).
#
# EVERY ENTRY MUST FIRE. A divergence that stops happening is removed here, not left as
# a comfortable exemption.
# ---------------------------------------------------------------------------

_Verdict = tuple[str | None, str | None, str, str, str]

_DECLARED_DIVERGENCES: dict[str, tuple[_Verdict, _Verdict, str]] = {
    "agr_row_free_text": (
        ("FR", "france", "row_dimension", "", ""),
        (None, None, "", "country_value_unmapped", "country_value_unmapped"),
        "DECLARED BY THE STORY. Rung 1 canonicalises through the governed country"
        " vocabulary, which is a SEED ON DISK: `normalize_country_value('France')` ->"
        " 'FR'. The warehouse has no such table, so the model accepts a value only by"
        " SHAPE (two letters A-Z) and reports anything else as unmapped. The SQL side is"
        " the conservative one -- it under-resolves, it never invents a code -- so the"
        " divergence costs a gap, never a wrong number.",
    ),
}


# ---------------------------------------------------------------------------
# The SQL side.
# ---------------------------------------------------------------------------

_MIRROR_DDL = (
    # Story 37.9 / migration 269: the PUBLISHED country -> market -> region meaning.
    # Flat scalars, which is why the model no longer needs an adapter branch.
    """
    CREATE TABLE mirror.country_market_projection (
        project_id VARCHAR, registry_id VARCHAR, hierarchy_version_id VARCHAR,
        vocabulary_version_id VARCHAR, hierarchy_content_hash VARCHAR,
        country_code VARCHAR, market_id VARCHAR, market_label VARCHAR,
        market_kind VARCHAR, region_id VARCHAR, region_label VARCHAR,
        display_order INTEGER
    )
    """,
    """
    CREATE TABLE mirror.project_tax_fee_activation (
        project_id VARCHAR, project_configuration_version_id VARCHAR,
        capability_state VARCHAR, tax_fees_active BOOLEAN, rule_set_id VARCHAR,
        rule_set_version_id VARCHAR, rule_set_content_hash VARCHAR, rounding VARCHAR,
        default_money_basis VARCHAR, rule_count BIGINT, reporting_currency VARCHAR,
        money_policy_version_id VARCHAR, money_policy_content_hash VARCHAR
    )
    """,
    """
    CREATE TABLE mirror.datastream_country_binding_dim (
        project_id VARCHAR, connector VARCHAR, datastream_id VARCHAR, market_id VARCHAR
    )
    """,
    """
    CREATE TABLE mirror.datastreams_dim (
        project_id VARCHAR, datastream_id VARCHAR, connector VARCHAR, name VARCHAR
    )
    """,
    """
    CREATE TABLE mirror.datastream_source_types (
        project_id VARCHAR, datastream_id VARCHAR, source_type VARCHAR
    )
    """,
    """
    CREATE TABLE main_marts.fact_daily_kpi (
        project_id VARCHAR, date DATE, connector VARCHAR, metric VARCHAR,
        breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE,
        pull_id VARCHAR, loaded_at VARCHAR
    )
    """,
)

#: `"<catalog>"."<schema>"."<relation>"` -> `"<schema>"."<relation>"`. The catalog names
#: the warehouse file and nothing else, so dropping it is the only edit made to the
#: compiled model -- its ladder runs verbatim.
_CATALOG = re.compile(r'"[A-Za-z0-9_]+"\."(mirror|main_marts|main)"\."')

#: A CTE that exists ONLY in the real ladder. See `_stand_up_relations`.
_LADDER_MARKER = "tracked_countries"

#: The mirror relations the model's absent-source guard asks about, DERIVED from the
#: DDL above rather than listed a second time: a relation added to one and forgotten in
#: the other is exactly how this fixture went silently vacuous.
_MIRROR_SOURCES = frozenset(
    re.search(r"CREATE TABLE mirror\.(\w+)", ddl).group(1)
    for ddl in _MIRROR_DDL
    if "CREATE TABLE mirror." in ddl
)


def _stand_up_relations(con) -> None:
    """Create the relations the model reads, empty, in ``con``.

    USED TWICE, AND THE SECOND USE IS THE ONE THAT WAS MISSING. `dbt compile` is
    not a text substitution: since AI-314 (2026-08-24) this model opens with
    ``toorow_absent_sources('mirror', [...])`` and ASKS THE WAREHOUSE whether its
    five mirror relations are there, rendering `toorow_absent_source_stub` --
    fourteen `CAST(NULL AS ...)` columns over ``FROM (SELECT 1) WHERE FALSE`` --
    when any of them is not. The compile database this fixture hands dbt was a
    brand-new empty DuckDB file, so all five were absent and the compiled twin
    was the STUB. It executed happily against the seeded database and returned
    zero rows, and fifteen tests read that as "the model dropped every scenario".

    So the compile database gets the same relations as the execution database.
    They stay empty there: the guard asks whether they EXIST, never what they
    hold.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    for ddl in _MIRROR_DDL:
        con.execute(ddl)


@pytest.fixture(scope="module")
def compiled_model(tmp_path_factory) -> str:
    """`dbt compile` the real model, and return its SQL with the catalog stripped.

    COMPILED RATHER THAN READ FROM A PREVIOUS BUILD: an artifact left by an earlier run
    could predate the model, and a stale artifact reporting agreement is worse than no
    test. This costs ~6 s and cannot be stale.
    """
    import duckdb

    tmp = tmp_path_factory.mktemp("bridge_agreement")
    catalogue = duckdb.connect(str(tmp / "compile.duckdb"))
    try:
        _stand_up_relations(catalogue)
    finally:
        catalogue.close()
    target = tmp / "target"
    compiled = (
        target / "compiled" / "connector" / "models" / "marts"
        / "fee_tax_country_resolution.sql"
    )
    profiles = tmp / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        "connector:\n"
        "  target: local\n"
        "  outputs:\n"
        "    local:\n"
        "      type: duckdb\n"
        f'      path: "{(tmp / "compile.duckdb").as_posix()}"\n'
        "      threads: 1\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "compile",
            "--select", "fee_tax_country_resolution",
            "--profiles-dir", str(profiles),
            "--project-dir", str(_DBT_DIR),
            "--target-path", str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "dbt compile failed, so the SQL twin could not be rendered:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert compiled.exists(), f"dbt compile produced no artifact at {compiled}"
    rendered = _CATALOG.sub(r'"\1"."', compiled.read_text(encoding="utf-8"))
    assert _LADDER_MARKER in rendered, (
        "dbt rendered the absent-source stub instead of the ladder, so this file would "
        "compare the Python engine against fourteen NULL columns and zero rows. The "
        "compile database is missing one of the mirror relations the model guards on "
        f"({', '.join(sorted(_MIRROR_SOURCES))}) -- see `_stand_up_relations`."
    )
    return rendered


@pytest.fixture(scope="module")
def sql_verdicts(compiled_model: str, tmp_path_factory) -> dict[str, _Verdict]:
    import duckdb

    con = duckdb.connect(str(tmp_path_factory.mktemp("bridge_db") / "test.duckdb"))
    try:
        _stand_up_relations(con)
        con.execute(
            "CREATE TABLE main.dim_country AS SELECT * FROM read_csv_auto(?)",
            [str(_DBT_DIR / "seeds" / "dim_country.csv")],
        )
        for project, markets, bound, dimension, value in _SCENARIOS:
            for order, (market_id, label, kind, codes) in enumerate(markets):
                for code in codes:
                    con.execute(
                        "INSERT INTO mirror.country_market_projection VALUES"
                        " (?, 'mdreg_EXAMPLE', 'mdver_EXAMPLE', 'mdvoc_EXAMPLE', ?,"
                        " ?, ?, ?, ?, NULL, NULL, ?)",
                        [project, "0" * 64, code, market_id, label, kind, order],
                    )
            # Every scenario project is ACTIVE: this test is about the ladder, and a
            # gated-off project would simply produce no row to compare.
            con.execute(
                "INSERT INTO mirror.project_tax_fee_activation VALUES (?, 'pcv_EXAMPLE',"
                " 'ready', TRUE, 'grs_EXAMPLE', 'grsv_EXAMPLE', ?, 'half_up',"
                " 'native_source', 1, 'EUR', 'grsv_EXAMPLE_MONEY', ?)",
                [project, "0" * 64, "0" * 64],
            )
            for index, market_id in enumerate(bound):
                con.execute(
                    "INSERT INTO mirror.datastream_country_binding_dim VALUES"
                    " (?, ?, ?, ?)",
                    [project, _CONNECTOR, f"dst_{project}_{index}", market_id],
                )
            con.execute(
                "INSERT INTO main_marts.fact_daily_kpi VALUES"
                " (?, CAST(? AS DATE), ?, 'cost', ?, ?, 100.0, 'pull_EXAMPLE',"
                " '2026-05-02T00:00:00Z')",
                [project, _DATE, _CONNECTOR, dimension, value],
            )
        rows = con.execute(
            f"""
            SELECT project_id, attr_country, attr_market, resolution_source,
                   country_gap_reason, market_gap_reason
            FROM ({compiled_model}) t
            """
        ).fetchall()
    finally:
        con.close()
    return {row[0]: (row[1], row[2], row[3], row[4], row[5]) for row in rows}


# ---------------------------------------------------------------------------
# The Python side.
# ---------------------------------------------------------------------------


def _projection(markets: tuple[_Market, ...]):
    """The SAME published hierarchy the SQL side reads, as `load_projection` returns it.

    Built as a real `GeographyProjection` and passed through
    `GovernedGeography.from_projection` on purpose: the adapter that filters the catch-all
    out of the tracked set is part of what this test compares, so stubbing it would leave
    the one piece of Story 37.9 most likely to disagree with the SQL unmeasured.
    """
    from core.country_registry import GeographyProjection

    return GeographyProjection(
        hierarchy_version_id="mdver_EXAMPLE",
        vocabulary_version_id="mdvoc_EXAMPLE",
        registry_id="mdreg_EXAMPLE",
        as_of=date(2026, 5, 1),
        market_of_value={
            code: market_id for market_id, _, _, codes in markets for code in codes
        },
        region_of_node={},
        labels={market_id: label for market_id, label, _, _ in markets},
        kinds={market_id: kind for market_id, _, kind, _ in markets},
        canonical_values=frozenset(
            code for _, _, _, codes in markets for code in codes
        ),
        rest_of_world_id=next(
            (market_id for market_id, _, kind, _ in markets if kind == "rest_of_world"),
            None,
        ),
        rest_of_world_label="Rest of World",
        rest_of_world_drill="country",
    )


def _python_verdict(
    markets: tuple[_Market, ...],
    bound: tuple[str, ...],
    dimension: str,
    value: str,
) -> _Verdict:
    geography = bridge.GovernedGeography.from_projection(
        _projection(markets) if markets else None
    )
    index = bridge.BindingIndex(
        # TRUE on purpose: the mirror relation EXISTS in this harness, so "the registry
        # could not be read" is not the state under test. Saying otherwise would let the
        # Python side reach a rung the SQL side structurally cannot, and the comparison
        # would be measuring the harness.
        registry_available=True,
        market_ids_by_connector=({_CONNECTOR: frozenset(bound)} if bound else {}),
    )
    resolution = bridge.resolve_row_geography(
        {
            "connector": _CONNECTOR,
            "breakdown_dimension": dimension,
            "breakdown_value": value,
        },
        geography=geography,
        binding_index=index,
    )
    return (
        resolution.country_code,
        resolution.market_id,
        resolution.resolution_source,
        resolution.country_gap.reason if resolution.country_gap else "",
        resolution.market_gap.reason if resolution.market_gap else "",
    )


@pytest.fixture(scope="module")
def python_verdicts() -> dict[str, _Verdict]:
    return {
        project: _python_verdict(markets, bound, dimension, value)
        for project, markets, bound, dimension, value in _SCENARIOS
    }


# ---------------------------------------------------------------------------
# The assertions.
# ---------------------------------------------------------------------------


def test_every_scenario_produced_a_row_in_both_engines(
    sql_verdicts: dict[str, _Verdict], python_verdicts: dict[str, _Verdict]
) -> None:
    """ANTI-VACUITY. Without this, a model that emitted nothing would "agree" perfectly."""
    expected = {scenario[0] for scenario in _SCENARIOS}
    assert set(python_verdicts) == expected
    missing = sorted(expected - set(sql_verdicts))
    assert not missing, (
        f"the compiled model emitted no row for {missing}. Every scenario project is"
        " activated and carries exactly one fact row, so a missing row is the model"
        " dropping it -- and an agreement test over zero rows agrees with everything."
    )
    extra = sorted(set(sql_verdicts) - expected)
    assert not extra, f"the model emitted rows for unknown projects: {extra}"


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: s[0] if isinstance(s, tuple) else s)
def test_the_two_engines_agree_or_the_gap_is_declared(
    scenario: tuple[Any, ...],
    sql_verdicts: dict[str, _Verdict],
    python_verdicts: dict[str, _Verdict],
) -> None:
    project = scenario[0]
    python = python_verdicts[project]
    sql = sql_verdicts[project]

    if project not in _DECLARED_DIVERGENCES:
        assert python == sql, (
            f"UNDECLARED DIVERGENCE on {project}: the Python bridge and its SQL twin"
            " answer the same geographic question differently, and nothing says why.\n"
            f"  python (country, market, source, country_reason, market_reason) = {python}\n"
            f"  sql    (country, market, source, country_reason, market_reason) = {sql}\n"
            "Either repair the engine that is wrong, or add the pair to"
            " _DECLARED_DIVERGENCES with the reason it cannot be repaired here. A"
            " divergence nothing watches is how a console and a warehouse come to"
            " disagree about an invoice with neither side wrong."
        )
        return

    declared_python, declared_sql, why = _DECLARED_DIVERGENCES[project]
    assert python != sql, (
        f"STALE DIVERGENCE on {project}: the two engines now AGREE, so the exemption in"
        f" _DECLARED_DIVERGENCES is a lie. Remove it. Its stated reason was: {why}"
    )
    assert (python, sql) == (declared_python, declared_sql), (
        f"THE DIVERGENCE ON {project} CHANGED SHAPE, which is a regression the exemption"
        " was never granted for.\n"
        f"  python now      = {python}   (declared: {declared_python})\n"
        f"  sql now         = {sql}      (declared: {declared_sql})\n"
        f"  declared reason = {why}"
    )


def test_no_declared_divergence_names_a_scenario_that_does_not_exist() -> None:
    """A registry keyed on a deleted scenario would silently stop asserting anything."""
    known = {scenario[0] for scenario in _SCENARIOS}
    orphans = sorted(set(_DECLARED_DIVERGENCES) - known)
    assert not orphans, (
        f"_DECLARED_DIVERGENCES names scenarios that no longer exist: {orphans}"
    )
