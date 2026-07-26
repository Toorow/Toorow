"""toorow -- Card template registry + selection contract (Story 9.1).

This module is the SPINE of Epic 9. It is DATA-DRIVEN and SOURCE-AGNOSTIC (AD-2):
a card template is a registry entry naming CANONICAL metrics/dimensions (warehouse
vocabulary), never a module name and never a code branch. Adding stories 9.3-9.6 is
"append one CardTemplate + one widget package" with ZERO changes to this module's
selection logic.

Responsibilities:
  * ``CARD_TEMPLATES``      -- the registry (data). v1 registers ``kpi`` (universal fallback).
  * ``list_templates``      -- the discoverable catalog the LLM reads to CHOOSE.
  * ``suggest_template``    -- server-side suggestion when the caller omits ``template``
                               (best fallback_rank whose required inputs are satisfied).
  * ``get_card``            -- resolve rows (report_ref OR ad-hoc metrics), compute the
                               delta-aware rollup, render the deterministic cited comment
                               (narrative.build_narrative, AD-9), carry R6
                               metric_definitions + llm_commentary_guidelines (via the
                               flows merge read path), and build the AD-1 envelope shaped
                               for the chosen card with ``meta.card_selection``.

Invariants: AD-1 (envelope + <=30-line summary built by the caller), AD-2 (canonical,
no module strings), AD-5 (project scoping is enforced by the tool wrapper before calling
here), AD-9 (never invent a cause -- delegated to narrative.py, called AS-IS).

ASCII-only stdout (AI-03). No private framework attributes on cross-module calls.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import rollup

logger = logging.getLogger(__name__)

# Sentinel for "any >= 1 canonical metric" -- makes a template the universal fallback.
ANY_METRIC = "*"

# Core-owned KPI card widget URI (mirrors DAILY_REPORT_WIDGET_URI naming). Every card
# is core-scoped: "ui://core/card-<id>".
KPI_CARD_WIDGET_URI = "ui://core/card-kpi"

# Business-card widget URIs (Stories 9.3-9.6). Same "ui://core/card-<id>" scheme.
KEYWORDS_CARD_WIDGET_URI = "ui://core/card-keywords"
CONVERSIONS_CARD_WIDGET_URI = "ui://core/card-conversions"
USERTYPES_CARD_WIDGET_URI = "ui://core/card-usertypes"
JOURNEY_CARD_WIDGET_URI = "ui://core/card-journey"
ATTRIBUTION_CARD_WIDGET_URI = "ui://core/card-attribution"
DEDUP_CARD_WIDGET_URI = "ui://core/card-dedup"
# Story 22.5: mediaplan pacing context card.
MEDIAPLAN_PACING_CARD_WIDGET_URI = "ui://core/card-mediaplan-pacing"

# Story 9.8: context-kind card. The connectors card answers an INVENTORY question
# ("which connectors exist and what do they feed?") from app-level tables (connections
# + health + report-to-datamodel chain), NOT from the warehouse fact rows.
CONNECTORS_CARD_WIDGET_URI = "ui://core/card-connectors"

# Card KINDS (Story 9.8). Data-driven, no module branches (AD-2):
#   "kpi"     -- fact-backed card (rows from the warehouse; suggestible by default).
#   "context" -- inventory/context card resolved from app-level sources (no fact rows);
#                EXPLICIT-ONLY (never auto-suggested) so the LLM discovers it via
#                list_card_templates and requests it by id.
CARD_KIND_KPI = "kpi"
CARD_KIND_CONTEXT = "context"

# French-first status labels for the connectors card (Story 9.8). Health/ledger status
# codes -> operator-facing labels with accents. Unknown -> "Inconnu" (never invent).
_TOOROW_STATUS_LABELS: dict[str, str] = {
    "ok": "Opérationnel",
    "stale": "Obsolète",
    "revoked": "Révoqué",
    "error": "En erreur",
}
_TOOROW_STATUS_UNKNOWN = "Inconnu"

# Story 10.2: FR labels for user_type breakdown values (new/returning/unknown from GA4
# newVsReturning).  Applied in _resolve_donut for dimension='user_type' only.
# Unknown raw value -> falls back to the raw string (never blank, AD-9 honesty).
_USER_TYPE_LABELS: dict[str, str] = {
    "new": "Nouveaux",
    "returning": "Fidèles",
    "unknown": "Indéterminés",
}

_DEFAULT_WINDOW_DAYS = 30

# Default top-N for bar/table/donut breakdown blocks (kept small for card real estate).
_DEFAULT_TOP_N = 8

# Sensible default CPA objective (currency-normalised, EUR) used by the gauge block
# when no explicit target is bound on the block. DOCUMENTED as a default in the payload
# (gauge.target_source = "default") so the UI can note it. Never a silent magic number.
_DEFAULT_CPA_TARGET = 50.0

# Special dimension token: bind a breakdown block to the row-level ``connector`` column
# (cross-source "by source" grouping) rather than a warehouse breakdown_dimension. This
# is source-agnostic: the connector value flows from the data rows (AD-2), never a
# hard-coded module name.
DIMENSION_CONNECTOR = "connector"

# Metrics that must NEVER be summed across breakdown rows (AD-4). Mirrors
# rollup._NON_ADDITIVE_METRICS; kept local so this module does not reach into a private
# attribute of rollup for a value it needs at block-resolution time.
_NON_ADDITIVE_METRICS = frozenset({"average_position", "roas", "ctr", "cpa"})

# Ratio metric -> (numerator, denominator) canonical component metrics. When both
# components are present in the rows a ratio metric is aggregated as RATIO-OF-SUMS
# (SUM(num)/SUM(den)), never as a mean of per-row ratios (AD-4, review-9-2b F-8).
# ``average_position`` is impression-weighted (handled separately, not a plain ratio).
# ``cpa`` is derived at block-resolution time in the gauge/table resolvers from its
# cost/conversions components rather than aggregated here.
_RATIO_COMPONENTS = {
    "ctr": ("clicks", "impressions"),
    "roas": ("conversions_value", "cost"),
}


@dataclass(frozen=True)
class CardTemplate:
    """A single card template -- DATA, not code (AD-2).

    A module whose datastreams map the ``required_metrics`` / ``required_dimensions``
    canonical fields can render this card with ZERO card code (the conformance rule).

    ``composition`` declares the ordered blocks this card renders:
      Each block = ``{type, binding, title?}`` where:
        type    = 'kpi_row'|'line'|'bar'|'gauge'|'donut'|'funnel'|'table'|'comment'
        binding = {metrics: '*'|<canonical-metric-name>, dimensions?: [...]}
                  '*' means all resolved metrics (universal for KPI/comment).
      The renderer maps block type -> viz primitive in order. Unknown types render
      a designed empty state and never crash (Story 9.2c).

    PRECONDITION (review-9-1 F-7): the special ``connector`` token must NEVER appear in
    ``required_dimensions``. ``connector`` is a row-level column, not a warehouse
    ``breakdown_dimension``, so ``_available_inputs`` never extracts it -- a card that
    REQUIRED it would never be satisfiable. Bind it only as an ``optional_dimensions`` entry
    or inside a block's ``dimensions`` binding (both handled specially at resolve time).
    """

    id: str
    title: str
    answers_question: str  # FR -- the ONE question this card answers
    widget_uri: str
    required_metrics: tuple[str, ...] = ()  # canonical; (ANY_METRIC,) = any >=1 metric
    required_dimensions: tuple[str, ...] = ()
    optional_dimensions: tuple[str, ...] = ()
    # Optional METRICS the card can enrich with when present (e.g. average_position on the
    # keywords card). Kept distinct from optional_dimensions so the catalog never mislabels
    # a metric as a dimension filter (review-9-3 F-6). Never gates selection.
    optional_metrics: tuple[str, ...] = ()
    fallback_rank: int = 0  # higher = preferred among templates that fit
    # Ordered composition contract (Story 9.2c). Immutable: stored as tuple of dicts.
    # Each block = {type, binding, title?}. See class docstring.
    composition: tuple[dict, ...] = ()
    # Card KIND (Story 9.8): CARD_KIND_KPI (fact-backed, suggestible) or
    # CARD_KIND_CONTEXT (app-level inventory, EXPLICIT-ONLY -- never auto-suggested).
    kind: str = CARD_KIND_KPI

    @property
    def is_context(self) -> bool:
        """True for a context-kind card (resolved from app-level sources, no fact rows)."""
        return self.kind == CARD_KIND_CONTEXT

    def to_catalog_entry(self) -> dict:
        """Serialise for list_card_templates (what the LLM reads to CHOOSE)."""
        return {
            "id": self.id,
            "title": self.title,
            "answers_question": self.answers_question,
            "widget_uri": self.widget_uri,
            "required_metrics": list(self.required_metrics),
            "required_dimensions": list(self.required_dimensions),
            "optional_dimensions": list(self.optional_dimensions),
            "optional_metrics": list(self.optional_metrics),
            "fallback_rank": self.fallback_rank,
            "composition": list(self.composition),
            # Story 9.8: expose kind so the admin catalog can label context vs kpi cards
            # and the LLM knows a context card is explicit-only (not auto-suggested).
            "kind": self.kind,
        }

    def is_satisfied_by(
        self, available_metrics: set[str], available_dimensions: set[str]
    ) -> bool:
        """True when the available canonical metrics/dimensions satisfy this card.

        ``ANY_METRIC`` requires >= 1 available metric (universal fallback). Otherwise
        every required metric AND every required dimension must be present.
        """
        req_metrics = set(self.required_metrics)
        if ANY_METRIC in req_metrics:
            if not available_metrics:
                return False
            req_metrics = req_metrics - {ANY_METRIC}
        if not req_metrics.issubset(available_metrics):
            return False
        return set(self.required_dimensions).issubset(available_dimensions)


# ---------------------------------------------------------------------------
# The registry (v1: KPI only -- the universal base every module inherits, 9.2).
# 9.3-9.6 append entries here; nothing else in this module changes.
# ---------------------------------------------------------------------------
CARD_TEMPLATES: list[CardTemplate] = [
    CardTemplate(
        id="kpi",
        title="Synthèse KPI",
        answers_question=(
            "Comment évoluent mes indicateurs clés sur la période "
            "(valeurs, variations, tendance) ?"
        ),
        widget_uri=KPI_CARD_WIDGET_URI,
        required_metrics=(ANY_METRIC,),  # any >= 1 canonical metric -> universal fallback
        required_dimensions=(),
        optional_dimensions=("date",),
        fallback_rank=0,  # the floor: every other card should outrank KPI when it fits
        # Composition contract (Story 9.2c): KPI = hero KPI row + cited comment.
        # 9.3-9.6 will declare richer compositions (bar+table+comment etc.) here.
        composition=(
            {"type": "kpi_row", "binding": {"metrics": "*"}},
            {"type": "comment", "binding": {"metrics": "*"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 9.3 Keywords / queries (search-source). Required clicks+impressions; dims page/query
    # (page is the reliably-present grain, query is optional). average_position is
    # optional (non-additive, routed to semantic_avg_position when the report path is
    # used). Composition: KPI hero + top-movers bar + top-queries table + comment.
    # -----------------------------------------------------------------------
    CardTemplate(
        id="keywords",
        title="Mots-clés",
        answers_question=(
            "Comment se portent mes mots-clés / requêtes "
            "(top requêtes, mouvements, opportunités) ?"
        ),
        widget_uri=KEYWORDS_CARD_WIDGET_URI,
        required_metrics=("clicks", "impressions"),
        required_dimensions=("page",),
        optional_dimensions=("query", "country", "device"),
        # average_position is a METRIC, not a dimension (review-9-3 F-6): the table block
        # binds it as a metric and it is non-additive (impression-weighted, never summed).
        optional_metrics=("average_position",),
        fallback_rank=20,
        composition=(
            {"type": "kpi_row", "binding": {"metrics": "*"}},
            {
                # Rank MOVERS bar (story 9.3 spec): signed position delta per query.
                # ``movers: true`` signals _resolve_bar to use the prior-period comparison
                # path instead of the generic top-N-by-value path (AD-2 source-agnostic:
                # reads canonical average_position/impressions + the query dimension).
                "type": "bar",
                "title": "Requêtes en mouvement (±pos.)",
                "binding": {
                    "metrics": "average_position",
                    "dimensions": ["query", "page"],
                    "movers": True,
                    "direction": "down_good",
                },
            },
            {
                "type": "table",
                "title": "Top requêtes",
                "binding": {
                    "metrics": ["clicks", "impressions", "average_position"],
                    "dimensions": ["query", "page"],
                },
            },
            {
                # Opportunités (review-epic-9-integration F-1): easy wins = top queries by
                # impressions whose impression-weighted average_position is still > 10 (lots
                # of demand, weak rank). AD-2: the ``opportunities`` flag drives the resolver,
                # never a template-id branch. Empty -> a designed empty table (never absent).
                "type": "table",
                "title": "Opportunités",
                "binding": {
                    "metrics": ["impressions", "average_position"],
                    "dimensions": ["query", "page"],
                    "opportunities": True,
                },
            },
            {
                # Story 10.5: cannibalisation table -- queries split across >= 2 pages,
                # read from the JOINT query>page composite grain (never the marginals).
                # AD-2: the ``cannibalisation`` binding flag drives the resolver, never a
                # template-id branch. Empty -> designed empty table (never absent).
                "type": "table",
                "title": "Cannibalisation",
                "binding": {
                    "metrics": ["impressions", "average_position"],
                    "dimensions": ["query", "page"],
                    "cannibalisation": True,
                },
            },
            {"type": "comment", "binding": {"metrics": "*"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 9.4 Conversions (cross-source). Required conversions; cost optional
    # (present -> CPA gauge lights up). "By source" groups on the row-level connector
    # column (DIMENSION_CONNECTOR). Composition: KPI + donut(by source) + gauge(CPA) +
    # table(by source) + comment.
    # -----------------------------------------------------------------------
    CardTemplate(
        id="conversions",
        title="Conversions",
        answers_question=(
            "D'où viennent mes conversions et à quel coût (par source, CPA) ?"
        ),
        widget_uri=CONVERSIONS_CARD_WIDGET_URI,
        required_metrics=("conversions",),
        required_dimensions=(),
        optional_dimensions=("cost", "connector", "source", "campaign_id"),
        fallback_rank=30,
        composition=(
            {"type": "kpi_row", "binding": {"metrics": "*"}},
            {
                "type": "donut",
                "title": "Conversions par source",
                "binding": {"metrics": "conversions", "dimensions": ["connector"]},
            },
            {
                "type": "gauge",
                "title": "CPA vs objectif",
                "binding": {
                    "metrics": "cpa",
                    "numerator": "cost",
                    "denominator": "conversions",
                    "direction": "down_good",
                    "unit": "EUR",
                },
            },
            {
                "type": "table",
                "title": "Par source",
                "binding": {
                    "metrics": ["conversions", "cost", "cpa"],
                    "dimensions": ["connector"],
                },
            },
            {"type": "comment", "binding": {"metrics": "*"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 9.5 User types (web-analytics source). Required active_users+sessions;
    # dims device_category/country.
    # Composition: KPI + donut(active_users by device) + bar(active_users by country) +
    # table + comment.
    # -----------------------------------------------------------------------
    CardTemplate(
        id="usertypes",
        title="Types d'utilisateurs",
        answers_question=(
            "Qui sont mes utilisateurs (Nouveaux vs fidèles, appareil, pays) ?"
        ),
        widget_uri=USERTYPES_CARD_WIDGET_URI,
        required_metrics=("active_users", "sessions"),
        required_dimensions=("device_category",),
        # Story 10.2: user_type is optional — the card degrades cleanly to the device
        # donut when the user_type_daily datastream is not enabled (required_dimensions
        # stays device_category so the card remains servable without user_type data).
        optional_dimensions=("country", "country>device", "user_type"),
        fallback_rank=20,
        composition=(
            {"type": "kpi_row", "binding": {"metrics": "*"}},
            # Story 10.2: new/returning donut leads the card (before device donut).
            # _resolve_donut returns an empty payload (slices:[]) when no user_type rows
            # are present → the shell renders donut-empty; the card remains valid (degrade).
            {
                "type": "donut",
                "title": "Nouveaux vs fidèles",
                "binding": {"metrics": "active_users", "dimensions": ["user_type"]},
            },
            {
                "type": "donut",
                "title": "Utilisateurs par appareil",
                "binding": {"metrics": "active_users", "dimensions": ["device_category"]},
            },
            {
                "type": "bar",
                "title": "Utilisateurs par pays",
                "binding": {"metrics": "active_users", "dimensions": ["country"]},
            },
            {
                "type": "table",
                "title": "Par appareil",
                "binding": {
                    "metrics": ["active_users", "sessions"],
                    "dimensions": ["device_category"],
                },
            },
            {"type": "comment", "binding": {"metrics": "*"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 9.6 User journey / funnel (web-analytics source). Required sessions+conversions. v1 funnel =
    # sessions -> conversions stage rate.
    # Story 10.4: added bar « Pages d'entrée » (sessions by landing_page, top 5) and
    # table « Pages les plus vues » (screen_page_views by page, columns Page/Vues/Part).
    # Both blocks are AFTER the funnel, BEFORE the comment. required_dimensions stays ()
    # so the card is servable without the pages datastreams (degrade-never-break).
    # optional_dimensions gains "landing_page" and "page" (the two 10.3 partitions).
    # Exit pages are OUT OF SCOPE: absent from the GA4 Data API v1 (no exit-page
    # equivalent in the Data API; documented in epic-10 plan, 10.3 availability table).
    # -----------------------------------------------------------------------
    CardTemplate(
        id="journey",
        title="Parcours utilisateur",
        answers_question=(
            "Où les utilisateurs décrochent-ils (étapes du parcours, taux de passage) ?"
        ),
        widget_uri=JOURNEY_CARD_WIDGET_URI,
        required_metrics=("sessions", "conversions"),
        required_dimensions=(),
        # Story 10.4: landing_page (sessions, GA4 entry pages) and page
        # (screen_page_views, GA4 top pages) are optional — both added by 10.3.
        # required_dimensions stays () so the card remains servable without them.
        optional_dimensions=(
            "active_users", "device_category", "country", "landing_page", "page"
        ),
        # Ranks above conversions (30): when BOTH sessions AND conversions are present the
        # richer funnel/journey card is the better answer; conversions still wins when only
        # conversions (no sessions) is available. KPI (0) remains the universal floor.
        fallback_rank=35,
        composition=(
            {
                "type": "funnel",
                "title": "Parcours sessions -> conversions",
                "binding": {"steps": ["sessions", "active_users", "conversions"]},
            },
            {"type": "kpi_row", "binding": {"metrics": "*"}},
            # Story 10.4 — entry pages bar (sessions by landing_page, top 5).
            # Reads ONLY connector='google-analytics' rows via the connector_scope binding
            # flag. Degrades to an empty bar (bars:[]) when no landing_page rows present.
            {
                "type": "bar",
                "title": "Pages d'entrée",
                "binding": {
                    "metrics": "sessions",
                    "dimensions": ["landing_page"],
                    "connector_scope": "google-analytics",
                    "top_n": 5,
                },
            },
            # Story 10.4 — top pages table (screen_page_views by page, columns Page/Vues/Part).
            # « Part » is computed against the TOP-N subtotal (SUM of returned page rows),
            # NOT the connector daily total — the page partition is top-N-bounded so the
            # long tail is dropped (a full-day denominator would be incorrect).
            # Reads ONLY connector='google-analytics' rows via the connector_scope binding flag.
            # Degrades to an empty table (rows:[]) when no page rows present.
            {
                "type": "table",
                "title": "Pages les plus vues",
                "binding": {
                    "metrics": "screen_page_views",
                    "dimensions": ["page"],
                    "connector_scope": "google-analytics",
                    "subtotal_share": True,
                },
            },
            {"type": "comment", "binding": {"metrics": "*"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 16.3 Attribution (last-click vs first-click via GA4 acquisition partitions).
    # Required: conversions; no required dimension (degrade-never-break when
    # the acquisition datastream is OFF). Optional dims = the three acquisition
    # partitions landed by 16.1.
    #
    # DOUBLE-COMPTE CONTRACT (inherited from 16.2 review):
    #   session_source_medium, session_campaign, first_user_source_medium are THREE
    #   MARGINAL partitions of the SAME conversions. A SUM across all three gives 3×.
    #   Every block in this composition carries a ``breakdown_dim_filter`` binding flag
    #   that restricts rows to ONE breakdown_dimension BEFORE aggregation.
    #   AD-2: the flag drives the resolver (no template-id branch).
    #
    # first_click has CHANNEL ONLY (no campaign) — 16.1 landed first_user_source_medium
    # only; no firstUserCampaignName partition. The campaign bar is last_click only.
    # -----------------------------------------------------------------------
    CardTemplate(
        id="attribution",
        title="Attribution",
        answers_question=(
            "Quelle part de mes conversions vient de chaque canal, "
            "en dernier clic vs premier clic ?"
        ),
        widget_uri=ATTRIBUTION_CARD_WIDGET_URI,
        required_metrics=("conversions",),
        required_dimensions=(),
        optional_dimensions=(
            "session_source_medium",      # last-click channel
            "session_campaign",           # last-click campaign
            "first_user_source_medium",   # first-click channel (campaign NULL by design)
        ),
        optional_metrics=("sessions",),
        # Ranks below journey (35) and conversions (30) — the generic conversions card
        # answers a cross-source question; attribution is more specific (GA4 acquisition
        # axes). When acquisition dims are present, attribution is the better answer but
        # we rank it just below conversions because required_metrics overlap. KPI (0) stays
        # the universal floor.
        fallback_rank=25,
        composition=(
            # Hero KPI: total conversions from the last-click channel partition.
            # breakdown_dim_filter restricts to session_source_medium so the hero is
            # computed on ONE partition (no double-count with campaign/first rows).
            # Degrade: if session_source_medium rows absent -> rollup value 0 or None.
            {
                "type": "kpi_row",
                "binding": {
                    "metrics": "conversions",
                    "breakdown_dim_filter": "session_source_medium",
                },
            },
            # Last-click channels bar (top-N by conversions).
            # breakdown_dim_filter: session_source_medium ONLY (not session_campaign).
            # Degrade: bars:[] when no session_source_medium rows present.
            # review-16-5 F12: title suffixed with "(top-N)" -- the share is a share
            # of the returned top-N, not the full GA4 day total (pattern 10.4).
            {
                "type": "bar",
                "title": "Canaux — dernier clic (top-N)",
                "binding": {
                    "metrics": "conversions",
                    "dimensions": ["session_source_medium"],
                    "breakdown_dim_filter": "session_source_medium",
                },
            },
            # First-click channels bar (top-N by conversions, for comparison).
            # Only first_user_source_medium is available for first-click (no campaign
            # for first-click by design — 16.1/16.2 constraint).
            # Degrade: bars:[] when no first_user_source_medium rows present.
            # review-16-5 F12: same top-N suffix.
            {
                "type": "bar",
                "title": "Canaux — premier clic (top-N)",
                "binding": {
                    "metrics": "conversions",
                    "dimensions": ["first_user_source_medium"],
                    "breakdown_dim_filter": "first_user_source_medium",
                },
            },
            # Campaign table (last-click only, session_campaign partition).
            # subtotal_share: Part = share of the returned top-N rows (10.4 semantics),
            # NOT a share of the GA4 day total (top-N bounded, long tail dropped).
            # breakdown_dim_filter: session_campaign ONLY.
            # Degrade: rows:[] when no session_campaign rows present.
            # review-16-5 F12: title suffixed with "(top-N)" -- share is of top-N.
            {
                "type": "table",
                "title": "Campagnes — dernier clic (top-N)",
                "binding": {
                    "metrics": ["conversions"],
                    "dimensions": ["session_campaign"],
                    "breakdown_dim_filter": "session_campaign",
                    "subtotal_share": True,
                    "subtotal_share_label": "Part (top-N)",
                    "dim_label": "Campagne",
                },
            },
            {"type": "comment", "binding": {"metrics": "*"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 17.3 Déduplication (custom card -- reads marts.dedup_estimate, not fact_daily_kpi).
    # Answers: « mes conversions sont-elles comptées en double par les régies ? »
    #
    # This is a CONTEXT-KIND card (kind=CARD_KIND_CONTEXT): it has NO required_metrics
    # from fact_daily_kpi because its data comes from marts.dedup_estimate. Resolution
    # is handled by _resolve_dedup_card (similar to the connectors card).
    #
    # EXPLICIT-ONLY: never auto-suggested (context card). The LLM must request it
    # with template="dedup". Registered here so list_card_templates exposes it.
    #
    # COMPOSITION contract (AI-54):
    #   1. kpi_row  -- héros : taux de duplication global (estimation label obligatoire)
    #                  NULL verified → état vide honnête « source de vérification indisponible »
    #   2. bar      -- « Revendiqué vs dédupliqué par canal » (claimed, dernier bloc: 2 bars)
    #                  Uses dedup_grouped binding flag -> two bar-sets in one block payload.
    #   3. table    -- Détail par canal : Canal / Revendiqué / Contribution dédupliquée / Part
    #   4. comment  -- build_dedup_comment (AD-9, <= 3 lines, formule + source + estimation)
    #
    # AD-9 RULE (non-négociable): every rendered value carries the label « Estimation » and
    # the formula; NULL rate → never rendered as 0%.
    # -----------------------------------------------------------------------
    CardTemplate(
        id="dedup",
        title="Déduplication",
        answers_question=(
            "Mes conversions sont-elles comptées en double par les régies ?"
        ),
        widget_uri=DEDUP_CARD_WIDGET_URI,
        required_metrics=(),   # context card: resolved from dedup_estimate, not fact rows
        required_dimensions=(),
        optional_dimensions=(),
        fallback_rank=0,       # irrelevant: context cards are never in the suggestion pool
        kind=CARD_KIND_CONTEXT,
        composition=(
            {
                "type": "kpi_row",
                "binding": {"metrics": "duplication_rate"},
                "title": "Taux de duplication (estimation)",
            },
            {
                "type": "bar",
                "title": "Revendiqué vs dédupliqué par canal",
                "binding": {
                    "metrics": ["claimed_conversions", "deduplicated_contribution"],
                    "dimensions": ["channel_connector"],
                    "dedup_grouped": True,
                },
            },
            {
                "type": "table",
                "title": "Détail par canal",
                "binding": {
                    "metrics": ["claimed_conversions", "deduplicated_contribution"],
                    "dimensions": ["channel_connector"],
                    "dedup_share": True,
                },
            },
            {"type": "comment", "binding": {"metrics": "*"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 22.5 Médiaplan Pacing (context-kind card). Answers: « Comment mon plan évolue-t-il
    # face aux dépenses réelles ? »
    #
    # This is a CONTEXT-KIND card (kind=CARD_KIND_CONTEXT): its data comes from the
    # plan-vs-actual pacing marts (plan_pacing_by_line / _by_channel / _by_plan) via
    # warehouse.query_plan_vs_actual, NOT from fact_daily_kpi.
    #
    # EXPLICIT-ONLY: never auto-suggested. The LLM must request it with
    # template="mediaplan_pacing" AND supply plan_id as a context arg.
    # Registered here so list_card_templates exposes it (the LLM discovers it via
    # that catalog).
    #
    # COMPOSITION contract (AI-54, AD-9):
    #   1. table    -- Lignes du plan : label / budget / consommé% / pace% / reste / extrapolé
    #                  badge « plan seul » (is_plan_only) ; NULL affiché « — » jamais 0
    #   2. table    -- Rollup par support (channel) : mêmes colonnes
    #   3. comment  -- build_mediaplan_pacing_comment (AD-9 : Estimation étiquetée, formule,
    #                  couverture jours réels, lignes plan-only signalées, version citée)
    #
    # AD-9 RULES (non-négociable) :
    #   * pace NULL → affiché « — », jamais 0 %
    #   * lignes plan-only → « plan seul », pas de % de pacing
    #   * chaque chiffre cite plan_version_id + pull_ids
    #   * extrapolated_spend = Estimation, jamais une mesure exacte
    # -----------------------------------------------------------------------
    CardTemplate(
        id="mediaplan_pacing",
        title="Pacing Médiaplan",
        answers_question=(
            "Comment mon plan évolue-t-il face aux dépenses réelles "
            "(consommé, pace, reste, extrapolé par ligne et par support) ?"
        ),
        widget_uri=MEDIAPLAN_PACING_CARD_WIDGET_URI,
        required_metrics=(),   # context card: resolved from pacing marts, not fact rows
        required_dimensions=(),
        optional_dimensions=(),
        fallback_rank=0,       # irrelevant: context cards are never in the suggestion pool
        kind=CARD_KIND_CONTEXT,
        composition=(
            {
                "type": "table",
                "title": "Lignes du plan",
                "binding": {"source": "plan_lines"},
            },
            {
                "type": "table",
                "title": "Rollup par support",
                "binding": {"source": "plan_channels"},
            },
            {"type": "comment", "binding": {"source": "plan_pacing"}},
        ),
    ),
    # -----------------------------------------------------------------------
    # 9.8 Connecteurs (context-kind card). Answers the INVENTORY question:
    # which connectors are available for a project and what do they feed?
    #
    # This is a CONTEXT card (kind=CARD_KIND_CONTEXT): it has NO required_metrics and NO
    # fact rows. get_card resolves it from Postgres app-level sources instead of the
    # warehouse (connections + health + the report-to-datamodel chain, Stories 2.5/8.3/8.9).
    #
    # EXPLICIT-ONLY selection: because required_metrics is empty, is_satisfied_by is True
    # for ANY input -- but suggest_template EXCLUDES context cards so it is never
    # auto-suggested. The LLM discovers it via list_card_templates and requests it with
    # template="connectors" (e.g. to answer "qu'est-ce que je peux brancher / qu'est-ce
    # qui tourne ?").
    #
    # Composition = a Connecteurs table (connecteur / statut / dernier extract / flows) +
    # a comment block citing the LEAST-FRESH connector (AD-9, story 9.7 pattern). The
    # table columns match EXACTLY the UI contract (Story 9.8): connector/status/
    # last_extract/flows.
    # -----------------------------------------------------------------------
    CardTemplate(
        id="connectors",
        title="Connecteurs",
        answers_question=(
            "Quels connecteurs sont disponibles et qu'alimentent-ils ?"
        ),
        widget_uri=CONNECTORS_CARD_WIDGET_URI,
        required_metrics=(),  # context card: no fact metrics gate selection
        required_dimensions=(),
        optional_dimensions=(),
        fallback_rank=0,  # irrelevant: context cards are never in the suggestion pool
        kind=CARD_KIND_CONTEXT,
        composition=(
            {
                "type": "table",
                "title": "Connecteurs",
                # binding is documentary only for a context card: the table rows are
                # resolved by the context resolver, not by the generic block resolver.
                "binding": {"source": "connectors"},
            },
            {"type": "comment", "binding": {"source": "connectors"}},
        ),
    ),
]


class CardTemplateNotFound(Exception):
    """Raised when an explicit ``template`` id is unknown."""


class CardTemplateUnsatisfied(Exception):
    """Raised when an explicit ``template`` cannot be rendered from available inputs.

    Carries ``missing`` so the caller can surface a structured, actionable error.
    """

    def __init__(self, template_id: str, missing: dict):
        self.template_id = template_id
        self.missing = missing
        super().__init__(f"Template '{template_id}' unsatisfied: {missing}")


def get_template(template_id: str) -> CardTemplate | None:
    """Return the registered template by id, or None."""
    for tpl in CARD_TEMPLATES:
        if tpl.id == template_id:
            return tpl
    return None


def _composition_has_cannibalisation(composition) -> bool:
    """True when any block in *composition* carries a ``cannibalisation`` binding flag."""
    for block in composition or ():
        if (block.get("binding") or {}).get("cannibalisation"):
            return True
    return False


def _needs_cannibalisation_positions(template_id: str | None) -> bool:
    """Story 10.5 F-1: does this EXPLICIT template read composite query>page positions?

    Narrowest, deterministic trigger for the extra ``semantic_avg_position_composite``
    fetch on the ad-hoc path: True iff *template_id* names a registered template whose
    composition contains a ``cannibalisation`` binding (today: only ``keywords``). Unknown
    or absent template -> False (the suggested path never surfaces average_position from
    fact, so keywords is never auto-selected there; no non-cannibalisation card pays for
    the query or receives its rows).
    """
    if not template_id:
        return False
    tpl = get_template(template_id)
    return tpl is not None and _composition_has_cannibalisation(tpl.composition)


def _serialize_context_events(events: list[dict] | None) -> list[dict]:
    """Serialise context_events for meta inclusion, joining dim_event_type fields.

    Story 31.5: each event in meta.context_events now carries ``platform``,
    ``source``, ``value`` (MMM regressors) + ``category`` and ``default_marker``
    (from the dim_event_type static dictionary via ``enrich_events_with_dim``).
    This is a pure additive extension — schema_version stays "1".
    AD-9: overlay is additive; no metric value is modified.
    """
    from core.report_dictionary import enrich_events_with_dim  # noqa: PLC0415

    enriched = enrich_events_with_dim(list(events or []))
    return [
        {
            "id": e.get("id"),
            "event_date": e.get("event_date"),
            "type": e.get("type"),
            "label": e.get("label"),
            "platform": e.get("platform"),
            "source": e.get("source"),
            "value": e.get("value"),
            "category": e.get("category", ""),
            "default_marker": e.get("default_marker", "pin"),
        }
        for e in enriched
    ]


def list_templates() -> list[dict]:
    """Return the catalog (serialised entries) -- the discoverable half of the contract."""
    return [tpl.to_catalog_entry() for tpl in CARD_TEMPLATES]


def suggest_template(
    available_metrics: set[str], available_dimensions: set[str]
) -> tuple[CardTemplate | None, list[CardTemplate]]:
    """Return ``(best, alternatives)`` for the available canonical inputs.

    ``best`` = the highest-fallback_rank template whose required inputs are satisfied.
    ``alternatives`` = the other satisfied templates (so the LLM can re-request). KPI
    (ANY_METRIC, rank 0) always qualifies when >= 1 metric is available, so ``best`` is
    None only when there are no metrics at all.

    CONTEXT cards (Story 9.8, kind=CARD_KIND_CONTEXT) are EXCLUDED from the suggestion
    pool: they answer an inventory question from app-level sources (no fact rows), so
    they must be requested explicitly by id (the LLM discovers them via
    list_card_templates). Excluding them here is the single choke point that keeps them
    explicit-only without a module/id branch elsewhere (AD-2).
    """
    satisfied = [
        tpl
        for tpl in CARD_TEMPLATES
        if not tpl.is_context
        and tpl.is_satisfied_by(available_metrics, available_dimensions)
    ]
    if not satisfied:
        return None, []
    satisfied.sort(key=lambda t: t.fallback_rank, reverse=True)
    return satisfied[0], satisfied[1:]


def _missing_inputs(
    tpl: CardTemplate, available_metrics: set[str], available_dimensions: set[str]
) -> dict:
    """Compute which required inputs an explicit template is missing (for the error)."""
    req_metrics = set(tpl.required_metrics)
    missing_metrics: list[str] = []
    if ANY_METRIC in req_metrics:
        req_metrics = req_metrics - {ANY_METRIC}
        if not available_metrics:
            missing_metrics.append(ANY_METRIC)
    missing_metrics += sorted(req_metrics - available_metrics)
    missing_dimensions = sorted(set(tpl.required_dimensions) - available_dimensions)
    return {"metrics": missing_metrics, "dimensions": missing_dimensions}


# ---------------------------------------------------------------------------
# Widget dist resolution -- source-agnostic (AD-2), same shape as _resolve_widget_dist.
# The KPI card is CORE-owned so its bundle lives under core-owned ui/cards/kpi/dist.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent.parent
_CARDS_DIR = _REPO_ROOT / "ui" / "cards"


def resolve_card_widget_dist(card_id: str) -> Path:
    """Resolve a card's built single-file HTML path.

    Precedence:
      1. ``TOOROW_CARD_<ID>_WIDGET_DIST`` env override (explicit path).
      2. ``ui/cards/<card_id>/dist/index.html`` (the single-file build, AD-11).
    No module names appear here (AD-2): a card is core-owned and its id is data.
    """
    env_key = f"TOOROW_CARD_{card_id.upper()}_WIDGET_DIST"
    override = os.environ.get(env_key)
    if override:
        return Path(override)
    return _CARDS_DIR / card_id / "dist" / "index.html"


# ---------------------------------------------------------------------------
# Row resolution + envelope assembly for get_card.
# ---------------------------------------------------------------------------


def _resolve_window(date_from: str, date_to: str, *, today: date | None = None) -> tuple[str, str]:
    """Resolve (start, end); default to the last _DEFAULT_WINDOW_DAYS when unset."""
    if today is None:
        today = datetime.now(tz=timezone.utc).date()
    end = date_to.strip() if date_to and date_to.strip() else today.isoformat()
    if date_from and date_from.strip():
        start = date_from.strip()
    else:
        try:
            end_date = date.fromisoformat(end)
        except (ValueError, TypeError):
            end_date = today
        start = (end_date - timedelta(days=_DEFAULT_WINDOW_DAYS)).isoformat()
    return start, end


def _available_inputs(rows: list[dict]) -> tuple[set[str], set[str]]:
    """Extract the canonical metrics + dimensions present in the resolved rows."""
    metrics = {r.get("metric") for r in rows if r.get("metric")}
    dimensions = {
        r.get("breakdown_dimension") for r in rows if r.get("breakdown_dimension")
    }
    return metrics, dimensions  # type: ignore[return-value]


def _sparkline_series(rows: list[dict], metric: str, start: str, end: str) -> list[dict]:
    """Build a per-day {date, value} series for a metric (the card sparkline).

    Sums the metric's rows by date on the SAME canonical partition per connector the
    totals use (review-epic-9-backend F-3): when a metric appears under several parallel
    breakdown dimensions for the same day, a naive cross-breakdown sum inflates the
    sparkline N-fold and disagrees with the KPI hero / donut / gauge. Filtering to
    ``_rows_for_dimension(..., connector)`` pins each connector to ONE breakdown partition
    (most day-coverage) before summing, so the sparkline day value equals the single-
    partition total. Restricted to the CURRENT window [start, end]. Sorted by date.
    """
    partition_rows = _rows_for_dimension(rows, metric, DIMENSION_CONNECTOR)
    by_date: dict[str, float] = {}
    for row in partition_rows:
        d = str(row.get("date") or "")[:10]
        if not d or d < start or d > end:
            continue
        try:
            by_date[d] = by_date.get(d, 0.0) + float(row.get("value") or 0)
        except (TypeError, ValueError):
            continue
    return [{"date": d, "value": by_date[d]} for d in sorted(by_date)]


# ---------------------------------------------------------------------------
# Per-block DATA RESOLVERS (Stories 9.3-9.6).
#
# CONTRACT: a resolver takes ``(block, ctx)`` and returns the SAME block dict with a
# resolved ``block["data"]`` payload attached (never mutating the template's declared
# block in the registry -- resolvers operate on shallow copies made by resolve_block).
#
#   block : {type, binding, title?}   -- the declared block from CardTemplate.composition
#   ctx   : _BlockContext             -- resolved rows + rollup + helpers (below)
#
# INVARIANTS every resolver upholds:
#   * AD-2 source-agnostic: reads canonical metric/dimension names from ``binding``,
#     never a module name. The special ``connector`` dimension groups on the row-level
#     connector column (cross-source), which is still data-driven (AD-2).
#   * AD-4 non-additive: metrics in _NON_ADDITIVE_METRICS are NEVER summed across
#     breakdown rows. Bars/tables show an impression-weighted value for average_position
#     (reusing rollup.weighted_avg_position) and cost/conversions ratios for CPA; the
#     kpi_row/donut read additive sums only for additive metrics.
#   * DEGRADE, NEVER RAISE: any missing input yields an EMPTY payload (empty list /
#     null value) so the UI renders its designed empty state. resolve_block wraps every
#     resolver in a try/except that falls back to an empty payload on any exception.
#
# PAYLOAD PLACEMENT: the resolved payload is attached under ``block["data"]`` (the key
# CardComposition already reads for table blocks: ``block.data?.rows`` /
# ``block.data?.columns``). The UI agent reads ``block.data`` for EVERY block type.
# ---------------------------------------------------------------------------


@dataclass
class _BlockContext:
    """Everything a block resolver needs, computed once per get_card call.

    ``current_rows`` / ``prior_rows`` are the RAW rows (every source) -- per-source
    breakdown blocks (donut/table "by source") read these so each source is shown.
    ``dedup_current_rows`` / ``dedup_prior_rows`` have cross-source conversions dedup
    applied (Rule P) -- CROSS-SOURCE scalar blocks (funnel steps, gauge ratios) read these
    so overlapping-source conversions are not double-counted (review-9-1 F-5). When only one
    source reports conversions the two sets are identical.
    """

    current_rows: list[dict]
    prior_rows: list[dict]
    rollup: dict  # {metric: {value, delta, delta_pct, ...}} from compute_rollup
    metric_definitions: dict | None
    resolved_metrics: list[str]
    start: str
    end: str
    dedup_current_rows: list[dict] | None = None
    dedup_prior_rows: list[dict] | None = None
    # Story 10.5: per-project / per-report config dict merged from the report override doc
    # (app.report_overrides via flows). Carries cannibalisation thresholds
    # (cannib_min_share_pct, cannib_min_position_gap) read with defaults; NEVER hardcoded
    # constants. Empty on the pure ad-hoc path (defaults then apply).
    config: dict = field(default_factory=dict)

    def cross_source_current(self) -> list[dict]:
        """Rows for cross-source scalar totals (dedup applied; falls back to raw)."""
        return self.dedup_current_rows if self.dedup_current_rows is not None else self.current_rows

    def cross_source_prior(self) -> list[dict]:
        """Prior-window rows for cross-source scalar totals (dedup applied; raw fallback)."""
        return self.dedup_prior_rows if self.dedup_prior_rows is not None else self.prior_rows

    def rows_for_metric(self, metric: str) -> list[dict]:
        """Current rows to aggregate for *metric* in a PER-SOURCE breakdown block.

        review-epic-9-backend F-1 / review-epic-9-integration F-2: a conversion attributed
        to TWO overlapping sources must be counted ONCE, under its priority source (Rule P).
        If the per-source donut/table read the RAW rows they would show that conversion twice
        and their total would exceed the gauge/rollup denominator (which read the deduped rows),
        making the cited comment internally inconsistent (55.6% of a raw 180 total vs a CPA
        computed over a deduped 100 denominator). Architecture invariant story 3.7 ("no metric
        counted twice") wins over "show every source raw": the conversions donut and table now
        read the SAME deduped base as the gauge/rollup/comment, so the donut total is provably
        equal to the gauge denominator. ``conversions`` dedup only removes overlapping-source
        conversions rows -- every OTHER metric (cost, clicks, active_users, ...) is unchanged
        by ``apply_conversions_dedup``, so reading the deduped set for them is a no-op.
        """
        if metric == "conversions":
            return self.cross_source_current()
        return self.current_rows


def _direction_for(metric: str, metric_definitions: dict | None) -> str | None:
    """Return the R6 direction ('up_good'|'down_good'|'neutral') for a metric, or None.

    Direction comes from metric_definitions (R6) when present; otherwise None (the UI
    treats missing direction as 'up_good' by default). Never invents a direction.
    """
    if not metric_definitions:
        return None
    entry = metric_definitions.get(metric)
    if isinstance(entry, dict):
        return entry.get("direction")
    return None


def _delta_pct_number(delta_pct: Any) -> float | None:
    """Parse the rollup's delta_pct string ('+12%'/'-5%') into a signed float.

    The rollup stores delta_pct as a display string; the block payload exposes a numeric
    ``delta_pct`` so the UI does its own formatting. None-safe.
    """
    if delta_pct is None:
        return None
    if isinstance(delta_pct, (int, float)):
        return float(delta_pct)
    s = str(delta_pct).strip().replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


# review-epic-10 CRITICAL-A: the canonical-partition selection is now the SINGLE source of
# truth in rollup.py (pure row logic, no warehouse import -- AD-1 compliant) so that BOTH
# compute_rollup's additive totals AND these card resolvers (sparkline/donut/gauge/funnel)
# pin to the EXACT SAME partition. Re-exported here under the legacy underscore names so no
# existing cards.py caller / test breaks. Behaviour is byte-for-byte identical to the code
# that used to live here (it was moved verbatim, not rewritten).
_canonical_breakdown_per_connector = rollup.canonical_breakdown_per_connector
_neg_name = rollup._neg_name
_rows_for_dimension = rollup.rows_for_dimension


def _group_key(row: dict, dimension: str) -> str:
    """Return the grouping label for *row* under *dimension* (connector vs breakdown)."""
    if dimension == DIMENSION_CONNECTOR:
        return str(row.get("connector") or "")
    return str(row.get("breakdown_value") or "")


def _first_present_dimension(rows: list[dict], candidates: list[str]) -> str | None:
    """Return the first candidate dimension actually present in *rows* (AD-2 degrade).

    ``connector`` is always considered present (it is a row column, not a breakdown).
    """
    present = {r.get("breakdown_dimension") for r in rows}
    for dim in candidates:
        if dim == DIMENSION_CONNECTOR or dim in present:
            return dim
    return None


def _aggregate_by_group(
    rows: list[dict], metric: str, dimension: str
) -> dict[str, float]:
    """Aggregate *metric* by group under *dimension*, AD-4 aware.

    Additive metrics are SUMMED per group. ``average_position`` is impression-weighted
    per group (rollup.weighted_avg_position over the group's rows + the impression rows
    of the SAME group). Ratio metrics (ctr/roas) aggregate as ratio-of-sums when their
    components are present, else a documented per-row mean fallback (AD-4).
    """
    from core import rollup as rollup_module  # noqa: PLC0415

    metric_rows = _rows_for_dimension(rows, metric, dimension)
    groups: dict[str, list[dict]] = {}
    for r in metric_rows:
        groups.setdefault(_group_key(r, dimension), []).append(r)

    result: dict[str, float] = {}
    if metric == "average_position":
        # Impression-weighted per group -- feed each group its own metric rows PLUS the
        # matching impression rows so the weighting keys line up (AD-4).
        imp_rows = _rows_for_dimension(rows, "impressions", dimension)
        imp_by_group: dict[str, list[dict]] = {}
        for r in imp_rows:
            imp_by_group.setdefault(_group_key(r, dimension), []).append(r)
        for g, grp_rows in groups.items():
            weighted = rollup_module.weighted_avg_position(
                grp_rows + imp_by_group.get(g, [])
            )
            if weighted is not None:
                result[g] = weighted
        return result

    if metric in _NON_ADDITIVE_METRICS:
        # AD-4 (review-9-2b F-8): a ratio metric aggregates as RATIO-OF-SUMS, not a mean
        # of per-row ratios. When the metric's numerator/denominator components are BOTH
        # present in the rows we recompute SUM(num)/SUM(den) per group (double-count-safe,
        # each component summed on its own canonical partition). Otherwise -- the metric was
        # materialised directly with no components available -- we fall back to a simple mean
        # of the per-row ratio values (documented limitation: a volume-blind average).
        components = _RATIO_COMPONENTS.get(metric)
        if components is not None:
            num_name, den_name = components
            num_by_group = _aggregate_by_group(rows, num_name, dimension)
            den_by_group = _aggregate_by_group(rows, den_name, dimension)
            if num_by_group and den_by_group:
                for g in set(num_by_group) | set(den_by_group):
                    den = den_by_group.get(g)
                    num = num_by_group.get(g)
                    if den:  # ratio-of-sums; zero/absent denominator -> group omitted
                        result[g] = (num or 0.0) / den
                return result
        for g, grp_rows in groups.items():
            vals = [float(r["value"]) for r in grp_rows if r.get("value") is not None]
            if vals:
                result[g] = sum(vals) / len(vals)
        return result

    # Additive: sum per group.
    for g, grp_rows in groups.items():
        total = 0.0
        seen = False
        for r in grp_rows:
            if r.get("value") is not None:
                total += float(r["value"])
                seen = True
        if seen:
            result[g] = total
    return result


def _sum_metric(rows: list[dict], metric: str, dimension: str | None = None) -> float:
    """Total an ADDITIVE metric across *rows* on ONE dimension (double-count-safe).

    A metric may appear under several parallel breakdown dimensions for the same day/
    connector, each of which independently totals the day. Summing across all of them
    inflates the total N-fold (review-9-3 F-2/F-4). So this always sums a SINGLE
    partition:
      * ``dimension`` given and not ``connector`` -> only rows of that breakdown_dimension.
      * ``dimension == connector`` -> the metric's rows under ONE canonical breakdown
        dimension (the most-populated partition via ``_rows_for_dimension``); when the
        metric carries no breakdown dimension at all, every row is summed (one implicit
        partition).
      * ``dimension is None`` -> every row of the metric (legacy behaviour; callers that
        want the double-count-safe total pass ``connector``).
    """
    if dimension == DIMENSION_CONNECTOR:
        selected = _rows_for_dimension(rows, metric, DIMENSION_CONNECTOR)
        return sum(float(r["value"]) for r in selected if r.get("value") is not None)

    total = 0.0
    for r in rows:
        if r.get("metric") != metric:
            continue
        if dimension is not None and r.get("breakdown_dimension") != dimension:
            continue
        if r.get("value") is not None:
            total += float(r["value"])
    return total


def _resolve_kpi_row(block: dict, ctx: _BlockContext) -> dict:
    """kpi_row payload: {metrics:[{metric,value,delta,delta_pct,direction}]}.

    Reads the delta-aware rollup (already AD-4 correct). Direction from R6 defs (R6).
    Order follows resolved_metrics, then any remaining rollup keys.

    Story 16.3 (Attribution): when binding.breakdown_dim_filter is set, the metric
    value is summed ONLY on the specified breakdown_dimension (one partition, no double-
    count across parallel acquisition axes). The rollup is not used for this metric in
    that case — instead a direct sum over the filtered current rows is computed.
    AD-2: the binding flag drives the path, never a template-id branch.
    """
    binding = block.get("binding") or {}
    want = binding.get("metrics", "*")
    if want == "*":
        ordered = list(ctx.resolved_metrics) + [
            m for m in ctx.rollup if m not in ctx.resolved_metrics
        ]
    else:
        ordered = [want] if isinstance(want, str) else list(want)

    breakdown_dim_filter = binding.get("breakdown_dim_filter")

    metrics_payload: list[dict] = []
    for m in ordered:
        if breakdown_dim_filter:
            # Story 16.3: sum on ONE partition only (double-count-safe).
            filtered = _filter_rows_by_breakdown_dim(ctx.current_rows, breakdown_dim_filter)
            prior_filtered = _filter_rows_by_breakdown_dim(ctx.prior_rows, breakdown_dim_filter)
            value = _sum_metric(filtered, m)
            prior_value = _sum_metric(prior_filtered, m)
            # review-16-3 F-2: an empty partition sums to 0.0 which is surfaced as
            # value=None (designed empty state). delta/delta_pct MUST follow: a
            # {value: None, delta: 0.0} row would look like "no data but flat trend".
            out_value = value if value else None
            if out_value is None:
                delta: float | None = None
                delta_pct: float | None = None
            else:
                has_both = prior_value is not None
                delta = (value - prior_value) if has_both else None
                delta_pct = None
                if delta is not None and prior_value:
                    delta_pct = round((delta / abs(prior_value)) * 100.0, 1)
            metrics_payload.append(
                {
                    "metric": m,
                    "value": out_value,
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "direction": _direction_for(m, ctx.metric_definitions),
                }
            )
        else:
            entry = ctx.rollup.get(m)
            if not entry:
                continue
            metrics_payload.append(
                {
                    "metric": m,
                    "value": entry.get("value"),
                    "delta": entry.get("delta"),
                    "delta_pct": _delta_pct_number(entry.get("delta_pct")),
                    "direction": _direction_for(m, ctx.metric_definitions),
                }
            )
    return {"metrics": metrics_payload}


def _resolve_line(block: dict, ctx: _BlockContext) -> dict:
    """line payload: {series:[{name, points:[{x:date, y:value}]}]} for bound metrics."""
    binding = block.get("binding") or {}
    want = binding.get("metrics", "*")
    if want == "*":
        metrics = list(ctx.resolved_metrics)
    else:
        metrics = [want] if isinstance(want, str) else list(want)

    series: list[dict] = []
    for m in metrics:
        pts = _sparkline_series(ctx.current_rows, m, ctx.start, ctx.end)
        if pts:
            series.append(
                {"name": m, "points": [{"x": p["date"], "y": p["value"]} for p in pts]}
            )
    return {"series": series}


def _resolve_bar_movers(
    block: dict, ctx: _BlockContext, dimension: str
) -> dict:
    """bar MOVERS payload for the keywords card (story 9.3 spec).

    Computes the impression-weighted average_position for EACH query (dimension value)
    in the current and prior periods, then produces a signed rank delta:

        delta = prior_position - current_position

    Positive delta = improved rank (lower position number = better). Only queries
    present in BOTH periods with |delta| >= 2 are included (queries in only one
    period yield no reliable signal and are skipped -- no fake deltas).

    Sorted descending by |delta|, capped at _DEFAULT_TOP_N.

    Bar item shape: {label, value, direction}
      * value    = signed delta (positive = gained positions)
      * direction = "up" when improved (delta > 0) | "down" when lost (delta < 0)

    Returns the standard bar partial-state payload (empty bars list) when:
      - ``average_position`` or ``impressions`` is absent from the rows, OR
      - no prior-period data is available, OR
      - no query reaches |delta| >= 2.
    Never raises; designed as an empty-state path (DEGRADE, NEVER RAISE).
    """
    from core import rollup as rollup_module  # noqa: PLC0415

    _MOVERS_MIN_DELTA = 2.0

    def _weighted_pos_per_group(rows: list[dict]) -> dict[str, float | None]:
        """Return {group_label: impression-weighted position} for the given rows."""
        # Collect average_position rows filtered to this dimension.
        pos_rows = _rows_for_dimension(rows, "average_position", dimension)
        imp_rows = _rows_for_dimension(rows, "impressions", dimension)
        # Group position rows by label.
        pos_by_group: dict[str, list[dict]] = {}
        for r in pos_rows:
            k = _group_key(r, dimension)
            if k:
                pos_by_group.setdefault(k, []).append(r)
        # Group impression rows by label (for weighting).
        imp_by_group: dict[str, list[dict]] = {}
        for r in imp_rows:
            k = _group_key(r, dimension)
            if k:
                imp_by_group.setdefault(k, []).append(r)
        result: dict[str, float | None] = {}
        for g, grp_pos in pos_by_group.items():
            weighted = rollup_module.weighted_avg_position(
                grp_pos + imp_by_group.get(g, [])
            )
            result[g] = weighted
        return result

    current_pos = _weighted_pos_per_group(ctx.current_rows)
    prior_pos = _weighted_pos_per_group(ctx.prior_rows)

    # Partial state: no prior data at all -> empty bars (designed empty state).
    if not prior_pos:
        return {
            "orientation": "horizontal",
            "dimension": dimension,
            "bars": [],
            "partial": True,
            "partial_reason": "Pas de période précédente disponible.",
        }

    movers: list[dict] = []
    for query, cur_val in current_pos.items():
        prior_val = prior_pos.get(query)
        # Skip queries present in only one period -- no reliable delta (spec: no fake deltas).
        if cur_val is None or prior_val is None:
            continue
        delta = prior_val - cur_val  # positive = rank improved (lower number = better)
        # review-epic-9-backend F-8: apply the threshold on the ROUNDED delta so the
        # displayed value and the filter agree (a raw 1.999... that rounds to 2.00 must pass;
        # a raw 2.000...01 that rounds to 2.00 must also pass -- both display as 2,00 pos.).
        rounded_delta = round(delta, 2)
        if abs(rounded_delta) < _MOVERS_MIN_DELTA:
            continue
        movers.append({
            "label": query,
            "value": rounded_delta,
            "direction": "up" if rounded_delta > 0 else "down",
        })

    movers.sort(key=lambda b: abs(b["value"]), reverse=True)
    movers = movers[:_DEFAULT_TOP_N]

    return {"orientation": "horizontal", "dimension": dimension, "bars": movers}


def _filter_rows_by_connector_scope(rows: list[dict], connector_scope: str | None) -> list[dict]:
    """Filter *rows* to a specific connector when *connector_scope* is set.

    Story 10.4: GA4 and GSC both land a 'page' breakdown_dimension (GA4 carries
    screen_page_views, connector='google-analytics'; GSC carries clicks/impressions,
    connector='gsc'). Binding a block with connector_scope='google-analytics' ensures
    the resolver only reads the GA4 page rows (not the GSC page rows). Idempotent when
    connector_scope is None (no filter applied).
    """
    if not connector_scope:
        return rows
    return [r for r in rows if r.get("connector") == connector_scope]


def _filter_rows_by_breakdown_dim(rows: list[dict], breakdown_dim_filter: str | None) -> list[dict]:
    """Filter *rows* to a specific breakdown_dimension when *breakdown_dim_filter* is set.

    Story 16.3 (Attribution): session_source_medium, session_campaign, and
    first_user_source_medium are THREE MARGINAL partitions of the SAME conversions.
    Summing across partitions without filtering inflates the total by N (the double-
    count invariant proved in test_customer_journeys_marginal_identity, 16.2).

    Binding a block with breakdown_dim_filter='session_source_medium' ensures the
    resolver only reads those rows — one partition, no double-count. Idempotent when
    breakdown_dim_filter is None (no filter, original behaviour).

    AD-2: this is a DATA-DRIVEN binding flag, never a template-id branch.
    """
    if not breakdown_dim_filter:
        return rows
    return [r for r in rows if r.get("breakdown_dimension") == breakdown_dim_filter]


def _resolve_bar(block: dict, ctx: _BlockContext) -> dict:
    """bar payload: {orientation, dimension, bars:[{label,value}]} top-N by value.

    When ``binding.movers is True`` and the bound metric is ``average_position``,
    dispatches to ``_resolve_bar_movers`` (keywords rank-movers path, story 9.3).
    Otherwise: groups the bound metric by binding.dimensions (first present).
    Additive -> summed; non-additive -> impression-weighted / mean (AD-4).
    Sorted desc by value, top-N.

    Story 10.4: when ``binding.connector_scope`` is set, only rows from that connector
    are used (e.g. 'google-analytics' for the GA4 landing_page bar so GSC 'page' rows
    are not included). When ``binding.top_n`` is set, it overrides _DEFAULT_TOP_N.
    """
    binding = block.get("binding") or {}
    metric = binding.get("metrics")
    if not isinstance(metric, str) or metric == "*":
        return {"orientation": "horizontal", "dimension": None, "bars": []}

    # Story 10.4: apply connector scope filter before dimension resolution.
    connector_scope = binding.get("connector_scope")
    src_rows = _filter_rows_by_connector_scope(ctx.current_rows, connector_scope)

    # Story 16.3: apply breakdown_dim_filter before dimension resolution (double-count-safe).
    # AD-2: binding flag, never a template-id branch.
    breakdown_dim_filter = binding.get("breakdown_dim_filter")
    src_rows = _filter_rows_by_breakdown_dim(src_rows, breakdown_dim_filter)

    dims = binding.get("dimensions") or []
    dimension = _first_present_dimension(src_rows, list(dims))
    if dimension is None:
        return {"orientation": "horizontal", "dimension": None, "bars": []}

    # Movers path: signed rank-delta per query (keywords card, story 9.3 spec).
    # AD-2: no template-id branch -- the ``movers`` flag in the binding is the contract.
    # NOTE (review 10.4 F-1): _resolve_bar_movers reads ctx.current_rows UNSCOPED --
    # connector_scope is intentionally NOT applied on the movers path. No current template
    # combines movers + connector_scope (keywords movers has no scope; the journey
    # entry-bar has no movers). If a future card needs both, thread src_rows into
    # _resolve_bar_movers.
    if binding.get("movers") and metric == "average_position":
        return _resolve_bar_movers(block, ctx, dimension)

    agg = _aggregate_by_group(src_rows, metric, dimension)
    top_n = int(binding.get("top_n") or _DEFAULT_TOP_N)
    bars = sorted(
        ({"label": k, "value": v} for k, v in agg.items() if k),
        key=lambda b: b["value"],
        reverse=True,
    )[:top_n]
    return {"orientation": "horizontal", "dimension": dimension, "bars": bars}


def _resolve_donut(block: dict, ctx: _BlockContext) -> dict:
    """donut payload: {total, dimension, slices:[{label,value,pct}]} share of a metric.

    Share only makes sense for ADDITIVE metrics -> the metric is summed per group. A
    non-additive metric yields an empty donut (share of a weighted average is undefined).
    """
    binding = block.get("binding") or {}
    metric = binding.get("metrics")
    if not isinstance(metric, str) or metric in _NON_ADDITIVE_METRICS:
        return {"total": 0.0, "dimension": None, "slices": []}
    dims = binding.get("dimensions") or []
    # review-epic-9-backend F-1: for conversions read the DEDUPED base so the donut total
    # equals the gauge denominator (no cross-source double count). No-op for other metrics.
    src_rows = ctx.rows_for_metric(metric)
    dimension = _first_present_dimension(src_rows, list(dims))
    if dimension is None:
        return {"total": 0.0, "dimension": None, "slices": []}

    agg = _aggregate_by_group(src_rows, metric, dimension)
    total = sum(v for v in agg.values())
    # Story 10.2: translate breakdown_value -> FR label for the user_type dimension.
    # For all other dimensions the raw value is used directly (no branching beyond the map).
    _label_map = _USER_TYPE_LABELS if dimension == "user_type" else {}
    ordered = sorted(
        (
            {
                "label": _label_map.get(k, k),
                "value": v,
                "pct": round((v / total) * 100.0, 1) if total else 0.0,
            }
            for k, v in agg.items()
            if k
        ),
        key=lambda s: s["value"],
        reverse=True,
    )
    # review-epic-9-backend F-7: when more than top-N sources exist, fold the remainder
    # into a final "Autres" slice so the slice values sum to the TRUE total and the pcts to
    # ~100 (never a silent tail that makes the donut visibly under-fill). The comment
    # builder still cites slices[0] (the true leader) from the full-total pct.
    if len(ordered) > _DEFAULT_TOP_N:
        head = ordered[: _DEFAULT_TOP_N - 1]
        tail = ordered[_DEFAULT_TOP_N - 1:]
        tail_value = sum(s["value"] for s in tail)
        head.append(
            {
                "label": "Autres",
                "value": tail_value,
                "pct": round((tail_value / total) * 100.0, 1) if total else 0.0,
            }
        )
        slices = head
    else:
        slices = ordered
    return {"total": total, "dimension": dimension, "slices": slices}


def _ratio_over_rows(
    rows: list[dict], numerator: str, denominator: str
) -> float | None:
    """SUM(numerator)/SUM(denominator) over *rows* (connector grouping, AD-4 safe).

    Each component is summed on a single canonical breakdown partition so parallel
    breakdowns are not double-counted. Returns ``None`` when the denominator is zero /
    absent (never divides by zero).
    """
    num = _sum_metric(rows, numerator, DIMENSION_CONNECTOR)
    den = _sum_metric(rows, denominator, DIMENSION_CONNECTOR)
    return (num / den) if den else None


def _resolve_gauge(block: dict, ctx: _BlockContext) -> dict:
    """gauge payload: {value,target,target_source,unit,direction,label,delta,delta_pct}.

    v1 supports a ratio metric (CPA) computed as SUM(numerator)/SUM(denominator) over the
    current rows (double-count-safe: each summed on a single breakdown dimension via the
    connector grouping). NEVER divides by zero -> value=null (UI empty state). Target from
    binding.target, else a documented default (_DEFAULT_CPA_TARGET) with target_source.

    Prior-period delta (review-9-3 F-5): when the ratio is computed and the prior window
    has a non-zero denominator, ``delta`` = value - prior_value and ``delta_pct`` = signed
    % change; both are ``None`` (NOT 0) when the prior window is empty / zero-denominator,
    so the UI can distinguish "no comparison" from "no change".
    """
    binding = block.get("binding") or {}
    metric = binding.get("metrics")
    numerator = binding.get("numerator")
    denominator = binding.get("denominator")
    unit = binding.get("unit", "")
    direction = binding.get("direction", "neutral")
    label = block.get("title") or (metric if isinstance(metric, str) else "")

    value: float | None = None
    prior_value: float | None = None
    if numerator and denominator:
        # Ratio (e.g. CPA = cost/conversions). Sum numerator/denominator on the connector
        # grouping so parallel breakdowns are not double-counted, then divide (AD-4). Cross-
        # source rows are deduped so overlapping-source conversions do not inflate the ratio.
        value = _ratio_over_rows(ctx.cross_source_current(), numerator, denominator)
        prior_value = _ratio_over_rows(ctx.cross_source_prior(), numerator, denominator)
    elif isinstance(metric, str) and metric in ctx.rollup:
        value = ctx.rollup[metric].get("value")

    # Prior-period delta -- empty/zero-denominator prior -> null delta (not 0).
    delta: float | None = None
    delta_pct: float | None = None
    if value is not None and prior_value is not None:
        delta = value - prior_value
        if prior_value != 0:
            delta_pct = round((delta / abs(prior_value)) * 100.0, 1)

    if "target" in binding and binding.get("target") is not None:
        target = binding.get("target")
        target_source = "binding"
    else:
        target = _DEFAULT_CPA_TARGET
        target_source = "default"

    return {
        "value": value,
        "prior_value": prior_value,
        "delta": delta,
        "delta_pct": delta_pct,
        "target": target,
        "target_source": target_source,
        "unit": unit,
        "direction": direction,
        "label": label,
    }


def _resolve_funnel(block: dict, ctx: _BlockContext) -> dict:
    """funnel payload: {steps:[{label,value,rate}]} ordered stages with pass-through rate.

    v1: uses the bound ``steps`` (ordered canonical metrics). Each step value = the
    additive total of that metric (connector grouping). rate = value / first-step value
    for the first step is 1.0; for later steps = value / previous-step value. Steps whose
    metric is absent are dropped so a 3-stage funnel degrades to the 2 present stages with
    a clear label. Empty when fewer than 2 stages resolve.
    """
    binding = block.get("binding") or {}
    step_metrics = binding.get("steps") or []
    # Cross-source scalar totals -> deduped rows (overlapping-source conversions counted
    # once, review-9-1 F-5). Falls back to raw rows when only one source is present.
    rows = ctx.cross_source_current()
    resolved: list[tuple[str, float]] = []
    for m in step_metrics:
        if not isinstance(m, str):
            continue
        # Present only when the metric has rows in the current window.
        if not any(r.get("metric") == m for r in rows):
            continue
        resolved.append((m, _sum_metric(rows, m, DIMENSION_CONNECTOR)))

    if len(resolved) < 2:
        return {"steps": []}

    steps: list[dict] = []
    first_val = resolved[0][1]
    prev_val = None
    for label, val in resolved:
        if prev_val is None:
            rate = 1.0
        elif prev_val > 0:
            rate = round(val / prev_val, 4)
        else:
            rate = None  # zero previous stage -> undefined rate (never divide by zero)
        steps.append({"label": label, "value": val, "rate": rate})
        prev_val = val
    # Attach overall conversion rate (last / first) for convenience.
    overall = round(resolved[-1][1] / first_val, 4) if first_val > 0 else None
    return {"steps": steps, "overall_rate": overall}


_OPPORTUNITY_MIN_POSITION = 10.0  # story 9.3: "low position" opportunity threshold (> 10)
_OPPORTUNITY_TOP_N = 5

# Story 10.5: cannibalisation detection thresholds (DEFAULTS ONLY -- never hardcoded as
# the effective value). The effective thresholds are read per-project/per-report from
# ctx.config with these as fallbacks: ctx.config.get("cannib_min_share_pct", 0.20) etc.
_CANNIB_MIN_SHARE_PCT_DEFAULT = 0.20   # a page must hold >= 20% of the query's impressions
_CANNIB_MIN_POSITION_GAP_DEFAULT = 2.0  # best/worst weighted position spread >= 2.0 positions
_CANNIB_MIN_PAGES = 2                    # cannibalisation requires >= 2 competing pages

# Cannibalisation table French column labels (Envelope Contract, Story 10.5).
_CANNIB_COLUMNS = ["Requête", "Page", "Part (%)", "Position moy."]
_CANNIB_EMPTY_LABEL = "Aucune cannibalisation détectée"


def _detect_cannibalisation(ctx: _BlockContext) -> list[dict]:
    """Detect query cannibalisation from the JOINT (query, page) grain (Story 10.5).

    A query is CANNIBALISED when several pages compete for it: >= 2 pages each holding a
    meaningful impression share AND a wide spread in their impression-weighted positions.

    JOINT-GRAIN SOURCE (design decision B, verified against the real code): marginal
    ``breakdown_dimension='query'`` and ``breakdown_dimension='page'`` rows CANNOT express
    which page belongs to which query (each is aggregated over the other axis, losing the
    join). The detection therefore reads the COMPOSITE ``breakdown_dimension='query>page'``
    rows produced by the fact_daily_kpi query>page block (impressions, additive) plus the
    matching composite ``average_position`` rows (impression-weighted per (query,page)).
    The average_position rows do NOT exist in fact_daily_kpi (AD-4 non-additive); get_card
    fetches them from ``semantic_avg_position_composite`` via
    ``warehouse.query_composite_positions`` and splices them onto ``ctx.current_rows``
    BEFORE split_periods on BOTH the ad-hoc and report paths (Story 10.5 F-1). Both metric
    families carry the (query, page) pair co-located in ``breakdown_value``; F-2 decodes it
    with a single maxsplit-1 partition (query first) so a value containing '>' is safe.

    AD-4: position per page is impression-weighted (SUM(pos*impr)/SUM(impr)), NEVER a mean
    and NEVER a SUM of average_position -- identical rule to _resolve_bar_movers.

    Thresholds (ctx.config, defaults applied):
      * cannib_min_share_pct   (default 0.20): min impression share for a page to count.
      * cannib_min_position_gap (default 2.0): min best/worst weighted-position spread.

    Returns a list of dicts sorted by n_pages desc then total impressions desc:
      ``[{query, pages: [{page, share_pct, avg_position}], n_pages, total_impressions}]``.
    Empty list when no query qualifies (degrade-never-raise; the caller renders the
    designed empty table).
    """
    from core import rollup as rollup_module  # noqa: PLC0415
    from core import warehouse as warehouse_module  # noqa: PLC0415

    min_share = float(ctx.config.get("cannib_min_share_pct", _CANNIB_MIN_SHARE_PCT_DEFAULT))
    min_gap = float(ctx.config.get("cannib_min_position_gap", _CANNIB_MIN_POSITION_GAP_DEFAULT))

    composite_dim = "query" + warehouse_module.COMPOSITE_SEPARATOR + "page"  # 'query>page'

    # Gather the composite rows co-locating (query, page). impressions -> share weight;
    # average_position -> per-page weighted position. Both keyed by (query, page).
    impr_by_qp: dict[tuple[str, str], float] = {}
    pos_rows_by_qp: dict[tuple[str, str], list[dict]] = {}
    impr_rows_by_qp: dict[tuple[str, str], list[dict]] = {}
    for r in ctx.current_rows:
        if r.get("breakdown_dimension") != composite_dim:
            continue
        # F-2: decode the query>page value with a SINGLE split on the FIRST separator
        # (maxsplit=1) so a query OR page that itself contains '>' is not truncated. We
        # know this grain is exactly query>page (2 parts, query first), so partition() is
        # correct and avoids the naive split that warehouse.split_composite_value would do
        # (that shared helper is left untouched -- other callers rely on its N-way split).
        raw_value = r.get("breakdown_value") or ""
        query, sep, page = raw_value.partition(warehouse_module.COMPOSITE_SEPARATOR)
        if not sep:  # separator absent -> not a composite value -> skip
            continue
        if not query or not page:
            continue
        key = (query, page)
        metric = r.get("metric")
        if metric == "impressions":
            val = r.get("value")
            if val is not None:
                impr_by_qp[key] = impr_by_qp.get(key, 0.0) + float(val)
            impr_rows_by_qp.setdefault(key, []).append(r)
        elif metric == "average_position":
            pos_rows_by_qp.setdefault(key, []).append(r)

    if not impr_by_qp:
        return []

    # Group pages under each query.
    pages_by_query: dict[str, list[str]] = {}
    for (query, page) in impr_by_qp:
        pages_by_query.setdefault(query, []).append(page)

    flagged: list[dict] = []
    for query, pages in pages_by_query.items():
        query_impr = sum(impr_by_qp.get((query, p), 0.0) for p in pages)
        if query_impr <= 0:
            continue
        page_stats: list[dict] = []
        for page in pages:
            key = (query, page)
            page_impr = impr_by_qp.get(key, 0.0)
            share = page_impr / query_impr if query_impr else 0.0
            # Impression-weighted position per page (AD-4). Feed the position rows PLUS the
            # matching impression rows so weighted_avg_position lines up its weights; a single
            # (query,page,date) staging row already carries its own weighted value.
            weighted_pos = rollup_module.weighted_avg_position(
                pos_rows_by_qp.get(key, []) + impr_rows_by_qp.get(key, [])
            )
            page_stats.append(
                {
                    "page": page,
                    "share_pct": share,
                    "avg_position": weighted_pos,
                    "_impressions": page_impr,
                }
            )
        # Pages meeting the share threshold and carrying a usable weighted position.
        qualifying = [
            s for s in page_stats
            if s["share_pct"] >= min_share and s["avg_position"] is not None
        ]
        if len(qualifying) < _CANNIB_MIN_PAGES:
            continue
        positions = [s["avg_position"] for s in qualifying]
        spread = max(positions) - min(positions)
        # Threshold on the ROUNDED spread so the displayed positions and the filter agree
        # (same rule as the movers bar, review-epic-9-backend F-8).
        if round(spread, 2) < min_gap:
            continue
        qualifying.sort(key=lambda s: s["share_pct"], reverse=True)
        flagged.append(
            {
                "query": query,
                "pages": [
                    {
                        "page": s["page"],
                        "share_pct": s["share_pct"],
                        "avg_position": s["avg_position"],
                    }
                    for s in qualifying
                ],
                "n_pages": len(qualifying),
                "total_impressions": query_impr,
            }
        )

    flagged.sort(key=lambda f: (f["n_pages"], f["total_impressions"]), reverse=True)
    return flagged


def _resolve_table_cannibalisation(ctx: _BlockContext) -> dict:
    """cannibalisation table payload (Story 10.5) -- rows flattened per (query, page).

    Emits one row per competing page of each flagged query. Columns EXACTLY the French
    Envelope Contract labels. Empty selection -> a DESIGNED EMPTY table (rows []) with the
    empty_label -- NEVER absent, NEVER an error (degrade-never-raise, AC 8).
    """
    flagged = _detect_cannibalisation(ctx)
    rows_out: list[dict] = []
    for entry in flagged:
        query = entry["query"]
        for pg in entry["pages"]:
            rows_out.append(
                {
                    "Requête": query,
                    "Page": pg["page"],
                    "Part (%)": round(pg["share_pct"] * 100.0, 1),
                    "Position moy.": round(pg["avg_position"], 2)
                    if pg["avg_position"] is not None
                    else None,
                }
            )
    return {
        "columns": list(_CANNIB_COLUMNS),
        "rows": rows_out,
        "empty_label": _CANNIB_EMPTY_LABEL,
    }


def _resolve_table_opportunities(block: dict, ctx: _BlockContext, dimension: str) -> dict:
    """Opportunités table for the keywords card (review-epic-9-integration F-1).

    "Easy wins": queries with high impressions AND a weak (high) average position -- lots
    of demand the site does not yet rank well for. Selection (canonical, AD-2):
      * impression-weighted average_position per query (AD-4, never a plain mean) over the
        CURRENT window on the canonical partition (via _aggregate_by_group);
      * keep only queries whose weighted position is > _OPPORTUNITY_MIN_POSITION (10);
      * order by IMPRESSIONS desc (the size of the opportunity), top-5.

    Columns EXACTLY: Requête / Impressions / Position moyenne. Empty selection -> a DESIGNED
    EMPTY table (columns present, rows []) so the block is NEVER absent (card rule e).
    Never raises.
    """
    columns = [
        {"key": "_dim", "label": "Requête", "numeric": False},
        {"key": "impressions", "label": "Impressions", "numeric": True},
        {"key": "average_position", "label": "Position moyenne", "numeric": True},
    ]
    positions = _aggregate_by_group(ctx.current_rows, "average_position", dimension)
    impressions = _aggregate_by_group(ctx.current_rows, "impressions", dimension)
    rows_out: list[dict] = []
    for group, pos in positions.items():
        if not group or pos is None:
            continue
        # > 10 threshold (boundary is EXCLUSIVE: exactly 10 is not yet an opportunity).
        if pos <= _OPPORTUNITY_MIN_POSITION:
            continue
        imp = impressions.get(group)
        if not imp:  # need impressions to size (and sort) the opportunity
            continue
        rows_out.append(
            {"_dim": group, "impressions": imp, "average_position": round(pos, 2)}
        )
    rows_out.sort(key=lambda r: r["impressions"], reverse=True)
    rows_out = rows_out[:_OPPORTUNITY_TOP_N]
    return {"columns": columns, "rows": rows_out}


def _resolve_table(block: dict, ctx: _BlockContext) -> dict:
    """table payload: {columns:[{key,label,numeric}], rows:[{...}]} per binding.

    First column = the dimension label; subsequent columns = the bound metrics. Additive
    metrics summed per group; non-additive shown weighted/ratio (AD-4). CPA is derived
    per group as SUM(cost)/SUM(conversions) when both are bound. Rows sorted desc by the
    first additive metric, top-N.

    When ``binding.opportunities is True`` dispatches to _resolve_table_opportunities (the
    keywords "easy wins" table, review-epic-9-integration F-1). AD-2: a binding FLAG drives
    the path, never a template-id branch.

    Story 10.4: when ``binding.connector_scope`` is set, only rows from that connector
    are used (GA4 vs GSC disambiguation). When ``binding.subtotal_share is True``, a
    « Part » column is added whose denominator is the SUM of the returned rows (the top-N
    subtotal), NOT the connector daily total. This is the correct denominator for top-N-
    bounded partitions where the long tail is dropped (percentages sum to ~100% over the
    returned rows, not over the connector day total).
    """
    binding = block.get("binding") or {}
    # Story 10.5: cannibalisation table -- reads the JOINT query>page composite grain
    # directly (its own presence check inside _detect_cannibalisation), so it does NOT go
    # through _first_present_dimension (which only sees single-dim marginals). Empty ->
    # designed empty table with empty_label; never absent, never raises (AC 8).
    if binding.get("cannibalisation"):
        return _resolve_table_cannibalisation(ctx)
    if binding.get("opportunities"):
        dims = binding.get("dimensions") or []
        dimension = _first_present_dimension(ctx.current_rows, list(dims))
        if dimension is None:
            # No usable dimension -> still emit the designed empty opportunities table.
            return {
                "columns": [
                    {"key": "_dim", "label": "Requête", "numeric": False},
                    {"key": "impressions", "label": "Impressions", "numeric": True},
                    {"key": "average_position", "label": "Position moyenne", "numeric": True},
                ],
                "rows": [],
            }
        return _resolve_table_opportunities(block, ctx, dimension)

    # Story 10.4: apply connector scope filter before dimension resolution.
    connector_scope = binding.get("connector_scope")
    src_rows_raw = _filter_rows_by_connector_scope(ctx.current_rows, connector_scope)

    # Story 16.3: apply breakdown_dim_filter (double-count-safe for acquisition axes).
    # AD-2: binding flag, never a template-id branch.
    breakdown_dim_filter = binding.get("breakdown_dim_filter")
    src_rows_raw = _filter_rows_by_breakdown_dim(src_rows_raw, breakdown_dim_filter)

    metrics = binding.get("metrics") or []
    if isinstance(metrics, str):
        metrics = [metrics]
    dims = binding.get("dimensions") or []
    # review-epic-9-backend F-1: pick the present dimension from the raw rows (dimension
    # presence is identical raw vs deduped -- dedup only drops overlapping conversions rows,
    # never a whole breakdown dimension).
    dimension = _first_present_dimension(src_rows_raw, list(dims))
    # Story 16.3: dim_label override from binding (e.g. "Campagne" for attribution table).
    # Falls back to the existing logic (dimension name or "Source") when not set.
    _dim_label_override = binding.get("dim_label")

    if dimension is None or not metrics:
        # Story 10.4: when subtotal_share is True but no rows -> emit designed empty table.
        if binding.get("subtotal_share"):
            _share_label = binding.get("subtotal_share_label", "Part")
            return {
                "columns": [
                    {"key": "_dim", "label": _dim_label_override or "Page", "numeric": False},
                    {"key": metrics[0] if metrics else "value", "label": "Vues", "numeric": True},
                    {"key": "part_pct", "label": _share_label, "numeric": True},
                ],
                "rows": [],
            }
        return {"columns": [], "rows": []}

    # Story 10.4: subtotal_share path — « Part » column = a page's share of the TOP-N
    # SUBTOTAL (SUM over ALL aggregated top-N page rows, computed BEFORE the display cap).
    # So « Part » is "this page's share of total page views", a stable analyst metric
    # (like GA's "% of total"), NEVER a share of the connector daily total (the page
    # partition is top-N-bounded and does not sum to the day total). The subtotal is
    # emitted in the payload for transparency. NOTE: the table then displays only the top
    # `_DEFAULT_TOP_N` rows, so the DISPLAYED rows may sum to < 100 % — that is correct and
    # honest (the remaining top-N pages are the real tail, review 10.4 F-2). AD-2: the
    # binding flag drives the path, never a template-id branch.
    subtotal_share = binding.get("subtotal_share", False)
    if subtotal_share:
        primary_metric = metrics[0]
        _share_label = binding.get("subtotal_share_label", "Part")
        agg = _aggregate_by_group(src_rows_raw, primary_metric, dimension)
        subtotal = sum(v for v in agg.values() if v is not None)
        rows_out: list[dict] = []
        for g, v in agg.items():
            if not g:
                continue
            part_pct = round((v / subtotal) * 100.0, 1) if subtotal else 0.0
            rows_out.append({"_dim": g, primary_metric: v, "part_pct": part_pct})
        rows_out.sort(key=lambda r: (r.get(primary_metric) is None, -(r.get(primary_metric) or 0)))
        rows_out = rows_out[:_DEFAULT_TOP_N]
        return {
            "columns": [
                {"key": "_dim", "label": _dim_label_override or "Page", "numeric": False},
                {
                    "key": primary_metric,
                    "label": "Vues" if primary_metric == "screen_page_views" else primary_metric,
                    "numeric": True,
                },
                {"key": "part_pct", "label": _share_label, "numeric": True},
            ],
            "rows": rows_out,
            "subtotal": subtotal,
        }

    dim_label = _dim_label_override or ("Source" if dimension == DIMENSION_CONNECTOR else dimension)
    columns: list[dict] = [{"key": "_dim", "label": dim_label, "numeric": False}]

    # Per-group aggregates for each requested metric (skip derived cpa here; computed below).
    # review-epic-9-backend F-1: each metric reads its consistent base -- conversions from the
    # DEDUPED set (same as the gauge/rollup/comment), every other metric from the raw rows.
    # Story 10.4: connector_scope rows already filtered to src_rows_raw.
    per_metric: dict[str, dict[str, float]] = {}
    groups: set[str] = set()
    for m in metrics:
        if m == "cpa":
            continue
        # For connector-scoped or breakdown_dim_filtered rows, use src_rows_raw directly.
        scoped = connector_scope or breakdown_dim_filter
        rows_for_m = src_rows_raw if scoped else ctx.rows_for_metric(m)
        agg = _aggregate_by_group(rows_for_m, m, dimension)
        per_metric[m] = agg
        groups.update(agg.keys())
        columns.append({"key": m, "label": m, "numeric": True})

    derive_cpa = "cpa" in metrics
    if derive_cpa:
        columns.append({"key": "cpa", "label": "cpa", "numeric": True})

    rows_out_generic: list[dict] = []
    for g in groups:
        if not g:
            continue
        row: dict[str, Any] = {"_dim": g}
        for m in per_metric:
            row[m] = per_metric[m].get(g)
        if derive_cpa:
            cost = per_metric.get("cost", {}).get(g)
            conv = per_metric.get("conversions", {}).get(g)
            row["cpa"] = (cost / conv) if (cost is not None and conv) else None
        rows_out_generic.append(row)

    # Sort desc by the first additive metric column (fallback: first metric).
    sort_key = next(
        (m for m in metrics if m not in _NON_ADDITIVE_METRICS and m != "cpa"),
        metrics[0] if metrics else "_dim",
    )
    rows_out_generic.sort(key=lambda r: (r.get(sort_key) is None, -(r.get(sort_key) or 0)))
    rows_out_generic = rows_out_generic[:_DEFAULT_TOP_N]
    return {"columns": columns, "rows": rows_out_generic}


def _resolve_comment(block: dict, ctx: _BlockContext, rendered_comment: str) -> dict:
    """comment payload: {text: rendered_comment} (already built, AD-9)."""
    return {"text": rendered_comment}


_BLOCK_RESOLVERS = {
    "kpi_row": _resolve_kpi_row,
    "line": _resolve_line,
    "bar": _resolve_bar,
    "donut": _resolve_donut,
    "gauge": _resolve_gauge,
    "funnel": _resolve_funnel,
    "table": _resolve_table,
}


def resolve_block(block: dict, ctx: _BlockContext, rendered_comment: str) -> dict:
    """Return a shallow COPY of *block* with a resolved ``data`` payload attached.

    DEGRADE, NEVER RAISE (card rule e): any resolver exception -> empty payload so the
    UI renders its designed empty state. Unknown block types get an empty {} payload.
    """
    out = dict(block)
    btype = block.get("type")
    try:
        if btype == "comment":
            out["data"] = _resolve_comment(block, ctx, rendered_comment)
        else:
            resolver = _BLOCK_RESOLVERS.get(btype)
            out["data"] = resolver(block, ctx) if resolver else {}
    except Exception as exc:  # noqa: BLE001 -- a block never breaks the card (rule e)
        logger.debug("resolve_block(%s) degraded to empty: %s", btype, exc)
        out["data"] = {}
    return out


# ---------------------------------------------------------------------------
# Story 9.8 -- Context-kind card resolution (connectors card).
#
# A context card is resolved from APP-LEVEL Postgres sources, NOT the warehouse:
#   * connections + health   -- app.connection_ref + app.connection_health (Story 2.5)
#   * last successful extract -- the extract ledger surface (Story 8.3) per connection
#   * flows each connector feeds -- the report-to-datamodel chain (Story 8.9)
#
# DB down => a DESIGNED EMPTY envelope (never crash). Everything is best-effort and
# project-scoped (AD-5): every query filters on project_id so project B's connections
# never leak into project A's card.
# ---------------------------------------------------------------------------

# The exact column contract the connectors UI is being built against (Story 9.8).
_CONNECTORS_TABLE_COLUMNS: list[dict] = [
    {"key": "connector", "label": "Connecteur", "numeric": False, "sortable": True},
    {"key": "status", "label": "Statut", "numeric": False, "sortable": True},
    {"key": "last_extract", "label": "Dernier extrait", "numeric": False, "sortable": True},
    {"key": "flows", "label": "Flows alimentés", "numeric": False, "sortable": False},
]


def _status_label(status: str | None) -> str:
    """Map a health/ledger status code to its French-first label (Story 9.8).

    Unknown / missing statuses map to "Inconnu" -- never invents a state (AD-9).
    """
    if not status:
        return _TOOROW_STATUS_UNKNOWN
    return _TOOROW_STATUS_LABELS.get(str(status).lower(), _TOOROW_STATUS_UNKNOWN)


def _fetch_connectors_inventory(project_id: str, conn) -> list[dict]:
    """Return the per-connector inventory for a project (Story 9.8).

    Each entry::

        {
          "connector":     <provider label>,           # what the operator recognises
          "connection_id": <conn_ ULID>,
          "status_code":   "ok"|"stale"|"revoked"|...|None,   # raw health status
          "last_extract":  "YYYY-MM-DD"|None,           # last successful extract date
          "last_extract_ts": ISO-8601|None,             # for freshness meta / least-fresh
          "flows":         [ "<module>/<report>", ... ],# reports this connector feeds
        }

    AD-5: the query is project-scoped on app.connection_ref.project_id. AD-3: NEVER
    reads a token column -- only id/provider/health/timestamps.
    """
    # 1) Connections + cached health for the project (Story 2.5 surface). LEFT JOIN so a
    #    connection with no health row yet still appears (status_code -> None -> Inconnu).
    connections: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                r.id,
                r.provider,
                h.status            AS health_status,
                h.last_fetched_at
            FROM app.connection_ref r
            LEFT JOIN app.connection_health h ON h.connection_ref_id = r.id
            -- Portee ORG, pas projet : un credential appartient a une personne mais
            -- est utilisable dans son organisation -- c'est ce qui permet a un
            -- collegue de rafraichir un report sans posseder l'acces a la source.
            -- Filtrer sur r.project_id rendait invisible tout credential branche
            -- depuis un autre projet de la MEME org (architecture-org-tenancy 3.5 :
            -- owner_org_id remplace project_id).
            WHERE r.owner_org_id = (SELECT org_id FROM app.projects WHERE id = %s)
            ORDER BY r.provider ASC, r.created_at ASC
            """,
            (project_id,),
        )
        for row in cur.fetchall():
            connections.append(
                {
                    "connection_id": row[0],
                    "connector": row[1],
                    "status_code": row[2],
                    "last_fetched_at": row[3].isoformat() if row[3] is not None else None,
                }
            )

    if not connections:
        return []

    # 2) Last SUCCESSFUL extract per connection from the pull ledger (Story 8.3 source of
    #    truth: the most recent 'done' pull). Scoped to this project's connections. The
    #    "last extract" is the pull covering the most recent DATA date, NOT the most recently
    #    COMPLETED job (review-epic-9-backend F-5): a backfill can complete recently while
    #    covering an OLD window, so ordering by completed_at would report a stale date_to as
    #    the "last extract" even though fresher data already exists. Order by the job's
    #    data-window end (date_to) first, then completed_at as a tie-break.
    conn_ids = [c["connection_id"] for c in connections]
    last_extract_by_conn: dict[str, tuple[str | None, str | None]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (pj.connection_ref_id)
                pj.connection_ref_id,
                pj.date_to::text        AS last_date,
                pj.completed_at
            FROM app.pull_jobs pj
            WHERE pj.connection_ref_id = ANY(%s)
              AND pj.state = 'done'
            ORDER BY pj.connection_ref_id,
                     pj.date_to DESC NULLS LAST,
                     pj.completed_at DESC NULLS LAST
            """,
            (conn_ids,),
        )
        for row in cur.fetchall():
            last_extract_by_conn[row[0]] = (
                row[1],
                row[2].isoformat() if row[2] is not None else None,
            )

    # 3) Which flows (reports) each connector feeds. Reuse the 8.9 report-to-datamodel
    #    chain: a datastream on this connection maps target fields consumed by report
    #    packs. We derive the connector -> reports edge from the datastreams' module +
    #    the module's report packs (the same relation the chain endpoint exposes).
    flows_by_conn = _fetch_flows_by_connection(project_id, conn_ids, conn)

    inventory: list[dict] = []
    for c in connections:
        cid = c["connection_id"]
        last_date, last_ts = last_extract_by_conn.get(cid, (None, None))
        # Freshness for the "least-fresh" comment prefers the actual extract timestamp;
        # falls back to the health poller's last_fetched_at when no extract exists yet.
        freshness_ts = last_ts or c.get("last_fetched_at")
        inventory.append(
            {
                "connector": c["connector"],
                "connection_id": cid,
                "status_code": c["status_code"],
                "last_extract": last_date,
                "last_extract_ts": freshness_ts,
                "flows": flows_by_conn.get(cid, []),
            }
        )
    return inventory


def _fetch_flows_by_connection(
    project_id: str, conn_ids: list[str], conn
) -> dict[str, list[str]]:
    """Return {connection_id: ["<module>/<report>", ...]} the connector feeds (Story 8.9).

    A datastream belongs to a connection (connection_ref_id) and a module (module_name).
    A connector "feeds" a report when the module owns a report pack (the report-to-
    datamodel relation surfaced by report_chain). We list the module's report ids from
    the loaded modules; when unavailable we fall back to the module name alone so the
    card still shows what the connector powers (degrade, never blank).

    AD-5: only datastreams whose datastream.project_id = project_id are considered.
    """
    if not conn_ids:
        return {}

    # datastream -> (connection, module) edges for this project's connections.
    module_by_conn: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ds.connection_ref_id, ds.module_name
            FROM app.datastreams ds
            WHERE ds.project_id = %s
              AND ds.connection_ref_id = ANY(%s)
              AND ds.enabled = TRUE
            """,
            (project_id, conn_ids),
        )
        for conn_ref_id, module_name in cur.fetchall():
            if not conn_ref_id or not module_name:
                continue
            module_by_conn.setdefault(conn_ref_id, set()).add(module_name)

    # Resolve module -> report ids from the loaded module registry (best-effort). The
    # report ids are the "flows" a report_ref can pin (flow.report base_report_id form).
    reports_by_module = _reports_by_module()

    flows_by_conn: dict[str, list[str]] = {}
    for conn_ref_id, modules in module_by_conn.items():
        flow_labels: list[str] = []
        for mod_name in sorted(modules):
            report_ids = reports_by_module.get(mod_name)
            if report_ids:
                flow_labels.extend(f"{mod_name}/{rid}" for rid in report_ids)
            else:
                # No report pack discoverable -> show the module the connector feeds.
                flow_labels.append(mod_name)
        flows_by_conn[conn_ref_id] = flow_labels
    return flows_by_conn


def _reports_by_module() -> dict[str, list[str]]:
    """Return {module_name: [report_id, ...]} from the loaded module registry.

    Best-effort + fail-soft (module registry unavailable in some unit contexts -> {}).
    Read-only; no module-name hardcodes (AD-2) -- the names come from the loaded packs.
    """
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        modules = list(get_loaded_modules())
    except Exception as exc:  # noqa: BLE001 -- registry absent in unit contexts
        logger.debug("connectors card: module registry unavailable: %s", exc)
        return {}

    out: dict[str, list[str]] = {}
    for mod in modules:
        name = getattr(mod, "name", None)
        if not name:
            continue
        ids: list[str] = []
        for rep in getattr(mod, "reports", None) or []:
            if isinstance(rep, dict) and rep.get("id"):
                ids.append(rep["id"])
        out[name] = ids
    return out


def _least_fresh_connector(inventory: list[dict]) -> dict | None:
    """Return the inventory entry with the OLDEST last extract (the least fresh).

    Entries with NO extract timestamp sort first (they are the least fresh: never
    extracted). Ties broken by connector name. Returns None for an empty inventory.
    """
    if not inventory:
        return None
    return min(
        inventory,
        key=lambda e: (e.get("last_extract_ts") or "", e.get("connector") or ""),
    )


def _resolve_connectors_card(
    tpl: CardTemplate,
    project_id: str,
    *,
    context_events: list[dict] | None,
    alerts: list[dict] | None,
    trace_id: str | None,
) -> tuple[str, dict, str]:
    """Resolve the connectors context card end-to-end (Story 9.8).

    Returns ``(summary, envelope, widget_uri)`` exactly like get_card. The envelope
    matches the UI contract: a Connecteurs table + a comment block. DB down =>
    designed empty envelope (empty rows, empty comment) -- NEVER crashes.
    """
    from core import narrative as narrative_module  # noqa: PLC0415

    inventory: list[dict] = []
    db_ok = True
    try:
        from core import db as _core_db  # noqa: PLC0415

        with _core_db.get_connection() as conn:
            inventory = _fetch_connectors_inventory(project_id, conn)
    except Exception as exc:  # noqa: BLE001 -- DB down => designed empty (never crash)
        db_ok = False
        logger.warning("connectors card: inventory_fetch_failed project=%s: %s", project_id, exc)
        inventory = []

    # Build table rows (the exact UI contract shape).
    rows: list[dict] = []
    for entry in inventory:
        flows = entry.get("flows") or []
        rows.append(
            {
                "connector": entry.get("connector") or "?",
                "status": _status_label(entry.get("status_code")),
                "last_extract": entry.get("last_extract") or "-",
                "flows": ", ".join(flows) if flows else "-",
            }
        )

    # Deterministic cited comment: least-fresh connector (AD-9, story 9.7 pattern).
    least_fresh = _least_fresh_connector(inventory)
    rendered_comment = narrative_module.build_connectors_comment(
        least_fresh=least_fresh,
        connector_count=len(inventory),
    )

    composition = [
        {
            "type": "table",
            "title": "Connecteurs",
            "data": {"columns": list(_CONNECTORS_TABLE_COLUMNS), "rows": rows},
        },
        {"type": "comment", "data": {"text": rendered_comment}},
    ]

    # Freshness / provenance meta from the health data (no pull_ids on a context card).
    # source_system = "app" (the app-level tables the card reads); keep schema-valid.
    fresh_ts = least_fresh.get("last_extract_ts") if least_fresh else None
    provenance = {
        "source_system": "app",
        "source_field": "connection_health",
        "pull_id": None,
        "pull_ids": [],
    }

    card_selection = {
        "chosen": tpl.id,
        "mode": "explicit",  # context cards are always explicit-only
        "answers_question": tpl.answers_question,
        "alternatives": [],
    }

    meta: dict = {
        "freshness": {"last_pull": fresh_ts, "cadence_hours": 24, "stale_since": None},
        "provenance": provenance,
        "alerts": alerts or [],
        "trace_id": trace_id,
        "as_of": None,
        "context_events": _serialize_context_events(context_events),
        "card_selection": card_selection,
        "project_id": project_id,
    }

    data: dict = {
        "card_id": tpl.id,
        "card_type": tpl.id,  # "connectors"
        "title": tpl.title,
        "answers_question": tpl.answers_question,
        "connectors": sorted({e.get("connector") for e in inventory if e.get("connector")}),
        "rendered_comment": rendered_comment,
        "composition": composition,
    }

    envelope = {"schema_version": "1", "meta": meta, "data": data}

    # Traceability (Story 9.2c parity): a context card carries no fact rows / pull_ids,
    # but we still emit the card.* trace so a failing card is observable. row_count = the
    # number of connectors in the inventory.
    _emit_card_trace(
        chosen=tpl,
        selection_mode="explicit",
        current_rows=inventory,
        resolved_metrics=[],
        available_dimensions=set(),
        pull_ids=[],
        project_id=project_id,
        trace_id=trace_id,
    )

    # <=30-line summary (AD-1 text channel). Reuse _build_summary shape.
    summary = _build_summary(tpl, card_selection, rendered_comment, None)
    if not db_ok:
        # Signal degraded resolution in the text channel (ASCII, AI-03) without crashing.
        summary = summary + "\n(inventaire indisponible : base de données injoignable)"
    return summary, envelope, tpl.widget_uri


def _format_rate(rate: float | None) -> str:
    """Format a duplication rate as a human-readable multiplier (e.g. 1.6x).

    Returns '?' when None (never fabricates a value -- AD-9). Rates are published as
    claimed/verified (e.g. 1.6 = régies claim 1.6x the real volume), so we display
    the multiplier with one decimal and the 'x' suffix. French decimal comma.
    """
    if rate is None:
        return "?"
    return f"{rate:.1f}x".replace(".", ",")


def _resolve_dedup_card(
    tpl: CardTemplate,
    project_id: str,
    start: str,
    end: str,
    *,
    context_events: list[dict] | None,
    alerts: list[dict] | None,
    trace_id: str | None,
) -> tuple[str, dict, str]:
    """Resolve the dedup context card end-to-end (Story 17.3).

    Reads marts.dedup_estimate for the given project + window. Builds the envelope
    with a kpi_row (taux de duplication héros), a grouped-bar (claimed vs deduplicated
    per canal), a detail table, and a cited deterministic comment (AD-9).

    Dégradés honnêtes (AD-9 / non-négociable) :
      * Projet sans source désignée (0 lignes) → état « aucune source de vérification
        désignée » (lien console), jamais un chiffre inventé.
      * verified NULL (stripe v1 ou absent) → taux non affiché, jamais 0.
      * Régies BLOCKED → contribution NULL, expliqué.

    Returns (summary, envelope, widget_uri) identical to get_card.
    """
    from core import narrative as narrative_module  # noqa: PLC0415
    from core import warehouse as warehouse_module  # noqa: PLC0415

    # --- Fetch dedup_estimate rows for the window ---------------------------
    rows: list[dict] = []
    try:
        rows = warehouse_module.query_dedup_estimate(project_id, start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dedup card: query_dedup_estimate failed project=%s: %s", project_id, exc
        )

    # --- Aggregate across the window (multi-day) ----------------------------
    # Per-channel sums across the date window; per-project aggregates (verified,
    # claimed_total, rate) are repeated on each channel row -- pick from any row.
    channels: dict[str, dict] = {}   # {channel_connector: {claimed, dedup}}
    verified_total: float | None = None
    claimed_total: float | None = None
    duplication_rate: float | None = None
    verification_source_type: str | None = None
    verification_source_id: str | None = None
    lead_event_name: str | None = None
    pull_ids: list[str] = []

    # verified_total / claimed_total are PER-(project, date) aggregates REPEATED on
    # every channel row of that date (self-describing mart) -- summing them across
    # channel rows would multiply them xN channels (the exact double-count this card
    # exists to expose; caught by test_dedup_multi_channel_values_match_epic_example).
    # Take each date's value ONCE, then sum across dates.
    verified_by_date: dict[str, float] = {}
    claimed_by_date: dict[str, float] = {}

    for row in rows:
        ch = str(row.get("channel_connector") or "")
        if not ch:
            continue
        if ch not in channels:
            channels[ch] = {"claimed": 0.0, "dedup": 0.0, "dedup_null": False}

        claimed_v = row.get("claimed_conversions")
        if claimed_v is not None:
            channels[ch]["claimed"] = channels[ch]["claimed"] + float(claimed_v)

        dedup_v = row.get("deduplicated_contribution")
        if dedup_v is not None:
            channels[ch]["dedup"] = channels[ch]["dedup"] + float(dedup_v)
        else:
            # NULL dedup_contribution → flag it (stripe v1 / absent verified)
            channels[ch]["dedup_null"] = True

        d = str(row.get("date") or "")
        vt = row.get("verified_total")
        if vt is not None:
            verified_by_date[d] = float(vt)  # repeated per channel row -> once per date
        ct = row.get("claimed_total")
        if ct is not None:
            claimed_by_date[d] = float(ct)

        # review-17-3 F-1: provenance is collected on EVERY row (inside the loop --
        # a previous refactor left this block orphaned outside it, losing all but the
        # last pull_id and gating provenance on claimed_by_date).
        if row.get("verification_source_type"):
            verification_source_type = str(row["verification_source_type"])
        if row.get("verification_source_id"):
            verification_source_id = str(row["verification_source_id"])
        if row.get("lead_event_name"):
            lead_event_name = str(row["lead_event_name"])
        if row.get("pull_id"):
            pull_ids.append(str(row["pull_id"]))

    # review-17-3 F-2 (AD-9): the window rate is a ratio-of-sums restricted to the
    # INTERSECTION of dates carrying BOTH sides. A day with claimed but verified NULL
    # (stripe v1, GA4 gap) must NOT keep its claimed in the numerator while missing
    # from the denominator -- that silently inflates the rate on an amputated axis.
    # Totals shown to the user still cover their own full axes; only the RATE is
    # computed on the common-coverage days, and the coverage is exposed.
    common_dates = set(verified_by_date) & set(claimed_by_date)
    if verified_by_date:
        verified_total = sum(verified_by_date.values())
    if claimed_by_date:
        claimed_total = sum(claimed_by_date.values())
    rate_verified = sum(verified_by_date[d] for d in common_dates)
    rate_claimed = sum(claimed_by_date[d] for d in common_dates)
    if common_dates and rate_verified:
        duplication_rate = rate_claimed / rate_verified
    # else: rate stays None (opt-out, or no day carries BOTH sides -- never a fake 0%)
    rate_coverage_days = len(common_dates)
    rate_claimed_days = len(claimed_by_date)

    pull_ids = sorted(set(pull_ids))

    # --- Shopify: best-effort transaction reconciliation (review-17-5 fix 7) --
    # For shopify source, attempt to surface the measured coverage rate from
    # marts.transaction_reconciliation_daily (17.4). Degraded: view absent → skip.
    measured_coverage_rate: float | None = None
    if verification_source_type == "shopify":
        try:
            recon_rows = warehouse_module.query_transaction_reconciliation_daily(
                project_id, start, end
            )
            if recon_rows:
                # Weighted average: coverage_rate = Σ matched / Σ with_txn
                total_matched = sum(
                    float(r.get("matched_count") or 0) for r in recon_rows
                )
                total_with_txn = sum(
                    float(r.get("shopify_orders_with_txn") or 0) for r in recon_rows
                )
                if total_with_txn:
                    measured_coverage_rate = round(
                        (total_matched / total_with_txn) * 100.0, 1
                    )
        except Exception as exc:  # noqa: BLE001
            # Best-effort: never propagate; log only.
            logger.warning(
                "dedup card: query_transaction_reconciliation_daily failed "
                "project=%s: %s",
                project_id, exc,
            )

    # --- Designed-empty state: no source configured (opt-out project) --------
    no_source_configured = len(rows) == 0 or verification_source_type is None
    if no_source_configured:
        # Return a legible, honest envelope with a link to the console.
        designed_empty_comment = (
            "Aucune source de vérification désignée pour ce projet. "
            "Configurez une source dans la console d'administration "
            "pour activer la déduplication estimée."
        )
    else:
        designed_empty_comment = None

    # --- Build composition blocks -------------------------------------------
    # 1. kpi_row: taux de duplication en héros. NULL → state vide (valeur absente).
    kpi_metric_entry: dict = {
        "metric": "duplication_rate",
        "value": duplication_rate,
        "delta": None,
        "delta_pct": None,
        "direction": "down_good",  # lower rate = less double-counting = better
        # AD-9: label obligatoire — toujours visible même quand la valeur n'est pas NULL.
        "estimate_label": "Estimation",
        "unit": "x (réclamé / réel)",
    }
    if duplication_rate is None:
        # Honest empty state: source indisponible (stripe v1, or 0 régies this window).
        kpi_metric_entry["empty_reason"] = (
            "Source de vérification indisponible (données régies non disponibles "
            "ou source 'stripe' non encore livrée)."
            if not no_source_configured
            else "Aucune source de vérification désignée."
        )

    kpi_block = {
        "type": "kpi_row",
        "title": "Taux de duplication (Estimation)",
        "binding": {"metrics": "duplication_rate"},
        "data": {
            "metrics": [kpi_metric_entry],
            "estimate_label": "Estimation",
            "formula": "taux = Σ revendiqué ÷ réel",
        },
    }

    # 2. Bar: 2-bar grouped (claimed vs deduplicated) per channel_connector.
    #    Two separate bar lists, one per metric, in one block (dedup_grouped semantic).
    bars_claimed: list[dict] = []
    bars_dedup: list[dict] = []
    for ch, agg in sorted(channels.items(), key=lambda kv: kv[1]["claimed"], reverse=True):
        bars_claimed.append({"label": ch, "value": agg["claimed"]})
        if not agg["dedup_null"]:
            bars_dedup.append({"label": ch, "value": round(agg["dedup"], 2)})
        else:
            bars_dedup.append({"label": ch, "value": None, "null_reason": "source indisponible"})

    bar_block = {
        "type": "bar",
        "title": "Revendiqué vs dédupliqué par canal (Estimation)",
        "binding": {
            "metrics": ["claimed_conversions", "deduplicated_contribution"],
            "dimensions": ["channel_connector"],
            "dedup_grouped": True,
        },
        "data": {
            "orientation": "horizontal",
            "dimension": "channel_connector",
            "series": [
                {
                    "metric": "claimed_conversions",
                    "label": "Revendiqué",
                    "bars": bars_claimed,
                },
                {
                    "metric": "deduplicated_contribution",
                    "label": "Contribution dédupliquée (Estimation)",
                    "bars": bars_dedup,
                },
            ],
            "estimate_label": "Estimation",
        },
    }

    # 3. Table: Canal / Revendiqué / Contribution dédupliquée / Part.
    #    Part = deduplicated_contribution / verified_total (share of real volume).
    table_rows: list[dict] = []
    for ch, agg in sorted(channels.items(), key=lambda kv: kv[1]["claimed"], reverse=True):
        dedup_val = agg["dedup"] if not agg["dedup_null"] else None
        part_pct: float | None = None
        if dedup_val is not None and verified_total:
            # review-17-5 MEDIUM: "part du réel de la fenêtre" semantics.
            # A channel active only on a SUBSET of window days is still divided by
            # the FULL window's verified_total (sum over ALL days). This dilution is
            # intentional -- the denominator is the total real volume of the window,
            # so the parts of all channels sum to ≤ 100% (never over). The card
            # makes no claim that each channel was active every day.
            part_pct = round((dedup_val / verified_total) * 100.0, 1)
        table_rows.append(
            {
                "_dim": ch,
                "claimed_conversions": agg["claimed"],
                "deduplicated_contribution": round(dedup_val, 2) if dedup_val is not None else None,
                "part_pct": part_pct,
            }
        )

    table_block = {
        "type": "table",
        "title": "Détail par canal",
        "binding": {
            "metrics": ["claimed_conversions", "deduplicated_contribution"],
            "dimensions": ["channel_connector"],
            "dedup_share": True,
        },
        "data": {
            "columns": [
                {"key": "_dim", "label": "Canal", "numeric": False},
                {"key": "claimed_conversions", "label": "Revendiqué", "numeric": True},
                {
                    "key": "deduplicated_contribution",
                    "label": "Contribution dédupliquée (Estimation)",
                    "numeric": True,
                },
                {"key": "part_pct", "label": "Part (%)", "numeric": True},
            ],
            "rows": table_rows,
            "estimate_label": "Estimation",
        },
    }

    # 4. Comment block — build after the other blocks so the builder can cite them.
    _block_data_for_comment = {
        "kpi_row": kpi_block.get("data") or {},
        "bar": bar_block.get("data") or {},
        "table": table_block.get("data") or {},
    }

    if no_source_configured:
        rendered_comment = designed_empty_comment
    else:
        rendered_comment = narrative_module.build_dedup_comment(
            block_data=_block_data_for_comment,
            duplication_rate=duplication_rate,
            verified_total=verified_total,
            claimed_total=claimed_total,
            verification_source_type=verification_source_type,
            lead_event_name=lead_event_name,
            pull_ids=pull_ids,
            context_events=context_events or [],
            rate_coverage_days=rate_coverage_days,
            rate_claimed_days=rate_claimed_days,
            measured_coverage_rate=measured_coverage_rate,
        )

    comment_block = {
        "type": "comment",
        "binding": {"metrics": "*"},
        "data": {"text": rendered_comment or ""},
    }

    composition = [kpi_block, bar_block, table_block, comment_block]

    # --- Provenance + freshness meta ----------------------------------------
    provenance = {
        "source_system": "dedup_estimate",
        "source_field": "marts.dedup_estimate",
        "pull_id": pull_ids[-1] if pull_ids else None,
        "pull_ids": pull_ids,
    }

    card_selection = {
        "chosen": tpl.id,
        "mode": "explicit",
        "answers_question": tpl.answers_question,
        "alternatives": [],
    }

    meta: dict = {
        "freshness": {"last_pull": None, "cadence_hours": 24, "stale_since": None},
        "provenance": provenance,
        "alerts": alerts or [],
        "trace_id": trace_id,
        "as_of": None,
        "context_events": _serialize_context_events(context_events),
        "card_selection": card_selection,
        "project_id": project_id,
    }

    data: dict = {
        "card_id": tpl.id,
        "card_type": tpl.id,
        "title": tpl.title,
        "answers_question": tpl.answers_question,
        "date_range": {"start": start, "end": end},
        "connectors": sorted(channels.keys()),
        "rendered_comment": rendered_comment or "",
        "composition": composition,
        # Dedup-specific metadata (AD-9 provenance on every chiffre).
        "dedup_meta": {
            "verification_source_type": verification_source_type,
            "verification_source_id": verification_source_id,
            "lead_event_name": lead_event_name,
            "duplication_rate": duplication_rate,
            # review-17-3 F-2: the rate is computed on the INTERSECTION of dates
            # carrying both claimed AND verified -- the coverage is exposed so the
            # UI/comment can say "taux sur N/M jours vérifiés" honestly (AD-9).
            "rate_coverage_days": rate_coverage_days,
            "rate_claimed_days": rate_claimed_days,
            "verified_total": verified_total,
            "claimed_total": claimed_total,
            "estimate_label": "Estimation",
            "formula": (
                "taux = Σ revendiqué ÷ réel ; "
                "contribution = revendiqué_canal × réel ÷ Σ revendiqué"
            ),
            "honesty_note": (
                "estimation agrégée, jamais une mesure exacte (sans jointure user-level "
                "BigQuery CAP-14/AD-14)"
            ),
            # review-17-5 fix-7: measured coverage rate from transaction reconciliation
            # (shopify source only; None when view absent or non-shopify source).
            "measured_coverage_rate": measured_coverage_rate,
            # BLOCKED (Phase B): lead_event_name not yet filtering (16.1 AC10)
            "blocked_lead_filter": True,
            # BLOCKED (Phase B): stripe v1 not delivered (15.7)
            "blocked_stripe_v1": True,
        },
    }

    envelope = {"schema_version": "1", "meta": meta, "data": data}

    _emit_card_trace(
        chosen=tpl,
        selection_mode="explicit",
        current_rows=rows,
        resolved_metrics=["duplication_rate", "claimed_conversions", "deduplicated_contribution"],
        available_dimensions={"channel_connector"},
        pull_ids=pull_ids,
        project_id=project_id,
        trace_id=trace_id,
    )

    # Build <=30-line summary (AD-1 text channel).
    summary_lines: list[str] = [
        f"Carte demandée : {tpl.title} -- « {tpl.answers_question} »",
        "",
    ]
    if rendered_comment:
        summary_lines.extend(rendered_comment.splitlines())
    summary = "\n".join(summary_lines[:30])
    return summary, envelope, tpl.widget_uri


def _build_unmapped_actuals_block(
    *, project_id: str, plan_id: str
) -> tuple[dict, str]:
    """Build the « Actuals non mappés » composition block (Story 22.5 / 22.8 E1-F-2).

    Reads the plan-perimeter spend that carries no active ventilation via
    ``mediaplan_mapping.list_unmapped_actuals`` (needs Postgres for the line
    windows/mappings AND the warehouse for the daily spend).

    Best-effort but NEVER silently omitted: if either source is unavailable the block
    is still returned, carrying an honest note « Périmètre non vérifiable (entrepôt
    indisponible) » (AD-9 -- a missing block would falsely read as "100 % mappé").

    Returns ``(block, summary_line)`` -- ``summary_line`` is a single French line
    (<=1 line, AD-1 budget) summarising the perimeter, e.g. "Actuals non mappés :
    2 campagne(s), 40 € (dont hors fenêtre de lignes mappées)" or "... : aucun".
    """
    from core import mediaplan_mapping as _mapping  # noqa: PLC0415

    try:
        from core import db as _db  # noqa: PLC0415

        with _db.get_connection() as _conn:
            result = _mapping.list_unmapped_actuals(_conn, plan_id=plan_id)
        # Defensive: a fake/garbage result (non-dict) is treated as unverifiable.
        if not isinstance(result, dict):
            raise TypeError("list_unmapped_actuals returned a non-dict result")
        unmapped = result.get("unmapped")
        if not isinstance(unmapped, list):
            raise TypeError("list_unmapped_actuals returned no unmapped list")
    except Exception as exc:  # noqa: BLE001 -- honest degrade, never a missing block
        logger.warning(
            "mediaplan_pacing: unmapped_actuals unavailable plan=%s: %s: %s",
            plan_id, type(exc).__name__, exc,
        )
        note = "Périmètre non vérifiable (entrepôt indisponible)"
        block = {
            "type": "table",
            "title": "Actuals non mappés",
            "binding": {"source": "plan_unmapped_actuals"},
            "data": {
                "columns": [
                    {"key": "connector", "label": "Connecteur", "numeric": False},
                    {"key": "campaign_ref", "label": "Campagne", "numeric": False},
                    {"key": "spend", "label": "Dépense réelle (€)", "numeric": True},
                    {"key": "reason", "label": "Motif", "numeric": False},
                ],
                "rows": [],
                "note": note,
                "verifiable": False,
            },
        }
        return block, f"Actuals non mappés : {note}"

    rows: list[dict] = []
    total_spend = 0.0
    has_out_of_window = False
    for u in unmapped:
        spend = u.get("spend")
        spend_f = float(spend) if spend is not None else 0.0
        total_spend += spend_f
        reason = u.get("reason") or _mapping.REASON_UNMAPPED
        if reason == _mapping.REASON_OUT_OF_WINDOW:
            has_out_of_window = True
        rows.append({
            "connector": u.get("connector"),
            "campaign_ref": u.get("campaign_ref"),
            "spend": round(spend_f, 2),
            "reason": reason,
        })

    block = {
        "type": "table",
        "title": "Actuals non mappés",
        "binding": {"source": "plan_unmapped_actuals"},
        "data": {
            "columns": [
                {"key": "connector", "label": "Connecteur", "numeric": False},
                {"key": "campaign_ref", "label": "Campagne", "numeric": False},
                {"key": "spend", "label": "Dépense réelle (€)", "numeric": True},
                {"key": "reason", "label": "Motif", "numeric": False},
            ],
            "rows": rows,
            "verifiable": True,
        },
    }

    if not rows:
        summary_line = "Actuals non mappés : aucun"
    else:
        suffix = " (dont hors fenêtre de lignes mappées)" if has_out_of_window else ""
        summary_line = (
            f"Actuals non mappés : {len(rows)} campagne(s), "
            f"{total_spend:.0f} €{suffix}"
        )
    return block, summary_line


def _resolve_mediaplan_pacing_card(
    tpl: CardTemplate,
    project_id: str,
    plan_id: str,
    *,
    context_events: list[dict] | None,
    alerts: list[dict] | None,
    trace_id: str | None,
) -> tuple[str, dict, str]:
    """Resolve the mediaplan pacing context card end-to-end (Story 22.5).

    Reads the three pacing marts (plan_pacing_by_line / _by_channel / _by_plan)
    via warehouse.query_plan_vs_actual, then fetches plan metadata
    (name, active version) from mediaplan_store.get_plan.

    Dégradés honnêtes (AD-9 / non-négociable) :
      * pace NULL (allocated_to_date=0 ou ligne plan-only) → affiché « — », jamais 0 %
      * lignes plan-only → badge « plan seul », PAS de pace ni de % consommé
      * extrapolated_spend = Estimation (never a measure)
      * WarehouseUnavailable → propagée honnêtement au caller (503)

    Returns (summary, envelope, widget_uri) identical to get_card context resolvers.
    """
    from core import narrative as narrative_module  # noqa: PLC0415
    from core import warehouse as warehouse_module  # noqa: PLC0415

    # --- Fetch plan metadata (label, active version) from Postgres -----------
    plan_meta: dict = {}
    plan_version_id: str | None = None
    plan_name: str | None = None
    as_of_day: str | None = None
    try:
        from core import db as db_module  # noqa: PLC0415
        from core import mediaplan_store as store_module  # noqa: PLC0415
        with db_module.get_connection() as _conn:
            plan_meta = store_module.get_plan(_conn, plan_id=plan_id) or {}
    except Exception as exc:  # noqa: BLE001 -- best-effort; mart rows carry version already
        plan_meta = {}
        logger.warning("mediaplan_pacing: get_plan failed plan=%s: %s", plan_id, exc)
    # AD-5 (review 22.5 F-1): a plan_id belonging to ANOTHER project must never
    # leak its name/version -- treat it exactly like an unknown plan. The mart
    # query below is project-scoped anyway, so the card would be empty; this
    # guard removes the metadata leak. Raised OUTSIDE the best-effort try above.
    if plan_meta and str(plan_meta.get("project_id") or "") != str(project_id):
        raise CardTemplateNotFound("Plan introuvable pour ce projet")
    active = plan_meta.get("active_version") or {}
    plan_version_id = active.get("id") or None
    plan_name = plan_meta.get("name") or None

    # --- Fetch pacing mart rows -----------------------------------------------
    # WarehouseUnavailable is NOT caught here: it propagates to get_card which
    # surfaces it as an honest 503 (AD-9: never a silent empty pacing).
    pacing = warehouse_module.query_plan_vs_actual(
        project_id=project_id, plan_id=plan_id
    )
    lines: list[dict] = pacing.get("lines", [])
    channels: list[dict] = pacing.get("channels", [])
    plan_rows: list[dict] = pacing.get("plan", [])

    # Derive as_of_day from the plan row (honest anchor, data-driven).
    if plan_rows:
        as_of_day = str(plan_rows[0].get("as_of_day") or "") or None

    # Derive plan_version_id from mart rows when Postgres was unavailable.
    if not plan_version_id and lines:
        plan_version_id = str(lines[0].get("plan_version_id") or "") or None
    if not plan_version_id and plan_rows:
        plan_version_id = str(plan_rows[0].get("plan_version_id") or "") or None

    # Collect pull_ids for provenance citations (AD-9).
    pull_ids: list[str] = []
    for row in lines + channels + plan_rows:
        for k in ("actual_pull_id_min", "actual_pull_id_max"):
            v = row.get(k)
            if v:
                pull_ids.append(str(v))
    pull_ids = sorted(set(pull_ids))

    # --- Build line table block -----------------------------------------------
    # Columns: label / channel / budget / consommé% / pace% / reste / extrapolé / badge
    # NULL displayed as None (the UI renders « — »), never 0.
    def _pct(v: float | None) -> float | None:
        """Return value as a percentage float (0.25 -> 25.0) or None."""
        return round(v * 100.0, 1) if v is not None else None

    def _round2(v: float | None) -> float | None:
        return round(v, 2) if v is not None else None

    line_table_rows: list[dict] = []
    for r in lines:
        is_plan_only = bool(r.get("is_plan_only"))
        line_table_rows.append({
            "line_key": r.get("line_key"),
            "label": r.get("label") or r.get("line_key"),
            "channel": r.get("channel"),
            "budget": _round2(r.get("budget")),
            "actual_to_date": _round2(r.get("actual_to_date")),
            "allocated_to_date": _round2(r.get("allocated_to_date")),
            "consumed_pct": _pct(r.get("consumed_pct")),   # None for plan-only
            "pace_pct": _pct(r.get("pace")),                # None when alloc=0 or plan-only
            "remaining_budget": _round2(r.get("remaining_budget")),
            "extrapolated_spend": _round2(r.get("extrapolated_spend")),  # Estimation
            "is_plan_only": is_plan_only,
            "plan_version_id": r.get("plan_version_id"),
            "actual_pull_id_min": r.get("actual_pull_id_min"),
            "actual_pull_id_max": r.get("actual_pull_id_max"),
        })

    line_table_block = {
        "type": "table",
        "title": "Lignes du plan",
        "binding": {"source": "plan_lines"},
        "data": {
            "columns": [
                {"key": "label", "label": "Ligne", "numeric": False},
                {"key": "channel", "label": "Support", "numeric": False},
                {"key": "budget", "label": "Budget (€)", "numeric": True},
                {"key": "actual_to_date", "label": "Dépensé", "numeric": True},
                {"key": "consumed_pct", "label": "Consommé (%)", "numeric": True},
                {"key": "pace_pct", "label": "Pace (%)", "numeric": True},
                {"key": "remaining_budget", "label": "Reste (€)", "numeric": True},
                {
                    "key": "extrapolated_spend",
                    "label": "Extrapolé (€) (Estimation)",
                    "numeric": True,
                },
                {"key": "is_plan_only", "label": "Plan seul", "numeric": False},
            ],
            "rows": line_table_rows,
            # AD-9: extrapolated column carries Estimation label
            "estimate_columns": ["extrapolated_spend"],
            "plan_only_badge_column": "is_plan_only",
        },
    }

    # --- Build channel rollup block -------------------------------------------
    channel_table_rows: list[dict] = []
    for r in channels:
        channel_table_rows.append({
            "channel": r.get("channel"),
            "budget": _round2(r.get("budget")),
            "actual_to_date": _round2(r.get("actual_to_date")),
            "allocated_to_date": _round2(r.get("allocated_to_date")),
            "consumed_pct": _pct(r.get("consumed_pct")),
            "pace_pct": _pct(r.get("pace")),
            "remaining_budget": _round2(r.get("remaining_budget")),
            "extrapolated_spend": _round2(r.get("extrapolated_spend")),
            "plan_only_line_count": r.get("plan_only_line_count"),
            "paceable_line_count": r.get("paceable_line_count"),
            "plan_version_id": r.get("plan_version_id"),
        })

    channel_table_block = {
        "type": "table",
        "title": "Rollup par support",
        "binding": {"source": "plan_channels"},
        "data": {
            "columns": [
                {"key": "channel", "label": "Support", "numeric": False},
                {"key": "budget", "label": "Budget (€)", "numeric": True},
                {"key": "actual_to_date", "label": "Dépensé", "numeric": True},
                {"key": "consumed_pct", "label": "Consommé (%)", "numeric": True},
                {"key": "pace_pct", "label": "Pace (%)", "numeric": True},
                {"key": "remaining_budget", "label": "Reste (€)", "numeric": True},
                {
                    "key": "extrapolated_spend",
                    "label": "Extrapolé (€) (Estimation)",
                    "numeric": True,
                },
            ],
            "rows": channel_table_rows,
            "estimate_columns": ["extrapolated_spend"],
        },
    }

    # --- Build comment block --------------------------------------------------
    plan_only_keys = [r.get("line_key") for r in lines if r.get("is_plan_only")]

    block_data_for_comment = {
        "table": line_table_block.get("data") or {},
        "channel_table": channel_table_block.get("data") or {},
    }

    # Review 22.5 F-3: by_plan carries no days_elapsed -- inject the coverage
    # from the line rollup (max across paceable lines) so the comment can cite
    # "N jour(s) de réel" (leçon 17.5) without changing the mart grain.
    if plan_rows and "days_elapsed" not in plan_rows[0]:
        _elapsed = [
            r.get("days_elapsed")
            for r in lines
            if r.get("days_elapsed") and not r.get("is_plan_only")
        ]
        if _elapsed:
            plan_rows[0]["days_elapsed"] = max(_elapsed)

    rendered_comment = narrative_module.build_mediaplan_pacing_comment(
        block_data=block_data_for_comment,
        plan_id=plan_id,
        plan_name=plan_name,
        plan_version_id=plan_version_id,
        as_of_day=as_of_day,
        plan_rows=plan_rows,
        plan_only_keys=plan_only_keys,
        pull_ids=pull_ids,
        context_events=context_events or [],
    )

    comment_block = {
        "type": "comment",
        "binding": {"source": "plan_pacing"},
        "data": {"text": rendered_comment or ""},
    }

    # --- Build [Unmapped Actuals] block (Story 22.5 / 22.8 E1-F-2) -------------
    # The card MUST surface the plan-perimeter spend that carries no active
    # ventilation. Best-effort: it needs BOTH Postgres (line windows + mappings)
    # AND the warehouse (daily spend). If EITHER is unavailable the block is STILL
    # rendered with an honest note ("Périmètre non vérifiable") -- never omitted
    # silently (AD-9: a missing block would falsely read as "100 % mappé").
    unmapped_block, unmapped_summary = _build_unmapped_actuals_block(
        project_id=project_id, plan_id=plan_id
    )

    composition = [line_table_block, channel_table_block, unmapped_block, comment_block]

    # --- Provenance + freshness meta ------------------------------------------
    provenance: dict = {
        "source_system": "mediaplan_pacing",
        "source_field": "marts.plan_pacing_by_line + plan_pacing_by_channel",
        "plan_id": plan_id,
        "plan_version_id": plan_version_id,
        "pull_ids": pull_ids,
        "pull_id": pull_ids[-1] if pull_ids else None,
    }

    card_selection = {
        "chosen": tpl.id,
        "mode": "explicit",
        "answers_question": tpl.answers_question,
        "alternatives": [],
    }

    meta: dict = {
        "freshness": {"last_pull": None, "cadence_hours": 24, "stale_since": None},
        "provenance": provenance,
        "alerts": alerts or [],
        "trace_id": trace_id,
        "as_of": as_of_day,
        "context_events": _serialize_context_events(context_events),
        "card_selection": card_selection,
        "project_id": project_id,
    }

    channels_list: list[str] = sorted({r.get("channel") or "" for r in lines if r.get("channel")})

    data: dict = {
        "card_id": tpl.id,
        "card_type": tpl.id,
        "title": tpl.title,
        "answers_question": tpl.answers_question,
        "plan_id": plan_id,
        "plan_name": plan_name,
        "plan_version_id": plan_version_id,
        "as_of_day": as_of_day,
        "date_range": {"start": None, "end": as_of_day},
        "connectors": channels_list,
        "rendered_comment": rendered_comment or "",
        "composition": composition,
        # AD-9 pacing metadata on the envelope so LLM can cite it.
        "pacing_meta": {
            "plan_version_id": plan_version_id,
            "pull_ids": pull_ids,
            "as_of_day": as_of_day,
            "estimate_label": "Estimation",
            "pace_formula": "Pace = (réel − prévu_to-date) / prévu_to-date",
            "honesty_note": (
                "extrapolated_spend est une Estimation (run-rate linéaire) ; "
                "jamais une mesure exacte. pace NULL = allocation to-date nulle ou ligne plan seul."
            ),
        },
    }

    envelope = {"schema_version": "1", "meta": meta, "data": data}

    _emit_card_trace(
        chosen=tpl,
        selection_mode="explicit",
        current_rows=[],
        resolved_metrics=["budget", "actual_to_date", "pace", "extrapolated_spend"],
        available_dimensions={"line_key", "channel"},
        pull_ids=pull_ids,
        project_id=project_id,
        trace_id=trace_id,
    )

    # Build <=30-line summary (AD-1 text channel, <=30 lignes LLM).
    summary_lines: list[str] = [
        f"Carte demandée : {tpl.title} — « {tpl.answers_question} »",
        f"Plan : {plan_name or plan_id}  |  Version active : {plan_version_id or '?'}",
        f"Arrêté au : {as_of_day or '?'}",
        # E1-F-2: one honest summary line for [Unmapped Actuals] (<=1 line, AD-1).
        unmapped_summary,
        "",
    ]
    if rendered_comment:
        summary_lines.extend(rendered_comment.splitlines())
    # Add a concise line table (pace + consumed) — capped to keep <=30 lines.
    if lines:
        summary_lines.append("")
        summary_lines.append("Lignes (budget / consommé% / pace%) :")
        for r in line_table_rows[:20]:
            lbl = r.get("label") or r.get("line_key") or "?"
            budget_s = f"{r['budget']:.0f} €" if r.get("budget") is not None else "?"
            cons_s = f"{r['consumed_pct']:.1f}%" if r.get("consumed_pct") is not None else "—"
            pace_s = f"{r['pace_pct']:+.1f}%" if r.get("pace_pct") is not None else "—"
            plan_only_s = " [plan seul]" if r.get("is_plan_only") else ""
            summary_lines.append(
                f"  {lbl}: {budget_s} / consommé {cons_s} / pace {pace_s}{plan_only_s}"
            )
    summary = "\n".join(summary_lines[:30])
    return summary, envelope, tpl.widget_uri


def get_card(
    loaded_modules: list,
    project_id: str,
    *,
    template: str | None = None,
    metrics: list[str] | None = None,
    report_ref: str | None = None,
    date_from: str = "",
    date_to: str = "",
    identity: str = "anonymous",
    context_events: list[dict] | None = None,
    alerts: list[dict] | None = None,
    trace_id: str | None = None,
    today: date | None = None,
    plan_id: str | None = None,  # Story 22.5: required for mediaplan_pacing context card
) -> tuple[str, dict, str]:
    """Resolve a card end-to-end. Returns ``(summary, envelope, widget_uri)``.

    Row resolution (AD-2, source-agnostic):
      * ``report_ref`` ("{module}/{report_id}") -> reuse the report read path
        (warehouse.query_report over the report def's metrics/dimensions/window).
      * ``metrics=[...]`` -> ad-hoc warehouse.query_daily_report filtered to those
        canonical metrics (cross-connector).

    Template selection:
      * explicit ``template`` -> validated; raises CardTemplateNotFound /
        CardTemplateUnsatisfied(missing) when unknown / not satisfiable.
      * omitted -> suggest_template() picks the best fitting template; the choice +
        alternatives ride on ``meta.card_selection`` so the LLM can re-request.

    Commentary (hybrid): the deterministic cited comment (narrative.build_narrative,
    AD-9) goes to ``data.rendered_comment``; R6 metric_definitions +
    llm_commentary_guidelines (flows merge read path) go to the envelope + summary.
    """
    from core import narrative as narrative_module  # noqa: PLC0415
    from core import rollup as rollup_module  # noqa: PLC0415
    from core import warehouse as warehouse_module  # noqa: PLC0415

    start, end = _resolve_window(date_from, date_to, today=today)

    # --- Story 9.8: flow.report preferred card_template -------------------------
    # A flow.report may pin a preferred card_template via flows_upsert. When the caller
    # did NOT pass an explicit template AND a report_ref is given, honor the flow's
    # card_template. An explicit ``template`` argument from the LLM always overrides.
    # Unknown card_template id on the flow => ignore with a structured warning (never
    # break the report). This runs BEFORE the context-card dispatch so a flow can pin a
    # context template too.
    if not template and report_ref and "/" in report_ref:
        flow_tpl = _flow_preferred_template(project_id, report_ref, loaded_modules)
        if flow_tpl:
            if get_template(flow_tpl) is not None:
                template = flow_tpl
            else:
                logger.warning(
                    "get_card: %s",
                    {
                        "event": "flow_card_template_ignored",
                        "reason": "unknown_template",
                        "card_template": flow_tpl,
                        "report_ref": report_ref,
                        "project_id": project_id,
                    },
                )

    # --- Story 9.8: context-kind card dispatch (connectors) ---------------------
    # A context card is resolved from app-level Postgres sources, NOT the warehouse. It
    # is EXPLICIT-ONLY: only reachable via an explicit template id (suggest_template
    # excludes context cards), so we dispatch here before any warehouse query.
    if template:
        _explicit = get_template(template)
        if _explicit is None:
            raise CardTemplateNotFound(template)
        if _explicit.is_context:
            # Story 17.3: the dedup card is a context card resolved from marts.dedup_estimate.
            # Story 22.5: the mediaplan_pacing card reads the plan pacing marts.
            # All other context cards fall through to the connectors resolver (Story 9.8).
            # AD-2: dispatch is data-driven (template id), never a code branch per-feature.
            if _explicit.id == "dedup":
                return _resolve_dedup_card(
                    _explicit,
                    project_id,
                    start,
                    end,
                    context_events=context_events,
                    alerts=alerts,
                    trace_id=trace_id,
                )
            if _explicit.id == "mediaplan_pacing":
                # plan_id MUST be supplied by the caller (Story 22.5 rule).
                # The MCP tool wrapper in main.py passes it as an explicit kwarg.
                if not plan_id:
                    # Review 22.5 F-5: the template EXISTS -- the missing input is
                    # plan_id, so surface the structured unsatisfied error.
                    raise CardTemplateUnsatisfied(
                        "mediaplan_pacing", {"plan_id": "requis"}
                    )
                return _resolve_mediaplan_pacing_card(
                    _explicit,
                    project_id,
                    plan_id,
                    context_events=context_events,
                    alerts=alerts,
                    trace_id=trace_id,
                )
            return _resolve_connectors_card(
                _explicit,
                project_id,
                context_events=context_events,
                alerts=alerts,
                trace_id=trace_id,
            )

    # --- Resolve rows (delta-aware: current + prior window) ---------------------
    resolved_metrics: list[str] = []
    r6_metric_defs: dict | None = None
    r6_guidelines: str | None = None
    connectors_present: list[str] = []

    if report_ref and "/" in report_ref:
        module_name, _, report_def_id = report_ref.partition("/")
        from core import reports as reports_module  # noqa: PLC0415

        report = reports_module.find_report(loaded_modules, module_name, report_def_id)
        if report is not None:
            resolved_metrics = list(report.get("metrics", []))
            report_connectors = report.get("connectors") or None
            all_rows = warehouse_module.query_report(
                project_id,
                module_name,
                resolved_metrics,
                report.get("dimensions", []),
                start,
                end,
                connectors=report_connectors,
            )
            # Story 10.5 F-1 (report path): query_report routes average_position to the
            # MARGINAL semantic_average_position view, NEVER the composite. So the
            # 'query>page' impression-weighted positions are absent here too. When the
            # effective template (explicit or flow-preferred, both already folded into
            # `template` above) carries the cannibalisation binding, splice the composite
            # positions from semantic_avg_position_composite the same way as the ad-hoc
            # path. Degrade-never-raise: [] -> designed empty table.
            if _needs_cannibalisation_positions(template):
                _present_connectors = report_connectors or sorted(
                    {r.get("connector") for r in all_rows if r.get("connector")}
                )
                _composite_dim = "query" + warehouse_module.COMPOSITE_SEPARATOR + "page"
                _composite_pos_rows = warehouse_module.query_composite_positions(
                    project_id,
                    _composite_dim,
                    start,
                    end,
                    _present_connectors or None,
                )
                if _composite_pos_rows:
                    all_rows = list(all_rows) + _composite_pos_rows
        else:
            all_rows = []
        # R6: pull metric_definitions + llm_commentary_guidelines via the flows merge
        # read path (identical to get_report). Best-effort: DB down -> no definitions.
        r6_metric_defs, r6_guidelines = _fetch_r6(project_id, report_ref, loaded_modules)
    else:
        resolved_metrics = [m.strip() for m in (metrics or []) if m and m.strip()]
        all_rows = warehouse_module.query_daily_report(
            project_id, start, end, None
        )
        # Story 10.5 F-1 (CRITICAL): fact_daily_kpi NEVER carries average_position (AD-4
        # non-additive, test_composite_additive_only.sql forbids it), so the composite
        # 'query>page' impression-weighted positions the cannibalisation detector needs
        # are ABSENT from query_daily_report. Fetch them from semantic_avg_position_composite
        # and splice them onto all_rows BEFORE split_periods -- but ONLY when the card in
        # play actually reads them (the keywords card's cannibalisation block). Narrowest
        # trigger: an EXPLICIT template whose composition carries a `cannibalisation`
        # binding. On the ad-hoc suggested path average_position is never surfaced from
        # fact, so keywords is never auto-suggested -> no other card pays this query, and
        # NON-keywords cards never receive these composite rows. Degrade-never-raise: [] ->
        # the block renders its designed empty table.
        if _needs_cannibalisation_positions(template):
            _present_connectors = sorted(
                {r.get("connector") for r in all_rows if r.get("connector")}
            )
            _composite_dim = "query" + warehouse_module.COMPOSITE_SEPARATOR + "page"
            _composite_pos_rows = warehouse_module.query_composite_positions(
                project_id,
                _composite_dim,
                start,
                end,
                _present_connectors or None,
            )
            if _composite_pos_rows:
                all_rows = list(all_rows) + _composite_pos_rows
        all_rows_unfiltered = all_rows
        if resolved_metrics:
            wanted = set(resolved_metrics)
            all_rows = [r for r in all_rows if r.get("metric") in wanted]
        # R6 for the ad-hoc path: resolve guidelines from the module pack whose metrics
        # overlap best, via the project's stored override for that report. Best-effort:
        # any failure -> (None, None). Never crashes (spec: "else omit cleanly").
        r6_metric_defs, r6_guidelines = _fetch_r6_adhoc(
            project_id, resolved_metrics, loaded_modules
        )

    current_rows, prior_rows = rollup_module.split_periods(all_rows, start, end)
    connectors_present = sorted({r.get("connector") for r in current_rows if r.get("connector")})

    available_metrics, available_dimensions = _available_inputs(current_rows)
    # When we know the requested metrics but rows are empty (unpopulated mart), still
    # let suggestion see the requested canonical metrics so KPI is offered (empty state).
    if not available_metrics and resolved_metrics:
        available_metrics = set(resolved_metrics)

    # --- Template selection -----------------------------------------------------
    alternatives: list[CardTemplate] = []
    if template:
        chosen = get_template(template)
        if chosen is None:
            raise CardTemplateNotFound(template)
        if not chosen.is_satisfied_by(available_metrics, available_dimensions):
            raise CardTemplateUnsatisfied(
                template,
                _missing_inputs(chosen, available_metrics, available_dimensions),
            )
        _, alternatives = suggest_template(available_metrics, available_dimensions)
        alternatives = [t for t in alternatives if t.id != chosen.id]
        selection_mode = "explicit"
    else:
        chosen, alternatives = suggest_template(available_metrics, available_dimensions)
        selection_mode = "suggested"
        if chosen is None:
            # No metric at all -> fall back to KPI so the card still renders an empty state.
            chosen = get_template("kpi")
            selection_mode = "fallback_empty"

    # Ad-hoc metrics path: re-include rows for the chosen template's DECLARED metrics
    # (required + optional) that the caller's ``metrics`` filter dropped -- e.g. the
    # keywords movers bar reads the OPTIONAL average_position (story 9.3). Selection
    # above intentionally saw only the caller-requested metrics; the declared blocks
    # then render from the full declared inputs. report_ref path is already scoped
    # by the report definition and never expanded.
    if resolved_metrics and not (report_ref and "/" in report_ref):
        declared = {
            m
            for m in (*chosen.required_metrics, *chosen.optional_metrics)
            if m and m != ANY_METRIC
        }
        if declared - set(resolved_metrics):
            expanded_wanted = set(resolved_metrics) | declared
            current_rows, prior_rows = rollup_module.split_periods(
                [r for r in all_rows_unfiltered if r.get("metric") in expanded_wanted],
                start,
                end,
            )

    # --- Rollup + deterministic cited comment (AD-9) ----------------------------
    pull_ids = sorted({r.get("pull_id") for r in current_rows if r.get("pull_id")})
    # AD-4-conversions (review-9-1 F-5): the CROSS-SOURCE scalar total (the rollup the
    # narrative cites, and the funnel/gauge scalars) must not double-count conversions when
    # several connectors report them for the same (project, date). Apply the SAME
    # priority-source dedup (Rule P, declarative in the dbt seed) get_daily_report uses --
    # BEFORE aggregating cross-source totals.
    #
    # review-epic-9-backend F-1 / review-epic-9-integration F-2 (UPDATED RATIONALE): the
    # per-source conversions donut/table now ALSO read the DEDUPED rows (via
    # _BlockContext.rows_for_metric). Architecture invariant story 3.7 "no metric counted
    # twice" wins over "show every source raw": a conversion reported by two overlapping
    # sources appears ONCE, under its priority source. This makes the donut total provably
    # equal the gauge/rollup denominator, so the cited comment (source share % + CPA) is internally
    # consistent. Non-conversions per-source metrics (cost, clicks, active_users, ...) are
    # untouched by apply_conversions_dedup, so the raw==deduped for them.
    from core import metrics as metrics_module  # noqa: PLC0415

    rollup_current = metrics_module.apply_conversions_dedup(current_rows)
    rollup_prior = metrics_module.apply_conversions_dedup(prior_rows)
    rollup = rollup_module.compute_rollup(
        rollup_current + rollup_prior, resolved_metrics or sorted(available_metrics),
        start, end, project_id, pull_ids,
    )

    # --- Sparkline series per metric --------------------------------------------
    series = {
        m: _sparkline_series(current_rows, m, start, end)
        for m in (resolved_metrics or sorted(available_metrics))
    }

    # --- Per-block data resolution (Stories 9.3-9.6) ----------------------------
    # Each declared block is augmented with its resolved ``data`` payload so the UI
    # renderer reads block.data. Degrade-never-raise is handled inside resolve_block.
    block_ctx = _BlockContext(
        current_rows=current_rows,
        prior_rows=prior_rows,
        rollup=rollup,
        metric_definitions=r6_metric_defs,
        resolved_metrics=resolved_metrics or sorted(available_metrics),
        start=start,
        end=end,
        # Cross-source scalar blocks (funnel/gauge) read the deduped sets so overlapping-
        # source conversions are counted once; per-source blocks keep the raw rows above.
        dedup_current_rows=rollup_current,
        dedup_prior_rows=rollup_prior,
        # Story 10.5: per-project/per-report config (cannibalisation thresholds) from the
        # report override doc. Best-effort merge; empty on the pure ad-hoc path (defaults apply).
        config=_fetch_card_config(project_id, report_ref, resolved_metrics, loaded_modules),
    )

    # --- Story 9.7: per-card deterministic cited comment (AD-9) ------------------
    # Two-phase approach: first resolve NON-comment blocks to collect their data
    # payloads, then call the per-card builder (which cites those EXACT numbers),
    # then pass the resulting comment to the comment block resolver.
    #
    # Dispatch is DATA-DRIVEN (AD-2): CARD_COMMENT_BUILDERS[template_id] -> builder.
    # Cards without a builder (kpi, future cards) fall back to build_narrative.
    _non_comment_blocks = [b for b in chosen.composition if b.get("type") != "comment"]
    _resolved_non_comment = [resolve_block(b, block_ctx, "") for b in _non_comment_blocks]
    # Collect resolved payloads keyed by block type for the per-card builder.
    # Story 10.5: a card can have several blocks of the same TYPE (keywords has three
    # ``table`` blocks). Flag-bearing blocks are keyed by their DISCRIMINATOR (the binding
    # flag) so the comment builder can find them unambiguously -- e.g. the cannibalisation
    # table lands under _block_data["cannibalisation"], not the generic "table" slot that
    # the first table (Top requêtes) already occupies.
    _block_data: dict[str, dict] = {}
    for _rb, _src in zip(_resolved_non_comment, _non_comment_blocks):
        _binding = _src.get("binding") or {}
        _key = None
        if _binding.get("cannibalisation"):
            _key = "cannibalisation"
        elif _binding.get("opportunities"):
            _key = "opportunities"
        elif _rb.get("type") == "donut":
            # Story 10.2: when multiple donut blocks exist (e.g. user_type + device_category),
            # key each by its first binding dimension so per-card builders can address them
            # unambiguously. The first-wins generic "donut" key still holds the FIRST donut
            # (preserves backward compatibility for builders that read _block_data["donut"]).
            # e.g. user_type donut -> "donut_user_type"; device donut -> "donut" (first-wins).
            _dims = _binding.get("dimensions") or []
            _dim_key = _dims[0] if _dims else None
            if _dim_key:
                _key = f"donut_{_dim_key}"
            else:
                _key = "donut"
        elif _rb.get("type") == "bar":
            # Story 16.3 (Attribution): when multiple bar blocks exist (last-click + first-
            # click), key each by its breakdown_dim_filter so builders can address them
            # unambiguously. The first-wins generic "bar" key holds the FIRST bar (backward
            # compat for journey / keywords builders that read _block_data["bar"]).
            _bdf = _binding.get("breakdown_dim_filter")
            _bar_dims = _binding.get("dimensions") or []
            _bar_dim_key = _bdf or (_bar_dims[0] if _bar_dims else None)
            if _bar_dim_key:
                _key = f"bar_{_bar_dim_key}"
            else:
                _key = "bar"
        else:
            _key = _rb.get("type")
        if _key and _key not in _block_data:
            _block_data[_key] = _rb.get("data") or {}
        # Also store under the generic "donut" key for the FIRST donut (backward compat).
        if _rb.get("type") == "donut" and "donut" not in _block_data:
            _block_data["donut"] = _rb.get("data") or {}
        # Also store under the generic "bar" key for the FIRST bar (backward compat).
        if _rb.get("type") == "bar" and "bar" not in _block_data:
            _block_data["bar"] = _rb.get("data") or {}

    per_card_builder = narrative_module.CARD_COMMENT_BUILDERS.get(chosen.id)
    if per_card_builder is not None:
        try:
            rendered_comment = per_card_builder(
                block_data=_block_data,
                rollup=rollup,
                context_events=context_events or [],
                pull_ids=pull_ids,
            )
        except Exception as exc:  # noqa: BLE001 -- builder never crashes get_card (rule e)
            logger.debug(
                "get_card: per-card builder for %s degraded to generic: %s", chosen.id, exc
            )
            rendered_comment = narrative_module.build_narrative(
                project_id=project_id,
                report_id=report_ref or f"card/{chosen.id}",
                rollup=rollup,
                context_events=context_events or [],
                alerts=alerts or [],
                as_of=None,
                narrative_prompt=None,
            )
    else:
        # Generic build_narrative for kpi and any future card without a dedicated builder.
        rendered_comment = narrative_module.build_narrative(
            project_id=project_id,
            report_id=report_ref or f"card/{chosen.id}",
            rollup=rollup,
            context_events=context_events or [],
            alerts=alerts or [],
            as_of=None,
            narrative_prompt=None,
        )

    # Assemble final resolved_composition: previously-resolved non-comment blocks
    # + comment blocks resolved with the now-finalized rendered_comment.
    _non_comment_iter = iter(_resolved_non_comment)
    resolved_composition: list[dict] = []
    for block in chosen.composition:
        if block.get("type") == "comment":
            resolved_composition.append(resolve_block(block, block_ctx, rendered_comment))
        else:
            resolved_composition.append(next(_non_comment_iter))

    # --- Assemble the AD-1 envelope shaped for the card -------------------------
    loaded_ats = [str(r.get("loaded_at")) for r in current_rows if r.get("loaded_at")]
    last_pull = max(loaded_ats) if loaded_ats else None
    provenance = {
        "source_system": (connectors_present[0] if connectors_present else None),
        "source_field": "fact_daily_kpi",
        "pull_id": pull_ids[-1] if pull_ids else None,
        "pull_ids": pull_ids,
    }

    card_selection = {
        "chosen": chosen.id,
        "mode": selection_mode,
        "answers_question": chosen.answers_question,
        "alternatives": [
            {"id": t.id, "answers_question": t.answers_question} for t in alternatives
        ],
    }

    meta: dict = {
        "freshness": {"last_pull": last_pull, "cadence_hours": 24, "stale_since": None},
        "provenance": provenance,
        "alerts": alerts or [],
        "trace_id": trace_id,
        "as_of": None,
        "context_events": _serialize_context_events(context_events),
        "card_selection": card_selection,
        "project_id": project_id,
    }

    data: dict = {
        "card_id": chosen.id,
        "card_type": chosen.id,  # Story 9.2c: explicit card_type for traceability
        "title": chosen.title,
        "answers_question": chosen.answers_question,
        "date_range": {"start": start, "end": end},
        "connectors": connectors_present,
        "metrics": rollup,  # {metric: {value, delta, delta_pct, ...}} -- delta-aware (R6)
        "series": series,  # {metric: [{date, value}]} -- sparkline
        "rendered_comment": rendered_comment,  # AD-9 cited what+why
        # Story 9.2c + 9.3-9.6: resolved composition -- ordered blocks each carrying
        # its ``data`` payload so the renderer maps block type -> viz primitive and reads
        # block.data. Mirrors the template's declared composition, augmented with data.
        "composition": resolved_composition,
    }
    if r6_metric_defs:
        data["metric_definitions"] = r6_metric_defs
    if r6_guidelines:
        data["llm_commentary_guidelines"] = r6_guidelines

    envelope = {"schema_version": "1", "meta": meta, "data": data}

    # --- Story 9.2c: Backend traceability (card.* span attributes + structured log) ---
    # Attach card.* attributes to the ambient OTel span (created by TracingMiddleware in
    # main.py). record_current_span_attributes is a no-op when tracing is disabled (AC10).
    # The structured logger.info is ALWAYS emitted (even when OTel is off) for debuggability.
    _emit_card_trace(
        chosen=chosen,
        selection_mode=selection_mode,
        current_rows=current_rows,
        resolved_metrics=resolved_metrics or sorted(available_metrics),
        available_dimensions=available_dimensions,
        pull_ids=pull_ids,
        project_id=project_id,
        trace_id=trace_id,
    )

    # --- The <=30-line summary (AD-1 text channel) ------------------------------
    summary = _build_summary(chosen, card_selection, rendered_comment, r6_guidelines)

    return summary, envelope, chosen.widget_uri


def _emit_card_trace(
    *,
    chosen: CardTemplate,
    selection_mode: str,
    current_rows: list[dict],
    resolved_metrics: list[str],
    available_dimensions: set[str],
    pull_ids: list[str],
    project_id: str,
    trace_id: str | None = None,
) -> None:
    """Emit card.* span attributes + a structured INFO log (Story 9.2c).

    OTel span: attached to the ambient span from TracingMiddleware (main.py) via
    ``tracing.record_current_span_attributes``. No new span is created here. No-op
    when tracing is disabled.

    Structured log: always emitted regardless of OTel state so debugging works without
    an exporter. ASCII-only (AI-03). Never logs tokens/secrets (AD-3).
    """
    composition_types = ",".join(b.get("type", "") for b in chosen.composition)
    metrics_str = ",".join(sorted(resolved_metrics))
    dims_str = ",".join(sorted(available_dimensions))
    pull_ids_str = ",".join(pull_ids)
    row_count = len(current_rows)

    attrs: dict[str, Any] = {
        "card.template_id": chosen.id,
        "card.selection_mode": selection_mode,
        "card.composition": composition_types,
        "card.metrics": metrics_str,
        "card.dimensions": dims_str,
        "card.row_count": row_count,
        "card.pull_ids": pull_ids_str,
        "project_id": project_id,
    }
    # trace_id correlates the card span with the MCP caller's trace (Story 9.2c spec /
    # review-9-2b F-9). Only set on the span when present -- OTel rejects None attributes.
    if trace_id:
        attrs["trace_id"] = trace_id

    # Always emit a structured log for debuggability even without OTel (ASCII, no secrets).
    logger.info(
        "get_card: %s",
        {
            "event": "get_card",
            "card.template_id": chosen.id,
            "card.selection_mode": selection_mode,
            "card.composition": composition_types,
            "card.metrics": metrics_str,
            "card.dimensions": dims_str,
            "card.row_count": row_count,
            "card.pull_ids": pull_ids_str,
            "project_id": project_id,
            "trace_id": trace_id,
        },
    )

    # Attach to ambient OTel span (no-op when disabled / no span active).
    try:
        from core import tracing as tracing_module  # noqa: PLC0415

        if tracing_module.is_enabled():
            tracing_module.record_current_span_attributes(attrs)
    except Exception as exc:  # noqa: BLE001 -- tracing never crashes a tool path (T2.2)
        logger.debug("get_card: tracing.record_current_span_attributes failed: %s", exc)


def _flow_preferred_template(
    project_id: str, report_ref: str, loaded_modules: list
) -> str | None:
    """Return the flow.report's preferred ``card_template`` id, or None (Story 9.8).

    Reads the merged report flow doc (base pack + stored override) via the flows public
    read path and returns its ``card_template`` field when set. Best-effort: DB down /
    no override -> None (the report renders with the default suggestion). Never raises.
    """
    try:
        from core import db as _core_db  # noqa: PLC0415
        from core import flows as _flows_module  # noqa: PLC0415

        with _core_db.get_connection() as conn:
            merged = _flows_module.fetch_report_override_merged(
                project_id, report_ref, loaded_modules, conn
            )
        val = merged.get("card_template")
        return val.strip() if isinstance(val, str) and val.strip() else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_card: flow_card_template_fetch_skipped: %s", exc)
        return None


def _fetch_r6(
    project_id: str, report_ref: str, loaded_modules: list
) -> tuple[dict | None, str | None]:
    """Fetch R6 metric_definitions + llm_commentary_guidelines via the flows merge path.

    Mirrors get_report exactly. Best-effort: any failure -> (None, None).
    Used by the report_ref path in get_card.
    """
    try:
        from core import db as _core_db  # noqa: PLC0415
        from core import flows as _flows_module  # noqa: PLC0415

        # AI-02: use the public flows read wrapper (no private-attribute reach-in).
        with _core_db.get_connection() as conn:
            merged = _flows_module.fetch_report_override_merged(
                project_id, report_ref, loaded_modules, conn
            )
        defs = merged.get("metric_definitions") or None
        guidelines = (merged.get("llm_commentary_guidelines") or "").strip() or None
        return defs, guidelines
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_card: r6_fetch_skipped: %s", exc)
        return None, None


def _fetch_r6_adhoc(
    project_id: str, resolved_metrics: list[str], loaded_modules: list
) -> tuple[dict | None, str | None]:
    """Fetch R6 guidelines for the ad-hoc (no report_ref) path in get_card.

    Strategy: scan loaded_modules for a report pack whose metrics overlap the
    resolved_metrics, then attempt to pull the project's stored override for that
    report. This lets an operator who has configured guidelines via flows_upsert on
    a related report still have them surface on the ad-hoc card path.

    If no matching report is found, or no override exists, or the DB is down:
    return (None, None) cleanly (never crash — spec: "else omit cleanly").

    Note: module packs do not currently define metric_definitions or
    llm_commentary_guidelines in the base doc (flows.py: "R6 fields come from the
    override only"). So metric_definitions is always None on this path today; the
    function is forward-compatible for when packs add them.
    """
    try:
        if not loaded_modules or not resolved_metrics:
            return None, None

        from core import db as _core_db  # noqa: PLC0415
        from core import flows as _flows_module  # noqa: PLC0415

        wanted = set(resolved_metrics)
        best_ref: str | None = None
        best_overlap = 0

        # Find the module report pack with the highest metric overlap.
        for mod in loaded_modules:
            mod_name = getattr(mod, "name", None) or ""
            for rep in getattr(mod, "reports", None) or []:
                if not isinstance(rep, dict):
                    continue
                rep_metrics = set(rep.get("metrics") or [])
                overlap = len(rep_metrics & wanted)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_ref = f"{mod_name}/{rep.get('id', '')}"

        if not best_ref or best_overlap == 0:
            return None, None

        with _core_db.get_connection() as conn:
            merged = _flows_module.fetch_report_override_merged(
                project_id, best_ref, loaded_modules, conn
            )
        defs = merged.get("metric_definitions") or None
        guidelines = (merged.get("llm_commentary_guidelines") or "").strip() or None
        return defs, guidelines
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_card: r6_adhoc_fetch_skipped: %s", exc)
        return None, None


_CARD_CONFIG_KEYS = ("cannib_min_share_pct", "cannib_min_position_gap")


def _fetch_card_config(
    project_id: str,
    report_ref: str | None,
    resolved_metrics: list[str],
    loaded_modules: list,
) -> dict:
    """Return the per-project/per-report config dict for card resolvers (Story 10.5).

    Reads the merged report override doc (base pack + app.report_overrides via the flows
    public read path) and extracts the top-level config keys card resolvers honor -- today
    the cannibalisation thresholds (cannib_min_share_pct, cannib_min_position_gap). Same
    top-level-of-the-merged-doc convention as the R6 fields (metric_definitions,
    llm_commentary_guidelines).

    report_ref path -> uses that exact report doc. Ad-hoc path -> reuses the best
    metric-overlap report (identical strategy to _fetch_r6_adhoc) so an operator who set
    thresholds on a related report still has them applied. Best-effort: DB down / no
    override / no matching report -> empty dict (defaults then apply in the resolvers).
    Never raises.
    """
    try:
        from core import db as _core_db  # noqa: PLC0415
        from core import flows as _flows_module  # noqa: PLC0415

        ref = report_ref if (report_ref and "/" in report_ref) else None
        if ref is None:
            # Ad-hoc: find the report pack with the highest metric overlap (as _fetch_r6_adhoc).
            wanted = set(resolved_metrics or [])
            best_overlap = 0
            for mod in loaded_modules or []:
                mod_name = getattr(mod, "name", None) or ""
                for rep in getattr(mod, "reports", None) or []:
                    if not isinstance(rep, dict):
                        continue
                    overlap = len(set(rep.get("metrics") or []) & wanted)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        ref = f"{mod_name}/{rep.get('id', '')}"
            if not ref or best_overlap == 0:
                return {}

        with _core_db.get_connection() as conn:
            merged = _flows_module.fetch_report_override_merged(
                project_id, ref, loaded_modules, conn
            )
        return {k: merged[k] for k in _CARD_CONFIG_KEYS if k in merged}
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_card: card_config_fetch_skipped: %s", exc)
        return {}


def _build_summary(
    chosen: CardTemplate,
    card_selection: dict,
    rendered_comment: str,
    guidelines: str | None,
) -> str:
    """Build the <=30-line LLM summary for a card (AD-1 text channel).

    Carries: which card was chosen + the question it answers, the deterministic cited
    comment (so the claim/citation survive), and -- when present -- the R6 commentary
    guidelines inviting Claude to deepen per the user's actual question.
    """
    lines: list[str] = []
    verb = {
        "explicit": "Carte demandée",
        "suggested": "Carte suggérée",
        "fallback_empty": "Carte par défaut",
    }.get(card_selection.get("mode", ""), "Carte")
    lines.append(f"{verb} : {chosen.title} -- « {chosen.answers_question} »")
    if card_selection.get("alternatives"):
        alt_ids = ", ".join(a["id"] for a in card_selection["alternatives"])
        lines.append(f"Alternatives disponibles : {alt_ids}.")
    lines.append("")
    lines.extend(rendered_comment.splitlines())
    if guidelines:
        lines.append("")
        lines.append(f"Directives de commentaire : {guidelines}")
    # AD-1 <=30-line budget. When the content overflows, keep the first 29 lines and add
    # an explicit "[tronque]" marker (ASCII, AI-03) so the LLM knows content was dropped
    # rather than silently losing the tail (review-9-1 F-9, matches get_daily_report).
    if len(lines) > 30:
        return "\n".join(lines[:29] + ["[tronque]"])
    return "\n".join(lines)
