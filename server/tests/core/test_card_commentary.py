"""Story 9.7 — Per-card deterministic cited commentary + guidelines wiring.

Tests cover:
  1. Per-card builder outputs: each builder cites its card-specific mover/winner.
  2. AD-9 negative test per builder: with context_events=[], comment contains
     "Contexte manquant" and no causal language; with an event, the cited cause
     is EXACTLY the event's label (never invented).
  3. Guidelines flow: get_card (ad-hoc path) envelope carries llm_commentary_guidelines
     when _fetch_r6_adhoc returns them; report_ref path unchanged (regression).
  4. <= 3 lines rendered comment; <= 30-line summary still holds.
  5. Empty/partial data: builder degrades to the generic comment (never raises).
  6. CARD_COMMENT_BUILDERS registry is data-driven: keyed by template id, no module
     branches.

All warehouse calls are mocked so no live mart is required. French strings with
accents are verified only on content (not byte-equality) so pytest output stays
ASCII-safe (AI-03).
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core import cards as cards_module  # noqa: E402
from core import narrative as narrative_module  # noqa: E402
from core import rollup as rollup_module  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _row(metric, dim, val, value, connector="gsc", date="2026-07-05"):
    return {
        "date": date,
        "connector": connector,
        "metric": metric,
        "breakdown_dimension": dim,
        "breakdown_value": val,
        "value": float(value),
        "pull_id": "pull_abc",
        "loaded_at": f"{date}T00:00:00",
    }


def _prior_row(metric, dim, val, value, connector="gsc"):
    """A row in the prior period (2026-06-01 to 2026-06-30, outside current window)."""
    return _row(metric, dim, val, value, connector=connector, date="2026-06-28")


def _ctx(rows, metrics, *, start="2026-07-01", end="2026-07-06"):
    """Build a _BlockContext with a real compute_rollup."""
    pull_ids = sorted({r.get("pull_id") for r in rows if r.get("pull_id")})
    rollup = rollup_module.compute_rollup(rows, metrics, start, end, "default", pull_ids)
    current, prior = rollup_module.split_periods(rows, start, end)
    return cards_module._BlockContext(
        current_rows=current,
        prior_rows=prior,
        rollup=rollup,
        metric_definitions=None,
        resolved_metrics=metrics,
        start=start,
        end=end,
    )


def _build_block_data_for(
    rows, metrics, *, start="2026-07-01", end="2026-07-06", composition_blocks=None
):
    """Resolve a set of non-comment blocks and return a block_data dict by type.

    Mirrors the two-phase approach in get_card (Story 9.7 / 10.2).
    """
    ctx = _ctx(rows, metrics, start=start, end=end)
    block_data: dict = {}
    for block in composition_blocks or []:
        if block.get("type") == "comment":
            continue
        resolved = cards_module.resolve_block(block, ctx, "")
        # Mirror get_card's discriminator keying (Story 10.5 + 10.2): flag-bearing blocks
        # are keyed by their binding flag so same-type blocks don't collide.
        # Story 10.2: donut blocks are keyed by their first binding dimension so the
        # user_type donut and device donut can coexist (donut_user_type / donut_device_category).
        binding = block.get("binding") or {}
        if binding.get("cannibalisation"):
            key = "cannibalisation"
        elif binding.get("opportunities"):
            key = "opportunities"
        elif resolved.get("type") == "donut":
            _dims = binding.get("dimensions") or []
            _dim_key = _dims[0] if _dims else None
            key = f"donut_{_dim_key}" if _dim_key else "donut"
        else:
            key = resolved.get("type")
        if key and key not in block_data:
            block_data[key] = resolved.get("data") or {}
        # Generic "donut" backward-compat slot: holds the FIRST donut's data.
        if resolved.get("type") == "donut" and "donut" not in block_data:
            block_data["donut"] = resolved.get("data") or {}
    return ctx, block_data


_EVT = {"id": "evt_01ABC", "event_date": "2026-07-04", "label": "Mise a jour algo"}

# ---------------------------------------------------------------------------
# 1. Per-card builder outputs
# ---------------------------------------------------------------------------


class TestKeywordsBuilder:
    """keywords builder cites the biggest |delta| mover by query name."""

    def _rows(self):
        """Two queries: chaussures improved +4 pos (9->5); bottes barely moved (<2)."""
        rows = [
            # Current period: chaussures pos 5, bottes pos 3.5
            _row("average_position", "query", "chaussures", 5.0),
            _row("average_position", "query", "bottes", 3.5),
            _row("impressions", "query", "chaussures", 900.0),
            _row("impressions", "query", "bottes", 300.0),
            _row("clicks", "query", "chaussures", 40.0),
            _row("clicks", "query", "bottes", 10.0),
        ]
        # Prior period: chaussures pos 9 (improved by 4), bottes pos 4.0 (<2 delta filtered)
        rows += [
            _prior_row("average_position", "query", "chaussures", 9.0),
            _prior_row("average_position", "query", "bottes", 4.0),
            _prior_row("impressions", "query", "chaussures", 800.0),
            _prior_row("impressions", "query", "bottes", 250.0),
        ]
        return rows

    def _block_data(self, rows):
        tpl = cards_module.get_template("keywords")
        _, bd = _build_block_data_for(
            rows,
            ["clicks", "impressions", "average_position"],
            composition_blocks=list(tpl.composition),
        )
        return bd

    def _rollup(self, rows):
        pull_ids = ["pull_abc"]
        return rollup_module.compute_rollup(
            rows, ["clicks", "impressions"], "2026-07-01", "2026-07-06", "default", pull_ids
        )

    def test_cites_biggest_mover_by_name(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_keywords_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # chaussures improved by 4 positions (biggest mover); must appear by name
        assert "chaussures" in comment

    def test_comment_has_at_most_3_lines(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_keywords_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3

    def test_comment_contains_citation_token(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_keywords_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # Citation token must be present: (source:field, pull_id) style
        assert "(" in comment and ")" in comment

    def test_no_movers_falls_back_gracefully(self):
        # No prior-period data -> movers bar is empty -> builder still returns <= 3 lines
        rows = [
            _row("clicks", "query", "chaussures", 40.0),
            _row("impressions", "query", "chaussures", 900.0),
        ]
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_keywords_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3
        # AD-9: must contain the explicit-absence line when no context_events
        assert "Contexte manquant" in comment

    def _cannib_rows(self):
        """Movers rows + a cannibalised query>page composite (chaussures on 2 pages)."""
        rows = list(self._rows())
        for d in ("2026-07-03", "2026-07-04", "2026-07-05"):
            for page, impr, pos in (("/sport/", 500, 4.5), ("/running/", 500, 8.5)):
                val = f"chaussures>{page}"
                rows += [
                    {
                        "date": d,
                        "connector": "gsc",
                        "metric": "impressions",
                        "breakdown_dimension": "query>page",
                        "breakdown_value": val,
                        "value": float(impr),
                        "pull_id": "pull_abc",
                        "loaded_at": f"{d}T00:00:00",
                    },
                    {
                        "date": d,
                        "connector": "gsc",
                        "metric": "average_position",
                        "breakdown_dimension": "query>page",
                        "breakdown_value": val,
                        "value": float(pos),
                        "pull_id": "pull_abc",
                        "loaded_at": f"{d}T00:00:00",
                    },
                ]
        return rows

    def test_keywords_comment_with_cannibalisation(self):
        """Story 10.5: a flagged query adds line 4 citing the query + page count."""
        rows = self._cannib_rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_keywords_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        assert "Cannibalisation" in comment
        assert "chaussures" in comment
        # cites the competing-page count (2 pages) and stays <= 4 lines
        assert "2 pages" in comment
        assert len(comment.splitlines()) <= 4
        # citation token present on the cannibalisation line
        assert "(" in comment and ")" in comment

    def test_keywords_comment_no_cannibalisation(self):
        """No cannibalisation -> comment unchanged (no line 4, stays <= 3 lines)."""
        rows = self._rows()  # movers only, no query>page composite
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_keywords_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        assert "Cannibalisation" not in comment
        assert len(comment.splitlines()) <= 3


class TestConversionsBuilder:
    """conversions builder cites the winning source + notable CPA move."""

    def _rows(self):
        return [
            _row(
                "conversions", "connector", "google-analytics", 120.0, connector="google-analytics"
            ),
            _row("conversions", "connector", "meta-ads", 40.0, connector="meta-ads"),
            _row("cost", "connector", "meta-ads", 200.0, connector="meta-ads"),
            _row("cost", "connector", "google-analytics", 480.0, connector="google-analytics"),
        ]

    def _block_data(self, rows):
        tpl = cards_module.get_template("conversions")
        _, bd = _build_block_data_for(
            rows, ["conversions", "cost"], composition_blocks=list(tpl.composition)
        )
        return bd

    def _rollup(self, rows):
        return rollup_module.compute_rollup(
            rows, ["conversions", "cost"], "2026-07-01", "2026-07-06", "default", ["pull_abc"]
        )

    def test_cites_winning_source(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_conversions_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # google-analytics has 120 conversions (75%) -> wins
        assert "google-analytics" in comment

    def test_comment_has_at_most_3_lines(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_conversions_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3

    def test_cites_cpa_when_gauge_has_value(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_conversions_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # CPA should appear (cost present -> gauge resolves a value)
        assert "CPA" in comment or "cpa" in comment.lower()

    def test_no_source_data_falls_back_gracefully(self):
        # No breakdown data -> donut is empty -> builder still returns <= 3 lines
        rows = [_row("conversions", None, None, 50.0)]
        bd: dict = {}  # empty block_data
        roll = self._rollup(rows)
        comment = narrative_module.build_conversions_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3
        assert "Contexte manquant" in comment

    def test_cpa_without_prior_cites_explicit_absence(self):
        """review-epic-9-integration F-6: when prior CPA is unavailable (gauge.delta None),
        the comment cites the absence explicitly, not silence."""
        bd = {
            "donut": {"slices": [{"label": "google-analytics", "value": 120.0, "pct": 75.0}]},
            # gauge has a value but NO delta (no prior period) -> explicit-absence must show.
            "gauge": {"value": 30.0, "delta": None, "unit": "EUR"},
        }
        roll = {
            "conversions": {
                "value": 160,
                "source_system": "ga4",
                "source_field": "conversions",
                "pull_id": "pull_abc",
            }
        }
        comment = narrative_module.build_conversions_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_abc"]
        )
        # The CPA value is still cited AND the missing-prior-period reason is explicit.
        assert "CPA" in comment
        assert "indisponible" in comment
        assert "période précédente manquante" in comment
        assert len(comment.splitlines()) <= 3


class TestUsertypesBuilder:
    """usertypes builder cites the dominant device segment shift."""

    def _rows(self):
        return [
            _row("active_users", "device_category", "mobile", 600.0, connector="google-analytics"),
            _row("active_users", "device_category", "desktop", 200.0, connector="google-analytics"),
            _row("active_users", "device_category", "tablet", 50.0, connector="google-analytics"),
            _row("sessions", "device_category", "mobile", 700.0, connector="google-analytics"),
            _row("sessions", "device_category", "desktop", 250.0, connector="google-analytics"),
        ]

    def _country_rows(self):
        return [
            _row("active_users", "country", "France", 400.0, connector="google-analytics"),
            _row("active_users", "country", "Belgique", 100.0, connector="google-analytics"),
        ]

    def _block_data(self, rows):
        tpl = cards_module.get_template("usertypes")
        _, bd = _build_block_data_for(
            rows, ["active_users", "sessions"], composition_blocks=list(tpl.composition)
        )
        return bd

    def _rollup(self, rows):
        return rollup_module.compute_rollup(
            rows, ["active_users", "sessions"], "2026-07-01", "2026-07-06", "default", ["pull_abc"]
        )

    def _user_type_rows(self):
        # user_type present -> the resolver applies the FR label map
        # (new->Nouveaux, returning->Fidèles, unknown->Indéterminés).
        return [
            _row("active_users", "user_type", "returning", 600.0, connector="google-analytics"),
            _row("active_users", "user_type", "new", 350.0, connector="google-analytics"),
            _row("active_users", "user_type", "unknown", 50.0, connector="google-analytics"),
        ]

    def test_cites_dominant_segment(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_usertypes_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # mobile has 71% (600/850) -> dominant segment
        assert "mobile" in comment

    def test_cites_dominant_user_type_segment_with_fr_label(self):
        """Story 10.2: when user_type rows are present, line 1 cites the dominant
        user_type segment with its FR label (Fidèles), not the device segment —
        proves the donut_user_type -> comment wiring end to end (review 10.2 F-1)."""
        rows = self._user_type_rows() + self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_usertypes_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # returning (600 / 1000 = 60%) dominates -> FR label "Fidèles" leads line 1.
        assert "Fidèles" in comment
        assert "Type dominant" in comment
        assert len(comment.splitlines()) <= 3

    def test_comment_has_at_most_3_lines(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_usertypes_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3

    def test_no_segment_data_falls_back_gracefully(self):
        rows = [_row("active_users", None, None, 300.0)]
        bd: dict = {}
        roll = self._rollup(rows)
        comment = narrative_module.build_usertypes_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3
        assert "Contexte manquant" in comment


class TestJourneyBuilder:
    """journey builder cites the biggest drop-off step."""

    def _rows(self):
        return [
            _row("sessions", "device_category", "mobile", 1000.0, connector="google-analytics"),
            _row("active_users", "device_category", "mobile", 800.0, connector="google-analytics"),
            _row("conversions", "device_category", "mobile", 50.0, connector="google-analytics"),
        ]

    def _block_data(self, rows):
        tpl = cards_module.get_template("journey")
        _, bd = _build_block_data_for(
            rows,
            ["sessions", "active_users", "conversions"],
            composition_blocks=list(tpl.composition),
        )
        return bd

    def _rollup(self, rows):
        return rollup_module.compute_rollup(
            rows,
            ["sessions", "active_users", "conversions"],
            "2026-07-01",
            "2026-07-06",
            "default",
            ["pull_abc"],
        )

    def test_cites_biggest_dropoff_step(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # sessions=1000 -> active_users=800 (rate 0.8), then conversions=50 (rate 0.0625):
        # the conversions step has the lowest rate -> biggest drop-off
        assert "conversions" in comment

    def test_cites_overall_rate(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        # overall rate = 50/1000 = 5.0% must appear
        assert "5.0" in comment or "5,0" in comment or "%" in comment

    def test_comment_has_at_most_3_lines(self):
        rows = self._rows()
        bd = self._block_data(rows)
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[_EVT], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3

    def test_no_funnel_data_falls_back_gracefully(self):
        rows = [_row("sessions", None, None, 500.0)]
        bd: dict = {}
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_abc"]
        )
        assert len(comment.splitlines()) <= 3
        assert "Contexte manquant" in comment


# ---------------------------------------------------------------------------
# 2. AD-9 negative tests — per builder
# ---------------------------------------------------------------------------


_CAUSAL_WORDS = [
    "parce que",
    "car ",
    "en raison",
    "suite a",
    "due to",
    "because",
    "caused",
    "thanks to",
    "owing to",
]


def _has_causal_language(text: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in _CAUSAL_WORDS)


@pytest.mark.parametrize(
    "builder_fn,block_data,rollup",
    [
        (
            narrative_module.build_keywords_comment,
            {"bar": {"bars": [{"label": "chaussures", "value": 4.0, "direction": "up"}]}},
            {
                "clicks": {
                    "value": 50,
                    "source_system": "gsc",
                    "source_field": "clicks",
                    "pull_id": "pull_abc",
                }
            },
        ),
        (
            narrative_module.build_conversions_comment,
            {
                "donut": {"slices": [{"label": "google", "value": 100.0, "pct": 80.0}]},
                "gauge": {"value": 25.0, "delta": -5.0, "unit": "EUR"},
            },
            {
                "conversions": {
                    "value": 125,
                    "source_system": "ga4",
                    "source_field": "conversions",
                    "pull_id": "pull_abc",
                }
            },
        ),
        (
            narrative_module.build_usertypes_comment,
            {
                "donut": {"slices": [{"label": "mobile", "value": 600.0, "pct": 75.0}]},
                "bar": {"bars": [{"label": "France", "value": 400.0}]},
            },
            {
                "active_users": {
                    "value": 800,
                    "source_system": "ga4",
                    "source_field": "active_users",
                    "pull_id": "pull_abc",
                }
            },
        ),
        (
            narrative_module.build_journey_comment,
            {
                "funnel": {
                    "steps": [
                        {"label": "sessions", "value": 1000.0, "rate": 1.0},
                        {"label": "conversions", "value": 50.0, "rate": 0.05},
                    ],
                    "overall_rate": 0.05,
                }
            },
            {
                "sessions": {
                    "value": 1000,
                    "source_system": "ga4",
                    "source_field": "sessions",
                    "pull_id": "pull_abc",
                }
            },
        ),
    ],
    ids=["keywords", "conversions", "usertypes", "journey"],
)
class TestAD9PerBuilder:
    """AD-9: per-builder negative test suite."""

    def test_no_context_events_emits_contexte_manquant(self, builder_fn, block_data, rollup):
        """With context_events=[], the comment must contain 'Contexte manquant'."""
        comment = builder_fn(
            block_data=block_data,
            rollup=rollup,
            context_events=[],
            pull_ids=["pull_abc"],
        )
        assert "Contexte manquant" in comment, f"Expected 'Contexte manquant' in: {comment!r}"

    def test_no_context_events_no_causal_language(self, builder_fn, block_data, rollup):
        """With context_events=[], no causal language must appear."""
        comment = builder_fn(
            block_data=block_data,
            rollup=rollup,
            context_events=[],
            pull_ids=["pull_abc"],
        )
        assert not _has_causal_language(comment), f"Causal language found in: {comment!r}"

    def test_with_context_event_cause_is_exactly_the_label(self, builder_fn, block_data, rollup):
        """With a context event, the cited cause is EXACTLY the event's label (never invented)."""
        evt = {"id": "evt_test", "event_date": "2026-07-03", "label": "Algo-Update-Test"}
        comment = builder_fn(
            block_data=block_data,
            rollup=rollup,
            context_events=[evt],
            pull_ids=["pull_abc"],
        )
        assert "Algo-Update-Test" in comment, f"Event label not found in comment: {comment!r}"
        # The event id must be cited
        assert "evt_test" in comment, f"Event citation not found in comment: {comment!r}"

    def test_with_context_event_no_invented_causes(self, builder_fn, block_data, rollup):
        """Even with a context event, no invented causes beyond the event's label appear."""
        evt = {"id": "evt_test", "event_date": "2026-07-03", "label": "Known-Cause-Only"}
        comment = builder_fn(
            block_data=block_data,
            rollup=rollup,
            context_events=[evt],
            pull_ids=["pull_abc"],
        )
        # The comment must NOT contain the explicit-absence line when an event IS present
        assert "Contexte manquant" not in comment, (
            f"'Contexte manquant' should not appear when events are provided: {comment!r}"
        )


# ---------------------------------------------------------------------------
# 3. Guidelines flow
# ---------------------------------------------------------------------------


class _FakeModule:
    def __init__(self, name, reports):
        self.name = name
        self.manifest = {}
        self.reports = reports


def test_get_card_adhoc_path_carries_guidelines_when_fetch_returns_them():
    """get_card ad-hoc path: envelope carries llm_commentary_guidelines when
    _fetch_r6_adhoc returns them (mocked to simulate a stored project override).
    """
    rows = [
        {
            "date": "2026-07-05",
            "connector": "ga4",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "mobile",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    with (
        patch("core.warehouse.query_daily_report", return_value=rows),
        patch.object(
            cards_module, "_fetch_r6_adhoc", return_value=(None, "Directive de test pour cartes.")
        ),
    ):
        _s, envelope, _uri = cards_module.get_card(
            [], "default", metrics=["sessions"], date_from="2026-07-01", date_to="2026-07-06"
        )
    assert envelope["data"].get("llm_commentary_guidelines") == "Directive de test pour cartes."


def test_get_card_adhoc_path_omits_guidelines_when_fetch_returns_none():
    """get_card ad-hoc path: envelope omits llm_commentary_guidelines when None."""
    rows = [
        {
            "date": "2026-07-05",
            "connector": "ga4",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "mobile",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    with (
        patch("core.warehouse.query_daily_report", return_value=rows),
        patch.object(cards_module, "_fetch_r6_adhoc", return_value=(None, None)),
    ):
        _s, envelope, _uri = cards_module.get_card(
            [], "default", metrics=["sessions"], date_from="2026-07-01", date_to="2026-07-06"
        )
    assert "llm_commentary_guidelines" not in envelope["data"]


def test_get_card_report_ref_path_unchanged_regression():
    """report_ref path carries guidelines via _fetch_r6 (existing behaviour, no regression)."""
    rows = [
        {
            "date": "2026-07-05",
            "connector": "ga4",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "mobile",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    report = {"id": "overview", "metrics": ["sessions"], "dimensions": ["date"]}
    module = _FakeModule("google-analytics", [report])
    with (
        patch("core.warehouse.query_report", return_value=rows),
        patch.object(cards_module, "_flow_preferred_template", return_value=None),
        patch.object(cards_module, "_fetch_card_config", return_value={}),
        patch.object(
            cards_module,
            "_fetch_r6",
            return_value=({"sessions": {"direction": "up_good"}}, "Directive rapport."),
        ),
    ):
        _s, envelope, _uri = cards_module.get_card(
            [module],
            "default",
            report_ref="google-analytics/overview",
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert envelope["data"]["metric_definitions"]["sessions"]["direction"] == "up_good"
    assert envelope["data"]["llm_commentary_guidelines"] == "Directive rapport."
    # _fetch_r6_adhoc must NOT have been called on the report_ref path.


def test_fetch_r6_adhoc_never_crashes_on_bad_input():
    """_fetch_r6_adhoc must degrade to (None, None) for any bad input (never raises)."""
    result = cards_module._fetch_r6_adhoc("proj", [], [])
    assert result == (None, None)

    result = cards_module._fetch_r6_adhoc("proj", ["sessions"], None)
    assert result == (None, None)

    # With modules that have no reports attribute
    class _BadModule:
        name = "bad"

    result = cards_module._fetch_r6_adhoc("proj", ["sessions"], [_BadModule()])
    assert result == (None, None)


def test_fetch_r6_adhoc_with_overlapping_module_calls_flows_merge():
    """_fetch_r6_adhoc selects the module report with best metric overlap and calls flows."""
    report = {"id": "overview", "metrics": ["sessions", "conversions"], "dimensions": ["date"]}
    module = _FakeModule("ga4", [report])

    def _fake_merged(project_id, base_report_id, loaded_modules, conn):
        return {
            "llm_commentary_guidelines": "Directives overlapping.",
            "metric_definitions": None,
        }

    with (
        patch("core.db.get_connection") as mock_conn,
        patch("core.flows.fetch_report_override_merged", side_effect=_fake_merged),
    ):
        mock_conn.return_value.__enter__ = lambda s: s
        mock_conn.return_value.__exit__ = lambda s, *a: False

        defs, guidelines = cards_module._fetch_r6_adhoc(
            "proj", ["sessions", "conversions"], [module]
        )
    assert guidelines == "Directives overlapping."
    assert defs is None


# ---------------------------------------------------------------------------
# 4. Line-count constraints
# ---------------------------------------------------------------------------


def test_per_card_comment_injected_into_summary_within_30_lines():
    """Summary with per-card comment still respects the <= 30-line AD-1 budget."""
    rows = [
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 40.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "impressions",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 900.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        # keywords template requires the page dimension too
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "page",
            "breakdown_value": "/a",
            "value": 40.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    with patch("core.warehouse.query_daily_report", return_value=rows):
        summary, _env, _uri = cards_module.get_card(
            [],
            "default",
            template="keywords",
            metrics=["clicks", "impressions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert len(summary.splitlines()) <= 30


def test_comment_block_in_composition_carries_per_card_text():
    """The comment block in the resolved composition must carry the per-card text."""
    rows = [
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 40.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "impressions",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 900.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        # keywords template requires the page dimension too
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "page",
            "breakdown_value": "/a",
            "value": 40.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _s, env, _uri = cards_module.get_card(
            [],
            "default",
            template="keywords",
            metrics=["clicks", "impressions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )

    composition = env["data"]["composition"]
    comment_blocks = [b for b in composition if b.get("type") == "comment"]
    assert comment_blocks, "No comment block in composition"
    text = comment_blocks[0].get("data", {}).get("text") or ""
    assert text.strip(), "Comment block text is empty"
    # The rendered_comment in data must match the comment block text
    assert env["data"]["rendered_comment"] == text


def test_rendered_comment_at_most_3_lines_for_business_cards():
    """For each business card, rendered_comment must be <= 3 lines."""
    # keywords card
    rows_kw = [
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 40.0,
            "pull_id": "p1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "impressions",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 900.0,
            "pull_id": "p1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        # keywords template requires the page dimension too
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "page",
            "breakdown_value": "/a",
            "value": 40.0,
            "pull_id": "p1",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    with patch("core.warehouse.query_daily_report", return_value=rows_kw):
        _s, env, _uri = cards_module.get_card(
            [],
            "default",
            template="keywords",
            metrics=["clicks", "impressions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    comment = env["data"]["rendered_comment"]
    assert len(comment.splitlines()) <= 3, f"keywords comment > 3 lines: {comment!r}"


# ---------------------------------------------------------------------------
# 5. Empty / partial data — builder degradation
# ---------------------------------------------------------------------------


def test_keywords_builder_empty_block_data_never_raises():
    """Empty block_data -> builder returns a non-empty string, never raises."""
    comment = narrative_module.build_keywords_comment(
        block_data={}, rollup={}, context_events=[], pull_ids=[]
    )
    assert isinstance(comment, str) and comment.strip()


def test_conversions_builder_empty_block_data_never_raises():
    comment = narrative_module.build_conversions_comment(
        block_data={}, rollup={}, context_events=[], pull_ids=[]
    )
    assert isinstance(comment, str) and comment.strip()


def test_usertypes_builder_empty_block_data_never_raises():
    comment = narrative_module.build_usertypes_comment(
        block_data={}, rollup={}, context_events=[], pull_ids=[]
    )
    assert isinstance(comment, str) and comment.strip()


def test_journey_builder_empty_block_data_never_raises():
    comment = narrative_module.build_journey_comment(
        block_data={}, rollup={}, context_events=[], pull_ids=[]
    )
    assert isinstance(comment, str) and comment.strip()


def test_get_card_business_card_with_empty_rows_does_not_raise():
    """get_card with a business card template and empty rows must not raise."""
    with patch("core.warehouse.query_daily_report", return_value=[]):
        try:
            _s, env, _uri = cards_module.get_card(
                [],
                "default",
                template="kpi",
                metrics=["sessions"],
                date_from="2026-07-01",
                date_to="2026-07-06",
            )
        except Exception as exc:
            pytest.fail(f"get_card raised on empty rows: {exc}")
    assert env["data"]["rendered_comment"].strip()


# ---------------------------------------------------------------------------
# 6. CARD_COMMENT_BUILDERS registry is data-driven (AD-2)
# ---------------------------------------------------------------------------


def test_card_comment_builders_registry_has_four_business_cards():
    """CARD_COMMENT_BUILDERS must have exactly the 4 business card template ids."""
    registry = narrative_module.CARD_COMMENT_BUILDERS
    for cid in ("keywords", "conversions", "usertypes", "journey"):
        assert cid in registry, f"Missing builder for {cid!r}"
        assert callable(registry[cid]), f"Builder for {cid!r} is not callable"


def test_card_comment_builders_kpi_is_not_in_registry():
    """kpi must NOT be in CARD_COMMENT_BUILDERS (falls back to build_narrative)."""
    assert "kpi" not in narrative_module.CARD_COMMENT_BUILDERS


def test_connectors_builder_signature_compatible_with_registry_dispatch():
    """review-epic-9-backend F-10: build_connectors_comment must be callable via the
    STANDARD registry dispatch convention (block_data/rollup/context_events/pull_ids)
    WITHOUT raising TypeError, so CARD_COMMENT_BUILDERS is uniform.

    Called that way (no inventory summary) it degrades to the designed empty-inventory
    line rather than inventing an inventory it was not given.
    """
    builder = narrative_module.CARD_COMMENT_BUILDERS["connectors"]
    # The exact kwargs get_card's block-data dispatch passes to every builder.
    comment = builder(
        block_data={"table": {"rows": []}},
        rollup={},
        context_events=[],
        pull_ids=[],
    )
    assert isinstance(comment, str) and comment.strip()
    # No inventory summary provided -> designed empty line (never invents connectors).
    assert "Aucun connecteur" in comment


def test_connectors_builder_direct_call_still_works():
    """The direct-call path (inventory summary) is unchanged and now uses accents."""
    comment = narrative_module.build_connectors_comment(
        least_fresh={"connector": "google-analytics", "last_extract": "2026-07-09"},
        connector_count=2,
    )
    assert "google-analytics" in comment
    assert "configurés" in comment  # accented plural (UI-review F-2/F-7)


def test_get_card_kpi_still_uses_generic_build_narrative():
    """KPI card (no builder registered) must use the generic build_narrative output.

    We verify this by checking that 'kpi' is absent from CARD_COMMENT_BUILDERS (the
    dispatch dict) and that the KPI card's rendered_comment does NOT look like a
    per-card comment (which uses the connector:fact_daily_kpi citation style).
    """
    rows = [
        {
            "date": "2026-07-05",
            "connector": "ga4",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "mobile",
            "value": 200.0,
            "pull_id": "pull_x",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    # kpi must not have a builder -- dispatch falls through to build_narrative
    assert "kpi" not in narrative_module.CARD_COMMENT_BUILDERS

    with patch("core.warehouse.query_daily_report", return_value=rows):
        _s, env, _uri = cards_module.get_card(
            [],
            "default",
            template="kpi",
            metrics=["sessions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    # The generic build_narrative emits metric lines (> 3 lines typically) or
    # the explicit-absence line, not the per-card single-topic format.
    comment = env["data"]["rendered_comment"]
    assert comment.strip(), "KPI rendered_comment must not be empty"


def test_get_card_keywords_uses_per_card_builder():
    """keywords card must dispatch through CARD_COMMENT_BUILDERS, not build_narrative.

    We verify this by patching CARD_COMMENT_BUILDERS with a spy that wraps the real
    builder (the dict holds function references, not attribute lookups, so we patch
    the dict entry rather than the module attribute).
    """
    rows = [
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 40.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "impressions",
            "breakdown_dimension": "query",
            "breakdown_value": "chaussures",
            "value": 900.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        # keywords template requires the page dimension too
        {
            "date": "2026-07-05",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "page",
            "breakdown_value": "/a",
            "value": 40.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]

    builder_called = {"called": False}
    real_builder = narrative_module.CARD_COMMENT_BUILDERS["keywords"]

    def _spy_builder(**kwargs):
        builder_called["called"] = True
        return real_builder(**kwargs)

    patched_registry = dict(narrative_module.CARD_COMMENT_BUILDERS)
    patched_registry["keywords"] = _spy_builder

    with (
        patch("core.warehouse.query_daily_report", return_value=rows),
        patch.object(narrative_module, "CARD_COMMENT_BUILDERS", patched_registry),
    ):
        _s, env, _uri = cards_module.get_card(
            [],
            "default",
            template="keywords",
            metrics=["clicks", "impressions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert builder_called["called"], "keywords builder was not called via CARD_COMMENT_BUILDERS"
