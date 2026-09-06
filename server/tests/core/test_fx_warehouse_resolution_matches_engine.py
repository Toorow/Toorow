"""The DIFFERENTIAL ORACLE of the FX cutover -- Story 67.13.

`docs/product-architecture/capabilities/currency-fx.md` ratifies, in step 3 of the
cutover it decides, that the posed-rate resolution now exists TWICE:

    "This is a second implementation of one rule and must be admitted as one: the
     precedent is dbt/macros/fee_tax_condition_matcher.sql standing beside
     core.tax_fee_rule_set, and what that precedent carries is a DIFFERENTIAL
     ORACLE, not two half-tested engines."

Two engines, one rule, and a single artefact both are judged against. That
artefact is:

  * `dbt/seeds/fx_posed_rates_fixture.csv` + `..._conditions_fixture.csv` -- the
    five cases;
  * the `probe` table inside `dbt/tests/test_fx_posed_rate_governs.sql` -- what
    each case must produce.

`dbt build` runs the SQL engine against that table. THIS FILE runs the PYTHON
engine -- `core.fx_rate_sets._resolve_posed`, unmodified -- against the very same
table, PARSED OUT OF THE SAME FILE so the two can never be edited apart. If one
engine's behaviour moves, exactly one of the two tests reddens, and the
disagreement is named rather than discovered six months later inside a total.

WHY IT IS NOT PG-GATED. `_resolve_posed` touches its connection only through
`conn.cursor()` / `execute` / `fetchall`, and the SQL it sends selects the same
rows `app.fx_posed_rates_v` publishes. Standing those rows up directly exercises
every line that decides anything -- the three-valued verdict, the specificity
ranking, the tie refusal -- with no Postgres and no fixture drift.
`server/tests/integration/test_fx_conditional_rate_postgres.py` is what proves
the SQL half of `_resolve_posed` against a real base; this file proves the RULE.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from core.fx_rate_sets import FxGap, _resolve_posed

REPO = Path(__file__).resolve().parents[3]
RATES_CSV = REPO / "dbt" / "seeds" / "fx_posed_rates_fixture.csv"
CONDS_CSV = REPO / "dbt" / "seeds" / "fx_posed_rate_conditions_fixture.csv"
DBT_TEST = REPO / "dbt" / "tests" / "test_fx_posed_rate_governs.sql"

#: One probe row of the dbt singular test, after the normalisation below has
#: removed its SQL comments and its column aliases -- so the first row (which
#: must carry `AS case_name` ... for the UNION to have names) and the ten that
#: follow are read by ONE pattern instead of two that could drift apart.
_PROBE = re.compile(
    r"SELECT\s+'(?P<case>\w+)',\s*'(?P<project>fxp_\w+)',\s*"
    r"DATE '(?P<on_date>[\d-]+)',\s*'(?P<connector>[\w-]+)',\s*"
    r"CAST\((?P<rate>NULL|[\d.]+) AS .*?\),\s*"
    r"(?P<gap>'[\w_]+'|CAST\(NULL AS .*?\)),\s*"
    r"(?P<rows>\d+),"
)

#: The eight aliases the probe CTE declares. Stripped BY NAME rather than by
#: `\s+AS \w+`, which would also eat the ` AS DECIMAL` of a CAST and leave an
#: expression this file could no longer read.
_ALIASES = re.compile(
    r"\s+AS (?:case_name|project_id|on_date|connector|expect_rate|expect_gap"
    r"|expect_rows|meaning)\b"
)


def _cases() -> list[dict]:
    text = DBT_TEST.read_text(encoding="utf-8")
    block = re.search(r"^probe AS \(\n(.*?)\n\),\n", text, re.S | re.M)
    assert block is not None, (
        "the `probe` CTE of test_fx_posed_rate_governs.sql no longer parses -- "
        "the two engines are judged against that table and this file cannot read it"
    )
    body = re.sub(r"--[^\n]*", "", block.group(1))       # SQL comments
    body = _ALIASES.sub("", body)                        # column aliases
    body = re.sub(r"\s+", " ", body)                     # line breaks
    out = []
    for m in _PROBE.finditer(body):
        row = m.groupdict()
        gap = row["gap"]
        out.append(
            {
                "case": row["case"],
                "project": row["project"],
                "on_date": date.fromisoformat(row["on_date"]),
                "connector": row["connector"],
                "rate": None if row["rate"] == "NULL" else Decimal(row["rate"]),
                "gap": gap.strip("'") if gap.startswith("'") else None,
                "rows": int(row["rows"]),
            }
        )
    return out


class _Cursor:
    """The two methods `_resolve_posed` uses, over the fixture instead of a base.

    The WHERE clause it sends is applied here in Python, verbatim in meaning:
    the view already carries `method = 'fixed'`, `validation_status =
    'validated'` and both window ends, so only the window test remains.
    """

    def __init__(self, rates, conditions):
        self._rates = rates
        self._conditions = conditions
        self._result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql, params):
        version_id, base, quote, on_date = params
        self._result = [
            (
                r["observation_id"],
                Decimal(r["rate"]),
                date.fromisoformat(r["effective_date"]),
                date.fromisoformat(r["valid_from"]),
                date.fromisoformat(r["valid_to"]),
                self._conditions.get(r["observation_id"], {}),
            )
            for r in self._rates
            if r["rate_set_version_id"] == version_id
            and r["base_currency"] == base
            and r["quote_currency"] == quote
            and date.fromisoformat(r["valid_from"])
            <= on_date
            <= date.fromisoformat(r["valid_to"])
        ]

    def fetchall(self):
        return self._result


class _Conn:
    def __init__(self, rates, conditions):
        self._rates = rates
        self._conditions = conditions

    def cursor(self):
        return _Cursor(self._rates, self._conditions)


@pytest.fixture(scope="module")
def fixture_rows():
    rates = list(csv.DictReader(RATES_CSV.read_text(encoding="utf-8").splitlines()))
    conditions: dict[str, dict[str, list[str]]] = {}
    for row in csv.DictReader(CONDS_CSV.read_text(encoding="utf-8").splitlines()):
        conditions.setdefault(row["observation_id"], {}).setdefault(
            row["condition_key"], []
        ).append(row["condition_value"])
    # The count the view computes in Postgres, recomputed from the same two
    # relations -- so a fixture whose `condition_key_count` disagrees with its own
    # conditions is caught here rather than producing a specificity nobody meant.
    for row in rates:
        declared = int(row["condition_key_count"])
        actual = len(conditions.get(row["observation_id"], {}))
        assert declared == actual, (
            f"{row['observation_id']} declares condition_key_count={declared} but "
            f"carries {actual} condition keys -- the specificity the warehouse "
            f"ranks by would not be the specificity the engine ranks by"
        )
    return rates, conditions


def _version_of(rates, project: str) -> str:
    versions = {r["rate_set_version_id"] for r in rates if r["project_id"] == project}
    assert len(versions) == 1, (project, versions)
    return versions.pop()


def test_the_case_table_is_read_and_is_not_empty():
    """Anti-vacuity: without it every assertion below passes over nothing."""
    cases = _cases()
    assert len(cases) == 11, [c["case"] for c in cases]
    assert {c["gap"] for c in cases} == {
        None,
        "fx_condition_conflict",
        "fx_condition_unresolved",
    }


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["case"])
def test_python_engine_agrees_with_the_warehouse_expectation(case, fixture_rows):
    """`_resolve_posed` produces exactly what the dbt test requires of the SQL.

    `expect_rows = 0` in the warehouse is `None` here: no posed rate covers the
    figure, so the caller falls back -- to the seed in the warehouse, to the
    ingested steps in the application. It is a different outcome from a REFUSAL
    (a gap), and conflating the two is the whole family of defect the capability's
    bullets 10 and 11 refuse.
    """
    rates, conditions = fixture_rows
    conn = _Conn(rates, conditions)
    version_id = _version_of(rates, case["project"])

    def resolve():
        return _resolve_posed(
            conn,
            version_id=version_id,
            base="USD",
            quote="EUR",
            on_date=case["on_date"],
            context={"connector": case["connector"]},
        )

    if case["gap"] is not None:
        with pytest.raises(FxGap) as excinfo:
            resolve()
        assert excinfo.value.code == case["gap"]
        return

    result = resolve()
    if case["rows"] == 0:
        assert result is None, (
            f"{case['case']}: the warehouse serves no segment here, so the engine "
            f"must serve no posed rate either -- it returned {result}"
        )
        return

    assert result is not None, f"{case['case']}: the warehouse serves a rate, the engine does not"
    rate, effective, derivation = result
    assert rate == case["rate"]
    assert isinstance(effective, date)
    # The winner is named on both sides: the warehouse carries `observation_id`,
    # the engine carries it in `derivation`. A rate a reader cannot trace back to
    # the rule that served it is the `fx_source='seed'` defect in another costume.
    assert derivation["posed"] is True
    assert derivation["observation_id"].startswith("fxo_")


def test_a_posed_rate_never_resolves_outside_its_declared_window(fixture_rows):
    """Bullet 7, and WORDING A -- the window is the only temporal authority.

    Decided 2026-08-22: the posting date is not a boundary anywhere in this
    capability. Nothing in either engine reads it, so this holds in both
    directions -- a day before the window is refused exactly like a day after,
    which a "forward only" rule (Wording B) would not give.
    """
    rates, conditions = fixture_rows
    conn = _Conn(rates, conditions)
    version_id = _version_of(rates, "fxp_window")

    inside = _resolve_posed(
        conn, version_id=version_id, base="USD", quote="EUR",
        on_date=date(2026, 7, 5), context={"connector": "meta-ads"},
    )
    assert inside is not None and inside[0] == Decimal("0.500000")

    for outside in (date(2026, 6, 30), date(2026, 7, 11), datetime(2027, 1, 1).date()):
        assert (
            _resolve_posed(
                conn, version_id=version_id, base="USD", quote="EUR",
                on_date=outside, context={"connector": "meta-ads"},
            )
            is None
        ), f"a posed rate resolved on {outside}, outside the window it was declared for"
