"""A conditional posed rate resolves -- the spreadsheet `IF`, against a real Postgres.

Ratified 2026-08-17 (amendment at the end of
``docs/product-architecture/alignment-register.md``, question 2 of the audit): a rate
may be set "as a value in the table -- or as a simple conditional rule (a rate that
applies when a named condition holds, in the spirit of a spreadsheet `IF`)". Migration
279 and 8748e279 delivered the value; migration 288 and this suite deliver the `IF`.

WHY POSTGRES AND NOT A DOUBLE. Everything that matters here is a relation between
STORED ROWS: whether two posed rates can coexist under
``uq_fx_rate_observation`` (which migration 288 had to re-key on the condition
digest), which of several matching rules is the most specific, and what happens when
two are equally specific. A fake would agree with whatever the writer believed --
which is exactly how the three defects below survived until they were measured.

WHAT IT PROVES, and each one was a MEASURED failure on the disposable base at 287
before it was a test:

  1. two posed rates coexist. Before this, ``post_fixed_rate`` minted a version
     holding one observation and activated it, superseding the previous -- so posting
     GBP/EUR silently removed USD/EUR from resolution, and a Project could hold
     exactly one posed pair at a time. A conditional rate is impossible under that:
     it must coexist with the rate it refines for either to have a precedence;
  2. a posed rate stops at the end of its own window. Before this it resolved
     indefinitely, because the only path that could serve it was the carry-forward
     one;
  3. it keeps the label ``fixed`` all the way to the reader. Before this it arrived
     labelled ``carry_forward`` -- the word migration 279 exists to prevent, restored
     at exactly the moment somebody read the number;
  4. the most specific matching rule wins, and the unconditional rate is the fallback
     rather than a rival;
  5. a tie REFUSES, naming the rules in conflict. It never picks one quietly;
  6. a condition the figure cannot answer is a NAMED gap, not a silent fall-through
     to the general rate.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

ORG = "org_test_fixture"


def _policy():
    """A Money Policy that permits everything this suite is NOT testing.

    Carry-forward and a very wide staleness bound are ON deliberately: if the posed
    path ever stopped honouring its window, these settings are what would let the old
    behaviour serve a rate anyway, so leaving them on is what makes the window
    assertion mean something.
    """
    from core.money_policy import MoneyPolicy

    return MoneyPolicy(
        rule_set_id="rs_fx_conditional",
        version_id="v_fx_conditional",
        content_hash="0" * 64,
        reporting_currency="EUR",
        reporting_currency_minor_unit=2,
        rounding="half_up",
        rate_source_priority=("operator",),
        allow_triangulation=False,
        triangulation_pivot=None,
        allow_carry_forward=True,
        max_staleness_days=3650,
    )


def _new_project(conn) -> str:
    project_id = f"proj_fxc_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, 'Conditional rate', %s, 'active', 'pytest', %s)
            """,
            (project_id, f"fxc-{uuid.uuid4().hex[:12]}", ORG),
        )
    return project_id


def _post(conn, project_id: str, **overrides):
    from core.fx_fixed_rates import post_fixed_rate

    payload = {
        "base_currency": "USD",
        "quote_currency": "EUR",
        "rate": "0.92",
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "note": "",
    }
    payload.update(overrides)
    return post_fixed_rate(
        conn, org_id=ORG, project_id=project_id, actor="operator@example.com", **payload
    )


def _resolve(conn, project_id: str, *, base="USD", on=date(2026, 6, 1), context=None):
    from core.fx_rate_sets import resolve_rate

    return resolve_rate(
        conn,
        project_id=project_id,
        base_currency=base,
        quote_currency="EUR",
        on_date=on,
        policy=_policy(),
        context=context,
    )


# ---------------------------------------------------------------------------
# 1-3. The three defects the conditional rule had to stand on.
# ---------------------------------------------------------------------------


def test_two_posed_rates_coexist_instead_of_erasing_each_other(live_postgres):
    """The finding that made the conditional rule impossible until it was fixed.

    Measured at 287: post USD/EUR, post GBP/EUR, and USD/EUR resolved to
    ``FxGap(no_rate_for_pair)``. Each post minted a one-observation version and
    superseded the last, and ``resolve_rate`` reads a single active version.
    """
    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, base_currency="USD", rate="0.92")
    second = _post(conn, project_id, base_currency="GBP", rate="1.17")

    # The new version is a SNAPSHOT: the rate posted plus the one still in force.
    assert second["observation_count"] == 2
    assert second["carried_forward"] == 1

    assert _resolve(conn, project_id, base="USD").rate == Decimal("0.92")
    assert _resolve(conn, project_id, base="GBP").rate == Decimal("1.17")


def test_a_posed_rate_stops_at_the_end_of_its_own_window(live_postgres):
    """A rate declared for 2026 must not convert a 2027 figure.

    The policy above allows carry-forward with a 10-year staleness bound, so if the
    posed path did not own its window this would resolve rather than gap -- which is
    precisely what it did before migration 288 promoted the window into a column.
    """
    from core.fx_rate_sets import FxGap

    conn = live_postgres
    project_id = _new_project(conn)
    _post(conn, project_id, valid_from="2026-01-01", valid_to="2026-12-31")

    assert _resolve(conn, project_id, on=date(2026, 6, 1)).rate == Decimal("0.92")

    with pytest.raises(FxGap) as caught:
        _resolve(conn, project_id, on=date(2027, 6, 1))
    assert caught.value.code == "no_rate_for_pair"


def test_a_posed_rate_reaches_the_reader_labelled_fixed(live_postgres):
    """`carry_forward` here would be migration 279's defect restored at read time."""
    conn = live_postgres
    project_id = _new_project(conn)
    _post(conn, project_id)

    evidence = _resolve(conn, project_id)

    assert evidence.method == "fixed"
    assert evidence.provider == "operator"
    assert evidence.as_payload()["fx_method"] == "fixed"


# ---------------------------------------------------------------------------
# 4. Precedence: the most specific matching rule wins.
# ---------------------------------------------------------------------------


def test_a_matching_condition_beats_the_unconditional_rate(live_postgres):
    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    _post(conn, project_id, rate="0.95", conditions={"country": ["FR", "BE"]})

    evidence = _resolve(conn, project_id, context={"country": "FR"})

    assert evidence.rate == Decimal("0.95")
    assert evidence.method == "fixed"
    # The provenance answers "why THIS number", not merely "a fixed rate applied".
    assert evidence.derivation["conditions"] == {"country": ["BE", "FR"]}
    assert evidence.derivation["specificity"] == 1
    assert evidence.derivation["matched_context"] == {"country": "FR"}


def test_the_unconditional_rate_is_the_fallback_when_the_condition_misses(live_postgres):
    """The simple fixed rate keeps working -- the guarantee this change must not break."""
    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    _post(conn, project_id, rate="0.95", conditions={"country": ["FR"]})

    evidence = _resolve(conn, project_id, context={"country": "DE"})

    assert evidence.rate == Decimal("0.92")
    assert evidence.derivation["specificity"] == 0


def test_more_condition_keys_outrank_fewer(live_postgres):
    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    _post(conn, project_id, rate="0.95", conditions={"country": ["FR"]})
    _post(
        conn,
        project_id,
        rate="0.99",
        conditions={"country": ["FR"], "connector": ["google_ads"]},
    )

    evidence = _resolve(
        conn, project_id, context={"country": "FR", "connector": "google_ads"}
    )

    assert evidence.rate == Decimal("0.99")
    assert evidence.derivation["specificity"] == 2


# ---------------------------------------------------------------------------
# 5-6. The two refusals. Neither may ever become a quiet pick.
# ---------------------------------------------------------------------------


def test_two_equally_specific_rules_refuse_instead_of_one_winning_silently(live_postgres):
    """The discipline `tax_fee_rule_set.validate_ladder_rules` applies to the fee ladder.

    Stronger here: a fee picked by `rule_id ASC` still appears in `applied_rule_ids`,
    whereas a RATE picked that way restates every monetary figure it touches and
    leaves nothing to notice it by.
    """
    from core.fx_rate_sets import FxGap

    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    _post(conn, project_id, rate="0.95", conditions={"country": ["FR"]})
    _post(conn, project_id, rate="0.97", conditions={"connector": ["google_ads"]})

    with pytest.raises(FxGap) as caught:
        _resolve(conn, project_id, context={"country": "FR", "connector": "google_ads"})

    assert caught.value.code == "fx_condition_conflict"
    # It names the repair AND the rules, so the conflict is actionable.
    message = str(caught.value)
    assert "fxo_" in message
    assert "narrow one of the conditions" in message

    # And it is a refusal, not a preference: neither rate was served.
    assert "0.95" not in message.split("(")[0]


def test_a_condition_the_figure_cannot_answer_is_a_named_gap(live_postgres):
    """UNRESOLVED is not NO_MATCH -- the three-valued rule borrowed from the fee/tax
    matcher: "if one clause cannot be evaluated, the conjunction is UNKNOWN, not
    false". Serving the general rate here would produce a total that looks governed.
    """
    from core.fx_rate_sets import FxGap

    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    _post(conn, project_id, rate="0.95", conditions={"country": ["FR"]})

    with pytest.raises(FxGap) as caught:
        _resolve(conn, project_id, context=None)

    assert caught.value.code == "fx_condition_unresolved"
    assert "country" in str(caught.value)


# ---------------------------------------------------------------------------
# Storage: what migration 288 had to change for any of the above to be possible.
# ---------------------------------------------------------------------------


def test_a_conditional_and_a_simple_rate_share_a_pair_and_a_date(live_postgres):
    """Migration 288's re-keyed uniqueness, asserted where it bites.

    Under 144's `UNIQUE (version, base, quote, effective_date)` these two rows collide
    on every column, so the conditional rule could not be stored beside the one it
    refines -- and a precedence rule between rows that cannot coexist is a rule about
    nothing.
    """
    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    result = _post(conn, project_id, rate="0.95", conditions={"country": ["FR"]})

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.rate, o.conditions, o.valid_from, o.valid_to, o.condition_digest
              FROM app.fx_rate_observations o
             WHERE o.rate_set_version_id = %s
             ORDER BY o.rate
            """,
            (result["id"],),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    assert [str(row[0]) for row in rows] == [
        "0.920000000000000000",
        "0.950000000000000000",
    ]
    assert [dict(row[1] or {}) for row in rows] == [{}, {"country": ["FR"]}]
    # The window is a COLUMN now, not only a line in `derivation`.
    assert rows[0][2] == date(2026, 1, 1)
    assert rows[0][3] == date(2026, 12, 31)
    # Distinct digests are what let both rows exist under one unique key.
    assert rows[0][4] != rows[1][4]


def test_reposting_one_rule_replaces_it_and_leaves_the_others_standing(live_postgres):
    """Editing one line of a table edits that line."""
    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    _post(conn, project_id, rate="0.95", conditions={"country": ["FR"]})
    _post(conn, project_id, rate="0.96", conditions={"country": ["FR"]})

    assert _resolve(conn, project_id, context={"country": "FR"}).rate == Decimal("0.96")
    assert _resolve(conn, project_id, context={"country": "DE"}).rate == Decimal("0.92")


def test_reposting_an_identical_rule_mints_no_rival_version(live_postgres):
    """Idempotency survives the snapshot AND the round trip through NUMERIC(38, 18).

    The values are deliberately typed in a different order: they canonicalise to one
    condition, so this is the same rule and not a second one.
    """
    conn = live_postgres
    project_id = _new_project(conn)

    _post(conn, project_id, rate="0.92")
    first = _post(conn, project_id, rate="0.95", conditions={"country": ["FR", "BE"]})
    again = _post(conn, project_id, rate="0.95", conditions={"country": ["BE", "FR"]})

    assert again["replayed"] is True
    assert again["id"] == first["id"]


# ---------------------------------------------------------------------------
# Reachability. A rule nothing can reach is a rule nobody has.
# ---------------------------------------------------------------------------


def test_the_condition_is_reachable_from_the_one_derivation_path(live_postgres):
    """`money_derivation.derive` is where a real figure becomes comparable.

    Proving the rule only through `resolve_rate` would prove a function, not a
    capability: `derive` is the single door every monetary value goes through, and
    until it passed a context the conditional rate could never fire on anything real.
    The context is `native.source_reference`, filtered to the keys a rate may be
    conditioned on -- `irrelevant` below must not become a matching clause.
    """
    from core.money_derivation import (
        GAP_RATE_CONDITION_UNRESOLVED,
        NativeMoney,
        derive,
    )

    conn = live_postgres
    project_id = _new_project(conn)
    _post(conn, project_id, rate="0.92")
    _post(conn, project_id, rate="0.95", conditions={"country": ["FR"]})

    def _derive(source_reference):
        return derive(
            conn,
            project_id=project_id,
            policy=_policy(),
            native=NativeMoney(
                amount="100",
                currency="USD",
                unit="decimal",
                adapter="decimal",
                on_date=date(2026, 6, 1),
                source_reference=source_reference,
            ),
        )

    in_france = _derive({"country": "FR", "irrelevant": "not a condition"})
    assert in_france.money_gap_code is None
    assert in_france.reporting_amount_micros == 95_000_000
    assert in_france.rate.method == "fixed"

    elsewhere = _derive({"country": "DE"})
    assert elsewhere.reporting_amount_micros == 92_000_000

    # And a figure that cannot answer the condition produces its OWN gap code -- not
    # `fx_rate_unavailable`, which would send someone hunting for a rate that is
    # already posted.
    unknown = _derive({})
    assert unknown.money_gap_code == GAP_RATE_CONDITION_UNRESOLVED
    assert unknown.reporting_amount_micros is None


def test_an_ingested_quotation_is_never_carried_into_a_posed_version(live_postgres):
    """A provider's number must not be restated as one a person posed.

    `_rates_in_force` filters on `method = 'fixed'`. Without it a posed version --
    whose provider is `operator` -- would copy an observed quotation forward and
    relabel it, which is migration 279's defect in the other direction.
    """
    conn = live_postgres
    project_id = _new_project(conn)
    first = _post(conn, project_id, rate="0.92")

    # An ingested-looking observation, landed on the active version by hand.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.fx_rate_observations
                (id, rate_set_version_id, project_id, base_currency, quote_currency,
                 rate, effective_date, as_of_date, method, retrieved_at,
                 validation_status, content_hash)
            VALUES (%s, %s, %s, 'CHF', 'EUR', 1.05, '2026-01-01', '2026-01-01',
                    'direct', NOW(), 'validated', %s)
            """,
            (f"fxo_{'0' * 26}", first["id"], project_id, "a" * 64),
        )

    later = _post(conn, project_id, base_currency="GBP", rate="1.17")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT base_currency, method FROM app.fx_rate_observations "
            "WHERE rate_set_version_id = %s ORDER BY base_currency",
            (later["id"],),
        )
        rows = cur.fetchall()

    assert rows == [("GBP", "fixed"), ("USD", "fixed")]
    assert "CHF" not in {row[0] for row in rows}
