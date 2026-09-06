"""Story 37.6 — WHICH sources can carry a Country split, derived and not asserted.

Task 5's open review finding asks for "the final applicable connector/managed-feed
regression proof". The word that matters is FINAL: a hand-written list of connector
names would be a claim about the descriptors, and it would go stale the first time a
connector gained or lost a country dimension without anyone editing this file.

So the matrix is DERIVED from the shipped `source_capabilities` — the same descriptors
`datastream_intents.compile_geographic_intent` reads at runtime — and then pinned. A
connector that gains country capability fails this file with its own name in the diff,
which is the only way the list can stay true.

WHAT MAKES A SOURCE APPLICABLE, and it is two conditions, not one:

  1. it declares a dimension whose `canonical_target` is `country` (or a report whose
     dimension list literally contains `country`);
  2. at least one of that report's `supported_grains` CONTAINS that field.

Condition 2 is the one a name list cannot express. A connector can expose a country
dimension and still be unable to deliver it JOINTLY with the grain a Datastream
already pulls — and the honest compilation of that is `blocked`, with the Datastream
and the reason named, never a silent consolidation. `country.md`: "If a source cannot
provide a compatible Country breakdown, the proposal names the Datastream and reason.
It never presents global-only data as country-complete."

THE MANAGED-FEED / FILE-SOURCE PATH IS A THIRD CASE and is covered below. It is not a
`connector_pull`, so there is no provider report to interrogate: the country column is
either in the declared grain or it is not, and the compilation says
`preserved_full_grain` or `blocked` accordingly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.datastream_intents import (  # noqa: E402
    _country_fields,
    compile_geographic_intent,
)
from core.geographic_reporting import GeographicPosture, Market  # noqa: E402

_MODULES = Path(__file__).resolve().parents[2] / "modules"

#: The connectors that can deliver a Country split TODAY, each with the reports that
#: can. Pinned exactly: a new applicable report is a change to what an operator is
#: offered, and it must be seen rather than absorbed.
_EXPECTED_APPLICABLE: dict[str, tuple[str, ...]] = {
    "cm360": ("reach",),
    "dv360": ("reach", "youtube_compatible"),
    "google-analytics": ("catalog_daily", "standard_daily"),
    "gsc": ("catalog_daily", "country_daily"),
    "taboola": ("campaign_summary",),
    "youtube-analytics": ("audience_geography",),
}

_LOCAL_MARKETS = GeographicPosture(
    mode="local_markets",
    markets=(Market(id="france", label="France", country_codes=("FR",)),),
)


def _capabilities(module: str) -> dict:
    raw = json.loads((_MODULES / module / "manifest.json").read_text(encoding="utf-8"))
    return raw.get("source_capabilities") or {}


def _derive_matrix() -> dict[str, dict[str, tuple[str, ...]]]:
    """Read every shipped descriptor and classify each report. No names hard-coded."""

    matrix: dict[str, dict[str, tuple[str, ...]]] = {}
    for manifest_path in sorted(_MODULES.glob("*/manifest.json")):
        module = manifest_path.parent.name
        caps = _capabilities(module)
        applicable: list[str] = []
        declared_but_blocked: list[str] = []
        for report in caps.get("reports", []):
            fields = _country_fields(report, caps)
            if not fields:
                continue
            grains = [set(grain) for grain in report.get("supported_grains", [])]
            joint = any(field in grain for grain in grains for field in fields)
            (applicable if joint else declared_but_blocked).append(str(report.get("id")))
        if applicable or declared_but_blocked:
            matrix[module] = {
                "applicable": tuple(sorted(applicable)),
                "declared_but_blocked": tuple(sorted(declared_but_blocked)),
            }
    return matrix


def test_the_applicable_source_matrix_is_exactly_what_the_descriptors_say() -> None:
    """The FINAL matrix Task 5 asks for, measured rather than declared."""

    matrix = _derive_matrix()
    assert matrix, "no descriptor was read -- the guard would be vacuous"

    applicable = {
        module: entry["applicable"] for module, entry in matrix.items() if entry["applicable"]
    }
    assert applicable == _EXPECTED_APPLICABLE, (
        "the set of sources that can carry a Country split changed.\n"
        f"  derived  = {applicable}\n"
        f"  expected = {_EXPECTED_APPLICABLE}\n"
        "This is what an operator is offered when they enable Country. Update the "
        "expectation deliberately, in the same commit as the descriptor change."
    )


def test_a_declared_country_dimension_with_no_joint_grain_is_reported_not_hidden() -> None:
    """Condition 2, and why the matrix cannot be a list of connector names.

    Today no shipped descriptor is in this state, and the assertion says so rather than
    passing on an empty set: a guard that silently finds nothing proves nothing. The
    branch it protects is exercised on a synthetic report below, where the grain really
    does exclude the country field.
    """

    matrix = _derive_matrix()
    blocked = {
        module: entry["declared_but_blocked"]
        for module, entry in matrix.items()
        if entry["declared_but_blocked"]
    }
    assert blocked == {}, (
        "a shipped report declares a country dimension it cannot deliver jointly with "
        f"any supported grain: {blocked}. That is a legitimate state -- the compilation "
        "must say `blocked` and name the Datastream -- but it is a change to what "
        "Country activation can promise, so it is recorded here on purpose."
    )

    # The branch itself, on a descriptor built to sit in that state.
    caps = {
        "fields": [
            {"field_id": "geo_country", "kind": "dimension", "canonical_target": "country"}
        ],
        "reports": [
            {
                "id": "narrow_report",
                "dimensions": ["date", "campaign_id", "geo_country"],
                # Every supported grain EXCLUDES the country field: the provider can
                # break down by country, but never jointly with the campaign grain the
                # Datastream already pulls.
                "supported_grains": [["date", "campaign_id"]],
            }
        ],
    }
    intent = {
        "source": {
            "kind": "connector_pull",
            "report_id": "narrow_report",
            "selection": {
                "dimensions": ["date", "campaign_id"],
                "grain": ["date", "campaign_id"],
            },
        }
    }
    compiled = compile_geographic_intent(intent, _LOCAL_MARKETS, caps)
    snapshot = compiled["geographic"]
    assert snapshot["compilation_status"] == "blocked"
    assert snapshot["effective_country_field"] is None
    # Nothing was added to the plan: a blocked source must not be silently widened.
    assert compiled["source"]["selection"]["grain"] == ["campaign_id", "date"]


def test_every_applicable_report_really_compiles_country_complete() -> None:
    """The matrix and the compiler must be one answer.

    Derived membership is worthless if `compile_geographic_intent` disagrees with it:
    the operator sees the proposal the COMPILER produced, not the matrix. Each
    applicable report is compiled with the grain it already supports, and must come
    back `country_complete` with its country field named.
    """

    for module, reports in _EXPECTED_APPLICABLE.items():
        caps = _capabilities(module)
        by_id = {str(item.get("id")): item for item in caps.get("reports", [])}
        for report_id in reports:
            report = by_id[report_id]
            fields = _country_fields(report, caps)
            grains = [sorted(set(grain)) for grain in report.get("supported_grains", [])]
            # Start from a grain the provider supports and that does NOT already carry
            # the country field: that is the state a Datastream is in before Country is
            # enabled, and the compilation has to widen it.
            base = next(
                (
                    grain
                    for grain in sorted(grains, key=len)
                    if not any(field in grain for field in fields)
                ),
                None,
            )
            if base is None:
                # Every supported grain already carries country: the plan needs no
                # widening, and `preserved_full_grain` is the honest status.
                base = min(grains, key=len)
            intent = {
                "source": {
                    "kind": "connector_pull",
                    "report_id": report_id,
                    "selection": {"dimensions": list(base), "grain": list(base)},
                }
            }
            snapshot = compile_geographic_intent(intent, _LOCAL_MARKETS, caps)["geographic"]
            assert snapshot["compilation_status"] == "country_complete", (
                f"{module}/{report_id} is in the applicable matrix but compiles "
                f"{snapshot['compilation_status']!r} from grain {base}"
            )
            assert snapshot["effective_country_field"] in fields, (
                f"{module}/{report_id} resolved a country field the descriptor does "
                f"not declare: {snapshot['effective_country_field']!r} not in {fields}"
            )
            # The country field really entered the plan, and the prior grain survived.
            grain_after = snapshot["impact"]["grain_after"]
            assert snapshot["effective_country_field"] in grain_after
            assert set(base) <= set(grain_after), (
                "widening a plan for Country must never DROP a dimension the "
                "Datastream already pulled"
            )


def test_a_managed_feed_carries_country_when_its_declared_grain_does() -> None:
    """The third case: a file source has no provider report to interrogate.

    `country.md`'s activation flow is the same for it — "Add Country to supported
    extraction plans" — but there is nothing to widen: the column is in the declared
    grain or it is not. A feed that already carries it is `preserved_full_grain`, which
    is the status that says "we did not change your extraction, and the split is real".
    """

    intent = {
        "source": {
            "kind": "managed_feed",
            "selection": {
                "dimensions": ["date", "campaign_id", "country"],
                "grain": ["date", "campaign_id", "country"],
            },
        }
    }
    snapshot = compile_geographic_intent(intent, _LOCAL_MARKETS, None)["geographic"]
    assert snapshot["compilation_status"] == "preserved_full_grain"
    assert snapshot["effective_country_field"] == "country"


def test_a_managed_feed_without_a_country_column_is_blocked_not_consolidated() -> None:
    """"It never presents global-only data as country-complete" (country.md)."""

    intent = {
        "source": {
            "kind": "managed_feed",
            "selection": {
                "dimensions": ["date", "campaign_id"],
                "grain": ["date", "campaign_id"],
            },
        }
    }
    snapshot = compile_geographic_intent(intent, _LOCAL_MARKETS, None)["geographic"]
    assert snapshot["compilation_status"] == "blocked"
    assert snapshot["effective_country_field"] is None


def test_a_free_text_country_field_is_deliberately_not_applicable() -> None:
    """A field named for a country is not a governed country dimension.

    `strava` maps `country` to `club_country` in its canonical dimension mapping and
    says so in the descriptor: "free-form, NOT an ISO-3166 code; renamed club_country
    to avoid colliding with the vocabulary-governed 'country' dimension". Its
    `canonical_target` is therefore not `country`, and it must not appear in the matrix
    — a free-text club location resolved through the ISO vocabulary would land real
    rows in Unknown and call it geography.
    """

    caps = _capabilities("strava")
    assert caps, "the descriptor must exist for this exclusion to mean anything"
    country_targets = [
        field
        for field in caps.get("fields", [])
        if field.get("kind") == "dimension" and field.get("canonical_target") == "country"
    ]
    assert country_targets == [], (
        "strava now declares a governed country dimension; if that is deliberate it "
        "belongs in the applicable matrix, and if it is not, the descriptor is wrong"
    )
    assert "strava" not in _derive_matrix()
