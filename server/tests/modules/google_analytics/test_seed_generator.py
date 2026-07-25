"""Unit tests for the GA4 seed CSV generator — Story 1.4, T1.3."""

from __future__ import annotations

import csv

# Import from the module's seeds package
import importlib.util
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

_SEEDS_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-analytics" / "seeds"


def _import_generate_seed():
    """Dynamically import generate_seed.py (hyphenated parent dir)."""
    spec = importlib.util.spec_from_file_location("generate_seed", _SEEDS_DIR / "generate_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _import_generate_seed()


@pytest.fixture(scope="module")
def rows(gen):
    rng = random.Random(42)  # deterministic
    end_date = date.today()
    return gen.generate_rows(end_date=end_date, rng=rng)


# ---------------------------------------------------------------------------
# AC1 assertions
# ---------------------------------------------------------------------------


def test_column_names(gen, tmp_path):
    """Output CSV has the required column headers."""
    rng = random.Random(1)
    rows = gen.generate_rows(rng=rng)
    out = tmp_path / "test.csv"
    gen.write_csv(rows, out)
    with out.open() as fh:
        reader = csv.DictReader(fh)
        assert set(reader.fieldnames) == {
            "date",
            "device_category",
            "country",
            "sessions",
            "active_users",
            "conversions",
        }


def test_90_distinct_dates(rows):
    """Exactly 90 distinct dates must be present."""
    dates = {r["date"] for r in rows}
    assert len(dates) == 90


def test_date_range(rows):
    """Dates must be the 90 days ending yesterday (end_date - 1)."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    first_expected = today - timedelta(days=90)

    min_date = date.fromisoformat(min(r["date"] for r in rows))
    max_date = date.fromisoformat(max(r["date"] for r in rows))

    assert min_date == first_expected
    assert max_date == yesterday


def test_device_categories(rows):
    """Exactly 3 device categories: desktop, tablet, mobile."""
    devices = {r["device_category"] for r in rows}
    assert devices == {"desktop", "tablet", "mobile"}


def test_countries(rows):
    """At least 5 countries, all with GA4 full names."""
    countries = {r["country"] for r in rows}
    assert len(countries) >= 5
    required = {"France", "Germany", "United Kingdom", "United States", "Spain"}
    assert required.issubset(countries)


def test_metric_integrity(rows):
    """conversions <= active_users <= sessions for every row (AC1, AD-4)."""
    for r in rows:
        sessions = int(r["sessions"])
        active_users = int(r["active_users"])
        conversions = int(r["conversions"])
        assert active_users <= sessions, (
            f"active_users ({active_users}) > sessions ({sessions}) for row {r}"
        )
        assert conversions <= active_users, (
            f"conversions ({conversions}) > active_users ({active_users}) for row {r}"
        )


def test_no_negative_values(rows):
    """All metric values must be non-negative."""
    for r in rows:
        assert int(r["sessions"]) >= 0
        assert int(r["active_users"]) >= 0
        assert int(r["conversions"]) >= 0


def test_weekday_higher_than_weekend(rows):
    """Weekday rows have higher average sessions than weekend rows (AC1 seasonality)."""
    weekday_sessions = []
    weekend_sessions = []
    for r in rows:
        d = date.fromisoformat(r["date"])
        if d.weekday() in (5, 6):  # Saturday=5, Sunday=6
            weekend_sessions.append(int(r["sessions"]))
        else:
            weekday_sessions.append(int(r["sessions"]))

    avg_weekday = sum(weekday_sessions) / len(weekday_sessions)
    avg_weekend = sum(weekend_sessions) / len(weekend_sessions)

    # Weekend should be 25-40% lower than weekdays
    assert avg_weekend < avg_weekday, (
        f"Expected weekend avg ({avg_weekend:.1f}) < weekday avg ({avg_weekday:.1f})"
    )
    ratio = avg_weekend / avg_weekday
    assert ratio < 0.80, (
        f"Weekend/weekday ratio {ratio:.2f} is not low enough (expected < 0.80)"
    )


def test_row_count(rows):
    """Expected: 90 days × 3 devices × 5 countries = 1350 rows."""
    assert len(rows) == 90 * 3 * 5


def test_write_csv_round_trip(gen, tmp_path):
    """Written CSV can be read back with consistent data."""
    rng = random.Random(7)
    rows = gen.generate_rows(rng=rng)
    out = tmp_path / "round_trip.csv"
    gen.write_csv(rows, out)

    with out.open() as fh:
        back = list(csv.DictReader(fh))

    assert len(back) == len(rows)
    assert back[0]["date"] == rows[0]["date"]
    assert int(back[0]["sessions"]) == int(rows[0]["sessions"])
