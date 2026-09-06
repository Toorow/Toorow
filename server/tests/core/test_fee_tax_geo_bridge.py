"""Story 41.2 -- the Epic 37 country/market bridge and the AD-8 source_type ladder.

Offline (no Postgres): the four-rung ladder, the D3 country/market asymmetry, the three
outcomes of the evaluators, the anti-hardcode guards over the three NEW core modules and
the NEW .sql, and the frozen 14-column contract of
dbt/models/marts/fee_tax_country_resolution.sql (parsed off disk -- Story 41.3 codes
against it sight unseen).

Pg-gated (skipped, with an explicit reason, unless TEST_POSTGRES_DSN points at a database
where migrations 104 / 119 are applied): the binding index against real
app.market_bindings + app.datastreams rows, and resolve_source_type against real
app.datastreams.data_role + app.datastream_source_types rows. A SKIP states the truth --
a FAILURE would blame this story for an unapplied migration.

The single most important assertion in this file is test 17: an UNRESOLVED country is
never reported as NO_MATCH. Known false is +0 micros with the cascade intact; unresolvable
is a typed gap with the total flagged incomplete. They are different answers to different
questions and must never collapse.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from core import fee_tax_geo_bridge as bridge
from core import fee_tax_rules as ftr
from core import fee_tax_source_types as fts
from core.geographic_conformance import make_country_resolver
from core.geographic_semantics import UNKNOWN_BUCKET_ID

# Story 48.2: `Other markets` stopped being a reserved constant and became a
# governed Rest of World node whose id is per-Project. What this file proves is
# unchanged -- a reporting GROUPING is never a budgetable market -- so the
# fixtures name one of each kind the governed geography cannot resolve.
REST_OF_WORLD_NODE = "mdnode_REST_OF_WORLD"

# Keep the background workers off for anything this file imports transitively
# (pattern: test_dataset_access_grants.py). None of the three new modules starts one.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVER_CORE = _REPO_ROOT / "server" / "core"
_MODULES_DIR = _REPO_ROOT / "server" / "modules"
_MODEL_SQL = _REPO_ROOT / "dbt" / "models" / "marts" / "fee_tax_country_resolution.sql"
_SCHEMA_YML = _REPO_ROOT / "dbt" / "models" / "marts" / "schema_fee_tax_bridge.yml"
_MIRROR_SYNC = _SERVER_CORE / "mirror_sync.py"
_SOURCES_YML = _REPO_ROOT / "dbt" / "models" / "staging" / "sources_mirror.yml"

_NEW_CORE_MODULES = (
    "fee_tax_geo_bridge.py",
    "fee_tax_source_types.py",
    "fee_tax_country_defaults.py",
)

# The frozen output contract of section D.2, in order. Story 41.3 joins this view once and
# reads these names; changing one is a breaking change to a story that is already coded.
_FROZEN_COLUMNS = (
    "project_id",
    "date",
    "connector",
    "metric",
    "breakdown_dimension",
    "breakdown_value",
    "attr_country",
    "attr_market",
    "attr_source_type",
    "resolution_source",
    "country_gap_reason",
    "market_gap_reason",
    "source_type_gap_reason",
    "gap_code",
)

# The 8 manifest categories (server/core/schemas/manifest.schema.json) and the 7
# data_role values (migration 093), for the exhaustive sweep.
_CATEGORIES = (
    "paid_media",
    "ad_server",
    "analytics_product",
    "commerce_billing",
    "crm",
    "email_sms",
    "files_warehouses",
    "business_context",
)
_DATA_ROLES = (
    "Spend",
    "Performance",
    "Revenue & conversions",
    "Forecast & plan",
    "Context",
    "Reference & targets",
    "Operational",
)


# ---------------------------------------------------------------------------
# Postgres availability (pattern: test_fee_tax_rules.py / test_dataset_access_grants.py).
# ---------------------------------------------------------------------------


def _pg_state() -> tuple[bool, bool, bool]:
    """(postgres reachable, migration 104 applied, migration 119 applied)."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False, False, False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('app.market_bindings') IS NOT NULL")
                has_104 = bool(cur.fetchone()[0])
                cur.execute("SELECT to_regclass('app.datastream_source_types') IS NOT NULL")
                has_119 = bool(cur.fetchone()[0])
        return True, has_104, has_119
    except Exception:
        return False, False, False


_PG_REACHABLE, _PG_HAS_104, _PG_HAS_119 = _pg_state()

market_bindings_schema = pytest.mark.skipif(
    not (_PG_REACHABLE and _PG_HAS_104),
    reason="migration 104 (app.market_bindings) is not applied on TEST_POSTGRES_DSN",
)
source_types_schema = pytest.mark.skipif(
    not (_PG_REACHABLE and _PG_HAS_119),
    reason="migration 119 (app.datastream_source_types) is not applied on TEST_POSTGRES_DSN",
)


# ---------------------------------------------------------------------------
# Fixtures / builders.
# ---------------------------------------------------------------------------


def _geography(*markets: tuple[str, tuple[str, ...]]) -> bridge.GovernedGeography:
    """A published Country model from (market_id, country_codes) pairs.

    Story 37.9: this used to build a `GeographicPosture` out of
    `app.project_preferences`. The shape of the fixture is unchanged on purpose --
    what these tests prove about the ladder did not move -- but the authority did:
    the bridge now reasons from the PUBLISHED hierarchy version that Analyze reads.
    """
    return bridge.GovernedGeography(
        hierarchy_version_id="mdver_TEST",
        markets=tuple(
            bridge.GovernedMarket(
                id=market_id, label=market_id.upper(), country_codes=tuple(codes)
            )
            for market_id, codes in markets
        ),
    )


#: A Project that has published NO Country meaning. It is NOT "Global": the bridge
#: must reach its own rung-4 reason and never present it as a decided posture.
_NO_COUNTRY_MODEL = bridge.GovernedGeography.absent()


def _row(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "connector": "connector-under-test",
        "breakdown_dimension": "campaign_id",
        "breakdown_value": "cmp-1",
    }
    payload.update(over)
    return payload


def _country_row(value: str, **over: Any) -> dict[str, Any]:
    return _row(breakdown_dimension="country", breakdown_value=value, **over)


_REGISTRY_ABSENT = bridge.BindingIndex(registry_available=False)
_REGISTRY_EMPTY = bridge.BindingIndex(registry_available=True)


class _FakeCursor:
    def __init__(self, script, log: list[tuple[str, Any]]) -> None:
        self._script = script
        self._log = log
        self._rows: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))
        self._rows = list(self._script(sql, params) or ())

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Minimal psycopg-shaped connection driven by a ``(sql, params) -> rows`` script."""

    def __init__(self, script) -> None:
        self._script = script
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._script, self.statements)


# ===========================================================================
# Ladder rung 1 -- the row's own country (AC1)
# ===========================================================================


def test_01_row_country_dimension_resolves_and_labels_its_provenance() -> None:
    resolution = bridge.resolve_row_geography(
        _country_row("FR"), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )
    assert resolution.country_code == "FR"
    assert resolution.source == bridge.SOURCE_ROW_DIMENSION
    assert resolution.resolution_source == "row_dimension"
    assert resolution.country_gap is None
    assert resolution.is_country_resolved


def test_02_row_value_alias_resolves_through_the_shared_vocabulary() -> None:
    """Proves the bridge REUSES country_vocabulary rather than forking normalisation."""
    resolution = bridge.resolve_row_geography(
        _country_row("France"), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )
    assert resolution.country_code == "FR"


def test_03_unmapped_row_value_is_a_typed_gap_pointing_at_dimension_conformance() -> None:
    resolution = bridge.resolve_row_geography(
        _country_row("Freedonia"), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )
    assert resolution.country_code is None
    assert resolution.country_gap is not None
    assert resolution.country_gap.reason == bridge.REASON_VALUE_UNMAPPED
    # The repair surface 37.9 already owns -- not a second, parallel one.
    assert resolution.country_gap.repair["surface"] == "dimension_conformance"


def test_04_injected_mdm_resolver_resolves_a_client_spelling() -> None:
    """The bridge DELEGATES MDM resolution to the 37.9 resolver; it never re-implements it."""
    resolver = make_country_resolver(conformance={("meta-ads", "Espagne"): "ES"})
    resolution = bridge.resolve_row_geography(
        _country_row("Espagne", connector="meta-ads"),
        geography=_NO_COUNTRY_MODEL,
        resolver=resolver,
        binding_index=_REGISTRY_EMPTY,
    )
    assert resolution.country_code == "ES"
    assert resolution.source == bridge.SOURCE_ROW_DIMENSION


def test_05_a_composite_sub_dimension_is_not_a_country_row() -> None:
    """Pins the EXACT-match rule: 'country>device' must not be read as a country row."""
    resolution = bridge.resolve_row_geography(
        _row(breakdown_dimension="country>device", breakdown_value="FR>mobile"),
        geography=_NO_COUNTRY_MODEL,
        binding_index=_REGISTRY_EMPTY,
    )
    assert resolution.country_code is None
    assert resolution.source is None
    assert resolution.country_gap is not None
    assert resolution.country_gap.reason == bridge.REASON_NO_BINDING_COUNTRY_MODEL_ABSENT


def test_06_row_country_also_places_the_market_when_the_posture_owns_it() -> None:
    """A market-conditioned rule fires on a country row too."""
    resolution = bridge.resolve_row_geography(
        _country_row("FR"),
        geography=_geography(("france", ("FR", "MC"))),
        binding_index=_REGISTRY_EMPTY,
    )
    assert resolution.country_code == "FR"
    assert resolution.market_id == "france"
    assert resolution.market_gap is None
    assert resolution.is_market_resolved


def test_07_row_country_outside_every_tracked_market_gaps_only_the_market() -> None:
    resolution = bridge.resolve_row_geography(
        _country_row("GB"),
        geography=_geography(("france", ("FR", "MC"))),
        binding_index=_REGISTRY_EMPTY,
    )
    # The COUNTRY resolved; only the market did not.
    assert resolution.country_code == "GB"
    assert resolution.country_gap is None
    assert resolution.market_id is None
    assert resolution.market_gap is not None
    assert resolution.market_gap.reason == bridge.REASON_OUTSIDE_TRACKED


# ===========================================================================
# Ladder rung 2 -- the declared binding, and the D3 asymmetry (AC1, AC2)
# ===========================================================================


def test_08_single_country_binding_resolves_both_axes() -> None:
    resolution = bridge.resolve_row_geography(
        _row(connector="connector-x"),
        geography=_geography(("france", ("FR",)), ("uk", ("GB",))),
        binding_index=bridge.BindingIndex(
            registry_available=True,
            market_ids_by_connector={"connector-x": frozenset({"france"})},
        ),
    )
    assert resolution.country_code == "FR"
    assert resolution.market_id == "france"
    assert resolution.source == bridge.SOURCE_DECLARED_BINDING
    assert resolution.resolution_source == "declared_binding"
    assert resolution.gaps() == ()


def test_09_d3_multi_country_market_resolves_the_market_and_gaps_the_country() -> None:
    """THE case a market holding several countries turns on.

    The market resolves and a `market` condition MATCHES; the country does not resolve and
    a `country` condition is UNRESOLVED -- on the very same row. The two attributes carry
    INDEPENDENT resolution states.
    """
    resolution = bridge.resolve_row_geography(
        _row(connector="connector-x"),
        geography=_geography(("france", ("FR", "MC"))),
        binding_index=bridge.BindingIndex(
            registry_available=True,
            market_ids_by_connector={"connector-x": frozenset({"france"})},
        ),
    )
    assert resolution.market_id == "france"
    assert resolution.market_gap is None
    assert resolution.country_code is None
    assert resolution.country_gap is not None
    assert resolution.country_gap.reason == bridge.REASON_NOT_SINGLE_COUNTRY
    # resolution_source is the provenance of the COUNTRY resolution (AC14).
    assert resolution.resolution_source == ""

    assert bridge.evaluate_market_condition(["france"], resolution) is bridge.ConditionOutcome.MATCH
    assert (
        bridge.evaluate_country_condition(["FR"], resolution)
        is bridge.ConditionOutcome.UNRESOLVED
    )


@pytest.mark.parametrize("synthetic", [REST_OF_WORLD_NODE, UNKNOWN_BUCKET_ID])
def test_10_a_synthetic_grouping_is_never_a_market_binding(synthetic: str) -> None:
    resolution = bridge.resolve_row_geography(
        _row(connector="connector-x"),
        geography=_geography(("france", ("FR",))),
        binding_index=bridge.BindingIndex(
            registry_available=True,
            market_ids_by_connector={"connector-x": frozenset({synthetic})},
        ),
    )
    assert resolution.country_code is None
    assert resolution.market_id is None
    assert resolution.country_gap is not None
    assert resolution.market_gap is not None
    assert resolution.country_gap.reason == bridge.REASON_NOT_BINDABLE
    assert resolution.market_gap.reason == bridge.REASON_NOT_BINDABLE


def test_11_two_markets_on_one_connector_is_a_gap_not_a_pick() -> None:
    resolution = bridge.resolve_row_geography(
        _row(connector="connector-x"),
        geography=_geography(("france", ("FR",)), ("uk", ("GB",))),
        binding_index=bridge.BindingIndex(
            registry_available=True,
            market_ids_by_connector={"connector-x": frozenset({"france", "uk"})},
        ),
    )
    assert resolution.country_code is None
    assert resolution.market_id is None
    assert resolution.country_gap.reason == bridge.REASON_AMBIGUOUS
    assert resolution.market_gap.reason == bridge.REASON_AMBIGUOUS


def test_12_global_posture_skips_rung_2_but_never_rung_1() -> None:
    resolved = bridge.resolve_row_geography(
        _country_row("FR"), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )
    assert resolved.country_code == "FR"
    assert resolved.market_id is None  # a Global posture has no markets to place it in

    gapped = bridge.resolve_row_geography(
        _row(), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )
    assert gapped.country_code is None
    assert gapped.country_gap.reason == bridge.REASON_NO_BINDING_COUNTRY_MODEL_ABSENT


# ===========================================================================
# Rung 3 -- the project posture (AC14, C5)
# ===========================================================================


def test_12b_real_row_data_outranks_the_posture_inference() -> None:
    """Also the Story 37.6 forward-compat proof: when the fanout lands, its rows win."""
    resolution = bridge.resolve_row_geography(
        _country_row("ES"),
        geography=_geography(("france", ("FR",))),
        binding_index=_REGISTRY_EMPTY,
    )
    assert resolution.country_code == "ES"
    assert resolution.resolution_source == "row_dimension"


def test_12c_single_country_posture_resolves_every_row() -> None:
    resolution = bridge.resolve_row_geography(
        _row(),
        geography=_geography(("france", ("FR",))),
        binding_index=_REGISTRY_EMPTY,
    )
    assert resolution.country_code == "FR"
    assert resolution.market_id == "france"
    assert resolution.source == bridge.SOURCE_PROJECT_POSTURE
    assert resolution.country_gap is None
    assert resolution.market_gap is None


def test_12d_the_posture_rung_fires_with_migration_104_unapplied() -> None:
    """THE test that proves the module is useful on day one.

    Migration 104 is not applied and nothing writes a datastream binding, so rung 2 cannot
    fire for anybody. Rung 3 reads only app.project_preferences -- already mirrored -- so a
    single-country project resolves TODAY, with no migration and no declaration.
    """
    resolution = bridge.resolve_row_geography(
        _row(),
        geography=_geography(("france", ("FR",))),
        binding_index=_REGISTRY_ABSENT,
    )
    assert resolution.country_code == "FR"
    assert resolution.resolution_source == "project_posture"


def test_12e_two_tracked_countries_stay_a_gap() -> None:
    """We never guess between two countries."""
    resolution = bridge.resolve_row_geography(
        _row(),
        geography=_geography(("france", ("FR",)), ("uk", ("GB",))),
        binding_index=_REGISTRY_EMPTY,
    )
    assert resolution.country_code is None
    assert resolution.market_id is None
    assert resolution.country_gap.reason == bridge.REASON_NO_BINDING_MULTI_COUNTRY


def test_12f_global_posture_gap_carries_its_own_distinct_reason() -> None:
    resolution = bridge.resolve_row_geography(
        _row(), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )
    assert resolution.country_gap.reason == bridge.REASON_NO_BINDING_COUNTRY_MODEL_ABSENT
    assert resolution.country_gap.reason != bridge.REASON_NO_BINDING_MULTI_COUNTRY


def test_12g_one_market_holding_two_countries_is_not_single_country() -> None:
    """Guards the len(local_markets) vs len(country_codes) trap.

    Exactly ONE market, so a naive len(markets) == 1 test would resolve this project to an
    arbitrary code. The posture tracks TWO countries: it stays a gap.
    """
    geography = _geography(("france", ("FR", "MC")))
    assert len(geography.markets) == 1
    assert len(geography.country_codes) == 2

    resolution = bridge.resolve_row_geography(
        _row(), geography=geography, binding_index=_REGISTRY_EMPTY
    )
    assert resolution.country_code is None
    assert resolution.country_gap.reason == bridge.REASON_NO_BINDING_MULTI_COUNTRY


@pytest.mark.parametrize(
    ("market_ids", "expected"),
    [
        (frozenset({"france", "other"}), bridge.REASON_AMBIGUOUS),
        (frozenset({REST_OF_WORLD_NODE}), bridge.REASON_NOT_BINDABLE),
    ],
)
def test_12h_an_inference_never_overrules_a_contradiction(
    market_ids: frozenset[str], expected: str
) -> None:
    """A single-country posture does NOT rescue a contradictory declaration.

    The posture here WOULD resolve every row on its own (exactly one tracked country), so
    if the rung were reachable after a contradiction this test would return 'FR'. It must
    not: an inference may fill a silence, never overrule a contradiction.
    """
    resolution = bridge.resolve_row_geography(
        _row(connector="connector-x"),
        geography=_geography(("france", ("FR",))),
        binding_index=bridge.BindingIndex(
            registry_available=True,
            market_ids_by_connector={"connector-x": market_ids},
        ),
    )
    assert resolution.country_code is None
    assert resolution.resolution_source == ""
    assert resolution.country_gap.reason == expected


def test_12i_provenance_is_exhaustive_and_distinguishable() -> None:
    """The four outcomes yield exactly the four frozen labels, and the label is non-empty
    IF AND ONLY IF the country resolved (AC14)."""
    single = _geography(("france", ("FR",)))
    multi = _geography(("france", ("FR",)), ("uk", ("GB",)))
    cases = [
        (_country_row("ES"), single, _REGISTRY_EMPTY, "row_dimension"),
        (
            _row(connector="connector-x"),
            multi,
            bridge.BindingIndex(
                registry_available=True,
                market_ids_by_connector={"connector-x": frozenset({"uk"})},
            ),
            "declared_binding",
        ),
        (_row(), single, _REGISTRY_EMPTY, "project_posture"),
        (_row(), multi, _REGISTRY_EMPTY, ""),
    ]
    seen = []
    for row, geography, index, expected in cases:
        resolution = bridge.resolve_row_geography(row, geography=geography, binding_index=index)
        assert resolution.resolution_source == expected
        assert bool(resolution.resolution_source) is resolution.is_country_resolved
        seen.append(resolution.resolution_source)
    assert set(seen) == {"row_dimension", "declared_binding", "project_posture", ""}


# ===========================================================================
# Rungs 2 / 4 continued -- "cannot read" is not "nothing declared"
# ===========================================================================


def test_13_unreadable_registry_has_its_own_reason_when_rung_3_declines() -> None:
    resolution = bridge.resolve_row_geography(
        _row(),
        geography=_geography(("france", ("FR",)), ("uk", ("GB",))),
        binding_index=_REGISTRY_ABSENT,
    )
    assert resolution.country_gap.reason == bridge.REASON_REGISTRY_UNAVAILABLE
    assert resolution.market_gap.reason == bridge.REASON_REGISTRY_UNAVAILABLE


def test_14_nothing_declared_is_reported_differently_from_cannot_read() -> None:
    resolution = bridge.resolve_row_geography(
        _row(),
        geography=_geography(("france", ("FR",)), ("uk", ("GB",))),
        binding_index=_REGISTRY_EMPTY,
    )
    assert resolution.country_gap.reason == bridge.REASON_NO_BINDING_MULTI_COUNTRY
    assert resolution.country_gap.reason != bridge.REASON_REGISTRY_UNAVAILABLE


# ===========================================================================
# The evaluators -- the load-bearing distinction (AC3, AC4)
# ===========================================================================


def _resolved_fr() -> bridge.GeoResolution:
    return bridge.resolve_row_geography(
        _country_row("FR"), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )


def _unresolved() -> bridge.GeoResolution:
    return bridge.resolve_row_geography(
        _row(), geography=_NO_COUNTRY_MODEL, binding_index=_REGISTRY_EMPTY
    )


def test_15_country_condition_on_the_resolved_value_matches() -> None:
    assert (
        bridge.evaluate_country_condition(["FR"], _resolved_fr())
        is bridge.ConditionOutcome.MATCH
    )


def test_16_a_known_false_country_condition_is_no_match_and_raises_no_gap() -> None:
    """KNOWN FALSE -> the caller contributes +0 micros and the cascade continues."""
    resolution = _resolved_fr()
    assert (
        bridge.evaluate_country_condition(["GB"], resolution)
        is bridge.ConditionOutcome.NO_MATCH
    )
    assert resolution.country_gap is None


def test_17_an_unresolvable_country_is_unresolved_and_never_no_match() -> None:
    """THE assertion this story exists to protect.

    NO_MATCH means "+0 micros, carry on". UNRESOLVED means "we cannot honestly evaluate
    this rule: it does not fire, the total is flagged incomplete, and no zero is
    fabricated". Collapsing the second into the first would silently under-report a fee.
    """
    outcome = bridge.evaluate_country_condition(["FR"], _unresolved())
    assert outcome is bridge.ConditionOutcome.UNRESOLVED
    assert outcome != bridge.ConditionOutcome.NO_MATCH


@pytest.mark.parametrize("condition", [None, []])
def test_18_an_unconditioned_rule_is_not_poisoned_by_an_unknown_attribute(
    condition: Any,
) -> None:
    assert (
        bridge.evaluate_country_condition(condition, _unresolved())
        is bridge.ConditionOutcome.MATCH
    )


def test_19_market_condition_mirrors_the_same_three_outcomes() -> None:
    resolved = bridge.resolve_row_geography(
        _row(connector="connector-x"),
        geography=_geography(("france", ("FR",))),
        binding_index=bridge.BindingIndex(
            registry_available=True,
            market_ids_by_connector={"connector-x": frozenset({"france"})},
        ),
    )
    unresolved = _unresolved()
    assert bridge.evaluate_market_condition(["france"], resolved) is bridge.ConditionOutcome.MATCH
    assert bridge.evaluate_market_condition(["uk"], resolved) is bridge.ConditionOutcome.NO_MATCH
    assert (
        bridge.evaluate_market_condition(["france"], unresolved)
        is bridge.ConditionOutcome.UNRESOLVED
    )
    assert bridge.evaluate_market_condition(None, unresolved) is bridge.ConditionOutcome.MATCH


def test_20_combine_outcomes_implements_unresolved_over_no_match_over_match() -> None:
    outcome = bridge.ConditionOutcome
    assert bridge.combine_outcomes(outcome.MATCH, outcome.NO_MATCH) is outcome.NO_MATCH
    assert bridge.combine_outcomes(outcome.NO_MATCH, outcome.UNRESOLVED) is outcome.UNRESOLVED
    assert bridge.combine_outcomes(outcome.MATCH, outcome.MATCH) is outcome.MATCH
    assert bridge.combine_outcomes() is outcome.MATCH


def test_20b_the_precedence_holds_in_COMPOSITION_not_only_in_isolation() -> None:
    """E41-AD9 precedence, exercised through the whole chain -- review finding E.

    WHY THIS EXISTS. Mutating ``combine_outcomes`` so that ``UNRESOLVED`` collapses into
    ``NO_MATCH`` reddened exactly ONE test: test_20 above, its own direct unit test. The
    28 tests of ``resolve_row_geography`` stayed green, because none of them ever calls
    the combiner. So the precedence was held at the unit level and NOWHERE in the chain a
    caller would actually use -- and the chain is where the money is: ``NO_MATCH`` means
    +0 micros with the cascade intact, ``UNRESOLVED`` means the rule does not fire, a
    typed gap is emitted and the total is flagged incomplete.

    The composition, in the order a caller runs it:
        resolve_row_geography -> evaluate_country_condition
                              -> evaluate_market_condition
                              -> combine_outcomes

    The row is the D3 asymmetry case: a datastream bound to a market holding TWO
    countries. The MARKET resolves, the COUNTRY does not. A rule conditioned on a
    DIFFERENT market is therefore genuinely KNOWN FALSE (``NO_MATCH``) while its country
    clause is UNRESOLVABLE -- the exact pair whose precedence decides whether a fee is
    silently skipped as "+0" or honestly reported as a gap.
    """
    outcome = bridge.ConditionOutcome
    resolution = bridge.resolve_row_geography(
        _row(connector="connector-x", datastream_id="dst_1"),
        geography=_geography(("emea", ("FR", "DE")), ("uk", ("GB",))),
        binding_index=bridge.BindingIndex(
            registry_available=True,
            market_ids_by_connector={"connector-x": frozenset({"emea"})},
        ),
    )
    # The premise of the composition, asserted so a fixture drift cannot silently turn
    # this test into a tautology over two MATCHes.
    assert resolution.market_id == "emea"
    assert resolution.country_code is None
    assert resolution.country_gap is not None
    assert resolution.country_gap.reason == bridge.REASON_NOT_SINGLE_COUNTRY

    country = bridge.evaluate_country_condition(["FR"], resolution)
    market = bridge.evaluate_market_condition(["uk"], resolution)
    assert country is outcome.UNRESOLVED
    assert market is outcome.NO_MATCH

    combined = bridge.combine_outcomes(country, market)
    assert combined is outcome.UNRESOLVED, (
        "a KNOWN FALSE market clause must not be allowed to answer for an UNRESOLVABLE"
        " country clause: asserting 'false' would skip a rule that might have applied and"
        " turn a gap into a fabricated +0"
    )
    # And stated the other way round, because argument order must not decide money.
    assert bridge.combine_outcomes(market, country) is outcome.UNRESOLVED


def test_21_the_gap_payload_carries_everything_a_surface_needs() -> None:
    resolution = bridge.resolve_row_geography(
        _row(connector="connector-x", datastream_id="dst_1"),
        geography=_NO_COUNTRY_MODEL,
        binding_index=_REGISTRY_EMPTY,
    )
    gap = resolution.country_gap
    assert gap.code in {bridge.GAP_COUNTRY_UNRESOLVED, bridge.GAP_MARKET_UNRESOLVED}
    assert gap.flags_total_incomplete is True
    assert gap.connector == "connector-x"
    assert gap.breakdown_dimension == "campaign_id"
    assert gap.datastream_id == "dst_1"
    json.dumps(gap.as_dict())  # JSON-serialisable, no Decimal / date / Enum leaking
    assert resolution.gap_codes() == (
        bridge.GAP_COUNTRY_UNRESOLVED,
        bridge.GAP_MARKET_UNRESOLVED,
    )


def test_22_no_unresolved_path_ever_fabricates_a_country() -> None:
    single = _geography(("france", ("FR",)))
    multi = _geography(("france", ("FR",)), ("uk", ("GB",)))
    unresolved_cases = [
        (_country_row("Freedonia"), _NO_COUNTRY_MODEL, _REGISTRY_EMPTY),
        (
            _row(connector="connector-x"),
            _geography(("france", ("FR", "MC"))),
            bridge.BindingIndex(
                registry_available=True,
                market_ids_by_connector={"connector-x": frozenset({"france"})},
            ),
        ),
        (
            _row(connector="connector-x"),
            single,
            bridge.BindingIndex(
                registry_available=True,
                market_ids_by_connector={"connector-x": frozenset({REST_OF_WORLD_NODE})},
            ),
        ),
        (
            _row(connector="connector-x"),
            multi,
            bridge.BindingIndex(
                registry_available=True,
                market_ids_by_connector={"connector-x": frozenset({"france", "uk"})},
            ),
        ),
        (_row(), multi, _REGISTRY_ABSENT),
        (_row(), multi, _REGISTRY_EMPTY),
    ]
    for row, geography, index in unresolved_cases:
        resolution = bridge.resolve_row_geography(row, geography=geography, binding_index=index)
        assert resolution.country_code is None
        assert resolution.resolution_source == ""

    # And the module ships no default-country constant of any kind.
    offenders = [
        name
        for name in dir(bridge)
        if isinstance(getattr(bridge, name), str)
        and re.fullmatch(r"[A-Z]{2}", getattr(bridge, name))
    ]
    assert not offenders, f"a two-letter country-shaped constant ships in the bridge: {offenders}"


# ===========================================================================
# Forward-compat + AD-2 (AC5, AC12)
# ===========================================================================


def _new_core_sources() -> list[tuple[Path, str]]:
    out = []
    for name in _NEW_CORE_MODULES:
        path = _SERVER_CORE / name
        assert path.exists(), f"expected new core module missing: {path}"
        out.append((path, path.read_text(encoding="utf-8")))
    return out


def test_23a_no_market_composition_literal_ships_in_the_new_modules() -> None:
    """Replays test_no_geographic_hardcode rule 1 locally (that file stays UNMODIFIED)."""
    offenders: list[str] = []
    for path, source in _new_core_sources():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if {"id", "label", "country_codes"} <= keys:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "a market composition ships as a code literal: " + ", ".join(offenders)


def test_23b_no_preset_or_default_market_constant_ships() -> None:
    """Replays rule 2."""
    pattern = re.compile(r"^\s*(?P<name>[A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=", re.MULTILINE)
    banned = re.compile(
        r"(PRESET|DEFAULT_MARKET|MARKET_DEFAULT|DEFAULT_COUNTRIES|"
        r"DEFAULT_COUNTRY_CODES|PRIORITY_COUNTRIES|MARKET_TEMPLATE)",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path, source in _new_core_sources():
        for match in pattern.finditer(source):
            if banned.search(match.group("name")):
                offenders.append(f"{path.name}:{match.group('name')}")
    assert not offenders, "a market preset / default ships as a constant: " + ", ".join(offenders)


def test_23c_no_iso_code_collection_ships_in_the_new_modules() -> None:
    """Replays rule 3: rates and country sets live in the seed, never in code."""
    code_re = re.compile(r"^[A-Z]{2}$")
    offenders: list[str] = []
    for path, source in _new_core_sources():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                continue
            elts = node.elts
            if len(elts) < 2:
                continue
            if all(
                isinstance(el, ast.Constant)
                and isinstance(el.value, str)
                and code_re.fullmatch(el.value)
                for el in elts
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "a literal ISO code collection ships: " + ", ".join(offenders)


def test_23d_no_connector_module_name_appears_in_the_new_modules() -> None:
    """AC5: the bridge branches on the dimension, the binding and the posture -- never on
    a connector. That is what makes Story 37.6 land with zero Epic 41 change."""
    module_names = sorted(
        path.name
        for path in _MODULES_DIR.iterdir()
        if path.is_dir() and (path / "manifest.json").exists()
    )
    assert module_names, "no connector modules discovered -- the guard would be vacuous"
    offenders: list[str] = []
    for path, source in _new_core_sources():
        lowered = source.lower()
        for name in module_names:
            if re.search(r"(?<![a-z0-9-])" + re.escape(name) + r"(?![a-z0-9-])", lowered):
                offenders.append(f"{path.name}:{name}")
    assert not offenders, "a connector name appears in a bridge module: " + ", ".join(offenders)


def test_24_the_bridge_reuses_epic_37_instead_of_forking_it() -> None:
    source = (_SERVER_CORE / "fee_tax_geo_bridge.py").read_text(encoding="utf-8")
    assert "from core.country_vocabulary import normalize_country_value" in source
    # Story 37.9: the reused Epic 37 object is the PUBLISHED projection, not the
    # `project_preferences` posture -- `GovernedGeography` is a reading of
    # `country_registry.GeographyProjection` and builds no second hierarchy.
    assert "from core.country_registry import GeographyProjection" in source
    assert "from core.country_registry import MARKET" in source
    assert "from core.geographic_reporting import" not in source
    assert (
        "from core.geographic_semantics import COUNTRY_ABSENT_BUCKET_ID, COUNTRY_PARTITION"
        in source
    )
    # No forked market index and no forked alias table.
    assert "market_index" not in source
    assert "alias_map" not in source
    # The non-budgetable refusal is still Epic 37's, only expressed differently: Story
    # 48.2 turned `Other markets` into a governed node whose id is per-Project, so
    # `geographic_reporting.resolve_market_binding` had nothing left to refuse BY NAME
    # -- it raised on "unknown market id" for every case that mattered. Looking the id
    # up in the published market list answers the same question with one branch and no
    # bucket name, which is why the refusal moved rather than being re-implemented.
    assert "def market_by_id" in source
    assert "resolve_market_binding" not in source


# ===========================================================================
# The dbt view contract (AC6) -- asserted offline
# ===========================================================================


def _model_sql() -> str:
    assert _MODEL_SQL.exists(), f"expected new dbt model missing: {_MODEL_SQL}"
    return _MODEL_SQL.read_text(encoding="utf-8")


def _final_select_aliases(sql: str) -> list[str]:
    """Aliases of the model's FINAL SELECT, in order."""
    lines = sql.splitlines()
    start = max(index for index, line in enumerate(lines) if line.strip() == "SELECT")
    end = next(
        index
        for index, line in enumerate(lines)
        if index > start and line.strip().upper().startswith("FROM ")
    )
    aliases: list[str] = []
    for line in lines[start + 1 : end]:
        match = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*,?\s*$", line)
        if match:
            aliases.append(match.group(1))
    return aliases


def test_25_the_view_emits_exactly_the_frozen_fourteen_column_contract() -> None:
    aliases = _final_select_aliases(_model_sql())
    assert aliases == list(_FROZEN_COLUMNS)
    assert len(aliases) == 14


def test_25b_the_revision_three_provenance_rename_landed_completely() -> None:
    sql = _model_sql()
    for literal in ("row_dimension", "declared_binding", "project_posture"):
        assert f"'{literal}'" in sql, f"the view never emits resolution_source={literal!r}"
    # The revision-2 names must not survive anywhere, or 41.3 would pin a dead string.
    assert "row_breakdown" not in sql
    assert "datastream_binding" not in sql
    # The gap label is the empty string, never a placeholder word.
    assert "ELSE ''" in sql


def test_26_the_view_reads_only_the_four_mirror_relations_and_the_fact() -> None:
    sql = _model_sql()
    sources = set(re.findall(r"source\(\s*'mirror'\s*,\s*'([a-z_]+)'\s*\)", sql))
    assert sources == {
        # Story 48.4 SPLIT what project_preferences used to answer, and Story 37.9 took
        # the rest: ACTIVATION moved to the one authority that agrees with
        # app.project_capabilities by construction, and GEOGRAPHY moved to the published
        # hierarchy version Analyze already reads. `project_preferences` supplies this
        # model nothing at all any more.
        "country_market_projection",
        "project_tax_fee_activation",
        "datastream_country_binding_dim",
        "datastreams_dim",
        "datastream_source_types",
    }
    refs = set(re.findall(r"ref\(\s*'([a-z_0-9]+)'\s*\)", sql))
    assert refs == {"fact_daily_kpi", "dim_country"}
    # It must not reach into Story 41.3's models.
    assert "fee_tax_ladder_daily" not in sql


def test_27_the_view_names_no_connector_and_no_iso_literal() -> None:
    # Comment lines are stripped first, for the reason `_sql_without_comments` was
    # written: prose cannot emit a value. Without it this guard became unsound the
    # day a connector module was named `bigquery` -- every core dbt model names
    # BigQuery in its dialect notes, so the check started reporting the warehouse
    # adapter as leaked connector vocabulary. The rule is unchanged; only what
    # counts as "in the view" is.
    sql = _sql_without_comments(_model_sql())
    literals = set(re.findall(r"'([^']*)'", sql))
    iso_shaped = sorted(item for item in literals if re.fullmatch(r"[A-Z]{2}", item))
    assert not iso_shaped, f"an ISO literal ships in the view: {iso_shaped}"

    module_names = sorted(
        path.name
        for path in _MODULES_DIR.iterdir()
        if path.is_dir() and (path / "manifest.json").exists()
    )
    lowered = sql.lower()
    offenders = [
        name
        for name in module_names
        if re.search(r"(?<![a-z0-9-])" + re.escape(name) + r"(?![a-z0-9-])", lowered)
    ]
    assert not offenders, f"a connector name ships in the view: {offenders}"


def _sql_without_comments(sql: str) -> str:
    """The SQL with its comments removed, so a literal named in prose is not mistaken
    for one the model can actually emit.

    BOTH comment forms, since 2026-08-10. Stripping ``--`` alone was enough while this
    guard read one model; widened to `dbt/macros/`, it started reporting the `'FR'` of
    `fee_tax_condition_matcher.sql:55` -- a worked example inside a ``{# ... #}`` block,
    which is documentation the macro cannot emit any more than a ``--`` line can.
    """
    without_jinja = re.sub(r"\{#.*?#\}", "", sql, flags=re.DOTALL)
    return "\n".join(
        line for line in without_jinja.splitlines() if not line.lstrip().startswith("--")
    )


#: Every `.sql` of this epic that can emit a number or a geography -- the seven marts
#: and the five macros. Derived by glob, never listed: a mart added tomorrow is covered
#: without anyone remembering this file.
def _fee_tax_sql_files() -> list[Path]:
    return sorted((_REPO_ROOT / "dbt" / "models" / "marts").glob("fee_tax_*.sql")) + sorted(
        (_REPO_ROOT / "dbt" / "macros").glob("fee_tax_*.sql")
    )


def _declared_two_letter_vocabulary() -> set[str]:
    """Two-letter values the fee/tax CONTRACT declares -- read from the schema yml, so
    the excuse expires the day the contract stops declaring them.

    Today this is exactly `{"HT"}`: a tax basis, not a country. Hand-listing it would
    have been a second vocabulary free to disagree with the first.
    """
    declared: set[str] = set()
    for path in sorted((_REPO_ROOT / "dbt" / "models" / "marts").glob("schema_fee_tax*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for model in document.get("models", []) or []:
            for column in model.get("columns", []) or []:
                for test in column.get("tests", []) or []:
                    if not isinstance(test, dict):
                        continue
                    accepted = test.get("accepted_values")
                    if not isinstance(accepted, dict):
                        continue
                    values = accepted.get("arguments", {}).get("values", accepted.get("values", []))
                    declared.update(str(value) for value in values or [])
    return {value for value in declared if re.fullmatch(r"[A-Z]{2}", value)}


def test_27c_no_tax_rate_and_no_iso_literal_ships_in_the_sql_that_computes_the_MONEY() -> None:
    """The two guards of this epic stopped at `.py` and at ONE model. Widened here.

    `tax-fees.md:42` -- *"rates are hardcoded in a connector or in core code"* -- was
    held by `test_16c` over `server/core/**.py` and `server/modules/**.py`, and the ISO
    rule by `test_27` over `fee_tax_country_resolution.sql` alone. **The code that
    actually computes the money is neither.** Measured on 2026-08-10, before this test
    existed:

        a VAT default injected into `fee_tax_rules_effective.sql`
            pytest (conformance + every epic-41 selector) -> 59 passed, 0 red
            dbt build --select "fee_tax_rules_effective+"  -> PASS=174 ERROR=0

        ISO literals 'FR'/'XX' injected into `fee_tax_ladder_daily.sql`
            pytest -> 101 passed, 0 red
            dbt build -> PASS=58 ERROR=0

    One client's tax policy applied to every other client passed every gate. That is
    the exact failure `tax-fees.md:42` names, in the one place where it reaches an
    invoice.

    Both predicates are the existing ones, unchanged -- only what they read is wider.
    """
    categories = re.compile(r"\b(VAT|DST|SALES_TAX|GST|AGENCY_FEE|REGULATORY_TAX|PLATFORM_FEE)\b")
    # A decimal literal next to a tax category on the SAME line. Narrow on purpose,
    # word for word with test_16c: a bare `0.15` is a legitimate threshold in a hundred
    # places, and a scan that flagged those would be turned off within a week.
    literal = re.compile(r"\b0\.\d+\b")
    excused = _declared_two_letter_vocabulary()

    files = _fee_tax_sql_files()
    assert len(files) >= 12, f"the glob found {len(files)} fee/tax .sql files, expected the 12+"

    shipped_rates: list[str] = []
    shipped_iso: list[str] = []
    for path in files:
        body = _sql_without_comments(path.read_text(encoding="utf-8"))
        for number, line in enumerate(body.splitlines(), start=1):
            if categories.search(line) and literal.search(line):
                shipped_rates.append(f"{path.name}:{number}: {line.strip()[:80]}")
        leaked = sorted(
            {
                found
                for found in re.findall(r"'([^']*)'", body)
                if re.fullmatch(r"[A-Z]{2}", found) and found not in excused
            }
        )
        if leaked:
            shipped_iso.append(f"{path.name}: {leaked}")

    # ONE assertion, not two in a row. Two sequential asserts make the first leak hide
    # the second: injecting a rate AND an ISO literal reported only the rate, and a
    # reader repairing it would have believed the file clean. That is this week's
    # "partial guard hides the rest", reproduced inside the guard written to close it.
    leaks = [f"tax/fee rate: {item}" for item in shipped_rates]
    leaks += [f"ISO literal: {item}" for item in shipped_iso]
    assert leaks == [], leaks


def _accepted_values(column: str) -> set[str]:
    document = yaml.safe_load(_SCHEMA_YML.read_text(encoding="utf-8"))
    columns = {item["name"]: item for item in document["models"][0]["columns"]}
    for test in columns[column].get("tests", []):
        if isinstance(test, dict) and "accepted_values" in test:
            return set(test["accepted_values"]["arguments"]["values"])
    raise AssertionError(f"no accepted_values test declared on {column}")


def test_27b_every_gap_reason_the_view_emits_is_declared_accepted() -> None:
    """The schema tests and the model cannot drift: a reason the view can emit but the
    yml does not accept fails `dbt test` in production, not here."""
    body = _sql_without_comments(_model_sql())
    for column in ("country_gap_reason", "market_gap_reason", "source_type_gap_reason"):
        assert "" in _accepted_values(column), f"{column} must accept '' for a resolved row"

    country_accepted = _accepted_values("country_gap_reason")
    market_accepted = _accepted_values("market_gap_reason")
    for reason in (
        "country_value_unmapped",
        "binding_ambiguous_for_connector",
        "binding_not_bindable",
        "market_not_single_country",
        "no_binding_and_country_model_absent",
        "no_binding_and_multiple_countries_governed",
        "country_outside_tracked_markets",
    ):
        assert f"'{reason}'" in body, f"the view never emits {reason!r}"
        assert reason in (country_accepted | market_accepted)

    # The Python bridge owns one more reason the mirror cannot distinguish (see the model
    # header): the SQL vocabulary is a strict SUBSET, never a divergent one.
    assert bridge.REASON_REGISTRY_UNAVAILABLE not in body
    assert (country_accepted | market_accepted) - {""} <= bridge.GAP_REASONS


def test_27c_a_gap_reason_says_what_actually_happened() -> None:
    """'nothing was declared' is NOT 'the signals disagree'.

    No signal disagreed -- none was offered. A mislabelled reason propagates into the 41.7
    rule matrix and sends an operator to reconcile a conflict that does not exist, so
    'signals_disagree' stays RESERVED for a declaration and a derivation that genuinely
    conflict (which the warehouse cannot observe today, because the manifest category is
    not mirrored).
    """
    body = _sql_without_comments(_model_sql())
    assert "'source_type_not_declared'" in body
    assert "'signals_disagree'" not in body

    accepted = _accepted_values("source_type_gap_reason")
    assert accepted == {
        "",
        "no_datastream_for_connector",
        "source_type_ambiguous_for_connector",
        "source_type_not_declared",
        "signals_disagree",
    }
    # Everything the view can emit is accepted; the reserved value is declared but unused.
    emitted = {
        "",
        "no_datastream_for_connector",
        "source_type_ambiguous_for_connector",
        "source_type_not_declared",
    }
    assert emitted < accepted
    for reason in emitted - {""}:
        assert f"'{reason}'" in body


def test_27d_the_view_is_module_gated_so_off_means_zero_rows() -> None:
    """Contract C.1: OFF => every new mart returns zero rows.

    Before the gate this view emitted one row per fact row for EVERY project on the
    platform -- including a project with no preferences row at all, which is OFF by
    definition -- and ran its not_null + 6-key unique schema tests over every org's entire
    fact table on every build.

    The gate must be an INNER join, not a LEFT join with a COALESCE default: an absent
    preferences row means OFF, and `WHERE COALESCE(flag, FALSE)` on a LEFT join would still
    have produced the row before filtering it, which is the same cost.
    """
    body = _sql_without_comments(_model_sql())
    # Story 48.4 moved the flag, not the gate. `fee_tax_alignment_enabled` was a
    # SECOND activation authority beside app.project_capabilities and this model
    # believed it, so a Project disabled through a Change Set kept resolving
    # countries. The assertion follows the authority rather than the column name.
    assert "tax_fees_active" in body, "the view does not read the activation authority"
    assert "fee_tax_alignment_enabled" not in body, (
        "the retired flag must not come back as a second gate"
    )
    assert "enabled_projects" in body
    assert re.search(r"JOIN\s+enabled_projects", body), "the gate must be an inner JOIN"
    assert not re.search(r"LEFT\s+JOIN\s+enabled_projects", body)


def test_27e_every_dialect_specific_construct_sits_behind_a_target_type_branch() -> None:
    """Portability, replayed locally over THIS model (the 41.3 guard covers only its own
    three, so it never reads this file).

    The rule is 41.3's section B.5 rule: an adapter-specific construct is allowed only
    inside a target.type branch. Everything else must be accepted by both DuckDB and
    BigQuery -- and a hard parse failure here would take the WHOLE dbt build down, every
    pre-existing mart included (C.8/11), which is not an acceptable blast radius for an
    additive overlay.
    """
    banned: tuple[tuple[str, str], ...] = (
        (r"::", "the postfix :: cast is DuckDB/Postgres syntax, not BigQuery"),
        (r"\bDOUBLE\b", "DOUBLE is not a BigQuery type"),
        (r"\bVARCHAR\b", "VARCHAR is DuckDB; STRING is accepted by both"),
        (r"\bTEXT\b", "TEXT is not a BigQuery type"),
        (r"\bDECIMAL\s*\(", "a parameterised DECIMAL(p,s) must live inside a branch"),
        (r"\bNUMERIC\s*\(", "a parameterised NUMERIC(p,s) must live inside a branch"),
        (r"\bQUALIFY\b", "QUALIFY is not portable"),
        (r"\blist_contains\b", "list_contains is DuckDB-only"),
        (r"\bstring_split\b", "string_split is DuckDB-only"),
        (r"\bjson_extract\b", "json_extract is DuckDB-only"),
        (r"\bfrom_json\b", "from_json is DuckDB-only"),
        (r"\bregexp_\w+", "regexp_full_match is DuckDB; REGEXP_CONTAINS is BigQuery"),
        (r"\bCONCAT_WS\b", "BigQuery has no CONCAT_WS"),
        (r"\bUNNEST\s*\(", "a SELECT-list UNNEST is DuckDB-only"),
        (r"\bDATE\s*-", "DATE - DATE is DuckDB-only"),
    )
    code = _sql_without_comments(_model_sql())

    # Story 37.9: THERE IS NO ADAPTER BRANCH ANY MORE, and its removal is a consequence
    # rather than a cleanup. The model carried exactly one `target.type == 'duckdb'`
    # exception because Epic 37's market list arrived as
    # `project_preferences.local_markets` JSON and needed `from_json` / `UNNEST`; the
    # whole model was therefore a deliberate no-op outside DuckDB and had never been
    # executed anywhere else. `mirror.country_market_projection` lands flat scalars, so
    # every construct is now accepted by both engines and the model means the same thing
    # on each. A branch reappearing here is a regression to prove, not to wave through.
    assert "market_json_supported" not in code
    assert "target.type" not in code, (
        "this model is portable end-to-end since Story 37.9; a new adapter branch needs "
        "its own justification in the header"
    )

    offenders = [why for pattern, why in banned if re.search(pattern, code, re.IGNORECASE)]
    assert not offenders, "a dialect-specific construct ships in the model: " + str(offenders)


def test_28_story_41_2_touched_neither_shared_mirror_file() -> None:
    """D1: Story 41.1 owns EVERY Epic-41 edit to mirror_sync.py and sources_mirror.yml."""
    for path in (_MIRROR_SYNC, _SOURCES_YML):
        text = path.read_text(encoding="utf-8")
        assert "fee_tax_country_resolution" not in text
        assert "fee_tax_geo_bridge" not in text
        assert "country_tax_defaults" not in text
    # And the model's schema tests live in their OWN yml, not in the shared marts schema.
    assert _SCHEMA_YML.exists()
    shared = (_REPO_ROOT / "dbt" / "models" / "marts" / "schema.yml").read_text(encoding="utf-8")
    assert "fee_tax_country_resolution" not in shared


# ===========================================================================
# The source_type ladder (AC7, AC12)
# ===========================================================================


def test_29_paid_media_derives_only_from_the_two_agreement_pairs() -> None:
    assert fts.derive_source_type("paid_media", "Spend") == fts.PAID_MEDIA
    assert fts.derive_source_type("paid_media", "Performance") == fts.PAID_MEDIA


def test_30_a_verification_feed_in_the_paid_media_category_stays_unknown() -> None:
    """DO NOT "fix" this to PAID_MEDIA.

    A verification feed sits in the paid_media manifest category and emits no cost metric
    at all (only measured / viewable impressions). Classifying it as PAID_MEDIA would drag
    it into net media -- exactly the KEEP_SEPARATE breach Story 41.4 exists to prevent.
    41.4 reaches those feeds through a VERIFICATION-category rule whose base_target is
    MEASURED_IMPRESSIONS, never through source_type, so UNKNOWN here is safe AND correct.
    """
    assert fts.derive_source_type("paid_media", "Operational") == fts.UNKNOWN
    assert fts.derive_source_type("analytics_product", "Operational") == fts.UNKNOWN


def test_31_the_other_agreement_pairs_derive_their_own_type() -> None:
    assert fts.derive_source_type("analytics_product", "Context") == fts.ORGANIC_ANALYTICS
    assert fts.derive_source_type("analytics_product", "Performance") == fts.ORGANIC_ANALYTICS
    assert (
        fts.derive_source_type("commerce_billing", "Revenue & conversions")
        == fts.COMMERCE_REVENUE
    )


def test_32_a_disagreement_or_a_missing_signal_is_unknown_and_never_raises() -> None:
    # An ad server is publisher revenue: neither media cost nor commerce revenue.
    assert fts.derive_source_type("ad_server", "Spend") == fts.UNKNOWN
    assert fts.derive_source_type(None, None) == fts.UNKNOWN
    assert fts.derive_source_type("", "") == fts.UNKNOWN


def test_33_the_exhaustive_sweep_never_silently_yields_paid_media() -> None:
    """8 manifest categories x (7 data roles + None) = 64 pairs, machine-proved."""
    pairs = [
        (category, role)
        for category in _CATEGORIES
        for role in (*_DATA_ROLES, None)
    ]
    assert len(pairs) == 64

    paid = []
    for category, role in pairs:
        value = fts.derive_source_type(category, role)
        assert value in fts.ALL_SOURCE_TYPES
        # Declaration-only types are derived for NO pair at all.
        assert value not in {fts.LEAD_GEN_MEDIA, fts.DIRECT_SERVICE_COST}
        if value == fts.PAID_MEDIA:
            paid.append((category, role))
    assert paid == [("paid_media", "Spend"), ("paid_media", "Performance")]


def test_34_the_vocabulary_cannot_drift_from_story_41_1() -> None:
    assert fts.ALL_SOURCE_TYPES == ftr.SOURCE_TYPES


def test_35_an_explicit_declaration_wins_over_the_derivation(monkeypatch) -> None:
    monkeypatch.setattr(
        fts, "fetch_declared_source_type", lambda _conn, _ds: (fts.LEAD_GEN_MEDIA, "alice@x")
    )
    monkeypatch.setattr(
        fts,
        "fetch_derivation_signals",
        lambda *_a, **_k: ("analytics_product", "Context"),
    )
    resolution = fts.resolve_source_type("dst_1", "prj_1", object())
    assert resolution.value == fts.LEAD_GEN_MEDIA
    assert resolution.source == fts.RESOLVED_BY_DECLARATION
    assert resolution.declared_by == "alice@x"
    assert resolution.excluded_from_cost_cascade is False


def test_36_a_declaration_outside_the_vocabulary_raises_and_is_never_downgraded(
    monkeypatch,
) -> None:
    monkeypatch.setattr(fts, "fetch_declared_source_type", lambda _conn, _ds: ("MEDIA", "bob@x"))
    with pytest.raises(fts.InvalidSourceType):
        fts.resolve_source_type("dst_1", "prj_1", object())


def test_36b_derivation_runs_when_nothing_is_declared(monkeypatch) -> None:
    monkeypatch.setattr(fts, "fetch_declared_source_type", lambda _conn, _ds: None)
    monkeypatch.setattr(
        fts, "fetch_derivation_signals", lambda *_a, **_k: ("paid_media", "Spend")
    )
    resolution = fts.resolve_source_type("dst_1", "prj_1", object())
    assert resolution.value == fts.PAID_MEDIA
    assert resolution.source == fts.RESOLVED_BY_DERIVATION
    assert resolution.declared_by is None


def test_36c_an_unreadable_store_degrades_to_a_typed_unknown_and_never_raises() -> None:
    def _boom(_sql: str, _params: Any) -> list[tuple]:
        raise RuntimeError("relation does not exist")

    resolution = fts.resolve_source_type("dst_1", "prj_1", _FakeConn(_boom))
    assert resolution.value == fts.UNKNOWN
    assert resolution.source == fts.RESOLVED_BY_DEFAULT
    assert resolution.excluded_from_cost_cascade is True


def test_37_unknown_is_excluded_from_the_cost_cascade_with_a_renderable_reason() -> None:
    for value in (fts.PAID_MEDIA, fts.LEAD_GEN_MEDIA, fts.DIRECT_SERVICE_COST):
        assert fts.is_in_cost_cascade(value) is True
        assert fts.exclusion_reason_for(value) == ""
    for value in (fts.COMMERCE_REVENUE, fts.ORGANIC_ANALYTICS, fts.UNKNOWN):
        assert fts.is_in_cost_cascade(value) is False
        assert fts.exclusion_reason_for(value).strip()
    assert fts.is_in_cost_cascade(None) is False


# ===========================================================================
# The binding index -- offline degradation
# ===========================================================================


def test_binding_index_reports_the_registry_absent_when_migration_104_is_unapplied() -> None:
    def _script(sql: str, _params: Any) -> list[tuple]:
        if "to_regclass" in sql:
            return [(False,)]
        raise AssertionError("no further query may run once the registry is absent")

    index = bridge.load_datastream_binding_index("prj_1", _FakeConn(_script))
    assert index.registry_available is False
    assert index.market_ids_for("connector-x") == frozenset()


def test_binding_index_joins_datastreams_to_obtain_the_connector() -> None:
    """The used-by store carries NEITHER a connector NOR a datastream_id column: the
    join on app.datastreams is what turns (consumer_kind, consumer_id) into a connector.

    Story 48.2 moved the store from app.market_bindings to the generic Master
    Data used-by contract, so the script answers the registry lookup first."""

    def _script(sql: str, _params: Any) -> list[tuple]:
        if "to_regclass" in sql:
            return [(True,)]
        if "FROM app.master_data_registries" in sql:
            return [("mdreg_1", "org_1", "prj_1", "country", "Country Registry",
                     "active", "mdver_1", None, None, "system", None, None)]
        if "FROM app.master_data_used_by" in sql:
            return [
                ("france", "datastream", "dst_1", None, None, "mdver_1", None),
                ("uk", "datastream", "dst_2", None, None, "mdver_1", None),
                ("france", "objective", "obj_1", None, None, "mdver_1", None),
                ("france", "datastream", "dst_missing", None, None, "mdver_1", None),
            ]
        if "FROM app.datastreams" in sql:
            return [("dst_1", "connector-x"), ("dst_2", "connector-y")]
        raise AssertionError(f"unexpected statement: {sql}")

    index = bridge.load_datastream_binding_index("prj_1", _FakeConn(_script))
    assert index.registry_available is True
    assert index.market_ids_for("connector-x") == frozenset({"france"})
    assert index.market_ids_for("connector-y") == frozenset({"uk"})
    # A non-datastream binding kind and a binding to a dead datastream are both dropped.
    assert index.market_ids_for("connector-z") == frozenset()


def test_binding_index_never_raises_into_a_reporting_read() -> None:
    def _script(sql: str, _params: Any) -> list[tuple]:
        if "to_regclass" in sql:
            return [(True,)]
        raise RuntimeError("registry momentarily unavailable")

    index = bridge.load_datastream_binding_index("prj_1", _FakeConn(_script))
    assert index.registry_available is False


def test_ambiguity_is_detected_from_the_set_of_distinct_markets() -> None:
    def _script(sql: str, _params: Any) -> list[tuple]:
        if "to_regclass" in sql:
            return [(True,)]
        if "FROM app.master_data_registries" in sql:
            return [("mdreg_1", "org_1", "prj_1", "country", "Country Registry",
                     "active", "mdver_1", None, None, "system", None, None)]
        if "FROM app.master_data_used_by" in sql:
            return [
                ("france", "datastream", "dst_1", None, None, "mdver_1", None),
                ("uk", "datastream", "dst_2", None, None, "mdver_1", None),
            ]
        return [("dst_1", "connector-x"), ("dst_2", "connector-x")]

    index = bridge.load_datastream_binding_index("prj_1", _FakeConn(_script))
    assert index.market_ids_for("connector-x") == frozenset({"france", "uk"})


# ===========================================================================
# Pg-gated
# ===========================================================================


@market_bindings_schema
def test_38_the_binding_index_reads_real_market_bindings() -> None:
    """Structural only: the probe reports the relation present and the index is honest."""
    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        index = bridge.load_datastream_binding_index("prj_does_not_exist", conn)
        assert index.registry_available is True
        assert index.market_ids_for("connector-x") == frozenset()
        conn.rollback()


@source_types_schema
def test_39_resolve_source_type_reads_the_real_tables() -> None:
    """An unknown datastream has no declaration and no signals -> a typed UNKNOWN."""
    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        resolution = fts.resolve_source_type("dst_does_not_exist", "prj_none", conn)
        assert resolution.value == fts.UNKNOWN
        assert resolution.source == fts.RESOLVED_BY_DEFAULT
        conn.rollback()
