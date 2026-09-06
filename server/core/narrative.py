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
      emits the ``context_missing`` phrase verbatim when context_events == []
      (the sentence itself is spelled once, in ``core.narrative_phrases``)

Builder signature::

    def build_<card>_comment(
        *,
        block_data: dict,         -- pre-resolved block payloads keyed by block type
        rollup: dict,             -- {metric: {value, delta, delta_pct, ...}}
        context_events: list[dict],
        pull_ids: list[str],
        dimension_labels: dict | None = None,  -- the client's word per dimension
    ) -> str

  ``block_data`` carries the resolved payloads produced by the block resolvers in
  cards.py — e.g. block_data["bar"]["bars"], block_data["donut"]["slices"],
  block_data["gauge"]["value"] / "delta". This lets each builder cite the EXACT
  numbers the card renders, guaranteeing comment ↔ card consistency.

  ``dimension_labels`` is the SAME already-resolved map the card's block titles are
  composed from (``cards.get_card`` reads it once at cards.py:4197 and hands one
  object to both). A sentence that names a dimension prints the client's word for
  it — see ``dimension_word`` — so a heading and the sentence under it can never
  call one dimension two things.

# AD-1 ENFORCEMENT: This module has zero imports from warehouse, modules, or BigQuery.
# It receives only pre-computed rollups, context events, alert dicts, and resolved
# block payloads. Raw rows never enter this file. Enforce by code review AND by the
# CI guard (make check-narrative-no-raw): any import of the warehouse module or any
# raw mart/table query in this file is a blocker.
"""

from __future__ import annotations

# The label-source vocabulary of `meta.dimension_labels`, from the module that
# declares the envelope contract. `core.envelope` imports nothing and reaches no
# store, so reading it here does not give the narration an inch of data access
# (AD-1, `scripts/check_narrative_no_raw.py`).
from core.envelope import LABEL_SOURCE_CLIENT

# ---------------------------------------------------------------------------
# THE WORDS ARE NOT IN THIS FILE, and that is the amendment of 2026-08-25
# (`analyze-and-test.md`, « how a narrative sentence is rendered »), which is
# Jean's arbitration of 2026-08-22 made mechanical: a récit is rendered in the
# READER's language, so a narrative sentence written into the code -- in any
# language -- is the defect. This module keeps the FORM of every sentence (which
# facts, in which order, with which citation) and names each one by a stable
# English key; `core.narrative_phrases` decides how that key reads.
#
# `core.narrative_phrases` imports nothing, so AD-1 is untouched.
# ---------------------------------------------------------------------------
from core.narrative_phrases import dimension_default, metric_label, phrase

# AD-9 / HG-1: the exact verbatim line emitted when no context events and no alerts
# are present. NEVER omit, NEVER invent a cause. The NAME stays here -- callers and
# tests read it as the contract; only its spelling moved to the catalogue.
_CONTEXT_MISSING_LINE = phrase("context_missing")


def context_absence_line(
    context_unavailable: object, *, read_and_empty: str | None
) -> str | None:
    """THE one place a reader holding no context event picks which absence it says.

    AI-350, and it is the class of AI-344 rather than one more instance of it.
    "No event to show" has two causes that must never share a sentence: the window
    was READ and holds none, or no store could serve it. Every reader of context
    events reaches this fork -- the report's "Why" section, the daily report's
    zero-row branch, the morning briefing's header line -- and each one used to
    decide it for itself, which is how the zero-row branch of ``get_daily_report``
    kept saying "aucun événement connu" while ``meta.context_events_unavailable``
    said the opposite on the same response.

    ``context_unavailable`` is the ``{reason, repair}`` payload of
    ``core.context_events.ContextEventsUnavailable``; anything else (``None``, an
    empty dict, a value of another type) means the window WAS read.

    ``read_and_empty`` is the caller's OWN sentence for a read-and-empty window --
    ``context_missing`` in the narrative, ``summary_context_none`` in the rollup
    summary -- because those two surfaces have always worded that fact differently
    and this function is not the place to unify them. ``None`` means the caller
    says nothing at all when the window was read (the briefing header, which does
    not spend a line on a non-event).

    The unread sentence, on the other hand, is ONE clause for the whole product:
    ``narrative_phrases``' ``context_unavailable`` (context-hub.md, note "Where
    each reader carries the unread window", 2026-09-01 -- "the phrase is the one
    from the catalogue, never a second wording"). The reason and the repair ride
    the envelope, never the sentence.
    """
    if isinstance(context_unavailable, dict) and context_unavailable:
        return phrase("context_unavailable")
    return read_and_empty


# How operator-authored commentary guidance is framed on the model channel
# (story 53.5, CAV-18).
#
# `llm_commentary_guidelines` is free text an operator stores per report. It used
# to be appended to the narrative prompt with no frame, so a directive like
# "attribute drops to seasonality" reached the model indistinguishable from the
# deterministic, cited comment sitting next to it -- and the model's elaboration
# then carried that card's credibility while asserting a cause no evidence
# supports. The deterministic builder already refuses to state an absent cause
# (AD-9, `_CONTEXT_MISSING_LINE`); this frame extends the SAME rule to the channel
# where the model elaborates, instead of leaving it enforced on one half only.
#
# Shared by `cards._build_summary` and `reports._build_report_summary` so the two
# cannot drift on the wording that carries the constraint.
GUIDANCE_FRAME = phrase("operator_guidance_frame")

# Citation tightness rule (AC2): the full token including parens must not exceed
# this many characters. When a ULID pull_id would push past it, truncate pull_id.
_MAX_CITATION_CHARS = 60


# ---------------------------------------------------------------------------
# Story 27.9 -- THE CLIENT'S WORD IN A SENTENCE.
#
# governance.md, "A client label reaches every surface that shows the number",
# lists a narrative sentence beside a chart and an axis. The render path was armed
# and this one was not: a card whose heading read "Users by Terminal" (the client's
# word, substituted by `cards._resolve_block_title`) carried a comment underneath
# saying "Segment dominant" -- one dimension called two things in one session,
# which is the defect the label exists to prevent, not a smaller version of it.
#
# AD-1 DECIDES WHERE THE RESOLUTION LIVES, AND IT IS NOT HERE. The narration reads
# nothing: `cards.get_card` already resolves the map ONCE
# (`dimension_lineage.resolve_report_dimension_labels`, cards.py:4197) and hands the
# SAME object to the block titles and to the builders below, so the heading and the
# sentence under it cannot disagree. `scripts/check_narrative_no_raw.py` keeps that
# true mechanically.
# ---------------------------------------------------------------------------


def dimension_word(dimension_labels: dict | None, name: str, default: str) -> str:
    """The word a SENTENCE prints for the canonical dimension *name* (PURE).

    Same contract as ``cards._resolve_block_title``, and deliberately the same
    shape, because it is the same rule on a second surface:

    * the client's own word wins whenever one was stored for this dimension;
    * where nobody named it the sentence keeps *default* -- the prose it ships,
      which is the prose a person can read;
    * the canonical identifier NEVER reaches the sentence. "Premier
      device_category" is exactly what the label exists to prevent, so a map
      entry that merely falls back to the identifier is treated as no name at
      all;
    * no label is ever invented: an absent map, an absent entry and a fallback
      entry all yield the shipped prose, unchanged.
    """
    entry = (dimension_labels or {}).get(name) or {}
    if entry.get("label_source") == LABEL_SOURCE_CLIENT and entry.get("display_label"):
        return str(entry["display_label"])
    return default


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

    MULTI-SOURCE. When the rollup value adds several connectors together, the
    citation must not read as one source — that misattribution is what made a
    cross-source sum look like a single provider's figure. ``rollup._provenance``
    supplies ``source_systems``; a value built from more than one is cited as
    ``a+b`` and, when that would blow the tightness budget, as ``N sources``. The
    count is never dropped: losing "how many" is the failure, losing "which ones"
    is only a display limit.
    """
    source_system = info.get("source_system") or ""
    source_field = info.get("source_field") or ""
    pull_id = info.get("pull_id")
    source_count = int(info.get("source_count") or 0)

    if not pull_id or not source_system:
        return phrase("citation_unavailable")

    token = f"({source_system}:{source_field}, {pull_id})"
    if len(token) > _MAX_CITATION_CHARS:
        token = f"({source_system}:{source_field}, {str(pull_id)[:10]})"
    if len(token) > _MAX_CITATION_CHARS and source_count > 1:
        token = f"({source_count} sources:{source_field}, {str(pull_id)[:10]})"
    return token


def _delta_fragment(info: dict) -> str:
    """Return the ``(delta_pct vs period)`` fragment, or empty when no delta known."""
    delta_pct = info.get("delta_pct")
    period = info.get("period") or ""
    if delta_pct is None:
        return ""
    return phrase("delta_vs_period", delta_pct=delta_pct, period=period)


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
        label = metric_label(metric)
        citation = _metric_citation(info)
        # A combined total the reconciliation gate refused is not replaced by a
        # plausible one: the line states the refusal and gives each source's own
        # figure, so a reader can still act without being handed a sum that may
        # double-count. `rollup._combination_refusal` owns the decision.
        refusal = info.get("combination_refused")
        if refusal:
            per_source = info.get("per_source") or {}
            detail = " ; ".join(
                phrase("metric_per_source", name=name, value=_format_number(value))
                for name, value in sorted(per_source.items())
            )
            lines.append(
                phrase(
                    "metric_combination_refused",
                    label=label,
                    refusal=refusal,
                    detail=(
                        phrase("metric_combination_detail", detail=detail)
                        if detail
                        else ""
                    ),
                    citation=citation,
                )
            )
            continue
        lines.append(
            phrase(
                "metric_line",
                label=label,
                value=_format_number(info.get("value")),
                delta=_delta_fragment(info),
                citation=citation,
            )
        )
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
# Story 48.2: a bucket is identified by its KIND, not by a magic id. Rest of
# World is now a governed node whose id differs per Project and whose label the
# operator chooses, so a hard-coded `__other_markets__` string would name
# nothing. Unknown keeps a reserved id because it is an evidence state.
UNKNOWN_MARKET_BUCKET_ID = "__unknown__"

# Prose for the two non-country groupings. Deliberately NOT phrased as a place:
# they are reporting groupings, not markets and not countries. Their WORDS are in
# the catalogue like every other sentence; the two names stay here because the
# rule they carry -- « a grouping is never named like a country » -- is this
# module's, not the catalogue's.
_REST_OF_WORLD_PHRASE = phrase("market_rest_of_world")
_UNKNOWN_MARKET_PHRASE = phrase("market_unknown")


def _bucket_kind(bucket: dict) -> str:
    for key in ("geography_bucket_kind", "market_kind", "kind"):
        value = bucket.get(key)
        if value:
            return str(value)
    return ""


def is_country_market_bucket(bucket: dict) -> bool:
    """True only for a real client-defined market (never Rest of World/Unknown)."""

    return _bucket_kind(bucket) == "assigned"


def market_display_label(bucket: dict) -> str:
    """Return the label to print for a market bucket.

    Never falls back to ``market_code``/``country_codes``: a report cites what
    the operator named the market. When no label is available the deterministic
    ``?`` placeholder is used, exactly like the other builders — an unknown name
    is never invented from a code.
    """

    kind = _bucket_kind(bucket)
    label = bucket.get("market_label") or bucket.get("label")
    if kind == "unknown":
        return _UNKNOWN_MARKET_PHRASE
    if kind == "rest_of_world":
        # The operator may have renamed it; the governed label wins over ours.
        return str(label) if label else _REST_OF_WORLD_PHRASE
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
            lines.append(
                phrase("market_split_line", label=label, value=value, citation=citation)
            )
        else:
            lines.append(
                phrase(
                    "market_split_grouping_line",
                    label=label,
                    value=value,
                    citation=citation,
                )
            )
    return lines


def _why_lines(
    context_events: list[dict],
    alerts: list[dict],
    as_of: str | None,
    context_scope: dict | None = None,
    context_unavailable: dict | None = None,
) -> list[str]:
    """Build Section 2 (Why): context events + alerts, or the explicit-absence line.

    AD-9: when neither context events nor alerts are present, emit EXACTLY the
    ``_CONTEXT_MISSING_LINE`` (never omitted, never softened, never invented).
    When ``as_of`` is set, append the reconstitution line.

    ``context_unavailable`` (AI-344, context-hub.md amendment of 2026-09-01) is
    the ``{reason, repair}`` the caller received when the events could not be
    READ from any store. The absence line then does not apply -- it would claim
    the window was read and found empty -- and the section says the context is
    unknown instead. The reason and the repair travel on the envelope, not here:
    a narrative sentence is rendered from the catalogue, never from a payload.

    ``context_scope`` is the pairing descriptor of
    ``core.briefing.context_events_in_claim_scope`` -- what the events listed
    below were compared to the claim ON, and what could not be compared. A "Why"
    section ASSERTS a relationship, so when the caller scoped its events this
    section says on what basis, in the same vocabulary the briefing and the
    anomaly evaluator use. It is never composed here: a descriptor built next to
    the render would describe a filter this function did not run.

    A caller that passes no descriptor gets Story 6.4's behaviour unchanged --
    the report and card builders of `core.reports` and `core.cards`, which answer
    a question a person asked. The proactive path (`reporting_mcp
    .get_daily_report`) passes one, and `test_why_lines_scope.py` holds that it
    does.
    """
    lines: list[str] = []

    for evt in context_events or []:
        evt_id = evt.get("id")
        evt_date = evt.get("event_date", "?")
        evt_label = evt.get("label", "?")
        description = evt.get("description")
        if not evt_id:
            citation = phrase("citation_unavailable")
        elif str(evt_id).startswith("evt_"):
            citation = f"({evt_id})"
        else:
            citation = f"(evt_{evt_id})"
        lines.append(
            phrase(
                "context_event_line",
                date=evt_date,
                label=evt_label,
                detail=(
                    phrase("context_event_detail", description=description)
                    if description
                    else ""
                ),
                citation=citation,
            )
        )

    for alert in alerts or []:
        alert_id = alert.get("id")
        alert_metric = alert.get("metric", alert.get("rule", phrase("alert_subject_fallback")))
        message = alert.get("message", "")
        if alert_id:
            citation = f"({alert_id})" if "_" in str(alert_id) else f"(alert_{alert_id})"
        else:
            citation = phrase("citation_unavailable")
        lines.append(
            phrase("alert_line", subject=alert_metric, message=message, citation=citation)
        )

    if not lines:
        # AD-9 / HG-1 hard gate: explicit absence, verbatim -- or, when the events
        # were never read, the explicit UNKNOWN (AI-344). Never nothing. The fork
        # itself is `context_absence_line`, shared with every other reader (AI-350).
        lines.append(
            context_absence_line(
                context_unavailable, read_and_empty=_CONTEXT_MISSING_LINE
            )
        )

    if context_scope:
        lines.append(context_scope_line(context_scope))

    if as_of:
        lines.append(phrase("as_of_reconstituted", as_of=as_of))

    return lines


def context_scope_line(context_scope: dict) -> str:
    """One line naming what the attachment was checked on, and what it was not.

    Composed from the descriptor's OWN lists, in their order, and from nothing
    else. It carries no number, no event and no cause -- it is the sentence that
    keeps an attached event from reading as a verified relationship when only one
    of its three dimensions was verifiable.

    An empty ``unscoped_dimensions`` prints no second half rather than an empty
    one: "Non comparé :" followed by nothing is a sentence a reader has to guess
    at, and there is nothing to guess -- everything was compared.
    """
    line = phrase(
        "context_scope_line",
        basis=", ".join(str(b) for b in context_scope.get("basis") or []),
    )
    unscoped = [str(d) for d in context_scope.get("unscoped_dimensions") or []]
    if unscoped:
        line += phrase("context_scope_unscoped", unscoped=", ".join(unscoped))
    return line


def _pull_citation(pull_ids: list[str], connector: str | None = None) -> str:
    """Build a ``(connector:fact_daily_kpi, pull_id)`` citation token for card builders.

    Uses the last pull_id (most recent). Falls back to ``(contexte manquant)`` when
    no pull_ids are available (AD-9: never fabricate a source).
    """
    pull_id = pull_ids[-1] if pull_ids else None
    source_system = connector or "connector"
    if not pull_id:
        return phrase("citation_unavailable")
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
        citation = phrase("citation_unavailable")
    elif str(evt_id).startswith("evt_"):
        citation = f"({evt_id})"
    else:
        citation = f"(evt_{evt_id})"
    return phrase("cause_line", date=evt_date, label=evt_label, citation=citation)


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
    dimension_labels: dict | None = None,
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
    # Story 27.9: the client's own word for the dimension these lines name. The
    # blocks bind `query`; the catalogue holds the prose this card ships for it.
    query_word = dimension_word(dimension_labels, "query", dimension_default("query"))

    # Line 1: biggest mover from the bar block.
    bars = (block_data.get("bar") or {}).get("bars") or []
    if bars:
        top = bars[0]  # already sorted by |delta| desc in _resolve_bar_movers
        label = top.get("label") or "?"
        delta = top.get("value")
        direction = top.get("direction", "")
        if delta is not None:
            lines.append(
                phrase(
                    "keywords_top_mover",
                    word=query_word,
                    label=label,
                    direction=phrase(
                        "direction_gain" if direction == "up" else "direction_loss"
                    ),
                    sign="+" if float(delta) > 0 else "",
                    delta=_format_number(delta),
                    citation=citation,
                )
            )
        else:
            lines.append(
                phrase(
                    "keywords_mover_unquantified",
                    word=query_word,
                    label=label,
                    citation=citation,
                )
            )
    else:
        # No movers found — cite the rollup total instead.
        clicks_entry = rollup.get("clicks") or {}
        lines.append(
            phrase(
                "keywords_total_clicks",
                value=_format_number(clicks_entry.get("value")),
                citation=citation,
            )
        )

    # Line 2: overall impressions from rollup (secondary context).
    imp_entry = rollup.get("impressions") or {}
    if imp_entry.get("value") is not None:
        lines.append(
            phrase(
                "keywords_impressions",
                value=_format_number(imp_entry.get("value")),
                delta=_delta_fragment(imp_entry),
                citation=citation,
            )
        )

    # Line 3: cause or explicit-absence.
    cause = _cause_line(context_events)
    lines.append(cause if cause is not None else _CONTEXT_MISSING_LINE)

    # Line 4 (Story 10.5): cannibalisation, ONLY when the block has rows. Cite the top
    # cannibalised query + its competing-page count from the EXACT rendered table (AD-9:
    # cause-only, no invented explanation). Guard: never raise if the key is absent.
    lines = lines[:3]
    cannib = (block_data.get("cannibalisation") or {}).get("rows") or []
    if cannib:
        # AI-59: rows are keyed by the column `key`, like every other table block.
        top_query = cannib[0].get("_dim") or "?"
        # Competing-page count = distinct pages of the top query in the rendered rows.
        n_pages = sum(1 for r in cannib if r.get("_dim") == top_query)
        # The SAME word as line 1: a comment that called `query` two things would
        # reopen, inside one comment, the drift this story closes between the
        # heading and the numbers.
        lines.append(
            phrase(
                "keywords_cannibalisation",
                word=query_word,
                label=top_query,
                page_count=n_pages,
                citation=citation,
            )
        )

    return "\n".join(lines[:4])


def build_conversions_comment(
    *,
    block_data: dict,
    rollup: dict,
    context_events: list[dict],
    pull_ids: list[str],
    dimension_labels: dict | None = None,
) -> str:
    """Conversions card: cite the winning source + the notable CPA move.

    Reads block_data["donut"]["slices"] (winning source by conversions share) and
    block_data["gauge"] (CPA value + prior delta).

    ``dimension_labels`` is accepted for registry uniformity and used by NOTHING
    here, on purpose: this card groups on the row-level ``connector`` column
    (cards.py:157), which is not a canonical dimension and therefore carries no
    client label. Naming it from the label map would invent a name.

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
            phrase(
                "conversions_top_source",
                label=src_label,
                pct=pct_str,
                citation=citation,
            )
        )
    else:
        # Fallback: total conversions from rollup.
        conv_entry = rollup.get("conversions") or {}
        lines.append(
            phrase(
                "conversions_total",
                value=_format_number(conv_entry.get("value")),
                citation=citation,
            )
        )

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
            lines.append(
                phrase(
                    "conversions_cpa_with_delta",
                    value=cpa_str,
                    unit=unit_str,
                    delta=f"{sign}{_format_number(cpa_delta)}{unit_str}",
                    citation=citation,
                )
            )
        else:
            # review-epic-9-integration F-6: when the prior CPA is unavailable, cite the
            # absence EXPLICITLY rather than silently dropping the "move" -- the LLM must be
            # able to tell "no prior period" apart from "CPA comparison suppressed".
            lines.append(
                phrase(
                    "conversions_cpa_no_prior",
                    value=cpa_str,
                    unit=unit_str,
                    citation=citation,
                )
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
    dimension_labels: dict | None = None,
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

    # Story 27.9: the client's word for the three dimensions these lines name.
    # The defaults are the prose the card ships, held by the catalogue -- never
    # the identifier: `Segment dominant` is readable and `device_category
    # dominant` is the defect.
    user_type_word = dimension_word(
        dimension_labels, "user_type", dimension_default("user_type")
    )
    device_word = dimension_word(
        dimension_labels, "device_category", dimension_default("device_category")
    )
    country_word = dimension_word(
        dimension_labels, "country", dimension_default("country")
    )

    # Line 1: dominant segment.
    if ut_slices or dev_slices:
        top = (ut_slices or dev_slices)[0]
        seg_pct = top.get("pct")
        lines.append(
            phrase(
                "usertypes_dominant_segment",
                word=user_type_word if ut_slices else device_word,
                label=top.get("label") or "?",
                pct=f"{seg_pct:.1f} %" if seg_pct is not None else "?",
                citation=citation,
            )
        )
    else:
        # Fallback: total active_users.
        au_entry = rollup.get("active_users") or {}
        lines.append(
            phrase(
                "usertypes_active_total",
                value=_format_number(au_entry.get("value")),
                citation=citation,
            )
        )

    # Line 2: top country from bar block.
    bars = (block_data.get("bar") or {}).get("bars") or []
    if bars:
        top_country = bars[0]
        country_val = _format_number(top_country.get("value"))
        if _bucket_kind(top_country) in {"rest_of_world", "unknown"}:
            # Story 37.8, kept by 48.2: a grouping is never presented as a
            # country. The test is now the bucket KIND -- Rest of World has a
            # per-Project governed id, so matching a literal would match nothing.
            lines.append(
                phrase(
                    "usertypes_geography_grouping",
                    label=market_display_label(top_country),
                    value=country_val,
                    citation=citation,
                )
            )
        elif is_country_market_bucket(top_country):
            # NOT `country_word`, and not an oversight: a market is a group of
            # countries the operator named (story 37.8/48.2), a governed object
            # with its own label -- `market_display_label` above. Renaming
            # `country` renames the dimension, never this grouping.
            lines.append(
                phrase(
                    "usertypes_top_market",
                    label=market_display_label(top_country),
                    value=country_val,
                    citation=citation,
                )
            )
        else:
            lines.append(
                phrase(
                    "usertypes_top_country",
                    word=country_word,
                    label=top_country.get("label") or "?",
                    value=country_val,
                    citation=citation,
                )
            )
    else:
        # Fallback: sessions from rollup.
        sess_entry = rollup.get("sessions") or {}
        if sess_entry.get("value") is not None:
            lines.append(
                phrase(
                    "usertypes_sessions",
                    value=_format_number(sess_entry.get("value")),
                    delta=_delta_fragment(sess_entry),
                    citation=citation,
                )
            )

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
    dimension_labels: dict | None = None,
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
        step_rate = worst.get("rate")
        lines.append(
            phrase(
                "journey_worst_step",
                label=worst.get("label") or "?",
                rate=f"{step_rate * 100:.1f} %" if step_rate is not None else "?",
                citation=citation,
            )
        )
    else:
        # No funnel data -- cite total sessions from rollup.
        sess_entry = rollup.get("sessions") or {}
        lines.append(
            phrase(
                "journey_sessions_total",
                value=_format_number(sess_entry.get("value")),
                citation=citation,
            )
        )

    # Line 2: Story 10.4 — top entry page from the landing_page bar block (preferred
    # when present), else fall back to the overall funnel conversion rate.
    # AD-9: only cites the entry page if it is actually present in the rendered bar block
    # (block_data["bar"]["bars"][0]); never invents a page name.
    entry_bars = (block_data.get("bar") or {}).get("bars") or []
    if entry_bars:
        top_bar = entry_bars[0]
        # Story 27.9: the bar binds `landing_page`; the catalogue holds the prose
        # this card ships for it, and the client's word replaces it when set.
        landing_word = dimension_word(
            dimension_labels, "landing_page", dimension_default("landing_page")
        )
        lines.append(
            phrase(
                "journey_top_landing_page",
                word=landing_word,
                label=top_bar.get("label") or "?",
                sessions=_format_number(top_bar.get("value")),
                citation=citation,
            )
        )
    elif overall_rate is not None:
        lines.append(
            phrase(
                "journey_overall_rate",
                rate=f"{overall_rate * 100:.1f} %",
                citation=citation,
            )
        )
    else:
        # Fallback: conversions from rollup.
        conv_entry = rollup.get("conversions") or {}
        if conv_entry.get("value") is not None:
            lines.append(
                phrase(
                    "journey_conversions",
                    value=_format_number(conv_entry.get("value")),
                    delta=_delta_fragment(conv_entry),
                    citation=citation,
                )
            )

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
    dimension_labels: dict | None = None,
) -> str:
    """Connecteurs card (Story 9.8): cite the LEAST-FRESH connector.

    This is a CONTEXT card (inventory). It is normally called DIRECTLY by
    cards._resolve_connectors_card with the pre-resolved inventory summary
    (``least_fresh`` + ``connector_count``).

    review-epic-9-backend F-10: it ALSO accepts the STANDARD fact-card builder kwargs
    (``block_data``, ``rollup``, ``context_events``, ``pull_ids``, ``dimension_labels``)
    and IGNORES the ones it
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
        return phrase("connectors_none")

    name = least_fresh.get("connector") or "?"
    last_date = least_fresh.get("last_extract")
    lines: list[str] = []

    # Plural is a property of a LANGUAGE, not of a count: gluing an "s" onto a
    # template decided French grammar at the call site. Two keys, and a language
    # that pluralises differently answers in its own column.
    lines.append(
        phrase(
            "connectors_count_many" if connector_count > 1 else "connectors_count_one",
            count=connector_count,
        )
    )

    if last_date:
        lines.append(phrase("connectors_least_fresh", name=name, date=last_date))
    else:
        lines.append(phrase("connectors_least_fresh_never", name=name))

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
#
# Story 27.9 extends that uniformity to ``dimension_labels``: the generic dispatch
# passes it to WHATEVER builder is registered, so every entry accepts it, and the
# three that name no canonical dimension (conversions, dedup, mediaplan_pacing)
# say at their signature WHY they ignore it. A builder that quietly dropped it
# would print our word under a heading printing the client's.
# ---------------------------------------------------------------------------

def build_attribution_comment(
    *,
    block_data: dict,
    rollup: dict,
    context_events: list[dict],
    pull_ids: list[str],
    dimension_labels: dict | None = None,
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
    # Story 27.9: both bars of this card break down the SAME canonical dimension
    # (`session_source_medium` last click, `first_user_source_medium` first click)
    # under ONE prose word, held by the catalogue. One word, so the two lines of
    # one comment cannot call it two things.
    channel_word = dimension_word(
        dimension_labels,
        "session_source_medium",
        dimension_default("session_source_medium"),
    )

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
            share_str = phrase(
                "attribution_share_fragment",
                pct=round((conv / bar_subtotal) * 100.0, 1),
            )
        lines.append(
            phrase(
                "attribution_last_click_leader",
                word=channel_word,
                label=channel,
                conversions=conv_str,
                share=share_str,
                citation=citation,
            )
        )
    else:
        # Fallback: total conversions from rollup when no acquisition data present.
        conv_entry = rollup.get("conversions") or {}
        lines.append(
            phrase(
                "conversions_total",
                value=_format_number(conv_entry.get("value")),
                citation=citation,
            )
        )

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
                lines.append(
                    phrase(
                        "attribution_gap",
                        label=last_bars[0].get("label", "?"),
                        last_pct=top_last_pct,
                        first_pct=top_first_pct,
                        direction=phrase(
                            "attribution_stronger_last"
                            if top_last_pct > top_first_pct
                            else "attribution_stronger_first"
                        ),
                        citation=citation,
                    )
                )
                noted_gap = True

    if not noted_gap:
        # No notable gap or first-click data missing — use conversions fallback if
        # the first bars are present but gap is < 5 pp, cite first-click availability.
        if first_bars and last_bars:
            # "canal(aux)" was the dimension named a SECOND time, in a second
            # spelling: the day a client renamed it, line 1 said their word and
            # this one still said ours. It counts values now, and names nothing.
            lines.append(
                phrase(
                    "attribution_gap_not_significant",
                    value_count=len(first_bars),
                    citation=citation,
                )
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
    dimension_labels: dict | None = None,
) -> str:
    """Déduplication card (Story 17.3): cited deterministic comment.

    ``dimension_labels`` is accepted (registry uniformity, F-10) and used by
    nothing: this card's bars group on ``channel_connector``, which is not a
    canonical dimension and carries no client label.

    AD-9 / Epic 17 mandate (non-négociable) :
      * L'estimation est TOUJOURS étiquetée comme telle — le mot « estimation » est
        VERBATIM dans chaque rendu.
      * La formule est toujours citée (rate = Σ claimed ÷ actual).
      * La source de vérité désignée est nommée explicitement.
      * NULL rate → « source de vérification indisponible » — jamais un 0% déguisé.
      * Sans context_events → la phrase ``context_missing`` verbatim (AD-9).

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
    formula = phrase("dedup_formula")
    if duplication_rate is not None:
        rate_str = f"{duplication_rate:.1f}x".replace(".", ",")
        source_str = ""
        if verification_source_type:
            source_str = phrase(
                "dedup_source_fragment", source=verification_source_type
            )
            if verification_source_type == "ga4" and lead_event_name:
                source_str += phrase("dedup_event_fragment", event=lead_event_name)
        # Coverage wording: mention the days covered vs total days claimed.
        if (
            rate_coverage_days is not None
            and rate_claimed_days is not None
            and rate_coverage_days < rate_claimed_days
        ):
            coverage_str = phrase(
                "dedup_coverage_partial",
                covered=rate_coverage_days,
                claimed=rate_claimed_days,
            )
        elif rate_coverage_days is not None:
            coverage_str = phrase("dedup_coverage_days", covered=rate_coverage_days)
        else:
            coverage_str = None
        rate_label = phrase("dedup_rate_label", rate=rate_str)
        if coverage_str:
            rate_label += phrase("dedup_rate_coverage", coverage=coverage_str)
        lines.append(
            phrase(
                "dedup_estimation",
                rate_label=rate_label,
                formula=formula,
                source=source_str,
                citation=citation,
            )
        )
    else:
        # AD-9: NULL rate -> honest absence, never 0%.
        lines.append(
            phrase(
                "dedup_estimation_unavailable", formula=formula, citation=citation
            )
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
                phrase(
                    "dedup_leading_channel",
                    label=ch_label,
                    conversions=_format_number(dedup_val),
                    share=part_str,
                    citation=citation,
                )
            )

    # Line 3 (optional extra): measured reconciliation for shopify source (review-17-5 fix-7).
    # Clearly distinguished from the aggregate estimation -- a transaction-level coverage.
    # Only present when data is available; absent → nothing (never a fake 0%).
    if measured_coverage_rate is not None:
        coverage_pct_str = f"{measured_coverage_rate:.1f} %".replace(".", ",")
        lines.append(
            phrase(
                "dedup_measured_coverage", pct=coverage_pct_str, citation=citation
            )
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
    dimension_labels: dict | None = None,
) -> str:
    """Médiaplan pacing card (Story 22.5): cited deterministic comment.

    ``dimension_labels`` is accepted (registry uniformity, F-10) and used by
    nothing: this card reads plan lines from the pacing marts, not a canonical
    dimension of ``fact_daily_kpi``.

    AD-9 / Epic 22 mandate (non-négociable) :
      * L'extrapolation est TOUJOURS étiquetée « Estimation ».
      * La formule de pace est citée (Pace = (actual − planned to-date) / planned to-date).
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
    if pull_ids:
        pull_citation = phrase(
            "pacing_citation_with_pull", version=version_str, pull_id=pull_ids[-1]
        )
    else:
        pull_citation = phrase("pacing_citation", version=version_str)

    lines: list[str] = []
    formula = phrase("pacing_formula")

    # --- Line 1: plan header + formule + couverture ----------------------------
    # Review 22.5 F-2: never a bare "?" in user-facing copy.
    as_of_str = as_of_day or phrase("pacing_as_of_unavailable")
    # Review 22.5 F-3 (leçon 17.5): coverage in DAYS of real data, not just the
    # as-of date. days_elapsed comes from the plan-level pacing row when present.
    days_elapsed = plan_rows[0].get("days_elapsed") if plan_rows else None
    coverage_str = (
        phrase("pacing_coverage_days", days=int(days_elapsed)) if days_elapsed else ""
    )
    lines.append(
        phrase(
            "pacing_plan_header",
            plan=plan_display,
            as_of=as_of_str,
            coverage=coverage_str,
            formula=formula,
            citation=pull_citation,
        )
    )

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
            pace_part = phrase(
                "pacing_overall",
                sign="+" if pace_pct >= 0 else "",
                pct=f"{pace_pct:.1f}",
                actual=(
                    phrase("pacing_amount", amount=f"{overall_actual:.0f}")
                    if overall_actual is not None
                    else phrase("pacing_amount_unavailable")
                ),
                budget=(
                    phrase("pacing_amount", amount=f"{overall_budget:.0f}")
                    if overall_budget is not None
                    else phrase("pacing_amount_unavailable")
                ),
            )

    plan_only_str: str | None = None
    if plan_only_keys:
        plan_only_labels = [str(k) for k in plan_only_keys[:5]]
        plan_only_str = ", ".join(f"« {k} »" for k in plan_only_labels)
        if len(plan_only_keys) > 5:
            plan_only_str += phrase(
                "pacing_plan_only_overflow", count=len(plan_only_keys) - 5
            )

    if pace_part and plan_only_str is not None:
        lines.append(
            phrase(
                "pacing_line_both",
                pace=pace_part,
                plan_only=phrase("pacing_plan_only", lines=plan_only_str),
                citation=pull_citation,
            )
        )
    elif pace_part:
        lines.append(
            phrase("pacing_line_single", part=pace_part, citation=pull_citation)
        )
    elif plan_only_str is not None:
        # The plan-only signal STANDS ALONE, so it opens the line and is
        # capitalised. It used to be produced by slicing the joined sentence back
        # apart on ": " -- a split that read French typography out of the string
        # and broke the day the phrase was translated. Two keys instead: the
        # capitalisation is a property of the language, and the language is the
        # catalogue's business.
        lines.append(
            phrase(
                "pacing_plan_only_standalone",
                lines=plan_only_str,
                citation=pull_citation,
            )
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
    context_scope: dict | None = None,
    context_unavailable: dict | None = None,
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

    NO ``dimension_labels`` PARAMETER, and that is measured rather than forgotten
    (story 27.9): this builder writes one line per METRIC and one per context event
    or alert, and names no dimension anywhere — so there is nothing here for a
    client label to rename. Threading the map in "just in case" would produce a
    call site that looks armed and is not, which is worse than the absence.
    ``test_narrative_speaks_the_client_word.py`` holds that claim: the day a line
    here names a dimension, it goes red and the map has to arrive.

    ``context_scope`` travels straight to :func:`_why_lines`. It is the caller's
    to build, because only the caller knows which events it scoped and how; see
    ``core.briefing.context_events_in_claim_scope``.
    """
    what = _what_lines(rollup, narrative_prompt)
    why = _why_lines(context_events, alerts, as_of, context_scope, context_unavailable)

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
        kept.append(phrase("narrative_truncated", event_count=n_events))
        return "\n".join(kept)

    # Section 1 fits; fill the remaining budget with Section 2, reserving one line
    # for the truncation note.
    budget = max_lines - len(what) - 1  # -1 for the truncation note line
    kept: list[str] = list(what)
    kept.append("")  # blank separator
    budget -= 1  # the blank line consumes budget too
    if budget > 0:
        kept.extend(why[:budget])
    kept.append(phrase("narrative_truncated", event_count=n_events))
    return "\n".join(kept[:max_lines])
