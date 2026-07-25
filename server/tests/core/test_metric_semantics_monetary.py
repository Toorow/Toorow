"""Tests for Story 39.1 -- classify monetary metrics platform-wide (Epic 39).

Offline (no DB): the pure `_classify_monetary` classifier grounded against the real
dim_metric.csv seed (partition, ratio short-circuit, token families, AD-2 grep), the
`monetary` projection through parse_dim_metric / platform_defaults_from_seeds, the
preservation of the bit-identical seed<->defaults equivalence (invariant 3, `monetary`
excluded exactly as target_mart is), the `monetary` cascade through the pure reducer, the
`is_metric_monetary` fail-soft entry point, the datamodel `_detect_conflicts` tightened
(monetary-aware) CURRENCY_CONFLICT, and the gap-DECISION helpers (CURRENCY_GAP EMISSION is
deferred to Story 39.3, which adds the used-by source-currency provenance seam).

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 083
(additive BOOLEAN NOT NULL DEFAULT FALSE, replayable), the seeded import writing exactly 5
monetary defaults, idempotency (no monetary flip on re-import), the live PROJECT-override
cascade of `monetary`, and backfill safety. Pattern calqué sur test_metric_semantics.py /
test_dataset_access_grants.py.
"""

from __future__ import annotations

import csv as _csv
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import datamodel  # noqa: E402
from core import metric_semantics as ms  # noqa: E402

# ---------------------------------------------------------------------------
# Postgres availability check (calqué sur test_metric_semantics.py)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEEDS_DIR = _REPO_ROOT / "dbt" / "seeds"
_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"
_MIGRATION_083 = (
    _REPO_ROOT / "infra" / "nango" / "migrations" / "083_metric_definitions_monetary.sql"
)

# The 5 monetary metrics of the 19 canonical seed metrics (the intended partition).
_EXPECTED_MONETARY = {"cost", "revenue", "ad_revenue", "all_revenue", "conversions_value"}


def _independent_seed_names() -> list[str]:
    """Re-derive the ordered canonical metric names straight from dim_metric.csv (no module)."""
    names: list[str] = []
    with (_SEEDS_DIR / "dim_metric.csv").open("r", newline="", encoding="utf-8") as handle:
        for row in _csv.DictReader(handle):
            name = (row.get("name") or "").strip()
            if name:
                names.append(name)
    return names


# ===========================================================================
# Offline -- the pure classifier
# ===========================================================================


def test_classifier_partition_over_real_seed():
    """§1: _classify_monetary partitions the seed metrics into exactly the 5 monetary set.

    Grounded by reading dim_metric.csv INDEPENDENTLY (not a frozen literal duplicating the
    classifier): read the CSV names, apply the classifier, assert the partition."""
    names = _independent_seed_names()
    monetary = {n for n in names if ms._classify_monetary(n)}
    assert monetary == _EXPECTED_MONETARY
    # every other seed metric is non-monetary
    assert all(not ms._classify_monetary(n) for n in names if n not in _EXPECTED_MONETARY)


def test_classifier_distinguishes_conversions_pair():
    """§2: conversions (count) is non-monetary; conversions_value (money) is monetary."""
    assert ms._classify_monetary("conversions") is False
    assert ms._classify_monetary("conversions_value") is True


def test_classifier_ratios_short_circuit():
    """§3: ratios are non-monetary even when their numerator is monetary (roas->revenue)."""
    for ratio in ("roas", "ctr", "cpa", "viewability_rate"):
        assert ms._classify_monetary(ratio) is False
    # explicit ratio aggregation_type also short-circuits, even for a money-token name.
    assert ms._classify_monetary("cost_per_something", aggregation_type="ratio") is False


def test_classifier_token_families():
    """§4: money-token families -> True; ratio / non-money -> False."""
    for money in ("net_media_cost", "platform_fee", "refund_amount", "gross_revenue",
                  "media_spend", "order_value"):
        assert ms._classify_monetary(money) is True, money
    for non_money in ("impression_share", "bounce_rate", "session_count", "click_count"):
        assert ms._classify_monetary(non_money) is False, non_money


def test_classifier_empty_and_unknown_fail_soft():
    """§4b: empty / unknown names classify to False (no crash, no naked-amount false positive)."""
    assert ms._classify_monetary("") is False
    assert ms._classify_monetary("made_up_metric") is False


def test_ad2_no_provider_name_in_classifier_constants():
    """§5 (AD-2): the classifier + its constants carry ZERO provider/connector vocabulary.

    Only metric-dictionary vocabulary (money nouns) is allowed -- same status as the ratio
    name sets. Assert no known provider name appears in the money constants nor the classifier
    docstring/body."""
    import inspect

    blob = (
        " ".join(sorted(ms._MONETARY_EXACT_NAMES))
        + " "
        + ms._MONETARY_TOKEN_RE.pattern
        + " "
        + inspect.getsource(ms._classify_monetary)
    ).lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust",
        "facebook", "pinterest", "amazon", "microsoft", "snapchat",
    ]
    hits = [name for name in forbidden if name in blob]
    assert not hits, f"provider name(s) in the monetary classifier: {hits}"


# ===========================================================================
# Offline -- projection & invariant 3
# ===========================================================================


def test_parse_dim_metric_emits_monetary():
    """§6: parse_dim_metric emits a `monetary` key on every definition dict, with the partition."""
    defs = {d["canonical_name"]: d for d in ms.parse_dim_metric(_SEEDS_DIR)}
    for name, definition in defs.items():
        assert "monetary" in definition
        assert definition["monetary"] is (name in _EXPECTED_MONETARY)


def test_platform_defaults_carry_monetary():
    """§7: platform_defaults_from_seeds()["definitions"] carries monetary."""
    projection = ms.platform_defaults_from_seeds(_SEEDS_DIR)
    by_name = {d["canonical_name"]: d for d in projection["definitions"]}
    assert by_name["cost"]["monetary"] is True
    assert by_name["sessions"]["monetary"] is False


def test_invariant3_ignores_monetary(tmp_path):
    """§8: the bit-identical seed<->defaults equivalence still passes with monetary present.

    The equivalence compares projection definitions against a referential rebuilt from the
    seed text -- with `monetary` STRIPPED (as target_mart is stripped from the groups
    comparison). Proof it is genuinely ignored: mutating the projection's monetary values
    keeps the (monetary-stripped) equivalence green, while a full-content comparison notices.
    """
    seed_dim = (
        "name,additive,aggregation_rule,ratio_numerator,ratio_denominator\n"
        "revenue,true,sum,,\n"
        "cost,true,sum,,\n"
        "sessions,true,sum,,\n"
        "roas,false,ratio,revenue,cost\n"
    )
    seed_prio = "metric,connector,priority\nrevenue,src_a,1\nrevenue,src_b,2\n"
    (tmp_path / "dim_metric.csv").write_text(seed_dim, encoding="utf-8")
    (tmp_path / "metric_source_priority.csv").write_text(seed_prio, encoding="utf-8")

    projection = ms.platform_defaults_from_seeds(tmp_path)

    def _strip_monetary(defs):
        return [{k: v for k, v in d.items() if k != "monetary"} for d in defs]

    expected_definitions = [
        {"canonical_name": "revenue", "aggregation_type": "sum", "additive": True,
         "ratio_numerator": None, "ratio_denominator": None},
        {"canonical_name": "cost", "aggregation_type": "sum", "additive": True,
         "ratio_numerator": None, "ratio_denominator": None},
        {"canonical_name": "sessions", "aggregation_type": "sum", "additive": True,
         "ratio_numerator": None, "ratio_denominator": None},
        {"canonical_name": "roas", "aggregation_type": "ratio", "additive": False,
         "ratio_numerator": "revenue", "ratio_denominator": "cost"},
    ]
    # Invariant 3 (monetary-stripped) holds.
    assert _strip_monetary(projection["definitions"]) == expected_definitions

    # Monetary IS present and correct on the full projection.
    by_name = {d["canonical_name"]: d for d in projection["definitions"]}
    assert by_name["revenue"]["monetary"] is True
    assert by_name["sessions"]["monetary"] is False

    # Mutating monetary does NOT break the stripped equivalence, but WOULD break a
    # full-content comparison -- proving monetary is deliberately excluded from invariant 3.
    mutated = [dict(d) for d in projection["definitions"]]
    mutated[0]["monetary"] = not mutated[0]["monetary"]
    assert _strip_monetary(mutated) == expected_definitions  # stripped: still equal
    assert mutated != projection["definitions"]  # full-content: differs


def test_invariant3_real_seed_stripped(tmp_path):
    """§8b: over the REAL seed, the monetary-stripped projection matches an independent read.

    Robustesse seeds mouvants: re-derive additive/aggregation/ratio per metric from the CSV
    independently and assert the projection matches once monetary is stripped -- the genuine
    two-implementation cross-check unaffected by monetary's presence."""
    projection = ms.platform_defaults_from_seeds(_SEEDS_DIR)
    independent: dict[str, dict] = {}
    with (_SEEDS_DIR / "dim_metric.csv").open("r", newline="", encoding="utf-8") as handle:
        for row in _csv.DictReader(handle):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            independent[name] = {
                "canonical_name": name,
                "aggregation_type": (row.get("aggregation_rule") or "").strip() or None,
                "additive": (row.get("additive") or "").strip().lower() == "true",
                "ratio_numerator": (row.get("ratio_numerator") or "").strip() or None,
                "ratio_denominator": (row.get("ratio_denominator") or "").strip() or None,
            }
    for definition in projection["definitions"]:
        stripped = {k: v for k, v in definition.items() if k != "monetary"}
        assert stripped == independent[definition["canonical_name"]]


# ===========================================================================
# Offline -- cascade of `monetary` (pure reducer, in-memory rows)
# ===========================================================================


def test_monetary_cascades_project_over_platform():
    """§9: reduce_definitions_by_specificity -- PROJECT monetary override wins over PLATFORM.

    monetary is just another column on the definition row, so a PROJECT override cascades
    with NO new reducer logic."""
    rows = [
        {"canonical_name": "cost", "monetary": True, "scope_level": "PLATFORM",
         "org_id": None, "project_id": None},
        {"canonical_name": "cost", "monetary": False, "scope_level": "PROJECT",
         "org_id": None, "project_id": "proj_x"},
    ]
    resolved = ms.reduce_definitions_by_specificity(rows)
    assert resolved["cost"]["monetary"] is False  # project wins


def test_is_metric_monetary_platform_fallback():
    """§10: is_metric_monetary (no project) -> classifier fallback when DB has no row.

    With no reachable/loaded PLATFORM row, is_metric_monetary fails soft to _classify_monetary:
    revenue -> True, sessions -> False."""
    assert ms.is_metric_monetary("revenue") is True
    assert ms.is_metric_monetary("sessions") is False


def test_is_metric_monetary_unknown_fail_soft():
    """§11: is_metric_monetary on an unknown name -> False (fail-soft, no crash)."""
    assert ms.is_metric_monetary("made_up_metric") is False


# ===========================================================================
# Offline -- detector wiring (datamodel._detect_conflicts, synthetic fields)
# ===========================================================================


@pytest.fixture
def _monetary_by_classifier(monkeypatch):
    """Route _detect_conflicts' monetary check through the PURE classifier (no DB).

    _detect_conflicts -> _field_is_monetary -> metric_semantics.is_metric_monetary. Offline we
    patch is_metric_monetary to the pure classifier so the detector tests never touch Postgres
    (and never pay a connection timeout)."""
    monkeypatch.setattr(
        ms, "is_metric_monetary",
        lambda name, *, project_id=None: ms._classify_monetary(name),
    )
    return monkeypatch


def _codes(conflicts: list[dict]) -> set[str]:
    return {c["code"] for c in conflicts}


def test_currency_conflict_still_fires_on_monetary_multisource(_monetary_by_classifier):
    """§12: a monetary field fed by >=2 modules -> CURRENCY_CONFLICT (no regression)."""
    field = {"name": "cost", "data_type": "currency", "field_kind": "metric", "measure": "sum"}
    used_by = [
        {"module_name": "m1", "datastream_name": "d1"},
        {"module_name": "m2", "datastream_name": "d2"},
    ]
    conflicts = datamodel._detect_conflicts(field, used_by)
    assert "CURRENCY_CONFLICT" in _codes(conflicts)


def test_currency_gap_decision_helpers(_monetary_by_classifier):
    """§13: the gap DECISION helpers are correct AND (Story 39.3) EMISSION is now re-enabled.

    39.1 shipped the classifier + the gap-decision helpers dormant; Story 39.3 added the
    used-by currency provenance seam + taught conflict_resolutions.list_conflicts to render
    the code, then RE-ENABLED emission. So a monetary field whose feeding stream carries no
    resolvable source currency now emits CURRENCY_GAP (fail-closed, E39-FR02)."""
    field = {"name": "revenue", "data_type": "currency", "field_kind": "metric", "measure": "sum"}
    used_by_no_ccy = [{"module_name": "m1", "datastream_name": "d1"}]
    # decision: monetary field + no resolvable source currency == gap
    assert datamodel._field_is_monetary(field, None) is True
    assert datamodel._used_by_has_currency(used_by_no_ccy[0]) is False
    # Story 39.3: emission is now live.
    conflicts = datamodel._detect_conflicts(field, used_by_no_ccy)
    assert "CURRENCY_GAP" in _codes(conflicts)


def test_used_by_has_currency_true_when_present(_monetary_by_classifier):
    """§13b: a used-by row WITH a resolvable source-currency signal is detected."""
    assert datamodel._used_by_has_currency({"module_name": "m1", "currency": "EUR"}) is True
    assert datamodel._used_by_has_currency({"module_name": "m1", "source_currency": "USD"}) is True
    assert datamodel._used_by_has_currency({"module_name": "m1", "currency": "  "}) is False


def test_non_monetary_field_no_gap_but_legacy_conflict(_monetary_by_classifier):
    """§14: a non-monetary decimal field fed by 2 modules -> legacy CURRENCY_CONFLICT (superset-
    compatible, no regression) but NEVER CURRENCY_GAP (it is not money)."""
    field = {"name": "sessions", "data_type": "decimal", "field_kind": "metric", "measure": "sum"}
    used_by = [
        {"module_name": "m1", "datastream_name": "d1"},
        {"module_name": "m2", "datastream_name": "d2"},
    ]
    conflicts = datamodel._detect_conflicts(field, used_by)
    codes = _codes(conflicts)
    assert "CURRENCY_CONFLICT" in codes  # legacy decimal heuristic preserved
    assert "CURRENCY_GAP" not in codes  # not monetary -> no gap


def test_monetary_multisource_fires_conflict_and_gap(_monetary_by_classifier):
    """§15: monetary + multi-source WITH no resolvable source currency -> CURRENCY_CONFLICT AND
    (Story 39.3, emission re-enabled) CURRENCY_GAP both fire: the sources may disagree AND none
    declares a currency (fail-closed)."""
    field = {"name": "cost", "data_type": "currency", "field_kind": "metric", "measure": "sum"}
    used_by = [
        {"module_name": "m1", "datastream_name": "d1"},
        {"module_name": "m2", "datastream_name": "d2"},
    ]
    codes = _codes(datamodel._detect_conflicts(field, used_by))
    assert "CURRENCY_CONFLICT" in codes
    assert "CURRENCY_GAP" in codes


def test_monetary_multisource_with_currency_fires_conflict_not_gap(_monetary_by_classifier):
    """§15b: monetary + multi-source WHERE streams declare a source currency -> CURRENCY_CONFLICT
    fires (they may still differ) but NO CURRENCY_GAP (a currency is resolvable)."""
    field = {"name": "cost", "data_type": "currency", "field_kind": "metric", "measure": "sum"}
    used_by = [
        {"module_name": "m1", "datastream_name": "d1", "source_currency": "USD"},
        {"module_name": "m2", "datastream_name": "d2", "source_currency": "EUR"},
    ]
    codes = _codes(datamodel._detect_conflicts(field, used_by))
    assert "CURRENCY_CONFLICT" in codes
    assert "CURRENCY_GAP" not in codes


def test_detect_conflicts_fail_soft_when_classifier_raises(monkeypatch):
    """§16: _detect_conflicts never raises when the monetary resolution blows up.

    Monkeypatch is_metric_monetary to raise; the field degrades to non-monetary (no crash),
    conflicts still compute (only the legacy path may fire)."""
    def _boom(name, *, project_id=None):
        raise RuntimeError("metric_semantics unavailable")

    monkeypatch.setattr(ms, "is_metric_monetary", _boom)
    field = {"name": "cost", "data_type": "currency", "field_kind": "metric", "measure": "sum"}
    used_by = [
        {"module_name": "m1", "datastream_name": "d1"},
        {"module_name": "m2", "datastream_name": "d2"},
    ]
    conflicts = datamodel._detect_conflicts(field, used_by)  # must not raise
    codes = _codes(conflicts)
    # degraded to non-monetary: no GAP; legacy currency heuristic still fires.
    assert "CURRENCY_GAP" not in codes
    assert "CURRENCY_CONFLICT" in codes


# ===========================================================================
# Live Postgres -- migration 083 DDL, seeded import, idempotency, cascade
# ===========================================================================


def _apply_migration(conn, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _ensure_set_updated_at(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS app")
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger AS $$
            BEGIN NEW.updated_at = now(); RETURN NEW; END;
            $$ LANGUAGE plpgsql
            """
        )
    conn.commit()


def _bootstrap(conn) -> None:
    """Apply 049 then 083 idempotently to a throwaway DB."""
    _ensure_set_updated_at(conn)
    _apply_migration(conn, _MIGRATION_049)
    _apply_migration(conn, _MIGRATION_083)


@pg_available
def test_083_ddl_adds_monetary_column_replayable():
    """§17: after 083, monetary is BOOLEAN NOT NULL DEFAULT FALSE; re-applying 083 is a no-op."""
    from core.db import get_connection

    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration(conn, _MIGRATION_049)
        _apply_migration(conn, _MIGRATION_083)
        _apply_migration(conn, _MIGRATION_083)  # replay must not error
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'app' AND table_name = 'metric_definitions'
                  AND column_name = 'monetary'
                """
            )
            row = cur.fetchone()
    assert row is not None
    data_type, is_nullable, column_default = row
    assert data_type == "boolean"
    assert is_nullable == "NO"
    assert "false" in (column_default or "").lower()


@pg_available
def test_import_writes_five_monetary_defaults():
    """§18: import writes exactly 5 monetary PLATFORM defaults (the rest FALSE)."""
    from core.db import get_connection

    with get_connection() as conn:
        _bootstrap(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_name FROM app.metric_definitions "
                "WHERE scope_level = 'PLATFORM' AND monetary ORDER BY canonical_name"
            )
            monetary_names = {r[0] for r in cur.fetchall()}
    assert monetary_names == _EXPECTED_MONETARY


@pg_available
def test_import_monetary_is_idempotent():
    """§19: a second import changes no monetary value and emits no monetary-flip audit."""
    from core.db import get_connection

    with get_connection() as conn:
        _bootstrap(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app.metric_semantics_audit")
            audit_before = cur.fetchone()[0]

    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)  # re-run

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app.metric_semantics_audit")
            audit_after = cur.fetchone()[0]
            # monetary values unchanged (still exactly the 5).
            cur.execute(
                "SELECT COUNT(*) FROM app.metric_definitions "
                "WHERE scope_level = 'PLATFORM' AND monetary"
            )
            assert cur.fetchone()[0] == len(_EXPECTED_MONETARY)
    assert audit_after == audit_before  # no upserted audit on the stable classification


@pg_available
def test_monetary_cascade_project_override_live():
    """§20: a PROJECT cost definition with monetary=FALSE overrides the PLATFORM TRUE.

    resolve_metric_definitions(project)["cost"]["monetary"] == False, and
    is_metric_monetary("cost", project_id=project) == False."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"mon_org_{suffix}"
    project_id = f"mon_proj_{suffix}"

    with get_connection() as conn:
        _bootstrap(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, f"MonOrg-{suffix}", f"mon-org-{suffix}"),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (project_id, org_id, f"MonProj-{suffix}"),
            )
        conn.commit()
    try:
        # PLATFORM cost is monetary.
        assert ms.is_metric_monetary("cost") is True
        # PROJECT override -> non-monetary.
        ms.upsert_metric_definition(
            canonical_name="cost",
            aggregation_type="sum",
            additive=True,
            monetary=False,
            scope_level=ms.SCOPE_PROJECT,
            project_id=project_id,
            created_by="system",
        )
        resolved = ms.resolve_metric_definitions(project_id)
        assert resolved["cost"]["monetary"] is False
        assert ms.is_metric_monetary("cost", project_id=project_id) is False
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()


@pg_available
def test_backfill_safety_pre083_row_defaults_false():
    """§21: a row inserted WITHOUT monetary reads FALSE (NOT NULL default holds), never NULL."""
    from core.db import get_connection

    with get_connection() as conn:
        _bootstrap(conn)
        name = f"legacy_metric_{uuid.uuid4().hex[:8]}"
        with conn.cursor() as cur:
            # Insert omitting monetary -> the DEFAULT FALSE applies (simulates a pre-083 row).
            cur.execute(
                "INSERT INTO app.metric_definitions "
                "(id, canonical_name, aggregation_type, additive, scope_level, created_by) "
                "VALUES (%s, %s, 'sum', true, 'PLATFORM', 'system')",
                (f"metdef_{uuid.uuid4().hex}", name),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT monetary FROM app.metric_definitions WHERE canonical_name = %s",
                (name,),
            )
            value = cur.fetchone()[0]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.metric_definitions WHERE canonical_name = %s", (name,))
        conn.commit()
    assert value is False
