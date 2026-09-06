"""Business-route evaluation outcomes for Story 45.1."""

from core.evaluation_paths import evaluate_business_path_expectation


def _observed(*, domain: str = "bdm_sales", path_key: str = "ctxp_current"):
    return [
        {
            "path_key": path_key,
            "ordered_path": [
                {
                    "kind": "node",
                    "node_type": "business_domain",
                    "id": domain,
                    "version_number": 2,
                },
                {
                    "kind": "node",
                    "node_type": "report_view",
                    "id": "ga4/acquisition",
                    "version_number": 1,
                },
            ],
        }
    ]


def test_path_evaluation_reports_missing_and_wrong_domain_separately():
    expected = [
        {
            "domain_id": "bdm_sales",
            "target_type": "report_view",
            "target_id": "ga4/acquisition",
        }
    ]
    assert evaluate_business_path_expectation(expected, [])["outcome"] == "missing_path"
    assert (
        evaluate_business_path_expectation(expected, _observed(domain="bdm_marketing"))["outcome"]
        == "wrong_domain"
    )


def test_path_evaluation_detects_version_drift_from_semantic_route():
    expected = [
        {
            "domain_id": "bdm_sales",
            "target_type": "report_view",
            "target_id": "ga4/acquisition",
            "baseline_path_key": "ctxp_previous",
        }
    ]
    result = evaluate_business_path_expectation(expected, _observed())
    assert result["outcome"] == "path_version_drift"
    assert result["coverage_pct"] == 100.0


def test_path_evaluation_passes_when_route_and_version_match():
    expected = [
        {
            "domain_id": "bdm_sales",
            "target_type": "report_view",
            "target_id": "ga4/acquisition",
            "baseline_path_key": "ctxp_current",
        }
    ]
    result = evaluate_business_path_expectation(expected, _observed())
    assert result == {
        "outcome": "pass",
        "observed_state": "resolved",
        "coverage_pct": 100.0,
        "missing_path_count": 0,
        "wrong_domain_count": 0,
        "path_version_drift_count": 0,
        "matched_path_keys": ["ctxp_current"],
    }


def test_unavailable_business_context_is_unverifiable_not_a_missing_path():
    """An envelope that could not resolve its paths is unknown, not empty.

    Story 45.1 H-2 / 45.4 M-2: `get_report` sets `business_context_paths` to `[]`
    when resolution fails, and this evaluator read that as the factual absence of
    a governed route -- turning an infrastructure failure into a `missing_path`
    regression. The state now travels with the evidence.
    """
    expected = [
        {
            "domain_id": "bdm_sales",
            "target_type": "report_view",
            "target_id": "ga4/acquisition",
        }
    ]
    for state in ("unavailable", "denied"):
        result = evaluate_business_path_expectation(expected, [], state)
        assert result["outcome"] == "unverifiable"
        assert result["observed_state"] == state
        # Not scored in either direction, and it must not dilute coverage.
        assert result["coverage_pct"] is None
        assert result["missing_path_count"] == 0

    # A genuinely empty resolution is still a missing path.
    missing = evaluate_business_path_expectation(expected, [], "resolved")
    assert missing["outcome"] == "missing_path"
    assert missing["missing_path_count"] == 1


def test_no_expected_route_is_unverifiable_never_a_coverage_of_one_hundred():
    """Story 67.6 — la jauge etait collee a 100 % en ne mesurant rien.

    CE QUI ETAIT MESURE le 2026-08-22 : cette branche rendait `pass` et
    `coverage_pct: 100.0`, et `corpus.yaml` ne porte AUCUN
    `expected_business_routes` (`grep -c` -> 0). Toutes les questions passaient
    donc par ici, et la synthese annoncait << couverture des chemins : 100 % >>
    sur un corpus qui n'exprime aucun chemin.

    Une jauge collee a 100 % ne mesure pas la perfection : elle mesure qu'on ne
    lui demande rien. Et c'est le pire des deux, parce qu'elle se lit comme la
    premiere.
    """
    result = evaluate_business_path_expectation([], _observed())

    assert result["outcome"] == "unverifiable"
    # `None`, jamais 100 : `run_evals._scored_paths` exclut de la moyenne tout
    # enregistrement sans chiffre, donc l'absence cesse de compter comme un
    # succes sans qu'aucun agregat ne change de forme.
    assert result["coverage_pct"] is None
    # Et la raison est NOMMEE : << invérifiable >> sans cause ne dit pas s il
    # manque une attente ou une résolution.
    assert result["unverifiable_reason"] == "no_expected_route_declared"
    # Ce n'est pas non plus un echec : rien n'est manquant, rien n'a derive.
    assert result["missing_path_count"] == 0
    assert result["wrong_domain_count"] == 0
    assert result["path_version_drift_count"] == 0


def test_an_expectation_that_matches_still_scores_one_hundred():
    """Le jumeau : la jauge doit pouvoir atteindre 100 pour de VRAIES raisons.

    Sans lui, remplacer 100 par None partout passerait le test ci-dessus.
    """
    expected = [
        {
            "domain_id": "bdm_sales",
            "target_type": "report_view",
            "target_id": "ga4/acquisition",
        }
    ]
    result = evaluate_business_path_expectation(expected, _observed())
    assert result["outcome"] == "pass"
    assert result["coverage_pct"] == 100.0
