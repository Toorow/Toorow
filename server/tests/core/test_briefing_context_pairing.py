"""Story 53.8 / CAV-15 — a context event is attached only when it is scoped to the claim.

Both sides of the class are covered here, because repairing one side is exactly
how this defect survived:

  * ``core.briefing._find_context_event``      — the briefing-side pairing;
  * ``core.anomaly_alerts._fetch_context_events_for_anomaly`` — the evaluator-side one.

The ``build_briefing``-level cases here pass firings carrying ``type`` because
they exercise the PAIRING, not the classifier. The producer-derived test that
proves the classifier reads a key its producers actually emit (AC3) lives in
``test_briefing_claim_grammar.py``.

The claim's own date is the anomaly's ``window_date``. The briefing date is NOT
a pairing key: attaching the nearest-in-time event to every claim of the day
asserts a relationship nobody checked (proactive-assertions.md:88-92).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core import anomaly_alerts
from core.briefing import (
    CONTEXT_BASIS_CLAIM_WINDOW,
    CONTEXT_BASIS_EXACT_DATE,
    CONTEXT_BASIS_METRIC,
    CONTEXT_BASIS_PLATFORM,
    CONTEXT_DIM_CONNECTOR,
    CONTEXT_DIM_METRIC,
    _find_context_event,
    build_briefing,
    context_events_in_claim_scope,
)

# ---------------------------------------------------------------------------
# AC4 — the pairing uses the anomaly's own window_date, never briefing_date
# ---------------------------------------------------------------------------


def test_event_three_days_before_the_claim_is_not_attached():
    """An anomaly on D with an event on D-3: adjacency is not a relationship."""
    events = [
        {"id": "evt_A", "event_date": "2026-07-09", "label": "Campaign launch"},
    ]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics")
    assert evt is None, f"Expected no event for a D-3 event, got {evt!r}"
    assert pairing is None


def test_event_on_the_claim_date_is_attached():
    events = [
        {"id": "evt_A", "event_date": "2026-07-12", "label": "Campaign launch"},
    ]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics")
    assert evt is not None and evt["id"] == "evt_A"
    assert CONTEXT_BASIS_EXACT_DATE in pairing["basis"]


def test_pairing_uses_the_claim_date_not_the_briefing_date():
    """The briefing runs on D+1; the anomaly window is D. The event on D wins."""
    events = [
        {"id": "evt_briefing_day", "event_date": "2026-07-13", "label": "Other"},
        {"id": "evt_claim_day", "event_date": "2026-07-12", "label": "Campaign launch"},
    ]
    firings = [
        {
            "type": "anomaly",
            "metric": "sessions",
            "connector": "google-analytics",
            "observed_value": 12000.0,
            "expected_value": 3000.0,
            "window_date": "2026-07-12",
            "firing_id": "fire_1",
            "pull_ids": [],
        }
    ]
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-13",
        alert_firings=firings,
        rollup={},
        context_events=events,
        nightly_run_id=None,
    )
    anomalies = [i for i in result["insights"] if i["type"] == "anomaly"]
    assert anomalies, f"Expected an anomaly insight, got {result['insights']!r}"
    assert anomalies[0]["context_event_id"] == "evt_claim_day"


def test_no_window_date_means_no_context_event():
    """A claim that does not carry its own date cannot be scoped -- so nothing attaches."""
    events = [{"id": "evt_A", "event_date": "2026-07-13", "label": "Campaign launch"}]
    firings = [
        {
            "type": "anomaly",
            "metric": "sessions",
            "connector": "google-analytics",
            "observed_value": 12000.0,
            "expected_value": 3000.0,
            "firing_id": "fire_1",
            "pull_ids": [],
        }
    ]
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-13",
        alert_firings=firings,
        rollup={},
        context_events=events,
        nightly_run_id=None,
    )
    anomalies = [i for i in result["insights"] if i["type"] == "anomaly"]
    assert anomalies[0]["context_event_id"] is None
    assert anomalies[0]["context_event_pairing"] is None


# ---------------------------------------------------------------------------
# AC4 — a declared platform that contradicts the claim's connector disqualifies
# ---------------------------------------------------------------------------


def test_declared_platform_mismatch_disqualifies_the_event():
    events = [
        {
            "id": "evt_meta",
            "event_date": "2026-07-12",
            "label": "Meta creative swap",
            "platform": "meta-ads",
        },
    ]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics")
    assert evt is None, "An event declaring another platform must not attach"
    assert pairing is None


def test_declared_platform_match_is_recorded_in_the_basis():
    events = [
        {
            "id": "evt_ga",
            "event_date": "2026-07-12",
            "label": "Tag release",
            "platform": "google-analytics",
        },
    ]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics")
    assert evt["id"] == "evt_ga"
    assert CONTEXT_BASIS_PLATFORM in pairing["basis"]
    assert CONTEXT_DIM_CONNECTOR not in pairing["unscoped_dimensions"]


def test_platform_match_is_preferred_over_an_unscoped_event():
    events = [
        {"id": "evt_none", "event_date": "2026-07-12", "label": "Unscoped"},
        {
            "id": "evt_ga",
            "event_date": "2026-07-12",
            "label": "Tag release",
            "platform": "google-analytics",
        },
    ]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics")
    assert evt["id"] == "evt_ga"


# ---------------------------------------------------------------------------
# AC5 — the pairing basis is stated, and what it cannot scope is stated
# ---------------------------------------------------------------------------


def test_metric_is_declared_unscoped_when_the_event_names_none():
    """An event about EVERY metric is admissible -- and nothing about it was compared.

    Migration 322 gave `app.context_events` a nullable `metric`, so this is no
    longer "always". What survives the change is the half that matters: an event
    naming no metric is not rejected, and the payload does not pretend the
    dimension was checked for it.
    """
    events = [{"id": "evt_A", "event_date": "2026-07-12", "label": "Campaign launch"}]
    _evt, pairing = _find_context_event(
        events, "2026-07-12", "google-analytics", "sessions"
    )
    assert CONTEXT_DIM_METRIC in pairing["unscoped_dimensions"]
    assert CONTEXT_BASIS_METRIC not in pairing["basis"]


# ---------------------------------------------------------------------------
# Migration 322 -- the third dimension of the criterion, on both scoped walks.
#
# `proactive-assertions.md` is incomplete if "a context event is attached to a
# claim without being scoped to that claim's metric, connector and date". Date
# and connector were scoped on 2026-07-31 (story 53.8); `metric` could not be,
# because no column carried it. It does now.
# ---------------------------------------------------------------------------


def test_an_event_about_another_metric_is_not_attached():
    """The whole point: a bid change is not the context of an impressions claim."""
    events = [
        {
            "id": "evt_cost",
            "event_date": "2026-07-12",
            "label": "Bid raised",
            "metric": "cost",
        }
    ]
    evt, pairing = _find_context_event(
        events, "2026-07-12", "google-analytics", "impressions"
    )
    assert evt is None, f"An event about another metric must not attach, got {evt!r}"
    assert pairing is None


def test_an_event_about_this_metric_takes_metric_out_of_unscoped():
    events = [
        {
            "id": "evt_cost",
            "event_date": "2026-07-12",
            "label": "Bid raised",
            "metric": "cost",
        }
    ]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics", "cost")
    assert evt is not None and evt["id"] == "evt_cost"
    assert CONTEXT_BASIS_METRIC in pairing["basis"]
    assert CONTEXT_DIM_METRIC not in pairing["unscoped_dimensions"]


def test_a_claim_naming_no_metric_cannot_check_the_dimension_either():
    """A claim with no metric of its own scopes nothing, and says so.

    The comparison needs BOTH sides. Reporting the dimension as checked because
    the EVENT named one would claim an examination that never had a second term.
    """
    events = [
        {
            "id": "evt_cost",
            "event_date": "2026-07-12",
            "label": "Bid raised",
            "metric": "cost",
        }
    ]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics", None)
    assert evt is not None, "An unverifiable dimension does not disqualify"
    assert CONTEXT_DIM_METRIC in pairing["unscoped_dimensions"]


def test_the_metric_comparison_ignores_case_like_the_connector_one():
    events = [
        {"id": "evt_cost", "event_date": "2026-07-12", "label": "Bid", "metric": "Cost"}
    ]
    evt, _pairing = _find_context_event(events, "2026-07-12", None, "cost")
    assert evt is not None and evt["id"] == "evt_cost"


def test_null_platform_does_not_silently_qualify_the_connector():
    events = [{"id": "evt_A", "event_date": "2026-07-12", "label": "Campaign launch"}]
    evt, pairing = _find_context_event(events, "2026-07-12", "google-analytics")
    assert evt is not None, "A null platform does not disqualify -- it is unverifiable"
    assert CONTEXT_DIM_CONNECTOR in pairing["unscoped_dimensions"]
    assert CONTEXT_BASIS_PLATFORM not in pairing["basis"]


def test_marker_is_a_field_on_every_attached_insight():
    events = [{"id": "evt_A", "event_date": "2026-07-12", "label": "Campaign launch"}]
    firings = [
        {
            "type": "anomaly",
            "metric": "sessions",
            "connector": "google-analytics",
            "observed_value": 12000.0,
            "expected_value": 3000.0,
            "window_date": "2026-07-12",
            "firing_id": "fire_1",
            "pull_ids": [],
        }
    ]
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-13",
        alert_firings=firings,
        rollup={},
        context_events=events,
        nightly_run_id=None,
    )
    anomaly = [i for i in result["insights"] if i["type"] == "anomaly"][0]
    pairing = anomaly["context_event_pairing"]
    assert pairing is not None
    assert pairing["basis"] == [CONTEXT_BASIS_EXACT_DATE]
    assert CONTEXT_DIM_METRIC in pairing["unscoped_dimensions"]
    assert pairing["claim_date"] == "2026-07-12"


# ---------------------------------------------------------------------------
# AC4 — the OTHER side of the class: anomaly_alerts._fetch_context_events_for_anomaly
# ---------------------------------------------------------------------------


def _duck_conn(rows, raise_on_platform=False, has_metric_column=False):
    conn = MagicMock()
    seen: list[tuple] = []

    def _execute(sql, params=None):
        seen.append((sql, params))
        if raise_on_platform and "platform" in sql:
            raise RuntimeError("Binder Error: column platform does not exist")
        res = MagicMock()
        # The column probes of migrations 286 and 322 read the catalogue before
        # the fetch, so the fixture answers them the way a real mirror would.
        if "information_schema.columns" in sql:
            answers = has_metric_column if "'metric'" in sql else True
            res.fetchall = MagicMock(return_value=[(1,)] if answers else [])
            return res
        res.fetchall = MagicMock(return_value=rows)
        return res

    conn.execute = MagicMock(side_effect=_execute)
    conn._seen = seen
    return conn


def test_evaluator_side_scopes_on_the_connector_too():
    conn = _duck_conn([("Campaign launch",)])
    labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE", "2026-07-12", conn, connector="google-analytics"
    )
    assert labels == ["Campaign launch"]
    # The FETCH, not the first statement: migration 286 made this path probe the
    # mirror for `retired_at` before reading, so an index into the call list would
    # be asserting on which question came first rather than on what was asked.
    sql, params = next(one for one in conn._seen if "mirror.context_events" in one[0])
    assert "platform" in sql, "The evaluator side must scope on the connector, not date alone"
    assert "google-analytics" in params
    assert CONTEXT_BASIS_PLATFORM in pairing["basis"]


def test_evaluator_side_degrades_when_the_mirror_has_no_platform_column():
    """Pre-055 mirror: the query falls back and the payload says the dimension is unscoped."""
    conn = _duck_conn([("Campaign launch",)], raise_on_platform=True)
    labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE", "2026-07-12", conn, connector="google-analytics"
    )
    assert labels == ["Campaign launch"]
    assert CONTEXT_DIM_CONNECTOR in pairing["unscoped_dimensions"]
    assert CONTEXT_BASIS_PLATFORM not in pairing["basis"]


def test_evaluator_side_never_raises():
    conn = MagicMock()
    conn.execute = MagicMock(side_effect=RuntimeError("catalog gone"))
    labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE", "2026-07-12", conn, connector="google-analytics"
    )
    assert labels == []
    assert pairing["unscoped_dimensions"]


def test_evaluator_side_declares_metric_unscoped_when_the_claim_names_none():
    conn = _duck_conn([("Campaign launch",)])
    _labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE", "2026-07-12", conn, connector="google-analytics"
    )
    assert CONTEXT_DIM_METRIC in pairing["unscoped_dimensions"]


def test_evaluator_side_scopes_on_the_metric_too():
    """Migration 322 -- the SQL carries the third discriminant, or says it did not."""
    conn = _duck_conn([("Campaign launch",)], has_metric_column=True)
    labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE", "2026-07-12", conn, connector="google-analytics", metric="cost"
    )
    assert labels == ["Campaign launch"]
    sql, params = next(one for one in conn._seen if "mirror.context_events" in one[0])
    # An event that declares ANOTHER metric is out; one that declares none stays,
    # exactly as the connector discriminant reads.
    assert "metric IS NULL OR metric = ?" in sql
    assert "cost" in params
    assert CONTEXT_BASIS_METRIC in pairing["basis"]
    assert CONTEXT_DIM_METRIC not in pairing["unscoped_dimensions"]


def test_evaluator_side_degrades_when_the_mirror_has_no_metric_column():
    """Pre-322 mirror: the clause is not emitted, and the payload does not claim it.

    Decided BEFORE the read, never by catching a BinderException: an
    exception-driven guard falls back to a query with no metric discriminant on
    precisely the mirrors that cannot apply one.
    """
    conn = _duck_conn([("Campaign launch",)], has_metric_column=False)
    labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE", "2026-07-12", conn, connector="google-analytics", metric="cost"
    )
    assert labels == ["Campaign launch"]
    sql, _params = next(one for one in conn._seen if "mirror.context_events" in one[0])
    assert "metric IS NULL" not in sql
    assert CONTEXT_DIM_METRIC in pairing["unscoped_dimensions"]
    assert CONTEXT_BASIS_METRIC not in pairing["basis"]


def test_the_metric_clause_survives_a_missing_platform_column():
    """Losing one comparable dimension because another is not would widen for nothing."""
    conn = _duck_conn(
        [("Campaign launch",)], raise_on_platform=True, has_metric_column=True
    )
    _labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE", "2026-07-12", conn, connector="google-analytics", metric="cost"
    )
    fallback = [
        one for one in conn._seen
        if "mirror.context_events" in one[0] and "platform" not in one[0]
    ][-1]
    assert "metric IS NULL OR metric = ?" in fallback[0]
    assert CONTEXT_DIM_CONNECTOR in pairing["unscoped_dimensions"]
    assert CONTEXT_BASIS_METRIC in pairing["basis"]


def test_the_evaluator_hands_each_anomaly_its_own_metric():
    """A per-claim discriminant that the caller never passes scopes nothing.

    `_fetch_context_events_for_anomaly` cannot know the claim's metric; the row
    it is being computed for does. This holds the wire between them.
    """
    import inspect

    source = inspect.getsource(anomaly_alerts.evaluate_anomalies)
    assert "metric=metric," in source, (
        "the anomaly loop must pass its row's own metric to the context fetch"
    )


# ---------------------------------------------------------------------------
# AI-169 -- l'evenement metier et la mesure ne partagent PAS la meme frontiere
#
# `capabilities/reporting-timezone.md` : incomplet si « business events and measures align
# on different undisclosed boundaries ». Cet appariement est exactement l'endroit ou un
# evenement metier rencontre une mesure, et il les aligne par EGALITE DE CHAINE sur une
# date calendaire :
#
#   * `app.context_events` valide un `YYYY-MM-DD` nu et ne stocke AUCUNE colonne de fuseau ;
#   * la date de la mesure est tiree sur l'horloge de la SOURCE, que la plateforme observe
#     et enregistre desormais par run (AI-161).
#
# « Meme jour » veut donc dire « meme chaine », et les deux jours peuvent commencer a des
# heures d'ecart. Ce n'est pas un defaut a corriger -- au grain DATE il n'y a pas de
# sous-journee a re-decouper, et un evenement saisi par un humain n'a pas d'horloge a
# recuperer. C'est un fait a DIRE.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Migration 322 -- the WINDOW claim: `narrative._why_lines`, fed by
# `reporting_mcp.get_daily_report`. The third proactive path, and the one that
# received every annotation of the window with no scope at all.
# ---------------------------------------------------------------------------


def test_the_window_scope_drops_an_event_about_another_metric():
    events = [
        {"id": "evt_cost", "event_date": "2026-07-12", "label": "Bid", "metric": "cost"},
        {"id": "evt_wide", "event_date": "2026-07-12", "label": "Outage"},
    ]
    kept, scope = context_events_in_claim_scope(
        events, start="2026-07-10", end="2026-07-15", metrics=["sessions"], connectors=[]
    )
    assert [e["id"] for e in kept] == ["evt_wide"]
    assert CONTEXT_BASIS_CLAIM_WINDOW in scope["basis"]
    # The one kept event named no metric, so the dimension went unchecked.
    assert CONTEXT_DIM_METRIC in scope["unscoped_dimensions"]


def test_the_window_scope_drops_an_event_outside_the_window():
    events = [{"id": "evt_old", "event_date": "2026-07-01", "label": "Old"}]
    kept, _scope = context_events_in_claim_scope(
        events, start="2026-07-10", end="2026-07-15", metrics=["cost"], connectors=[]
    )
    assert kept == []


def test_the_window_scope_drops_an_event_of_another_connector():
    events = [
        {
            "id": "evt_meta",
            "event_date": "2026-07-12",
            "label": "Meta push",
            "platform": "meta-ads",
        }
    ]
    kept, scope = context_events_in_claim_scope(
        events,
        start="2026-07-10",
        end="2026-07-15",
        metrics=["cost"],
        connectors=["google-analytics"],
    )
    assert kept == []
    assert CONTEXT_DIM_CONNECTOR in scope["unscoped_dimensions"]


def test_the_window_scope_reports_a_dimension_as_checked_only_when_every_kept_event_named_it():
    events = [
        {
            "id": "evt_cost",
            "event_date": "2026-07-12",
            "label": "Bid",
            "metric": "cost",
            "platform": "google-ads",
        },
        {"id": "evt_wide", "event_date": "2026-07-13", "label": "Outage"},
    ]
    both, scope = context_events_in_claim_scope(
        events,
        start="2026-07-10",
        end="2026-07-15",
        metrics=["cost"],
        connectors=["google-ads"],
    )
    assert [e["id"] for e in both] == ["evt_cost", "evt_wide"]
    # `evt_wide` named neither, so neither dimension is claimed as compared.
    assert CONTEXT_DIM_METRIC in scope["unscoped_dimensions"]
    assert CONTEXT_DIM_CONNECTOR in scope["unscoped_dimensions"]

    narrowed, scope = context_events_in_claim_scope(
        [events[0]],
        start="2026-07-10",
        end="2026-07-15",
        metrics=["cost"],
        connectors=["google-ads"],
    )
    assert [e["id"] for e in narrowed] == ["evt_cost"]
    assert scope["basis"] == [
        CONTEXT_BASIS_CLAIM_WINDOW,
        CONTEXT_BASIS_METRIC,
        CONTEXT_BASIS_PLATFORM,
    ]


def test_a_claim_that_names_no_dimension_scopes_nothing_and_says_so():
    events = [{"id": "evt_wide", "event_date": "2026-07-12", "label": "Outage"}]
    kept, scope = context_events_in_claim_scope(
        events, start="2026-07-10", end="2026-07-15", metrics=[], connectors=[]
    )
    assert [e["id"] for e in kept] == ["evt_wide"]
    assert CONTEXT_DIM_METRIC in scope["unscoped_dimensions"]
    assert CONTEXT_DIM_CONNECTOR in scope["unscoped_dimensions"]


def test_the_day_boundary_is_declared_unscoped_because_it_can_never_be_checked():
    from core.briefing import CONTEXT_DIM_DAY_BOUNDARY, context_pairing_descriptor

    pairing = context_pairing_descriptor(platform_checked=True, claim_date="2026-08-04")
    assert CONTEXT_DIM_DAY_BOUNDARY in pairing["unscoped_dimensions"]


def test_it_stays_unscoped_even_when_every_other_dimension_was_checked():
    """Toujours, comme `metric` : aucune colonne ne le supporte d'aucun cote.

    Un conditionnel laisserait croire qu'il est parfois verifie.
    """
    from core.briefing import CONTEXT_DIM_DAY_BOUNDARY, context_pairing_descriptor

    for platform_checked in (True, False):
        pairing = context_pairing_descriptor(
            platform_checked=platform_checked, claim_date="2026-08-04"
        )
        assert CONTEXT_DIM_DAY_BOUNDARY in pairing["unscoped_dimensions"]
        # Et il n'entre JAMAIS dans la base : la base dit ce qui a ete compare.
        assert CONTEXT_DIM_DAY_BOUNDARY not in pairing["basis"]


def test_the_disclosure_reaches_the_text_channel():
    """Une cle que rien n'affiche n'informe personne -- la lecon d'AI-158 et d'AI-132."""
    from core.briefing import CONTEXT_DIM_DAY_BOUNDARY, context_pairing_descriptor
    from core.candidate_emission import cited_fate_line

    pairing = context_pairing_descriptor(platform_checked=True, claim_date="2026-08-04")
    line = cited_fate_line(subject="Business context pairing", descriptor=pairing)
    assert CONTEXT_DIM_DAY_BOUNDARY in line
    assert "Not scoped" in line
