"""Agent-facing card-key contract for Epic 35 (Story 35.1, A path).

Gate 35.0 decided **A**: the research agent SELECTS an existing card by supplying
``get_card`` keys; it does not compose a new one. This module is the single, versioned
projection the agent reads to know *which* card a project can render and *exactly* which
keys to submit. It is derived entirely from ``server/core/cards.py`` — there is no second
template/metric/dimension vocabulary here.

Three things:

1. ``agent_card_catalog(...)`` — the per-project catalogue: every non-context template with
   ``satisfiable`` + ``missing`` computed against the project's available metrics/dimensions.
2. ``recommend_card(...)`` — thin wrapper over ``cards.suggest_template`` (best + alternatives).
3. ``build_card_keys(...)`` — the ONE place that constructs the ``card`` object of the 35.0
   published-insight schema (``mode: "template"``), fail-closed on unknown/unsatisfiable
   templates or missing inputs. Agent, server and renderer therefore share one card shape.

Availability inputs are passed in — this module is PURE (no DB/warehouse). Resolving a
project's real available fields is the capability-discovery MCP tool in Story 35.2.

ASCII-only module (L-3).
"""

from __future__ import annotations

from dataclasses import dataclass

from core import cards
from core.daily_insights_schema import CARD_MODE_TEMPLATE

CARD_KEY_CONTRACT_VERSION = "1"


@dataclass(frozen=True)
class CardKeysResult:
    """Outcome of ``build_card_keys``.

    ``ok`` True → ``card`` is the published-insight ``card`` object ready to embed.
    Otherwise ``reason_code`` mirrors the 35.0 validator vocabulary so the two layers
    agree (``template_unknown`` / ``metric_unavailable`` / ``dimension_unavailable``).
    """

    ok: bool
    card: dict | None = None
    reason_code: str | None = None
    message: str | None = None


def agent_card_catalog(
    available_metrics: set[str],
    available_dimensions: set[str],
) -> list[dict]:
    """Agent-readable catalogue for a project.

    One entry per NON-context template (context cards are explicit-only, same rule as
    ``cards.suggest_template``). Each entry is the registry's own ``to_catalog_entry()``
    plus ``satisfiable`` and, when not, the ``missing`` metrics/dimensions — so the agent
    never requests a card the project cannot render.
    """

    catalogue: list[dict] = []
    for tpl in cards.CARD_TEMPLATES:
        if tpl.is_context:
            continue
        entry = tpl.to_catalog_entry()
        satisfiable = tpl.is_satisfied_by(available_metrics, available_dimensions)
        entry["satisfiable"] = satisfiable
        if not satisfiable:
            entry["missing"] = _missing_inputs(tpl, available_metrics, available_dimensions)
        catalogue.append(entry)
    # Deterministic order: satisfiable first, then by fallback_rank desc, then id.
    catalogue.sort(key=lambda e: (not e["satisfiable"], -e.get("fallback_rank", 0), e["id"]))
    return catalogue


def recommend_card(
    available_metrics: set[str],
    available_dimensions: set[str],
) -> dict:
    """ "Which single card best fits this project right now?" — best + alternatives.

    Deterministic wrapper over ``cards.suggest_template``. Returns ids + the question each
    card answers so the agent can explain its choice. ``best`` is None when nothing fits.
    """

    best, alternatives = cards.suggest_template(available_metrics, available_dimensions)
    return {
        "best": _brief(best) if best else None,
        "alternatives": [_brief(t) for t in alternatives],
    }


def build_card_keys(
    template: str,
    *,
    date_from: str,
    date_to: str,
    metrics: list[str] | None = None,
    report_ref: str | None = None,
    plan_id: str | None = None,
    available_metrics: set[str],
    available_dimensions: set[str],
) -> CardKeysResult:
    """Construct the published-insight ``card`` object for a template selection (A path).

    Fail-closed: unknown template, context-kind template, an unsatisfiable template, or an
    explicitly requested metric that is not available all return a reason_code that matches
    the 35.0 validator. The returned ``card`` embeds directly under ``payload["card"]`` and
    the same keys drive ``get_card`` at render time.
    """

    tpl = cards.get_template(template)
    if tpl is None:
        return CardKeysResult(
            False, reason_code="template_unknown", message=f"unknown template {template!r}"
        )
    if tpl.is_context:
        return CardKeysResult(
            False,
            reason_code="template_unknown",
            message=f"template {template!r} is context-kind (explicit-only, not agent-selectable)",
        )
    if not tpl.is_satisfied_by(available_metrics, available_dimensions):
        missing = _missing_inputs(tpl, available_metrics, available_dimensions)
        # Surface the dominant missing input class first (metrics then dimensions).
        if missing["metrics"]:
            return CardKeysResult(
                False,
                reason_code="metric_unavailable",
                message=f"template {template!r} needs metrics {missing['metrics']} not available",
            )
        return CardKeysResult(
            False,
            reason_code="dimension_unavailable",
            message=f"template {template!r} needs dimensions {missing['dimensions']} not available",
        )

    # Explicitly requested extra metrics must themselves be available and never a raw
    # non-additive value (the semantic layer resolves those at render time).
    clean_metrics: list[str] = []
    for m in metrics or []:
        if m != cards.ANY_METRIC and m not in available_metrics:
            return CardKeysResult(
                False,
                reason_code="metric_unavailable",
                message=f"requested metric {m!r} not available",
            )
        clean_metrics.append(m)

    card: dict = {"mode": CARD_MODE_TEMPLATE, "template": template}
    if clean_metrics:
        card["metrics"] = clean_metrics
    if report_ref:
        card["reportRef"] = report_ref
    if plan_id:
        card["planId"] = plan_id
    return CardKeysResult(True, card=card)


# ---------------------------------------------------------------------------
# Helpers (delegate to cards.py; nothing redefined)
# ---------------------------------------------------------------------------


def _brief(tpl: "cards.CardTemplate") -> dict:
    return {
        "id": tpl.id,
        "answers_question": tpl.answers_question,
        "fallback_rank": tpl.fallback_rank,
    }


def _missing_inputs(
    tpl: "cards.CardTemplate",
    available_metrics: set[str],
    available_dimensions: set[str],
) -> dict:
    """Metrics/dimensions a template needs but the project lacks (mirrors cards._missing_inputs)."""

    req_metrics = set(tpl.required_metrics)
    missing_metrics: list[str] = []
    if cards.ANY_METRIC in req_metrics:
        req_metrics = req_metrics - {cards.ANY_METRIC}
        if not available_metrics:
            missing_metrics.append(cards.ANY_METRIC)
    missing_metrics.extend(sorted(req_metrics - available_metrics))
    missing_dimensions = sorted(set(tpl.required_dimensions) - available_dimensions)
    return {"metrics": missing_metrics, "dimensions": missing_dimensions}
