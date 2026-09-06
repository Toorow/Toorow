"""Planned versus observed, and the currency each side is actually in — story 61.4.

WHAT WAS MEASURED, AND WHAT IT COST. On 2026-08-09 the pacing marts labelled the
observed spend with the PLAN's currency (`media_plans.currency`, carried up as
`currency`), while that spend was `fx_convert_at_read('cost')` — an amount already
converted into the currency the warehouse FX join targeted. A plan in USD on a
Project converting into EUR was therefore served as a USD pacing composed of a USD
budget and a EUR spend. It reached four production callers: the pacing route
(`GET /api/mediaplans/{plan_id}/pacing`), the MCP card `mediaplan_pacing`,
`mediaplan_alerts` and the scheduler. Nothing anywhere compared the two currencies
— `grep` for a comparison returned zero — so nothing could go red.

The second half is worse because it accuses somebody. `fx_convert_at_read` yields
NULL when no rate resolves, `SUM()` skips NULLs, so a day with no exchange rate
contributed **0** to the to-date spend, `pace` went negative, and
`mediaplan_alerts` fired a `mediaplan_pace_underdelivery` naming a campaign that
had spent normally. A false alert is worse than an absent one: it spends the
credit the next alert will need.

THIS FILE HOLDS THE RULE, IN ONE SENTENCE: an amount is shown only under a
currency that produced it, and nothing composed of two currencies is shown at all.

The warehouse half of the same rule is `dbt/tests/test_plan_pacing_currency_is_declared.sql`,
proven on a real build; this is the application half. Fixtures are `proj_EXAMPLE`,
`owner@example.com`, `camp_EXAMPLE_*`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.datastream_workbench_placements import (
    ANALYZE_PACING_REASON,
    KPI_VARIANCE_UNAVAILABLE_REASON,
    MONEY_STATE_ALIGNED,
    MONEY_STATE_PLAN_MISMATCH,
    MONEY_STATE_POLICY_UNCONFIRMED,
    MONEY_STATE_REPORTING_MISMATCH,
    MONEY_STATE_REPORTING_UNRESOLVED,
    _read_money_frame,
)
from core.mediaplan_alerts import (
    DEFAULT_OVERRUN_THRESHOLD,
    DEFAULT_UNDERDELIVERY_THRESHOLD,
    KIND_UNDERDELIVERY,
    _evaluate_rows,
)
from core.money_policy import PolicyGap

_PROJECT = "proj_EXAMPLE"
_PLAN = "1b6bd5c0-0000-4000-8000-000000000001"
_VERSION = "1b6bd5c0-0000-4000-8000-0000000000f1"


# ---------------------------------------------------------------------------
# Doubles. The Project's two currencies are READ, never assumed.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, owner: "_Connection") -> None:
        self._owner = owner
        self._rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, _params=None) -> None:
        self._owner.statements.append(sql)
        if "project_preferences" in sql:
            self._rows = [] if self._owner.canonical is _ABSENT else [(self._owner.canonical,)]
        else:  # pragma: no cover -- an unexpected read must not answer silently
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


_ABSENT = object()


class _Connection:
    def __init__(self, *, canonical: object = "EUR") -> None:
        self.canonical = canonical
        self.statements: list[str] = []

    def cursor(self):
        return _Cursor(self)


class _Policy:
    """The shape `resolve_money_policy` returns, reduced to what is read here."""

    def __init__(self, currency: str) -> None:
        self.reporting_currency = currency
        self.version_id = "rsv_EXAMPLE_money"


def _frame(*, plan_currency: str | None, canonical: object = "EUR", policy: str | None = "EUR"):
    def _resolve(_conn, *, project_id):  # noqa: ARG001 -- signature fidelity
        if policy is None:
            raise PolicyGap(
                "money_policy_unconfirmed",
                "This Project has no confirmed Money Policy.",
                capability="currency_fx",
            )
        return _Policy(policy)

    with patch("core.money_policy.resolve_money_policy", _resolve):
        return _read_money_frame(
            _Connection(canonical=canonical),
            project_id=_PROJECT,
            plan={"id": _PLAN, "currency": plan_currency},
        )


# ---------------------------------------------------------------------------
# 1. Two currencies: the refusal names BOTH, and carries no number.
# ---------------------------------------------------------------------------


def test_plan_currency_differs_from_the_conversion_currency_and_the_refusal_names_both():
    """`epic-61:119-121`, proven rather than asserted.

    The plan is in USD; the spend was converted into EUR. The frame refuses to
    call the two comparable, and its sentence names BOTH codes — a refusal that
    does not say which two things disagree is a refusal nobody can act on.
    """
    frame = _frame(plan_currency="USD", canonical="EUR", policy="EUR")

    assert frame["state"] == MONEY_STATE_PLAN_MISMATCH
    assert frame["comparable"] is False
    assert "USD" in frame["message"] and "EUR" in frame["message"]
    # Each amount keeps its own currency: the budget is USD, the spend EUR. The
    # tab may print both, and nothing composed of them.
    assert frame["plan_currency"] == "USD"
    assert frame["spend_currency"] == "EUR"


def test_the_refusal_carries_no_amount_of_any_kind():
    """A refusal that slips a number in is not a refusal.

    Not a style rule: `0` and a bare figure are exactly what a person reads as a
    measurement. The sentence must be words and currency codes only.
    """
    frame = _frame(plan_currency="USD", canonical="EUR", policy="EUR")

    assert not any(character.isdigit() for character in frame["message"]), frame["message"]


def test_equal_currencies_read_against_each_other():
    """The converse, and the half a "no false label" rule usually forgets.

    A frame that answered every currency question with a refusal would satisfy
    the test above and be useless.
    """
    frame = _frame(plan_currency="EUR", canonical="EUR", policy="EUR")

    assert frame["state"] == MONEY_STATE_ALIGNED
    assert frame["comparable"] is True
    assert frame["spend_currency"] == "EUR"


# ---------------------------------------------------------------------------
# 2. A currency nobody can name produces no amount at all.
# ---------------------------------------------------------------------------


def test_governed_currency_disagreeing_with_the_conversion_shows_no_observed_amount():
    """The confirmed Money Policy says GBP; the conversion targeted EUR.

    Either label would be a false statement about the number, so no observed
    amount may be shown. This is the case the mart calls
    `reporting_currency_mismatch`, and it is the one a `COALESCE` between the two
    authorities would have hidden.
    """
    frame = _frame(plan_currency="EUR", canonical="EUR", policy="GBP")

    assert frame["state"] == MONEY_STATE_REPORTING_MISMATCH
    assert frame["spend_currency"] is None
    assert frame["comparable"] is False
    assert "GBP" in frame["message"] and "EUR" in frame["message"]


def test_no_conversion_currency_at_all_shows_no_observed_amount():
    """Story 48.3 removed the `'EUR'` DEFAULT from `canonical_currency`.

    A NULL there means nobody chose, not "euros by omission", and an unnamed
    currency cannot label an amount.
    """
    frame = _frame(plan_currency="EUR", canonical=None, policy=None)

    assert frame["state"] == MONEY_STATE_REPORTING_UNRESOLVED
    assert frame["spend_currency"] is None
    assert frame["comparable"] is False


def test_an_unconfirmed_money_policy_does_not_suppress_a_correctly_labelled_amount():
    """It is a provenance statement, not a gap — and the distinction is deliberate.

    `canonical_currency` is operator-confirmed (its `_origin` /
    `_confirmation_status` pair), so the amount is real and correctly labelled;
    what is missing is the GOVERNED authority. Suppressing here would delete
    served numbers to punish a Project for a Change Set it has not published, and
    the honest report is to show the amount and say the currency is not governed.
    """
    frame = _frame(plan_currency="EUR", canonical="EUR", policy=None)

    assert frame["state"] == MONEY_STATE_POLICY_UNCONFIRMED
    assert frame["spend_currency"] == "EUR"
    assert frame["reporting_currency"] is None
    assert frame["comparable"] is False


def test_the_project_currency_is_read_from_the_database_and_not_assumed():
    """The one statement this frame runs against the Project, and it is a READ."""
    conn = _Connection(canonical="EUR")
    with patch("core.money_policy.resolve_money_policy", lambda *_a, **_k: _Policy("EUR")):
        _read_money_frame(conn, project_id=_PROJECT, plan={"currency": "EUR"})

    assert len(conn.statements) == 1
    assert "canonical_currency" in conn.statements[0]
    assert "project_preferences" in conn.statements[0]


# ---------------------------------------------------------------------------
# 3. What the tab says INSTEAD of a pacing panel (arbitrage A1), and what it
#    names as non-existent (arbitrage A5 / acceptance 6).
# ---------------------------------------------------------------------------


def test_the_reading_is_named_as_living_in_analyze_and_the_door_is_a_real_address():
    """61.4 adds no budget-versus-actual panel here, and says where it lives.

    Two ratified documents place pacing in Analyze. The reference is semantic —
    the console builds the address from `navigation.ts` — and now names the
    shipped Reports > Pacing lens rather than the former workspace-only door.
    """
    frame = _frame(plan_currency="EUR")
    reference = frame["analyze_reference"]

    assert reference["workspace"] == "analyze"
    assert reference["section"] == "reports"
    assert reference["object_type"] is None and reference["object_id"] is None
    assert reference["tab"] is None and reference["version_id"] is None
    assert reference["lens"] == "pacing"


def test_the_door_admits_what_the_console_does_not_carry_yet():
    """AI-190, in words, on the surface that would otherwise imply a promise.

    The console and MCP App now read the same pacing Result. The handoff must say
    so explicitly rather than preserving the retired console-parity debt.
    """
    assert "MCP App" in ANALYZE_PACING_REASON
    assert "console now carry the same Result" in ANALYZE_PACING_REASON
    assert "Analyze > Reports > Pacing" in ANALYZE_PACING_REASON
    # And it names what the reading is ABOUT to carry, which is the whole point of
    # 61.4: the reporting currency and where its rate came from.
    assert "reporting currency" in ANALYZE_PACING_REASON
    assert "FX provenance" in ANALYZE_PACING_REASON


def test_the_kpi_variance_is_named_as_non_existent_with_the_count_that_proves_it():
    """Acceptance 6: written, not silently omitted.

    `epic-61:48` asks for a KPI variance. `app.media_plan_lines` carries eleven
    columns and none of them is a KPI target, so there is nothing to vary against
    — and the sentence says the number rather than hand-waving.
    """
    assert "eleven columns" in KPI_VARIANCE_UNAVAILABLE_REASON
    assert "no KPI variance can be computed" in KPI_VARIANCE_UNAVAILABLE_REASON
    # It names what CAN be compared, so the sentence is a measurement and not a
    # complaint.
    assert "Only the budget can be compared" in KPI_VARIANCE_UNAVAILABLE_REASON


# ---------------------------------------------------------------------------
# 4. THE FALSE UNDER-DELIVERY. A missing exchange rate accuses nobody.
# ---------------------------------------------------------------------------


class _AlertConnection:
    """A connection that would happily write a firing — so the refusal is visible.

    `_write_firing` is doubled at the module boundary below; this object exists so
    a guard that FAILS to stop the row reaches an observable write instead of an
    exception that could be mistaken for the guard working.
    """

    def cursor(self):  # pragma: no cover -- reached only when a guard fails
        raise AssertionError("no statement should be run for a skipped row")


def _evaluate(rows: list[dict]) -> list[dict]:
    written: list[dict] = []

    def _write_firing(**kwargs):
        written.append(kwargs)
        return "firing_EXAMPLE"

    with patch("core.mediaplan_alerts._write_firing", _write_firing):
        _evaluate_rows(
            rows=rows,
            level="line",
            plan_id=_PLAN,
            plan_version_id=_VERSION,
            project_id=_PROJECT,
            overrun_thr=DEFAULT_OVERRUN_THRESHOLD,
            underdelivery_thr=DEFAULT_UNDERDELIVERY_THRESHOLD,
            overrun_source="documented_default",
            underdelivery_source="documented_default",
            window_date=None,
            as_of_day="2026-03-10",
            conn=_AlertConnection(),
        )
    return written


def _row(**overrides) -> dict:
    """A line pacing far below its allocation — a genuine under-delivery shape."""
    row = {
        "line_key": "line_display_q3",
        "label": "Display Q3",
        "is_plan_only": False,
        "allocated_to_date": 1000.0,
        "actual_to_date": 400.0,
        "pace": -0.60,
        "currency": "EUR",
        "money_gap_code": None,
        "actual_withheld": False,
    }
    row.update(overrides)
    return row


def test_a_genuine_under_delivery_still_fires():
    """The control. Without it the two tests below prove only that nothing fires."""
    written = _evaluate([_row()])

    assert len(written) == 1
    assert written[0]["metadata"]["kind"] == KIND_UNDERDELIVERY


def test_a_missing_exchange_rate_does_not_fire_an_under_delivery():
    """THE ONE THIS STORY EXISTS FOR.

    Same pace, same threshold, same everything — except that the money behind the
    number could not be established. The row is skipped, and no
    `mediaplan_pace_underdelivery` names a campaign that spent normally.
    """
    written = _evaluate([_row(money_gap_code="fx_rate_unavailable", actual_withheld=True)])

    assert written == []


def test_a_currency_mismatch_does_not_fire_either():
    """The other half of the same rule.

    A pace computed across two currencies is not a small pace, it is not a pace at
    all. The mart no longer produces one; this guard refuses it even if some
    other warehouse did.
    """
    written = _evaluate([_row(money_gap_code="plan_currency_mismatch", currency=None)])

    assert written == []


def test_the_alert_names_the_currency_of_the_amount_it_cites():
    """`actual_to_date` travels in the firing metadata, and it is money.

    It used to travel with no currency at all, so a reader of `app.alert_firings`
    could not tell what `400.0` was denominated in — and, before this story, the
    only currency anywhere near it was the plan's, on a converted amount.
    """
    written = _evaluate([_row()])

    assert written[0]["metadata"]["actual_to_date"] == 400.0
    assert written[0]["metadata"]["currency"] == "EUR"


@pytest.mark.parametrize(
    "overrides",
    [
        {"pace": None},
        {"is_plan_only": True},
        {"allocated_to_date": 0.0},
    ],
)
def test_the_three_pre_existing_refusals_are_untouched(overrides):
    """61.4 adds a guard; it removes none. These three are 22.6's and still hold."""
    assert _evaluate([_row(**overrides)]) == []
