"""Story 41.2 -- the country tax defaults seed, its loader, and auto-population.

Offline (no Postgres): the governed seed parses fail-closed with an enforced non-empty
``source_note`` on every row; the ``dedup_hash`` recipe is stable, order-insensitive and
blind to the rate; auto-population is OFF-BY-DEFAULT, idempotent, and never overwrites or
resurrects a human decision. Every write goes through Story 41.1's audited store -- this
story issues no SQL against ``app.fee_tax_rules`` at all.

Pg-gated (skipped, with an explicit reason, unless TEST_POSTGRES_DSN points at a database
where migration 119 AND the dedup_hash column are present): the partial UNIQUE that makes
idempotency a DATABASE guarantee rather than a hope.

The seed ships DEFAULTS REQUIRING OPERATOR CONFIRMATION, NOT TAX ADVICE: every rule it
proposes lands ``status='proposed'`` and is inert until a human confirms it. Several tests
below exist purely to keep that honest.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from core import country_activation as activation
from core import fee_tax_country_defaults as defaults
from core import fee_tax_rules as ftr
from core import geographic_reporting as geo  # noqa: F401 -- GLOBAL / LOCAL_MARKETS below
from core.country_vocabulary import get_supported_country_codes
from core.geographic_reporting import LOCAL_MARKETS, GeographicPosture, Market
from core.geographic_semantics import UNKNOWN_BUCKET_ID

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_CSV = _REPO_ROOT / "dbt" / "seeds" / "country_tax_defaults.csv"
_SEED_NOTICE = _REPO_ROOT / "dbt" / "seeds" / "country_tax_defaults.NOTICE.md"
_MODULE = _REPO_ROOT / "server" / "core" / "fee_tax_country_defaults.py"

_SEED_HEADER = "iso_code,tax_category,form,rate,label,source_note,effective_from"
_SEED_ROW_COUNT = 19

TEST_ORG_ID = "org_test_fixture"


# ---------------------------------------------------------------------------
# Postgres availability.
# ---------------------------------------------------------------------------


def _pg_state() -> tuple[bool, bool]:
    """(postgres reachable, migration 119 applied WITH the dedup_hash column)."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False, False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('app.fee_tax_rules') IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM information_schema.columns "
                    "            WHERE table_schema = 'app' AND table_name = 'fee_tax_rules' "
                    "              AND column_name = 'dedup_hash')"
                )
                applied = bool(cur.fetchone()[0])
        return True, applied
    except Exception:
        return False, False


_PG_REACHABLE, _PG_HAS_DEDUP = _pg_state()

dedup_schema = pytest.mark.skipif(
    not (_PG_REACHABLE and _PG_HAS_DEDUP),
    reason="migration 119 / the fee_tax_rules.dedup_hash column is absent on TEST_POSTGRES_DSN",
)


@pytest.fixture(autouse=True)
def _clear_seed_cache():
    defaults.get_country_tax_defaults.cache_clear()
    yield
    defaults.get_country_tax_defaults.cache_clear()


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.statements)


class _FakeStore:
    """Stands in for Story 41.1's audited rule store."""

    def __init__(
        self,
        *,
        active: bool = True,
        existing: list[dict[str, Any]] | None = None,
        inserted: bool = True,
    ) -> None:
        self.active = active
        self.existing = existing or []
        self.inserted = inserted
        self.active_calls = 0
        self.list_calls = 0
        self.created: list[dict[str, Any]] = []

    def is_fee_tax_alignment_active(self, project_id: str, conn: object) -> bool:
        self.active_calls += 1
        return self.active

    def list_rules(self, project_id: str, conn: object, **_kw: Any) -> list[dict[str, Any]]:
        self.list_calls += 1
        return list(self.existing)

    def create_rule(
        self,
        conn: object,
        *,
        project_id: str,
        rule: dict[str, Any],
        created_by: str,
        **_kw: Any,
    ) -> dict[str, Any]:
        self.created.append(rule)
        row = dict(rule)
        row["project_id"] = project_id
        row["created_by"] = created_by
        row["inserted"] = self.inserted
        return row


def _install(monkeypatch, store: _FakeStore, posture: GeographicPosture) -> None:
    monkeypatch.setattr(
        ftr, "is_fee_tax_alignment_active", store.is_fee_tax_alignment_active
    )
    monkeypatch.setattr(ftr, "list_rules", store.list_rules)
    monkeypatch.setattr(ftr, "create_rule", store.create_rule)
    # Story 37.9: the stub follows the reader. `auto_populate_tax_rules` read
    # `project_preferences` through `geographic_reporting`, which the ratified Country
    # capability replaced and never writes back -- so a governed Project read as Global
    # and every tax default was skipped. It now reads the one governed reader, and a
    # stub left on the old symbol would have kept these tests green against a function
    # production no longer calls.
    monkeypatch.setattr(activation, "governed_posture", lambda *_a, **_k: posture)


def _posture(*markets: tuple[str, tuple[str, ...]]) -> GeographicPosture:
    return GeographicPosture(
        mode=LOCAL_MARKETS,
        markets=tuple(
            Market(id=market_id, label=market_id.upper(), country_codes=codes)
            for market_id, codes in markets
        ),
    )


def _write_csv(tmp_path: Path, rows: list[str], header: str = _SEED_HEADER) -> Path:
    path = tmp_path / "country_tax_defaults.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


_GOOD_ROW = (
    "FR,REGULATORY_TAX,PERCENTAGE,0.030000,France DST 3%,"
    "Default starting point; confirm with your tax advisor.,2019-01-01"
)


# ===========================================================================
# The seed loader (AC8, AC13)
# ===========================================================================


def test_40_the_seed_parses_with_exact_decimals_and_real_dates() -> None:
    rows = defaults.load_country_tax_defaults()
    assert len(rows) == _SEED_ROW_COUNT
    for row in rows:
        assert isinstance(row.rate, Decimal)  # NEVER a float: 0.03 is not exact in binary
        assert not isinstance(row.rate, float)
        assert isinstance(row.effective_from, date)
        assert row.form == defaults.SEED_FORM
        assert row.tax_category in defaults.SEED_TAX_CATEGORIES
        assert Decimal(0) <= row.rate < Decimal(1)


def test_41_every_single_row_carries_a_non_empty_source_note() -> None:
    """The honesty gate (E41-NFR03): a default nobody can explain must not ship."""
    rows = defaults.load_country_tax_defaults()
    empty = [row.iso_code for row in rows if not row.source_note.strip()]
    assert not empty, f"rows without a source_note: {empty}"
    assert all(row.label.strip() for row in rows)


def test_42_every_seeded_code_is_in_the_governed_iso_vocabulary() -> None:
    supported = get_supported_country_codes()
    unknown = sorted(
        {row.iso_code for row in defaults.load_country_tax_defaults()} - supported
    )
    assert not unknown, f"seeded codes outside the governed vocabulary: {unknown}"


@pytest.mark.parametrize(
    ("header", "rows"),
    [
        # missing header column
        ("iso_code,tax_category,form,rate,label,effective_from", [_GOOD_ROW]),
        # unknown tax_category
        (
            _SEED_HEADER,
            ["FR,AGENCY_FEE,PERCENTAGE,0.030000,x,a note,2019-01-01"],
        ),
        # a form other than PERCENTAGE
        (_SEED_HEADER, ["FR,REGULATORY_TAX,FLAT,0.030000,x,a note,2019-01-01"]),
        # a rate outside [0, 1)
        (_SEED_HEADER, ["FR,REGULATORY_TAX,PERCENTAGE,1.500000,x,a note,2019-01-01"]),
        # an empty source_note
        (_SEED_HEADER, ["FR,REGULATORY_TAX,PERCENTAGE,0.030000,x,,2019-01-01"]),
        # an unparseable effective_from
        (_SEED_HEADER, ["FR,REGULATORY_TAX,PERCENTAGE,0.030000,x,a note,not-a-date"]),
        # a code outside the governed ISO vocabulary
        (_SEED_HEADER, ["ZZ,REGULATORY_TAX,PERCENTAGE,0.030000,x,a note,2019-01-01"]),
        # a duplicate (iso_code, tax_category, effective_from)
        (_SEED_HEADER, [_GOOD_ROW, _GOOD_ROW]),
    ],
)
def test_43_the_loader_is_fail_closed(tmp_path: Path, header: str, rows: list[str]) -> None:
    path = _write_csv(tmp_path, rows, header=header)
    with pytest.raises(defaults.CountryTaxDefaultsError):
        defaults.load_country_tax_defaults(path)


def test_44_the_seeds_dir_override_is_honoured(tmp_path: Path, monkeypatch) -> None:
    # Warm the shared ISO vocabulary from the REAL seeds dir first: the loader validates
    # against it and the override must not have to carry a copy of dim_country.csv.
    get_supported_country_codes()

    _write_csv(
        tmp_path,
        [
            _GOOD_ROW,
            "GB,SALES_TAX,PERCENTAGE,0.200000,UK VAT,Standard rate; confirm.,2011-01-04",
        ],
    )
    monkeypatch.setenv("TOOROW_DBT_SEEDS_DIR", str(tmp_path))
    assert defaults.default_tax_seed_path() == tmp_path / "country_tax_defaults.csv"
    assert len(defaults.load_country_tax_defaults()) == 2


def test_45_the_seed_is_cached_and_the_cache_can_be_cleared() -> None:
    first = defaults.get_country_tax_defaults()
    assert defaults.get_country_tax_defaults() is first
    defaults.get_country_tax_defaults.cache_clear()
    second = defaults.get_country_tax_defaults()
    assert second is not first
    assert second == first


def test_46_a_country_with_no_seeded_default_returns_empty_and_never_raises() -> None:
    """No federal VAT exists there; a single national rate would be a fabrication."""
    assert defaults.defaults_for_country("US") == ()
    assert defaults.defaults_for_country(None) == ()
    assert len(defaults.defaults_for_country("fr")) == 2  # case-insensitive lookup


def test_47_no_python_rate_map_ships_in_the_loader_module() -> None:
    """Rates live in the seed. A dict of ISO code -> rate here is a defect (C.3 / AC8)."""
    source = _MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_MODULE))
    code_re = re.compile(r"^[A-Z]{2}$")

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            iso_keys = [
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and code_re.fullmatch(key.value)
            ]
            assert not iso_keys, f"an ISO-keyed dict ships at line {node.lineno}: {iso_keys}"
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            elts = node.elts
            if len(elts) >= 2 and all(
                isinstance(el, ast.Constant)
                and isinstance(el.value, str)
                and code_re.fullmatch(el.value)
                for el in elts
            ):
                raise AssertionError(f"an ISO code collection ships at line {node.lineno}")

    # No module-level constant may hold a rate-shaped number.
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
            assert not (0 < value.value < 1), f"a rate literal ships at line {node.lineno}"


def test_48_the_csv_has_no_comment_line_and_the_notice_carries_the_disclaimer() -> None:
    """A leading '#' would become the header row and break `dbt seed` (agate has no
    comment syntax) -- hence the sibling NOTICE. Guard both halves."""
    lines = _SEED_CSV.read_text(encoding="utf-8").splitlines()
    assert lines[0] == _SEED_HEADER
    assert not lines[0].startswith("#")
    assert len([line for line in lines[1:] if line.strip()]) == _SEED_ROW_COUNT

    notice = _SEED_NOTICE.read_text(encoding="utf-8")
    assert "NOT TAX ADVICE" in notice.upper()
    assert "operator confirmation" in notice.lower()
    # And the same statement is in the loader docstring.
    assert "NOT TAX ADVICE" in (defaults.load_country_tax_defaults.__doc__ or "").upper()


# ===========================================================================
# dedup_hash (AC10, D4)
# ===========================================================================


def _key(**over: Any) -> str:
    payload: dict[str, Any] = {
        "scope_kind": "project",
        "category": "REGULATORY_TAX",
        "form": "PERCENTAGE",
        "effective_from": date(2019, 1, 1),
        "countries": ["FR"],
    }
    payload.update(over)
    return defaults.build_auto_rule_dedup_key(**payload)


def test_49_the_key_is_order_insensitive_and_normalising() -> None:
    assert _key(countries=["FR", "GB"]) == _key(countries=["GB", "FR"])
    assert _key(countries=["fr"]) == _key(countries=[" FR "])


def test_50_the_key_is_stable_and_pins_the_canonical_payload_verbatim() -> None:
    key = _key()
    assert key == _key()  # same inputs -> same key, no clock and no ULID in the recipe
    assert key.startswith("ftk_")
    assert len(key) == len("ftk_") + 32

    # The CANONICAL PAYLOAD is pinned verbatim, not just the digest: an accidental recipe
    # change must fail LOUDLY here rather than silently re-populate every project with
    # duplicate proposals on the next run.
    payload = "v1|auto_country|project|REGULATORY_TAX|PERCENTAGE|2019-01-01|country=FR"
    expected = "ftk_" + hashlib.sha256(payload.encode("ascii")).hexdigest()[:32]
    assert key == expected


def test_51_the_key_structurally_cannot_depend_on_the_rate_or_the_label() -> None:
    """An operator editing the rate on a proposed auto rule must not mint a duplicate."""
    params = set(inspect.signature(defaults.build_auto_rule_dedup_key).parameters)
    assert params == {"scope_kind", "category", "form", "effective_from", "countries"}
    assert "rate" not in params
    assert "label" not in params


def test_52_every_identity_component_changes_the_key() -> None:
    base = _key()
    assert _key(category="SALES_TAX") != base
    assert _key(form="FLAT") != base
    assert _key(effective_from=date(2020, 1, 1)) != base
    assert _key(scope_kind="datastream") != base
    assert _key(countries=["GB"]) != base


# ===========================================================================
# Auto-population (AC9, AC10, AC11)
# ===========================================================================


def test_53_module_off_is_a_no_op_and_the_store_is_never_touched(monkeypatch) -> None:
    store = _FakeStore(active=False)
    _install(monkeypatch, store, _posture(("france", ("FR",))))
    conn = _FakeConn()

    result = defaults.auto_populate_tax_rules("prj_1", conn)

    assert result.skipped_reason == defaults.SKIPPED_MODULE_OFF
    assert result.created == 0
    assert store.list_calls == 0
    assert store.created == []
    assert conn.statements == []


def test_54_a_global_posture_is_a_no_op(monkeypatch) -> None:
    """Also covers a brand-new project: an absent preferences row reads as Global."""
    store = _FakeStore()
    _install(monkeypatch, store, GeographicPosture())

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert result.skipped_reason == defaults.SKIPPED_POSTURE_GLOBAL
    assert result.created == 0
    assert store.created == []


def test_55_two_tracked_countries_yield_their_four_proposed_rules(monkeypatch) -> None:
    """Epic AC executed: DST GB 2%, DST FR 3%, plus each country's standard VAT."""
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("uk", ("GB",)), ("france", ("FR",))))

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn(), identity="tester")

    assert result.created == 4
    assert result.unchanged == 0
    assert result.no_default == 0
    assert result.countries == ("FR", "GB")
    assert len(store.created) == 4

    by_identity = {
        (rule["category"], rule["conditions"]["country"][0]): rule for rule in store.created
    }
    assert set(by_identity) == {
        ("REGULATORY_TAX", "FR"),
        ("REGULATORY_TAX", "GB"),
        ("SALES_TAX", "FR"),
        ("SALES_TAX", "GB"),
    }
    assert by_identity[("REGULATORY_TAX", "GB")]["rate"] == Decimal("0.020000")
    assert by_identity[("REGULATORY_TAX", "FR")]["rate"] == Decimal("0.030000")
    assert by_identity[("SALES_TAX", "GB")]["rate"] == Decimal("0.200000")
    assert by_identity[("SALES_TAX", "FR")]["rate"] == Decimal("0.200000")

    for (category, code), rule in by_identity.items():
        assert rule["origin"] == "auto_country"
        assert rule["status"] == "proposed"  # INERT until a human confirms it
        assert rule["scope_kind"] == "project"
        assert rule["scope_ref"] is None
        assert rule["conditions"] == {"country": [code]}
        assert rule["form"] == "PERCENTAGE"
        assert rule["sequence_order"] == 900
        assert isinstance(rule["rate"], Decimal)
        assert "currency" not in rule  # 41.1 refuses a currency on a PERCENTAGE rule
        assert rule["dedup_hash"]
        if category == "REGULATORY_TAX":
            assert rule["cascade_phase"] == 3
            assert rule["base_target"] == "NET_MEDIA"
        else:
            assert rule["cascade_phase"] == 6
            assert rule["base_target"] == "RUNNING_SUBTOTAL"
        # And every payload survives Story 41.1's real validator untouched.
        ftr.validate_rule(dict(rule))


def test_56_an_auto_rule_carries_no_source_type_scope(monkeypatch) -> None:
    """REGRESSION, and the most expensive bug this story shipped before review.

    A source_type_scope is flattened by migration 119 into a
    condition_key='source_type' row, the cascade correctly ranks an UNRESOLVABLE attribute
    above a known-false one, and NOTHING writes app.datastream_source_types -- so
    attr_source_type is NULL on every row in production. A scope therefore attached an
    UNSATISFIABLE PRECONDITION to every auto rule: confirming DST FR 3 % returned a NULL
    total with gap SOURCE_TYPE_UNRESOLVED, E41-FR02 could never compose a total, and the
    C5 posture rung that exists to avoid blank day-one totals was neutralised.

    A country tax rule's applicability is already fully expressed by its country
    condition. Do NOT restore the scope: when a declaration surface exists (41.6 / 41.8)
    it comes back as an explicit operator choice, never as a silent default.
    """
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("france", ("FR",))))
    defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert store.created
    for rule in store.created:
        assert "source_type_scope" not in rule
        # And 41.1 normalises the absent field to [], which the flattening view turns into
        # NO condition row at all -- i.e. "applies to everything", never "no match".
        assert ftr.validate_rule(dict(rule))["source_type_scope"] == []


def test_56b_no_auto_rule_declares_a_precondition_the_warehouse_cannot_resolve() -> None:
    """THE structural guard the missing fixture should have been.

    Every stored precondition becomes a ``fee_tax_rule_conditions`` row that the cascade
    must satisfy, and an attribute it cannot resolve makes the rule UNRESOLVED rather than
    matched. So the set of condition keys auto-population emits must never widen past what
    the bridge actually resolves. This test reads the REAL payloads -- the same
    ``build_auto_rule_payloads`` output any fixture must be built from -- so a future
    divergence between what this story emits and what the ladder can consume fails HERE,
    with no dbt run required.
    """
    payloads = defaults.build_auto_rule_payloads(["FR", "GB", "DE"])
    assert payloads, "the guard would be vacuous with no payloads"

    # Replays migration 119's app.fee_tax_rule_conditions_v: the conditions JSONB flattened
    # to (key, value) rows, UNION ALL source_type_scope unnested as condition_key rows.
    condition_keys: set[str] = set()
    for payload in payloads:
        condition_keys.update(payload.get("conditions") or {})
        if payload.get("source_type_scope"):
            condition_keys.add("source_type")

    assert condition_keys == set(defaults.AUTO_CONDITION_KEYS) == {"country"}
    # The bridge resolves `country` (four rungs) and `market`. It resolves NOTHING else
    # today, so nothing else may be declared.
    assert "source_type" not in condition_keys
    assert condition_keys <= {"country", "market"}


def test_56c_the_payload_generator_is_the_single_source_of_truth() -> None:
    """A fixture must be seeded from this output, never from a hand-written copy.

    The hand-written fixture rules in 41.3's seeder are what let the source_type_scope bug
    reach a green build: no test ever exercised the payload auto-population really emits.
    """
    single = defaults.build_auto_rule_payloads(["FR"])
    assert [rule["category"] for rule in single] == ["REGULATORY_TAX", "SALES_TAX"]
    assert all(rule["conditions"] == {"country": ["FR"]} for rule in single)

    # Deterministic and order-stable, so a fixture built from it is byte-stable.
    assert defaults.build_auto_rule_payloads(["FR"]) == single
    assert defaults.build_auto_rule_payloads(["FR", "US"]) == single  # US seeds nothing
    assert defaults.build_auto_rule_payloads([]) == ()

    # Identical to what auto_populate_tax_rules writes, field for field.
    assert single == tuple(
        defaults.build_auto_rule_payload(item) for item in defaults.defaults_for_country("FR")
    )


def test_57_re_running_creates_nothing_and_keys_identically(monkeypatch) -> None:
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("uk", ("GB",)), ("france", ("FR",))))

    first = defaults.auto_populate_tax_rules("prj_1", _FakeConn())
    assert first.created == 4
    first_hashes = [rule["dedup_hash"] for rule in store.created]

    # The store now holds them, all still `proposed`.
    store.existing = [
        {"dedup_hash": rule["dedup_hash"], "status": "proposed"} for rule in store.created
    ]
    store.created = []

    second = defaults.auto_populate_tax_rules("prj_1", _FakeConn())
    assert second.created == 0
    assert second.unchanged == 4
    assert store.created == []  # nothing is even attempted
    # And the keys the second run computed are byte-identical to the first run's.
    assert sorted(rule["dedup_hash"] for rule in store.existing) == sorted(first_hashes)


def test_58_a_concurrent_insert_is_reported_unchanged_not_duplicated(monkeypatch) -> None:
    """ON CONFLICT is the guarantee; the pre-read is only the report."""
    store = _FakeStore(inserted=False)  # DO NOTHING fired: another run got there first
    _install(monkeypatch, store, _posture(("france", ("FR",))))

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert result.created == 0
    assert result.unchanged == 2
    assert len(store.created) == 2  # attempted, refused by the constraint, counted honestly


def test_59_a_confirmed_rule_is_never_overwritten(monkeypatch) -> None:
    store = _FakeStore()
    posture = _posture(("france", ("FR",)))
    _install(monkeypatch, store, posture)

    confirmed_hash = defaults.build_auto_rule_dedup_key(
        scope_kind="project",
        category="REGULATORY_TAX",
        form="PERCENTAGE",
        effective_from=date(2019, 1, 1),
        countries=("FR",),
    )
    store.existing = [
        {"dedup_hash": confirmed_hash, "status": "confirmed", "rate": Decimal("0.015000")}
    ]

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert result.skipped_confirmed == 1
    assert result.created == 1  # only the VAT row
    assert all(rule["dedup_hash"] != confirmed_hash for rule in store.created)
    # The human's edited rate survives untouched.
    assert store.existing[0]["rate"] == Decimal("0.015000")
    # ...and the difference is REPORTED, not silently swallowed. rate_drift says the
    # stored rate and the seed rate differ; it deliberately does NOT guess why (an
    # operator override and a seed correction look identical from here -- `status` is
    # what tells them apart).
    assert result.rate_drift == 1
    assert result.drifted[0]["stored_rate"] == "0.015000"
    assert result.drifted[0]["status"] == "confirmed"


def test_60_a_disabled_rule_is_never_resurrected(monkeypatch) -> None:
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("france", ("FR",))))

    disabled_hash = defaults.build_auto_rule_dedup_key(
        scope_kind="project",
        category="SALES_TAX",
        form="PERCENTAGE",
        effective_from=date(2014, 1, 1),
        countries=("FR",),
    )
    store.existing = [{"dedup_hash": disabled_hash, "status": "disabled"}]

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert result.skipped_disabled == 1
    assert all(rule["dedup_hash"] != disabled_hash for rule in store.created)


def test_60b_a_seed_correction_is_reported_not_silently_ignored(monkeypatch) -> None:
    """The hole `rate` being outside dedup_hash would otherwise leave.

    Excluding `rate` is right -- an operator's edit must not mint a duplicate -- but it
    means a SEED CORRECTION is invisible: ship a rate, learn it was wrong, fix the seed in
    place, re-run, and the hash still matches, so the row is `unchanged` and the run
    reports success while the stale rate keeps invoicing. Nothing compared the two. Now
    something does.
    """
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("uk", ("GB",))))

    seeded = defaults.defaults_for_country("GB")
    stale = defaults.build_auto_rule_payload(seeded[0])
    store.existing = [
        {
            "dedup_hash": stale["dedup_hash"],
            "status": "proposed",
            # the rate we shipped before the seed was corrected
            "rate": stale["rate"] + Decimal("0.005000"),
        }
    ]

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert result.rate_drift == 1
    assert result.unchanged == 1  # still counted as present -- nothing is rewritten
    # Nothing is written FOR THE DRIFTED RULE. Not `store.created == []`: GB seeds TWO
    # defaults (DST REGULATORY_TAX + VAT SALES_TAX) and only the DST one is pre-existing
    # here, so the VAT rule is legitimately created. Asserting an empty list would pin an
    # unrelated seed fact and fail the day a country gains a third default.
    assert stale["dedup_hash"] not in {row["dedup_hash"] for row in store.created}
    drift = result.drifted[0]
    assert drift["iso_code"] == "GB"
    assert drift["seed_rate"] == str(stale["rate"])
    assert drift["stored_rate"] != drift["seed_rate"]
    assert "rate_drift" in result.as_dict()


def test_60c_an_unchanged_rate_raises_no_false_drift(monkeypatch) -> None:
    """Compared as exact Decimals, so 0.02 and 0.020000 are the SAME rate.

    A false drift would train an operator to ignore the signal, which is worse than not
    having one. An absent or unreadable stored rate reports no drift either: inventing one
    from a value we could not read would be its own fabrication.
    """
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("uk", ("GB",))))

    payloads = defaults.build_auto_rule_payloads(["GB"])
    store.existing = [
        {"dedup_hash": payloads[0]["dedup_hash"], "status": "proposed", "rate": Decimal("0.02")},
        {"dedup_hash": payloads[1]["dedup_hash"], "status": "proposed", "rate": None},
    ]

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert result.rate_drift == 0
    assert result.drifted == ()
    assert result.unchanged == 2


def test_61_a_tracked_country_with_no_seeded_default_is_counted_not_raised(
    monkeypatch,
) -> None:
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("france", ("FR", "MC"))))

    result = defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    assert result.no_default == 1
    assert result.created == 2
    assert result.countries == ("FR", "MC")


def test_62_rules_are_emitted_per_country_never_per_market(monkeypatch) -> None:
    """A market is a NAMED GROUP of countries; a tax is levied per country."""
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("france", ("FR", "MC"))))
    defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    conditions = [rule["conditions"] for rule in store.created]
    assert all(set(item) == {"country"} for item in conditions)
    assert all(len(item["country"]) == 1 for item in conditions)
    assert {item["country"][0] for item in conditions} == {"FR"}
    assert all("market" not in item for item in conditions)


def test_63_an_auto_rule_never_keys_on_a_synthetic_grouping(monkeypatch) -> None:
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("france", ("FR",)), ("uk", ("GB",))))
    defaults.auto_populate_tax_rules("prj_1", _FakeConn())

    values = {
        value for rule in store.created for value in rule["conditions"].get("country", [])
    }
    # A grouping is never emitted as a country condition: Unknown by its
    # reserved id, Rest of World because it is a node id and the assertion
    # below restricts every value to the canonical vocabulary.
    assert UNKNOWN_BUCKET_ID not in values
    assert values <= get_supported_country_codes()


def test_64_every_write_goes_through_the_audited_store(monkeypatch) -> None:
    """AD-27: no raw SQL against app.fee_tax_rules anywhere in this story."""
    store = _FakeStore()
    _install(monkeypatch, store, _posture(("france", ("FR",))))
    conn = _FakeConn()

    defaults.auto_populate_tax_rules("prj_1", conn)

    assert not [sql for sql, _ in conn.statements if "fee_tax_rules" in sql.lower()]
    source = _MODULE.read_text(encoding="utf-8")
    assert not re.search(r"INSERT\s+INTO\s+app\.", source, re.IGNORECASE)
    assert not re.search(r"UPDATE\s+app\.", source, re.IGNORECASE)


# ===========================================================================
# Pg-gated
# ===========================================================================


@dedup_schema
def test_65_the_partial_unique_makes_idempotency_a_database_guarantee() -> None:
    """A second row with the same (project_id, dedup_hash) is refused BY POSTGRES.

    Rolled back at the end: this test seeds nothing durable.
    """
    import psycopg
    from ulid import ULID

    key = "ftk_" + str(ULID())
    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        try:
            project_id = f"proj_{ULID()}"
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                    "VALUES (%s, %s, %s, %s, 'test-41-2')",
                    (project_id, TEST_ORG_ID, project_id, project_id.replace("_", "-").lower()),
                )
            insert = (
                "INSERT INTO app.fee_tax_rules "
                "(id, project_id, scope_kind, scope_ref, category, form, rate, base_target, "
                " cascade_phase, sequence_order, conditions, source_type_scope, "
                " effective_from, status, origin, dedup_hash, label, created_by) "
                "VALUES (%s, %s, 'project', NULL, 'REGULATORY_TAX', 'PERCENTAGE', %s, "
                "        'NET_MEDIA', 3, 900, '{}'::jsonb, ARRAY[]::TEXT[], "
                "        DATE '2019-01-01', 'proposed', 'auto_country', %s, '', 'test-41-2')"
            )
            with conn.cursor() as cur:
                cur.execute(insert, (f"ftr_{ULID()}", project_id, Decimal("0.030000"), key))
            with conn.cursor() as cur, pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(insert, (f"ftr_{ULID()}", project_id, Decimal("0.030000"), key))
        finally:
            conn.rollback()
