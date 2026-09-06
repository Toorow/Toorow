"""Evaluate and persist traceable business-path evidence."""

from __future__ import annotations

import json
from typing import Any

VALID_PATH_OUTCOMES = frozenset(
    {"pass", "missing_path", "wrong_domain", "path_version_drift", "unverifiable"}
)


def _nodes(path: dict[str, Any], node_type: str) -> list[dict[str, Any]]:
    return [
        segment
        for segment in path.get("ordered_path") or []
        if segment.get("kind") == "node" and segment.get("node_type") == node_type
    ]


def _target_matches(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    target = observed.get("target") or {}
    if target:
        return target.get("type") == expected.get("target_type") and target.get(
            "id"
        ) == expected.get("target_id")
    return any(
        node.get("id") == expected.get("target_id")
        for node in _nodes(observed, str(expected.get("target_type")))
    )


def evaluate_business_path_expectation(
    expected_routes: list[dict[str, Any]],
    observed_paths: list[dict[str, Any]],
    observed_state: str = "resolved",
) -> dict[str, Any]:
    """Compare semantic expected routes with version-sensitive observed keys.

    `observed_state` is `meta.business_context_state` from the report envelope.
    An empty `business_context_paths` means two different things -- this view has
    no governed route, or the resolver could not answer -- and only the first is a
    fact about the product. Missing evidence is reported as `unverifiable`, never
    as a verdict in either direction; the same rule the CAP-16 gate applies to
    adherence.
    """
    if observed_state not in ("resolved", ""):
        return {
            "outcome": "unverifiable",
            "observed_state": observed_state,
            "coverage_pct": None,
            "missing_path_count": 0,
            "wrong_domain_count": 0,
            "path_version_drift_count": 0,
            "matched_path_keys": [],
        }
    if not expected_routes:
        # UNE ATTENTE VIDE N'EST PAS UNE COUVERTURE DE 100 % -- story 67.6.
        #
        # Cette branche rendait `pass` et `coverage_pct: 100.0`. Mesure du
        # 2026-08-22 : `corpus.yaml` ne porte AUCUN `expected_business_routes`
        # (grep -c -> 0), donc TOUTES les questions passaient par ici et la
        # synthese annoncait << couverture des chemins : 100 % >> sur un corpus
        # qui n'exprime aucun chemin. Une jauge collee a 100 % ne mesure pas la
        # perfection, elle mesure qu'on ne lui demande rien.
        #
        # `unverifiable` est le mot que ce module a deja : son docstring dit
        # << missing evidence is reported as `unverifiable`, never as a verdict in
        # either direction >>, et `run_evals._scored_paths` exclut deja de la
        # moyenne tout enregistrement dont `coverage_pct` est None. Le chiffre
        # cesse donc de mentir sans qu'aucun agregat ne change de forme.
        return {
            "outcome": "unverifiable",
            "observed_state": observed_state or "resolved",
            "unverifiable_reason": "no_expected_route_declared",
            "coverage_pct": None,
            "missing_path_count": 0,
            "wrong_domain_count": 0,
            "path_version_drift_count": 0,
            "matched_path_keys": [],
        }

    missing = 0
    wrong = 0
    drift = 0
    matched: list[str] = []
    for expected in expected_routes:
        target_candidates = [path for path in observed_paths if _target_matches(expected, path)]
        if not target_candidates:
            missing += 1
            continue
        route_candidates = []
        for path in target_candidates:
            domain_ids = {node.get("id") for node in _nodes(path, "business_domain")}
            classification_ids = {
                node.get("id") for node in _nodes(path, "business_classification")
            }
            if expected.get("domain_id") not in domain_ids:
                continue
            classification_id = expected.get("classification_id")
            if classification_id and classification_id not in classification_ids:
                continue
            route_candidates.append(path)
        if not route_candidates:
            wrong += 1
            continue
        observed = route_candidates[0]
        path_key = observed.get("path_key")
        if path_key:
            matched.append(path_key)
        baseline = expected.get("baseline_path_key")
        if baseline and baseline != path_key:
            drift += 1

    covered = len(expected_routes) - missing - wrong
    coverage = round((covered / len(expected_routes)) * 100, 2)
    outcome = (
        "missing_path"
        if missing
        else "wrong_domain"
        if wrong
        else "path_version_drift"
        if drift
        else "pass"
    )
    return {
        "outcome": outcome,
        "observed_state": "resolved",
        "coverage_pct": coverage,
        "missing_path_count": missing,
        "wrong_domain_count": wrong,
        "path_version_drift_count": drift,
        "matched_path_keys": sorted(set(matched)),
    }


def record_evaluation_path_result(
    conn,
    *,
    run_id: str,
    question_id: str,
    expected_route: dict[str, Any],
    observed_path_key: str | None,
    trace_id: str | None,
    outcome: str,
) -> None:
    if outcome not in VALID_PATH_OUTCOMES:
        raise ValueError("Unknown business-path evaluation outcome")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.eval_business_path_results
                (run_id, question_id, expected_route, observed_path_key, trace_id, outcome)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (run_id, question_id) DO UPDATE SET
                expected_route = EXCLUDED.expected_route,
                observed_path_key = EXCLUDED.observed_path_key,
                trace_id = EXCLUDED.trace_id,
                outcome = EXCLUDED.outcome,
                observed_at = now()
            """,
            (
                run_id,
                question_id,
                json.dumps(expected_route, sort_keys=True),
                observed_path_key,
                trace_id,
                outcome,
            ),
        )
