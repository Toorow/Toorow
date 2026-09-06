"""Story 27.9 -- the client's name for a dimension REACHES the thing a person reads.

WHY THIS FILE EXISTS. `app.dimension_labels` (migration 103), `resolve_label_map` and
`decorate_envelope_with_labels` all shipped on 2026-07-25, and the closure review of
2026-08-01 found what a `grep` for callers found again on 2026-08-22: nothing outside
`tests/core/test_dimension_lineage.py` ever called them. A client could rename
a dimension, the row landed in the table, the cascade resolved it correctly -- and every
chart, legend and axis went on showing the internal identifier. A label stored and never
read renames nothing.

The rule is written in docs/product-architecture/governance.md, section "A client label
reaches every surface that shows the number".

NOTHING HERE NAMES A DIMENSION. The set of labellable dimensions is derived from the
shipped manifests, so a connector that conforms a new one tomorrow is covered by exactly
these assertions without an edit.
"""

from __future__ import annotations

import pytest
from core import dimension_lineage as dl
from core.dimension_conformance import LABEL_SOURCE_CLIENT, LABEL_SOURCE_FALLBACK
from core.envelope import build_canonical_envelope

_FRESHNESS = {"last_pull": "2026-08-22T00:00:00Z", "cadence_hours": 24, "stale_since": None}
_PROVENANCE = [{"source_system": "example", "pull_id": "pull_EXAMPLE"}]
_RANGE = {"start": "2026-08-01", "end": "2026-08-21"}


def _a_real_dimension() -> str:
    """One canonical dimension the shipped tree actually declares."""
    known = sorted(dl.known_canonical_dimensions())
    assert known, "no shipped manifest declares a canonical dimension"
    return known[0]


# ---------------------------------------------------------------------------
# What a report labels: what it SHOWS, derived from the manifests.
# ---------------------------------------------------------------------------


def test_the_labellable_vocabulary_is_read_from_the_manifests_not_listed():
    known = dl.known_canonical_dimensions()
    assert len(known) > 10, "the canonical vocabulary did not reach server/modules"
    assert all(isinstance(name, str) and name == name.strip().lower() for name in known)


def test_a_report_labels_the_dimensions_it_shows_and_nothing_else():
    dimension = _a_real_dimension()
    rows = [{dimension: "x", "clicks": 1, "not_a_dimension_at_all": "y"}]
    assert dl.dimensions_in_rows(rows) == [dimension]
    # A column no manifest conforms is not a dimension, and resolving the whole
    # vocabulary on every read would put a hundred names a reader never sees into
    # the envelope.
    assert "not_a_dimension_at_all" not in dl.dimensions_in_rows(rows)
    assert dl.dimensions_in_rows([]) == []


# ---------------------------------------------------------------------------
# The envelope: additive, exactly like meta.branding.
# ---------------------------------------------------------------------------


def _envelope(dimension_labels):
    return build_canonical_envelope(
        rows=[],
        meta_freshness=_FRESHNESS,
        meta_provenance=_PROVENANCE,
        date_range=_RANGE,
        connectors=["example"],
        dimension_labels=dimension_labels,
    )


@pytest.mark.parametrize("empty", [None, {}])
def test_nothing_to_say_omits_the_key_rather_than_writing_null(empty):
    """AD-1 additive key. A `null` would make every existing reader branch on it."""
    assert "dimension_labels" not in _envelope(empty)["meta"]


def test_the_label_a_client_chose_travels_on_the_envelope():
    dimension = _a_real_dimension()
    label_map = {
        dimension: {
            "display_label": "Langue",
            "description": None,
            "scope_level": "project",
            "label_source": LABEL_SOURCE_CLIENT,
        }
    }
    meta = _envelope(label_map)["meta"]
    assert meta["dimension_labels"] == label_map
    # The stable identifier is the KEY, never replaced: the product joins on it and
    # the person never sees it.
    assert dimension in meta["dimension_labels"]


def test_a_fallback_is_distinguishable_from_a_name_a_client_chose():
    """Otherwise a reader cannot tell 'they called it this' from 'nobody named it'."""
    dimension = _a_real_dimension()
    resolved = dl.build_label_map([dimension], {})
    assert resolved[dimension]["display_label"] == dimension
    assert resolved[dimension]["label_source"] == LABEL_SOURCE_FALLBACK

    chosen = dl.build_label_map([dimension], {dimension: {"display_label": "Langue"}})
    assert chosen[dimension]["display_label"] == "Langue"
    assert chosen[dimension]["label_source"] == LABEL_SOURCE_CLIENT


# ---------------------------------------------------------------------------
# The resolver never takes a report down.
# ---------------------------------------------------------------------------


def test_a_store_that_cannot_be_reached_serves_the_report_with_the_fallback(monkeypatch):
    """The number is the answer; the label is how it is read. Same posture as branding."""
    dimension = _a_real_dimension()

    def _explode(*_args, **_kwargs):
        raise RuntimeError("the label store is unreachable")

    monkeypatch.setattr(dl, "_org_of_project", lambda _project_id: "org_EXAMPLE")
    monkeypatch.setattr("core.dimension_conformance.resolve_dimension_labels", _explode)

    labels = dl.resolve_report_dimension_labels("proj_EXAMPLE", [{dimension: "x"}])
    assert labels[dimension]["display_label"] == dimension
    assert labels[dimension]["label_source"] == LABEL_SOURCE_FALLBACK


def test_a_report_showing_no_conformed_dimension_asks_the_store_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(dl, "_org_of_project", lambda _p: called.append("org") or "org_EXAMPLE")
    assert dl.resolve_report_dimension_labels("proj_EXAMPLE", [{"clicks": 1}]) == {}
    assert called == [], "the label store was queried for a report that shows no dimension"


# ---------------------------------------------------------------------------
# The wiring itself: the one canonical envelope builder is fed by the resolver.
# ---------------------------------------------------------------------------


def test_the_daily_report_call_site_resolves_labels_from_the_rows_it_serves():
    """The gap the review named was a CALLER gap, not a function gap.

    `resolve_label_map` behaved correctly and nobody called it, so a unit test on the
    function could never have failed. This asserts the wiring instead: the canonical
    envelope builder is called with the resolver's output, from the module that builds
    the daily report.
    """
    import inspect

    from core import reporting_mcp

    source = inspect.getsource(reporting_mcp)
    assert "dimension_labels=dimension_lineage.resolve_report_dimension_labels(" in source, (
        "the canonical envelope is built without the client labels again -- this is the "
        "exact state the 2026-08-01 closure review refused"
    )
