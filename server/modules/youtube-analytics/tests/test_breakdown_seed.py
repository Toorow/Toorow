"""AI-342 -- the breakdown seed is derived from the manifest, and it adds up.

WHAT THIS FILE HOLDS, AND WHY IT IS NOT THE dbt TEST. The two singular dbt tests
(`test_youtube_breakdown_reconciles`, `test_youtube_canonical_breakdown_unchanged`)
assert what the BUILT mart says, and they can only run where a warehouse has been
built. This file asserts the two properties of the FIXTURE itself, at import cost:

  * the landing is derived by the manifest -- a profile's own dimensions crossed
    with its own metrics -- so a renamed field or a retired metric changes the
    seed without anyone editing the loader. A hand-listed explosion is exactly
    what `AI-213` had to repair in the local-loop runner;
  * every breakdown of a day totals that day's channel roll-up, per metric. The
    mart's whole double-count discipline (each series independently totals the
    day, MIN(breakdown_dimension) picks one) rests on that property, and a
    fixture whose marginals disagreed with its own roll-up would let every
    reconciliation test pass against a fiction.

It also pins the ONE thing the fixture must NOT do: land an additive row for
`viewer_percentage`. That metric is a share (`aggregation=latest`,
`non_additive=true`), and the demographic profile carries nothing else.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest

_MODULE_DIR = Path(__file__).parents[1]
_SERVER_DIR = Path(__file__).parents[3]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

_ADDITIVE_METRICS = ("views", "estimated_minutes_watched")


def _loader():
    path = _MODULE_DIR / "seeds" / "load_youtube_analytics_seed.py"
    spec = importlib.util.spec_from_file_location("youtube_breakdown_seed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def loader():
    return _loader()


@pytest.fixture(scope="module")
def manifest():
    return json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


def test_every_landed_row_is_a_dimension_and_metric_the_manifest_declares(loader, manifest):
    """No dimension, and no metric, that the profile does not declare."""
    declared_dimensions: set[str] = set()
    declared_metrics: set[str] = set()
    for report in manifest["source_capabilities"]["reports"]:
        for dimension in report.get("dimensions") or []:
            if dimension not in ("date", "channel_id"):
                declared_dimensions.add(dimension)
        declared_metrics.update(report.get("metrics") or [])

    rows = loader.generate_breakdown_rows()
    assert rows, "the breakdown seed lands nothing at all"
    landed_dimensions = {row["breakdown_dimension"] for row in rows}
    landed_metrics = {row["metric"] for row in rows}
    assert landed_dimensions <= declared_dimensions, sorted(
        landed_dimensions - declared_dimensions
    )
    assert landed_metrics <= declared_metrics, sorted(landed_metrics - declared_metrics)


def test_a_two_dimension_profile_lands_the_same_cell_under_each_of_its_dimensions(loader):
    """`audience_device` reports a cross, and both marginals must be readable.

    This is the shape AI-342 found the staging model collapsing: two cells of one
    pull sharing `(breakdown_dimension='device_type', breakdown_value='MOBILE')`.
    If the loader ever stopped landing the cross, the staging repair would have
    nothing left to prove.
    """
    rows = loader.generate_breakdown_rows()
    per_key: dict[tuple, int] = defaultdict(int)
    for row in rows:
        if row["metric"] != "views":
            continue
        per_key[
            (row["date"], row["breakdown_dimension"], row["breakdown_value"])
        ] += 1
    crossed = {key: count for key, count in per_key.items() if count > 1}
    assert crossed, (
        "no breakdown key lands more than one cell, so the two-dimension profile "
        "is not being exploded and the collapse AI-342 repaired cannot be reproduced"
    )
    assert all(key[1] in ("device_type", "operating_system") for key in crossed), sorted(crossed)


def test_every_breakdown_of_a_day_totals_that_days_channel_roll_up(loader):
    """The property the mart's parallel-series discipline rests on."""
    daily = defaultdict(float)
    for row in loader.generate_rows():
        if row["video"]:  # the per-video grain is not the channel roll-up
            continue
        if row["metric"] in _ADDITIVE_METRICS:
            daily[(row["date"], row["metric"])] += row["value"]
    assert daily, "the daily golden pull carries neither additive metric"

    breakdown = defaultdict(float)
    for row in loader.generate_breakdown_rows():
        if row["metric"] not in _ADDITIVE_METRICS:
            continue
        breakdown[
            (row["date"], row["metric"], row["breakdown_dimension"])
        ] += row["value"]
    assert breakdown, "the breakdown fixture carries no additive metric"

    for (date, metric, dimension), total in sorted(breakdown.items()):
        expected = daily.get((date, metric))
        assert expected is not None, (
            f"{dimension} reports {date} and the channel roll-up does not"
        )
        assert total == pytest.approx(expected), (
            f"{metric} split by {dimension} on {date} totals {total}, the channel "
            f"roll-up says {expected}"
        )


def test_the_share_metric_lands_but_never_as_an_additive_row(loader, manifest):
    """`viewer_percentage` is collected, and the fact must never sum it.

    The seed lands it -- refusing to would hide a real report -- and the mart
    branch filters it out by naming the additive set. Both halves are asserted
    here so the day someone widens that filter, this fails.
    """
    fields = {
        field["field_id"]: field
        for field in manifest["source_capabilities"]["fields"]
        if field.get("field_id")
    }
    assert fields["viewer_percentage"]["non_additive"] is True
    rows = loader.generate_breakdown_rows()
    shares = [row for row in rows if row["metric"] == "viewer_percentage"]
    assert shares, "the demographic profile lands nothing"
    assert {row["breakdown_dimension"] for row in shares} == {"age_group", "gender"}
    assert not [row for row in shares if row["metric"] in _ADDITIVE_METRICS]


def test_the_fact_branch_names_exactly_the_additive_metrics_of_this_landing():
    """The mart's filter and this module's additive set are the same two names.

    Read from the model rather than restated: a third additive breakdown metric
    added to the fact without being reconciled here would otherwise be summed
    with nothing asserting that it totals its day.
    """
    model = (
        _SERVER_DIR.parent / "dbt" / "models" / "marts" / "fact_daily_kpi.sql"
    ).read_text(encoding="utf-8")
    marker = "{% set youtube_breakdown_metrics = "
    line = next(part for part in model.split("\n") if part.startswith(marker))
    named = json.loads(line[len(marker) : line.rindex("%}")].strip().replace("'", '"'))
    assert tuple(named) == _ADDITIVE_METRICS
