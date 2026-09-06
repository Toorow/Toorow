"""toorow — canonical AD-1 envelope builder (Story 1.5, T4).

Builds the structuredContent envelope consumed by the shared ui/shell.

# AD-1: canonical envelope shape — {schema_version, meta: {freshness, provenance, alerts[]}, data}
# AD-2: source-agnostic — no module-specific strings here.
"""

from __future__ import annotations

# Canonical schema version for the Story 1.5 envelope.
# NOTE: main.py uses "1.0.0" for the health/list_connectors envelope;
# the Story 1.5 spec (AC3) mandates "1" for the cross-connector data envelope.
# Both are valid per Architecture Spine — schema_version tracks the envelope
# contract, not a semver of the platform.
_SCHEMA_VERSION = "1"

#: The four keys an analytical-path declaration carries. Held as a strict set:
#: an extra key is a second vocabulary, a missing one is a disclosure that does
#: not disclose.
ANALYTICAL_PATH_KEYS = frozenset({"path", "relation", "governed_result", "note"})

#: `meta.dimension_labels[<identifier>].label_source` -- how the word a person
#: reads was obtained. It lives HERE, with the envelope contract that carries it,
#: because it is read by surfaces that must never reach the store: the narration
#: (AD-1: it receives resolved blocks, it never reads) puts these words in
#: sentences, and `scripts/check_narrative_no_raw.py` refuses it any import that
#: could read. `core.dimension_conformance` re-exports both names, so the store
#: side and the reading side spell one vocabulary once.
LABEL_SOURCE_CLIENT = "client"                 # a stored row won the cascade
LABEL_SOURCE_FALLBACK = "fallback_identifier"  # nothing stored -> the stable id is shown

#: THE NOTE IS SHORT ON PURPOSE, and the reason is measured (2026-08-16).
#:
#: `meta.analytical_path` weighed 318 bytes on EVERY card -- the single largest
#: item in `meta`, and the same paragraph on all of them. The model channel is
#: bounded at 4096 bytes; spending a quarter of a small card's budget to repeat
#: one constant sentence is what pushed `dedup` past the ceiling and left
#: `keywords` and `attribution` with under 100 bytes of room.
#:
#: What the note must carry is one fact: this figure did not come from a governed
#: Query Spec, so it and the Console's can disagree. That fits in a line. The
#: reasoning behind it belongs in the contract, not in every answer -- a host
#: reads it once, not once per card.
_UNGOVERNED_NOTE = (
    "Read from `{relation}`, not from a governed Query Spec; the Console's "
    "Explore path can disagree and neither reconciles the other."
)


def stored_dimension_labels(dimension_labels: dict | None) -> dict:
    """The `meta.dimension_labels` entries that carry a STORED word (AI-355).

    A `fallback_identifier` entry says only "nothing is stored -- show the
    identifier", about an identifier the reader already holds, and both readers
    of these words (`cards.dimension_word`, the narration) already refuse to
    trust it. 128 bytes of no-information on EVERY card is what pushed
    `keywords` past the model-channel ceiling (mcp-tool-surface.md, amendment
    2026-09-02). Every writer of `meta.dimension_labels` filters HERE -- a
    second writer with its own rule is how the two channels drifted apart.
    """
    return {
        key: value
        for key, value in (dimension_labels or {}).items()
        if (value or {}).get("label_source") != LABEL_SOURCE_FALLBACK
    }


def declare_analytical_path(*, path: str, relation: str, note: str | None = None) -> dict:
    """Declare WHICH relation produced a figure, on a path that is not governed.

    Every surface that emits a number outside `core.query_execution` reads SOME
    relation, and it is never the Datastream's published output relation. This is
    the one place that vocabulary is written, so a reader comparing two figures
    is comparing two declarations of the same shape rather than one declaration
    and one silence.

    `governed_result` is False here by construction, not by omission: a builder
    that could be asked to claim `True` would eventually be asked.
    """
    relation = str(relation or "").strip()
    if not relation:
        raise ValueError("declare_analytical_path: a path must name the relation it read")
    if not str(path or "").strip():
        raise ValueError("declare_analytical_path: a path must name its kind")
    return {
        "path": path,
        "relation": relation,
        "governed_result": False,
        "note": note or _UNGOVERNED_NOTE.format(relation=relation),
    }


#: The analytical path this builder serves. `core.query_execution` is the other
#: one; it pins `semantic_view_version_id`, `query_spec_version_id` and its own
#: `relation` on the Result manifest, and declares itself governed.
#:
#: IMPORT this constant; never retype `fact_daily_kpi`. Two hand-written copies
#: of a relation name is how a disclosure starts describing a path that moved.
ANALYTICAL_PATH_MART = declare_analytical_path(
    path="mart",
    relation="fact_daily_kpi",
    # Short for the reason `_UNGOVERNED_NOTE` states: this rides on every card,
    # and the model channel is 4096 bytes.
    note=(
        "Read from the aggregate mart, not from a governed Query Spec; the "
        "Console's Explore path can disagree and neither reconciles the other."
    ),
)


def build_envelope(*, meta: dict, data: dict, analytical_path: dict) -> dict:
    """The ONE constructor of an AD-1 envelope. Both surfaces come through here.

    Story 53.9 delivered `meta.analytical_path` to `build_canonical_envelope` and
    therefore to `get_daily_report` alone, because `core.cards` — the module
    `caveats-register.md:74` names as the mart side of CAV-17 — assembled its four
    envelopes as dict literals. `get_card` shipped a figure read from
    `fact_daily_kpi` with no word about which engine produced it, and no test
    could tell: the meta-key contract was pinned on the constructor, which
    `cards.py` never called.

    Anything the AD-1 contract gains from here on is stamped in THIS function, so
    it reaches every surface that emits a number. A hand-built envelope on this
    path is refused by
    `tests/core/test_two_surfaces_one_number.py::test_the_figure_bearing_path_builds_no_envelope_by_hand`.

    `analytical_path` is required, not defaulted: a surface that cannot say which
    relation it read must fail loudly rather than inherit the mart's answer.
    """
    if not isinstance(meta, dict):
        raise ValueError("build_envelope: meta must be a dict")
    if not isinstance(analytical_path, dict) or set(analytical_path) != set(ANALYTICAL_PATH_KEYS):
        raise ValueError(
            "build_envelope: analytical_path must carry exactly "
            f"{sorted(ANALYTICAL_PATH_KEYS)}; build it with declare_analytical_path()"
        )
    if not str(analytical_path.get("relation") or "").strip():
        raise ValueError("build_envelope: an envelope must name the relation it read")
    if not isinstance(analytical_path.get("governed_result"), bool):
        raise ValueError("build_envelope: governed_result must be declared, never inferred")

    sealed = dict(meta)
    alerts = sealed.get("alerts")
    if alerts is None:
        sealed["alerts"] = []  # always a list, never null (Architecture Spine)
    elif not isinstance(alerts, list):
        raise ValueError("build_envelope: meta.alerts must be a list")
    # WHICH analytical path produced these numbers (story 53.9, CAV-17).
    #
    # Toorow answers the same business question through two paths that read two
    # different relations: this one reads an ungoverned relation, while
    # `core.query_execution` reads the Datastream's own published output relation
    # and pins a Semantic View version and a Query Spec version. When they
    # disagree, a person comparing a figure in chat with the same figure in the
    # Console has today no way to see that they were not asking the same engine.
    # `visualization-and-rendering.md:347` records the missing Result IDENTITY;
    # the possible divergence of VALUE is recorded nowhere.
    #
    # Reconciling the two paths is architectural work this field does not attempt.
    # What it does is make the difference VISIBLE and comparable -- including to
    # Test, which can now tell a semantic disagreement from a rendering one.
    sealed["analytical_path"] = dict(analytical_path)

    return {"schema_version": _SCHEMA_VERSION, "meta": sealed, "data": data}


def build_canonical_envelope(
    rows: list[dict],
    meta_freshness: dict,
    meta_provenance: list[dict] | dict,
    date_range: dict,
    connectors: list[str],
    report_profile: str = "standard_daily",
    confidence: dict | None = None,
    branding: dict | None = None,
    dimension_labels: dict | None = None,
) -> dict:
    """Build the canonical AD-1 structuredContent envelope.

    Parameters
    ----------
    rows:
        Full dataset (all fact_daily_kpi rows for the query scope).
    meta_freshness:
        Dict: ``{"last_pull": <ISO str>, "cadence_hours": 24, "stale_since": null}``
    meta_provenance:
        Dict or list[dict] for multi-connector responses.
        Shape: ``{"source_system": str, "pull_id": str}``
        Typed as ``list[dict]`` to support the future multi-connector extension
        (Architecture Spine §Canonical Envelope note).
    date_range:
        Dict with ``start`` and ``end`` ISO-8601 date strings.
    connectors:
        List of connector names covered by this report.
    report_profile:
        Widget report profile identifier (default: ``"standard_daily"``).
    confidence:
        Optional confidence dict for AD-9 groundwork (Story 3.5).
        When provided, set as ``meta["confidence"]``.  When None, the key is
        omitted entirely (AD-1 additive key; existing callers unaffected, HG-6).
        Shape: the three NAMED terms `core.confidence` measures --
        ``{"completeness" | "per_connector", "freshness", "provenance",
        "unknown_terms", "limiting_term", ...}``. There is no scalar over them:
        `proactive-assertions.md` ("Incomplete if": *a single score merges
        evidence of different natures*) refuses one, and the `score` key that
        used to travel here through `reporting_mcp` and `reports` is removed.
        A reader takes the weakest term by NAME, never a product.
    branding:
        Optional org branding dict from ``core.branding.resolve_org_branding``
        (Story 23.1, AI-31 additive key). When provided, set as
        ``meta["branding"]`` so the widget shell merges the org colors into its
        ThemeProvider. When None the key is OMITTED entirely — the widget
        renders the default toorow theme (never ``null``, AC1).
    dimension_labels:
        Optional ``{canonical_dimension -> {display_label, description,
        scope_level, label_source}}`` from
        ``core.dimension_lineage.resolve_report_dimension_labels`` (Story 27.9,
        additive key). The client owns the NAME of a dimension while the product
        owns its stable identifier, and until this key existed the label was
        stored and read by nobody. Empty/None -> the key is OMITTED entirely
        (never ``null``), exactly like ``branding``.

    Returns
    -------
    dict
        Canonical envelope matching the AD-1 contract exactly.
    """
    meta: dict = {
        "freshness": meta_freshness,
        "provenance": meta_provenance,
        "alerts": [],  # always a list, never null (Architecture Spine)
    }
    if confidence is not None:
        meta["confidence"] = confidence
    if branding is not None:
        meta["branding"] = branding
    stored = stored_dimension_labels(dimension_labels)
    if stored:
        meta["dimension_labels"] = stored

    # `meta.analytical_path` is stamped by `build_envelope`, not here: stamping it
    # here is exactly what made it a `get_daily_report` field for the whole of
    # story 53.9 while `get_card` shipped a mart figure that named no engine.
    return build_envelope(
        meta=meta,
        data={
            "report_profile": report_profile,
            "date_range": date_range,
            "connectors": connectors,
            "rows": rows,
        },
        analytical_path=ANALYTICAL_PATH_MART,
    )


def derive_meta_from_rows(rows: list[dict], connectors: list[str]) -> tuple[dict, list[dict]]:
    """Derive freshness and provenance from raw fact_daily_kpi rows.

    Returns (meta_freshness, meta_provenance_list).

    meta_freshness shape:
        {"last_pull": <ISO str | None>, "cadence_hours": 24, "stale_since": null}

    meta_provenance_list shape (one entry per connector):
        [{"source_system": str, "pull_id": str}]
    """
    # Aggregate per connector
    pull_ids: dict[str, str] = {}
    freshness_timestamps: dict[str, str] = {}

    for row in rows:
        connector = row.get("connector") or "unknown"
        pull_id = row.get("pull_id") or ""
        loaded_at = str(row.get("loaded_at") or "")

        if pull_id > pull_ids.get(connector, ""):
            pull_ids[connector] = pull_id
        if loaded_at > freshness_timestamps.get(connector, ""):
            freshness_timestamps[connector] = loaded_at

    # Freshness across connectors.
    #
    # `last_pull` is the NEWEST load across every contributing connector, and on a
    # cross-source figure that is the optimistic end of the range: one connector
    # refreshed minutes ago hides one frozen for days, because the figure adds
    # both. `overview.md:63` states the rule for the Project posture -- *"the
    # latest interval complete across every required active input under policy,
    # never the newest timestamp from one isolated source"* -- and `core.project_overview`
    # obeys it with `min(verified_dates)`. This builder is the other surface, and
    # it disagreed silently.
    #
    # `last_pull` keeps its meaning (consumers read it), and two honest companions
    # are added: `complete_through`, the OLDEST contributing load -- the point
    # through which every source has actually reported -- and `per_connector`, so
    # the spread is inspectable instead of collapsed.
    all_timestamps = [v for v in freshness_timestamps.values() if v]
    last_pull = max(all_timestamps) if all_timestamps else None
    complete_through = min(all_timestamps) if all_timestamps else None

    meta_freshness = {
        "last_pull": last_pull,
        "complete_through": complete_through,
        "per_connector": dict(sorted(freshness_timestamps.items())),
        "cadence_hours": 24,
        # NOT a claim of freshness. This builder does not evaluate staleness; only
        # `core.health_enrichment` does, and it is reached from `get_daily_report`
        # alone -- never from `get_card` / `get_report`, the surfaces that get
        # frozen into a Render and shared. `stale_since_evaluated` says which of
        # the two situations a reader is in, so an unevaluated null stops reading
        # as "evaluated, and fresh" (README.md:123, invariant 8).
        "stale_since": None,
        "stale_since_evaluated": False,
    }

    # Per-connector provenance list (NFR8 AC5 Story 2.7: include source_field)
    covered = connectors if connectors else sorted(pull_ids.keys())
    meta_provenance: list[dict] = [
        {
            "source_system": connector,
            # The relation is IMPORTED from the declaration, never retyped.
            "source_field": ANALYTICAL_PATH_MART["relation"],
            "pull_id": pull_ids.get(connector),
        }
        for connector in covered
    ]

    return meta_freshness, meta_provenance
