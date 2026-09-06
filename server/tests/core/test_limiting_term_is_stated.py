"""AI-297 -- le terme qui bride la confiance est NOMME, dans le canal qu'on lit.

CE QUI ETAIT FAUX. `core.confidence` plaide dans son propre docstring pour
l'existence de `limiting_term` : un nombre unique sur trois natures differentes
-- completude, fraicheur, tracabilite -- compense, et `overview.md:32-34` refuse
cet effondrement. « Naming the limiter is the minimum that keeps the number
readable. »

Personne ne le nommait. Mesure du 2026-08-16 : `limiting_term` apparaissait dans
`core/confidence.py`, ou il est calcule, et NULLE PART AILLEURS dans le serveur.
La divulgation censee garder un nombre honnete etait elle-meme illisible, ce qui
rend ce nombre pire qu'absent : present, et cru.

Trois tests, et le troisieme est celui qu'on oublie : le silence quand il n'y a
rien a dire. Une ligne « terme limitant : aucun » sur chaque report non mesure
serait du bruit, et le bruit est la maniere dont une vraie divulgation cesse
d'etre lue.
"""
from __future__ import annotations

from core.reports import _MAX_LINES, _state_the_limiting_term


def test_the_weakest_term_is_named_with_its_value():
    envelope = {
        "meta": {
            "confidence": {
                "score": 0.72,
                "completeness": 0.9,
                "freshness": 0.8,
                "provenance": 1.0,
                "unknown_terms": [],
                "limiting_term": "freshness",
            }
        }
    }
    out = _state_the_limiting_term("Rapport.\nDeux lignes.", envelope)

    assert "freshness" in out
    assert "0.8" in out
    # Le resume d'origine est intact -- la divulgation s'ajoute, elle ne remplace pas.
    assert out.startswith("Rapport.\nDeux lignes.")


def test_an_unmeasured_term_is_named_too_because_it_is_why_the_score_is_gone():
    """Sans cette moitie, un lecteur voit le limiteur et cherche le score absent.

    `score` est None des qu'un terme est inconnu -- c'est delibere : un facteur
    inconnu entrant a 1.0 rendait un report non mesurable MIEUX note qu'un report
    mesure et imparfait. Le dire ici est ce qui empeche la question.
    """
    envelope = {
        "meta": {
            "confidence": {
                "score": None,
                "completeness": None,
                "freshness": 0.6,
                "provenance": 1.0,
                "unknown_terms": ["completeness"],
                "limiting_term": "freshness",
            }
        }
    }
    out = _state_the_limiting_term("Rapport.", envelope)

    assert "freshness" in out
    assert "completeness" in out


def test_nothing_to_disclose_says_nothing():
    """Le silence est une reponse, et c'est la bonne quand rien n'a ete mesure."""
    for envelope in (
        {},
        {"meta": {}},
        {"meta": {"confidence": None}},
        {"meta": {"confidence": {"limiting_term": None, "unknown_terms": []}}},
    ):
        assert _state_the_limiting_term("Rapport.", envelope) == "Rapport."


def test_the_thirty_line_ceiling_wins_over_the_disclosure():
    """NFR1 n'est pas negociable, meme pour une divulgation.

    Un resume qui depasserait le plafond serait tronque ailleurs, et ce qui
    tomberait serait choisi par un accident de longueur -- donc potentiellement
    un chiffre cite. Mieux vaut ne pas ajouter la ligne que de decider au hasard
    quoi perdre.
    """
    long_summary = "\n".join(f"ligne {i}" for i in range(_MAX_LINES))
    envelope = {"meta": {"confidence": {"limiting_term": "freshness", "freshness": 0.5}}}

    assert _state_the_limiting_term(long_summary, envelope) == long_summary


def test_render_report_actually_appends_it_and_not_only_the_helper():
    """LE BRANCHEMENT, pas seulement la fonction.

    Ecrit apres avoir sonde le garde ci-dessus : retirer l'appel dans
    `render_report` le laissait entierement VERT. Un garde qui n'eprouve que la
    fonction prouve qu'elle sait dire la phrase, jamais qu'elle est appelee -- et
    c'est l'appel qui manquait pendant toute la vie de `limiting_term`.
    """
    from unittest.mock import patch

    from core import reports as reports_module

    rows = [
        {
            "date": "2026-07-01",
            "connector": "google-analytics",
            "metric": "sessions",
            "value": 1450.0,
            "pull_id": "pull_EXAMPLE",
            "loaded_at": "2026-07-01T00:00:00",
        }
    ]
    verdict = {
        "score": None,
        "completeness": None,
        "freshness": 0.6,
        "provenance": 1.0,
        "unknown_terms": ["completeness"],
        "limiting_term": "freshness",
    }

    class _Loaded:
        name = "google-analytics"
        manifest: dict = {}
        reports = [
            {
                "id": "overview_daily",
                "metrics": ["sessions"],
                "date_window": {"default_days": 1},
                "narrative_prompt": "Rapport.",
            }
        ]

    with (
        patch("core.warehouse.query_report", return_value=rows),
        patch("core.confidence.compute_confidence", return_value=verdict),
    ):
        summary, envelope, _uri = reports_module.render_report(
            [_Loaded()],
            "proj_EXAMPLE",
            "google-analytics/overview_daily",
            "2026-07-01",
            "2026-07-01",
        )

    assert envelope["meta"]["confidence"]["limiting_term"] == "freshness"
    assert "freshness" in summary
    assert "completeness" in summary
