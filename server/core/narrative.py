"""toorow — deterministic what+why narrative builder (Story 6.4, AC1-AC4;
per-card builders added in Story 9.7).

This module produces a deterministic, template-based what+why skeleton from
structured inputs. It is NOT an LLM call. Its output is the text placed in the
``summary`` channel by ``get_report`` and ``get_daily_report``. The LLM client
(Claude) then interprets this text, guided by the report's ``narrative_prompt``.

The STRUCTURE and CITATIONS are fixed by this code — that is how "every claim
cited" (FR7) is satisfied without any server-side LLM call.

Story 9.7 — per-card builders:
  Each business-card template (keywords, conversions, usertypes, journey) gets a
  DEDICATED deterministic builder that cites its card-specific mover/winner/segment.
  Builders are registered in ``CARD_COMMENT_BUILDERS`` (a dict keyed by template id
  — AD-2: no module branches, data-driven dispatch). ``get_card`` in cards.py calls
  the per-card builder when one is registered, and falls back to ``build_narrative``
  for cards without a specific builder (kpi, future cards).

  Every builder:
    * produces <= 3 lines
    * cites numbers with provenance (connector:fact + pull_id style)
    * NEVER states a cause absent from the provided context_events (AD-9):
      emits "Contexte manquant pour cette période." when context_events == []

Builder signature::

    def build_<card>_comment(
        *,
        block_data: dict,         -- pre-resolved block payloads keyed by block type
        rollup: dict,             -- {metric: {value, delta, delta_pct, ...}}
        context_events: list[dict],
        pull_ids: list[str],
    ) -> str

  ``block_data`` carries the resolved payloads produced by the block resolvers in
  cards.py — e.g. block_data["bar"]["bars"], block_data["donut"]["slices"],
  block_data["gauge"]["value"] / "delta". This lets each builder cite the EXACT
  numbers the card renders, guaranteeing comment ↔ card consistency.

# AD-1 ENFORCEMENT: This module has zero imports from warehouse, modules, or BigQuery.
# It receives only pre-computed rollups, context events, alert dicts, and resolved
# block payloads. Raw rows never enter this file. Enforce by code review AND by the
# CI guard (make check-narrative-no-raw): any import of the warehouse module or any
# raw mart/table query in this file is a blocker.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# French metric labels (UX-DR10 French-first). Source-agnostic (AD-2): these are
# warehouse-vocabulary metric names, not module names. Unknown metrics fall back
# to their raw metric key.
# ---------------------------------------------------------------------------
_METRIC_LABELS: dict[str, str] = {
    "clicks": "Clics",
    "impressions": "Impressions",
    "sessions": "Sessions",
    "active_users": "Utilisateurs actifs",
    "conversions": "Conversions",
    "cost": "Coût",
    "revenue": "Revenu",
    "average_position": "Position moyenne",
    "roas": "ROAS",
    "ctr": "CTR",
    "cpa": "CPA",
}

# AD-9 / HG-1: the exact verbatim line emitted when no context events and no alerts
# are present. NEVER omit, NEVER invent a cause.
_CONTEXT_MISSING_LINE = "Contexte manquant pour cette période."

# Citation tightness rule (AC2): the full token including parens must not exceed
# this many characters. When a ULID pull_id would push past it, truncate pull_id.
_MAX_CITATION_CHARS = 60


def _format_number(value) -> str:
    """Format a metric value French-first: thousands separated by narrow no-break space.

    Integers render without decimals (``1 245``); floats keep one decimal with a
    comma decimal separator (``6,1``).
    """
    if value is None:
        return "?"
    if isinstance(value, float) and not value.is_integer():
        # One-decimal float, comma decimal separator, space thousands.
        int_part, _, frac = f"{value:.1f}".partition(".")
        grouped = f"{int(int_part):,}".replace(",", " ")
        return f"{grouped},{frac}"
    ivalue = int(value)
    return f"{ivalue:,}".replace(",", " ")


def _metric_citation(info: dict) -> str:
    """Build the ``(source_system:source_field, pull_id)`` citation token (AC2).

    Truncates ``pull_id`` to the first 10 chars only if the full token would exceed
    the 60-char tightness budget. Emits ``(contexte manquant)`` when provenance is
    absent (AD-9 — never fabricate a source).
    """
    source_system = info.get("source_system") or ""
    source_field = info.get("source_field") or ""
    pull_id = info.get("pull_id")

    if not pull_id or not source_system:
        return "(contexte manquant)"

    token = f"({source_system}:{source_field}, {pull_id})"
    if len(token) > _MAX_CITATION_CHARS:
        token = f"({source_system}:{source_field}, {str(pull_id)[:10]})"
    return token


def _delta_fragment(info: dict) -> str:
    """Return the ``(delta_pct vs period)`` fragment, or empty when no delta known."""
    delta_pct = info.get("delta_pct")
    period = info.get("period") or ""
    if delta_pct is None:
        return ""
    return f" ({delta_pct} vs {period})"


def _what_lines(rollup: dict, narrative_prompt: str | None) -> list[str]:
    """Build Section 1 (What): prompt line + one cited line per metric.

    Metric lines are ordered by magnitude of delta (largest absolute delta first);
    metrics with no delta sort last but keep a stable order. Each line carries a
    citation token so the citation-integrity rule holds (AC2, AC6).
    """
    lines: list[str] = []
    prompt = (narrative_prompt or "").strip()
    if prompt:
        lines.append(prompt)

    def _sort_key(item: tuple[str, dict]):
        _metric, info = item
        delta = info.get("delta")
        # None deltas sort after real deltas; real deltas by descending |delta|.
        return (0 if delta is not None else 1, -abs(delta) if delta is not None else 0.0)

    for metric, info in sorted(rollup.items(), key=_sort_key):
        label = _METRIC_LABELS.get(metric, metric)
        value_str = _format_number(info.get("value"))
        delta_str = _delta_fragment(info)
        citation = _metric_citation(info)
        lines.append(f"{label}: {value_str}{delta_str} {citation}")
    return lines


# ---------------------------------------------------------------------------
# Story 37.8 — market citation rules.
#
# A Local-markets report line must cite the operator's market LABEL, never a
# bare ISO code, and must never present the synthetic ``Other markets`` or
# ``Unknown`` groupings as if they were countries. These helpers are the single
# place that decides how a market bucket is named in prose, so no card builder
# has to re-derive it (and none may fall back to the raw code).
# ---------------------------------------------------------------------------
OTHER_MARKETS_BUCKET_ID = "__other_markets__"
UNKNOWN_MARKET_BUCKET_ID = "__unknown_market__"

# Prose used for the two non-country groupings. Deliberately NOT phrased as a
# place: they are reporting groupings, not markets and not countries.
_OTHER_MARKETS_PHRASE = "Hors marchés suivis"
_UNKNOWN_MARKET_PHRASE = "Géographie non résolue"


def is_country_market_bucket(bucket: dict) -> bool:
    """True only for a real client-defined market (never Other/Unknown)."""

    kind = bucket.get("market_kind") or bucket.get("kind")
    bucket_id = bucket.get("market_id") or bucket.get("id") or bucket.get("breakdown_value")
    if bucket_id in {OTHER_MARKETS_BUCKET_ID, UNKNOWN_MARKET_BUCKET_ID}:
        return False
    return kind == "tracked"


def market_display_label(bucket: dict) -> str:
    """Return the label to print for a market bucket.

    Never falls back to ``market_code``/``country_codes``: a report cites what
    the operator named the market. When no label is available the deterministic
    ``?`` placeholder is used, exactly like the other builders — an unknown name
    is never invented from a code.
    """

    bucket_id = bucket.get("market_id") or bucket.get("id") or bucket.get("breakdown_value")
    if bucket_id == OTHER_MARKETS_BUCKET_ID:
        return _OTHER_MARKETS_PHRASE
    if bucket_id == UNKNOWN_MARKET_BUCKET_ID:
        return _UNKNOWN_MARKET_PHRASE
    label = bucket.get("market_label") or bucket.get("label")
    return str(label) if label else "?"


def build_market_split_lines(
    *,
    buckets: list[dict],
    pull_ids: list[str] | None = None,
    connector: str | None = None,
    limit: int = 3,
) -> list[str]:
    """Cited lines for a Local-markets split, ordered as provided.

    Tracked markets are cited by label; ``Other markets`` and ``Unknown`` get a
    grouping phrase that cannot be read as a country name.
    """

    citation = _pull_citation(list(pull_ids or []), connector)
    lines: list[str] = []
    for bucket in (buckets or [])[:limit]:
        value = _format_number(bucket.get("value"))
        label = market_display_label(bucket)
        if is_country_market_bucket(bucket):
            lines.append(f"Marché « {label} » : {value} {citation}")
        else:
            lines.append(f"{label} : {value} {citation}")
    return lines


def _why_lines(
    context_events: list[dict],
    alerts: list[dict],
    as_of: str | None,
) -> list[str]:
    """Build Section 2 (Why): context events + alerts, or the explicit-absence line.

    AD-9: when neither context events nor alerts are present, emit EXACTLY the
    ``_CONTEXT_MISSING_LINE`` (never omitted, never softened, never invented).
    When ``as_of`` is set, append the reconstitution line.
    """
    lines: list[str] = []

    for evt in context_events or []:
        evt_id = evt.get("id")
        evt_date = evt.get("event_date", "?")
        evt_label = evt.get("label", "?")
        description = evt.get("description")
        if not evt_id:
            citation = "(contexte manquant)"
        elif str(evt_id).startswith("evt_"):
            citation = f"({evt_id})"
        else:
            citation = f"(evt_{evt_id})"
        detail = f": {description}" if description else ""
        lines.append(f"{evt_date} — {evt_label}{detail} {citation}")

    for alert in alerts or []:
        alert_id = alert.get("id")
        alert_metric = alert.get("metric", alert.get("rule", "alerte"))
        message = alert.get("message", "")
        if alert_id:
            citation = f"({alert_id})" if "_" in str(alert_id) else f"(alert_{alert_id})"
        else:
            citation = "(contexte manquant)"
        lines.append(f"⚠ {alert_metric}: {message} {citation}")

    if not lines:
        # AD-9 / HG-1 hard gate: explicit absence, verbatim.
        lines.append(_CONTEXT_MISSING_LINE)

    if as_of:
        lines.append(f"Données reconstituées au {as_of}.")

    return lines


def _pull_citation(pull_ids: list[str], connector: str | None = None) -> str:
    """Build a ``(connector:fact_daily_kpi, pull_id)`` citation token for card builders.

    Uses the last pull_id (most recent). Falls back to ``(contexte manquant)`` when
    no pull_ids are available (AD-9: never fabricate a source).
    """
    pull_id = pull_ids[-1] if pull_ids else None
    source_system = connector or "connector"
    if not pull_id:
        return "(contexte manquant)"
    token = f"({source_system}:fact_daily_kpi, {pull_id})"
    if len(token) > _MAX_CITATION_CHARS:
        token = f"({source_system}:fact_daily_kpi, {str(pull_id)[:10]})"
    return token


def _cause_line(context_events: list[dict]) -> str | None:
    """Return a single-line cause citation from the first context event, or None.

    AD-9: the cause is EXACTLY the event's label — never invented. When
    context_events is empty, callers must emit ``_CONTEXT_MISSING_LINE`` instead.
    """
    if not context_events:
        return None
    evt = context_events[0]
    evt_id = evt.get("id")
    evt_date = evt.get("event_date", "?")
    evt_label = evt.get("label", "?")
    if not evt_id:
        citation = "(contexte manquant)"
    elif str(evt_id).startswith("evt_"):
        citation = f"({evt_id})"
    else:
        citation = f"(evt_{evt_id})"
    return f"Contexte : {evt_date} — {evt_label} {citation}"


# ---------------------------------------------------------------------------
# Story 9.7 — Per-card deterministic cited builders.
#
# CONTRACT: every builder returns a string of <= 3 lines (newline-separated).
# It NEVER states a cause absent from context_events (AD-9).
# The _CONTEXT_MISSING_LINE is emitted verbatim when context_events is empty.
#
# ``block_data`` is a dict keyed by block type ("bar", "donut", "gauge", "funnel")
# carrying the resolved payload produced by the block resolvers in cards.py. Builders
# cite the EXACT numbers the card renders (comment <-> card consistency).
# ---------------------------------------------------------------------------


def build_keywords_comment(
    *,
    block_data: dict,
    rollup: dict,
    context_events: list[dict],
    pull_ids: list[str],
) -> str:
    """Keywords card: cite the biggest mover (|delta| position) by query name.

    Reads block_data["bar"]["bars"] — the movers bar payload produced by
    _resolve_bar_movers. Each bar item = {label, value (signed delta), direction}.

    <= 3 lines (+ 1 optional cannibalisation line, Story 10.5):
      1. Biggest mover (query name + signed delta) with citation
      2. Overall clicks/impressions KPI line with citation
      3. Cause line (context_events[0].label) OR _CONTEXT_MISSING_LINE
      4. (OPTIONAL) Cannibalisation: top cannibalised query + page count, ONLY when the
         cannibalisation block has non-empty rows (AD-9: cited from the rendered numbers,
         never invented). Absent when no cannibalisation is detected -> comment stays 3 lines.
    """
    citation = _pull_citation(pull_ids)
    lines: list[str] = []

    # Line 1: biggest mover from the bar block.
    bars = (block_data.get("bar") or {}).get("bars") or []
    if bars:
        top = bars[0]  # already sorted by |delta| desc in _resolve_bar_movers
        label = top.get("label") or "?"
        delta = top.get("value")
        direction = top.get("direction", "")
        if delta is not None:
            sign = "+" if float(delta) > 0 else ""
            dir_fr = "gain" if direction == "up" else "perte"
            lines.append(
                f"Requête « {label} » : {dir_fr} de {sign}{_format_number(delta)} pos. {citation}"
            )
        else:
            lines.append(f"Requête « {label} » en mouvement. {citation}")
    else:
        # No movers found — cite the rollup total instead.
        clicks_entry = rollup.get("clicks") or {}
        clicks_val = _format_number(clicks_entry.get("value"))
        lines.append(f"Clics totaux : {clicks_val}. {citation}")

    # Line 2: overall impressions from rollup (secondary context).
    imp_entry = rollup.get("impressions") or {}
    if imp_entry.get("value") is not None:
        imp_val = _format_number(imp_entry.get("value"))
        imp_delta = _delta_fragment(imp_entry)
        lines.append(f"Impressions : {imp_val}{imp_delta} {citation}")

    # Line 3: cause or explicit-absence.
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    # Line 4 (Story 10.5): cannibalisation, ONLY when the block has rows. Cite the top
    # cannibalised query + its competing-page count from the EXACT rendered table (AD-9:
    # cause-only, no invented explanation). Guard: never raise if the key is absent.
    lines = lines[:3]
    cannib = (block_data.get("cannibalisation") or {}).get("rows") or []
    if cannib:
        top_query = cannib[0].get("Requête") or "?"
        # Competing-page count = distinct pages of the top query in the rendered rows.
        n_pages = sum(1 for r in cannib if r.get("Requête") == top_query)
        lines.append(
            f"Cannibalisation : requête « {top_query} » en compétition sur "
            f"{n_pages} pages. {citation}"
        )

    return "\n".join(lines[:4])


def build_conversions_comment(
    *,
    block_data: dict,
    rollup: dict,
    context_events: list[dict],
    pull_ids: list[str],
) -> str:
    """Conversions card: cite the winning source + the notable CPA move.

    Reads block_data["donut"]["slices"] (winning source by conversions share) and
    block_data["gauge"] (CPA value + prior delta).

    <= 3 lines:
      1. Winning conversion source (name + share %) with citation
      2. CPA vs prior period with citation (omitted when gauge has no value)
      3. Cause line OR _CONTEXT_MISSING_LINE
    """
    citation = _pull_citation(pull_ids)
    lines: list[str] = []

    # Line 1: winning source from donut block.
    slices = (block_data.get("donut") or {}).get("slices") or []
    if slices:
        top = slices[0]  # already sorted desc by value in _resolve_donut
        src_label = top.get("label") or "?"
        src_pct = top.get("pct")
        pct_str = f"{src_pct:.1f} %" if src_pct is not None else "?"
        lines.append(
            f"Source principale : « {src_label} » ({pct_str} des conversions) {citation}"
        )
    else:
        # Fallback: total conversions from rollup.
        conv_entry = rollup.get("conversions") or {}
        conv_val = _format_number(conv_entry.get("value"))
        lines.append(f"Conversions totales : {conv_val}. {citation}")

    # Line 2: CPA delta from gauge block.
    gauge = block_data.get("gauge") or {}
    cpa_val = gauge.get("value")
    cpa_delta = gauge.get("delta")
    if cpa_val is not None:
        cpa_str = _format_number(cpa_val)
        unit = gauge.get("unit") or ""
        unit_str = f" {unit}" if unit else ""
        if cpa_delta is not None:
            sign = "+" if float(cpa_delta) > 0 else ""
            delta_str = f"{sign}{_format_number(cpa_delta)}{unit_str}"
            lines.append(f"CPA : {cpa_str}{unit_str} ({delta_str} vs période préc.) {citation}")
        else:
            # review-epic-9-integration F-6: when the prior CPA is unavailable, cite the
            # absence EXPLICITLY rather than silently dropping the "move" -- the LLM must be
            # able to tell "no prior period" apart from "CPA comparison suppressed".
            lines.append(
                f"CPA : {cpa_str}{unit_str} (évolution du CPA indisponible : "
                f"période précédente manquante) {citation}"
            )

    # Line 3: cause or explicit-absence.
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    return "\n".join(lines[:3])


def build_usertypes_comment(
    *,
    block_data: dict,
    rollup: dict,
    context_events: list[dict],
    pull_ids: list[str],
) -> str:
    """User types card: cite the dominant user type and device/country context.

    Story 10.2 (additive): when user_type data is available, line 1 cites the
    dominant user_type (Nouveaux/Fideles). Falls back to the device segment if no
    user_type donut is present (degrade path — backward compatible).

    Reads:
      block_data["donut_user_type"]["slices"]   -- new/returning (Story 10.2, preferred)
      block_data["donut_device_category"]["slices"] -- device split (fallback / line 2)
      block_data["donut"]["slices"]             -- generic fallback (backward compat)
      block_data["bar"]["bars"]                 -- country split

    <= 3 lines:
      1. Dominant user_type segment (new/returning) OR device segment (degrade)
      2. Top country if bar data present (else device segment, else rollup active_users)
      3. Cause line OR _CONTEXT_MISSING_LINE
    """
    citation = _pull_citation(pull_ids)
    lines: list[str] = []

    # Story 10.2: prefer user_type donut for line 1 (more directly answers "Qui sont mes
    # utilisateurs ?"). Fall back to device donut when user_type_daily is not enabled.
    ut_slices = (block_data.get("donut_user_type") or {}).get("slices") or []
    # Device donut is always keyed by its dimension (device_category is a required
    # dimension, so donut_device_category is always populated). We do NOT fall back
    # to the generic "donut" slot here: since 10.2 the user_type donut is FIRST in the
    # composition, so block_data["donut"] (first-wins) holds user_type, not device.
    dev_slices = (block_data.get("donut_device_category") or {}).get("slices") or []

    # Line 1: dominant segment.
    if ut_slices:
        top = ut_slices[0]
        seg_label = top.get("label") or "?"
        seg_pct = top.get("pct")
        pct_str = f"{seg_pct:.1f} %" if seg_pct is not None else "?"
        lines.append(
            f"Type dominant : « {seg_label} » ({pct_str} des utilisateurs actifs) {citation}"
        )
    elif dev_slices:
        top = dev_slices[0]
        seg_label = top.get("label") or "?"
        seg_pct = top.get("pct")
        pct_str = f"{seg_pct:.1f} %" if seg_pct is not None else "?"
        lines.append(
            f"Segment dominant : « {seg_label} » ({pct_str} des utilisateurs actifs) {citation}"
        )
    else:
        # Fallback: total active_users.
        au_entry = rollup.get("active_users") or {}
        au_val = _format_number(au_entry.get("value"))
        lines.append(f"Utilisateurs actifs : {au_val}. {citation}")

    # Line 2: top country from bar block.
    bars = (block_data.get("bar") or {}).get("bars") or []
    if bars:
        top_country = bars[0]
        country_val = _format_number(top_country.get("value"))
        top_id = (
            top_country.get("market_id")
            or top_country.get("id")
            or top_country.get("label")
        )
        if top_id in {OTHER_MARKETS_BUCKET_ID, UNKNOWN_MARKET_BUCKET_ID}:
            # Story 37.8: these groupings are never presented as a country.
            lines.append(
                f"{market_display_label(top_country)} : "
                f"{country_val} utilisateurs actifs {citation}"
            )
        elif top_country.get("market_kind") == "tracked":
            lines.append(
                f"Premier marché : « {market_display_label(top_country)} » "
                f"({country_val} utilisateurs actifs) {citation}"
            )
        else:
            country_label = top_country.get("label") or "?"
            lines.append(
                f"Premier pays : « {country_label} » "
                f"({country_val} utilisateurs actifs) {citation}"
            )
    else:
        # Fallback: sessions from rollup.
        sess_entry = rollup.get("sessions") or {}
        if sess_entry.get("value") is not None:
            sess_val = _format_number(sess_entry.get("value"))
            sess_delta = _delta_fragment(sess_entry)
            lines.append(f"Sessions : {sess_val}{sess_delta} {citation}")

    # Line 3: cause or explicit-absence.
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    return "\n".join(lines[:3])


def build_journey_comment(
    *,
    block_data: dict,
    rollup: dict,
    context_events: list[dict],
    pull_ids: list[str],
) -> str:
    """Journey card: cite the biggest drop-off step in the funnel + top entry page.

    Reads block_data["funnel"]["steps"] — ordered list of {label, value, rate}.
    The biggest drop-off is the transition with the lowest step-to-step rate
    (excluding the first step whose rate is always 1.0).

    Story 10.4: when landing_page rows are present, block_data["bar"] carries the entry-
    pages bar (sessions by landing_page). Line 2 cites the first entry page (highest
    sessions) when available, overriding the overall-rate fallback. The drop-off line
    (line 1) is unchanged — this is strictly ADDITIVE. The cause line (line 3) is also
    unchanged (AD-9: never invents a cause).

    <= 3 lines:
      1. Biggest drop-off step (step name + drop rate %) with citation
      2a. (Story 10.4) Top entry page (page name + sessions) when landing_page bar present
      2b. Overall funnel conversion rate (first -> last) with citation (when available)
      3. Cause line OR _CONTEXT_MISSING_LINE
    """
    citation = _pull_citation(pull_ids)
    lines: list[str] = []

    steps = (block_data.get("funnel") or {}).get("steps") or []
    overall_rate = (block_data.get("funnel") or {}).get("overall_rate")

    # Line 1: biggest drop-off (step with the lowest rate, excluding first step).
    eligible = [s for s in steps if s.get("rate") is not None and s.get("rate") != 1.0]
    if eligible:
        worst = min(eligible, key=lambda s: s["rate"])
        step_label = worst.get("label") or "?"
        step_rate = worst.get("rate")
        rate_pct = f"{step_rate * 100:.1f} %" if step_rate is not None else "?"
        lines.append(
            f"Décroché principal : étape « {step_label} » (taux de passage {rate_pct}) {citation}"
        )
    else:
        # No funnel data -- cite total sessions from rollup.
        sess_entry = rollup.get("sessions") or {}
        sess_val = _format_number(sess_entry.get("value"))
        lines.append(f"Sessions totales : {sess_val}. {citation}")

    # Line 2: Story 10.4 — top entry page from the landing_page bar block (preferred
    # when present), else fall back to the overall funnel conversion rate.
    # AD-9: only cites the entry page if it is actually present in the rendered bar block
    # (block_data["bar"]["bars"][0]); never invents a page name.
    entry_bars = (block_data.get("bar") or {}).get("bars") or []
    if entry_bars:
        top_bar = entry_bars[0]
        page_label = top_bar.get("label") or "?"
        page_sessions = top_bar.get("value")
        sessions_str = _format_number(page_sessions)
        lines.append(
            f"Première page d'entrée : « {page_label} » ({sessions_str} sessions) {citation}"
        )
    elif overall_rate is not None:
        rate_pct = f"{overall_rate * 100:.1f} %"
        lines.append(f"Taux de conversion global : {rate_pct}. {citation}")
    else:
        # Fallback: conversions from rollup.
        conv_entry = rollup.get("conversions") or {}
        if conv_entry.get("value") is not None:
            conv_val = _format_number(conv_entry.get("value"))
            conv_delta = _delta_fragment(conv_entry)
            lines.append(f"Conversions : {conv_val}{conv_delta} {citation}")

    # Line 3: cause or explicit-absence.
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    return "\n".join(lines[:3])


def build_connectors_comment(
    *,
    least_fresh: dict | None = None,
    connector_count: int = 0,
    block_data: dict | None = None,
    rollup: dict | None = None,
    context_events: list[dict] | None = None,
    pull_ids: list[str] | None = None,
) -> str:
    """Connecteurs card (Story 9.8): cite the LEAST-FRESH connector.

    This is a CONTEXT card (inventory). It is normally called DIRECTLY by
    cards._resolve_connectors_card with the pre-resolved inventory summary
    (``least_fresh`` + ``connector_count``).

    review-epic-9-backend F-10: it ALSO accepts the STANDARD fact-card builder kwargs
    (``block_data``, ``rollup``, ``context_events``, ``pull_ids``) and IGNORES the ones it
    does not need, so CARD_COMMENT_BUILDERS is UNIFORM -- a caller going through the generic
    registry dispatch (get_card's block-data path) never hits a TypeError on the connectors
    entry. When called that way (no inventory summary), it degrades to the designed
    empty-inventory line rather than inventing an inventory it was not given.

    <= 3 lines, AD-9 (never invents):
      1. Inventory count line.
      2. Least-fresh connector: name + last extract date (or "jamais extrait" when none).
      3. (never a cause line -- an inventory card has no causes to attribute)

    An empty inventory yields a single designed-empty line (never blank).
    """
    if not least_fresh or connector_count <= 0:
        return "Aucun connecteur configuré pour ce projet."

    name = least_fresh.get("connector") or "?"
    last_date = least_fresh.get("last_extract")
    lines: list[str] = []

    plural = "s" if connector_count > 1 else ""
    lines.append(f"{connector_count} connecteur{plural} configuré{plural} pour ce projet.")

    if last_date:
        lines.append(
            f"Connecteur le moins frais : « {name} » (dernier extrait le {last_date})."
        )
    else:
        lines.append(
            f"Connecteur le moins frais : « {name} » (jamais extrait)."
        )

    return "\n".join(lines[:3])


# ---------------------------------------------------------------------------
# Story 9.7 — Registry: card template id -> per-card builder.
#
# AD-2 DATA-DRIVEN: dispatch is a dict lookup (no if/elif chains, no module branches).
# Cards without an entry keep the generic ``build_narrative`` (kpi, future cards).
# Adding a new card builder = add one entry here + one function above.
#
# Story 9.8: the "connectors" builder is called DIRECTLY by cards.py with the inventory
# summary, but (review-epic-9-backend F-10) it is now SIGNATURE-COMPATIBLE with the generic
# block-data dispatch too (accepts + ignores block_data/rollup/context_events/pull_ids), so
# the registry is uniform and no caller can hit a TypeError on this entry.
# ---------------------------------------------------------------------------

def build_attribution_comment(
    *,
    block_data: dict,
    rollup: dict,
    context_events: list[dict],
    pull_ids: list[str],
) -> str:
    """Attribution card (Story 16.3): cite the last-click leader + first-click comparison.

    AD-9 / Epic 16 mandate: every line is a DESCRIPTIVE DATA OBSERVATION (channel X
    carried N last-click conversions), NEVER a causal assertion (channel X CAUSED the
    sale). The difference between last-click and first-click is presented as a factual
    gap between two counting conventions, not an explanation of user behaviour.

    Reads:
      block_data["bar"]["bars"]        -- last-click channel bar (first bar block)
      block_data["bar_first_user_source_medium"]["bars"]  -- first-click channel bar
      rollup["conversions"]            -- total conversions window (fallback)

    <= 3 lines:
      1. Canal leader dernier clic (label, conversions) + share% from bar [cited]
      2. Écart first vs last IF both bars present AND delta >= 5 pp on top canal
         (DATA OBSERVATION, jamais cause — AD-9) [cited]
         OR: fallback total conversions from rollup [cited]
      3. cause from _cause_line(context_events) OR _CONTEXT_MISSING_LINE
    """
    citation = _pull_citation(pull_ids)
    lines: list[str] = []

    # Line 1: leading last-click channel.
    # block_data["bar"] holds the FIRST bar block = last-click channels (composition order).
    last_bars = (block_data.get("bar") or {}).get("bars") or []
    if last_bars:
        top = last_bars[0]
        channel = top.get("label") or "?"
        conv = top.get("value")
        conv_str = _format_number(conv)
        # Compute share of the last-click bar subtotal (sum of returned bars).
        bar_subtotal = sum(b.get("value") or 0 for b in last_bars)
        share_str = ""
        if bar_subtotal > 0 and conv is not None:
            share_pct = round((conv / bar_subtotal) * 100.0, 1)
            share_str = f", {share_pct} % du top-N dernier clic"
        lines.append(
            f"Canal leader (dernier clic) : « {channel} » "
            f"({conv_str} conversions{share_str}) {citation}"
        )
    else:
        # Fallback: total conversions from rollup when no acquisition data present.
        conv_entry = rollup.get("conversions") or {}
        conv_val = _format_number(conv_entry.get("value"))
        lines.append(f"Conversions totales : {conv_val}. {citation}")

    # Line 2: first vs last comparison on the top canal, IF both bars are populated
    # and the delta is notable (>= 5 pp). This is a DESCRIPTIVE GAP, never a cause.
    # block_data["bar_first_user_source_medium"] holds the first-click bar.
    first_bars = (block_data.get("bar_first_user_source_medium") or {}).get("bars") or []
    noted_gap = False
    if last_bars and first_bars:
        # Find the top last-click canal in the first-click bars.
        top_last_label = (last_bars[0].get("label") or "").lower()
        last_subtotal = sum(b.get("value") or 0 for b in last_bars)
        first_subtotal = sum(b.get("value") or 0 for b in first_bars)
        top_last_pct = (
            round((last_bars[0].get("value") or 0) / last_subtotal * 100.0, 1)
            if last_subtotal > 0 else 0.0
        )
        top_first_pct: float | None = None
        for b in first_bars:
            if (b.get("label") or "").lower() == top_last_label:
                if first_subtotal > 0:
                    top_first_pct = round((b.get("value") or 0) / first_subtotal * 100.0, 1)
                break
        if top_first_pct is not None:
            gap = abs(top_last_pct - top_first_pct)
            if gap >= 5.0:
                direction = (
                    "plus fort en dernier clic"
                    if top_last_pct > top_first_pct
                    else "plus fort en premier clic"
                )
                lines.append(
                    f"Écart last/first pour « {last_bars[0].get('label', '?')} » : "
                    f"{top_last_pct} % (dernier clic) vs {top_first_pct} % "
                    f"(premier clic) — {direction}. {citation}"
                )
                noted_gap = True

    if not noted_gap:
        # No notable gap or first-click data missing — use conversions fallback if
        # the first bars are present but gap is < 5 pp, cite first-click availability.
        if first_bars and last_bars:
            lines.append(
                f"Premier clic disponible : {len(first_bars)} canal(aux) — "
                f"écart non significatif vs dernier clic. {citation}"
            )
        # else: if first_bars absent, line 2 stays absent (3-line budget, <= 3 lines).

    # Line 3: cause or explicit-absence (AD-9 / HG-1).
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    return "\n".join(lines[:3])


def build_dedup_comment(
    *,
    block_data: dict,
    duplication_rate: float | None,
    verified_total: float | None,
    claimed_total: float | None,
    verification_source_type: str | None,
    lead_event_name: str | None,
    pull_ids: list[str],
    context_events: list[dict],
    rate_coverage_days: int | None = None,
    rate_claimed_days: int | None = None,
    measured_coverage_rate: float | None = None,
) -> str:
    """Déduplication card (Story 17.3): cited deterministic comment.

    AD-9 / Epic 17 mandate (non-négociable) :
      * L'estimation est TOUJOURS étiquetée comme telle — le mot « estimation » est
        VERBATIM dans chaque rendu.
      * La formule est toujours citée (taux = Σ revendiqué ÷ réel).
      * La source de vérité désignée est nommée explicitement.
      * NULL rate → « source de vérification indisponible » — jamais un 0% déguisé.
      * Sans context_events → « Contexte manquant pour cette période. » verbatim (AD-9).

    <= 3 lines:
      1. Taux de duplication estimé + formule + source de vérité [cité]
         OR « source de vérification indisponible » when rate is NULL [cité]
      2. Canal leader (meilleure contribution dédupliquée) avec sa Part [cité]
         OR absent when bar series is empty.
      3. Cause de context_events[0] OR _CONTEXT_MISSING_LINE (AD-9 verbatim)
    """
    citation = _pull_citation(pull_ids)
    lines: list[str] = []

    # Line 1: taux + formule + source + couverture (AD-9 / review-17-5 AD-9 CRITICAL).
    # When rate_coverage_days < rate_claimed_days the rate is computed on a subset of
    # days (only those carrying BOTH claimed and verified). This must be surfaced to
    # the user honestly rather than presenting the rate as if it covered the full window.
    formula = "taux = Σ revendiqué ÷ réel"
    if duplication_rate is not None:
        rate_str = f"{duplication_rate:.1f}x".replace(".", ",")
        source_str = ""
        if verification_source_type:
            source_str = f", source : {verification_source_type}"
            if verification_source_type == "ga4" and lead_event_name:
                source_str += f" (évènement : {lead_event_name})"
        # Coverage wording: mention the days covered vs total days claimed.
        if (
            rate_coverage_days is not None
            and rate_claimed_days is not None
            and rate_coverage_days < rate_claimed_days
        ):
            coverage_str = (
                f"sur {rate_coverage_days} des {rate_claimed_days} jours "
                f"de la fenêtre portant la vérité"
            )
        elif rate_coverage_days is not None:
            coverage_str = f"sur {rate_coverage_days} jours"
        else:
            coverage_str = None
        rate_label = f"Taux de duplication estimé : {rate_str}"
        if coverage_str:
            rate_label += f" ({coverage_str})"
        lines.append(
            f"Estimation : {rate_label} "
            f"({formula}{source_str}) {citation}"
        )
    else:
        # AD-9: NULL rate -> honest absence, never 0%.
        lines.append(
            f"Estimation indisponible : source de vérification absente ou non configurée "
            f"({formula}) {citation}"
        )

    # Line 2: leading canal from the bar series (dedup contribution, first bar set).
    # block_data["bar"]["series"][1] = deduplicated_contribution bars.
    series = (block_data.get("bar") or {}).get("series") or []
    dedup_series = None
    for s in series:
        if s.get("metric") == "deduplicated_contribution":
            dedup_series = s
            break
    if dedup_series:
        dedup_bars = [b for b in (dedup_series.get("bars") or []) if b.get("value") is not None]
        if dedup_bars and verified_total:
            top = dedup_bars[0]
            ch_label = top.get("label") or "?"
            dedup_val = top.get("value")
            part_pct = (
                round((dedup_val / verified_total) * 100.0, 1)
                if dedup_val is not None
                else None
            )
            part_str = f"{part_pct:.1f} %" if part_pct is not None else "?"
            lines.append(
                f"Canal leader (estimation dédupliquée) : « {ch_label} » "
                f"({_format_number(dedup_val)} conversions, {part_str} du réel) {citation}"
            )

    # Line 3 (optional extra): measured reconciliation for shopify source (review-17-5 fix-7).
    # Clearly distinguished from the aggregate estimation -- a transaction-level coverage.
    # Only present when data is available; absent → nothing (never a fake 0%).
    if measured_coverage_rate is not None:
        coverage_pct_str = f"{measured_coverage_rate:.1f} %".replace(".", ",")
        lines.append(
            f"Réconciliation par transaction (mesurée) : couverture {coverage_pct_str} "
            f"{citation}"
        )

    # Line 3 (last): cause or explicit-absence (AD-9 / HG-1 verbatim).
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    # Cap at 4 lines to accommodate the optional measured_coverage_rate line.
    return "\n".join(lines[:4])


def build_mediaplan_pacing_comment(
    *,
    block_data: dict,
    plan_id: str,
    plan_name: str | None,
    plan_version_id: str | None,
    as_of_day: str | None,
    plan_rows: list[dict],
    plan_only_keys: list,
    pull_ids: list[str],
    context_events: list[dict],
) -> str:
    """Médiaplan pacing card (Story 22.5): cited deterministic comment.

    AD-9 / Epic 22 mandate (non-négociable) :
      * L'extrapolation est TOUJOURS étiquetée « Estimation ».
      * La formule de pace est citée (Pace = (réel − prévu to-date) / prévu to-date).
      * La couverture est affichée (as_of_day + nombre de jours de réel).
      * Les lignes plan-only sont signalées « plan seul » (jamais 0 %).
      * La source est citée (version du plan + mart).
      * pace NULL → affiché « — », jamais 0 %.
      * Sans context_events → _CONTEXT_MISSING_LINE verbatim (AD-9).

    <= 3 lignes :
      1. Plan courant : version + arrêté au as_of_day + formule + source [cité]
      2. Lignes plan-only listées (si présentes) [cité] OU résumé pace plan-level
      3. Cause context_events[0] OR _CONTEXT_MISSING_LINE
    """
    # Citation token: version du plan + mart (AD-9 provenance).
    version_str = plan_version_id or "?"
    plan_display = plan_name or plan_id
    pull_citation = f"(plan version {version_str}, mart plan_pacing_by_line)"
    if pull_ids:
        last_pull = pull_ids[-1]
        pull_citation = f"(plan version {version_str}, mart plan_pacing_by_line, pull {last_pull})"

    lines: list[str] = []
    formula = "Pace = (réel − prévu to-date) / prévu to-date"

    # --- Line 1: plan header + formule + couverture ----------------------------
    # Review 22.5 F-2: never a bare "?" in user-facing copy.
    as_of_str = as_of_day or "(date indisponible)"
    # Review 22.5 F-3 (leçon 17.5): coverage in DAYS of real data, not just the
    # as-of date. days_elapsed comes from the plan-level pacing row when present.
    days_elapsed = plan_rows[0].get("days_elapsed") if plan_rows else None
    coverage_str = f", {int(days_elapsed)} jour(s) de réel" if days_elapsed else ""
    line1 = (
        f"Plan « {plan_display} » — arrêté au {as_of_str}{coverage_str} "
        f"({formula}) {pull_citation}"
    )
    lines.append(line1)

    # --- Line 2: plan-only signal OU résumé pace niveau plan -------------------
    # Line 2 carries BOTH signals when both exist (vitest AI-54 catch: the
    # plan-only listing must never eat the pace summary and its « Estimation »
    # label -- AD-9 non-negotiable on the extrapolation).
    pace_part: str | None = None
    if plan_rows:
        pr = plan_rows[0]
        overall_pace = pr.get("pace")
        overall_actual = pr.get("actual_to_date")
        overall_budget = pr.get("budget")
        if overall_pace is not None:
            pace_pct = round(overall_pace * 100.0, 1)
            sign = "+" if pace_pct >= 0 else ""
            actual_s = (
                f"{overall_actual:.0f} €" if overall_actual is not None else "n/d"
            )
            budget_s = (
                f"{overall_budget:.0f} €" if overall_budget is not None else "n/d"
            )
            pace_part = (
                f"Pace global : {sign}{pace_pct:.1f} % "
                f"(réel {actual_s} / budget {budget_s}) — "
                f"Estimation extrapolation (run-rate linéaire)"
            )

    plan_only_part: str | None = None
    if plan_only_keys:
        plan_only_labels = [str(k) for k in plan_only_keys[:5]]
        plan_only_str = ", ".join(f"« {k} »" for k in plan_only_labels)
        suffix = f" (+ {len(plan_only_keys) - 5} autres)" if len(plan_only_keys) > 5 else ""
        plan_only_part = f"lignes plan seul (aucun pacing calculé) : {plan_only_str}{suffix}"

    if pace_part and plan_only_part:
        lines.append(f"{pace_part} ; {plan_only_part} {pull_citation}")
    elif pace_part:
        lines.append(f"{pace_part} {pull_citation}")
    elif plan_only_part:
        # Capitalise when the plan-only signal stands alone.
        lines.append(
            f"Lignes plan seul (aucun pacing calculé) : "
            f"{plan_only_part.split(' : ', 1)[1]} {pull_citation}"
        )
    # else: no line 2 (honest: nothing to say)

    # --- Line 3: cause or explicit-absence (AD-9 / HG-1 verbatim) ------------
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    return "\n".join(lines[:3])


CARD_COMMENT_BUILDERS: dict[str, object] = {
    "keywords": build_keywords_comment,
    "conversions": build_conversions_comment,
    "usertypes": build_usertypes_comment,
    "journey": build_journey_comment,
    "connectors": build_connectors_comment,
    "attribution": build_attribution_comment,
    # Story 17.3: dedup is a context card; its builder is called DIRECTLY by
    # cards._resolve_dedup_card (not via the generic block-data dispatch). Registered
    # here for catalog completeness and direct testability.
    "dedup": build_dedup_comment,
    # Story 22.5: mediaplan_pacing is a context card; its builder is called DIRECTLY
    # by cards._resolve_mediaplan_pacing_card. Registered here for catalog completeness
    # and direct testability.
    "mediaplan_pacing": build_mediaplan_pacing_comment,
}


def build_narrative(
    *,
    project_id: str,
    report_id: str | None,
    rollup: dict,
    context_events: list[dict],
    alerts: list[dict],
    as_of: str | None,
    narrative_prompt: str | None,
    max_lines: int = 30,
) -> str:
    """Assemble the deterministic what+why narrative with inline citations.

    Returns a multi-line string of ≤``max_lines`` lines. Section 1 (What) is filled
    first so critical cited metric lines survive truncation (summary-first ordering,
    AC4). Section 2 (Why) follows, separated by a blank line; if the cap forces
    Section 2 out entirely, a single truncation note is appended naming the number
    of context events available.

    AD-1: keyword-only signature with NO ``rows`` parameter — passing ``rows=`` is a
    TypeError (mechanical enforcement of the raw-row boundary; see
    ``test_ad1_no_raw_rows_accepted``).
    """
    what = _what_lines(rollup, narrative_prompt)
    why = _why_lines(context_events, alerts, as_of)

    # Assemble: What, blank separator, Why.
    lines: list[str] = list(what)
    if why:
        lines.append("")
        lines.extend(why)

    if len(lines) <= max_lines:
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 30-line cap enforcement (AC4). Section 1 (What) is summary-first and must
    # survive. If the full narrative overflows, keep What intact (truncating it
    # only if What alone already exceeds the cap) and drop/trim Section 2, adding
    # a truncation note naming how many context events were available.
    # ------------------------------------------------------------------
    n_events = len(context_events or [])

    if len(what) >= max_lines:
        # Even Section 1 overflows — keep the first (max_lines - 1) lines and add a
        # truncation note as the final line.
        kept = what[: max_lines - 1]
        kept.append(f"[… contexte tronqué — {n_events} événements disponibles]")
        return "\n".join(kept)

    # Section 1 fits; fill the remaining budget with Section 2, reserving one line
    # for the truncation note.
    budget = max_lines - len(what) - 1  # -1 for the truncation note line
    kept: list[str] = list(what)
    kept.append("")  # blank separator
    budget -= 1  # the blank line consumes budget too
    if budget > 0:
        kept.extend(why[:budget])
    kept.append(f"[… contexte tronqué — {n_events} événements disponibles]")
    return "\n".join(kept[:max_lines])
