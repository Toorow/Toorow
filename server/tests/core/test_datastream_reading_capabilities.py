"""What an ACTIVE Project capability does to the reading of one day -- lot B3.

The module under test is `core.datastream_reading_capabilities`, driven THROUGH
the route that mounts it, because the question this lot answers is what a person
sees on the data -- and the payload is what carries it there.

The doubles are `test_datastream_daily_breakdown`'s own, imported rather than
respelled: `_conn` already answers the capability switch and the mapping store,
and a second cursor double would be a second opinion about what this route reads.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.core.test_datastream_daily_breakdown import (
    _breakdown,
    _conn,
    _described,
    _described_money,
    _no_country,
    _no_mart,
    _pull,
    _reading_double,
)

# ---------------------------------------------------------------------------
# An active capability lands ON THE DATA -- lot B3, amendment 11.
#
# "Une capacite activee AJOUTE UN ONGLET, OU COLORE LE CHAMP DANS L'APERCU --
# jamais une liste de modules." What is held here is that the effect travels on
# the reading and that OFF travels as nothing at all: no entry, no annotated
# amount, and not even the read of the authority that would designate one.
#
# AND NOTHING BELOW LIGHTS UP ON A LIVE PROJECT. `select capability_key, state,
# count(*) from app.project_capabilities group by 1, 2` on 2026-08-12: SIX rows
# over 31 projects, ZERO in `ready` or `degraded` -- `competitors`, `country`,
# `placement_mapping`, `tax_fees` are `disabled` on 31/31 and `currency_fx` and
# `reporting_timezone` are `draft` on 31/31. Every ON state in this section is
# fabricated by this file, and it says so, exactly as story 58.5's country
# fixture does one section up.
# ---------------------------------------------------------------------------


def _capabilities(payload):
    return {entry["key"]: entry for entry in payload["reading"]["capabilities"]}


def test_no_capability_of_the_estate_projects_anything_onto_a_reading() -> None:
    """The default of `_conn` is the measured estate, and it draws nothing."""
    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(_conn(pulls=[_pull()]), day="2026-07-10")

    assert payload["reading"]["capabilities"] == []


def test_an_inactive_currency_capability_annotates_no_amount_and_says_which_door() -> None:
    """OFF is a sentence, not a silence -- and not `no_monetary_concept_declared`.

    Those are two different repairs: one sends a person to Project Settings, the
    other to publish a Semantic Concept. Answering the second while the switch is
    off queues work on a desk that cannot use it.
    """
    from core.datastream_reading_capabilities import MONEY_CAPABILITY_NOT_ACTIVE

    conn = _conn(pulls=[_pull()], monetary_concepts=("cost",))
    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(conn, day="2026-07-10")

    for zone in ("collected", "mapped"):
        designation = payload["reading"][zone]["money_provenance"]
        assert designation["reason"] == MONEY_CAPABILITY_NOT_ACTIVE
        assert designation["columns"] == []
        assert designation["capability_state"] == "draft"
        # The whole shape, refusing: a screen must not tell a refusal from an
        # answer by counting keys.
        assert "rows" in designation and "reporting_currency" in designation
    # And the authority is not even asked: a capability nobody turned on costs no
    # statement, which is the same rule the country split has held since 58.5.
    assert not any("value_type = 'money'" in text for text in conn.statements)


def test_an_active_currency_capability_names_the_fields_it_colours() -> None:
    from core.datastream_reading_capabilities import EFFECT_MONEY_LINE

    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(
            _conn(
                pulls=[_pull()],
                monetary_concepts=("cost",),
                currency_fx_capability_state="ready",
            ),
            day="2026-07-10",
        )

    entry = _capabilities(payload)["currency_fx"]
    assert entry["effect"] == EFFECT_MONEY_LINE
    assert entry["state"] == "ready"
    assert entry["degraded"] is False
    # THE FIELD, not the module: what travels is the column the line lands under.
    assert entry["fields"] == {"collected": [], "mapped": ["cost"]}
    assert entry["reason"] is None


def test_an_active_currency_capability_with_no_amount_says_so_on_the_reading() -> None:
    """ON and nothing to colour is a state of the SEMANTIC MODEL, and it is named.

    It is the state of every project of this product on the day it turns the
    capability on: 0 of 18 has published a `money` Concept version.
    """
    from core.datastream_reading_capabilities import NO_MONETARY_COLUMN

    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(
            _conn(pulls=[_pull()], currency_fx_capability_state="ready"),
            day="2026-07-10",
        )

    entry = _capabilities(payload)["currency_fx"]
    assert entry["reason"] == NO_MONETARY_COLUMN
    assert entry["fields"] == {"collected": [], "mapped": []}
    assert entry["message"]


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", "unset"])
def test_only_ready_and_degraded_project_a_capability_onto_the_reading(state) -> None:
    """The SAME threshold that opens the `Cost` tab and the country split.

    Two thresholds for one switch is how a tab and a column end up saying two
    different things about one capability.
    """
    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(
            _conn(
                pulls=[_pull()],
                currency_fx_capability_state=state,
                country_capability_state=state,
                reporting_timezone_capability_state=state,
            ),
            day="2026-07-10",
        )

    assert payload["reading"]["capabilities"] == []


def test_a_degraded_capability_projects_AND_says_it_is_degraded() -> None:
    """Hiding evidence already collected is worse than showing it diminished."""
    with _no_mart(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(
            _conn(
                pulls=[_pull()],
                monetary_concepts=("cost",),
                currency_fx_capability_state="degraded",
            ),
            day="2026-07-10",
        )

    entry = _capabilities(payload)["currency_fx"]
    assert entry["degraded"] is True
    assert entry["fields"]["mapped"] == ["cost"]


#: A mapping that binds a field to the CANONICAL country dimension. It is the
#: mapping that makes a column a country here, never its spelling: `pays` bound to
#: `country` is one, and a column named `country` that no field binds is not.
_COUNTRY_MAPPING = {
    "grain": [],
    "fields": [
        {"field_id": "pays", "suggestion": {"sensitivity": "none"},
         "binding": {"status": "confirmed", "canonical_target": "country"}},
    ],
}


def _country_described():
    return _described(
        collected={"columns": ["date", "pays"]},
        mapped={"columns": ["date", "country"]},
    )


def test_an_active_country_capability_marks_the_field_on_both_sides() -> None:
    from core.datastream_reading_capabilities import EFFECT_FIELD_MARKED

    with _no_mart(), _no_country(), _country_described(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(
            collected_columns=("date", "pays"), mapped_columns=("date", "country")
        ),
    ):
        payload = _breakdown(
            _conn(
                pulls=[_pull()],
                mapping_version=_COUNTRY_MAPPING,
                country_capability_state="ready",
            ),
            day="2026-07-10",
        )

    entry = _capabilities(payload)["country"]
    assert entry["effect"] == EFFECT_FIELD_MARKED
    # The SOURCE spelling on the collected side and the CANONICAL one on the
    # mapped side: the same field, and the two relations do not name it alike.
    assert entry["fields"] == {"collected": ["pays"], "mapped": ["country"]}


def test_a_country_column_no_mapping_field_binds_is_not_a_country() -> None:
    """The spelling is not the authority, and this is the test that says so.

    Both relations carry a column literally called `country` and the mapping binds
    none of its fields to the canonical dimension. Marking it would be a
    classification invented on a name -- the exact reasoning story 48.3 removed
    from the money designation.
    """
    from core.datastream_reading_capabilities import NO_COUNTRY_FIELD_IN_MAPPING

    with _no_mart(), _no_country(), _described(
        collected={"columns": ["date", "country"]},
        mapped={"columns": ["date", "country"]},
    ), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(
            collected_columns=("date", "country"), mapped_columns=("date", "country")
        ),
    ):
        payload = _breakdown(
            _conn(pulls=[_pull()], country_capability_state="ready"),
            day="2026-07-10",
        )

    entry = _capabilities(payload)["country"]
    assert entry["reason"] == NO_COUNTRY_FIELD_IN_MAPPING
    assert entry["fields"] == {"collected": [], "mapped": []}
    # And the sentence names the gesture, not the mechanism.
    assert "Mapping" in entry["message"]


def test_a_bound_country_field_neither_relation_carries_says_which_absence() -> None:
    from core.datastream_reading_capabilities import NO_COUNTRY_COLUMN_IN_RELATION

    with _no_mart(), _no_country(), _described(
        collected={"columns": ["date"]}, mapped={"columns": ["date"]}
    ), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(collected_columns=("date",), mapped_columns=("date",)),
    ):
        payload = _breakdown(
            _conn(
                pulls=[_pull()],
                mapping_version=_COUNTRY_MAPPING,
                country_capability_state="ready",
            ),
            day="2026-07-10",
        )

    assert _capabilities(payload)["country"]["reason"] == NO_COUNTRY_COLUMN_IN_RELATION


class _Zone:
    reporting_timezone = "Europe/Paris"


def _timezone_policy(policy=None):
    return patch(
        "core.money_policy.try_resolve_timezone_policy",
        return_value=_Zone() if policy == "confirmed" else None,
    )


def test_the_reporting_timezone_signals_the_day_and_moves_not_one_value() -> None:
    """The ratified rule of this product, held as an assertion.

    The module SIGNALS a boundary; it never re-aligns a day. So the reading with
    the capability on and the reading with it off carry the same rows, byte for
    byte, and the only difference is a sentence.
    """
    from core.datastream_reading_capabilities import EFFECT_DAY_BOUNDARY_SIGNAL

    def _read(state):
        with _no_mart(), _described_money(), patch(
            "core.collected_mapped_reader.read_collected_and_mapped",
            return_value=_reading_double(collected_columns=("date", "spend")),
        ):
            return _breakdown(
                _conn(pulls=[_pull()], reporting_timezone_capability_state=state),
                day="2026-07-10",
            )

    with _timezone_policy("confirmed"):
        on = _read("ready")
    off = _read("draft")

    entry = _capabilities(on)["reporting_timezone"]
    assert entry["effect"] == EFFECT_DAY_BOUNDARY_SIGNAL
    assert entry["project_reporting_timezone"] == "Europe/Paris"
    assert "never corrected" in entry["message"]
    # It colours no field: there is none to colour, and holding a column open for
    # it is exactly what the amendment forbids.
    assert entry["fields"] == {"collected": [], "mapped": []}
    # AND NOT ONE ROW MOVED.
    for zone in ("collected", "mapped"):
        assert on["reading"][zone]["rows"] == off["reading"][zone]["rows"]
        assert on["reading"][zone]["columns"] == off["reading"][zone]["columns"]
    assert on["reading"]["day"] == off["reading"]["day"] == "2026-07-10"


def test_an_active_timezone_capability_with_no_policy_names_the_door() -> None:
    from core.datastream_reading_capabilities import NO_TIMEZONE_POLICY

    with _no_mart(), _described_money(), _timezone_policy(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        payload = _breakdown(
            _conn(pulls=[_pull()], reporting_timezone_capability_state="ready"),
            day="2026-07-10",
        )

    entry = _capabilities(payload)["reporting_timezone"]
    assert entry["reason"] == NO_TIMEZONE_POLICY
    assert "Project Settings" in entry["message"]


def test_the_switch_is_read_ONCE_for_the_grid_and_for_the_reading() -> None:
    """One statement over `app.project_capabilities`, whatever it is asked about.

    The day grid needed `country` and read it alone; the reading needs three. Two
    reads of one switch is how two halves of one screen end up disagreeing about
    it, and the singular read is now a fallback nobody on this route takes.
    """
    conn = _conn(pulls=[_pull()], country_capability_state="ready")
    with _no_mart(), _no_country(), _described_money(), patch(
        "core.collected_mapped_reader.read_collected_and_mapped",
        return_value=_reading_double(),
    ):
        _breakdown(conn, day="2026-07-10")

    reads = [text for text in conn.statements if "app.project_capabilities" in text]
    assert len(reads) == 1
    assert "capability_key = ANY" in reads[0]


def test_a_capability_never_reaches_a_reading_nobody_asked_for() -> None:
    """No day opened, no reading, and therefore no projection to make."""
    with _no_mart():
        payload = _breakdown(_conn(pulls=[_pull()], currency_fx_capability_state="ready"))
    assert payload["reading"] is None
