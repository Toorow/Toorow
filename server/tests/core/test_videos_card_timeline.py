"""La timeline des vues porte les dates de sortie -- les deux moitiés se rencontrent.

Elles vivaient déjà toutes les deux dans le produit, posées par le MÊME
connecteur, et ne s'étaient jamais rencontrées :

  * les vues au grain vidéo sont dans `fact_daily_kpi`
    (`breakdown_dimension='video'`), posées par le profil `video_daily` ;
  * les SORTIES sont des faits datés de `app.context_events`
    (`type='video_upload'`), posées par le profil `video_upload`, sur une porte
    différente.

Mesuré sur le projet de référence le 2026-08-19, données réelles : 547 vidéos,
34 640 lignes brutes, et quatre sorties dans la fenêtre -- dont les quatre
vidéos les plus vues de la période. Le jour d'une sortie porte 1 058 à 1 386
vues quand un jour sans sortie en porte 200 à 480. C'est exactement ce qu'aucune
des deux moitiés ne disait seule.

CE QUE CHAQUE TEST TIENT :

  * un repère est un fait DATÉ, pas une mesure : il a une date et un titre, et
    jamais de valeur ;
  * il est NOMMÉ, jamais identifié -- `b6wfcAYukFE` est ce dont la jointure a
    besoin, jamais ce qu'une personne lit ;
  * les types acceptés sont BOUND, jamais devinés ;
  * et l'absence se DIT : « rien n'est sorti » et « les sorties tombent hors de
    l'axe » sont deux réponses différentes, et une liste vide muette n'est ni
    l'une ni l'autre.
"""

from __future__ import annotations

import importlib

import pytest

cards = importlib.import_module("core.cards")


def _ctx(events, points_start="2026-07-13", days=5):
    """A block context whose current rows draw a `days`-long views axis."""
    from datetime import date, timedelta

    start = date.fromisoformat(points_start)
    rows = [
        {
            "date": (start + timedelta(days=offset)).isoformat(),
            "connector": "youtube-analytics",
            "metric": "views",
            "breakdown_dimension": "video",
            "breakdown_value": "vid_A",
            "value": 100.0 + offset,
            "pull_id": "pull_01",
        }
        for offset in range(days)
    ]
    end = (start + timedelta(days=days - 1)).isoformat()
    return cards._BlockContext(
        current_rows=rows,
        prior_rows=[],
        rollup={},
        metric_definitions=None,
        resolved_metrics=["views"],
        start=points_start,
        end=end,
        context_events=events,
    )


def _event(day, label, kind="video_upload"):
    return {"event_date": day, "type": kind, "label": label}


BLOCK = {"type": "line", "binding": {"metrics": "views", "events": "video_upload"}}


def test_a_release_lands_on_the_axis_with_its_title():
    ctx = _ctx([_event("2026-07-15", "Pique-nique vegan")])

    payload = cards._resolve_line(BLOCK, ctx)

    assert payload["markers"] == [{"index": "2026-07-15", "label": "Pique-nique vegan"}]
    assert payload["markers_reason"] is None


def test_a_marker_carries_no_value_because_a_release_is_not_a_measure():
    ctx = _ctx([_event("2026-07-15", "Pique-nique vegan")])

    marker = cards._resolve_line(BLOCK, ctx)["markers"][0]

    assert set(marker) == {"index", "label"}, "un repère a pris une valeur"


def test_the_reader_gets_the_title_never_the_identifier():
    """`b6wfcAYukFE` est ce dont la jointure a besoin, pas ce qu'on lit."""
    ctx = _ctx([{**_event("2026-07-15", "Ma recette de kimbap"), "entity_key": "kp_xUwbBtHM"}])

    marker = cards._resolve_line(BLOCK, ctx)["markers"][0]

    assert marker["label"] == "Ma recette de kimbap"
    assert "kp_xUwbBtHM" not in marker["label"]


def test_only_the_bound_event_types_reach_the_axis():
    """Une sortie veut dire quelque chose sur une timeline de contenu, rien sur une de dépense."""
    ctx = _ctx(
        [
            _event("2026-07-14", "Sortie", kind="video_upload"),
            _event("2026-07-15", "Hausse du budget", kind="budget_change"),
        ]
    )

    markers = cards._resolve_line(BLOCK, ctx)["markers"]

    assert [m["label"] for m in markers] == ["Sortie"]


def test_a_block_that_binds_no_event_gets_no_marker_key_at_all():
    """Une courbe ordinaire ne gagne pas un champ vide au passage."""
    ctx = _ctx([_event("2026-07-15", "Sortie")])

    payload = cards._resolve_line({"type": "line", "binding": {"metrics": "views"}}, ctx)

    assert "markers" not in payload
    assert "markers_reason" not in payload


def test_nothing_published_says_so_rather_than_showing_an_empty_list():
    ctx = _ctx([])

    payload = cards._resolve_line(BLOCK, ctx)

    assert payload["markers"] == []
    assert payload["markers_reason"]["code"] == "no_dated_event_in_window"


def test_a_release_off_the_axis_is_a_different_answer_from_no_release():
    """Les deux se ressemblent à l'oeil ; une seule est au sujet du contenu."""
    ctx = _ctx([_event("2025-01-01", "Une vieille sortie")])

    payload = cards._resolve_line(BLOCK, ctx)

    assert payload["markers"] == []
    assert payload["markers_reason"]["code"] == "dated_event_outside_axis"
    assert payload["markers_reason"]["code"] != "no_dated_event_in_window"


def test_markers_are_ordered_by_date_whatever_the_store_returned():
    ctx = _ctx(
        [_event("2026-07-16", "Deuxieme"), _event("2026-07-14", "Premiere")],
        days=6,
    )

    markers = cards._resolve_line(BLOCK, ctx)["markers"]

    assert [m["index"] for m in markers] == ["2026-07-14", "2026-07-16"]


class TestTheVideosTemplate:
    def _template(self):
        return next(t for t in cards.CARD_TEMPLATES if t.id == "videos")

    def test_it_answers_a_question_about_videos_not_about_kpis(self):
        template = self._template()

        assert "publish" in template.answers_question.lower()
        assert template.required_metrics == ("views",)

    def test_the_video_grain_is_optional_so_a_quiet_channel_keeps_its_card(self):
        """Un canal qui n'a rien publié garde sa courbe et ses totaux."""
        template = self._template()

        assert "video" in template.optional_dimensions
        assert "video" not in template.required_dimensions
        assert template.is_satisfied_by({"views"}, set())

    def test_the_timeline_binds_the_release_event_by_name(self):
        line = next(b for b in self._template().composition if b["type"] == "line")

        assert line["binding"]["events"] == "video_upload"
        assert line["binding"]["metrics"] == "views"

    def test_the_table_names_its_metrics_rather_than_asking_for_all(self):
        """Une table dont les colonnes dépendent du jour change de forme chaque jour."""
        table = next(b for b in self._template().composition if b["type"] == "table")

        assert table["binding"]["metrics"] == [
            "views",
            "estimated_minutes_watched",
            "likes",
            "comments",
        ]
        assert table["binding"]["dimensions"] == ["video"]

    def test_it_outranks_the_kpi_floor_and_never_the_source_specific_cards(self):
        template = self._template()
        kpi = next(t for t in cards.CARD_TEMPLATES if t.id == "kpi")
        keywords = next(t for t in cards.CARD_TEMPLATES if t.id == "keywords")

        assert kpi.fallback_rank < template.fallback_rank < keywords.fallback_rank


@pytest.mark.parametrize("kind", ["video_upload"])
def test_the_axis_of_the_marker_is_the_axis_of_the_curve(kind):
    """Un repère posé ailleurs que sur un point dessiné serait une position inventée."""
    ctx = _ctx([_event("2026-07-14", "Sortie", kind=kind)])

    payload = cards._resolve_line(BLOCK, ctx)
    axis = {point["x"] for point in payload["series"][0]["points"]}

    assert payload["markers"][0]["index"] in axis
